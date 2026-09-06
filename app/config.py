from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
SESSION_PATH = ROOT_DIR / "sessions" / "market_tracker.session"
DATA_DIR = ROOT_DIR / "data"
DB_PATH = DATA_DIR / "tracker.db"
BACKUP_DIR = DATA_DIR / "backups"

load_dotenv(ROOT_DIR / ".env")

# Credentials provided by the operator for this deployment.
DEFAULT_API_ID = 36101343
DEFAULT_API_HASH = "116195fa5e0459d25a9a6266b40807d7"
DEFAULT_BOT_TOKEN = "8825465611:AAFdbsizmYkOgV2bCzTY9z2Q4ZDELYshcpA"
DEFAULT_BOT_USERNAME = "jsjeigiejwhnewbot"
DEFAULT_TARGET_CHANNEL_ID = 8825465611


def _env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name, "")
    if raw == "":
        return default
    return int(raw)


def _env_optional_int(name: str) -> int | None:
    raw = _env(name, "")
    if raw == "":
        return None
    return int(raw)


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name, "")
    if raw == "":
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _csv_strings(name: str) -> tuple[str, ...]:
    raw = _env(name, "")
    if not raw:
        return ()
    items = []
    for part in raw.split(","):
        item = part.strip()
        if item:
            items.append(item)
    return tuple(items)


def _csv_user_tokens(name: str) -> tuple[str, ...]:
    values = []
    for item in _csv_strings(name):
        values.append(item.lstrip("@").strip().lower())
    return tuple(values)


def _csv_channel_ids(name: str) -> tuple[int, ...]:
    values = []
    for item in _csv_strings(name):
        values.append(int(item))
    return tuple(values)


@dataclass(frozen=True)
class Settings:
    api_id: int
    api_hash: str
    bot_token: str
    target_channel_id: int | None
    target_channels: tuple[int, ...]
    bot_username: str = DEFAULT_BOT_USERNAME
    min_price: int = 5000
    max_price: int = 30000
    scan_interval: float = 2.0
    publish_delay: float = 4.0
    max_nft_count: int = 12
    strict_nft_filter: bool = False
    min_score: int = 6
    min_profile_score: int = 5
    russian_language_required: bool = True
    require_free_messages: bool = True
    enable_account_level_filter: bool = False
    max_account_level: int = 2
    manual_gender_filter: str = ""
    manual_nationality_filter: str = ""
    max_queue_size: int = 100
    profile_cache_ttl: int = 300
    min_gift_number: int | None = None
    max_gift_number: int | None = None
    allowed_models: tuple[str, ...] = field(default_factory=tuple)
    allowed_symbols: tuple[str, ...] = field(default_factory=tuple)
    allowed_backdrops: tuple[str, ...] = field(default_factory=tuple)
    favorite_models: tuple[str, ...] = field(default_factory=tuple)
    whitelist_users: tuple[str, ...] = field(default_factory=tuple)
    blacklist_users: tuple[str, ...] = field(default_factory=tuple)
    admin_user_id: int | None = None
    debug: bool = False
    db_backup_interval: int = 3600
    session_path: str = str(SESSION_PATH)
    db_path: str = str(DB_PATH)
    backup_dir: str = str(BACKUP_DIR)
    max_pages_per_gift: int = 8
    resale_page_limit: int = 50
    max_api_backoff: int = 32

    @property
    def channel_ids(self) -> tuple[int, ...]:
        ids: list[int] = []
        seen: set[int] = set()
        for value in (self.target_channel_id, *self.target_channels):
            if value is None or value in seen:
                continue
            seen.add(value)
            ids.append(value)
        return tuple(ids)

    def is_favorite_model(self, model: str | None) -> bool:
        if not model or not self.favorite_models:
            return False
        needle = model.casefold()
        return any(item.casefold() == needle for item in self.favorite_models)

    def normalized_gender_filter(self) -> str:
        return self.manual_gender_filter.strip().lower()

    def normalized_nationality_filter(self) -> str:
        return self.manual_nationality_filter.strip().lower()


def bot_id_from_token(token: str | None) -> int | None:
    """Bot id is the numeric prefix of a Bot API token (`123456:AAH...`)."""
    raw = (token or "").strip()
    if not raw or ":" not in raw:
        return None
    prefix = raw.split(":", 1)[0]
    if not prefix.isdigit():
        return None
    return int(prefix)


def resolve_publish_chat_ids(
    configured: tuple[int, ...],
    operator_chats: tuple[int, ...],
    bot_id: int | None = None,
) -> tuple[int, ...]:
    """Skip the bot's own id; deliver to real channels and operator private chats."""
    ids: list[int] = []
    seen: set[int] = set()
    for value in (*configured, *operator_chats):
        if value is None:
            continue
        chat_id = int(value)
        if bot_id is not None and chat_id == int(bot_id):
            continue
        if chat_id in seen:
            continue
        seen.add(chat_id)
        ids.append(chat_id)
    return tuple(ids)


def load_settings() -> Settings:
    api_id_raw = _env("API_ID")
    if not api_id_raw or api_id_raw == "PUT_API_ID_HERE":
        api_id_raw = str(DEFAULT_API_ID)

    api_hash = _env("API_HASH")
    if not api_hash or api_hash == "PUT_API_HASH_HERE":
        api_hash = DEFAULT_API_HASH

    bot_token = _env("BOT_TOKEN")
    if not bot_token or bot_token == "PUT_BOT_TOKEN_HERE":
        bot_token = DEFAULT_BOT_TOKEN

    target_raw = _env("TARGET_CHANNEL_ID")
    target_channel_id = DEFAULT_TARGET_CHANNEL_ID
    if target_raw and target_raw != "PUT_CHANNEL_ID_HERE":
        target_channel_id = int(target_raw)

    bot_username = _env("BOT_USERNAME", DEFAULT_BOT_USERNAME).lstrip("@") or DEFAULT_BOT_USERNAME
    target_channels = _csv_channel_ids("TARGET_CHANNELS")
    if not target_channels:
        target_channels = (DEFAULT_TARGET_CHANNEL_ID,)

    settings = Settings(
        api_id=int(api_id_raw),
        api_hash=api_hash,
        bot_token=bot_token,
        target_channel_id=target_channel_id,
        target_channels=target_channels,
        bot_username=bot_username,
        min_price=_env_int("MIN_PRICE", 5000),
        max_price=_env_int("MAX_PRICE", 30000),
        scan_interval=float(_env("SCAN_INTERVAL", "2")),
        publish_delay=float(_env("PUBLISH_DELAY", "4")),
        max_nft_count=_env_int("MAX_NFT_COUNT", 12),
        strict_nft_filter=_env_bool("STRICT_NFT_FILTER", False),
        min_score=_env_int("MIN_SCORE", 6),
        min_profile_score=_env_int("MIN_PROFILE_SCORE", 5),
        russian_language_required=_env_bool("RUSSIAN_LANGUAGE_REQUIRED", True),
        require_free_messages=_env_bool("REQUIRE_FREE_MESSAGES", True),
        enable_account_level_filter=_env_bool("ENABLE_ACCOUNT_LEVEL_FILTER", False),
        max_account_level=_env_int("MAX_ACCOUNT_LEVEL", 2),
        manual_gender_filter=_env("MANUAL_GENDER_FILTER"),
        manual_nationality_filter=_env("MANUAL_NATIONALITY_FILTER"),
        max_queue_size=_env_int("MAX_QUEUE_SIZE", 100),
        profile_cache_ttl=_env_int("PROFILE_CACHE_TTL", 300),
        min_gift_number=_env_optional_int("MIN_GIFT_NUMBER"),
        max_gift_number=_env_optional_int("MAX_GIFT_NUMBER"),
        allowed_models=_csv_strings("ALLOWED_MODELS"),
        allowed_symbols=_csv_strings("ALLOWED_SYMBOLS"),
        allowed_backdrops=_csv_strings("ALLOWED_BACKDROPS"),
        favorite_models=_csv_strings("FAVORITE_MODELS"),
        whitelist_users=_csv_user_tokens("WHITELIST_USERS"),
        blacklist_users=_csv_user_tokens("BLACKLIST_USERS"),
        admin_user_id=_env_optional_int("ADMIN_USER_ID"),
        debug=_env_bool("DEBUG", False),
        db_backup_interval=_env_int("DB_BACKUP_INTERVAL", 3600),
    )
    if not settings.channel_ids:
        raise RuntimeError("TARGET_CHANNEL_ID or TARGET_CHANNELS must be configured")
    return settings


def test_settings(**overrides) -> Settings:
    data = {
        "api_id": 1,
        "api_hash": "test",
        "bot_token": "0:test",
        "target_channel_id": -100123,
        "target_channels": (),
    }
    data.update(overrides)
    return Settings(**data)
