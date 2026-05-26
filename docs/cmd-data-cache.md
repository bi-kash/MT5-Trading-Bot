# `python data_cache.py` — Historical data cache

Manages a persistent local cache of M1 (1-minute) bar data per symbol.
Instead of re-downloading the same history on every backtest run, bars are
saved to disk once and only the gap since the last download is fetched on
subsequent runs. Higher timeframes (M5, M15, H1, H4, D1) are aggregated
on demand from the M1 base.

---

## Why it exists

- A single full download of 1,000,000 M1 bars (~1 year of forex) can take
  several minutes per symbol over MT5.
- Repeat backtests, walk-forward runs, and parameter sweeps would all pay
  that cost every time without a cache.
- With the cache, the second run fetches only the bars added since last
  download — typically a few hundred bars, done in seconds.

---

## Storage format

```
data_cache/
├── EURUSD_M1.pkl          # M1 bars as pandas DataFrame (pickle)
├── EURUSD_M1_info.txt     # Human-readable stats file (auto-generated)
├── USDJPY_M1.pkl
├── USDJPY_M1_info.txt
└── ...
```

Each `*_M1_info.txt` file is written automatically after every download and
shows the actual contents of the `.pkl`: date range, bar count, days
covered, file size, and freshness status.

---

## Commands

### Show cache status

```bash
python data_cache.py
python data_cache.py --info
```

Lists every cached symbol with bar count, date range, age, file size, and
whether it's fresh or stale. Also creates any missing `_info.txt` files
retroactively for symbols cached before info files were introduced.

Example output:
```
Cache directory : C:\...\data_cache
Symbols cached  : 3
Stale threshold : 10 days

  EURUSD       :  724,382 M1 bars   502.1 days  last=2026-05-25 14:00 (0:45:12 ago)  [fresh, 38.4 MB]  → EURUSD_M1_info.txt
  USDJPY       :  721,045 M1 bars   500.1 days  last=2026-05-25 14:00 (0:45:20 ago)  [fresh, 37.9 MB]  → USDJPY_M1_info.txt
  AUDUSD       :  705,311 M1 bars   490.3 days  last=2026-05-25 14:00 (0:45:28 ago)  [fresh, 37.1 MB]  → AUDUSD_M1_info.txt
```

---

### Download all live symbols

```bash
python data_cache.py --download-live
python data_cache.py --all-configured     # same thing
```

Downloads (or incrementally updates) every symbol listed in `SYMBOLS=` in
`.env`. Uses `CACHE_INITIAL_BARS` as the maximum bar count on first download.
On subsequent runs, only fetches the gap from the last cached bar to now.

---

### Download a specific symbol

```bash
python data_cache.py --symbol EURUSD
python data_cache.py --symbols EURUSD,USDJPY,XAUUSD
```

---

### Full re-download from scratch

```bash
python data_cache.py --symbol EURUSD --force
python data_cache.py --download-live --force
```

Discards existing cache for the symbol and re-fetches from MT5 up to
`--bars` (or `CACHE_INITIAL_BARS`).

---

### Override bar limit

```bash
python data_cache.py --download-live --bars 500000
```

Downloads up to 500,000 M1 bars per symbol instead of the `.env` default.

---

## All options

| Flag | Default | Description |
|------|---------|-------------|
| `--info` | — | Show cache state and exit (no network) |
| `--download-live` | — | Download all `SYMBOLS` from `.env` |
| `--all-configured` | — | Same as `--download-live` |
| `--symbol NAME` | — | Single symbol to refresh |
| `--symbols A,B,C` | — | Comma-separated list to refresh |
| `--bars N` | `CACHE_INITIAL_BARS` | Max M1 bars to fetch |
| `--force` | false | Re-download from scratch (skip incremental) |

---

## `.env` settings

| Key | Default | Description |
|-----|---------|-------------|
| `USE_DATA_CACHE` | `true` | `false` = always fetch fresh from MT5 (old behaviour) |
| `CACHE_DIR` | `data_cache` | Directory for `.pkl` cache files |
| `CACHE_STALE_DAYS` | `10` | Auto-refresh when last bar is older than N days |
| `CACHE_INITIAL_BARS` | `1000000` | Max M1 bars on first download (~1 year of 24/5 forex) |

---

## Typical workflow

```bash
# First run: download full history for live symbols (slow, ~5 min per symbol)
python data_cache.py --download-live

# Check what was downloaded
python data_cache.py --info

# Subsequent runs: only fetch new bars (fast, seconds)
python data_cache.py --download-live

# Before a big optimization or walk-forward run, refresh everything
python data_cache.py --download-live
python optimize.py
```

Backtests and walk-forward runs automatically use the cache when
`USE_DATA_CACHE=true` — no extra steps needed once the cache is populated.
