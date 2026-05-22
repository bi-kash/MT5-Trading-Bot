"""Weighted symbol allocation from backtest results.

Auto-detects which CSV schema you point it at:

  * backtest_results.csv (from backtest.py — single-window)
        score = max(0, expectancy_r) * sqrt(trades) * (1 - max_dd_pct/100)

  * backtest_walkforward_summary.csv (from backtest_walkforward.py)
        score = stability_score * sqrt(total_trades / 50)
        (where stability_score = mean_pf × consistency_pct already)
        — far more reliable: a symbol that's profitable in 8 of 10 periods
        is ranked above one that's profitable in only 1 lucky period.

Usage:
    python allocate.py                                        # single-window
    python allocate.py --csv backtest_walkforward_summary.csv # walk-forward
    python allocate.py --top 10 --min-pf 1.3 --min-trades 60
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


def _detect_schema(path: Path) -> str:
    """Return 'walkforward' if file has stability_score column, else 'single'."""
    with open(path, newline="") as f:
        header = next(csv.reader(f), [])
    return "walkforward" if "stability_score" in header else "single"


def _load_single(path: Path, args) -> list:
    """Original single-window backtest schema."""
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            sym = r.get("symbol", "").strip()
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
                "info": f"{r.get('backtested_at', '')}",
            })
    return rows


def _load_walkforward(path: Path, args) -> list:
    """Walk-forward summary schema — filters on consistency + mean PF."""
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            sym = r.get("symbol", "").strip()
            if not sym:
                continue
            tested        = _i(r.get("periods_tested"))
            consistency   = _f(r.get("consistency_pct"))
            mean_pf       = _f(r.get("mean_pf"))
            mean_exp      = _f(r.get("mean_expectancy_r"))
            total_trades  = _i(r.get("total_trades"))
            worst_dd      = _f(r.get("worst_dd_pct"))
            stability     = _f(r.get("stability_score"))

            if tested < args.min_periods:        continue
            if consistency < args.min_consistency: continue
            if mean_pf < args.min_pf:            continue
            if mean_exp < args.min_expectancy:   continue
            if total_trades < args.min_trades:   continue
            if worst_dd > args.max_dd:           continue

            # Boost score by trade-count reliability (relative to 50 trades)
            score = stability * math.sqrt(max(1, total_trades) / 50.0)
            rows.append({
                "symbol": sym, "category": r.get("category", ""),
                "trades": total_trades, "pf": mean_pf, "exp": mean_exp,
                "dd": worst_dd, "score": score,
                "info": f"cons={consistency:.0f}% periods={tested} std_PF={_f(r.get('std_pf')):.2f}",
            })
    return rows


def main() -> None:
    p = argparse.ArgumentParser(description="Weighted symbol picker from backtest CSV")
    p.add_argument("--csv", default=settings.backtest_output_csv,
                   help="Backtest CSV path (default: from .env)")
    p.add_argument("--top", type=int, default=10,
                   help="Show top-N symbols (default 10)")
    p.add_argument("--min-pf", type=float, default=1.2,
                   help="Min profit_factor (single) / mean_pf (walkforward). Default 1.2")
    p.add_argument("--min-trades", type=int, default=50,
                   help="Min trades (single) / total_trades (walkforward). Default 50")
    p.add_argument("--min-expectancy", type=float, default=0.05,
                   help="Min expectancy_r per trade. Default 0.05")
    p.add_argument("--max-dd", type=float, default=15.0,
                   help="Max acceptable drawdown %. Default 15")
    p.add_argument("--min-consistency", type=float, default=60.0,
                   help="[walkforward only] Min consistency_pct (60 = profitable in 6 of 10 periods)")
    p.add_argument("--min-periods", type=int, default=5,
                   help="[walkforward only] Min periods_tested required")
    args = p.parse_args()

    path = Path(args.csv)
    if not path.exists():
        print(f"ERROR: {path} not found.")
        print("Run `python backtest.py` or `python backtest_walkforward.py` first.")
        return

    schema = _detect_schema(path)
    print(f"\nSource : {path.resolve()}")
    print(f"Schema : {schema}\n")

    if schema == "walkforward":
        rows = _load_walkforward(path, args)
        filt = (f"min_pf>={args.min_pf}  cons>={args.min_consistency}%  "
                f"periods>={args.min_periods}  trades>={args.min_trades}  "
                f"E>={args.min_expectancy}R  DD<={args.max_dd}%")
    else:
        rows = _load_single(path, args)
        filt = (f"trades>={args.min_trades}  PF>={args.min_pf}  "
                f"E>={args.min_expectancy}R  DD<={args.max_dd}%")

    print(f"Filters: {filt}")

    if not rows:
        print(f"\nNo symbols passed the filters.")
        print("Try relaxing them — e.g. --min-pf 1.0 --min-trades 20")
        if schema == "walkforward":
            print("                          --min-consistency 50 --min-periods 3")
        return

    total = sum(r["score"] for r in rows) or 1.0
    for r in rows:
        r["weight"] = r["score"] / total
    rows.sort(key=lambda r: r["weight"], reverse=True)
    rows = rows[: args.top]

    sub_total = sum(r["weight"] for r in rows) or 1.0
    for r in rows:
        r["weight"] /= sub_total

    print(f"Selected: {len(rows)} symbols\n")
    print(f"  {'symbol':<12} {'category':<10} {'trades':>7} {'PF':>6} "
          f"{'E(R)':>7} {'DD%':>6} {'weight':>8} {'risk%':>7}  {'info'}")
    base_risk = settings.risk_per_trade
    for r in rows:
        per_trade_risk = base_risk * r["weight"] * len(rows)
        print(f"  {r['symbol']:<12} {r['category'][:10]:<10} "
              f"{r['trades']:>7} {r['pf']:>6.2f} {r['exp']:>+7.3f} "
              f"{r['dd']:>6.1f} {r['weight'] * 100:>7.1f}% "
              f"{per_trade_risk:>6.3f}%  {r['info']}")

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
