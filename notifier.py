"""Telegram + Discord notifications. Non-blocking, fault-tolerant."""
from __future__ import annotations

import threading
from typing import Optional

import requests

from config import settings
from logger import get_logger

log = get_logger("notifier")


def _post_telegram(text: str) -> None:
    if not (settings.telegram_bot_token and settings.telegram_chat_id):
        return
    try:
        url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
        requests.post(
            url,
            json={"chat_id": settings.telegram_chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as exc:  # network errors are non-fatal
        log.warning("Telegram send failed: %s", exc)


def _post_discord(text: str) -> None:
    if not settings.discord_webhook_url:
        return
    try:
        requests.post(settings.discord_webhook_url, json={"content": text}, timeout=10)
    except Exception as exc:
        log.warning("Discord send failed: %s", exc)


def notify(message: str, level: str = "info") -> None:
    """Fire-and-forget notification (Telegram + Discord)."""
    prefix = {"info": "ℹ️", "trade": "📈", "close": "✅", "error": "❌", "summary": "📊"}.get(level, "•")
    text = f"{prefix} {message}"

    def _send() -> None:
        _post_telegram(text)
        _post_discord(text)

    threading.Thread(target=_send, daemon=True).start()


def notify_error(message: str) -> None:
    if settings.notify_on_error:
        notify(message, level="error")
