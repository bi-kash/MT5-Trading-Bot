"""Risk management: position sizing, daily loss guard, drawdown guard, cooldown."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from config import settings
from logger import get_logger

log = get_logger("risk")


@dataclass
class TradeOutcome:
    closed_at: datetime
    symbol: str
    profit: float


@dataclass
class RiskState:
    session_start_equity: float = 0.0
    peak_equity: float = 0.0
    day_start_equity: float = 0.0
    day_date: Optional[str] = None
    recent_trades: List[TradeOutcome] = field(default_factory=list)
    halted: bool = False
    halt_reason: str = ""
    trades_today: int = 0
    cooldown_until: Optional[datetime] = None


class RiskManager:
    def __init__(self) -> None:
        self.s = settings
        self.state = RiskState()

    # ------------------------------------------------------------ #
    # Lifecycle                                                    #
    # ------------------------------------------------------------ #
    def init_from_account(self, equity: float) -> None:
        self.state.session_start_equity = equity
        self.state.peak_equity = equity
        self._roll_day_if_needed(equity)
        log.info("Risk initialized. Equity=%.2f", equity)

    def _today(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _roll_day_if_needed(self, equity: float) -> None:
        today = self._today()
        if self.state.day_date != today:
            self.state.day_date = today
            self.state.day_start_equity = equity
            self.state.trades_today = 0
            if self.state.halt_reason.startswith("Daily"):
                self.state.halted = False
                self.state.halt_reason = ""
            log.info("New trading day %s, day_start_equity=%.2f", today, equity)

    # ------------------------------------------------------------ #
    # Guards                                                       #
    # ------------------------------------------------------------ #
    def update_equity(self, equity: float) -> None:
        self._roll_day_if_needed(equity)
        self.state.peak_equity = max(self.state.peak_equity, equity)

        # Daily loss
        if self.state.day_start_equity > 0:
            day_loss_pct = (self.state.day_start_equity - equity) / self.state.day_start_equity * 100
            if day_loss_pct >= self.s.max_daily_loss_pct:
                self._halt(f"Daily loss {day_loss_pct:.2f}% >= {self.s.max_daily_loss_pct}%")

        # Drawdown from peak
        if self.state.peak_equity > 0:
            dd_pct = (self.state.peak_equity - equity) / self.state.peak_equity * 100
            if dd_pct >= self.s.max_drawdown_pct:
                self._halt(f"Drawdown {dd_pct:.2f}% >= {self.s.max_drawdown_pct}%")

    def _halt(self, reason: str) -> None:
        if not self.state.halted:
            self.state.halted = True
            self.state.halt_reason = reason
            log.error("TRADING HALTED: %s", reason)

    def can_open_new_trade(
        self,
        symbol: str,
        open_positions_total: int,
        open_positions_symbol: int,
    ) -> tuple[bool, str]:
        if self.state.halted:
            return False, f"halted: {self.state.halt_reason}"
        if self.state.trades_today >= self.s.max_trades_per_day:
            return False, "max trades per day reached"
        if open_positions_total >= self.s.max_open_positions:
            return False, "max open positions reached"
        if open_positions_symbol >= self.s.max_positions_per_symbol:
            return False, f"max positions for {symbol} reached"
        if self.state.cooldown_until and datetime.now(timezone.utc) < self.state.cooldown_until:
            return False, f"cooldown until {self.state.cooldown_until.isoformat()}"
        if not _within_trading_hours():
            return False, "outside trading hours"
        return True, "ok"

    def register_trade_opened(self) -> None:
        self.state.trades_today += 1

    def register_trade_closed(self, symbol: str, profit: float) -> None:
        self.state.recent_trades.append(
            TradeOutcome(datetime.now(timezone.utc), symbol, profit)
        )
        # keep last 50
        self.state.recent_trades = self.state.recent_trades[-50:]
        self._check_cooldown()

    def _check_cooldown(self) -> None:
        tail = self.state.recent_trades[-self.s.cooldown_losses:]
        if len(tail) < self.s.cooldown_losses:
            return
        if all(t.profit < 0 for t in tail):
            from datetime import timedelta
            self.state.cooldown_until = datetime.now(timezone.utc) + timedelta(minutes=self.s.cooldown_minutes)
            log.warning(
                "Cooldown engaged: %d consecutive losses. Pausing until %s",
                self.s.cooldown_losses, self.state.cooldown_until.isoformat()
            )

    # ------------------------------------------------------------ #
    # Position sizing                                              #
    # ------------------------------------------------------------ #
    def compute_lot_size(
        self,
        symbol_info,
        entry_price: float,
        sl_price: float,
        account_equity: float,
        account_currency: str,
    ) -> float:
        """Risk-based sizing: lot = (equity * risk%) / (sl_distance * tick_value_per_lot)."""
        sl_dist = abs(entry_price - sl_price)
        if sl_dist <= 0:
            return 0.0
        risk_amount = account_equity * (self.s.risk_per_trade / 100.0)

        tick_size = symbol_info.trade_tick_size or symbol_info.point
        tick_value = symbol_info.trade_tick_value  # value of one tick per 1 lot, in account currency
        if tick_size == 0 or tick_value == 0:
            log.warning("Symbol %s has zero tick size/value; cannot size", symbol_info.name)
            return 0.0

        loss_per_lot = (sl_dist / tick_size) * tick_value
        if loss_per_lot <= 0:
            return 0.0

        raw_lot = risk_amount / loss_per_lot

        step = symbol_info.volume_step or self.s.lot_step
        min_lot = max(symbol_info.volume_min or self.s.min_lot, self.s.min_lot)
        max_lot = min(symbol_info.volume_max or self.s.max_lot, self.s.max_lot)

        # round down to nearest step
        lot = (int(raw_lot / step)) * step
        lot = max(min_lot, min(lot, max_lot))
        return round(lot, 2)


def _within_trading_hours() -> bool:
    now = datetime.now(timezone.utc)
    if not settings.trade_on_weekend and now.weekday() >= 5:
        return False
    return settings.trading_hours_start <= now.hour < settings.trading_hours_end
