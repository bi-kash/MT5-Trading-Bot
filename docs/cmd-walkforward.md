# `python backtest_walkforward.py` — Walk-forward backtest

Splits history into N equal time periods and runs the full backtest on each
independently. Aggregates stability metrics per symbol. Far more reliable than
a single-window backtest for deciding which symbols to trade live.

---

## The problem it solves

A single backtest gives one PF number. That number can be misleading:

- **PF = 2.0** might mean all profit came from 2 great weeks in 10 months
- **PF = 1.3** might mean profitable in 9 out of 10 periods — far more reliable

Walk-forward testing reveals which is which. The key metric is
`consistency_pct`: the percentage of periods where PF > 1.0.

---

## Quick start

```bash
python backtest_walkforward.py
```

Uses `WALKFORWARD_PERIODS`, `WALKFORWARD_BARS_PER_PERIOD`, and `SYMBOLS`
from `.env`. Scans the same symbol list as `backtest.py`.

---

## Commands

```bash
# Default run (10 periods × 10,000 bars each)
python backtest_walkforward.py

# Custom periods and bar count
python backtest_walkforward.py --periods 12 --bars 8000

# Single symbol
python backtest_walkforward.py --symbol EURUSD
```

---

## All options

| Flag | Default | Description |
|------|---------|-------------|
| `--symbol NAME` | all symbols | Test a single symbol only |
| `--periods N` | `WALKFORWARD_PERIODS` (10) | Number of time windows |
| `--bars N` | `WALKFORWARD_BARS_PER_PERIOD` (10000) | LTF bars per window |

---

## Output files

```
backtest_results/
├── period_01_of_10.csv     # Results for all symbols in the oldest window
├── period_02_of_10.csv
├── ...
└── period_10_of_10.csv     # Results for all symbols in the most recent window
backtest_walkforward_summary.csv   # One row per symbol with stability metrics
```

The summary CSV is updated and written after **every symbol** — safe to
interrupt with Ctrl+C; partial results are preserved.

---

## Key metrics in the summary

| Column | What it tells you | Target |
|--------|-------------------|--------|
| `consistency_pct` | **% of periods with PF > 1.0** — the key metric | **≥ 70%** |
| `mean_pf` | Average profit factor across periods | ≥ 1.2 |
| `std_pf` | Variability of PF across periods | Low = consistent |
| `min_pf` | Worst-case single window | ≥ 0.8 (not catastrophic) |
| `max_pf` | Best-case single window | Sanity check |
| `mean_expectancy_r` | Average R per trade across all periods | ≥ 0.1 |
| `total_trades` | Sum across all periods | ≥ 50 |
| `worst_dd_pct` | Worst drawdown in any single window | < 15% |
| `stability_score` | `mean_pf × (consistency_pct / 100)` | Higher = better |

The summary is **sorted by `stability_score`** — use this to pick live symbols.

---

## How to read it

```
consistency_pct ≥ 70    ← profitable in at least 7 of 10 periods
mean_pf ≥ 1.2           ← real edge on average
std_pf ≤ 0.4            ← doesn't blow up in bad periods
min_pf ≥ 0.8            ← survivable worst case
```

Avoid symbols where `mean_pf` looks great but `std_pf` is huge — that means
one lucky window is pulling up the average.

---

## `.env` settings

| Key | Default | Description |
|-----|---------|-------------|
| `WALKFORWARD_PERIODS` | `10` | Number of time windows |
| `WALKFORWARD_BARS_PER_PERIOD` | `10000` | LTF bars per window |
| `WALKFORWARD_OUTPUT_DIR` | `backtest_results` | Directory for per-period CSVs |
| `WALKFORWARD_SUMMARY_CSV` | `backtest_walkforward_summary.csv` | Summary file path |

---

## Typical workflow

```bash
# 1. Download history (much faster runs)
python data_cache.py --download-live

# 2. Walk-forward across all symbols
python backtest_walkforward.py

# 3. Pick symbols by stability
python allocate.py --csv backtest_walkforward_summary.csv \
                   --min-consistency 70 --min-pf 1.2

# 4. Paste SYMBOLS=... into .env
```
