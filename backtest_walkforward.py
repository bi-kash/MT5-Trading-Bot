"""Walk-forward backtest — split history into N periods, run on each, aggregate.

Why this exists
---------------
A single backtest gives one performance number per symbol. That number can
be misleading: a symbol with PF=2.0 may have made all its profit in one lucky
window. A symbol with PF=1.3 may be profitable in 9 out of 10 windows — far
more trustworthy.

For each symbol this script:
  1. Fetches full LTF + HTF history
  2. Splits LTF into N equal-bar periods, walking back from most recent
  3. Runs a full backtest on each period independently
  4. Saves per-period results to  backtest_results/period_NN_of_NN.csv
  5. Aggregates stability metrics into  backtest_walkforward_summary.csv

Key stability metrics in the summary:
  * consistency_pct   — % of periods with PF > 1.0  (THE key number)
  * mean_pf ± std_pf  — average performance and variability
  * min_pf / max_pf   — worst-case and best-case window
  * stability_score   — mean_pf × (consistency_pct / 100)
                        Use this to rank symbols for live trading.

Usage:
    python backtest_walkforward.py
    python backtest_walkforward.py --periods 12 --bars 8000
    python backtest_walkforward.py --symbol EURUSD
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Tuple

import numpy as np
import pandas as pd

from backtest import (
    CsvWriter,
    _categorise,
    _get_instrument,
    _resolve_spread_ticks,
    _resolve_symbol_list,
    load_mt5,
    run_backtest,
)
from config import settings
from logger import get_logger

log = get_logger("walkforward")

# enough lead-in for the EMA-200 + ATR/ADX buffers to stabilise
WARMUP_BARS = 300


def split_periods(n_bars: int, n_periods: int, bars_per_period: int,
                  warmup: int) -> List[Tuple[int, int]]:
    """Return [(start_idx, end_idx), ...] — oldest period first.

    Walks back from the end of history. Drops periods that would not have
    enough warmup data in front of them.
    """
    periods: List[Tuple[int, int]] = []
    end = n_bars
    for i in range(n_periods):
        p_end = end - i * bars_per_period
        p_start = p_end - bars_per_period
        if p_start - warmup < 0:
            break
        periods.append((p_start, p_end))
    return list(reversed(periods))


def walkforward_symbol(symbol: str, equity: float, n_periods: int,
                       bars_per_period: int, connector, cache=None) -> List[dict]:
    """Run walk-forward on one symbol. Returns list of per-period summary dicts."""
    inst = _get_instrument(connector, symbol)
    if inst is None:
        return []

    spread_ticks = _resolve_spread_ticks(inst, settings.backtest_spread_pips)

    total_bars = n_periods * bars_per_period + WARMUP_BARS
    htf_bars = max(2000, total_bars // 4)

    try:
        ltf = load_mt5(symbol, settings.timeframe, total_bars, connector, cache=cache)
        htf = load_mt5(symbol, settings.higher_timeframe, htf_bars, connector, cache=cache)
    except Exception as exc:
        log.warning("[%s] data fetch failed: %s", symbol, exc)
        return []

    if len(ltf) < bars_per_period + WARMUP_BARS:
        log.warning("[%s] only %d bars (need %d) — skipping",
                    symbol, len(ltf), bars_per_period + WARMUP_BARS)
        return []

    periods = split_periods(len(ltf), n_periods, bars_per_period, WARMUP_BARS)
    if not periods:
        return []

    results: List[dict] = []
    for i, (start_idx, end_idx) in enumerate(periods, 1):
        slice_start = max(0, start_idx - WARMUP_BARS)
        ltf_slice = ltf.iloc[slice_start:end_idx].copy()
        slice_end_time = ltf_slice.index[-1]
        htf_slice = htf[htf.index <= slice_end_time]

        if len(htf_slice) < 250:
            continue

        bt = run_backtest(ltf_slice, htf_slice, inst=inst,
                          starting_equity=equity, spread_ticks=spread_ticks)
        summary = bt.summary(equity)
        summary["symbol"] = symbol
        summary["category"] = _categorise(inst.path)
        summary["tick_size"] = inst.tick_size
        summary["spread_ticks"] = round(spread_ticks, 2)
        summary["period"] = i
        # The "real" period start (after warmup buffer)
        period_start_pos = WARMUP_BARS if slice_start > 0 else (start_idx - slice_start)
        if period_start_pos < len(ltf_slice):
            summary["period_start"] = ltf_slice.index[period_start_pos].isoformat()
        else:
            summary["period_start"] = ltf_slice.index[0].isoformat()
        summary["period_end"] = slice_end_time.isoformat()
        results.append(summary)

    return results


def aggregate_symbol(symbol: str, period_results: List[dict]) -> dict:
    """Combine all period results for one symbol into stability metrics."""
    empty = {
        "symbol": symbol, "category": "",
        "periods_tested": 0, "periods_profitable": 0, "consistency_pct": 0.0,
        "mean_pf": 0.0, "median_pf": 0.0, "std_pf": 0.0,
        "min_pf": 0.0, "max_pf": 0.0,
        "mean_expectancy_r": 0.0, "std_expectancy_r": 0.0,
        "mean_trades": 0, "total_trades": 0,
        "mean_win_rate": 0.0, "worst_dd_pct": 0.0,
        "stability_score": 0.0,
    }
    if not period_results:
        return empty

    tested = len(period_results)
    profitable = sum(
        1 for r in period_results
        if r["trades"] > 0 and r["profit_factor"] > 1.0
    )

    # PF/expectancy stats only on periods that actually traded
    active = [r for r in period_results if r["trades"] > 0]
    pfs = [r["profit_factor"] for r in active if r["profit_factor"] < 999]
    expectancies = [r["expectancy_r"] for r in active]
    trades = [r["trades"] for r in active]
    dds = [r["max_drawdown_pct"] for r in active]
    win_rates = [r["win_rate"] for r in active]

    consistency = profitable / tested if tested > 0 else 0.0
    mean_pf = float(np.mean(pfs)) if pfs else 0.0

    return {
        "symbol": symbol,
        "category": period_results[0].get("category", ""),
        "periods_tested": tested,
        "periods_profitable": profitable,
        "consistency_pct": round(consistency * 100, 1),
        "mean_pf": round(mean_pf, 2),
        "median_pf": round(float(np.median(pfs)), 2) if pfs else 0.0,
        "std_pf": round(float(np.std(pfs)), 2) if pfs else 0.0,
        "min_pf": round(float(np.min(pfs)), 2) if pfs else 0.0,
        "max_pf": round(float(np.max(pfs)), 2) if pfs else 0.0,
        "mean_expectancy_r": round(float(np.mean(expectancies)), 3) if expectancies else 0.0,
        "std_expectancy_r": round(float(np.std(expectancies)), 3) if expectancies else 0.0,
        "mean_trades": round(float(np.mean(trades)), 1) if trades else 0,
        "total_trades": int(sum(trades)),
        "mean_win_rate": round(float(np.mean(win_rates)), 1) if win_rates else 0.0,
        "worst_dd_pct": round(float(np.max(dds)), 2) if dds else 0.0,
        "stability_score": round(mean_pf * consistency, 3),
    }


def main() -> None:
    p = argparse.ArgumentParser(description="Walk-forward backtest across N time periods")
    p.add_argument("--symbol", help="Test a single symbol only")
    p.add_argument("--periods", type=int, default=settings.walkforward_periods,
                   help=f"Number of periods (default: {settings.walkforward_periods})")
    p.add_argument("--bars", type=int, default=settings.walkforward_bars_per_period,
                   help=f"Bars per period (default: {settings.walkforward_bars_per_period})")
    args = p.parse_args()

    n_periods = args.periods
    bars_per_period = args.bars
    equity = settings.backtest_equity
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    output_dir = Path(settings.walkforward_output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_path = Path(settings.walkforward_summary_csv)

    from mt5_connector import MT5Connector
    connector = MT5Connector()
    if not connector.connect():
        log.error("MT5 connect failed")
        sys.exit(1)

    cache = None
    if settings.use_data_cache:
        from data_cache import DataCache
        cache = DataCache(connector=connector)

    try:
        symbols = _resolve_symbol_list(connector, args.symbol)
    except Exception as exc:
        log.error("Symbol discovery failed: %s", exc)
        connector.shutdown()
        sys.exit(1)

    if not symbols:
        print("No symbols to backtest.")
        connector.shutdown()
        return

    print(f"\nWalk-forward backtest: {stamp}")
    print(f"  Symbols : {len(symbols)}")
    print(f"  Periods : {n_periods} × {bars_per_period} bars "
          f"({settings.timeframe}) per period")
    print(f"  Total   : ~{n_periods * bars_per_period:,} bars per symbol")
    print(f"  Equity  : {equity:,.0f}")
    print(f"  Cache   : {'on (' + settings.cache_dir + ')' if cache is not None else 'off'}")
    print(f"  Per-period CSVs: {output_dir.resolve()}")
    print(f"  Summary       : {summary_path.resolve()}\n")

    # One writer per period file
    period_writers: List[CsvWriter] = [
        CsvWriter(output_dir / f"period_{i:02d}_of_{n_periods:02d}.csv", stamp)
        for i in range(1, n_periods + 1)
    ]

    summary_rows: List[dict] = []
    completed = 0
    skipped: List[str] = []
    col_w = max((len(s) for s in symbols), default=8)

    try:
        for idx, sym in enumerate(symbols, 1):
            print(f"[{idx:>4}/{len(symbols)}] {sym:<{col_w}}", end="  ", flush=True)
            try:
                periods = walkforward_symbol(
                    sym, equity, n_periods, bars_per_period, connector, cache=cache,
                )
            except Exception as exc:
                print(f"CRASH — {exc}")
                skipped.append(f"{sym}: {exc}")
                continue

            if not periods:
                print("SKIP — not enough data")
                skipped.append(f"{sym}: insufficient bars")
                continue

            for r in periods:
                pidx = r["period"] - 1
                if pidx < len(period_writers):
                    period_writers[pidx].write(r)

            agg = aggregate_symbol(sym, periods)
            summary_rows.append(agg)
            completed += 1

            # Re-sort and re-flush summary after every symbol — safe to Ctrl+C
            summary_rows.sort(key=lambda r: r.get("stability_score", 0), reverse=True)
            pd.DataFrame(summary_rows).to_csv(summary_path, index=False)

            print(
                f"tested={agg['periods_tested']:>2} "
                f"profitable={agg['periods_profitable']:>2}  "
                f"cons={agg['consistency_pct']:>5.1f}%  "
                f"PF={agg['mean_pf']:>4.2f}±{agg['std_pf']:.2f}  "
                f"trades={agg['total_trades']:>4}  "
                f"score={agg['stability_score']:>5.2f}"
            )
    except KeyboardInterrupt:
        print(f"\n\nInterrupted after {completed} symbols — partial results saved.")
    finally:
        connector.shutdown()

    if completed == 0:
        print("\nNo results.")
        return

    print(f"\n{completed} symbols → {summary_path.resolve()}")
    if skipped:
        print(f"{len(skipped)} skipped")

    # Top-10 by stability_score
    min_periods = max(3, n_periods // 2)
    qualified = [r for r in summary_rows if r.get("periods_tested", 0) >= min_periods]
    qualified.sort(key=lambda r: r.get("stability_score", 0), reverse=True)

    print(f"\n{'=' * 92}")
    print(f"  TOP SYMBOLS BY STABILITY SCORE  (require ≥{min_periods} periods tested)")
    print(f"{'=' * 92}")
    if not qualified:
        print(f"  (no symbol had ≥{min_periods} periods — try smaller --bars or --periods)")
        return

    print(f"  {'symbol':<12} {'cat':<14} {'tested':>6} {'prof':>5} "
          f"{'cons%':>6} {'mean_PF':>8} {'std_PF':>7} {'min_PF':>7} "
          f"{'trades':>7} {'score':>6}")
    for r in qualified[:10]:
        print(
            f"  {r['symbol']:<12} {str(r.get('category', ''))[:14]:<14} "
            f"{r['periods_tested']:>6} {r['periods_profitable']:>5} "
            f"{r['consistency_pct']:>6.1f} {r['mean_pf']:>8.2f} "
            f"{r['std_pf']:>7.2f} {r['min_pf']:>7.2f} "
            f"{r['total_trades']:>7} {r['stability_score']:>6.2f}"
        )

    print("\n  → Pick symbols with high consistency_pct AND high mean_pf.")
    print(f"  → Then run: python allocate.py --csv {summary_path}  to weight them.")


if __name__ == "__main__":
    main()
