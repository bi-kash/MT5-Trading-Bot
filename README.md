# MT5 Trading Bot (Python)

A production-leaning algorithmic trading bot for **MetaTrader 5**, built around
disciplined risk management and a multi-filter trend-pullback strategy.

> ⚠️ **No guarantees of profit.** Markets are adversarial. This bot is designed
> for *survivability* and a small positive edge compounded over many trades —
> not for hype-grade returns. Always demo first.

---

## Why this strategy

Most retail systems lose because they ignore market regime. This bot stacks
**four orthogonal filters** that must all agree before a trade fires:

| Filter | Indicator | Purpose |
|---|---|---|
| Higher-TF trend bias | EMA-200 on H4 | Only trade *with* the dominant trend |
| LTF trend alignment  | EMA-20 / EMA-50 / EMA-200 stack | Avoid counter-trend noise |
| Regime               | ADX > 22 | Skip ranging markets where trend rules bleed |
| Momentum pullback    | RSI crossing 50 from extremes | Enter on dips, not breakouts |

Risk is **ATR-scaled**: stop loss and take-profit distances adapt to current
volatility, position size is computed so dollar-risk per trade is constant.
A daily-loss kill switch, peak-equity drawdown guard, and consecutive-loss
cooldown round it out.

**Explicitly avoided**: martingale, grid, fixed-pip stops, news-spike scalping.

---

## Project structure

```
.
├── main.py            # Runtime loop
├── strategy.py        # TrendPullback (swap-in your own)
├── risk_manager.py    # Sizing, daily-loss, drawdown, cooldown
├── mt5_connector.py   # MetaTrader 5 SDK wrapper
├── indicators.py      # EMA, ATR, ADX, RSI, BBands (pandas)
├── ml_filter.py       # Optional probabilistic edge filter
├── notifier.py        # Telegram + Discord
├── journal.py         # CSV trade log
├── logger.py          # Rotating file + stdout
├── config.py          # .env -> typed Settings
├── backtest.py        # Bar-by-bar simulator
├── .env.example
└── requirements.txt
```

---

## macOS setup

The official `MetaTrader5` Python package is **Windows-only**. On macOS you have
two paths:

### Option A — Run MT5 via CrossOver / Wine (recommended)

1. Install **CrossOver** (https://www.codeweavers.com/crossover) or set up a
   Wine bottle:
   ```bash
   brew install --cask wine-stable
   ```
2. Inside Wine/CrossOver install your broker's **MetaTrader 5** terminal.
3. Inside the *same Wine prefix*, install Python 3.11 (Windows build) and run:
   ```cmd
   pip install -r requirements.txt
   python main.py
   ```
   The Python that talks to MT5 must run **inside the same Wine prefix** as
   the terminal — that's how `MetaTrader5` finds it.
4. Set `MT5_PATH` in `.env` to the Wine path of `terminal64.exe`, e.g.
   `C:\\Program Files\\MetaTrader 5\\terminal64.exe`.

### Option B — Linux/Windows VPS (most common in production)

Run the bot on a small Windows VPS near your broker (lower latency, no Wine
quirks). Most prop firms expect this. Use macOS only for development and
backtesting in that case.

### Option C — Backtest-only on native macOS

You can run `backtest.py` against CSV bar data on macOS without installing
the `MetaTrader5` package — just provide `--csv-ltf` and `--csv-htf` files.

---

## Installation

```bash
git init  # if not already a repo
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env with your credentials & risk settings
```

---

## Configuration

All settings live in `.env` — see `.env.example` for the full list with
inline documentation. The most important knobs:

| Key | Meaning |
|---|---|
| `DRY_RUN`           | `true` = no real orders; logs decisions only |
| `RISK_PER_TRADE`    | % of equity risked per trade (start at 0.25–0.5) |
| `MAX_DAILY_LOSS_PCT`| Daily loss kill-switch |
| `MAX_DRAWDOWN_PCT`  | Equity-from-peak kill-switch |
| `SL_ATR_MULT`       | Stop distance in ATR multiples |
| `TP_ATR_MULT`       | Take-profit in ATR multiples |
| `ADX_THRESHOLD`     | Below this → no trades (ranging market) |
| `MAGIC_NUMBER`      | Unique ID so the bot only manages its own trades |

---

## Running

```bash
# 1) Always start with DRY_RUN=true to verify signals & sizing
python main.py

# 2) When confident, set DRY_RUN=false and run on a DEMO account
#    for at least 2-4 weeks across different sessions.

# 3) Only then consider live capital, and start with the minimum lot.
```

Logs stream to stdout and to `logs/bot.log` (rotating). Every trade also
appends a row to `logs/trade_journal.csv`.

---

## Backtesting

```bash
# Backtest using bars pulled from your connected MT5 terminal
python backtest.py --symbol EURUSD --timeframe M15 --htf H4 --bars 30000

# Or from CSV files (columns: time, open, high, low, close, tick_volume)
python backtest.py --csv-ltf data/EURUSD_M15.csv --csv-htf data/EURUSD_H4.csv
```

The backtester reports trades, win rate, profit factor, expectancy, total P/L
and max drawdown. **Be skeptical of any in-sample result.** Always:

1. Reserve the last ~30% of your data as an **out-of-sample** holdout.
2. Walk-forward optimize the major parameters (`EMA_FAST`, `EMA_SLOW`,
   `ADX_THRESHOLD`, `SL_ATR_MULT`, `TP_ATR_MULT`). Avoid optimizing more
   than 4–5 knobs simultaneously — you will curve-fit.
3. Test on **multiple symbols** and **multiple market regimes** (trending
   year, ranging year, high-vol crisis period). If results survive all
   three, the edge is more likely real.

---

## Telegram / Discord notifications

Create a Telegram bot via `@BotFather`, get the token, then DM the bot and
visit `https://api.telegram.org/bot<TOKEN>/getUpdates` to find your chat ID.
Put both in `.env`. Discord uses a standard webhook URL.

---

## Prop-firm accounts

The bot is compatible with any broker that exposes MT5 (FTMO, MyForexFunds,
The5%ers, etc.), but **read your firm's rule sheet first**:

* Most firms cap **daily loss** and **overall drawdown**. Set
  `MAX_DAILY_LOSS_PCT` and `MAX_DRAWDOWN_PCT` *below* the firm's limits.
* Many forbid **martingale**, **grid**, and **hedging** between accounts.
  None of those are used here, but verify.
* Some require minimum trading days — adjust `MAX_TRADES_PER_DAY` so you
  don't blow the count in one session.
* News-trading restrictions: consider blocking high-impact news windows
  (not implemented here — open a TODO if you need it).

---

## Safety checklist before going live

- [ ] `DRY_RUN=false` only after ≥2 weeks of clean demo performance
- [ ] `MAX_DAILY_LOSS_PCT` and `MAX_DRAWDOWN_PCT` set conservatively
- [ ] `RISK_PER_TRADE` ≤ 0.5 % until you trust the system
- [ ] `MAGIC_NUMBER` is unique to this bot on the account
- [ ] `.env` is in `.gitignore`
- [ ] Bot runs on a VPS, not your laptop
- [ ] You can stop the bot remotely (e.g. systemd / pm2 / screen)
- [ ] Telegram alerts confirmed working

---

## License

MIT — use at your own risk. Not financial advice.
