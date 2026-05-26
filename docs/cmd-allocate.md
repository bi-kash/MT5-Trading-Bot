# `python allocate.py` — Symbol allocation

Reads backtest results, applies quality filters, and prints a ranked list of
symbols with suggested portfolio weights. Outputs a ready-to-paste
`SYMBOLS=...` line for `.env`.

Works with **both** `backtest_results.csv` (single window) and
`backtest_walkforward_summary.csv` (walk-forward). The schema is
auto-detected.

---

## Quick start

```bash
# From walk-forward results (recommended)
python allocate.py --csv backtest_walkforward_summary.csv

# From single-window results
python allocate.py --csv backtest_results.csv
```

---

## Commands

### Walk-forward summary (recommended)

```bash
python allocate.py --csv backtest_walkforward_summary.csv \
                   --min-consistency 70 --min-pf 1.2
```

Uses `stability_score = mean_pf × (consistency_pct / 100)` as the rank.
Add `--min-consistency 70` to require profitable in ≥ 7 of 10 periods.

### Single-window results

```bash
python allocate.py --csv backtest_results.csv \
                   --min-pf 1.2 --min-trades 50 --max-dd 15
```

### Stricter filtering

```bash
python allocate.py --csv backtest_walkforward_summary.csv \
                   --min-consistency 80 \
                   --min-pf 1.3 \
                   --min-trades 100 \
                   --max-dd 10 \
                   --top 5
```

---

## All options

| Flag | Default | Description |
|------|---------|-------------|
| `--csv PATH` | `backtest_results.csv` | Input results file |
| `--top N` | `10` | Max number of symbols to return |
| `--min-pf F` | `1.1` | Minimum profit factor (or mean_pf for WF) |
| `--min-trades N` | `20` | Minimum total trades |
| `--max-dd F` | `20.0` | Maximum drawdown % |
| `--min-consistency F` | `0` | Min consistency % (walk-forward only) |

---

## Weight formula

```
score  = max(0, expectancy_r) × √trades × (1 − max_dd_pct/100)
weight = score / Σ scores
```

Three independent dimensions:
- `expectancy_r` — edge per trade
- `√trades` — statistical reliability (diminishing returns on high counts)
- `(1 − dd_pct)` — penalises symbols that would shake you out in real life

---

## Example output

```
Qualified symbols (5 of 12 passed filters):
─────────────────────────────────────────────────────────────────
  #  symbol      pf     exp_r  trades   dd%    weight   risk%
  1  XAUUSD    1.54    +0.28   149     8.8%    28.4%    0.14%
  2  VIXM      1.54    +0.25   113     3.9%    23.1%    0.12%
  3  OILK      1.44    +0.20   117     3.9%    20.3%    0.10%
  4  GASS      1.40    +0.21   160     3.8%    19.8%    0.10%
  5  EURUSD    1.26    +0.15   128     3.7%     8.4%    0.04%
─────────────────────────────────────────────────────────────────

Paste into .env:
SYMBOLS=XAUUSD,VIXM,OILK,GASS,EURUSD
```

---

## Notes

- The `risk%` column is a *suggestion* — the bot uses a uniform
  `RISK_PER_TRADE` for all symbols. To apply per-symbol risk, manually
  set `RISK_PER_TRADE` to the lowest per-symbol risk shown.
- Pick symbols from **diverse categories** — don't trade five energy
  instruments simultaneously; high correlation multiplies drawdown.
- Re-run after every significant parameter or strategy change.
