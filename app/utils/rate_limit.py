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
    limiter: "ApiLimiter | None" = None,
    **kwargs: Any,
) -> T:
    backoff = 0
    while True:
        try:
            result = await func(*args, **kwargs)
            if limiter is not None:
                limiter.note_success()
            return result
        except FloodWaitError as error:
            seconds = int(getattr(error, "seconds", 0) or 0)
            if stats is not None:
                stats.inc("floodwaits")
            if limiter is not None:
                limiter.note_floodwait()
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
    """Paces Telegram calls, and slows itself down when Telegram pushes back.

    Every FloodWait means the previous pace was too fast, so the interval grows
    and only decays back after a quiet stretch. Sitting a little below the
    limit is faster overall than repeatedly sleeping off 40-second waits.
    """

    def __init__(
        self,
        concurrency: int = 1,
        min_interval: float = 0.25,
        max_interval: float = 5.0,
        adaptive: bool = True,
    ) -> None:
        self._semaphore = asyncio.Semaphore(max(1, concurrency))
        self._base_interval = min_interval
        self._min_interval = min_interval
        self._max_interval = max(min_interval, max_interval)
        self._adaptive = adaptive
        self._lock = asyncio.Lock()
        self._last = 0.0
        self._calls_since_floodwait = 0

    @property
    def interval(self) -> float:
        return self._min_interval

    def note_floodwait(self) -> None:
        if not self._adaptive:
            return
        self._calls_since_floodwait = 0
        raised = min(self._max_interval, round(self._min_interval * 1.5, 2))
        if raised > self._min_interval:
            self._min_interval = raised
            log("RATE", f"FloodWait: slowing calls down to {raised}s apart")

    def note_success(self) -> None:
        if not self._adaptive or self._min_interval <= self._base_interval:
            return
        self._calls_since_floodwait += 1
        if self._calls_since_floodwait < 200:
            return
        self._calls_since_floodwait = 0
        lowered = max(self._base_interval, round(self._min_interval * 0.8, 2))
        if lowered < self._min_interval:
            self._min_interval = lowered
            log("RATE", f"Quiet stretch: speeding calls back up to {lowered}s apart")

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
