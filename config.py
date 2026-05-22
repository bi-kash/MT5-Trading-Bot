"""Configuration loader. All settings come from environment / .env file."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv

load_dotenv()


def _get(key: str, default: Optional[str] = None) -> Optional[str]:
    val = os.getenv(key, default)
    if val is None:
        return None
    val = val.strip()
    return val if val != "" else default


def _get_int(key: str, default: int) -> int:
    v = _get(key)
    return int(v) if v is not None else default


def _get_float(key: str, default: float) -> float:
    v = _get(key)
    return float(v) if v is not None else default


def _get_bool(key: str, default: bool) -> bool:
    v = _get(key)
    if v is None:
        return default
    return v.lower() in ("1", "true", "yes", "on", "y")


def _get_list(key: str, default: List[str]) -> List[str]:
    v = _get(key)
    if v is None:
        return default
    return [x.strip() for x in v.split(",") if x.strip()]


@dataclass
class Settings:
    # MT5
    mt5_login: int = _get_int("MT5_LOGIN", 0)
    mt5_password: str = _get("MT5_PASSWORD", "") or ""
    mt5_server: str = _get("MT5_SERVER", "") or ""
    mt5_path: Optional[str] = _get("MT5_PATH")
    mt5_timeout: int = _get_int("MT5_TIMEOUT", 60000)
    mt5_portable: bool = _get_bool("MT5_PORTABLE", False)

    # Trading
    symbols: List[str] = field(default_factory=lambda: _get_list("SYMBOLS", ["EURUSD"]))
    timeframe: str = _get("TIMEFRAME", "M15") or "M15"
    higher_timeframe: str = _get("HIGHER_TIMEFRAME", "H4") or "H4"

    # Strategy
    ema_fast: int = _get_int("EMA_FAST", 20)
    ema_slow: int = _get_int("EMA_SLOW", 50)
    ema_trend: int = _get_int("EMA_TREND", 200)
    atr_period: int = _get_int("ATR_PERIOD", 14)
    adx_period: int = _get_int("ADX_PERIOD", 14)
    adx_threshold: float = _get_float("ADX_THRESHOLD", 22.0)
    atr_regime_percentile: int = _get_int("ATR_REGIME_PERCENTILE", 20)
    atr_regime_lookback: int = _get_int("ATR_REGIME_LOOKBACK", 50)
    rsi_period: int = _get_int("RSI_PERIOD", 14)
    rsi_overbought: float = _get_float("RSI_OVERBOUGHT", 70.0)
    rsi_oversold: float = _get_float("RSI_OVERSOLD", 30.0)
    bb_period: int = _get_int("BB_PERIOD", 20)
    bb_stddev: float = _get_float("BB_STDDEV", 2.0)

    # Risk
    risk_per_trade: float = _get_float("RISK_PER_TRADE", 0.5)
    max_daily_loss_pct: float = _get_float("MAX_DAILY_LOSS_PCT", 3.0)
    max_drawdown_pct: float = _get_float("MAX_DRAWDOWN_PCT", 10.0)
    max_open_positions: int = _get_int("MAX_OPEN_POSITIONS", 3)
    max_positions_per_symbol: int = _get_int("MAX_POSITIONS_PER_SYMBOL", 1)
    max_trades_per_day: int = _get_int("MAX_TRADES_PER_DAY", 10)

    sl_atr_mult: float = _get_float("SL_ATR_MULT", 1.5)
    tp_atr_mult: float = _get_float("TP_ATR_MULT", 2.5)
    trailing_atr_mult: float = _get_float("TRAILING_ATR_MULT", 1.0)
    use_trailing_stop: bool = _get_bool("USE_TRAILING_STOP", True)
    breakeven_at_r: float = _get_float("BREAKEVEN_AT_R", 1.0)

    max_spread_points: int = _get_int("MAX_SPREAD_POINTS", 25)
    slippage_points: int = _get_int("SLIPPAGE_POINTS", 10)
    magic_number: int = _get_int("MAGIC_NUMBER", 20260519)
    order_comment: str = _get("ORDER_COMMENT", "PyBot_v1") or "PyBot_v1"

    cooldown_losses: int = _get_int("COOLDOWN_LOSSES", 2)
    cooldown_minutes: int = _get_int("COOLDOWN_MINUTES", 60)

    min_lot: float = _get_float("MIN_LOT", 0.01)
    max_lot: float = _get_float("MAX_LOT", 5.0)
    lot_step: float = _get_float("LOT_STEP", 0.01)

    # Bot
    poll_interval_seconds: int = _get_int("POLL_INTERVAL_SECONDS", 5)
    trading_hours_start: int = _get_int("TRADING_HOURS_START", 7)
    trading_hours_end: int = _get_int("TRADING_HOURS_END", 20)
    trade_on_weekend: bool = _get_bool("TRADE_ON_WEEKEND", False)
    dry_run: bool = _get_bool("DRY_RUN", True)

    # Logging
    log_level: str = _get("LOG_LEVEL", "INFO") or "INFO"
    log_dir: str = _get("LOG_DIR", "logs") or "logs"
    journal_csv: str = _get("JOURNAL_CSV", "logs/trade_journal.csv") or "logs/trade_journal.csv"

    # Notifications
    telegram_bot_token: Optional[str] = _get("TELEGRAM_BOT_TOKEN")
    telegram_chat_id: Optional[str] = _get("TELEGRAM_CHAT_ID")
    discord_webhook_url: Optional[str] = _get("DISCORD_WEBHOOK_URL")
    notify_on_open: bool = _get_bool("NOTIFY_ON_OPEN", True)
    notify_on_close: bool = _get_bool("NOTIFY_ON_CLOSE", True)
    notify_on_error: bool = _get_bool("NOTIFY_ON_ERROR", True)
    daily_summary_hour: int = _get_int("DAILY_SUMMARY_HOUR", 22)

    # ML
    use_ml_filter: bool = _get_bool("USE_ML_FILTER", False)
    ml_model_path: str = _get("ML_MODEL_PATH", "models/filter.pkl") or "models/filter.pkl"
    ml_min_probability: float = _get_float("ML_MIN_PROBABILITY", 0.55)

    # Backtesting
    backtest_equity: float = _get_float("BACKTEST_EQUITY", 10_000.0)
    backtest_bars: int = _get_int("BACKTEST_BARS", 20_000)
    backtest_spread_pips: float = _get_float("BACKTEST_SPREAD_PIPS", 0.0)
    backtest_output_csv: str = _get("BACKTEST_OUTPUT_CSV", "backtest_results.csv") or "backtest_results.csv"
    backtest_symbols: List[str] = field(default_factory=lambda: _get_list("BACKTEST_SYMBOLS", []))
    backtest_scan_all: bool = _get_bool("BACKTEST_SCAN_ALL", True)
    backtest_all_symbols: bool = _get_bool("BACKTEST_ALL_SYMBOLS", False)
    backtest_symbol_filter: str = _get("BACKTEST_SYMBOL_FILTER", "") or ""
    backtest_symbol_groups: List[str] = field(default_factory=lambda: _get_list("BACKTEST_SYMBOL_GROUPS", []))
    backtest_min_bars: int = _get_int("BACKTEST_MIN_BARS", 500)
    # Quality pre-filters applied before fetching any bar data
    backtest_max_spread_pct: float = _get_float("BACKTEST_MAX_SPREAD_PCT", 0.1)
    backtest_max_spread_ticks: int = _get_int("BACKTEST_MAX_SPREAD_TICKS", 100)
    backtest_instrument_types: List[str] = field(
        default_factory=lambda: _get_list("BACKTEST_INSTRUMENT_TYPES", ["forex", "indices", "metals", "energy"])
    )
    backtest_exclude_filter: str = _get("BACKTEST_EXCLUDE_FILTER", "") or ""

    # Walk-forward backtest
    walkforward_periods: int = _get_int("WALKFORWARD_PERIODS", 10)
    walkforward_bars_per_period: int = _get_int("WALKFORWARD_BARS_PER_PERIOD", 10_000)
    walkforward_output_dir: str = _get("WALKFORWARD_OUTPUT_DIR", "backtest_results") or "backtest_results"
    walkforward_summary_csv: str = _get(
        "WALKFORWARD_SUMMARY_CSV", "backtest_walkforward_summary.csv"
    ) or "backtest_walkforward_summary.csv"

    def ensure_dirs(self) -> None:
        Path(self.log_dir).mkdir(parents=True, exist_ok=True)
        Path(self.journal_csv).parent.mkdir(parents=True, exist_ok=True)


settings = Settings()
settings.ensure_dirs()
