# MT5 Trading Bot (Python)

A disciplined algorithmic trading bot for **MetaTrader 5**. Two interchangeable
strategy variants, ATR-scaled risk, walk-forward backtesting, Bayesian parameter
optimization, and aggressive capital-preservation guards.

> **No guarantees of profit.** Markets are adversarial. This bot is built for
> *survivability* — a small positive edge compounded over many trades, not
> hype-grade returns. Always demo first.

---

## How it works in one paragraph

The bot waits for **four independent filters to all agree** before opening a
trade: (1) regime filter confirming the market is actually trending (ADX + ATR
percentile), (2) higher-timeframe trend bias, (3) lower-timeframe EMA stack
alignment, and (4) a strategy-specific entry trigger. Two strategies are
available — **RSI Cross** (wait for RSI to cross back through 50 after a
pullback) and **Swing Breakout** (wait for price to break the last N-bar swing
high/low after an RSI pullback). When all four filters agree, an order is placed
with an ATR-scaled stop loss and take profit. Position size is computed so the
**dollar risk per trade is constant**, regardless of which instrument or
volatility regime. Daily-loss / drawdown / cooldown guards halt trading when
things go wrong.

---

## The strategy — explained in detail

The strategy lives in [strategy.py](strategy.py). Every bar, four gates are
checked in order. **All must pass or no trade is placed.**

### Gate 1 — Regime filter: is the market in a tradeable state?

**What is a regime filter?**
A "regime" is the current market *state* — trending, ranging, or dead quiet.
The regime filter classifies that state and blocks trades when conditions are
unfavourable for trend-following. Without it, the strategy fires signals in
choppy sideways markets and loses steadily.

**The bot already has one — ADX threshold:**

ADX (Average Directional Index) measures *trend strength*, not direction.
ADX > 25 = clear trend. ADX < 20 = market ranging sideways.

```
if ADX < ADX_THRESHOLD (default: 22) → no trade
```

In [strategy.py:106](strategy.py#L106) this has been in place from the start.
The backtest results confirm it is working: NOKDKK, USDHKD, EURSGD perform
badly **not because the filter failed**, but because those exotic crosses almost
never reach ADX 22 in a clean trend — they're structurally choppy.

**New enhancement — ATR percentile co-filter:**

ADX can briefly spike above threshold during micro-trends inside a dead market.
The ATR percentile filter adds a second check: even if ADX is high, if the
current ATR is in the bottom 20% of recent ATR values, volatility is too
compressed to trade profitably.

```
if ATR < 20th-percentile of last 50 ATR bars → no trade
(controlled by ATR_REGIME_PERCENTILE and ATR_REGIME_LOOKBACK in .env)
```

Set `ATR_REGIME_PERCENTILE=0` to disable and use ADX-only (original behaviour).

**Does the regime filter help?**
Yes — significantly. On instruments where it fires frequently (ranging markets),
it is the primary reason those symbols show negative expectancy. On instruments
where it fires rarely (GOLD, DOW, WTI), those same symbols show 1.3–1.5 PF.
The regime filter is doing exactly what it should: making the strategy *selectively
aggressive* rather than always-on.

---

### Gate 2 — Higher-timeframe trend bias: which direction?

Checks H4 close vs H4 EMA-200 (configurable via `HIGHER_TIMEFRAME`).

```
H4 close > H4 EMA-200  →  only look for LONG (buy) signals
H4 close < H4 EMA-200  →  only look for SHORT (sell) signals
```

This is the "big picture" filter. Trades in the direction of the dominant
multi-day trend have higher follow-through probability than counter-trend
entries. A pullback on M15 that looks like a great long setup is often just
a dead-cat bounce if H4 says the market has been falling for weeks.

H4 is the default. `HIGHER_TIMEFRAME=D1` gives a more stubborn filter that
trades less but is cleaner. `H1` reacts faster but produces more whipsaws.

---

### Gate 3 — LTF EMA stack: does the entry timeframe agree?

On the M15 chart, two conditions must both hold for longs:

- `Close > EMA-200` — price is above the long-run trend line
- `EMA-20 > EMA-50` — short MA above long MA = upward momentum

(Inverted for shorts: `Close < EMA-200` and `EMA-20 < EMA-50`.)

If Gate 2 says "the H4 trend is up" and Gate 3 says "the M15 trend is also
up", the signal is aligned across two timeframes — that convergence is where
the highest-probability entries come from.

---

### Gate 4 — RSI pullback trigger: wait for the right entry moment

Even when the trend is confirmed across both timeframes, we do not buy
immediately. We wait for a **pullback** (a temporary dip inside the uptrend)
and enter when momentum resumes.

The trigger: RSI(14) crosses **back above 50** after having dipped toward the
oversold zone. That crossing means: "there was a pullback, and buyers are now
returning."

```
RSI(prev) < 50  AND  RSI(now) ≥ 50   →  BUY trigger
RSI(prev) > 50  AND  RSI(now) ≤ 50   →  SELL trigger
```

**Why not just buy when the trend looks good?**
Buying in the middle of a strong trending bar means competing with everyone
already positioned. A measured pullback gives a better entry price and puts
the natural swing low close to where your stop belongs. That is where
institutional desks build positions — not by chasing breakouts.

---

### What happens after a signal fires

| Step | Calculation | Setting |
|---|---|---|
| Stop loss | `entry ± SL_ATR_MULT × ATR` | `SL_ATR_MULT=1.5` |
| Take profit | `entry ± TP_ATR_MULT × ATR` → R:R ≈ 1.67 | `TP_ATR_MULT=2.5` |
| Position size | `risk_amount ÷ (sl_dist ÷ tick_size × tick_value)` | `RISK_PER_TRADE=0.5%` |
| Breakeven | SL moves to entry after `BREAKEVEN_AT_R` profit | `BREAKEVEN_AT_R=1.5` |
| Trailing | SL trails by `TRAILING_ATR_MULT × ATR` from breakeven | `TRAILING_ATR_MULT=1.8` |

Dollar risk is **constant per trade** regardless of which instrument or how
volatile it currently is. A GOLD trade and an EURUSD trade both risk exactly
`RISK_PER_TRADE`% of your current equity.

---

## Choosing a strategy (`STRATEGY_VARIANT`)

Two signal-generation strategies are available. Switch between them in `.env`:

```ini
STRATEGY_VARIANT=rsi_cross       # default
STRATEGY_VARIANT=swing_breakout  # alternative
```

Both strategies share the same Gates 1–3 (regime filter, HTF bias, EMA stack).
They differ only in Gate 4 — how the entry trigger is defined.

---

### Strategy 1: `rsi_cross` (default)

**Trigger:** RSI(14) crosses back through 50 after a pullback.

```
RSI(prev) < 50  AND  RSI(now) ≥ 50   →  BUY
RSI(prev) > 50  AND  RSI(now) ≤ 50   →  SELL
```

**When it works best:** Markets that pull back cleanly and resume trend.
RSI dips below 50, buyers return, RSI crosses back up — textbook pullback
entry.

**Limitation:** In very strong trends, RSI never dips back below 50. The bot
sits on the sideline watching the move without entering. Misses "runaway
trend" phases.

---

### Strategy 2: `swing_breakout`

**Trigger:** Price breaks the highest high (or lowest low) of the last N bars
**after** RSI has dipped below 50 at some point in that window.

```
BUY  (bull alignment): RSI dipped below 50 within last N bars
                       AND close > highest high of those N bars

SELL (bear alignment): RSI rallied above 50 within last N bars
                       AND close < lowest low of those N bars
```

Controlled by `SWING_LOOKBACK=10` in `.env` — the lookback window (bars).

**When it works best:** Strong trends where RSI barely dips — the price
breakout above the swing high is the confirmation. More suitable for
momentum-driven instruments (indices, commodities).

**Limitation:** Can enter near the end of a move when price is already
extended. The swing-high breakout entry is typically higher than the RSI-cross
entry in the same trend.

**Comparison:**

| | `rsi_cross` | `swing_breakout` |
|---|---|---|
| Entry type | Pullback entry | Breakout entry |
| Entry price | Lower (after dip) | Higher (at breakout) |
| Catches runaway trends | No — requires RSI dip | Yes — only needs a brief dip |
| Best for | Forex, ranging trends | Indices, strong trends |
| Setting | `STRATEGY_VARIANT=rsi_cross` | `STRATEGY_VARIANT=swing_breakout` |

**How to choose:** Run `python backtest_walkforward.py` with each variant
and compare the `stability_score` and `consistency_pct` for your symbols.
Use whichever scores higher for the instruments you trade.

> When you change strategy variant, **re-run `python optimize.py`** —
> the optimal parameters differ between strategies.

---

### Why does it work on some instruments but not others?

The strategy needs instruments that **trend persistently**. The backtest shows:

- **Works well:** GOLD, WTI, GASS, OILK (commodities with macro supply/demand
  trends), DOW, VIXM (indices with strong directional phases), EURUSD/GBPUSD
  (major forex with central-bank driven trends)
- **Fails:** exotic crosses (NOKDKK, EURSGD, USDHKD) that range most of the
  time; spread is also too wide to cover the R:R after real entry costs

The multi-symbol backtest is specifically designed to find *which instruments
your broker's version of this strategy works on* and discard the rest.

---

## Project structure

```
.
├── main.py                       # Live runtime loop
├── strategy.py                   # RSICross + SwingBreakout signal generation
├── risk_manager.py               # Sizing, daily-loss, drawdown, cooldown
├── mt5_connector.py              # MetaTrader 5 SDK wrapper
├── indicators.py                 # EMA / ATR / ADX / RSI / BBands (pandas)
├── notifier.py                   # Telegram + Discord alerts
├── journal.py                    # CSV trade log
├── logger.py                     # Rotating file + stdout
├── config.py                     # .env -> typed Settings (+ loads optimized_params.json)
├── backtest.py                   # Multi-symbol bar-by-bar simulator (single window)
├── backtest_walkforward.py       # Backtest across N time periods + stability metrics
├── data_cache.py                 # Persistent M1 bar cache with incremental refresh
├── optimize.py                   # Bayesian parameter optimization (Optuna)
├── allocate.py                   # Pick & weight live symbols from backtest results
├── ml_filter.py                  # Optional probabilistic filter (skeleton)
│
├── optimized_params.json         # Generated by optimize.py (gitignored)
├── backtest_results.csv          # Generated by backtest.py
├── backtest_results/             # Per-period CSVs from walkforward
├── backtest_walkforward_summary.csv
├── data_cache/                   # Cached M1 .pkl files (gitignored)
│   ├── EURUSD_M1.pkl
│   ├── EURUSD_M1_info.txt        # Human-readable cache stats
│   └── ...
│
├── docs/                         # Detailed command documentation
│   ├── cmd-backtest.md
│   ├── cmd-walkforward.md
│   ├── cmd-data-cache.md
│   ├── cmd-optimize.md
│   └── cmd-allocate.md
│
├── .env                          # Credentials & settings (gitignored)
├── .env.example                  # Annotated template
└── requirements.txt
```

---

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# edit .env with your MT5 credentials
```

Then the typical workflow:

```bash
# ── Step 0: Download history (one-time, fast on repeat runs) ──────────── #
python data_cache.py --download-live
# Saves ~1 year of M1 bars per symbol to data_cache/ (~40 MB each)
# Subsequent runs only fetch the gap — takes seconds.

# ── Step 1: Choose a strategy ─────────────────────────────────────────── #
# Edit .env:
#   STRATEGY_VARIANT=rsi_cross       (default — RSI crosses 50 after pullback)
#   STRATEGY_VARIANT=swing_breakout  (price breaks swing high after RSI dip)

# ── Step 2: Find which symbols work ───────────────────────────────────── #
# Quick single-window scan:
python backtest.py

# (Recommended) Walk-forward — stability across 10 time windows:
python backtest_walkforward.py

# ── Step 3: Optimize parameters ───────────────────────────────────────── #
pip install optuna           # one-time
python optimize.py           # ~20-40 min, 300 trials
# Then in .env: USE_OPTIMIZED_PARAMS=true

# ── Step 4: Pick best symbols ─────────────────────────────────────────── #
python allocate.py --csv backtest_walkforward_summary.csv --min-consistency 70
# Paste the SYMBOLS=... line into .env

# ── Step 5: Run live ──────────────────────────────────────────────────── #
# Start with DRY_RUN=true for at least 1 week before going live
python main.py
```

---

## Tuning trading frequency

The bot's signal frequency depends on three settings in `.env`. Here is a
practical guide to choosing the right combination for your goal.

### Timeframe combinations

| `TIMEFRAME` | `HIGHER_TIMEFRAME` | Signals / week (per symbol) | Best for |
|---|---|---|---|
| M1  | M15 | 20–50 | Scalping — very noisy, spread eats edge fast |
| M5  | H1  | 5–15 | **Active day trading — recommended for more trades** |
| M15 | H4  | 2–5  | Original default — swing trades, fewer but cleaner |
| H1  | D1  | 1–3  | Slow swing — very selective, fine for part-time watching |

**Current setting: M5 / H1** (changed from M15 / H4)

### ADX threshold effect on signal count

| `ADX_THRESHOLD` | Effect |
|---|---|
| 15 | Very loose — trades in almost any market, many false signals |
| 20 | **Active — current setting.** Allows moderate trends |
| 22 | Original default — reasonable balance |
| 25 | Strict — only clear trends, fewer but higher-quality signals |

**Current setting: 20** (changed from 25)

### Why you must re-backtest after changing these

The backtest results in `backtest_results.csv` were produced with specific
timeframe and filter settings. If you change `TIMEFRAME`, `HIGHER_TIMEFRAME`,
or `ADX_THRESHOLD`, those results are no longer valid — a symbol that looked
great on M15/H4 may perform differently on M5/H1.

**Always re-run after a settings change:**
```bash
python backtest.py       # re-test all symbols with new settings
python allocate.py       # re-pick the best symbols for the new settings
# then update SYMBOLS= in .env
```

### Timeframe data coverage

| `TIMEFRAME` | `BACKTEST_BARS` | Coverage |
|---|---|---|
| M1  | 50 000 | ~35 days |
| M5  | 50 000 | ~174 days (~6 months) |
| M15 | 20 000 | ~208 days (~10 months) |
| H1  | 5 000  | ~208 days |

With `BACKTEST_BARS=50000` and `TIMEFRAME=M5` you get ~6 months of history.
That is enough for statistical reliability (≥ 50 trades per symbol).

---

## Does it use stop loss?

**Yes — at multiple layers, defence in depth:**

| Layer | What it does | Where it lives |
|---|---|---|
| **Per-trade SL** | Every trade gets a hard stop at `entry ± SL_ATR_MULT × ATR` | `strategy.py` |
| **Per-trade TP** | Take-profit at `entry ± TP_ATR_MULT × ATR` (default 2.5 ATR ≈ 1.67 R:R) | `strategy.py` |
| **Breakeven move** | SL moves to entry after price reaches `BREAKEVEN_AT_R` of profit | `main.py` |
| **Trailing stop** | After breakeven, SL trails by `TRAILING_ATR_MULT × ATR` | `main.py` |
| **Daily loss kill-switch** | Halts ALL trading if daily loss ≥ `MAX_DAILY_LOSS_PCT` | `risk_manager.py` |
| **Drawdown guard** | Halts trading if equity falls ≥ `MAX_DRAWDOWN_PCT` from peak | `risk_manager.py` |
| **Cooldown** | After `COOLDOWN_LOSSES` consecutive losses → pause `COOLDOWN_MINUTES` | `risk_manager.py` |
| **Spread guard** | Skips entry if current spread > `MAX_SPREAD_POINTS` (slippage protection) | `main.py` |

**Position sizing formula** (so the SL distance becomes meaningful, not just a number):

```
risk_amount = account_equity × (RISK_PER_TRADE / 100)
sl_distance = SL_ATR_MULT × current_ATR
loss_per_lot = (sl_distance / tick_size) × tick_value
volume       = risk_amount / loss_per_lot          # rounded to broker lot step
```

The result: **every trade risks the same dollar amount**, regardless of how
volatile the instrument is or how wide the SL is.

---

## Configuration

All settings live in [`.env`](.env). See [`.env.example`](.env.example) for the
fully annotated template. The headline knobs:

**Strategy**

| Key | Default | What it does |
|---|---|---|
| `STRATEGY_VARIANT` | `rsi_cross` | `rsi_cross` or `swing_breakout` — see [Choosing a strategy](#choosing-a-strategy-strategy_variant) |
| `SWING_LOOKBACK` | `10` | Lookback bars for SwingBreakout strategy |
| `ADX_THRESHOLD` | `19` | Below this → no trades (ranging market) |
| `ATR_REGIME_PERCENTILE` | `20` | Skip when ATR < bottom N% of recent bars (0 = off) |

**Risk**

| Key | Default | What it does |
|---|---|---|
| `DRY_RUN` | `false` | `true` = log decisions only, no real orders |
| `RISK_PER_TRADE` | `0.5` | % of equity risked per trade |
| `MAX_DAILY_LOSS_PCT` | `3.0` | Daily kill-switch threshold |
| `MAX_DRAWDOWN_PCT` | `10.0` | Peak-to-trough kill-switch |
| `SL_ATR_MULT` | `1.5` | Stop distance in ATR multiples |
| `TP_ATR_MULT` | `2.5` | Take-profit in ATR multiples |
| `BREAKEVEN_AT_R` | `1.5` | Move SL to breakeven after N×R profit |
| `TRAILING_ATR_MULT` | `1.8` | ATR trailing stop distance |
| `MAGIC_NUMBER` | unique | Bot only manages its own trades |

**Data cache**

| Key | Default | What it does |
|---|---|---|
| `USE_DATA_CACHE` | `true` | Use local M1 cache; `false` = always fetch fresh |
| `CACHE_DIR` | `data_cache` | Directory for `.pkl` cache files |
| `CACHE_STALE_DAYS` | `10` | Auto-refresh when last bar is older than N days |
| `CACHE_INITIAL_BARS` | `1000000` | Max M1 bars on first download (~1 year) |

**Parameter optimization**

| Key | Default | What it does |
|---|---|---|
| `USE_OPTIMIZED_PARAMS` | `false` | `true` = load params from `optimized_params.json` |
| `OPTIMIZED_PARAMS_FILE` | `optimized_params.json` | Path to the optimization output file |

---

## Backtesting

Run `python backtest.py`. All parameters come from `.env` — no CLI needed.
The backtester works on **any instrument MT5 exposes** (forex, indices,
futures, stock CFDs, metals, crypto) by using each symbol's native
`tick_size` / `tick_value` from MT5.

### How much data is being backtested?

This is calculated from your `.env`:

```
BACKTEST_BARS = 20000      # LTF bars to fetch per symbol
TIMEFRAME     = M15        # Each bar = 15 minutes
HIGHER_TIMEFRAME = H4      # HTF bars = max(2000, bars/4) = 5000 H4 bars
```

| Setting | Calculation | Coverage |
|---|---|---|
| LTF (M15)  | 20,000 × 15 min = 300,000 min | ≈ 5,000 trading hours ≈ **~10 months of market activity** |
| HTF (H4)   | 5,000 × 4 hr = 20,000 hr | ≈ 833 trading days ≈ **~3.3 years of context** |

Want a longer or shorter window? Edit `BACKTEST_BARS` in `.env`:
* `40000` ≈ **~20 months** of M15 (recommended for more reliable stats)
* `10000` ≈ **~5 months** (quicker runs, less reliable for rare-signal symbols)

### Symbol filtering

Set `BACKTEST_ALL_SYMBOLS=true` and the bot pulls **every visible symbol from
MT5** (your broker may expose 10,000+). It then applies quality pre-filters
*before* fetching any bar data, so wasted work is minimised:

| Filter | Effect |
|---|---|
| `BACKTEST_INSTRUMENT_TYPES` | Whitelist categories (forex, indices, metals, energy, crypto, stocks, futures) |
| `BACKTEST_MAX_SPREAD_PCT`   | Skips symbols where spread > N% of price (kills illiquid stocks) |
| `BACKTEST_MAX_SPREAD_TICKS` | Hard cap on absolute spread |
| `BACKTEST_SYMBOL_FILTER`    | Optional regex to include only matching names |
| `BACKTEST_EXCLUDE_FILTER`   | Optional regex to exclude matching names |
| `BACKTEST_MIN_BARS`         | Skips symbols with too little history |

On a broker with 11,000 symbols, the default filters typically reduce that to
**80–150 symbols** that finish in 3–5 minutes total.

### Reading `backtest_results.csv`

Results are written **one symbol at a time** — so a crash or Ctrl+C never
loses progress. Each row gets a `backtested_at` timestamp.

| Column | What it means | Why it matters |
|---|---|---|
| `symbol`            | Instrument name as MT5 knows it (e.g. `EURUSD`, `XAUUSD`, `US500`) | What you'd put in `SYMBOLS=` |
| `category`          | Broad type: Forex / Indices / Metals / Energy / Stocks / Crypto | Sanity check |
| `trades`            | Total completed trades over the backtest window | **≥ 50 is statistically reasonable**, ≥ 100 better. Below 20 = mostly luck |
| `win_rate`          | % of trades that closed in profit | A trend system can be very profitable at 40% if avg_win ≫ avg_loss |
| `profit_factor`     | Gross profit ÷ gross loss | **> 1.3** = real edge after costs; > 1.5 = strong; > 2.0 suspiciously good (verify) |
| `expectancy_r`      | Average R-multiple per trade (R = the dollar amount risked) | **> 0.1R** is good; 0.2R+ is excellent. R-multiples are symbol-agnostic |
| `total_r`           | Sum of all R-multiples across the run | Net R over the period |
| `total_pnl`         | Net P&L in account currency (uses MT5 tick value, approximate) | What you'd have made/lost |
| `ending_equity`     | `BACKTEST_EQUITY + total_pnl` | Final balance |
| `max_drawdown_pct`  | Worst peak-to-trough drawdown during the run | **< 15%** is comfortable; > 25% means you'd panic and turn it off |
| `avg_win_r`         | Average winning trade size in R | Compare with `avg_loss_r` for skew |
| `avg_loss_r`        | Average losing trade size in R (negative) | Should be ~-1.0 if SL discipline holds |
| `tick_size`         | Smallest price increment for this instrument | Diagnostic |
| `spread_ticks`      | Spread used in simulation (ticks) | Diagnostic — high values explain weak results |
| `backtested_at`     | UTC datetime of this run | So you know when to re-test |

### Picking which symbols to trade live

Don't just trade the top-of-list symbol — combine criteria:

1. **`trades` ≥ 50** — fewer than 50 trades is statistical noise
2. **`profit_factor` ≥ 1.2** — anything less is barely above break-even after slippage
3. **`expectancy_r` ≥ 0.1R** — meaningful edge per trade
4. **`max_drawdown_pct` ≤ 15%** — survivable in real time
5. **Diverse categories** — don't pick five oil ETFs; correlation will eat you alive

The `allocate.py` tool does this filtering automatically (see next section).

### Current results — is the bot doing well?

Looking at your last run (`backtest_results.csv`, ~10 months of M15 data):

**Top performers with statistical reliability (≥100 trades):**

| Symbol | PF | Expectancy | DD% | Trades |
|---|---|---|---|---|
| GOLD  | 1.54 | +0.28R | 8.8% | 149 |
| VIXM  | 1.54 | +0.25R | 3.9% | 113 |
| OILK  | 1.44 | +0.20R | 3.9% | 117 |
| GASS  | 1.40 | +0.21R | 3.8% | 160 |
| DOW   | 1.30 | +0.17R | 8.9% | 176 |
| EURUSD| 1.26 | +0.15R | 3.7% | 128 |
| WTI   | 1.26 | +0.15R | 8.3% | 140 |

**Verdict: yes, the strategy has a real edge — but only on a specific subset.**
About 15 out of ~88 tested instruments cleared the 1.2 PF + 0.1R + low-DD bar.
The rest lost money. This is exactly what you'd expect for a trend-following
strategy: it works on trendy/liquid instruments and bleeds on choppy/exotic
ones. The whole point of the multi-symbol scan is to **find the edge symbols
and ignore the rest**.

Bad performers to avoid (deep drawdowns, negative expectancy):
NOKDKK, USDHKD, EURSGD, CHFSGD, DKKSEK, AUDSGD, GBPSGD — all exotic crosses
with wide spreads and weak trending behaviour. The strategy is doing its job
by losing slowly here, not catastrophically.

---

## Historical data cache (`data_cache.py`)

Repeated backtesting (single window, walk-forward, parameter sweeps) normally
re-downloads the same bars from MT5 every time. On large symbol sets this adds
minutes of wait time per run. The data cache solves this.

**How it works:** bars are stored locally as per-symbol M1 `.pkl` files.
Higher timeframes (M5, M15, H1, H4, D1) are aggregated on demand from the M1
base. On the first download, up to `CACHE_INITIAL_BARS` (default 1,000,000 ≈ 1
year) of M1 data are fetched. On every subsequent run, only the gap from the
last cached bar to now is fetched — typically a few hundred bars, done in
seconds.

A plain-text `*_info.txt` file is written alongside each `.pkl` showing what
it actually contains: date range, bar count, calendar days covered, file size,
and freshness status.

```bash
# One-time setup — download all live symbols
python data_cache.py --download-live

# Check what's cached
python data_cache.py --info

# Refresh a single symbol
python data_cache.py --symbol EURUSD

# Force re-download from scratch
python data_cache.py --download-live --force
```

→ See [docs/cmd-data-cache.md](docs/cmd-data-cache.md) for full documentation.

---

## Walk-forward backtest (`backtest_walkforward.py`)

A single backtest gives one performance number per symbol over one window.
That number can be **misleading**: a symbol with PF=2.0 might have made all
its money in one lucky 2-week run, while a symbol with PF=1.3 might be
profitable every single month. Without time-slicing you can't tell them apart.

The walk-forward backtester runs the full backtest on **N independent time
periods** and aggregates stability metrics — far more reliable than a single
window for choosing live symbols.

```bash
python backtest_walkforward.py                       # uses .env defaults
python backtest_walkforward.py --periods 12 --bars 8000
python backtest_walkforward.py --symbol EURUSD       # single symbol
```

**What it produces:**

```
backtest_results/                          (directory)
├── period_01_of_10.csv     # oldest 10K bars — every symbol's result on that window
├── period_02_of_10.csv     # next 10K bars
├── …
└── period_10_of_10.csv     # most recent 10K bars
backtest_walkforward_summary.csv           # one row per symbol with stability metrics
```

### Key metrics in the summary CSV

| Column | What it tells you |
|---|---|
| `periods_tested` | How many windows we ran (typically 10) |
| `periods_profitable` | How many had PF > 1.0 |
| `consistency_pct` | **% of periods profitable — the key metric** |
| `mean_pf` ± `std_pf` | Average performance and how much it varies |
| `min_pf` / `max_pf` | Worst-case and best-case single window |
| `mean_expectancy_r` | Average R per trade across all periods |
| `total_trades` | Sum across all periods |
| `worst_dd_pct` | Worst drawdown in any single window |
| `stability_score` | `mean_pf × (consistency_pct / 100)` — combined rank |

### How to read it

- **`consistency_pct ≥ 70`** is the gold standard — profitable in at least 7 of
  10 periods. These symbols probably keep working in unseen market conditions.
- **`mean_pf ≥ 1.3` with `std_pf ≤ 0.4`** = stable, real edge
- **`min_pf ≥ 0.8`** = even the worst window wasn't catastrophic
- Avoid symbols where `mean_pf` looks great but `std_pf` is huge — that's one
  lucky window pulling up the average

### Then allocate from it

`allocate.py` auto-detects the walk-forward summary schema and ranks by
`stability_score` instead of single-window PF:

```bash
python allocate.py --csv backtest_walkforward_summary.csv \
                   --min-consistency 70 --min-pf 1.3 --min-trades 50
```

This gives you a symbol list that's been validated across multiple market
regimes — much safer than picking based on a single backtest.

---

## Weighted live allocation (`allocate.py`)

After backtesting, run:

```bash
python allocate.py
```

It reads `backtest_results.csv`, applies quality filters, and prints a ranked
list of weighted symbol picks. The weight formula:

```
score  = max(0, expectancy_r) × √trades × (1 − max_dd_pct/100)
weight = score / Σ scores
```

Why this combines three independent dimensions:
* `expectancy_r` — captures the **edge size**
* `√trades` — rewards **statistical reliability** without going crazy on count
* `(1 − dd_pct)` — penalises symbols that, while profitable on paper, dug
  drawdowns that would have shaken you out in real life

Output ends with a ready-to-paste line:
```
SYMBOLS=GOLD,VIXM,OILK,GASS,DOW,EURUSD,WTI,OILU,USPX,SPXT
```
Just copy that into your `.env`.

Override the defaults if you want stricter or looser filtering:
```bash
python allocate.py --top 5 --min-pf 1.3 --min-trades 100 --max-dd 10
```

> **Caveat:** the bot itself currently uses a uniform `RISK_PER_TRADE` across
> all symbols. The "risk%" column from `allocate.py` is a *suggestion*:
> manually set `RISK_PER_TRADE` to the lowest per-symbol risk, then know that
> stronger-weighted symbols deserve more attention. A future enhancement is
> per-symbol risk scaling — open if you want it.

→ See [docs/cmd-allocate.md](docs/cmd-allocate.md) for all flags and filtering options.

---

## Parameter optimization (`optimize.py`)

The default parameters (`EMA_FAST=20`, `ADX_THRESHOLD=19`, `SL_ATR_MULT=1.5`,
etc.) are reasonable starting points — not optimal for any specific instrument
set. `optimize.py` finds better values automatically using **Optuna Bayesian
search** with walk-forward validation to guard against curve-fitting.

**Requires:** `pip install optuna`

```bash
# Full optimization — all live SYMBOLS, 300 trials (~20-40 min)
python optimize.py

# Then activate in .env:
USE_OPTIMIZED_PARAMS=true
```

**How it avoids overfitting:**
- Each trial is scored on a mini walk-forward (4 sub-periods), not a single window
- History is split 70% train / 30% held-out test
- Best params are validated on the test slice before saving
- A "Test/Train ratio" is reported: < 75% = warning, < 50% = likely overfitting

**What is optimized:** 8 high-impact parameters (`ema_fast`, `ema_slow`,
`adx_threshold`, `sl_atr_mult`, `tp_atr_mult`, `atr_regime_percentile`,
`trailing_atr_mult`, `breakeven_at_r`). The rest stay at `.env` defaults.

**Important:** Re-run after changing `STRATEGY_VARIANT` or your symbol list.
The optimal parameters differ significantly between `rsi_cross` and
`swing_breakout`.

→ See [docs/cmd-optimize.md](docs/cmd-optimize.md) for all flags, output format, and caveats.

---

## How to improve the algorithm

Plenty of room. In rough order of bang-for-buck:

### 1. Walk-forward validation *(implemented)*
The `backtest_walkforward.py` script splits history into N independent
periods and produces stability metrics per symbol. Use the
`consistency_pct` column to find symbols whose edge holds across time, not
just one lucky window. Next-level enhancements:
* Rolling-window walk-forward (re-train every N bars rather than fixed splits)
* Per-period parameter optimization (find the best `ADX_THRESHOLD` per window
  and see if the "best" value is stable or wildly different each time)

### 2. Per-asset-class parameters
The current params (`EMA_FAST=20`, `EMA_TREND=200`, `ADX_THRESHOLD=22`,
`SL_ATR_MULT=1.5`, `TP_ATR_MULT=2.5`) are reasonable defaults but **uniform
across forex, metals, indices, oil**. Each behaves differently:
* Metals/oil: longer trends → try `EMA_FAST=50`, `TP_ATR_MULT=3.5`
* Indices: faster mean-reversion → try `ADX_THRESHOLD=25`, tighter SL
* Forex majors: current defaults are probably near-optimal

Implementation: add `BACKTEST_PARAMS_PROFILE` switch in `.env` or symbol-group
overrides.

### 3. Parameter optimization *(implemented)*
`python optimize.py` uses Optuna Bayesian search (300 trials) to find the
best combination of 8 high-impact parameters. Scored using walk-forward
stability, validated on a held-out test slice. Results saved to
`optimized_params.json` and activated with `USE_OPTIMIZED_PARAMS=true`.
See [docs/cmd-optimize.md](docs/cmd-optimize.md).

### 4. Regime detection *(partially implemented)*
ADX > 22 was the original binary gate. The ATR-percentile co-filter
(`ATR_REGIME_PERCENTILE=20`) is now also active — it skips trades when
volatility is too compressed even if ADX is technically above threshold.

Next steps to improve further:
- **Dual ADX hysteresis**: enter only when ADX > 25, exit regime filter only
  when ADX drops below 18 (prevents whipsawing when ADX hovers at threshold)
- **Hurst exponent**: measures whether a time series is trending (H > 0.5) or
  mean-reverting (H < 0.5). More principled than ADX but computationally heavier
- **Volatility state machine**: classify bars as `trending / volatile / quiet`
  using a hidden-Markov or regime-switching model on ATR/ADX jointly

### 5. News-event filter
The biggest source of "weird" losing trades is high-impact news. Integrate a
calendar API (e.g. ForexFactory CSV, MyFXBook, FRED) and skip entries within
±15 min of high-impact releases. Most prop firms require this anyway.

### 6. Correlation control
Today the bot can open EURUSD and EURGBP and EURJPY simultaneously — that's
basically one EUR-long bet sized 3×. Add a correlation matrix (rolling
30-day) and refuse new positions whose 30-day return correlation with an
existing one is > 0.7.

### 7. Better exits
The current trailing stop is mechanical (ATR-based). Smarter exits:
* Time-based exit if trade hasn't progressed by N×R after M bars
* Chandelier exit (highest high − N × ATR)
* RSI-divergence exit (price up, RSI down → exhaustion)

### 8. ML edge filter
The `ml_filter.py` skeleton is already there. Train a logistic regression /
gradient boost on the features `[ADX, RSI, ATR/price, EMA distance, HTF
distance]` against the binary "did this signal make money?" target. Skip
signals where the model gives < `ML_MIN_PROBABILITY` confidence.

### 9. Are the current parameters best?

**Honest answer: no, they're reasonable defaults — not optimal.** They're
textbook values (Wilder/Faber/etc.) that work across many markets without
catastrophic failure, but no parameter is universally best.

What you *can* say from the current backtest:
* `ADX_THRESHOLD=22` clearly filters chop — symbols that struggle to trend
  (NOK/DKK/HKD crosses) lose money because even after the ADX filter they
  don't trend cleanly. So the filter is doing its job, just not on those
  symbols
* `SL_ATR_MULT=1.5 / TP_ATR_MULT=2.5` gives `avg_win_r ≈ +1.6, avg_loss_r ≈
  -1.0`. The R:R is roughly working as designed
* Win rate sits at 35–48% across symbols — that's normal for trend-following

For *your specific symbols* (GOLD, VIXM, OILK, GASS, DOW, EURUSD), the
parameters look broadly fine. The biggest gain is **dropping the symbols that
don't work**, which `allocate.py` does for you.

---

## Telegram / Discord notifications

Create a Telegram bot via `@BotFather`, get the token, then DM the bot and
visit `https://api.telegram.org/bot<TOKEN>/getUpdates` to find your chat ID.
Put both in `.env`. Discord uses a standard webhook URL.

---

## Prop-firm accounts

The bot is compatible with any broker that exposes MT5 (FTMO, MyForexFunds,
The5%ers, etc.), but **read your firm's rule sheet first**:

* Most firms cap **daily loss** and **overall drawdown** — set
  `MAX_DAILY_LOSS_PCT` and `MAX_DRAWDOWN_PCT` *below* the firm's limits.
* Many forbid **martingale**, **grid**, and **hedging** — none are used here.
* Some require minimum trading days — adjust `MAX_TRADES_PER_DAY` accordingly.
* News-trading restrictions — see improvement #5 above.

---

## Safety checklist before going live

- [ ] History cached — `python data_cache.py --download-live` completed
- [ ] Walk-forward run — `consistency_pct ≥ 70` on target symbols
- [ ] Symbols picked via `allocate.py --csv backtest_walkforward_summary.csv`
- [ ] Parameters optimized — `python optimize.py` run, test/train ratio ≥ 75%
- [ ] `USE_OPTIMIZED_PARAMS=true` set if using optimized params
- [ ] Strategy variant chosen (`STRATEGY_VARIANT=`) and re-backtested
- [ ] `DRY_RUN=false` only after ≥ 1 week of clean demo performance
- [ ] `MAX_DAILY_LOSS_PCT` and `MAX_DRAWDOWN_PCT` set below prop-firm limits
- [ ] `RISK_PER_TRADE` ≤ 0.5% until you trust the system
- [ ] `MAGIC_NUMBER` unique on the account (no conflicts with manual trades)
- [ ] `.env` in `.gitignore` — never committed credentials
- [ ] Bot runs on a VPS, not your laptop
- [ ] You can stop the bot remotely (systemd / pm2 / screen)
- [ ] Telegram/Discord alerts confirmed working on a test message

---

## macOS / Wine setup

The `MetaTrader5` Python package is Windows-only. Run the bot on a Windows
VPS (recommended) or inside a Wine prefix with Windows Python (advanced).
For development on macOS, use `python backtest.py --csv-ltf … --csv-htf …`
with CSV exports from another machine.

---

## License

MIT — use at your own risk. Not financial advice.
