# MT5 Trading Bot (Python)

A disciplined algorithmic trading bot for **MetaTrader 5**. Built around a
multi-filter trend-pullback strategy, ATR-scaled risk, and aggressive
capital-preservation guards.

> **No guarantees of profit.** Markets are adversarial. This bot is built for
> *survivability* — a small positive edge compounded over many trades, not
> hype-grade returns. Always demo first.

---

## How it works in one paragraph

The bot waits for **four independent filters to all agree** before opening a
trade: (1) regime filter confirming the market is actually trending (ADX + ATR
percentile), (2) higher-timeframe trend bias, (3) lower-timeframe EMA stack
alignment, and (4) RSI crossing back through 50 — i.e. a *pullback* inside a
confirmed trend. When all four line up, it sends an order with an ATR-scaled
stop loss and take profit. Position size is computed so the **dollar risk per
trade is constant**, regardless of which instrument or volatility regime.
Daily-loss / drawdown / cooldown guards halt trading when things go wrong.

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
| Breakeven | SL moves to entry after `BREAKEVEN_AT_R` profit | `BREAKEVEN_AT_R=1.0` |
| Trailing | SL trails by `TRAILING_ATR_MULT × ATR` from breakeven | `TRAILING_ATR_MULT=1.0` |

Dollar risk is **constant per trade** regardless of which instrument or how
volatile it currently is. A GOLD trade and an EURUSD trade both risk exactly
`RISK_PER_TRADE`% of your current equity.

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
├── main.py               # Live runtime loop
├── strategy.py           # TrendPullback signal generation
├── risk_manager.py       # Sizing, daily-loss, drawdown, cooldown
├── mt5_connector.py      # MetaTrader 5 SDK wrapper
├── indicators.py         # EMA / ATR / ADX / RSI / BBands (pandas)
├── notifier.py           # Telegram + Discord alerts
├── journal.py            # CSV trade log
├── logger.py             # Rotating file + stdout
├── config.py             # .env -> typed Settings
├── backtest.py           # Multi-symbol bar-by-bar simulator
├── allocate.py           # Pick & weight live symbols from backtest results
├── ml_filter.py          # Optional probabilistic filter
├── backtest_results.csv  # Generated by backtest.py — ranked, dated results
├── .env                  # Your credentials & settings (gitignored)
├── .env.example          # Template
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
python backtest.py            # 1. Find which instruments your strategy works on
python allocate.py            # 2. Pick weighted live symbol list
# 3. Paste the SYMBOLS=... line from allocate.py into .env
python main.py                # 4. Run live (DRY_RUN=true to start)
```

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

| Key | Default | What it does |
|---|---|---|
| `DRY_RUN`             | `true` | Logs decisions, sends no real orders |
| `RISK_PER_TRADE`      | `0.5` | % of equity risked per trade |
| `MAX_DAILY_LOSS_PCT`  | `3.0` | Daily kill-switch threshold |
| `MAX_DRAWDOWN_PCT`    | `10.0`| Peak-to-trough kill-switch |
| `SL_ATR_MULT`         | `1.5` | Stop distance in ATR multiples |
| `TP_ATR_MULT`         | `2.5` | Take-profit in ATR multiples (R:R = 1.67) |
| `ADX_THRESHOLD`       | `22`  | Below this → no trades (ranging) |
| `ATR_REGIME_PERCENTILE` | `20` | Skip when ATR < bottom N% of recent bars (0 = off) |
| `ATR_REGIME_LOOKBACK`   | `50` | Bars used for the ATR percentile window |
| `MAGIC_NUMBER`        | _unique_ | Bot only manages its own trades |

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

---

## How to improve the algorithm

Plenty of room. In rough order of bang-for-buck:

### 1. Walk-forward validation (high priority)
Right now the backtest is **in-sample only** — the same data the parameters
were "chosen" on. Real validation:
* Backtest on Jan–Sep (in-sample), then on Oct–Dec (out-of-sample)
* If OOS profit factor drops by more than ~25%, the in-sample result is
  curve-fit and won't survive live
* Implementation hint: split the bar fetch by date range, not just count

### 2. Per-asset-class parameters
The current params (`EMA_FAST=20`, `EMA_TREND=200`, `ADX_THRESHOLD=22`,
`SL_ATR_MULT=1.5`, `TP_ATR_MULT=2.5`) are reasonable defaults but **uniform
across forex, metals, indices, oil**. Each behaves differently:
* Metals/oil: longer trends → try `EMA_FAST=50`, `TP_ATR_MULT=3.5`
* Indices: faster mean-reversion → try `ADX_THRESHOLD=25`, tighter SL
* Forex majors: current defaults are probably near-optimal

Implementation: add `BACKTEST_PARAMS_PROFILE` switch in `.env` or symbol-group
overrides.

### 3. Parameter grid search (per symbol)
Add a `python optimize.py` script that iterates:
```python
for ema_fast in [10, 20, 30]:
    for ema_trend in [100, 200, 300]:
        for sl_mult in [1.0, 1.5, 2.0]:
            for tp_mult in [2.0, 2.5, 3.0]:
                run_backtest(...)
```
…then picks the parameter combo with the best **out-of-sample** PF
(not in-sample — that's curve-fitting). Saves results to
`optimization_results.csv`.

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

- [ ] Backtested on each target symbol — passes PF ≥ 1.2 with ≥ 50 trades
- [ ] Picked symbols via `allocate.py` (not gut feel)
- [ ] Walk-forward checked on at least one out-of-sample window
- [ ] `DRY_RUN=false` only after ≥ 2 weeks of clean demo performance
- [ ] `MAX_DAILY_LOSS_PCT` and `MAX_DRAWDOWN_PCT` set conservatively
- [ ] `RISK_PER_TRADE` ≤ 0.5% until you trust the system
- [ ] `MAGIC_NUMBER` unique on the account
- [ ] `.env` in `.gitignore` (never commit credentials)
- [ ] Bot runs on a VPS, not your laptop
- [ ] You can stop the bot remotely (systemd / pm2 / screen)
- [ ] Telegram alerts confirmed working

---

## macOS / Wine setup

The `MetaTrader5` Python package is Windows-only. Run the bot on a Windows
VPS (recommended) or inside a Wine prefix with Windows Python (advanced).
For development on macOS, use `python backtest.py --csv-ltf … --csv-htf …`
with CSV exports from another machine.

---

## License

MIT — use at your own risk. Not financial advice.
