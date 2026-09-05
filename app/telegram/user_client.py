from __future__ import annotations

from pathlib import Path

from telethon import TelegramClient
from telethon.errors import (
    AuthKeyUnregisteredError,
    SessionExpiredError,
    SessionRevokedError,
    UserDeactivatedBanError,
    UserDeactivatedError,
)

from app.config import Settings
from app.utils.logger import log
from app.utils.rate_limit import SessionInvalidError


def build_user_client(settings: Settings) -> TelegramClient:
    Path(settings.session_path).parent.mkdir(parents=True, exist_ok=True)
    return TelegramClient(
        settings.session_path,
        settings.api_id,
        settings.api_hash,
        flood_sleep_threshold=0,
    )


async def start_user_client(client: TelegramClient) -> None:
    log("AUTH", "Connecting Telethon user client")
    try:
        await client.start()
    except (AuthKeyUnregisteredError, SessionRevokedError, SessionExpiredError, UserDeactivatedError, UserDeactivatedBanError) as error:
        log("ERROR", f"Telegram user session is invalid: {type(error).__name__}")
        raise SessionInvalidError("Telegram user session is invalid. Delete the session file and log in again.") from error
    if not await client.is_user_authorized():
        log("ERROR", "Telegram user session is not authorized")
        raise SessionInvalidError("Telegram user session is not authorized")
    me = await client.get_me()
    username = getattr(me, "username", None)
    log("AUTH", f"User client authorized as @{username}" if username else "User client authorized")
