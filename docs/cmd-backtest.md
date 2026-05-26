# `python backtest.py` — Single-window backtest

Runs the full strategy bar-by-bar across one historical window for every
symbol you point it at. Results go to `backtest_results.csv`. Each row is
written immediately — a crash or Ctrl+C never loses progress.

---

## Quick start

```bash
python backtest.py
```

All settings come from `.env`. No arguments required for a standard run.

---

## Commands

### Run on all symbols (broker discovery)

```bash
# Uses BACKTEST_ALL_SYMBOLS or BACKTEST_SCAN_ALL from .env
python backtest.py
```

### Single symbol

```bash
python backtest.py --symbol EURUSD
python backtest.py --symbol XAUUSD
```

---

## Symbol selection (priority order)

| Priority | How | Config |
|----------|-----|--------|
| 1 | `--symbol` CLI argument | — |
| 2 | `BACKTEST_SYMBOLS=` list in `.env` | Explicit comma-separated list |
| 3 | `BACKTEST_ALL_SYMBOLS=true` | Every visible symbol from your MT5 terminal |
| 4 | `BACKTEST_SCAN_ALL=true` | Built-in list of 30 forex pairs + metals |
| 5 | `SYMBOLS=` (live symbols) | Fallback to your live trading list |

---

## Quality pre-filters (when `BACKTEST_ALL_SYMBOLS=true`)

Applied *before* fetching any bar data, so wasted work is minimised:

| Setting | Effect |
|---------|--------|
| `BACKTEST_INSTRUMENT_TYPES` | Whitelist: `forex,indices,metals,energy,crypto,stocks,futures` |
| `BACKTEST_MAX_SPREAD_PCT` | Skip symbols where spread > N% of price |
| `BACKTEST_MAX_SPREAD_TICKS` | Hard cap on absolute spread ticks |
| `BACKTEST_SYMBOL_FILTER` | Optional regex — only include matching names |
| `BACKTEST_EXCLUDE_FILTER` | Optional regex — exclude matching names |
| `BACKTEST_MIN_BARS` | Skip symbols with too little history |

---

## `.env` settings

| Key | Default | Description |
|-----|---------|-------------|
| `BACKTEST_EQUITY` | `10000` | Starting equity for simulation |
| `BACKTEST_BARS` | `20000` | LTF bars per symbol (HTF = max(2000, bars/4)) |
| `BACKTEST_SPREAD_PIPS` | `0` | `0` = use live broker spread; `>0` = override |
| `BACKTEST_OUTPUT_CSV` | `backtest_results.csv` | Results file |
| `BACKTEST_MIN_BARS` | `500` | Skip symbols with fewer bars |
| `USE_DATA_CACHE` | `true` | Use local M1 cache instead of re-fetching |

---

## Reading `backtest_results.csv`

| Column | What it means | Target |
|--------|---------------|--------|
| `symbol` | Instrument name | — |
| `category` | Forex / Indices / Metals / Energy / Stocks | — |
| `trades` | Total closed trades | **≥ 50** for statistical validity |
| `win_rate` | % of trades that closed in profit | 35–50% is normal for trend-following |
| `profit_factor` | Gross profit ÷ gross loss | **≥ 1.2** = real edge; ≥ 1.5 = strong |
| `expectancy_r` | Average R-multiple per trade | **≥ 0.1R** good; ≥ 0.2R excellent |
| `total_r` | Net R across all trades | — |
| `total_pnl` | Net P&L in account currency | — |
| `max_drawdown_pct` | Worst peak-to-trough drawdown | **< 15%** comfortable |
| `avg_win_r` | Average winning trade (R) | Compare with `avg_loss_r` |
| `avg_loss_r` | Average losing trade (R, negative) | Should be ~−1.0 |
| `spread_ticks` | Spread used in simulation | High = explains weak results |
| `backtested_at` | UTC timestamp of this run | — |

---

## Typical workflow

```bash
# 1. Download history first (much faster backtests)
python data_cache.py --download-live

# 2. Run backtest
python backtest.py

# 3. Pick best symbols
python allocate.py --csv backtest_results.csv

# 4. Paste SYMBOLS=... line into .env
```

---

## Data coverage

| `TIMEFRAME` | `BACKTEST_BARS` | Approximate coverage |
|-------------|-----------------|---------------------|
| M1 | 50,000 | ~35 trading days |
| M5 | 50,000 | ~6 months |
| M15 | 20,000 | ~10 months |
| H1 | 5,000 | ~8 months |

The higher-timeframe (HTF) fetch is `max(2000, BACKTEST_BARS / 4)`.
