"""CSV trade journal for post-hoc analysis."""
from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from config import settings
from logger import get_logger

log = get_logger("journal")

HEADERS = [
    "timestamp_utc", "event", "symbol", "side", "volume",
    "entry_price", "sl", "tp", "exit_price", "profit",
    "ticket", "reason", "comment",
]


def _ensure_header() -> None:
    p = Path(settings.journal_csv)
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(HEADERS)


def record(
    event: str,
    symbol: str,
    side: str = "",
    volume: float = 0.0,
    entry_price: float = 0.0,
    sl: float = 0.0,
    tp: float = 0.0,
    exit_price: float = 0.0,
    profit: float = 0.0,
    ticket: Optional[int] = None,
    reason: str = "",
    comment: str = "",
) -> None:
    try:
        _ensure_header()
        with open(settings.journal_csv, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow([
                datetime.now(timezone.utc).isoformat(), event, symbol, side, volume,
                entry_price, sl, tp, exit_price, profit,
                ticket if ticket is not None else "", reason, comment,
            ])
    except Exception as exc:
        log.warning("Failed to write journal: %s", exc)
