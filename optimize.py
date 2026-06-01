"""Parameter optimization for the trading bot using Optuna (Bayesian/TPE search).

How it works
------------
For each Optuna trial a parameter set is suggested. We score it by running a
mini walk-forward (N equal sub-periods) on the TRAINING slice of history (70%
by default). The objective = mean stability_score across all live SYMBOLS,
where stability_score = mean_pf × (profitable_periods / total_periods).

After finding the best trial, the best params are validated on the HELD-OUT
TEST slice (last 30%) — a window never seen during optimization. A large
train/test gap indicates overfitting.

Results are saved to optimized_params.json.
Set USE_OPTIMIZED_PARAMS=true in .env to apply them to the live bot and all
backtest/walk-forward runs.

Only 8 high-impact parameters are searched. Fixing the rest (EMA_TREND=200,
ATR_PERIOD=14, RSI_PERIOD=14, etc.) keeps the search space manageable and
reduces overfitting.

Install once:
    pip install optuna

Usage:
    python optimize.py                          # optimize all live SYMBOLS
    python optimize.py --symbol USDJPY          # single symbol
    python optimize.py --trials 300             # number of search trials
    python optimize.py --bars 50000             # history bars per symbol
    python optimize.py --train-pct 0.7          # training split (default 0.7)
    python optimize.py --periods 4              # WFO sub-periods per trial
    python optimize.py --no-validate            # skip out-of-sample check
    python optimize.py --output custom.json     # custom output path
"""
from __future__ import annotations

import argparse
import json
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

try:
    import optuna
    optuna.logging.set_verbosity(optuna.logging.WARNING)
except ImportError:
    print("ERROR: Optuna not installed. Run:  pip install optuna")
    sys.exit(1)

from backtest import _get_instrument, _resolve_spread_ticks, load_mt5, run_backtest
from config import settings
from logger import get_logger

log = get_logger("optimize")

OUTPUT_FILE = Path("optimized_params.json")
WARMUP = 300  # bars reserved for indicator warm-up before each period

# ── Parameter search space ─────────────────────────────────────────────── #
# Only high-impact params. Fewer dimensions = less overfitting.
# Format: name -> (type, low, high)
PARAM_DEFS: Dict[str, Tuple[str, Any, Any]] = {
    "ema_fast":              ("int",   8,    30 ),
    "ema_slow":              ("int",   30,   60 ),   # must stay > ema_fast (pruned if not)
    "adx_threshold":         ("int",   15,   35 ),
    "sl_atr_mult":           ("float", 0.8,  2.5),
    "tp_atr_mult":           ("float", 1.5,  3.5),
    "atr_regime_percentile": ("int",   0,    30 ),
    "trailing_atr_mult":     ("float", 0.5,  3.0),
    "breakeven_at_r":        ("float", 0.3,  2.5),
}


# ── Temporary settings override ────────────────────────────────────────── #

@contextmanager
def _temp_settings(**overrides):
    """Temporarily patch global `settings` for a single backtest trial."""
    originals = {}
    for k, v in overrides.items():
        if hasattr(settings, k):
            originals[k] = getattr(settings, k)
            setattr(settings, k, v)
    try:
        yield
    finally:
        for k, v in originals.items():
            setattr(settings, k, v)


# ── Walk-forward scoring ───────────────────────────────────────────────── #

def _score_slice(
    ltf: pd.DataFrame,
    htf: pd.DataFrame,
    inst,
    spread_ticks: float,
    n_periods: int,
    equity: float,
) -> Tuple[float, int]:
    """Mini walk-forward on a data slice. Returns (stability_score, total_trades)."""
    n_bars = len(ltf)
    bars_per_period = (n_bars - WARMUP) // n_periods
    if bars_per_period < 300:
        return 0.0, 0

    period_pfs: List[float] = []
    total_trades = 0

    for i in range(n_periods):
        p_start = WARMUP + i * bars_per_period
        p_end = p_start + bars_per_period
        if p_end > n_bars:
            break

        ltf_slice = ltf.iloc[p_start - WARMUP : p_end].copy()
        slice_end_time = ltf_slice.index[-1]
        htf_slice = htf[htf.index <= slice_end_time]

        if len(htf_slice) < 50:
            continue

        try:
            bt = run_backtest(
                ltf_slice, htf_slice,
                inst=inst, starting_equity=equity, spread_ticks=spread_ticks,
            )
            s = bt.summary(equity)
        except Exception:
            continue

        if s["trades"] > 0 and s["profit_factor"] < 999:
            period_pfs.append(s["profit_factor"])
            total_trades += s["trades"]

    if not period_pfs or total_trades < 5:
        return 0.0, total_trades

    mean_pf = float(np.mean(period_pfs))
    consistency = sum(1 for pf in period_pfs if pf > 1.0) / len(period_pfs)
    return round(mean_pf * consistency, 4), total_trades


# ── Optuna objective ───────────────────────────────────────────────────── #

def _objective(
    trial: optuna.Trial,
    train_data: List[Tuple],
    n_periods: int,
    equity: float,
) -> float:
    ema_fast = trial.suggest_int("ema_fast", *PARAM_DEFS["ema_fast"][1:])
    ema_slow = trial.suggest_int("ema_slow", *PARAM_DEFS["ema_slow"][1:])
    if ema_fast >= ema_slow:
        raise optuna.TrialPruned()

    sl_atr = trial.suggest_float("sl_atr_mult", *PARAM_DEFS["sl_atr_mult"][1:])
    tp_atr = trial.suggest_float("tp_atr_mult", *PARAM_DEFS["tp_atr_mult"][1:])
    # Mild R:R guard — TP should be at least 0.9 × SL to avoid pathological setups
    if tp_atr < sl_atr * 0.9:
        raise optuna.TrialPruned()

    params = {
        "ema_fast": ema_fast,
        "ema_slow": ema_slow,
        "adx_threshold": float(trial.suggest_int("adx_threshold", *PARAM_DEFS["adx_threshold"][1:])),
        "sl_atr_mult": sl_atr,
        "tp_atr_mult": tp_atr,
        "atr_regime_percentile": trial.suggest_int("atr_regime_percentile",
                                                    *PARAM_DEFS["atr_regime_percentile"][1:]),
        "trailing_atr_mult": trial.suggest_float("trailing_atr_mult",
                                                  *PARAM_DEFS["trailing_atr_mult"][1:]),
        "breakeven_at_r": trial.suggest_float("breakeven_at_r",
                                              *PARAM_DEFS["breakeven_at_r"][1:]),
    }

    with _temp_settings(**params):
        scores = []
        for ltf, htf, inst, spread_ticks in train_data:
            score, _ = _score_slice(ltf, htf, inst, spread_ticks, n_periods, equity)
            scores.append(score)

    return float(np.mean(scores)) if scores else 0.0


# ── Data loading ───────────────────────────────────────────────────────── #

def _load_symbol_data(
    symbols: List[str],
    bars: int,
    train_pct: float,
    connector,
    cache,
) -> Tuple[List[Tuple], List[Tuple]]:
    """Load LTF + HTF per symbol, split into TRAIN and TEST slices."""
    htf_bars = max(2000, bars // 4)
    train_data: List[Tuple] = []
    test_data: List[Tuple] = []

    for sym in symbols:
        inst = _get_instrument(connector, sym)
        if inst is None:
            log.warning("[%s] instrument not found — skipping", sym)
            continue

        spread_ticks = _resolve_spread_ticks(inst, settings.backtest_spread_pips)

        try:
            ltf = load_mt5(sym, settings.timeframe, bars, connector, cache=cache)
            htf = load_mt5(sym, settings.higher_timeframe, htf_bars, connector, cache=cache)
        except Exception as exc:
            log.warning("[%s] data load failed: %s", sym, exc)
            continue

        if len(ltf) < WARMUP + 1000:
            log.warning("[%s] only %d bars — skipping", sym, len(ltf))
            continue

        split_idx = int(len(ltf) * train_pct)
        ltf_train = ltf.iloc[:split_idx]
        ltf_test = ltf.iloc[max(0, split_idx - WARMUP):]

        split_time = ltf.index[split_idx - 1]
        htf_train = htf[htf.index <= split_time]

        train_data.append((ltf_train, htf_train, inst, spread_ticks))
        test_data.append((ltf_test, htf, inst, spread_ticks))

        log.info(
            "[%s] %d bars → train=%d (%s–%s)  test=%d (%s–%s)",
            sym, len(ltf),
            len(ltf_train), ltf_train.index[0].strftime("%Y-%m"), ltf_train.index[-1].strftime("%Y-%m"),
            len(ltf_test) - WARMUP, ltf_test.index[WARMUP].strftime("%Y-%m"), ltf_test.index[-1].strftime("%Y-%m"),
        )

    return train_data, test_data


# ── Best params extraction ─────────────────────────────────────────────── #

def _extract_best_params(study: optuna.Study) -> Dict[str, Any]:
    p = study.best_trial.params
    return {
        "ema_fast":              int(p["ema_fast"]),
        "ema_slow":              int(p["ema_slow"]),
        "adx_threshold":         float(p["adx_threshold"]),
        "sl_atr_mult":           round(float(p["sl_atr_mult"]), 2),
        "tp_atr_mult":           round(float(p["tp_atr_mult"]), 2),
        "atr_regime_percentile": int(p["atr_regime_percentile"]),
        "trailing_atr_mult":     round(float(p["trailing_atr_mult"]), 2),
        "breakeven_at_r":        round(float(p["breakeven_at_r"]), 2),
    }


# ── Single-symbol optimization (callable from optimize_all.py) ─────────── #

def optimize_symbol(
    symbol: str,
    bars: int,
    trials: int,
    train_pct: float,
    periods: int,
    no_validate: bool,
    equity: float,
    connector,
    cache,
    output_path: Path,
    *,
    train_data: Optional[List] = None,
    test_data:  Optional[List] = None,
) -> Optional[Dict[str, Any]]:
    """Optimize parameters for a single *symbol* and save the result to *output_path*.

    The *connector* is used for data loading only and is **not** shut down by
    this function — the caller owns the connector lifecycle.

    Pre-loaded *train_data* / *test_data* can be supplied (e.g. when running in
    parallel via ``optimize_all.py``) to skip the MT5 data-loading step entirely.

    Returns the result ``dict`` on success, or ``None`` if no usable data was
    found for the symbol.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    train_bars_approx = int(bars * train_pct)
    test_bars_approx  = bars - train_bars_approx

    if train_data is None:
        print(f"  Loading data for {symbol} …", flush=True)
        train_data, test_data = _load_symbol_data([symbol], bars, train_pct, connector, cache)

    if not train_data:
        print(f"  [SKIP {symbol}] No usable data — skipping")
        return None

    print(
        f"  Data ready  ({train_bars_approx:,} train / {test_bars_approx:,} test bars). "
        f"Starting {trials} trials…\n",
        flush=True,
    )

    study = optuna.create_study(
        direction="maximize",
        study_name=f"opt_{symbol}",
        sampler=optuna.samplers.TPESampler(seed=42),
        pruner=optuna.pruners.MedianPruner(),
    )

    completed = [0]

    def _cb(st: optuna.Study, tr: optuna.Trial) -> None:  # noqa: ARG001
        completed[0] += 1
        n = completed[0]
        if n % 25 == 0 or n == trials:
            best   = st.best_value if st.best_value is not None else 0.0
            best_t = st.best_trial.number if st.best_trial else "?"
            print(
                f"  [{symbol}] Trial {n:>4}/{trials}  "
                f"best_score={best:.4f}  (trial #{best_t})",
                flush=True,
            )

    study.optimize(
        lambda t: _objective(t, train_data, periods, equity),
        n_trials=trials,
        callbacks=[_cb],
    )

    if study.best_trial is None:
        print(f"  [FAIL {symbol}] No successful trials")
        return None

    best_params = _extract_best_params(study)
    train_score = round(study.best_value, 4)

    print(f"\n  [{symbol}] Best params  (train_score={train_score:.4f})")
    for k, v in best_params.items():
        orig = getattr(settings, k, "?")
        tag  = "  ← changed" if str(orig) != str(v) else ""
        print(f"    {k:<28} {str(v):<10}  (was {orig}){tag}")

    # ── Out-of-sample validation ───────────────────────────────────────── #
    test_score: Optional[float] = None
    test_periods = max(2, periods // 2)

    if not no_validate and test_data:
        print(
            f"\n  [{symbol}] Validating on held-out test slice "
            f"({1 - train_pct:.0%}, {test_periods} periods)…"
        )
        with _temp_settings(**best_params):
            ts_list = []
            for ltf, htf, inst, spread_ticks in test_data:
                s, trades = _score_slice(ltf, htf, inst, spread_ticks, test_periods, equity)
                ts_list.append((s, trades))
                log.info("[%s] test: score=%.4f trades=%d", inst.name, s, trades)
        test_score = round(float(np.mean([s for s, _ in ts_list])), 4)
        ratio = test_score / train_score if train_score > 0 else 0.0
        print(
            f"  [{symbol}] Train={train_score:.4f}  Test={test_score:.4f}  "
            f"Ratio={ratio:.2%}  ",
            end="",
        )
        if ratio >= 0.75:
            print("✓  Robust")
        elif ratio >= 0.50:
            print("⚠  Moderate degradation — monitor live carefully")
        else:
            print("✗  Large drop — likely overfitting; try more --bars")

    # ── Save JSON ─────────────────────────────────────────────────────── #
    result: Dict[str, Any] = {
        "optimized_at":      stamp,
        "symbol":            symbol,
        "timeframe":         settings.timeframe,
        "higher_timeframe":  settings.higher_timeframe,
        "strategy_variant":  settings.strategy_variant,
        "trials_run":        trials,
        "history_bars":      bars,
        "train_pct":         train_pct,
        "wfo_periods_train": periods,
        "wfo_periods_test":  test_periods,
        "train_score":       train_score,
        "test_score":        test_score,
        "overfitting_ratio": (
            round(test_score / train_score, 3) if test_score and train_score else None
        ),
        "params": best_params,
        "param_space": {
            k: {"type": v[0], "low": v[1], "high": v[2]}
            for k, v in PARAM_DEFS.items()
        },
        "usage": (
            "Applied automatically when USE_OPTIMIZED_PARAMS=true in .env "
            "and this file is in OPTIMIZED_PARAMS_DIR."
        ),
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"\n  [{symbol}] ✓  Saved → {output_path.resolve()}")

    return result


# ── Main ───────────────────────────────────────────────────────────────── #

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Optimize bot parameters via Optuna walk-forward search",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python optimize.py                      # optimize all live SYMBOLS, 300 trials
  python optimize.py --symbol USDJPY      # single symbol
  python optimize.py --trials 150         # faster run
  python optimize.py --bars 100000        # more history (requires cache)
  python optimize.py --no-validate        # skip out-of-sample check
""",
    )
    ap.add_argument("--symbol", help="Single symbol (default: all live SYMBOLS from .env)")
    ap.add_argument("--trials", type=int, default=300, help="Optuna trials (default: 300)")
    ap.add_argument("--bars", type=int, default=50_000, help="LTF bars per symbol (default: 50000)")
    ap.add_argument("--train-pct", type=float, default=0.7,
                    help="Training fraction; rest is held-out test (default: 0.7)")
    ap.add_argument("--periods", type=int, default=4,
                    help="WFO sub-periods per trial scoring (default: 4)")
    ap.add_argument("--no-validate", action="store_true",
                    help="Skip out-of-sample validation on test slice")
    ap.add_argument("--output", default=str(OUTPUT_FILE),
                    help=f"Output JSON file (default: {OUTPUT_FILE})")
    ap.add_argument("--per-symbol", action="store_true",
                    help="Optimize each symbol independently; saves to OUTPUT_DIR/SYMBOL.json")
    ap.add_argument("--output-dir", default=settings.optimized_params_dir,
                    help=(
                        f"Directory for per-symbol JSON files (used with --per-symbol; "
                        f"default: {settings.optimized_params_dir})"
                    ))
    args = ap.parse_args()

    output_path = Path(args.output)
    equity = settings.backtest_equity
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    symbols = [args.symbol] if args.symbol else list(settings.symbols)
    if not symbols:
        print("No symbols configured. Set SYMBOLS in .env.")
        sys.exit(1)

    from mt5_connector import MT5Connector
    connector = MT5Connector()
    if not connector.connect():
        log.error("MT5 connect failed")
        sys.exit(1)

    cache = None
    if settings.use_data_cache:
        from data_cache import DataCache
        cache = DataCache(connector=connector)

    train_bars_approx = int(args.bars * args.train_pct)
    test_bars_approx = args.bars - train_bars_approx

    print(f"\nParameter Optimization  —  {stamp}")
    print(f"  Symbols   : {', '.join(symbols)}")
    print(f"  Trials    : {args.trials}  (Optuna TPE / Bayesian)")
    print(f"  History   : {args.bars:,} {settings.timeframe} bars per symbol")
    print(f"  Split     : train {args.train_pct:.0%} (~{train_bars_approx:,} bars)  "
          f"test {1 - args.train_pct:.0%} (~{test_bars_approx:,} bars)")
    print(f"  WFO prd   : {args.periods} sub-periods per trial (training window only)")
    print(f"  Searching : {', '.join(PARAM_DEFS)}")
    print(f"  Output    : {output_path.resolve()}")
    print(f"  Cache     : {'on (' + settings.cache_dir + ')' if cache else 'off'}")
    print()

    # ── Per-symbol mode: each symbol optimised independently ───────────── #
    if args.per_symbol:
        out_dir = Path(args.output_dir)
        all_results: List[Dict[str, Any]] = []
        for idx, sym in enumerate(symbols, 1):
            print(f"\n{'=' * 60}")
            print(f"  [{idx}/{len(symbols)}] {sym}")
            print(f"{'=' * 60}")
            res = optimize_symbol(
                symbol=sym,
                bars=args.bars,
                trials=args.trials,
                train_pct=args.train_pct,
                periods=args.periods,
                no_validate=args.no_validate,
                equity=equity,
                connector=connector,
                cache=cache,
                output_path=out_dir / f"{sym}.json",
            )
            if res:
                all_results.append(res)
        connector.shutdown()
        if all_results:
            print(f"\n\n{'=' * 72}")
            print(f"  PER-SYMBOL SUMMARY  ({len(all_results)} completed)")
            print(f"{'=' * 72}")
            print(f"  {'Symbol':<12} {'Train':>7} {'Test':>7} {'Ratio':>8}  "
                  f"{'ef':>4} {'es':>4} {'adx':>4} {'sl':>5} {'tp':>5}")
            _div = "─" * 62
            print(f"  {_div}")
            for r in all_results:
                p  = r["params"]
                ov = r.get("overfitting_ratio")
                rs = f"{ov:.2%}" if ov is not None else "  N/A  "
                print(
                    f"  {r['symbol']:<12} {r['train_score']:>7.4f} "
                    f"{(r.get('test_score') or 0.0):>7.4f} {rs:>8}  "
                    f"{p['ema_fast']:>4} {p['ema_slow']:>4} "
                    f"{p['adx_threshold']:>4.0f} {p['sl_atr_mult']:>5.2f} "
                    f"{p['tp_atr_mult']:>5.2f}"
                )
            print(f"\n  Saved to : {out_dir.resolve()}/")
            print(f"  To use   : set USE_OPTIMIZED_PARAMS=true in .env")
        return

    print("Loading historical data...", flush=True)
    train_data, test_data = _load_symbol_data(symbols, args.bars, args.train_pct, connector, cache)
    connector.shutdown()

    if not train_data:
        print("No usable data loaded — cannot optimize.")
        sys.exit(1)

    actual_symbols = len(train_data)
    print(f"Data ready for {actual_symbols} symbol(s). Starting {args.trials} trials...\n")

    study = optuna.create_study(
        direction="maximize",
        study_name="bot_param_opt",
        sampler=optuna.samplers.TPESampler(seed=42),
        pruner=optuna.pruners.MedianPruner(),
    )

    completed = [0]

    def _cb(study: optuna.Study, trial: optuna.Trial) -> None:
        completed[0] += 1
        n = completed[0]
        if n % 25 == 0 or n == args.trials:
            best = study.best_value if study.best_value is not None else 0.0
            best_trial = study.best_trial.number if study.best_trial else "?"
            print(
                f"  Trial {n:>4}/{args.trials}  "
                f"best_score={best:.4f}  (trial #{best_trial})",
                flush=True,
            )

    study.optimize(
        lambda t: _objective(t, train_data, args.periods, equity),
        n_trials=args.trials,
        callbacks=[_cb],
    )

    best_params = _extract_best_params(study)
    train_score = round(study.best_value, 4)

    print(f"\n{'─' * 60}")
    print(f"BEST PARAMETERS  (train stability_score = {train_score:.4f})")
    print(f"{'─' * 60}")
    for k, v in best_params.items():
        orig = getattr(settings, k, "?")
        tag = "  ← changed" if str(orig) != str(v) else ""
        print(f"  {k:<28} {str(v):<10}  (was {orig}){tag}")

    # ── Out-of-sample validation ───────────────────────────────────────── #
    test_score: Optional[float] = None
    test_periods = max(2, args.periods // 2)

    if not args.no_validate and test_data:
        print(f"\nValidating on held-out test slice ({1 - args.train_pct:.0%} of data, "
              f"{test_periods} periods)...")
        with _temp_settings(**best_params):
            ts_list = []
            for ltf, htf, inst, spread_ticks in test_data:
                s, trades = _score_slice(ltf, htf, inst, spread_ticks, test_periods, equity)
                ts_list.append((s, trades))
                log.info("[%s] test: score=%.4f trades=%d", inst.name, s, trades)
        test_score = round(float(np.mean([s for s, _ in ts_list])), 4)
        ratio = test_score / train_score if train_score > 0 else 0.0

        print(f"\n  Train score  : {train_score:.4f}")
        print(f"  Test score   : {test_score:.4f}")
        print(f"  Test / Train : {ratio:.2%}  ", end="")
        if ratio >= 0.75:
            print("✓  Robust — small overfitting gap")
        elif ratio >= 0.50:
            print("⚠  Moderate degradation — monitor live carefully")
        else:
            print("✗  Large drop — likely overfitting. Try --bars with more data or fewer symbols.")

    # ── Save JSON ──────────────────────────────────────────────────────── #
    result = {
        "optimized_at": stamp,
        "symbols": symbols,
        "timeframe": settings.timeframe,
        "higher_timeframe": settings.higher_timeframe,
        "strategy_variant": settings.strategy_variant,
        "trials_run": args.trials,
        "history_bars": args.bars,
        "train_pct": args.train_pct,
        "wfo_periods_train": args.periods,
        "wfo_periods_test": test_periods,
        "train_score": train_score,
        "test_score": test_score,
        "overfitting_ratio": (
            round(test_score / train_score, 3) if test_score and train_score else None
        ),
        "params": best_params,
        "param_space": {
            k: {"type": v[0], "low": v[1], "high": v[2]}
            for k, v in PARAM_DEFS.items()
        },
        "usage": (
            "Set USE_OPTIMIZED_PARAMS=true in .env to apply these params. "
            "They override .env defaults for the live bot and all backtest runs."
        ),
    }
    output_path.write_text(json.dumps(result, indent=2), encoding="utf-8")

    print(f"\n✓  Saved → {output_path.resolve()}")
    print(f"   To use: set USE_OPTIMIZED_PARAMS=true in .env")
    print(f"   To view current state: python optimize.py --no-validate --trials 1  "
          f"(or just inspect {output_path.name})")


if __name__ == "__main__":
    main()
