from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any


COUNTER_KEYS = (
    "scanned",
    "total_scanned",
    "new_listings",
    "duplicates",
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
    "queued",
    "sent",
    "errors",
    "send_errors",
    "price_changes",
    "floodwaits",
    "filtered",
)


@dataclass
class RuntimeStats:
    started_at: float = field(default_factory=time.time)
    last_scan: str | None = None
    last_publish: str | None = None
    counters: dict[str, int] = field(default_factory=lambda: {key: 0 for key in COUNTER_KEYS})
    scan_times: deque[float] = field(default_factory=lambda: deque(maxlen=100))
    publish_times: deque[float] = field(default_factory=lambda: deque(maxlen=100))

    def inc(self, key: str, amount: int = 1) -> None:
        if key not in self.counters:
            self.counters[key] = 0
        self.counters[key] += amount
        if key in {"price_filtered", "profile_filtered", "rarity_filtered", "number_filtered",
                    "blacklist_filtered", "whitelist_filtered", "language_filtered", "nft_filtered",
                    "free_message_filtered", "account_level_filtered", "manual_tag_filtered"}:
            self.counters["filtered"] += amount

    def get(self, key: str) -> int:
        return int(self.counters.get(key, 0))

    def record_scan(self, duration: float, when: str) -> None:
        self.scan_times.append(duration)
        self.last_scan = when

    def record_publish(self, duration: float, when: str) -> None:
        self.publish_times.append(duration)
        self.last_publish = when

    @property
    def average_scan_time(self) -> float | None:
        return _avg(self.scan_times)

    @property
    def average_publish_time(self) -> float | None:
        return _avg(self.publish_times)

    def snapshot(self) -> dict[str, Any]:
        data = dict(self.counters)
        data["average_scan_time"] = self.average_scan_time
        data["average_publish_time"] = self.average_publish_time
        data["last_scan"] = self.last_scan
        data["last_publish"] = self.last_publish
        data["uptime_sec"] = int(time.time() - self.started_at)
        return data


def _avg(values: deque[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 3)
