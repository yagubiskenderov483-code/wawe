from __future__ import annotations

import logging
import re
from typing import Any

_SECRET_RE = re.compile(
    r"("
    r"\d{6,}:[A-Za-z0-9_-]{20,}"
    r"|[a-f0-9]{32}"
    r")",
    re.IGNORECASE,
)

_logger: logging.Logger | None = None
_debug_enabled = False


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        return redact_secrets(message)


def redact_secrets(text: str) -> str:
    if not text:
        return text
    return _SECRET_RE.sub("[redacted]", str(text))


def setup_logging(debug: bool = False) -> logging.Logger:
    global _logger, _debug_enabled
    _debug_enabled = debug
    logger = logging.getLogger("tracker")
    logger.setLevel(logging.DEBUG if debug else logging.INFO)
    logger.handlers.clear()
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingFormatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    logger.addHandler(handler)
    logger.propagate = False
    _logger = logger
    return logger


def get_logger() -> logging.Logger:
    global _logger
    if _logger is None:
        return setup_logging()
    return _logger


def is_debug() -> bool:
    return _debug_enabled


def set_debug(enabled: bool) -> None:
    global _debug_enabled
    _debug_enabled = enabled
    logger = get_logger()
    logger.setLevel(logging.DEBUG if enabled else logging.INFO)


def log(tag: str, message: str, *args: Any, level: int = logging.INFO) -> None:
    get_logger().log(level, "[%s] %s", tag, message if not args else message % args)


def debug(tag: str, message: str, *args: Any) -> None:
    if not _debug_enabled:
        return
    log(tag, message, *args, level=logging.DEBUG)
