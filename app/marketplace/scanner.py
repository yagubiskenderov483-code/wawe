from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

from telethon import TelegramClient
from telethon.tl.functions.payments import GetResaleStarGiftsRequest, GetStarGiftsRequest
from telethon.tl.types import StarGift, User
from telethon.tl.types.payments import StarGifts, StarGiftsNotModified

from app.config import Settings
from app.marketplace.filters import (
    calculate_priority,
    calculate_score,
    check_account_level,
    check_backdrop,
    check_blacklist,
    check_collectible,
    check_free_messages,
    check_gift_number,
    check_language,
    check_manual_profile_tags,
    check_model,
    check_nft_count,
    check_price,
    check_symbol,
    check_whitelist,
    classify_filter_stat,
    should_publish,
)
from app.marketplace.models import (
    STATUS_ERROR,
    STATUS_NEW,
    STATUS_QUEUED,
    STATUS_SKIPPED,
    Listing,
    QueueItem,
    utc_now_iso,
)
from app.marketplace.parser import parse_listing
from app.notifications.alerts import AlertManager
from app.profile.analyzer import ProfileAnalyzer
from app.storage.database import Database
from app.utils.logger import debug, log, redact_secrets
from app.utils.rate_limit import ApiLimiter, SessionInvalidError, invoke_telegram, next_backoff, sleep_seconds
from app.utils.state import AppState


class MarketplaceScanner:
    def __init__(
        self,
        settings: Settings,
        state: AppState,
        db: Database,
        client: TelegramClient,
        analyzer: ProfileAnalyzer,
        limiter: ApiLimiter,
        alerts: AlertManager,
    ) -> None:
        self.settings = settings
        self.state = state
        self.db = db
        self.client = client
        self.analyzer = analyzer
        self.limiter = limiter
        self.alerts = alerts
        self._gift_ids: list[int] = []
        self._gift_hash = 0
        self._users: dict[int, User] = {}

    async def run(self) -> None:
        self.state.scanner_running = True
        backoff = 2
        log("SCAN", "Scanner started")
        try:
            while not self.state.shutdown:
                if self.state.session_invalid:
                    log("ERROR", "Telegram user session is invalid, scanner stopped")
                    return
                try:
                    await self.state.pause_event.wait()
                    if self.state.shutdown:
                        return
                    if self.state.scanner_paused:
                        await sleep_seconds(0.5)
                        continue
                    started = time.monotonic()
                    await self.scan_once()
                    self.state.stats.record_scan(time.monotonic() - started, utc_now_iso())
                    backoff = 2
                    await sleep_seconds(self.settings.scan_interval)
                except SessionInvalidError as error:
                    self.state.session_invalid = True
                    log("ERROR", str(error))
                    await self.alerts.notify("MarketplaceScanner", error)
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    self.state.stats.inc("errors")
                    log("ERROR", f"Scanner error: {type(error).__name__}: {redact_secrets(str(error))}")
                    await self.alerts.notify("MarketplaceScanner", error)
                    await sleep_seconds(backoff)
                    backoff = next_backoff(backoff, self.settings.max_api_backoff)
        finally:
            self.state.scanner_running = False
            log("SCAN", "Scanner stopped")

    async def scan_once(self) -> None:
        self.state.stats.last_scan = utc_now_iso()
        gift_ids = await self._load_gift_ids()
        debug("SCAN", f"Gift types for resale: {len(gift_ids)}")
        total = 0
        for gift_id in gift_ids:
            if self.state.shutdown or self.state.scanner_paused:
                break
            total += await self._scan_gift(gift_id)
        if total:
            log("SCAN", f"Получено {total} лотов")

    async def _load_gift_ids(self) -> list[int]:
        async def _call():
            async with self.limiter:
                return await self.client(GetStarGiftsRequest(hash=self._gift_hash))

        result = await invoke_telegram(_call, stats=self.state.stats, max_backoff=self.settings.max_api_backoff)
        if isinstance(result, StarGiftsNotModified):
            debug("SCAN", "Star gifts not modified")
            return list(self._gift_ids)
        if not isinstance(result, StarGifts):
            gifts = getattr(result, "gifts", None) or []
        else:
            gifts = result.gifts
            self._gift_hash = getattr(result, "hash", 0) or 0
        ids: list[int] = []
        all_ids: list[int] = []
        for gift in gifts:
            if not isinstance(gift, StarGift) and type(gift).__name__ != "StarGift":
                continue
            gift_id = getattr(gift, "id", None)
            if gift_id is None:
                continue
            all_ids.append(int(gift_id))
            resale = getattr(gift, "availability_resale", None)
            min_stars = getattr(gift, "resell_min_stars", None)
            if resale or min_stars:
                ids.append(int(gift_id))
        chosen = ids or all_ids
        if chosen:
            self._gift_ids = chosen
        debug("SCAN", f"Resale gift_ids={len(self._gift_ids)}")
        return list(self._gift_ids)

    async def _scan_gift(self, gift_id: int) -> int:
        offset = ""
        pages = 0
        seen_offsets: set[str] = set()
        known_streak = 0
        found = 0
        while pages < self.settings.max_pages_per_gift:
            if self.state.shutdown or self.state.scanner_paused:
                break
            if offset in seen_offsets:
                debug("SCAN", f"Pagination loop detected for gift_id={gift_id}, reset offset")
                self.db.reset_scanner_offset(gift_id)
                break
            seen_offsets.add(offset)
            page_started = time.monotonic()
            try:
                result = await self._fetch_resale_page(gift_id, offset)
            except Exception as error:
                message = str(error).upper()
                if "OFFSET" in message:
                    log("SCAN", f"Stored offset unusable for gift_id={gift_id}, resetting")
                    self.db.reset_scanner_offset(gift_id)
                    if offset:
                        offset = ""
                        continue
                raise
            gifts = getattr(result, "gifts", None) or []
            users = getattr(result, "users", None) or []
            for user in users:
                user_id = getattr(user, "id", None)
                if user_id is not None:
                    self._users[int(user_id)] = user
            debug(
                "SCAN",
                f"gift_id={gift_id} page={pages} objects={len(gifts)} users={len(users)} "
                f"next_offset={getattr(result, 'next_offset', None)!r} time={time.monotonic() - page_started:.2f}s",
            )
            found += len(gifts)
            self.state.stats.inc("scanned", len(gifts))
            self.state.stats.inc("total_scanned", len(gifts))
            for gift in gifts:
                is_new_signal = await self._process_gift(gift)
                if is_new_signal is False:
                    known_streak += 1
                else:
                    known_streak = 0
            next_offset = getattr(result, "next_offset", None) or ""
            self.db.set_scanner_offset(gift_id, next_offset)
            pages += 1
            if not next_offset:
                break
            if known_streak >= 12:
                debug("SCAN", f"Stopping pagination for gift_id={gift_id}: reached known listings")
                break
            offset = next_offset
        return found

    async def _fetch_resale_page(self, gift_id: int, offset: str) -> Any:
        async def _call():
            async with self.limiter:
                return await self.client(
                    GetResaleStarGiftsRequest(
                        gift_id=gift_id,
                        offset=offset,
                        limit=self.settings.resale_page_limit,
                        stars_only=True,
                    )
                )

        return await invoke_telegram(_call, stats=self.state.stats, max_backoff=self.settings.max_api_backoff)

    async def _process_gift(self, gift: Any) -> Optional[bool]:
        listing = parse_listing(gift)
        if listing is None:
            return None
        existing = self.db.get_listing(listing.listing_key)
        signal = self._classify_signal(listing, existing)
        if signal is None:
            self.state.stats.inc("duplicates")
            return False

        if existing is None:
            listing.is_new = True
            listing.status = STATUS_NEW
            self.db.insert_listing(listing)
            self.state.stats.inc("new_listings")
            log("NEW", "Новый лот найден")
        else:
            listing.first_seen_at = existing.get("first_seen_at") or listing.first_seen_at
            listing.last_notified_price = existing.get("last_notified_price")
            if signal == "price_change":
                recorded = self.db.record_price_change(listing.listing_key, listing.old_price, listing.price)
                if not recorded:
                    self.state.stats.inc("duplicates")
                    return False
                listing.is_price_change = True
                self.state.stats.inc("price_changes")

        cheap = [
            check_collectible(listing),
            check_price(listing, self.settings),
            check_model(listing, self.settings),
            check_symbol(listing, self.settings),
            check_backdrop(listing, self.settings),
            check_gift_number(listing, self.settings),
        ]
        for result in cheap:
            if not result.passed:
                self._skip(listing, result.reason)
                return True

        owner = self._users.get(listing.owner_id) if listing.owner_id is not None else None
        stub_profile = await self.analyzer.get_profile(listing.owner_id, user=owner) if listing.owner_id else (
            await self.analyzer.get_profile(None, user=None)
        )
        listing.owner_username = stub_profile.username

        profile_checks = [
            check_blacklist(stub_profile, self.settings, listing),
            check_whitelist(stub_profile, self.settings, listing),
            check_language(stub_profile, self.settings),
            check_nft_count(stub_profile, self.settings),
            check_free_messages(stub_profile, self.settings),
            check_account_level(stub_profile, self.settings),
            check_manual_profile_tags(stub_profile, settings=self.settings),
        ]
        for result in profile_checks:
            if not result.passed:
                self._skip(listing, result.reason)
                return True

        final = should_publish(listing, stub_profile, self.settings)
        if not final.passed:
            self._skip(listing, final.reason)
            return True

        total, _, _ = calculate_score(listing, stub_profile, self.settings)
        listing.score = total
        self.db.update_listing(listing.listing_key, score=total, status=STATUS_QUEUED, owner_id=listing.owner_id)
        await self._enqueue(listing, stub_profile)
        return True

    def _classify_signal(self, listing: Listing, existing: Optional[dict[str, Any]]) -> Optional[str]:
        if existing is None:
            return "new"
        old_price = existing.get("price")
        status = existing.get("status")
        if listing.price is not None and old_price is not None and listing.price != old_price:
            listing.old_price = old_price
            listing.is_price_change = True
            if status == STATUS_QUEUED:
                return None
            return "price_change"
        if status in {STATUS_QUEUED}:
            return None
        if status == STATUS_ERROR:
            return "retry"
        if existing.get("sent_at"):
            return None
        if existing.get("last_notified_price") is not None and existing.get("last_notified_price") == listing.price:
            return None
        if status == STATUS_SKIPPED:
            reason = existing.get("skip_reason") or ""
            if reason.startswith("price_") and listing.price is not None:
                if self.settings.min_price <= listing.price <= self.settings.max_price:
                    return "price_in_range"
            return None
        if status == STATUS_NEW:
            return "retry"
        return None

    def _skip(self, listing: Listing, reason: str) -> None:
        self.db.mark_status(listing.listing_key, STATUS_SKIPPED, reason)
        stat = classify_filter_stat(reason)
        if stat:
            self.state.stats.inc(stat)
        debug("FILTER", f"{listing.listing_key} skipped: {reason}")

    async def _enqueue(self, listing: Listing, profile) -> None:
        if self.state.queue.is_full():
            log("QUEUE", "Queue is full, listing skipped")
            self.db.mark_status(listing.listing_key, STATUS_NEW, "queue_full")
            return
        priority = calculate_priority(listing, profile, self.settings)
        item = QueueItem(listing=listing, profile=profile, priority=priority)
        queued = await self.state.queue.put(item)
        if not queued:
            log("QUEUE", "Queue is full, listing skipped")
            self.db.mark_status(listing.listing_key, STATUS_NEW, "queue_full")
            return
        self.state.stats.inc("queued")
        debug("QUEUE", f"size={self.state.queue.qsize()} priority={priority}")
        log("QUEUE", "Лот добавлен в очередь")
