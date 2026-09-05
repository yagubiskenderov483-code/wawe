from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


STATUS_NEW = "NEW"
STATUS_QUEUED = "QUEUED"
STATUS_SENT = "SENT"
STATUS_SKIPPED = "SKIPPED"
STATUS_ERROR = "ERROR"

LANGUAGE_RU = "ru"
LANGUAGE_EN = "en"
LANGUAGE_MIXED = "mixed"
LANGUAGE_UNKNOWN = "unknown"


@dataclass
class Listing:
    listing_key: str
    gift_id: Optional[int] = None
    slug: Optional[str] = None
    gift_name: Optional[str] = None
    gift_number: Optional[int] = None
    price: Optional[int] = None
    model: Optional[str] = None
    symbol: Optional[str] = None
    backdrop: Optional[str] = None
    owner_id: Optional[int] = None
    first_seen_at: Optional[str] = None
    sent_at: Optional[str] = None
    score: Optional[int] = None
    unique_id: Optional[int] = None
    owner_username: Optional[str] = None
    collectible: bool = True
    resale: bool = True
    is_new: bool = False
    is_price_change: bool = False
    old_price: Optional[int] = None
    status: str = STATUS_NEW
    skip_reason: Optional[str] = None
    last_notified_price: Optional[int] = None

    @property
    def nft_url(self) -> Optional[str]:
        if not self.slug:
            return None
        return f"https://t.me/nft/{self.slug}"

    @property
    def price_change_percent(self) -> Optional[float]:
        if self.old_price in (None, 0) or self.price is None:
            return None
        return ((self.price - self.old_price) / self.old_price) * 100.0


@dataclass
class Profile:
    user_id: Optional[int] = None
    username: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    bio: Optional[str] = None
    language: Optional[str] = None
    language_score: Optional[float] = None
    nft_count: Optional[int] = None
    free_messages: Optional[bool] = None
    account_level: Optional[int] = None
    public_channel: Optional[bool] = None
    public_gifts: Optional[bool] = None
    manual_gender: Optional[str] = None
    manual_nationality: Optional[str] = None
    manual_tag: Optional[str] = None
    updated_at: Optional[str] = None
    cached: bool = False


@dataclass
class FilterResult:
    passed: bool
    reason: str

    def __iter__(self):
        yield self.passed
        yield self.reason


@dataclass
class QueueItem:
    listing: Listing
    profile: Profile
    priority: int
    extra: dict[str, Any] = field(default_factory=dict)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
