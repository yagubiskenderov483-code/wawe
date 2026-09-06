from __future__ import annotations

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


def build_user_client(settings: Settings) -> TelegramClient:
    from pathlib import Path

    Path(settings.session_path).parent.mkdir(parents=True, exist_ok=True)
    return TelegramClient(
        settings.session_path,
        settings.api_id,
        settings.api_hash,
        flood_sleep_threshold=0,
    )


async def connect_user_client(client: TelegramClient) -> bool:
    log("AUTH", "Connecting Telethon user client")
    await client.connect()
    try:
        authorized = await client.is_user_authorized()
    except (
        AuthKeyUnregisteredError,
        SessionRevokedError,
        SessionExpiredError,
        UserDeactivatedError,
        UserDeactivatedBanError,
    ):
        log("AUTH", "Saved session is unusable. Login via /login after the bot starts")
        return False
    if authorized:
        me = await client.get_me()
        username = getattr(me, "username", None)
        log("AUTH", f"User client authorized as @{username}" if username else "User client authorized")
        return True
    log("AUTH", "User session is not authorized. Login via /login after the bot starts")
    return False
