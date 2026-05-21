"""Trading strategy: trend-following with volatility & momentum filters.

Why this architecture
---------------------
Most retail single-indicator systems fail because they ignore *market regime*.
This strategy stacks four orthogonal filters so that trades only fire when
several independent edges agree:

  1. **Trend filter (EMA-200 on higher TF + EMA-50/EMA-20 on entry TF)**
     Trades only in the direction of the dominant trend on the higher
     timeframe — long-only when HTF close > HTF EMA-200, short-only when
     below. This is the single most robust filter for retail FX (see
     Lo's Adaptive Markets, Faber's trend studies).

  2. **Regime filter (ADX)**
     ADX < threshold → ranging market → no trades. The same trend rules
     bleed money in chop, so we sit out. ADX is statistically grounded
     (Wilder, 1978) and well-behaved across asset classes.

  3. **Momentum confirmation (RSI pullback)**
     Enter on a *pullback* inside an established trend (RSI re-crossing
     50 from oversold/overbought). Buying breakouts gets crowded; buying
     measured pullbacks in confirmed trends is the more durable edge.

  4. **Volatility-scaled risk (ATR)**
     Stops and targets scale with ATR — never a fixed pip distance.
     Position size is computed from risk %, so dollar risk is constant
     regardless of which symbol or which volatility regime.

We deliberately avoid: martingale, grid, news-spike scalping, fixed-pip
stops, and "holy grail" indicator stacks. The goal is *survivability*
and a small positive expectancy compounded over many trades — not a
spectacular equity curve.

The Strategy class is intentionally swappable: anything implementing
`generate_signal(df_ltf, df_htf) -> Signal | None` plugs into the runner.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

import pandas as pd

from config import settings
from indicators import adx, atr, ema, rsi

Side = Literal["buy", "sell"]


@dataclass
class Signal:
    side: Side
    entry: float          # reference price (latest close)
    sl: float             # stop loss price
    tp: float             # take profit price
    atr: float            # current ATR, for sizing & trailing
    reason: str           # human-readable why


class TrendPullbackStrategy:
    """Multi-timeframe trend + pullback + regime filter."""

    name = "TrendPullback"

    def __init__(self) -> None:
        self.s = settings

    # ---------------------------------------------------------------- #
    def _ltf_features(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["ema_fast"] = ema(out["close"], self.s.ema_fast)
        out["ema_slow"] = ema(out["close"], self.s.ema_slow)
        out["ema_trend"] = ema(out["close"], self.s.ema_trend)
        out["atr"] = atr(out, self.s.atr_period)
        out["adx"] = adx(out, self.s.adx_period)
        out["rsi"] = rsi(out["close"], self.s.rsi_period)
        return out

    def _htf_bias(self, df_htf: pd.DataFrame) -> Optional[Side]:
        close = df_htf["close"]
        trend = ema(close, self.s.ema_trend)
        if pd.isna(trend.iloc[-1]):
            return None
        if close.iloc[-1] > trend.iloc[-1]:
            return "buy"
        return "sell"

    # ---------------------------------------------------------------- #
    def generate_signal(
        self,
        df_ltf: pd.DataFrame,
        df_htf: pd.DataFrame,
    ) -> Optional[Signal]:
        need = max(self.s.ema_trend, self.s.atr_period, self.s.adx_period) + 5
        if len(df_ltf) < need or len(df_htf) < need:
            return None

        f = self._ltf_features(df_ltf)
        last, prev = f.iloc[-1], f.iloc[-2]

        if pd.isna(last["atr"]) or pd.isna(last["adx"]) or pd.isna(last["ema_trend"]):
            return None

        # 1) Regime: must be trending (ADX) + sufficient volatility (ATR percentile)
        if last["adx"] < self.s.adx_threshold:
            return None

        if self.s.atr_regime_percentile > 0:
            lookback = self.s.atr_regime_lookback
            atr_floor = f["atr"].rolling(lookback).quantile(
                self.s.atr_regime_percentile / 100.0
            ).iloc[-1]
            if not pd.isna(atr_floor) and float(last["atr"]) < atr_floor:
                return None

        # 2) HTF directional bias
        htf_bias = self._htf_bias(df_htf)
        if htf_bias is None:
            return None

        # 3) LTF trend alignment + pullback trigger
        close = last["close"]
        atr_val = float(last["atr"])
        sl_dist = self.s.sl_atr_mult * atr_val
        tp_dist = self.s.tp_atr_mult * atr_val

        bull_align = (
            htf_bias == "buy"
            and close > last["ema_trend"]
            and last["ema_fast"] > last["ema_slow"]
        )
        bear_align = (
            htf_bias == "sell"
            and close < last["ema_trend"]
            and last["ema_fast"] < last["ema_slow"]
        )

        # Pullback trigger: RSI crossed back up through 50 (buy) or down through 50 (sell)
        rsi_now, rsi_prev = float(last["rsi"]), float(prev["rsi"])
        crossed_up = rsi_prev < 50 <= rsi_now and rsi_prev > self.s.rsi_oversold - 5
        crossed_dn = rsi_prev > 50 >= rsi_now and rsi_prev < self.s.rsi_overbought + 5

        if bull_align and crossed_up:
            return Signal(
                side="buy",
                entry=close,
                sl=close - sl_dist,
                tp=close + tp_dist,
                atr=atr_val,
                reason=f"HTF up, EMA stack up, ADX={last['adx']:.1f}, RSI cross↑50",
            )
        if bear_align and crossed_dn:
            return Signal(
                side="sell",
                entry=close,
                sl=close + sl_dist,
                tp=close - tp_dist,
                atr=atr_val,
                reason=f"HTF down, EMA stack down, ADX={last['adx']:.1f}, RSI cross↓50",
            )
        return None
