from __future__ import annotations

import re

from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberBannedError,
    PhoneNumberFloodError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)

from app.utils.logger import log


def normalize_phone(raw: str) -> str | None:
    text = (raw or "").strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    if text.startswith("00"):
        text = "+" + text[2:]
    if text.startswith("+"):
        digits = re.sub(r"\D", "", text)
        if len(digits) < 8 or len(digits) > 15:
            return None
        return "+" + digits
    digits = re.sub(r"\D", "", text)
    if len(digits) < 8 or len(digits) > 15:
        return None
    if len(digits) == 11 and digits.startswith("8"):
        return "+7" + digits[1:]
    return "+" + digits


def normalize_login_code(raw: str) -> str | None:
    digits = re.sub(r"\D", "", raw or "")
    if 3 <= len(digits) <= 8:
        return digits
    return None


class BotTelethonAuth:
    def __init__(self, client: TelegramClient) -> None:
        self.client = client
        self.phone: str | None = None
        self.phone_code_hash: str | None = None

    def clear(self) -> None:
        self.phone = None
        self.phone_code_hash = None

    async def request_code(self, phone: str) -> None:
        sent = await self.client.send_code_request(phone)
        self.phone = phone
        self.phone_code_hash = getattr(sent, "phone_code_hash", None)
        log("AUTH", "Login code requested via bot")

    async def sign_in_code(self, code: str) -> str:
        if not self.phone or not self.phone_code_hash:
            raise RuntimeError("login_not_started")
        try:
            await self.client.sign_in(
                phone=self.phone,
                code=code,
                phone_code_hash=self.phone_code_hash,
            )
        except SessionPasswordNeededError:
            log("AUTH", "2FA password required")
            return "2fa"
        await self._mark_authorized()
        return "ok"

    async def sign_in_password(self, password: str) -> None:
        await self.client.sign_in(password=password)
        await self._mark_authorized()

    async def _mark_authorized(self) -> None:
        self.clear()
        me = await self.client.get_me()
        username = getattr(me, "username", None)
        log("AUTH", f"User client authorized as @{username}" if username else "User client authorized")


def describe_auth_error(error: BaseException) -> str:
    if isinstance(error, FloodWaitError):
        seconds = int(getattr(error, "seconds", 0) or 0)
        return f"Telegram просит подождать {seconds} сек. Потом снова /login"
    if isinstance(error, PhoneNumberInvalidError):
        return "Номер телефона не принят. Отправьте в формате +79001234567"
    if isinstance(error, (PhoneNumberBannedError, PhoneNumberFloodError)):
        return "Этот номер сейчас нельзя использовать для входа"
    if isinstance(error, PhoneCodeInvalidError):
        return "Код неверный. Отправьте код ещё раз или начните заново: /login"
    if isinstance(error, PhoneCodeExpiredError):
        return "Код устарел. Начните заново: /login"
    if isinstance(error, RuntimeError) and str(error) == "login_not_started":
        return "Сначала отправьте номер через /login"
    log("ERROR", f"Login failed: {type(error).__name__}")
    return f"Ошибка входа: {type(error).__name__}. Попробуйте /login ещё раз"
