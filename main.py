"""MT5 trading bot — main runtime loop.

Architecture
------------
  config.py        -> typed settings from .env
  mt5_connector.py -> all broker I/O
  strategy.py      -> pluggable signal generator (TrendPullback by default)
  risk_manager.py  -> sizing, daily-loss / drawdown / cooldown guards
  ml_filter.py     -> optional probabilistic edge filter
  notifier.py      -> Telegram + Discord alerts
  journal.py       -> append-only CSV trade log
  backtest.py      -> offline simulator on historical bars
"""
from __future__ import annotations

import signal as _signal
import sys
import time
from datetime import datetime, timezone
from typing import Dict, Optional

import MetaTrader5 as mt5

from config import settings
from journal import record as journal_record
from logger import get_logger
from ml_filter import MLFilter
from mt5_connector import MT5Connector
from notifier import notify, notify_error
from risk_manager import RiskManager
from strategy import Signal, TrendPullbackStrategy

log = get_logger("main")


class TradingBot:
    def __init__(self) -> None:
        self.mt5 = MT5Connector()
        self.strategy = TrendPullbackStrategy()
        self.risk = RiskManager()
        self.ml = MLFilter()
        self._running = True
        self._last_signal_bar: Dict[str, datetime] = {}
        self._known_tickets: set[int] = set()
        self._last_daily_summary_day: Optional[str] = None
        self._last_heartbeat: float = 0.0

    # ----------------------------------------------------------- #
    # Lifecycle                                                   #
    # ----------------------------------------------------------- #
    def start(self) -> None:
        if not self.mt5.connect():
            notify_error("MT5 connection failed at startup")
            sys.exit(1)

        acct = self.mt5.account()
        self.risk.init_from_account(acct.equity)
        for sym in settings.symbols:
            if self.mt5.symbol_info(sym) is None:
                log.error("Symbol %s not available, removing from list", sym)
        notify(
            f"<b>Bot started</b>\nStrategy: {self.strategy.name}\n"
            f"Symbols: {', '.join(settings.symbols)}\nTF: {settings.timeframe}/{settings.higher_timeframe}\n"
            f"Equity: {acct.equity:.2f} {acct.currency}\nDry run: {settings.dry_run}",
            level="info",
        )

        _signal.signal(_signal.SIGINT, self._stop)
        _signal.signal(_signal.SIGTERM, self._stop)

        try:
            self._run_loop()
        finally:
            self.mt5.shutdown()

    def _stop(self, *_args) -> None:
        log.info("Shutdown signal received")
        self._running = False

    # ----------------------------------------------------------- #
    # Main loop                                                   #
    # ----------------------------------------------------------- #
    def _run_loop(self) -> None:
        while self._running:
            try:
                if not self.mt5.ensure_connected():
                    notify_error("MT5 reconnect failed; sleeping 30s")
                    time.sleep(30)
                    continue

                acct = self.mt5.account()
                if acct is not None:
                    self.risk.update_equity(acct.equity)

                self._check_closed_positions()
                self._manage_open_positions()
                self._maybe_open_new_trades()
                self._maybe_send_daily_summary()
                self._maybe_log_heartbeat()
            except Exception as exc:                 # never crash the loop
                log.exception("Loop error: %s", exc)
                notify_error(f"Loop error: {exc}")

            time.sleep(settings.poll_interval_seconds)

    # ----------------------------------------------------------- #
    # Trade lifecycle                                             #
    # ----------------------------------------------------------- #
    def _maybe_log_heartbeat(self) -> None:
        now = time.time()
        if now - self._last_heartbeat < 300:   # every 5 minutes
            return
        self._last_heartbeat = now
        open_count = len(self.mt5.positions_for_magic())
        acct = self.mt5.account()
        equity = f"{acct.equity:.2f} {acct.currency}" if acct else "?"
        log.info(
            "Heartbeat — equity=%s open_positions=%d symbols=%s",
            equity, open_count, ",".join(settings.symbols),
        )

    def _maybe_open_new_trades(self) -> None:
        all_positions = self.mt5.positions_for_magic()
        for symbol in settings.symbols:
            self._evaluate_symbol(symbol, all_positions)

    def _evaluate_symbol(self, symbol: str, all_positions) -> None:
        sym_positions = [p for p in all_positions if p.symbol == symbol]
        ok, reason = self.risk.can_open_new_trade(
            symbol, len(all_positions), len(sym_positions)
        )
        if not ok:
            log.debug("[%s] skip: %s", symbol, reason)
            return

        # Spread filter
        info = self.mt5.symbol_info(symbol)
        tick = self.mt5.tick(symbol)
        if info is None or tick is None:
            return
        spread_points = (tick.ask - tick.bid) / info.point
        if spread_points > settings.max_spread_points:
            log.debug("[%s] spread %.1f > max %d", symbol, spread_points, settings.max_spread_points)
            return

        df_ltf = self.mt5.get_rates(symbol, settings.timeframe, n_bars=500)
        df_htf = self.mt5.get_rates(symbol, settings.higher_timeframe, n_bars=500)
        if df_ltf is None or df_htf is None:
            return

        # Don't fire twice on the same bar
        last_bar_time = df_ltf.index[-1]
        if self._last_signal_bar.get(symbol) == last_bar_time:
            return

        sig = self.strategy.generate_signal(df_ltf, df_htf)
        if sig is None:
            f = self.strategy._ltf_features(df_ltf)
            last = f.iloc[-1]
            log.debug(
                "[%s] no signal | ADX=%.1f(thr=%.0f) RSI=%.1f ATR=%.5f EMAok=%s",
                symbol,
                last.get("adx", float("nan")),
                settings.adx_threshold,
                last.get("rsi", float("nan")),
                last.get("atr", float("nan")),
                bool(last.get("close", 0) > last.get("ema_trend", 0)),
            )
            return

        # Optional ML filter
        feat_row = self._latest_feature_row(df_ltf)
        if not self.ml.passes(feat_row, sig.side):
            log.info("[%s] ML filter blocked %s signal", symbol, sig.side)
            self._last_signal_bar[symbol] = last_bar_time
            return

        self._open_trade(symbol, sig, last_bar_time)

    def _latest_feature_row(self, df_ltf):
        f = self.strategy._ltf_features(df_ltf)
        return f.iloc[-1]

    def _open_trade(self, symbol: str, sig: Signal, bar_time) -> None:
        info = self.mt5.symbol_info(symbol)
        acct = self.mt5.account()
        if info is None or acct is None:
            return

        volume = self.risk.compute_lot_size(
            info, sig.entry, sig.sl, acct.equity, acct.currency
        )
        if volume <= 0:
            log.warning("[%s] computed volume <= 0, skipping", symbol)
            return

        log.info(
            "[%s] SIGNAL %s vol=%.2f entry=%.5f sl=%.5f tp=%.5f | %s",
            symbol, sig.side.upper(), volume, sig.entry, sig.sl, sig.tp, sig.reason,
        )

        result = self.mt5.send_market_order(
            symbol=symbol, side=sig.side, volume=volume,
            sl=sig.sl, tp=sig.tp,
        )
        self._last_signal_bar[symbol] = bar_time

        if not result.ok:
            log.error("[%s] order failed: %s (retcode=%s)", symbol, result.comment, result.retcode)
            notify_error(f"Order failed {symbol}: {result.comment}")
            journal_record("ORDER_FAIL", symbol, sig.side, volume,
                           sig.entry, sig.sl, sig.tp, reason=sig.reason, comment=result.comment)
            return

        self.risk.register_trade_opened()
        journal_record("OPEN", symbol, sig.side, volume,
                       result.price or sig.entry, sig.sl, sig.tp,
                       ticket=result.ticket, reason=sig.reason)
        if settings.notify_on_open:
            notify(
                f"<b>{sig.side.upper()} {symbol}</b> {volume} lots\n"
                f"Entry: {result.price or sig.entry:.5f}\nSL: {sig.sl:.5f}\nTP: {sig.tp:.5f}\n"
                f"Reason: {sig.reason}",
                level="trade",
            )

    # ----------------------------------------------------------- #
    # Open-position management                                    #
    # ----------------------------------------------------------- #
    def _manage_open_positions(self) -> None:
        if not settings.use_trailing_stop and settings.breakeven_at_r <= 0:
            return
        for pos in self.mt5.positions_for_magic():
            self._known_tickets.add(pos.ticket)
            self._trail_position(pos)

    def _trail_position(self, pos) -> None:
        info = self.mt5.symbol_info(pos.symbol)
        tick = self.mt5.tick(pos.symbol)
        if info is None or tick is None:
            return

        df = self.mt5.get_rates(pos.symbol, settings.timeframe, n_bars=settings.atr_period + 30)
        if df is None or len(df) < settings.atr_period + 2:
            return
        from indicators import atr as _atr
        atr_val = float(_atr(df, settings.atr_period).iloc[-1])
        if atr_val <= 0:
            return

        is_buy = pos.type == mt5.POSITION_TYPE_BUY
        cur_price = tick.bid if is_buy else tick.ask
        entry = pos.price_open
        sl = pos.sl
        tp = pos.tp
        new_sl = sl

        # Initial risk distance (entry to original SL); fall back to ATR if SL is 0
        r_dist = abs(entry - sl) if sl else settings.sl_atr_mult * atr_val
        profit_dist = (cur_price - entry) if is_buy else (entry - cur_price)

        # 1) Breakeven move
        if settings.breakeven_at_r > 0 and profit_dist >= settings.breakeven_at_r * r_dist:
            be = entry
            if is_buy and (sl == 0 or be > sl):
                new_sl = be
            elif not is_buy and (sl == 0 or be < sl):
                new_sl = be

        # 2) ATR trailing stop, only tightens
        if settings.use_trailing_stop:
            trail = settings.trailing_atr_mult * atr_val
            candidate = cur_price - trail if is_buy else cur_price + trail
            if is_buy and candidate > new_sl:
                new_sl = candidate
            elif not is_buy and (new_sl == 0 or candidate < new_sl):
                new_sl = candidate

        if new_sl != sl and new_sl != 0:
            ok = self.mt5.modify_position_sl_tp(pos.ticket, new_sl, tp)
            if ok:
                log.info("[%s] trailing SL updated %.5f -> %.5f", pos.symbol, sl, new_sl)

    # ----------------------------------------------------------- #
    # Closed-position bookkeeping                                 #
    # ----------------------------------------------------------- #
    def _check_closed_positions(self) -> None:
        current = {p.ticket for p in self.mt5.positions_for_magic()}
        closed = self._known_tickets - current
        if not closed:
            self._known_tickets = current
            return

        # For closed tickets, look up deals in the recent history window
        from datetime import timedelta
        since = datetime.now(timezone.utc) - timedelta(days=2)
        until = datetime.now(timezone.utc) + timedelta(minutes=1)
        deals = self.mt5.history_deals(since, until)
        by_position: Dict[int, list] = {}
        for d in deals:
            by_position.setdefault(d.position_id, []).append(d)

        for ticket in closed:
            ds = by_position.get(ticket, [])
            if not ds:
                continue
            profit = sum(d.profit + d.commission + d.swap for d in ds)
            symbol = ds[-1].symbol
            exit_price = ds[-1].price
            self.risk.register_trade_closed(symbol, profit)
            journal_record("CLOSE", symbol, exit_price=exit_price, profit=profit, ticket=ticket)
            if settings.notify_on_close:
                emoji = "✅" if profit >= 0 else "🔻"
                notify(
                    f"{emoji} <b>Closed</b> {symbol} #{ticket}\n"
                    f"P/L: {profit:+.2f}",
                    level="close",
                )
        self._known_tickets = current

    # ----------------------------------------------------------- #
    # Daily summary                                               #
    # ----------------------------------------------------------- #
    def _maybe_send_daily_summary(self) -> None:
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")
        if now.hour != settings.daily_summary_hour:
            return
        if self._last_daily_summary_day == today:
            return
        acct = self.mt5.account()
        if acct is None:
            return
        day_start = self.risk.state.day_start_equity or acct.equity
        pnl = acct.equity - day_start
        trades = self.risk.state.trades_today
        notify(
            f"<b>Daily summary {today}</b>\n"
            f"Trades: {trades}\n"
            f"Day P/L: {pnl:+.2f} {acct.currency}\n"
            f"Equity: {acct.equity:.2f}\n"
            f"Halt: {self.risk.state.halt_reason or 'none'}",
            level="summary",
        )
        self._last_daily_summary_day = today


def main() -> None:
    TradingBot().start()


if __name__ == "__main__":
    main()
