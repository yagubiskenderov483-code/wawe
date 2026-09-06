from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any


COUNTER_KEYS = (
    "scanned",
    "total_scanned",
    "existing",
    "new_listings",
    "new",
    "duplicates",
    "skipped",
    "queued",
    "sent",
    "published_count",
    "errors",
    "send_errors",
    "price_changes",
    "floodwaits",
    "filtered",
    "price_filtered",
    "profile_filtered",
    "rarity_filtered",
    "number_filtered",
    "blacklist_filtered",
    "whitelist_filtered",
    "language_filtered",
    "nft_filtered",
    "free_message_filtered",
    "account_level_filtered",
    "manual_tag_filtered",
    "skip_price",
    "skip_market",
    "skip_nft",
    "skip_gender",
    "skip_language",
    "skip_free_messages",
    "skip_account_level",
    "skip_blacklist",
    "skip_duplicate",
    "skip_owner",
    "market_rejected_count",
)

_SKIP_KEYS = {
    "price_filtered",
    "profile_filtered",
    "rarity_filtered",
    "number_filtered",
    "blacklist_filtered",
    "whitelist_filtered",
    "language_filtered",
    "nft_filtered",
    "free_message_filtered",
    "account_level_filtered",
    "manual_tag_filtered",
    "skip_price",
    "skip_market",
    "skip_nft",
    "skip_gender",
    "skip_language",
    "skip_free_messages",
    "skip_account_level",
    "skip_blacklist",
    "skip_duplicate",
    "skip_owner",
}

_ALIAS = {
    "new": "new_listings",
    "new_listings": "new",
    "sent": "published_count",
    "published_count": "sent",
    "skip_price": "price_filtered",
    "price_filtered": "skip_price",
    "skip_nft": "nft_filtered",
    "nft_filtered": "skip_nft",
    "skip_language": "language_filtered",
    "language_filtered": "skip_language",
    "skip_free_messages": "free_message_filtered",
    "free_message_filtered": "skip_free_messages",
    "skip_account_level": "account_level_filtered",
    "account_level_filtered": "skip_account_level",
    "skip_blacklist": "blacklist_filtered",
    "blacklist_filtered": "skip_blacklist",
    "skip_gender": "manual_tag_filtered",
}


@dataclass
class RuntimeStats:
    started_at: float = field(default_factory=time.time)
    last_scan: str | None = None
    last_publish: str | None = None
    counters: dict[str, int] = field(default_factory=lambda: {key: 0 for key in COUNTER_KEYS})
    scan_times: deque[float] = field(default_factory=lambda: deque(maxlen=100))
    publish_times: deque[float] = field(default_factory=lambda: deque(maxlen=100))
    market_values: deque[float] = field(default_factory=lambda: deque(maxlen=500))
    listing_prices: deque[float] = field(default_factory=lambda: deque(maxlen=500))
    discounts: deque[float] = field(default_factory=lambda: deque(maxlen=500))

    def inc(self, key: str, amount: int = 1) -> None:
        if key not in self.counters:
            self.counters[key] = 0
        self.counters[key] += amount
        alias = _ALIAS.get(key)
        if alias:
            if alias not in self.counters:
                self.counters[alias] = 0
            if alias != key:
                self.counters[alias] += amount
        if key in _SKIP_KEYS:
            self.counters["skipped"] = self.counters.get("skipped", 0) + amount
            self.counters["filtered"] = self.counters.get("filtered", 0) + amount
        if key in {"skip_market"}:
            self.counters["market_rejected_count"] = self.counters.get("market_rejected_count", 0) + amount

    def get(self, key: str) -> int:
        return int(self.counters.get(key, 0))

    def record_scan(self, duration: float, when: str) -> None:
        self.scan_times.append(duration)
        self.last_scan = when

    def record_publish(self, duration: float, when: str) -> None:
        self.publish_times.append(duration)
        self.last_publish = when

    def record_market(self, listing_price: int | None, market_value: int | None, discount: float | None) -> None:
        if listing_price is not None:
            self.listing_prices.append(float(listing_price))
        if market_value is not None:
            self.market_values.append(float(market_value))
        if discount is not None:
            self.discounts.append(float(discount))

    @property
    def average_scan_time(self) -> float | None:
        return _avg(self.scan_times)

    @property
    def average_publish_time(self) -> float | None:
        return _avg(self.publish_times)

    @property
    def average_market_value(self) -> float | None:
        return _avg(self.market_values)

    @property
    def average_listing_price(self) -> float | None:
        return _avg(self.listing_prices)

    @property
    def average_discount(self) -> float | None:
        return _avg(self.discounts)

    def snapshot(self) -> dict[str, Any]:
        data = dict(self.counters)
        data["average_scan_time"] = self.average_scan_time
        data["average_publish_time"] = self.average_publish_time
        data["average_market_value"] = self.average_market_value
        data["average_listing_price"] = self.average_listing_price
        data["average_discount"] = self.average_discount
        data["last_scan"] = self.last_scan
        data["last_publish"] = self.last_publish
        data["uptime_sec"] = int(time.time() - self.started_at)
        return data


def _avg(values: deque[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 3)
