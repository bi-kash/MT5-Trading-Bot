"""MetaTrader 5 connection + order routing wrapper.

Encapsulates all MT5 SDK calls so the rest of the bot can stay broker-agnostic.
Reconnects automatically if the terminal drops the connection.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional

import MetaTrader5 as mt5
import pandas as pd

from config import settings
from logger import get_logger

log = get_logger("mt5")

TIMEFRAME_MAP: Dict[str, int] = {
    "M1": mt5.TIMEFRAME_M1,
    "M5": mt5.TIMEFRAME_M5,
    "M15": mt5.TIMEFRAME_M15,
    "M30": mt5.TIMEFRAME_M30,
    "H1": mt5.TIMEFRAME_H1,
    "H4": mt5.TIMEFRAME_H4,
    "D1": mt5.TIMEFRAME_D1,
    "W1": mt5.TIMEFRAME_W1,
}


@dataclass
class OrderResult:
    ok: bool
    ticket: Optional[int]
    price: Optional[float]
    retcode: Optional[int]
    comment: str


class MT5Connector:
    """Thin wrapper around the MetaTrader5 Python SDK."""

    def __init__(self) -> None:
        self._connected = False

    # ------------------------------------------------------------------ #
    # Connection                                                         #
    # ------------------------------------------------------------------ #
    def connect(self) -> bool:
        kwargs = {"timeout": settings.mt5_timeout, "portable": settings.mt5_portable}
        if settings.mt5_path:
            kwargs["path"] = settings.mt5_path

        if not mt5.initialize(**kwargs):
            log.error("mt5.initialize failed: %s", mt5.last_error())
            return False

        if settings.mt5_login:
            authorized = mt5.login(
                login=settings.mt5_login,
                password=settings.mt5_password,
                server=settings.mt5_server,
                timeout=settings.mt5_timeout,
            )
            if not authorized:
                log.error("mt5.login failed: %s", mt5.last_error())
                mt5.shutdown()
                return False

        info = mt5.account_info()
        if info is None:
            log.error("Could not retrieve account info")
            return False

        log.info(
            "Connected: login=%s broker=%s server=%s balance=%.2f currency=%s leverage=1:%s",
            info.login, info.company, info.server, info.balance, info.currency, info.leverage,
        )
        self._connected = True
        return True

    def shutdown(self) -> None:
        if self._connected:
            mt5.shutdown()
            self._connected = False
            log.info("MT5 connection closed")

    def ensure_connected(self) -> bool:
        if mt5.terminal_info() is None:
            log.warning("Terminal disconnected, attempting reconnect...")
            self.shutdown()
            for attempt in range(5):
                if self.connect():
                    return True
                time.sleep(2 ** attempt)
            return False
        return True

    # ------------------------------------------------------------------ #
    # Market data                                                        #
    # ------------------------------------------------------------------ #
    def get_rates(self, symbol: str, timeframe: str, n_bars: int = 500) -> Optional[pd.DataFrame]:
        tf = TIMEFRAME_MAP.get(timeframe.upper())
        if tf is None:
            log.error("Unknown timeframe %s", timeframe)
            return None

        # MT5 caps a single copy_rates_from_pos call at ~100K bars
        # ("Invalid params" above that). Chunk large requests transparently.
        import numpy as np
        CHUNK = 50_000
        chunks = []
        pos = 0
        remaining = n_bars
        while remaining > 0:
            take = min(CHUNK, remaining)
            r = mt5.copy_rates_from_pos(symbol, tf, pos, take)
            if r is None or len(r) == 0:
                if pos == 0:
                    log.warning("No rates for %s %s: %s", symbol, timeframe, mt5.last_error())
                    return None
                break   # broker ran out of older history — keep what we got
            chunks.append(r)
            pos += len(r)
            remaining -= len(r)
            if len(r) < take:
                break   # broker returned fewer bars than asked — no more history

        if not chunks:
            return None

        # Each chunk is chronological (oldest→newest) and chunk N is older than
        # chunk N-1, so reverse the list to get one chronological array overall.
        merged = np.concatenate(chunks[::-1])
        df = pd.DataFrame(merged)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df.set_index("time", inplace=True)
        df = df[~df.index.duplicated(keep="last")]   # belt-and-braces against overlap
        df.sort_index(inplace=True)
        return df

    def symbol_info(self, symbol: str):
        info = mt5.symbol_info(symbol)
        if info is None:
            return None
        if not info.visible:
            mt5.symbol_select(symbol, True)
            info = mt5.symbol_info(symbol)
        return info

    def tick(self, symbol: str):
        return mt5.symbol_info_tick(symbol)

    def account(self):
        return mt5.account_info()

    def positions(self, symbol: Optional[str] = None) -> List:
        if symbol:
            res = mt5.positions_get(symbol=symbol)
        else:
            res = mt5.positions_get()
        return list(res) if res else []

    def positions_for_magic(self) -> List:
        return [p for p in self.positions() if p.magic == settings.magic_number]

    def history_deals(self, since: datetime, until: datetime) -> List:
        deals = mt5.history_deals_get(since, until)
        return list(deals) if deals else []

    # ------------------------------------------------------------------ #
    # Order execution                                                    #
    # ------------------------------------------------------------------ #
    def _normalize_price(self, symbol_info, price: float) -> float:
        return round(float(price), symbol_info.digits)

    def send_market_order(
        self,
        symbol: str,
        side: str,                       # "buy" or "sell"
        volume: float,
        sl: float,
        tp: float,
        comment: Optional[str] = None,
    ) -> OrderResult:
        info = self.symbol_info(symbol)
        if info is None:
            return OrderResult(False, None, None, None, f"Symbol {symbol} not found")

        tick = self.tick(symbol)
        if tick is None:
            return OrderResult(False, None, None, None, "No tick data")

        if side == "buy":
            price = tick.ask
            order_type = mt5.ORDER_TYPE_BUY
        else:
            price = tick.bid
            order_type = mt5.ORDER_TYPE_SELL

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": float(volume),
            "type": order_type,
            "price": price,
            "sl": self._normalize_price(info, sl),
            "tp": self._normalize_price(info, tp),
            "deviation": settings.slippage_points,
            "magic": settings.magic_number,
            "comment": (comment or settings.order_comment)[:31],
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self._pick_filling_mode(info),
        }

        if settings.dry_run:
            log.info("[DRY RUN] would send %s %s %.2f @ %.5f sl=%.5f tp=%.5f",
                     side, symbol, volume, price, sl, tp)
            return OrderResult(True, None, price, None, "dry-run")

        result = mt5.order_send(request)
        if result is None:
            return OrderResult(False, None, None, None, f"order_send returned None: {mt5.last_error()}")

        if result.retcode != mt5.TRADE_RETCODE_DONE:
            return OrderResult(False, None, result.price, result.retcode, result.comment)

        return OrderResult(True, result.order, result.price, result.retcode, result.comment)

    def modify_position_sl_tp(self, ticket: int, sl: float, tp: float) -> bool:
        pos = next((p for p in self.positions() if p.ticket == ticket), None)
        if pos is None:
            return False
        info = self.symbol_info(pos.symbol)
        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "position": ticket,
            "symbol": pos.symbol,
            "sl": self._normalize_price(info, sl),
            "tp": self._normalize_price(info, tp),
        }
        if settings.dry_run:
            return True
        r = mt5.order_send(request)
        return r is not None and r.retcode == mt5.TRADE_RETCODE_DONE

    def close_position(self, ticket: int) -> bool:
        pos = next((p for p in self.positions() if p.ticket == ticket), None)
        if pos is None:
            return False
        info = self.symbol_info(pos.symbol)
        tick = self.tick(pos.symbol)
        if pos.type == mt5.POSITION_TYPE_BUY:
            price = tick.bid
            order_type = mt5.ORDER_TYPE_SELL
        else:
            price = tick.ask
            order_type = mt5.ORDER_TYPE_BUY
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "position": ticket,
            "symbol": pos.symbol,
            "volume": pos.volume,
            "type": order_type,
            "price": price,
            "deviation": settings.slippage_points,
            "magic": settings.magic_number,
            "comment": "close_" + settings.order_comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self._pick_filling_mode(info),
        }
        if settings.dry_run:
            return True
        r = mt5.order_send(request)
        return r is not None and r.retcode == mt5.TRADE_RETCODE_DONE

    @staticmethod
    def _pick_filling_mode(info) -> int:
        # Most brokers accept IOC; fall back through other modes if needed
        mode = info.filling_mode
        if mode & 1:  # FOK
            return mt5.ORDER_FILLING_FOK
        if mode & 2:  # IOC
            return mt5.ORDER_FILLING_IOC
        return mt5.ORDER_FILLING_RETURN
