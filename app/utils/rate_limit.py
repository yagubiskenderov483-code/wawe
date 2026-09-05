from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from telethon.errors import FloodWaitError
from telethon.errors.rpcbaseerrors import RPCError
from telethon.errors.rpcerrorlist import (
    AuthKeyUnregisteredError,
    SessionExpiredError,
    SessionRevokedError,
    UserDeactivatedBanError,
    UserDeactivatedError,
)

from app.utils.logger import debug, log
from app.utils.stats import RuntimeStats

T = TypeVar("T")

TRANSIENT_ERROR_MARKERS = (
    "timeout",
    "timed out",
    "connection",
    "network",
    "temporary",
    "internal",
    "retry",
    "unavailable",
    "flood",
)


class SessionInvalidError(RuntimeError):
    """Raised when the Telethon user session can no longer be used."""


SESSION_ERRORS = (
    AuthKeyUnregisteredError,
    SessionExpiredError,
    SessionRevokedError,
    UserDeactivatedError,
    UserDeactivatedBanError,
)


async def sleep_seconds(seconds: float, sleeper: Callable[[float], Awaitable[None]] | None = None) -> None:
    wait = max(0.0, float(seconds))
    if wait == 0:
        return
    if sleeper is None:
        await asyncio.sleep(wait)
    else:
        await sleeper(wait)


def next_backoff(current: int, maximum: int = 32) -> int:
    if current <= 0:
        return 2
    return min(maximum, current * 2)


def is_transient_error(error: BaseException) -> bool:
    name = type(error).__name__.lower()
    message = str(error).lower()
    if "flood" in name:
        return True
    blob = f"{name} {message}"
    return any(marker in blob for marker in TRANSIENT_ERROR_MARKERS)


async def invoke_telegram(
    func: Callable[..., Awaitable[T]],
    *args: Any,
    stats: RuntimeStats | None = None,
    max_backoff: int = 32,
    sleeper: Callable[[float], Awaitable[None]] | None = None,
    **kwargs: Any,
) -> T:
    backoff = 0
    while True:
        try:
            result = await func(*args, **kwargs)
            return result
        except FloodWaitError as error:
            seconds = int(getattr(error, "seconds", 0) or 0)
            if stats is not None:
                stats.inc("floodwaits")
            log("FLOODWAIT", f"Sleeping {seconds} seconds")
            await sleep_seconds(seconds, sleeper)
            backoff = 0
        except SESSION_ERRORS as error:
            log("ERROR", f"Telegram user session is invalid: {type(error).__name__}")
            raise SessionInvalidError("Telegram user session is invalid. Delete the session file and log in again.") from error
        except SessionInvalidError:
            raise
        except (OSError, RPCError, asyncio.TimeoutError, ConnectionError) as error:
            if not is_transient_error(error) and not isinstance(error, (OSError, asyncio.TimeoutError, ConnectionError)):
                raise
            backoff = next_backoff(backoff, max_backoff)
            log("ERROR", f"Temporary Telegram/network error, retry in {backoff}s: {type(error).__name__}")
            await sleep_seconds(backoff, sleeper)
        except Exception as error:
            if not is_transient_error(error):
                raise
            backoff = next_backoff(backoff, max_backoff)
            log("ERROR", f"Temporary error, retry in {backoff}s: {type(error).__name__}")
            await sleep_seconds(backoff, sleeper)


class ApiLimiter:
    def __init__(self, concurrency: int = 1, min_interval: float = 0.25) -> None:
        self._semaphore = asyncio.Semaphore(max(1, concurrency))
        self._min_interval = min_interval
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def __aenter__(self) -> "ApiLimiter":
        await self._semaphore.acquire()
        async with self._lock:
            now = asyncio.get_running_loop().time()
            wait = self._min_interval - (now - self._last)
            if wait > 0:
                debug("RATE", f"Throttling {wait:.2f}s")
                await asyncio.sleep(wait)
            self._last = asyncio.get_running_loop().time()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self._semaphore.release()
