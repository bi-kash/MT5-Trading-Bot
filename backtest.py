"""Simple bar-by-bar backtester for the TrendPullback strategy.

Limitations (be honest about these):
  * Single-position-per-symbol, fills at next bar's open.
  * Spread modeled as constant points; no slippage modeled.
  * SL/TP/trailing evaluated against bar high/low — coarse but standard.
  * No swap / commission unless you set them via CLI args.

Run:
  python backtest.py --symbol EURUSD --timeframe M15 --htf H4 --bars 20000
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

import numpy as np
import pandas as pd

from config import settings
from indicators import adx, atr, ema, rsi
from logger import get_logger
from strategy import TrendPullbackStrategy

log = get_logger("backtest")


@dataclass
class Trade:
    entry_time: datetime
    side: str
    entry: float
    sl: float
    tp: float
    volume: float
    exit_time: Optional[datetime] = None
    exit_price: Optional[float] = None
    pnl: float = 0.0
    reason: str = ""


@dataclass
class BacktestResult:
    trades: List[Trade] = field(default_factory=list)
    equity_curve: List[float] = field(default_factory=list)

    def summary(self, starting_equity: float) -> dict:
        n = len(self.trades)
        if n == 0:
            return {"trades": 0}
        pnls = np.array([t.pnl for t in self.trades])
        wins = pnls[pnls > 0]
        losses = pnls[pnls <= 0]
        eq = np.array(self.equity_curve)
        peak = np.maximum.accumulate(eq)
        dd = (peak - eq) / np.where(peak == 0, 1, peak)
        return {
            "trades": n,
            "win_rate": len(wins) / n,
            "avg_win": wins.mean() if len(wins) else 0.0,
            "avg_loss": losses.mean() if len(losses) else 0.0,
            "profit_factor": (wins.sum() / -losses.sum()) if losses.sum() < 0 else float("inf"),
            "expectancy": pnls.mean(),
            "total_pnl": pnls.sum(),
            "ending_equity": starting_equity + pnls.sum(),
            "max_drawdown_pct": float(dd.max() * 100) if len(dd) else 0.0,
        }


def load_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    if "time" in df.columns:
        df["time"] = pd.to_datetime(df["time"], utc=True)
        df.set_index("time", inplace=True)
    df.columns = [c.lower() for c in df.columns]
    return df


def load_mt5(symbol: str, timeframe: str, n_bars: int) -> pd.DataFrame:
    from mt5_connector import MT5Connector
    c = MT5Connector()
    if not c.connect():
        raise RuntimeError("MT5 connect failed")
    try:
        df = c.get_rates(symbol, timeframe, n_bars)
        if df is None:
            raise RuntimeError("No rates returned")
        return df
    finally:
        c.shutdown()


def run_backtest(
    df_ltf: pd.DataFrame,
    df_htf: pd.DataFrame,
    starting_equity: float = 10_000.0,
    pip_value: float = 10.0,        # $ per pip per 1.0 lot (rough EURUSD default)
    pip_size: float = 0.0001,
    spread_pips: float = 1.0,
) -> BacktestResult:
    strat = TrendPullbackStrategy()
    result = BacktestResult()
    equity = starting_equity
    open_trade: Optional[Trade] = None

    # Precompute HTF EMA-200 once and forward-fill onto LTF index
    htf_ema = ema(df_htf["close"], settings.ema_trend).reindex(df_ltf.index, method="ffill")
    htf_close = df_htf["close"].reindex(df_ltf.index, method="ffill")

    # LTF features
    f = df_ltf.copy()
    f["ema_fast"] = ema(f["close"], settings.ema_fast)
    f["ema_slow"] = ema(f["close"], settings.ema_slow)
    f["ema_trend"] = ema(f["close"], settings.ema_trend)
    f["atr"] = atr(f, settings.atr_period)
    f["adx"] = adx(f, settings.adx_period)
    f["rsi"] = rsi(f["close"], settings.rsi_period)
    f["htf_close"] = htf_close
    f["htf_ema"] = htf_ema

    need = max(settings.ema_trend, settings.atr_period, settings.adx_period) + 5

    for i in range(need, len(f) - 1):
        row = f.iloc[i]
        next_bar = f.iloc[i + 1]
        result.equity_curve.append(equity)

        # 1) Manage open trade against the *next* bar (where execution happens)
        if open_trade is not None:
            hit_sl = (open_trade.side == "buy" and next_bar["low"] <= open_trade.sl) or \
                     (open_trade.side == "sell" and next_bar["high"] >= open_trade.sl)
            hit_tp = (open_trade.side == "buy" and next_bar["high"] >= open_trade.tp) or \
                     (open_trade.side == "sell" and next_bar["low"] <= open_trade.tp)
            exit_price = None
            reason = ""
            # If both touched in the same bar, assume worst-case SL first (conservative)
            if hit_sl:
                exit_price, reason = open_trade.sl, "sl"
            elif hit_tp:
                exit_price, reason = open_trade.tp, "tp"

            if exit_price is not None:
                pnl_pips = (exit_price - open_trade.entry) / pip_size
                if open_trade.side == "sell":
                    pnl_pips = -pnl_pips
                pnl_pips -= spread_pips     # cost
                open_trade.exit_time = next_bar.name
                open_trade.exit_price = exit_price
                open_trade.pnl = pnl_pips * pip_value * open_trade.volume
                open_trade.reason = reason
                equity += open_trade.pnl
                result.trades.append(open_trade)
                open_trade = None

        # 2) Look for new signal at bar close → execute at next bar open
        if open_trade is None and not pd.isna(row["atr"]) and not pd.isna(row["adx"]):
            sig = _signal_from_row(f, i)
            if sig is not None:
                # Risk-based sizing in pips
                sl_pips = abs(sig.entry - sig.sl) / pip_size
                if sl_pips > 0:
                    risk_dollars = equity * (settings.risk_per_trade / 100.0)
                    volume = risk_dollars / (sl_pips * pip_value)
                    volume = max(settings.min_lot, min(volume, settings.max_lot))
                    open_trade = Trade(
                        entry_time=next_bar.name,
                        side=sig.side,
                        entry=next_bar["open"],
                        sl=sig.sl,
                        tp=sig.tp,
                        volume=round(volume, 2),
                    )

    return result


def _signal_from_row(f: pd.DataFrame, i: int):
    """Inline signal generator that uses the precomputed-features frame."""
    from strategy import Signal
    last, prev = f.iloc[i], f.iloc[i - 1]
    s = settings
    if last["adx"] < s.adx_threshold:
        return None
    htf_bias = "buy" if last["htf_close"] > last["htf_ema"] else "sell"
    close = last["close"]
    atr_val = float(last["atr"])
    sl_dist = s.sl_atr_mult * atr_val
    tp_dist = s.tp_atr_mult * atr_val
    bull = htf_bias == "buy" and close > last["ema_trend"] and last["ema_fast"] > last["ema_slow"]
    bear = htf_bias == "sell" and close < last["ema_trend"] and last["ema_fast"] < last["ema_slow"]
    crossed_up = prev["rsi"] < 50 <= last["rsi"]
    crossed_dn = prev["rsi"] > 50 >= last["rsi"]
    if bull and crossed_up:
        return Signal("buy", close, close - sl_dist, close + tp_dist, atr_val, "")
    if bear and crossed_dn:
        return Signal("sell", close, close + sl_dist, close - tp_dist, atr_val, "")
    return None


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", default=settings.symbols[0])
    p.add_argument("--timeframe", default=settings.timeframe)
    p.add_argument("--htf", default=settings.higher_timeframe)
    p.add_argument("--bars", type=int, default=20_000)
    p.add_argument("--equity", type=float, default=10_000.0)
    p.add_argument("--pip-value", type=float, default=10.0)
    p.add_argument("--pip-size", type=float, default=0.0001)
    p.add_argument("--spread-pips", type=float, default=1.0)
    p.add_argument("--csv-ltf", help="Optional CSV file instead of MT5")
    p.add_argument("--csv-htf", help="Optional CSV file instead of MT5")
    args = p.parse_args()

    if args.csv_ltf and args.csv_htf:
        ltf = load_csv(args.csv_ltf)
        htf = load_csv(args.csv_htf)
    else:
        ltf = load_mt5(args.symbol, args.timeframe, args.bars)
        htf = load_mt5(args.symbol, args.htf, max(2000, args.bars // 4))

    log.info("LTF bars: %d, HTF bars: %d", len(ltf), len(htf))
    result = run_backtest(
        ltf, htf, starting_equity=args.equity,
        pip_value=args.pip_value, pip_size=args.pip_size,
        spread_pips=args.spread_pips,
    )
    summary = result.summary(args.equity)
    print("\n=== Backtest Summary ===")
    for k, v in summary.items():
        print(f"{k:>18}: {v}")


if __name__ == "__main__":
    main()
