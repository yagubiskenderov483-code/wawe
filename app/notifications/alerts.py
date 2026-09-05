from __future__ import annotations

import time

from datetime import datetime, timezone

from aiogram import Bot

from app.config import Settings
from app.utils.logger import log, redact_secrets
from app.utils.state import AppState


def format_error_alert(module: str, error: str, when: str | None = None) -> str:
    stamp = when or datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = redact_secrets(str(error))
    return (
        "🚨 TRACKER ERROR\n\n"
        f"Module: {module}\n\n"
        "Error:\n"
        f"{body}\n\n"
        "Time:\n"
        f"{stamp}"
    )


class AlertManager:
    def __init__(self, settings: Settings, bot: Bot | None, state: AppState | None = None) -> None:
        self.settings = settings
        self.bot = bot
        self.state = state
        self._last_sent: dict[str, float] = {}
        self._min_interval = 60.0

    def bind_bot(self, bot: Bot) -> None:
        self.bot = bot

    async def notify(self, module: str, error: BaseException | str) -> None:
        text = redact_secrets(str(error))
        key = f"{module}:{type(error).__name__ if not isinstance(error, str) else 'str'}"
        now = time.monotonic()
        last = self._last_sent.get(key, 0)
        if now - last < self._min_interval:
            return
        self._last_sent[key] = now
        if self.settings.admin_user_id is None or self.bot is None:
            return
        try:
            await self.bot.send_message(
                chat_id=self.settings.admin_user_id,
                text=format_error_alert(module, text),
            )
        except Exception as send_error:
            log("ERROR", f"Failed to notify admin: {type(send_error).__name__}")
