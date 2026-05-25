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

    # ---------------------------------------------------------------- #
    def diagnose(self, df_ltf: pd.DataFrame, df_htf: pd.DataFrame) -> str:
        """Return a one-line description of the gate that blocked this bar."""
        f = self._ltf_features(df_ltf)
        last, prev = f.iloc[-1], f.iloc[-2]
        adx_val = float(last.get("adx", float("nan")))
        rsi_now = float(last.get("rsi", float("nan")))
        rsi_prev = float(prev.get("rsi", float("nan")))
        close = float(last.get("close", 0))
        ema_fast = float(last.get("ema_fast", 0))
        ema_slow = float(last.get("ema_slow", 0))
        ema_trend_val = float(last.get("ema_trend", 0))
        htf_bias = self._htf_bias(df_htf)

        bull_align = htf_bias == "buy" and close > ema_trend_val and ema_fast > ema_slow
        bear_align = htf_bias == "sell" and close < ema_trend_val and ema_fast < ema_slow
        crossed_up = rsi_prev < 50 <= rsi_now and rsi_prev > self.s.rsi_oversold - 5
        crossed_dn = rsi_prev > 50 >= rsi_now and rsi_prev < self.s.rsi_overbought + 5

        if adx_val < self.s.adx_threshold:
            return f"ADX={adx_val:.1f} < {self.s.adx_threshold:.0f} (weak trend — needs to strengthen)"
        if not (bull_align or bear_align):
            return (f"no alignment: HTF={htf_bias} EMAfast>slow={ema_fast>ema_slow} "
                    f"close>EMA200={close>ema_trend_val}")
        if not (crossed_up or crossed_dn):
            if bull_align:
                if rsi_now < 50:
                    arrow = "↑" if rsi_now > rsi_prev else "↓"
                    gap = 50.0 - rsi_now
                    if rsi_now > rsi_prev:
                        return f"RSI={rsi_now:.1f}{arrow} rising, {gap:.1f}pts from BUY trigger (needs cross above 50)"
                    return f"RSI={rsi_now:.1f}{arrow} falling (pullback deepening), wait for bottom then cross above 50"
                return f"RSI={rsi_now:.1f} already above 50 — no fresh cross, wait for dip below 50 first"
            if rsi_now > 50:
                arrow = "↓" if rsi_now < rsi_prev else "↑"
                gap = rsi_now - 50.0
                if rsi_now < rsi_prev:
                    return f"RSI={rsi_now:.1f}{arrow} falling, {gap:.1f}pts from SELL trigger (needs cross below 50)"
                return f"RSI={rsi_now:.1f}{arrow} rising (pullback deepening), wait for top then cross below 50"
            return f"RSI={rsi_now:.1f} already below 50 — no fresh cross, wait for rally above 50 first"
        return f"ATR={float(last['atr']):.5f} below volatility floor (ATR regime filter)"


class SwingBreakoutStrategy:
    """Trend-pullback with price-structure entry.

    Same trend stack (HTF EMA-200 + LTF EMA stack + ADX) as TrendPullbackStrategy
    but the entry trigger is *price-based*, not a single RSI bar event:

        BUY  = current close breaks above the highest high of the last N bars
               AND RSI dipped below 50 within those N bars (pullback happened)
        SELL = mirror: close breaks below last N-bar low AND RSI rallied above 50

    Why this is often better than the RSI-50 crossover:
      • Catches strong sustained trends where RSI never fully dips below 50
      • Trigger is objective (price levels) not a single-bar event
      • Catches shallow pullbacks too — RSI only needs to touch <50 at any point
    """

    name = "SwingBreakout"

    def __init__(self) -> None:
        self.s = settings

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
        return "buy" if close.iloc[-1] > trend.iloc[-1] else "sell"

    def generate_signal(
        self,
        df_ltf: pd.DataFrame,
        df_htf: pd.DataFrame,
    ) -> Optional[Signal]:
        n = max(self.s.swing_lookback, 2)
        need = max(self.s.ema_trend, self.s.atr_period, self.s.adx_period, n) + 5
        if len(df_ltf) < need or len(df_htf) < need:
            return None

        f = self._ltf_features(df_ltf)
        last = f.iloc[-1]
        if pd.isna(last["atr"]) or pd.isna(last["adx"]) or pd.isna(last["ema_trend"]):
            return None

        # Regime: ADX trend + ATR percentile
        if last["adx"] < self.s.adx_threshold:
            return None
        if self.s.atr_regime_percentile > 0:
            lookback = self.s.atr_regime_lookback
            atr_floor = f["atr"].rolling(lookback).quantile(
                self.s.atr_regime_percentile / 100.0
            ).iloc[-1]
            if not pd.isna(atr_floor) and float(last["atr"]) < atr_floor:
                return None

        htf_bias = self._htf_bias(df_htf)
        if htf_bias is None:
            return None

        close = float(last["close"])
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

        # Window = N bars BEFORE current bar
        window = f.iloc[-(n + 1):-1]

        if bull_align:
            rsi_dipped = bool((window["rsi"] < 50).any())
            swing_high = float(window["high"].max())
            if rsi_dipped and close > swing_high:
                return Signal(
                    side="buy",
                    entry=close,
                    sl=close - sl_dist,
                    tp=close + tp_dist,
                    atr=atr_val,
                    reason=(f"HTF up, EMA up, ADX={float(last['adx']):.1f}, "
                            f"RSI dipped<50 in last {n} bars, broke above {swing_high:.5f}"),
                )
        if bear_align:
            rsi_rallied = bool((window["rsi"] > 50).any())
            swing_low = float(window["low"].min())
            if rsi_rallied and close < swing_low:
                return Signal(
                    side="sell",
                    entry=close,
                    sl=close + sl_dist,
                    tp=close - tp_dist,
                    atr=atr_val,
                    reason=(f"HTF down, EMA down, ADX={float(last['adx']):.1f}, "
                            f"RSI rallied>50 in last {n} bars, broke below {swing_low:.5f}"),
                )
        return None

    def diagnose(self, df_ltf: pd.DataFrame, df_htf: pd.DataFrame) -> str:
        n = max(self.s.swing_lookback, 2)
        f = self._ltf_features(df_ltf)
        last = f.iloc[-1]
        adx_val = float(last.get("adx", float("nan")))
        rsi_now = float(last.get("rsi", float("nan")))
        close = float(last.get("close", 0))
        ema_fast = float(last.get("ema_fast", 0))
        ema_slow = float(last.get("ema_slow", 0))
        ema_trend_val = float(last.get("ema_trend", 0))
        htf_bias = self._htf_bias(df_htf)
        bull_align = htf_bias == "buy" and close > ema_trend_val and ema_fast > ema_slow
        bear_align = htf_bias == "sell" and close < ema_trend_val and ema_fast < ema_slow

        if adx_val < self.s.adx_threshold:
            return f"ADX={adx_val:.1f} < {self.s.adx_threshold:.0f} (weak trend)"
        if not (bull_align or bear_align):
            return (f"no alignment: HTF={htf_bias} EMAfast>slow={ema_fast>ema_slow} "
                    f"close>EMA200={close>ema_trend_val}")

        window = f.iloc[-(n + 1):-1]
        if bull_align:
            rsi_dipped = bool((window["rsi"] < 50).any())
            swing_high = float(window["high"].max())
            dist = swing_high - close
            if not rsi_dipped:
                return (f"no pullback yet — RSI hasn't dipped below 50 in last {n} bars "
                        f"(min RSI={float(window['rsi'].min()):.1f}). Wait for pullback.")
            return (f"pullback OK (RSI dipped<50), waiting for breakout above {swing_high:.5f} "
                    f"(close={close:.5f}, {dist:.5f} below trigger)")
        # bear_align
        rsi_rallied = bool((window["rsi"] > 50).any())
        swing_low = float(window["low"].min())
        dist = close - swing_low
        if not rsi_rallied:
            return (f"no pullback yet — RSI hasn't rallied above 50 in last {n} bars "
                    f"(max RSI={float(window['rsi'].max()):.1f}). Wait for pullback.")
        return (f"pullback OK (RSI rallied>50), waiting for breakdown below {swing_low:.5f} "
                f"(close={close:.5f}, {dist:.5f} above trigger)")


def make_strategy():
    """Factory — returns the strategy class selected via STRATEGY_VARIANT in .env."""
    variant = (settings.strategy_variant or "rsi_cross").lower()
    if variant == "swing_breakout":
        return SwingBreakoutStrategy()
    if variant == "rsi_cross":
        return TrendPullbackStrategy()
    raise ValueError(
        f"Unknown STRATEGY_VARIANT={variant!r}. "
        f"Valid values: 'rsi_cross', 'swing_breakout'"
    )
