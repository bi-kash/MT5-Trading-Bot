# `python optimize.py` — Parameter optimization

Finds the best strategy parameters for your live symbols using
**Optuna Bayesian (TPE) search** combined with **walk-forward validation**.
Results are saved to `optimized_params.json`. Set `USE_OPTIMIZED_PARAMS=true`
in `.env` to apply them to the live bot and all backtest runs.

---

## Why walk-forward, not grid search

A plain grid search optimizes on *all* available history. The "best" parameters
are usually the ones that fit quirks of that specific window — not genuine edge.
This is **curve-fitting**.

Walk-forward optimization scores each trial on **N independent sub-periods**
of the training data, using `stability_score = mean_pf × consistency_pct`.
A parameter set that looks great in one lucky window but fails in the other
three will score lower than one that earns modest profits consistently. This
mirrors how `backtest_walkforward.py` ranks symbols — the same logic applied
to parameters.

The held-out **test slice (last 30% of history)** is never used during the
search. After optimization, the best params are validated on the test slice.
A large train/test gap signals overfitting.

---

## Install dependency (one time)

```bash
pip install optuna
```

---

## Quick start

```bash
# Optimize all live SYMBOLS from .env — 300 trials, ~20-40 minutes
python optimize.py

# Then activate the results:
# In .env: USE_OPTIMIZED_PARAMS=true
```

---

## Commands

### Basic run

```bash
python optimize.py
```

Optimizes all symbols in `SYMBOLS=` from `.env`. Uses 300 trials, 50,000
bars of history, 70% train / 30% test split, 4 walk-forward sub-periods.

### Single symbol

```bash
python optimize.py --symbol EURUSD
```

Faster iteration. Useful when you want symbol-specific sanity checks before
running the full multi-symbol optimization.

### Adjust trial count

```bash
python optimize.py --trials 150     # faster, less thorough
python optimize.py --trials 500     # more thorough, ~40-60 min
```

Optuna's TPE typically converges within 100–200 trials for 8 parameters.
300 is the recommended balance of thoroughness vs time.

### Use more history

```bash
python optimize.py --bars 100000
```

More history = more reliable optimization but slower. Requires the data
cache to have enough bars (`python data_cache.py --download-live` first).

### Skip out-of-sample validation

```bash
python optimize.py --no-validate
```

Skips the test-slice validation. Useful if you just want to iterate quickly
and don't need the overfitting check right now.

### Custom output file

```bash
python optimize.py --output my_params_v2.json
```

---

## All options

| Flag | Default | Description |
|------|---------|-------------|
| `--symbol NAME` | all live SYMBOLS | Single symbol to optimize |
| `--trials N` | `300` | Number of Optuna trials |
| `--bars N` | `50000` | LTF history bars per symbol |
| `--train-pct F` | `0.7` | Training fraction (rest = held-out test) |
| `--periods N` | `4` | Walk-forward sub-periods per trial scoring |
| `--no-validate` | false | Skip out-of-sample validation |
| `--output PATH` | `optimized_params.json` | Custom output JSON path |

---

## What is being optimized

Only 8 high-impact parameters are searched. Fixing the rest (EMA_TREND=200,
ATR_PERIOD=14, RSI_PERIOD=14, etc.) keeps the search space small and reduces
overfitting risk.

| Parameter | Search range | What it controls |
|-----------|-------------|-----------------|
| `ema_fast` | 8 – 30 | Short-term momentum sensitivity |
| `ema_slow` | 30 – 80 | Medium-term trend direction |
| `adx_threshold` | 15 – 35 | How strong a trend must be to trade |
| `sl_atr_mult` | 0.8 – 2.5 | Stop loss width (ATR multiples) |
| `tp_atr_mult` | 1.5 – 5.0 | Take-profit distance (ATR multiples) |
| `atr_regime_percentile` | 0 – 30 | Volatility floor filter |
| `trailing_atr_mult` | 0.5 – 3.0 | Trailing stop tightness |
| `breakeven_at_r` | 0.3 – 2.5 | When to move SL to breakeven |

A trial is pruned early if `ema_fast >= ema_slow` (invalid EMA setup) or
`tp_atr_mult < sl_atr_mult × 0.9` (pathological R:R).

---

## Reading the output

```
Parameter Optimization  —  2026-05-25 14:30 UTC
  Symbols   : EURUSD, USDJPY, AUDUSD
  Trials    : 300
  ...

  Trial   25/300  best_score=0.8234  (trial #18)
  Trial   50/300  best_score=0.9102  (trial #43)
  Trial  300/300  best_score=1.1247  (trial #271)

──────────────────────────────────────────────────────────────
BEST PARAMETERS  (train stability_score = 1.1247)
──────────────────────────────────────────────────────────────
  ema_fast                     14         (was 20)  ← changed
  ema_slow                     58         (was 50)  ← changed
  adx_threshold                21.0       (was 19)  ← changed
  sl_atr_mult                  1.2        (was 1.5) ← changed
  tp_atr_mult                  3.2        (was 2.5) ← changed
  atr_regime_percentile        15         (was 20)
  trailing_atr_mult            2.1        (was 1.8) ← changed
  breakeven_at_r               1.8        (was 1.5) ← changed

Validating on held-out test slice (30% of data, 2 periods)...

  Train score  : 1.1247
  Test score   : 0.9833
  Test / Train : 87.43%  ✓  Robust — small overfitting gap
```

**Interpreting the overfitting check:**

| Test / Train ratio | Verdict |
|--------------------|---------|
| ≥ 75% | Robust — small gap, safe to use |
| 50–75% | Moderate degradation — monitor live carefully |
| < 50% | Likely overfitting — try more bars or more symbols |

---

## The output JSON

```json
{
  "optimized_at": "2026-05-25 14:30 UTC",
  "symbols": ["EURUSD", "USDJPY", "AUDUSD"],
  "train_score": 1.1247,
  "test_score": 0.9833,
  "overfitting_ratio": 0.874,
  "params": {
    "ema_fast": 14,
    "ema_slow": 58,
    "adx_threshold": 21.0,
    "sl_atr_mult": 1.2,
    "tp_atr_mult": 3.2,
    "atr_regime_percentile": 15,
    "trailing_atr_mult": 2.1,
    "breakeven_at_r": 1.8
  }
}
```

---

## Activating the results

```ini
# In .env
USE_OPTIMIZED_PARAMS=true
OPTIMIZED_PARAMS_FILE=optimized_params.json   # default, can omit
```

The bot prints at startup which params were loaded and their values:
```
[config] Optimized params loaded from optimized_params.json
         (optimized_at=2026-05-25 14:30 UTC):
         ema_fast=14, ema_slow=58, adx_threshold=21.0, ...
```

Set `USE_OPTIMIZED_PARAMS=false` to revert to `.env` defaults at any time.

---

## Important caveats

1. **Re-optimize after changing strategy variant.** `rsi_cross` and
   `swing_breakout` have different sensitivities to the same parameters.
   Params optimized for one won't be optimal for the other.

2. **Re-optimize after adding/removing symbols.** The global objective
   scores across all supplied symbols — different symbols will pull the
   optimum in different directions.

3. **Re-optimize periodically.** Markets change. Every 3–6 months is
   a reasonable cadence. Compare the new `test_score` to the old one —
   a declining test score suggests the market regime has shifted.

4. **Never optimize on the instrument you're about to trade tomorrow.**
   The test slice handles this partially, but it's not a substitute for
   genuine out-of-sample performance (i.e., live trading).
