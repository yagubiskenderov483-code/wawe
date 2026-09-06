from __future__ import annotations

from typing import Any, Iterable, Optional

from telethon import TelegramClient
from telethon.tl.functions.payments import GetResaleStarGiftsRequest

from app.config import Settings
from app.marketplace.models import (
    MARKET_CONFIDENCE_HIGH,
    MARKET_CONFIDENCE_LOW,
    MARKET_CONFIDENCE_MEDIUM,
    MARKET_CONFIDENCE_NONE,
    Listing,
    MarketEstimate,
)
from app.marketplace.parser import parse_listing
from app.storage.database import Database, market_cache_key
from app.utils.logger import debug, log
from app.utils.rate_limit import ApiLimiter, invoke_telegram
from app.utils.stats import RuntimeStats


def median_int(values: Iterable[int]) -> Optional[int]:
    sample = sorted(int(item) for item in values)
    if not sample:
        return None
    count = len(sample)
    mid = count // 2
    if count % 2:
        return sample[mid]
    return int(round((sample[mid - 1] + sample[mid]) / 2))


def market_confidence(sample_size: int) -> str:
    if sample_size <= 0:
        return MARKET_CONFIDENCE_NONE
    if sample_size < 5:
        return MARKET_CONFIDENCE_LOW
    if sample_size < 10:
        return MARKET_CONFIDENCE_MEDIUM
    return MARKET_CONFIDENCE_HIGH


def apply_market_estimate(listing: Listing, estimate: MarketEstimate) -> Listing:
    listing.market_value = estimate.market_value
    listing.floor_price = estimate.floor_price
    listing.market_confidence = estimate.confidence
    listing.market_sample_size = estimate.sample_size
    if listing.price is not None and estimate.market_value:
        listing.price_ratio = listing.price / estimate.market_value
        listing.discount_percent = ((estimate.market_value - listing.price) / estimate.market_value) * 100.0
    else:
        listing.price_ratio = None
        listing.discount_percent = None
    return listing


def _attr_match(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    return left.casefold() == right.casefold()


class MarketEstimator:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        client: TelegramClient | None,
        limiter: ApiLimiter | None,
        stats: RuntimeStats | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.client = client
        self.limiter = limiter
        self.stats = stats

    async def estimate(self, listing: Listing, force_refresh: bool = False) -> MarketEstimate:
        if listing.gift_id is None:
            log("MARKET", "Comparable scan skipped: gift_id unavailable")
            return MarketEstimate()

        cache_key = market_cache_key(listing.gift_id, listing.model, listing.symbol, listing.backdrop)
        if not force_refresh:
            cached = self.db.get_market_cache(cache_key, self.settings.market_cache_ttl)
            if cached is not None:
                estimate = MarketEstimate(
                    market_value=cached.get("market_value"),
                    floor_price=cached.get("floor_price"),
                    sample_size=int(cached.get("sample_size") or 0),
                    confidence=str(cached.get("confidence") or MARKET_CONFIDENCE_NONE),
                    cache_hit=True,
                )
                debug(
                    "MARKET",
                    f"cache hit key={cache_key} value={estimate.market_value} sample={estimate.sample_size}",
                )
                apply_market_estimate(listing, estimate)
                return estimate

        estimate = await self._scan_comparables(listing)
        self.db.set_market_cache(
            cache_key,
            listing.gift_id,
            listing.model,
            listing.symbol,
            listing.backdrop,
            estimate.market_value,
            estimate.floor_price,
            estimate.sample_size,
            estimate.confidence,
        )
        apply_market_estimate(listing, estimate)
        if estimate.market_value is not None:
            log("MARKET", f"Estimated market value: {estimate.market_value}")
            if listing.price_ratio is not None:
                log("MARKET", f"Ratio: {listing.price_ratio:.2f}")
            if listing.discount_percent is not None:
                debug("MARKET", f"discount={listing.discount_percent:.1f}% floor={estimate.floor_price}")
        else:
            log("MARKET", "Market value unavailable: no comparable listings")
        return estimate

    async def _scan_comparables(self, listing: Listing) -> MarketEstimate:
        if self.client is None:
            return MarketEstimate()

        exact: list[int] = []
        same_gift: list[int] = []
        offset = ""
        pages = 0
        seen_offsets: set[str] = set()
        needed = max(1, int(self.settings.market_sample_size))
        max_pages = max(2, (needed + self.settings.resale_page_limit - 1) // self.settings.resale_page_limit + 1)

        while pages < max_pages and (len(exact) < needed or len(same_gift) < needed):
            if offset in seen_offsets:
                break
            seen_offsets.add(offset)
            try:
                result = await self._fetch_page(int(listing.gift_id), offset)
            except Exception as error:
                log("ERROR", f"Market scan failed: {type(error).__name__}")
                break
            gifts = getattr(result, "gifts", None) or []
            for gift in gifts:
                parsed = parse_listing(gift)
                if parsed is None or parsed.price is None:
                    continue
                if parsed.listing_key == listing.listing_key:
                    continue
                if listing.unique_id is not None and parsed.unique_id == listing.unique_id:
                    continue
                if len(same_gift) < needed:
                    same_gift.append(parsed.price)
                if self._is_close_comparable(listing, parsed) and len(exact) < needed:
                    exact.append(parsed.price)
            next_offset = getattr(result, "next_offset", None) or ""
            pages += 1
            if not next_offset:
                break
            offset = next_offset

        sample = exact if exact else same_gift
        sample = sample[:needed]
        value = median_int(sample)
        floor = min(sample) if sample else None
        return MarketEstimate(
            market_value=value,
            floor_price=floor,
            sample_size=len(sample),
            confidence=market_confidence(len(sample)),
            prices=tuple(sample),
        )

    def _is_close_comparable(self, listing: Listing, other: Listing) -> bool:
        if listing.gift_id is not None and other.gift_id is not None and listing.gift_id != other.gift_id:
            return False
        wanted = [
            (listing.model, other.model),
            (listing.symbol, other.symbol),
            (listing.backdrop, other.backdrop),
        ]
        required = [(left, right) for left, right in wanted if left]
        if not required:
            return True
        return all(_attr_match(left, right) for left, right in required)

    async def _fetch_page(self, gift_id: int, offset: str) -> Any:
        async def _call():
            if self.limiter is not None:
                async with self.limiter:
                    return await self.client(
                        GetResaleStarGiftsRequest(
                            gift_id=gift_id,
                            offset=offset,
                            limit=self.settings.resale_page_limit,
                            stars_only=True,
                        )
                    )
            return await self.client(
                GetResaleStarGiftsRequest(
                    gift_id=gift_id,
                    offset=offset,
                    limit=self.settings.resale_page_limit,
                    stars_only=True,
                )
            )

        return await invoke_telegram(_call, stats=self.stats, max_backoff=self.settings.max_api_backoff)
