"""Bar-by-bar backtester for the TrendPullback strategy.

Works on ANY instrument that MT5 exposes — forex, metals, indices, futures,
stock CFDs, crypto — because P&L is computed from each symbol's native
tick_size/tick_value rather than a forex pip assumption.

All parameters come from .env via config.py. Common runs:

    # Scan the built-in 28-symbol forex+metals list
    python backtest.py

    # Auto-discover every visible symbol on the MT5 terminal
    # (set BACKTEST_ALL_SYMBOLS=true in .env, then:)
    python backtest.py

    # One-off single-symbol test
    python backtest.py --symbol US500

Results are written to BACKTEST_OUTPUT_CSV with a `backtested_at` column so
you can track when each symbol was last evaluated.
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import numpy as np
import pandas as pd

from config import apply_symbol_params, settings
from indicators import adx, atr, ema, rsi
from logger import get_logger

log = get_logger("backtest")

# ── Built-in forex/metals scan list (used when BACKTEST_SCAN_ALL=true) ────── #
_DEFAULT_SCAN_SYMBOLS: List[str] = [
    "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD", "NZDUSD",
    "EURGBP", "EURJPY", "EURCAD", "EURAUD", "EURNZD", "EURCHF",
    "GBPJPY", "GBPAUD", "GBPCAD", "GBPNZD", "GBPCHF",
    "AUDJPY", "AUDCAD", "AUDCHF", "AUDNZD",
    "CADJPY", "CADCHF", "CHFJPY", "NZDJPY", "NZDCAD", "NZDCHF",
    "XAUUSD", "XAGUSD",
]


# ── Instrument-info helper ────────────────────────────────────────────────── #

@dataclass
class InstrumentInfo:
    """Normalised symbol metadata pulled from MT5."""
    name: str
    tick_size: float        # smallest price increment (info.trade_tick_size)
    tick_value: float       # account-currency value of one tick per 1 lot
    digits: int             # display digits
    spread_ticks: float     # broker's current spread, in ticks
    volume_min: float
    volume_max: float
    volume_step: float
    path: str               # MT5 symbol group path (e.g. "Forex\Majors")


def _get_instrument(connector, symbol: str) -> Optional[InstrumentInfo]:
    """Fetch and validate symbol metadata. Returns None if symbol is unusable."""
    info = connector.symbol_info(symbol)
    if info is None or info.trade_tick_size <= 0 or info.trade_tick_value <= 0:
        return None
    return InstrumentInfo(
        name=symbol,
        tick_size=float(info.trade_tick_size),
        tick_value=float(info.trade_tick_value),
        digits=int(info.digits),
        spread_ticks=float(info.spread),
        volume_min=float(info.volume_min or 0.01),
        volume_max=float(info.volume_max or 100.0),
        volume_step=float(info.volume_step or 0.01),
        path=str(info.path or ""),
    )


def _resolve_spread_ticks(inst: InstrumentInfo, override_pips: float) -> float:
    """Convert the BACKTEST_SPREAD_PIPS setting into ticks for this instrument.

    0 -> use broker's current spread.
    >0 -> treat as forex pips and convert: 1 pip = 10 ticks on 5/3-digit
    brokers, else 1 pip = 1 tick.
    """
    if override_pips <= 0:
        return inst.spread_ticks
    if inst.digits in (3, 5):
        return override_pips * 10.0
    return override_pips


def _round_volume(volume: float, inst: InstrumentInfo) -> float:
    """Round volume to the broker's lot step, clamped to [min, max]."""
    if volume < inst.volume_min:
        return inst.volume_min
    steps = round(volume / inst.volume_step)
    v = steps * inst.volume_step
    v = max(inst.volume_min, min(v, inst.volume_max))
    return round(v, 8)


# ── Data structures ───────────────────────────────────────────────────────── #

@dataclass
class Trade:
    entry_time: datetime
    side: str
    entry: float
    sl: float
    tp: float
    volume: float
    risk_usd: float                          # dollar amount at risk at entry
    exit_time: Optional[datetime] = None
    exit_price: Optional[float] = None
    pnl: float = 0.0                         # USD (account currency, really)
    pnl_r: float = 0.0                       # R-multiples (pnl / risk_usd)
    reason: str = ""


@dataclass
class BacktestResult:
    trades: List[Trade] = field(default_factory=list)
    equity_curve: List[float] = field(default_factory=list)

    def summary(self, starting_equity: float) -> dict:
        base = {
            "trades": 0, "win_rate": 0.0, "profit_factor": 0.0,
            "expectancy_r": 0.0, "total_r": 0.0,
            "total_pnl": 0.0, "ending_equity": round(starting_equity, 2),
            "max_drawdown_pct": 0.0, "avg_win_r": 0.0, "avg_loss_r": 0.0,
        }
        n = len(self.trades)
        if n == 0:
            return base

        r = np.array([t.pnl_r for t in self.trades])
        usd = np.array([t.pnl for t in self.trades])
        win_mask = r > 0
        wins, losses = r[win_mask], r[~win_mask]

        eq = np.array(self.equity_curve) if self.equity_curve else np.array([starting_equity])
        peak = np.maximum.accumulate(eq)
        dd = (peak - eq) / np.where(peak == 0, 1, peak)

        gross_w = float(wins.sum()) if len(wins) else 0.0
        gross_l = float(abs(losses.sum())) if len(losses) else 0.0

        return {
            "trades": n,
            "win_rate": round(len(wins) / n * 100, 1),
            "profit_factor": round(gross_w / gross_l, 2) if gross_l > 0 else float("inf"),
            "expectancy_r": round(float(r.mean()), 3),
            "total_r": round(float(r.sum()), 2),
            "total_pnl": round(float(usd.sum()), 2),
            "ending_equity": round(starting_equity + float(usd.sum()), 2),
            "max_drawdown_pct": round(float(dd.max() * 100), 2),
            "avg_win_r": round(float(wins.mean()) if len(wins) else 0.0, 2),
            "avg_loss_r": round(float(losses.mean()) if len(losses) else 0.0, 2),
        }


# ── Data loading ──────────────────────────────────────────────────────────── #

def load_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    if "time" in df.columns:
        df["time"] = pd.to_datetime(df["time"], utc=True)
        df.set_index("time", inplace=True)
    df.columns = [c.lower() for c in df.columns]
    return df


def load_mt5(symbol: str, timeframe: str, n_bars: int, connector=None, cache=None) -> pd.DataFrame:
    """Load OHLCV bars for backtest.

    Routing:
      - If `cache` is provided (and USE_DATA_CACHE=true), serve from local M1
        cache with auto-refresh — fast on repeat runs.
      - Else fall through to direct MT5 fetch via `connector` (or open a
        temporary connection if no connector is supplied).
    """
    if cache is not None and settings.use_data_cache:
        df = cache.get_rates(symbol, timeframe, n_bars=n_bars)
        if df is not None and not df.empty:
            return df
        log.warning("[%s/%s] cache returned no data — falling back to direct MT5 fetch",
                    symbol, timeframe)

    if connector is not None:
        df = connector.get_rates(symbol, timeframe, n_bars)
        if df is None:
            raise RuntimeError(f"No rates for {symbol}/{timeframe}")
        return df

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


# ── Instrument classification & quality pre-filter ───────────────────────── #

# Known major currency codes — used to detect forex pairs by name
_FX_CURRENCIES = {
    "EUR", "GBP", "USD", "JPY", "CHF", "CAD", "AUD", "NZD",
    "SEK", "NOK", "DKK", "SGD", "HKD", "MXN", "ZAR", "TRY",
    "PLN", "CZK", "HUF", "RUB", "CNH", "CNY", "SAR", "AED",
}

# Well-known index name fragments (broker-agnostic)
_INDEX_FRAGMENTS = {
    "US30", "US500", "US100", "NAS100", "NAS", "SP500", "SPX",
    "DJIA", "DOW", "GER40", "GER30", "DAX", "UK100", "FTSE",
    "JPN225", "NKY", "AUS200", "HK50", "FRA40", "ESP35", "VIX",
    "EU50", "STOXX", "SMI", "CAC", "IBEX", "ASX",
}

# Known crypto tickers
_CRYPTO_TICKERS = {
    "BTC", "ETH", "LTC", "XRP", "ADA", "SOL", "DOT", "DOGE",
    "BNB", "AVAX", "MATIC", "LINK", "UNI", "ATOM", "XLM", "TRX",
}


def classify_instrument(path: str, name: str) -> str:
    """Return a broad instrument type for filtering purposes.

    Works across brokers because it checks the symbol name first and
    falls back to the MT5 group path.  Returns one of:
      forex | indices | metals | energy | crypto | stocks | futures | other
    """
    p = (path or "").lower().replace("/", "\\")
    n = (name or "").upper().rstrip("+-!")   # strip broker decorators

    # ── Crypto (name-based is most reliable) ──────────────────────────────
    if any(c in n for c in _CRYPTO_TICKERS) or "crypto" in p or "bitcoin" in p:
        return "crypto"

    # ── Metals (XAU/XAG/XPT/XPD are universal ISO codes) ─────────────────
    if n[:3] in {"XAU", "XAG", "XPT", "XPD"} or n[3:6] in {"XAU", "XAG"}:
        return "metals"
    if any(x in p for x in ("metal", "gold", "silver", "platinum", "palladium")):
        return "metals"

    # ── Energy ────────────────────────────────────────────────────────────
    energy_path = ("oil", "energy", "crude", "brent", "ngas", "gas", "wti",
                   "gasoline", "heating")
    energy_name = ("OIL", "WTI", "BRENT", "NGAS", "USOIL", "UKOIL",
                   "XBRUSD", "XTIUSD", "CL.", "NG.")
    if any(x in p for x in energy_path) or any(x in n for x in energy_name):
        return "energy"

    # ── Indices (name fragments first, then path) ─────────────────────────
    if n.startswith("#") or n.startswith("."):
        return "indices"
    if any(frag in n for frag in _INDEX_FRAGMENTS):
        return "indices"
    if any(x in p for x in ("index", "indice", "indices")):
        return "indices"

    # ── Futures ───────────────────────────────────────────────────────────
    if any(x in p for x in ("future", "futures", "fut")):
        return "futures"

    # ── Forex (6-char from two known currency codes, or with common suffixes)
    bare = re.sub(r'[._+\-]', '', n)   # strip separators some brokers add
    if len(bare) == 6 and bare[:3] in _FX_CURRENCIES and bare[3:] in _FX_CURRENCIES:
        return "forex"

    # ── Stocks (exchange-suffix pattern: AAPL.US, MSFT.UK, BMW.DE, …) ─────
    if re.search(r'\.[A-Z]{2,3}$', n):
        return "stocks"
    if any(x in p for x in ("stock", "share", "equity", "nyse", "nasdaq",
                             "lse", "asx", "tsx", "hkex", "euronext")):
        return "stocks"

    return "other"


def _quality_ok(info, allowed_types: List[str],
                max_spread_pct: float, max_spread_ticks: int,
                exclude_re) -> tuple:
    """Pre-screen a symbol using only symbol_info (no bar data needed).

    Returns (passes: bool, reason: str).
    """
    import MetaTrader5 as mt5

    # Must be tradeable
    if hasattr(mt5, "SYMBOL_TRADE_MODE_DISABLED") and \
            info.trade_mode == mt5.SYMBOL_TRADE_MODE_DISABLED:
        return False, "trade disabled"

    # Must have valid tick data
    if info.trade_tick_size <= 0 or info.trade_tick_value <= 0:
        return False, "invalid tick data"

    # Spread — absolute ticks
    if max_spread_ticks > 0 and info.spread > max_spread_ticks:
        return False, f"spread {info.spread} ticks"

    # Spread — % of mid-price (catches penny stocks / illiquid exotics)
    if max_spread_pct > 0:
        mid = (info.ask + info.bid) / 2.0 if info.ask > 0 and info.bid > 0 else info.ask
        if mid > 0:
            pct = (info.spread * float(info.point) / mid) * 100.0
            if pct > max_spread_pct:
                return False, f"spread {pct:.3f}% > {max_spread_pct}%"

    # Instrument type whitelist
    if allowed_types:
        itype = classify_instrument(info.path or "", info.name)
        if itype not in allowed_types:
            return False, f"type={itype}"

    # Exclusion regex
    if exclude_re and exclude_re.search(info.name):
        return False, "matches exclude pattern"

    return True, ""


def discover_symbols(connector,
                     all_visible: bool,
                     name_filter: str,
                     group_prefixes: List[str]) -> List[str]:
    """Pull symbols from MT5, apply path/regex filters AND quality pre-screen."""
    import MetaTrader5 as mt5

    syms = mt5.symbols_get()
    if syms is None:
        log.error("mt5.symbols_get() returned None")
        return []

    include_re  = re.compile(name_filter)   if name_filter                           else None
    exclude_re  = re.compile(settings.backtest_exclude_filter) \
                  if settings.backtest_exclude_filter else None
    norm_groups = [g.replace("/", "\\") for g in group_prefixes]
    allowed     = [t.lower().strip() for t in settings.backtest_instrument_types]

    passed: List[str] = []
    rejected_counts: dict = {}

    for s in syms:
        # Visibility
        if not s.visible:
            if not all_visible:
                continue
            mt5.symbol_select(s.name, True)

        # Path/group prefix filter
        if norm_groups:
            path_norm = (s.path or "").replace("/", "\\")
            if not any(path_norm.startswith(g) for g in norm_groups):
                rejected_counts["group"] = rejected_counts.get("group", 0) + 1
                continue

        # Name include regex
        if include_re and not include_re.search(s.name):
            rejected_counts["name_filter"] = rejected_counts.get("name_filter", 0) + 1
            continue

        # Quality pre-screen (uses symbol_info — no bar data)
        ok, reason = _quality_ok(
            s,
            allowed_types=allowed,
            max_spread_pct=settings.backtest_max_spread_pct,
            max_spread_ticks=settings.backtest_max_spread_ticks,
            exclude_re=exclude_re,
        )
        if not ok:
            rejected_counts[reason.split()[0]] = \
                rejected_counts.get(reason.split()[0], 0) + 1
            continue

        passed.append(s.name)

    total = len(syms)
    kept  = len(passed)
    log.info(
        "Symbol filter: %d total → %d kept, %d removed  breakdown=%s",
        total, kept, total - kept, rejected_counts,
    )
    print(f"  Symbols in MT5 : {total:,}")
    print(f"  After filters  : {kept:,}  "
          f"(removed: {total - kept:,} — see log for breakdown)")

    return sorted(set(passed))


# ── Core simulation ───────────────────────────────────────────────────────── #

def run_backtest(
    df_ltf: pd.DataFrame,
    df_htf: pd.DataFrame,
    inst: InstrumentInfo,
    starting_equity: float,
    spread_ticks: float,
) -> BacktestResult:
    result = BacktestResult()
    equity = starting_equity
    open_trade: Optional[Trade] = None

    htf_ema_vals = ema(df_htf["close"], settings.ema_trend).reindex(df_ltf.index, method="ffill")
    htf_close = df_htf["close"].reindex(df_ltf.index, method="ffill")

    f = df_ltf.copy()
    f["ema_fast"] = ema(f["close"], settings.ema_fast)
    f["ema_slow"] = ema(f["close"], settings.ema_slow)
    f["ema_trend"] = ema(f["close"], settings.ema_trend)
    f["atr"] = atr(f, settings.atr_period)
    f["adx"] = adx(f, settings.adx_period)
    f["rsi"] = rsi(f["close"], settings.rsi_period)
    f["htf_close"] = htf_close
    f["htf_ema"] = htf_ema_vals

    need = max(settings.ema_trend, settings.atr_period, settings.adx_period) + 5
    if len(f) <= need + 5:
        return result   # not enough bars

    for i in range(need, len(f) - 1):
        row = f.iloc[i]
        next_bar = f.iloc[i + 1]
        result.equity_curve.append(equity)

        # Manage open trade against next bar's range
        if open_trade is not None:
            hit_sl = (open_trade.side == "buy"  and next_bar["low"]  <= open_trade.sl) or \
                     (open_trade.side == "sell" and next_bar["high"] >= open_trade.sl)
            hit_tp = (open_trade.side == "buy"  and next_bar["high"] >= open_trade.tp) or \
                     (open_trade.side == "sell" and next_bar["low"]  <= open_trade.tp)
            exit_price, reason = None, ""
            if hit_sl:
                exit_price, reason = open_trade.sl, "sl"
            elif hit_tp:
                exit_price, reason = open_trade.tp, "tp"

            if exit_price is not None:
                price_change = exit_price - open_trade.entry
                if open_trade.side == "sell":
                    price_change = -price_change
                gross_ticks = price_change / inst.tick_size
                net_ticks = gross_ticks - spread_ticks
                pnl_usd = net_ticks * inst.tick_value * open_trade.volume

                open_trade.exit_time = next_bar.name
                open_trade.exit_price = exit_price
                open_trade.pnl = pnl_usd
                open_trade.pnl_r = pnl_usd / open_trade.risk_usd if open_trade.risk_usd > 0 else 0.0
                open_trade.reason = reason
                equity += pnl_usd
                result.trades.append(open_trade)
                open_trade = None

        # Signal at bar close → fill at next bar open
        if open_trade is None and not pd.isna(row["atr"]) and not pd.isna(row["adx"]):
            sig = _signal_from_row(f, i)
            if sig is not None:
                sl_dist_price = abs(sig.entry - sig.sl)
                sl_dist_ticks = sl_dist_price / inst.tick_size
                if sl_dist_ticks > 0:
                    risk_dollars = equity * (settings.risk_per_trade / 100.0)
                    raw_volume = risk_dollars / (sl_dist_ticks * inst.tick_value)
                    volume = _round_volume(raw_volume, inst)
                    actual_risk = volume * sl_dist_ticks * inst.tick_value
                    open_trade = Trade(
                        entry_time=next_bar.name,
                        side=sig.side,
                        entry=next_bar["open"],
                        sl=sig.sl,
                        tp=sig.tp,
                        volume=volume,
                        risk_usd=actual_risk,
                    )

    return result


def _signal_from_row(f: pd.DataFrame, i: int):
    """Inline signal logic matching the live strategy. Branches on STRATEGY_VARIANT."""
    variant = (settings.strategy_variant or "rsi_cross").lower()
    if variant == "swing_breakout":
        return _signal_swing_breakout(f, i)
    return _signal_rsi_cross(f, i)


def _signal_rsi_cross(f: pd.DataFrame, i: int):
    """RSI cross through 50 inside a confirmed trend (default)."""
    from strategy import Signal
    last, prev = f.iloc[i], f.iloc[i - 1]
    s = settings
    if last["adx"] < s.adx_threshold:
        return None
    if s.atr_regime_percentile > 0:
        lookback = s.atr_regime_lookback
        start = max(0, i + 1 - lookback)
        atr_floor = f["atr"].iloc[start : i + 1].quantile(s.atr_regime_percentile / 100.0)
        if not pd.isna(atr_floor) and float(last["atr"]) < atr_floor:
            return None
    htf_bias = "buy" if last["htf_close"] > last["htf_ema"] else "sell"
    close = last["close"]
    atr_val = float(last["atr"])
    bull = htf_bias == "buy"  and close > last["ema_trend"] and last["ema_fast"] > last["ema_slow"]
    bear = htf_bias == "sell" and close < last["ema_trend"] and last["ema_fast"] < last["ema_slow"]
    crossed_up = prev["rsi"] < 50 <= last["rsi"]
    crossed_dn = prev["rsi"] > 50 >= last["rsi"]
    if bull and crossed_up:
        return Signal("buy",  close, close - s.sl_atr_mult * atr_val, close + s.tp_atr_mult * atr_val, atr_val, "")
    if bear and crossed_dn:
        return Signal("sell", close, close + s.sl_atr_mult * atr_val, close - s.tp_atr_mult * atr_val, atr_val, "")
    return None


def _signal_swing_breakout(f: pd.DataFrame, i: int):
    """Pullback (RSI dipped <50 within last N bars) + price breakout above last N-bar swing high."""
    from strategy import Signal
    s = settings
    n = max(s.swing_lookback, 2)
    if i < n + 1:
        return None
    last = f.iloc[i]
    if last["adx"] < s.adx_threshold:
        return None
    if s.atr_regime_percentile > 0:
        lookback = s.atr_regime_lookback
        start = max(0, i + 1 - lookback)
        atr_floor = f["atr"].iloc[start : i + 1].quantile(s.atr_regime_percentile / 100.0)
        if not pd.isna(atr_floor) and float(last["atr"]) < atr_floor:
            return None
    htf_bias = "buy" if last["htf_close"] > last["htf_ema"] else "sell"
    close = last["close"]
    atr_val = float(last["atr"])
    bull = htf_bias == "buy"  and close > last["ema_trend"] and last["ema_fast"] > last["ema_slow"]
    bear = htf_bias == "sell" and close < last["ema_trend"] and last["ema_fast"] < last["ema_slow"]

    # Window = N bars BEFORE current bar
    window = f.iloc[i - n : i]

    if bull:
        rsi_dipped = bool((window["rsi"] < 50).any())
        swing_high = float(window["high"].max())
        if rsi_dipped and close > swing_high:
            return Signal("buy",  close,
                          close - s.sl_atr_mult * atr_val,
                          close + s.tp_atr_mult * atr_val,
                          atr_val, "")
    if bear:
        rsi_rallied = bool((window["rsi"] > 50).any())
        swing_low = float(window["low"].min())
        if rsi_rallied and close < swing_low:
            return Signal("sell", close,
                          close + s.sl_atr_mult * atr_val,
                          close - s.tp_atr_mult * atr_val,
                          atr_val, "")
    return None


# ── Per-symbol helper ─────────────────────────────────────────────────────── #

def _backtest_symbol(symbol: str, bars: int, equity: float, connector, cache=None) -> Optional[dict]:
    """Run backtest for one symbol. Returns summary dict or None on error."""
    inst = _get_instrument(connector, symbol)
    if inst is None:
        return {"symbol": symbol, "_error": "no symbol_info / zero tick"}

    spread_ticks = _resolve_spread_ticks(inst, settings.backtest_spread_pips)

    try:
        ltf = load_mt5(symbol, settings.timeframe, bars, connector, cache=cache)
        htf = load_mt5(symbol, settings.higher_timeframe, max(2000, bars // 4), connector, cache=cache)
    except Exception as exc:
        return {"symbol": symbol, "_error": str(exc)}

    if len(ltf) < settings.backtest_min_bars:
        return {"symbol": symbol, "_error": f"only {len(ltf)} bars (need {settings.backtest_min_bars})"}

    with apply_symbol_params(symbol):
        result = run_backtest(ltf, htf, inst=inst, starting_equity=equity, spread_ticks=spread_ticks)
    s = result.summary(equity)
    s["symbol"] = symbol
    s["category"] = _categorise(inst.path)
    s["tick_size"] = inst.tick_size
    s["spread_ticks"] = round(spread_ticks, 2)
    return s


def _categorise(path: str) -> str:
    """Best-effort category label from the MT5 symbol path."""
    if not path:
        return ""
    head = path.split("\\")[0].split("/")[0]
    return head[:24]


# ── Incremental CSV writer ────────────────────────────────────────────────── #

_CSV_COLS = [
    "symbol", "category", "trades", "win_rate", "profit_factor",
    "expectancy_r", "total_r", "total_pnl", "ending_equity",
    "max_drawdown_pct", "avg_win_r", "avg_loss_r",
    "tick_size", "spread_ticks", "backtested_at",
]


class CsvWriter:
    """Writes one symbol at a time; each call flushes to disk immediately.

    Existing rows for symbols NOT in the current run are preserved.
    Rows for symbols that are re-tested get replaced with fresh data.
    The file is always sorted by profit_factor desc after every write.
    """

    def __init__(self, path: Path, stamp: str) -> None:
        self.path = path
        self.stamp = stamp          # "YYYY-MM-DD HH:MM UTC" for every row this run
        self._df = self._load()

    def _load(self) -> pd.DataFrame:
        if self.path.exists():
            try:
                df = pd.read_csv(self.path)
                for col in _CSV_COLS:
                    if col not in df.columns:
                        df[col] = ""
                return df[_CSV_COLS].copy()
            except Exception as exc:
                log.warning("Could not read existing CSV (%s) — starting fresh", exc)
        return pd.DataFrame(columns=_CSV_COLS)

    def write(self, row: dict) -> None:
        """Merge one symbol's result and immediately flush to disk."""
        row = dict(row)
        row["backtested_at"] = self.stamp
        # Replace inf with 999 so the CSV stays numeric and sortable on re-load
        if row.get("profit_factor") == float("inf"):
            row["profit_factor"] = 999.0
        for col in _CSV_COLS:
            row.setdefault(col, "")
        df_row = pd.DataFrame([{c: row.get(c, "") for c in _CSV_COLS}])

        # Drop old row for this symbol (if it exists) then append the fresh one
        self._df = self._df[self._df["symbol"] != row["symbol"]]
        self._df = pd.concat([self._df, df_row], ignore_index=True)

        # Sort: most trades first, then profit_factor, then expectancy
        self._df["_pf_sort"] = pd.to_numeric(self._df["profit_factor"], errors="coerce").fillna(0)
        self._df.sort_values(
            by=["trades", "_pf_sort", "expectancy_r"],
            ascending=[False, False, False],
            inplace=True,
            kind="mergesort",
        )
        self._df.drop(columns=["_pf_sort"], inplace=True)
        self._df.to_csv(self.path, index=False)

    @property
    def rows(self) -> List[dict]:
        """Current in-memory results (all symbols, not just this run)."""
        return self._df.to_dict("records")


# ── Entry point ───────────────────────────────────────────────────────────── #

def _resolve_symbol_list(connector, single: Optional[str]) -> List[str]:
    if single:
        return [single.upper()]
    if settings.backtest_symbols:
        return [s.upper() for s in settings.backtest_symbols]
    if settings.backtest_all_symbols:
        syms = discover_symbols(
            connector,
            all_visible=True,
            name_filter=settings.backtest_symbol_filter,
            group_prefixes=settings.backtest_symbol_groups,
        )
        log.info("Discovered %d symbols from MT5", len(syms))
        return syms
    if settings.backtest_scan_all:
        return list(_DEFAULT_SCAN_SYMBOLS)
    return list(settings.symbols)


def main() -> None:
    p = argparse.ArgumentParser(description="Backtest TrendPullback across MT5 instruments")
    p.add_argument("--symbol", help="Test a single symbol only (skips multi-symbol scan)")
    p.add_argument("--csv-ltf", help="CSV file for LTF data instead of MT5")
    p.add_argument("--csv-htf", help="CSV file for HTF data instead of MT5")
    args = p.parse_args()

    equity      = settings.backtest_equity
    bars        = settings.backtest_bars
    output_csv  = settings.backtest_output_csv
    stamp       = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    out_path    = Path(output_csv)

    # CSV mode (single symbol from files) — limited; falls back to forex assumptions
    if args.csv_ltf and args.csv_htf:
        ltf = load_csv(args.csv_ltf)
        htf = load_csv(args.csv_htf)
        fake_inst = InstrumentInfo(
            name=args.symbol or "CSV",
            tick_size=0.00001, tick_value=1.0, digits=5,
            spread_ticks=10.0, volume_min=0.01, volume_max=100.0,
            volume_step=0.01, path="",
        )
        result = run_backtest(ltf, htf, inst=fake_inst,
                              starting_equity=equity, spread_ticks=10.0)
        summary = result.summary(equity)
        print("\n=== Backtest Summary ===")
        for k, v in summary.items():
            print(f"{k:>18}: {v}")
        return

    # Connect to MT5 once, reuse for every symbol
    from mt5_connector import MT5Connector
    connector = MT5Connector()
    if not connector.connect():
        log.error("Failed to connect to MT5 — check .env credentials")
        sys.exit(1)

    # Local M1 data cache — skips re-downloading bars across repeated runs
    cache = None
    if settings.use_data_cache:
        from data_cache import DataCache
        cache = DataCache(connector=connector)

    try:
        symbols = _resolve_symbol_list(connector, args.symbol)
    except Exception as exc:
        log.error("Symbol discovery failed: %s", exc)
        connector.shutdown()
        sys.exit(1)

    if not symbols:
        print("No symbols to backtest. Check BACKTEST_SYMBOLS / BACKTEST_ALL_SYMBOLS in .env")
        connector.shutdown()
        return

    print(f"\nBacktest run: {stamp}")
    print(f"  Symbols : {len(symbols)}")
    print(f"  Equity  : {equity:,.0f}")
    print(f"  Bars    : {bars}  ({settings.timeframe} + {settings.higher_timeframe})")
    print(f"  Spread  : {'broker live' if settings.backtest_spread_pips <= 0 else f'{settings.backtest_spread_pips} pips'}")
    print(f"  Cache   : {'on (' + settings.cache_dir + ')' if cache is not None else 'off'}")
    print(f"  Output  : {out_path.resolve()}")
    print(f"  (results written to CSV after EACH symbol — safe to Ctrl+C)\n")

    # CsvWriter loads the existing file once, then flushes after every symbol
    writer = CsvWriter(out_path, stamp)

    completed = 0
    skipped: List[str] = []
    col_w = max((len(s) for s in symbols), default=8)

    try:
        for idx, sym in enumerate(symbols, 1):
            print(f"[{idx:>4}/{len(symbols)}] {sym:<{col_w}}", end="  ", flush=True)
            try:
                row = _backtest_symbol(sym, bars, equity, connector, cache=cache)
            except Exception as exc:
                row = {"symbol": sym, "_error": f"crashed: {exc}"}

            if "_error" in row:
                print(f"SKIP — {row['_error']}")
                skipped.append(f"{sym}: {row['_error']}")
                continue

            # Write this symbol's result to CSV immediately
            writer.write(row)
            completed += 1

            pf = row["profit_factor"]
            pf_s = "  inf" if pf == float("inf") else f"{pf:>5.2f}"
            print(
                f"{row['trades']:>4} trades  "
                f"WR={row['win_rate']:>5.1f}%  "
                f"PF={pf_s}  "
                f"E={row['expectancy_r']:>+5.2f}R  "
                f"DD={row['max_drawdown_pct']:>5.1f}%  "
                f"[{row['category']}]  -> saved"
            )
    except KeyboardInterrupt:
        print(f"\n\nInterrupted after {completed} symbols — partial results already in CSV.")
    finally:
        connector.shutdown()

    if completed == 0:
        print(f"\nNo results. {len(skipped)} skipped.")
        if skipped:
            for s in skipped[:5]:
                print(f"  {s}")
        return

    print(f"\n{completed} symbol(s) written to {out_path.resolve()}")
    if skipped:
        print(f"{len(skipped)} skipped (no data / unsupported)")

    # Print top-10 from everything in the file (includes past runs for other symbols)
    all_rows = writer.rows
    qualified = [
        r for r in all_rows
        if isinstance(r.get("trades"), (int, float)) and int(r["trades"]) >= 5
    ]
    qualified.sort(
        key=lambda r: float(r.get("profit_factor", 0) or 0),
        reverse=True,
    )

    print(f"\n{'='*78}")
    print(f"  TOP SYMBOLS BY PROFIT FACTOR  ({completed} new + prior results, min 5 trades)")
    print(f"{'='*78}")
    if not qualified:
        print("  (no symbol produced >= 5 trades — try larger BACKTEST_BARS)")
        return

    print(f"  {'symbol':<12} {'cat':<14} {'trades':>7} {'WR%':>6} {'PF':>6} {'E(R)':>6} {'DD%':>6} {'backtested_at'}")
    for r in qualified[:10]:
        pf = float(r.get("profit_factor", 0) or 0)
        pf_s = "  999" if pf >= 999 else f"{pf:>6.2f}"
        bt = str(r.get("backtested_at", ""))[:16]
        print(f"  {str(r['symbol']):<12} {str(r.get('category',''))[:14]:<14} "
              f"{int(r['trades']):>7} {float(r.get('win_rate',0)):>6.1f} {pf_s} "
              f"{float(r.get('expectancy_r',0)):>+6.2f} {float(r.get('max_drawdown_pct',0)):>6.1f}"
              f"  {bt}")

    best = qualified[0]
    print(f"\n  Best: {best['symbol']}  "
          f"PF={best['profit_factor']}  "
          f"WR={best['win_rate']}%  "
          f"E={best['expectancy_r']}R  "
          f"backtested {best['backtested_at']}")


if __name__ == "__main__":
    main()
