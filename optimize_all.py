"""Run per-symbol parameter optimization for every configured symbol.

Two-phase design
----------------
Phase 1 — Data loading (sequential, single MT5 connection):
    All symbol data is fetched / read from the disk cache in the main process.
    MT5 is not thread-safe, so this step is intentionally sequential.

Phase 2 — Optimization (concurrent, up to --workers subprocesses):
    Each symbol's Optuna study runs independently in its own subprocess.
    Subprocesses each have their own copy of ``settings`` — no shared global
    state, no race conditions from ``_temp_settings()``.
    Detailed trial output goes to  OPTIMIZED_PARAMS_DIR/<SYMBOL>_optimize.log
    so the console stays clean.  The main process prints a one-liner per symbol
    as it completes.

Usage:
    python optimize_all.py                                # all SYMBOLS, 4 workers
    python optimize_all.py --symbols EURUSD,NZDUSD        # specific subset
    python optimize_all.py --bars 100000 --trials 300     # more thorough
    python optimize_all.py --workers 8                    # more parallelism (max 15)
    python optimize_all.py --no-validate                  # skip OOS check (faster)
    python optimize_all.py --skip-existing                # resume after Ctrl+C

Recommended one-liner for an overnight run:
    python optimize_all.py --bars 100000 --trials 300 --workers 8
"""
from __future__ import annotations

import argparse
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# NOTE: Heavy imports (config, optimize, MT5) are deferred to inside main()
# so that spawned worker sub-processes don't trigger module-level side-effects
# (e.g. the "[config] Optimized params loaded…" print) on the console.


# ── Worker (must be module-level so ProcessPoolExecutor can pickle it) ─────── #

def _optimize_worker(job: dict) -> Optional[dict]:
    """Subprocess worker: optimizes ONE symbol and saves its JSON result.

    stdout is redirected to OPTIMIZED_PARAMS_DIR/<SYMBOL>_optimize.log BEFORE
    importing optimize/config so that all startup messages and trial logs land
    in the file rather than interleaving on the console.
    """
    import sys as _sys
    from pathlib import Path as _Path

    symbol   = job["symbol"]
    out_path = _Path(job["output_path"])
    log_path = out_path.parent / f"{symbol}_optimize.log"

    # Open log file and redirect stdout NOW (before importing config / optuna)
    log_fh       = open(log_path, "w", encoding="utf-8", buffering=1)  # noqa: WPS515
    _orig_stdout = _sys.stdout
    _sys.stdout  = log_fh

    try:
        # Import happens here → config/optuna startup prints go to the log
        from optimize import optimize_symbol as _opt

        result = _opt(
            symbol      = symbol,
            bars        = job["bars"],
            trials      = job["trials"],
            train_pct   = job["train_pct"],
            periods     = job["periods"],
            no_validate = job["no_validate"],
            equity      = job["equity"],
            connector   = None,    # data already loaded; no MT5 access in workers
            cache       = None,
            output_path = out_path,
            train_data  = job["train_data"],   # pre-loaded in Phase 1
            test_data   = job["test_data"],
        )
    finally:
        _sys.stdout = _orig_stdout
        log_fh.close()

    return result


# ── Summary table ──────────────────────────────────────────────────────────── #

def _print_summary(results: List[Dict[str, Any]], out_dir: Path) -> None:
    if not results:
        print("\n  No results to display.")
        return

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    div   = "\u2500" * 72

    print(f"\n\n{'=' * 78}")
    print(f"  PER-SYMBOL OPTIMIZATION SUMMARY  \u2014  {stamp}")
    print(f"  {len(results)} symbol(s) completed")
    print(f"{'=' * 78}")
    print(
        f"  {'Symbol':<12} {'Train':>7} {'Test':>7} {'Ratio':>8}  "
        f"{'ef':>4} {'es':>4} {'adx':>4} {'sl':>5} {'tp':>5}  Status"
    )
    print(f"  {div}")

    for r in sorted(results, key=lambda x: x.get("train_score", 0), reverse=True):
        p  = r.get("params", {})
        tr = r.get("train_score", 0.0)
        ts = r.get("test_score")
        ov = r.get("overfitting_ratio")

        ratio_str = f"{ov:.2%}" if ov is not None else "  N/A  "
        if ov is None:
            status = "no-validate"
        elif ov >= 0.75:
            status = "\u2713 Robust"
        elif ov >= 0.50:
            status = "\u26a0 Moderate"
        else:
            status = "\u2717 Overfit"

        print(
            f"  {r.get('symbol', '?'):<12} {tr:>7.4f} {(ts or 0.0):>7.4f} {ratio_str:>8}  "
            f"{p.get('ema_fast', 0):>4} {p.get('ema_slow', 0):>4} "
            f"{p.get('adx_threshold', 0):>4.0f} {p.get('sl_atr_mult', 0):>5.2f} "
            f"{p.get('tp_atr_mult', 0):>5.2f}  {status}"
        )

    print(f"\n  Saved to : {out_dir.resolve()}/")
    print(f"  Logs     : {out_dir}/<SYMBOL>_optimize.log  (per-symbol trial details)")
    print(f"  Usage    : set USE_OPTIMIZED_PARAMS=true in .env")
    print(f"             the bot auto-loads per-symbol params from that directory")


# ── Main ───────────────────────────────────────────────────────────────────── #

def main() -> None:
    # Defer heavy imports — workers inherit this module's namespace but we don't
    # want module-level imports triggering prints 15 times on the console.
    from config  import settings
    from logger  import get_logger
    from optimize import PARAM_DEFS, _load_symbol_data

    log = get_logger("optimize_all")

    ap = argparse.ArgumentParser(
        description=(
            "Per-symbol optimization: data loads sequentially (MT5-safe), "
            "Optuna studies run concurrently in subprocesses."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python optimize_all.py                                # all cached symbols, 4 workers
  python optimize_all.py --symbols EURUSD,NZDUSD        # subset
  python optimize_all.py --bars 100000 --trials 300     # more thorough
  python optimize_all.py --workers 8                    # 8 parallel workers
  python optimize_all.py --no-validate                  # skip OOS check
  python optimize_all.py --skip-existing                # resume after interrupt
""",
    )
    ap.add_argument("--symbols",
                    help="Comma-separated symbols (default: all cached symbols, or SYMBOLS from .env)")
    ap.add_argument("--trials",  type=int, default=300,
                    help="Optuna trials per symbol (default: 300)")
    ap.add_argument("--bars",    type=int, default=50_000,
                    help="LTF bars per symbol (default: 50000)")
    ap.add_argument("--train-pct", type=float, default=0.7,
                    help="Training fraction; rest is held-out test (default: 0.7)")
    ap.add_argument("--periods", type=int, default=4,
                    help="WFO sub-periods per trial (default: 4)")
    ap.add_argument("--no-validate", action="store_true",
                    help="Skip out-of-sample validation (faster)")
    ap.add_argument("--output-dir", default=settings.optimized_params_dir,
                    help=f"Output directory (default: {settings.optimized_params_dir})")
    ap.add_argument("--workers", type=int, default=16,
                    help="Parallel workers for optimization, 1\u201315 (default: 4)")
    ap.add_argument("--skip-existing", action="store_true", default=False,
                    help="Skip symbols already having a JSON in OUTPUT_DIR (resume mode)")
    args = ap.parse_args()

    n_workers = max(1, min(args.workers, 15))
    out_dir   = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    equity    = settings.backtest_equity

    if args.symbols:
        symbols: List[str] = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        symbols = []
        cache_dir = Path(settings.cache_dir)
        if cache_dir.exists():
            for pkl in cache_dir.glob("*_M1.pkl"):
                sym = pkl.name.replace("_M1.pkl", "")
                symbols.append(sym)
        
        symbols = sorted(list(set(symbols)))
        if symbols:
            print(f"Auto-detected {len(symbols)} symbols from {cache_dir.name}/")
        else:
            print("No cached data found. Defaulting to SYMBOLS from .env.")
            symbols = list(settings.symbols)

    if not symbols:
        print("No symbols found. Populate data_cache or pass --symbols.")
        sys.exit(1)

    # Resume mode: skip already-finished symbols
    if args.skip_existing:
        done_syms = [s for s in symbols if (out_dir / f"{s}.json").exists()]
        symbols   = [s for s in symbols if s not in done_syms]
        if done_syms:
            print(f"  Skipping already-optimised: {', '.join(done_syms)}")
        if not symbols:
            print("All symbols done. Drop --skip-existing to re-run.")
            return

    actual_workers = min(n_workers, len(symbols))
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    est_h = len(symbols) * args.trials * 0.003 / 60 / actual_workers  # rough

    print(f"\nPer-Symbol Parameter Optimization  \u2014  {stamp}")
    print(f"  Symbols    : {', '.join(symbols)}")
    print(f"  Workers    : {actual_workers} concurrent (Phase 2; Phase 1 is sequential)")
    print(f"  Trials     : {args.trials} per symbol  (Optuna TPE / Bayesian)")
    print(f"  History    : {args.bars:,} {settings.timeframe} bars per symbol")
    print(f"  Split      : train {args.train_pct:.0%}  /  test {1 - args.train_pct:.0%}")
    print(f"  WFO prd    : {args.periods} sub-periods per trial")
    print(f"  Output dir : {out_dir.resolve()}")
    print(f"  Searching  : {', '.join(PARAM_DEFS)}")
    print(f"  Validate   : {'no (--no-validate)' if args.no_validate else 'yes (held-out slice)'}")
    print(f"  Est. time  : ~{est_h:.1f} h  (rough; depends on CPU speed)")
    print(f"  Trial logs : {out_dir}/<SYMBOL>_optimize.log")
    print()

    # ── Phase 1: Load all data sequentially (MT5 is not process/thread-safe) #
    from mt5_connector import MT5Connector
    connector = MT5Connector()
    if not connector.connect():
        log.error("MT5 connect failed")
        sys.exit(1)

    cache = None
    if settings.use_data_cache:
        from data_cache import DataCache
        cache = DataCache(connector=connector)
        print(f"  Cache : on ({settings.cache_dir})\n")

    print(f"Phase 1/2  \u2014  Loading data for {len(symbols)} symbol(s)  (sequential, MT5-safe)")
    print(f"  {'Symbol':<12}  {'Train bars':>12}  {'Test bars':>10}")
    print(f"  {'-'*40}")

    all_symbol_data: Dict[str, tuple] = {}
    failed: List[str] = []

    try:
        for sym in symbols:
            print(f"  {sym:<12}  ", end="", flush=True)
            td, vd = _load_symbol_data([sym], args.bars, args.train_pct, connector, cache)
            if td:
                n_train = len(td[0][0])
                n_test  = len(vd[0][0]) if vd else 0
                all_symbol_data[sym] = (td, vd)
                print(f"{n_train:>12,}  {n_test:>10,}")
            else:
                print("        no data  (skipping)")
                failed.append(sym)
    finally:
        connector.shutdown()
        print(f"\n  MT5 connection closed.")

    if not all_symbol_data:
        print("\nNo usable data loaded \u2014 cannot optimise.")
        sys.exit(1)

    # ── Phase 2: Optimise concurrently (subprocesses — no shared state) ──── #
    actual_workers = min(actual_workers, len(all_symbol_data))
    print(
        f"\nPhase 2/2  \u2014  Optimizing {len(all_symbol_data)} symbol(s)  "
        f"[{actual_workers} parallel worker(s)]\n"
        f"  Detailed trial output \u2192 <SYMBOL>_optimize.log\n"
    )

    jobs: List[dict] = [
        {
            "symbol":      sym,
            "bars":        args.bars,
            "trials":      args.trials,
            "train_pct":   args.train_pct,
            "periods":     args.periods,
            "no_validate": args.no_validate,
            "equity":      equity,
            "output_path": str(out_dir / f"{sym}.json"),
            "train_data":  td,
            "test_data":   vd,
        }
        for sym, (td, vd) in all_symbol_data.items()
    ]

    all_results: List[Dict[str, Any]] = []
    done_count = 0
    total      = len(jobs)

    try:
        with ProcessPoolExecutor(max_workers=actual_workers) as pool:
            future_to_sym = {pool.submit(_optimize_worker, job): job["symbol"] for job in jobs}
            for future in as_completed(future_to_sym):
                sym        = future_to_sym[future]
                done_count += 1
                try:
                    res = future.result()
                    if res:
                        all_results.append(res)
                        ov = res.get("overfitting_ratio")
                        if ov is None:
                            tag = "saved"
                        elif ov >= 0.75:
                            tag = "\u2713 Robust"
                        elif ov >= 0.50:
                            tag = "\u26a0 Moderate"
                        else:
                            tag = "\u2717 Overfit"
                        print(
                            f"  [{done_count:>2}/{total}] {sym:<12}  {tag:<12}  "
                            f"train={res['train_score']:.4f}  "
                            f"test={(res.get('test_score') or 0.0):.4f}  "
                            f"\u2192 {sym}.json",
                            flush=True,
                        )
                    else:
                        failed.append(sym)
                        print(
                            f"  [{done_count:>2}/{total}] {sym:<12}  SKIPPED (no data)",
                            flush=True,
                        )
                except Exception as exc:
                    failed.append(sym)
                    log.error("[%s] worker failed: %s", sym, exc, exc_info=True)
                    print(
                        f"  [{done_count:>2}/{total}] {sym:<12}  FAILED "
                        f"\u2014 see {sym}_optimize.log for traceback",
                        flush=True,
                    )

    except KeyboardInterrupt:
        print(f"\n\n  Interrupted. {done_count}/{total} symbols completed.")
        print("  Already-saved results remain intact.")
        print("  Re-run with --skip-existing to continue where you left off.")

    if failed:
        print(f"\n  Skipped / failed : {', '.join(failed)}")

    _print_summary(all_results, out_dir)


if __name__ == "__main__":
    main()
