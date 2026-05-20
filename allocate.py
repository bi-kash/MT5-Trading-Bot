"""Weighted symbol allocation from backtest_results.csv.

Reads the backtest output, filters by quality thresholds, and recommends a
weighted SYMBOLS list for the live bot. The weight combines edge size,
sample reliability, and drawdown control:

    score  = max(0, expectancy_r) * sqrt(trades) * (1 - max_dd_pct/100)
    weight = score / sum(scores across qualifying symbols)

Why this formula:
  * expectancy_r   — captures the average $/trade edge in R-multiples
  * sqrt(trades)   — rewards statistical reliability without over-weighting
                     symbols that just happened to fire many times
  * (1 - dd_pct)   — penalises symbols that, while profitable, dug deep
                     drawdowns that would have spooked you in real life

Usage:
    python allocate.py
    python allocate.py --top 10 --min-pf 1.3 --min-trades 60
    python allocate.py --csv backtest_results.csv --max-dd 10
"""
from __future__ import annotations

import argparse
import csv
import math
from pathlib import Path

from config import settings


def _f(v, default=0.0) -> float:
    try:
        return float(v)
    except (ValueError, TypeError):
        return default


def _i(v, default=0) -> int:
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return default


def main() -> None:
    p = argparse.ArgumentParser(description="Weighted symbol picker from backtest_results.csv")
    p.add_argument("--csv", default=settings.backtest_output_csv,
                   help="Backtest CSV path (default: from .env)")
    p.add_argument("--top", type=int, default=10,
                   help="Show top-N symbols (default 10)")
    p.add_argument("--min-pf", type=float, default=1.2,
                   help="Minimum profit factor (default 1.2)")
    p.add_argument("--min-trades", type=int, default=50,
                   help="Minimum trade count for statistical reliability (default 50)")
    p.add_argument("--min-expectancy", type=float, default=0.05,
                   help="Minimum expectancy_r per trade (default 0.05)")
    p.add_argument("--max-dd", type=float, default=15.0,
                   help="Maximum acceptable max_drawdown_pct (default 15)")
    args = p.parse_args()

    path = Path(args.csv)
    if not path.exists():
        print(f"ERROR: {path} not found. Run `python backtest.py` first.")
        return

    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            sym  = r.get("symbol", "").strip()
            if not sym:
                continue
            trades = _i(r.get("trades"))
            pf     = _f(r.get("profit_factor"))
            exp    = _f(r.get("expectancy_r"))
            dd     = _f(r.get("max_drawdown_pct"))

            if trades < args.min_trades:    continue
            if pf < args.min_pf:            continue
            if exp < args.min_expectancy:   continue
            if dd > args.max_dd:            continue

            score = max(0.0, exp) * math.sqrt(trades) * max(0.0, 1.0 - dd / 100.0)
            rows.append({
                "symbol": sym, "category": r.get("category", ""),
                "trades": trades, "pf": pf, "exp": exp, "dd": dd,
                "score": score,
                "backtested_at": r.get("backtested_at", ""),
            })

    if not rows:
        print(f"No symbols passed the filters in {path}.")
        print(f"Filters: trades>={args.min_trades}  PF>={args.min_pf}  "
              f"E>={args.min_expectancy}R  DD<={args.max_dd}%")
        print("Try relaxing them, e.g. --min-pf 1.0 --min-trades 30")
        return

    total = sum(r["score"] for r in rows) or 1.0
    for r in rows:
        r["weight"] = r["score"] / total
    rows.sort(key=lambda r: r["weight"], reverse=True)
    rows = rows[: args.top]

    # Re-normalise weights to sum to 1 across the chosen top-N
    sub_total = sum(r["weight"] for r in rows) or 1.0
    for r in rows:
        r["weight"] /= sub_total

    print(f"\nSource     : {path.resolve()}")
    print(f"Filters    : trades>={args.min_trades}  PF>={args.min_pf}  "
          f"E>={args.min_expectancy}R  DD<={args.max_dd}%")
    print(f"Selected   : {len(rows)} symbols\n")

    print(f"  {'symbol':<12} {'category':<10} {'trades':>6} {'PF':>6} "
          f"{'E(R)':>7} {'DD%':>6} {'weight':>8} {'risk%':>7}")
    base_risk = settings.risk_per_trade
    for r in rows:
        per_trade_risk = base_risk * r["weight"] * len(rows)
        print(f"  {r['symbol']:<12} {r['category'][:10]:<10} "
              f"{r['trades']:>6} {r['pf']:>6.2f} {r['exp']:>+7.3f} "
              f"{r['dd']:>6.1f} {r['weight']*100:>7.1f}% "
              f"{per_trade_risk:>6.3f}%")

    print("\n" + "=" * 70)
    print("  Copy this line to your .env to live-trade these symbols:")
    print("=" * 70)
    print(f"SYMBOLS={','.join(r['symbol'] for r in rows)}")

    print("\n  For weighted position sizing, set RISK_PER_TRADE to the LOWEST")
    print(f"  per-symbol risk above ({min(base_risk * r['weight'] * len(rows) for r in rows):.3f}%)")
    print("  — the bot will then risk the same dollar amount per trade across")
    print("  all symbols, but you can manually scale up the strong performers.")


if __name__ == "__main__":
    main()
