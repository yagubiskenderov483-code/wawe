from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

from telethon import TelegramClient
from telethon.tl.functions.payments import GetStarGiftsRequest
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
    check_market_value,
    check_model,
    check_nft_count,
    check_price,
    check_symbol,
    check_whitelist,
    classify_filter_stat,
    should_publish,
)
from app.marketplace.market import MarketEstimator
from app.marketplace.models import (
    SCANNER_MODE_INITIAL_SNAPSHOT,
    SCANNER_MODE_LIVE,
    STATUS_ERROR,
    STATUS_EXISTING,
    STATUS_NEW,
    STATUS_QUEUED,
    STATUS_SENT,
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
        market: MarketEstimator | None = None,
    ) -> None:
        self.settings = settings
        self.state = state
        self.db = db
        self.client = client
        self.analyzer = analyzer
        self.limiter = limiter
        self.alerts = alerts
        self.market = market or MarketEstimator(settings, db, client, limiter, state.stats)
        self._gift_ids: list[int] = []
        self._gift_hash = 0
        self._users: dict[int, User] = {}
        self._restore_mode()

    def _restore_mode(self) -> None:
        stored = self.db.get_scanner_mode()
        if stored in {SCANNER_MODE_INITIAL_SNAPSHOT, SCANNER_MODE_LIVE}:
            self.state.scanner_mode = stored
            return
        self.db.set_scanner_mode(SCANNER_MODE_INITIAL_SNAPSHOT)
        self.db.set_meta("snapshot_started_at", utc_now_iso())
        self.state.scanner_mode = SCANNER_MODE_INITIAL_SNAPSHOT
        log("SNAPSHOT", "Capturing current listings without publishing")

    def is_snapshot(self) -> bool:
        return self.state.scanner_mode == SCANNER_MODE_INITIAL_SNAPSHOT

    async def run(self) -> None:
        self.state.scanner_running = True
        backoff = 2
        log("SCANNER", "Scanner started")
        try:
            while not self.state.shutdown:
                if self.state.session_invalid:
                    log("ERROR", "Telegram user session is invalid, scanner stopped")
                    return
                try:
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
            log("SCANNER", "Scanner stopped")

    async def scan_once(self) -> None:
        self.state.stats.last_scan = utc_now_iso()
        gift_ids = await self._load_gift_ids()
        debug("SCANNER", f"Gift types for resale: {len(gift_ids)}")
        total = 0
        snapshot = self.is_snapshot()
        for gift_id in gift_ids:
            if self.state.shutdown:
                break
            total += await self._scan_gift(gift_id, snapshot=snapshot)
        if snapshot and not self.state.shutdown:
            self._finish_snapshot()
        if total:
            tag = "SNAPSHOT" if snapshot else "SCANNER"
            log(tag, f"Processed {total} listings")

    def _finish_snapshot(self) -> None:
        self.db.set_scanner_mode(SCANNER_MODE_LIVE)
        self.db.set_meta("snapshot_completed_at", utc_now_iso())
        self.state.scanner_mode = SCANNER_MODE_LIVE
        log("LIVE", "Initial snapshot complete. Switching to LIVE mode")

    async def _load_gift_ids(self) -> list[int]:
        async def _call():
            async with self.limiter:
                return await self.client(GetStarGiftsRequest(hash=self._gift_hash))

        result = await invoke_telegram(_call, stats=self.state.stats, max_backoff=self.settings.max_api_backoff)
        if isinstance(result, StarGiftsNotModified):
            debug("SCANNER", "Star gifts not modified")
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
        debug("SCANNER", f"Resale gift_ids={len(self._gift_ids)}")
        return list(self._gift_ids)

    async def _scan_gift(self, gift_id: int, snapshot: bool = False) -> int:
        offset = ""
        pages = 0
        seen_offsets: set[str] = set()
        known_streak = 0
        found = 0
        max_pages = self.settings.max_snapshot_pages_per_gift if snapshot else self.settings.max_pages_per_gift
        while pages < max_pages:
            if self.state.shutdown:
                break
            if offset in seen_offsets:
                debug("SCANNER", f"Pagination loop detected for gift_id={gift_id}, reset offset")
                self.db.reset_scanner_offset(gift_id)
                break
            seen_offsets.add(offset)
            page_started = time.monotonic()
            try:
                result = await self._fetch_resale_page(gift_id, offset)
            except Exception as error:
                message = str(error).upper()
                if "OFFSET" in message:
                    log("SCANNER", f"Stored offset unusable for gift_id={gift_id}, resetting")
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
                "SCANNER",
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
            if not snapshot and known_streak >= 1:
                debug("SCANNER", f"Stopping pagination for gift_id={gift_id}: reached known listings")
                break
            offset = next_offset
        return found

    async def _fetch_resale_page(self, gift_id: int, offset: str) -> Any:
        from telethon.tl.functions.payments import GetResaleStarGiftsRequest

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
        if self.is_snapshot():
            return self._snapshot_listing(listing, existing)

        signal = self._classify_signal(listing, existing)
        if signal is None:
            self.state.stats.inc("duplicates")
            return False

        if existing is None:
            listing.is_new = True
            listing.status = STATUS_NEW
            listing.is_initial_snapshot = False
            inserted = self.db.insert_listing(listing)
            if not inserted:
                self.state.stats.inc("duplicates")
                self.state.stats.inc("skip_duplicate")
                return False
            self.state.stats.inc("new_listings")
            log("LIVE", f"New listing detected: {listing.listing_key} gift_id={listing.gift_id} price={listing.price}")
        else:
            listing.first_seen_at = existing.get("first_seen_at") or listing.first_seen_at
            listing.last_notified_price = existing.get("last_notified_price")
            listing.is_initial_snapshot = bool(existing.get("is_initial_snapshot"))
            if signal != "retry":
                return False

        return await self._filter_and_maybe_queue(listing)

    def _snapshot_listing(self, listing: Listing, existing: Optional[dict[str, Any]]) -> bool:
        if existing is not None:
            log("SNAPSHOT", f"Existing listing -> SKIP {listing.listing_key}")
            self.state.stats.inc("existing")
            return False
        listing.is_new = False
        listing.is_initial_snapshot = True
        listing.status = STATUS_EXISTING
        listing.skip_reason = "initial_snapshot"
        inserted = self.db.insert_listing(listing)
        if not inserted:
            self.state.stats.inc("duplicates")
            return False
        self.db.mark_existing(listing.listing_key)
        self.state.stats.inc("existing")
        log("SNAPSHOT", f"Existing listing -> SKIP {listing.listing_key}")
        return False

    async def _filter_and_maybe_queue(self, listing: Listing) -> bool:
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

        if self.settings.unique_owners:
            seller_id = listing.seller_id or listing.owner_id
            if self.db.seller_was_published(seller_id):
                log("FILTER", f"Owner already published -> SKIP {seller_id}")
                self._skip(listing, "owner_already_published")
                return True
            if self.db.seller_is_queued(seller_id, listing.listing_key):
                log("FILTER", f"Owner already queued -> SKIP {seller_id}")
                self._skip(listing, "owner_already_queued")
                return True

        owner = self._users.get(listing.owner_id) if listing.owner_id is not None else None
        profile = await self.analyzer.get_profile(listing.owner_id, user=owner) if listing.owner_id else (
            await self.analyzer.get_profile(None, user=None)
        )
        listing.owner_username = profile.username
        listing.manual_gender = profile.manual_gender
        listing.manual_nationality = profile.manual_nationality

        black = check_blacklist(profile, self.settings, listing)
        if not black.passed:
            self._skip(listing, black.reason)
            return True

        profile_results = [
            check_nft_count(profile, self.settings),
            check_manual_profile_tags(profile, settings=self.settings),
            check_language(profile, self.settings),
            check_free_messages(profile, self.settings),
            check_account_level(profile, self.settings),
        ]
        whitelist_hit = check_whitelist(profile, self.settings, listing)
        if not whitelist_hit.passed:
            self._skip(listing, whitelist_hit.reason)
            return True
        apply_profile = whitelist_hit.reason != "whitelist_ok"
        if apply_profile:
            for result in profile_results:
                if not result.passed:
                    self._skip(listing, result.reason)
                    return True

        await self.market.estimate(listing)
        market = check_market_value(listing, self.settings)
        if not market.passed:
            self._skip(listing, market.reason)
            return True

        final = should_publish(listing, profile, self.settings, skip_market=True)
        if not final.passed:
            self._skip(listing, final.reason)
            return True

        total, _, profile_score = calculate_score(listing, profile, self.settings)
        listing.score = total
        listing.profile_score = profile_score
        self.db.update_listing(
            listing.listing_key,
            score=total,
            profile_score=profile_score,
            status=STATUS_QUEUED,
            owner_id=listing.owner_id,
            seller_id=listing.seller_id or listing.owner_id,
            market_value=listing.market_value,
            floor_price=listing.floor_price,
            price_ratio=listing.price_ratio,
            discount_percent=listing.discount_percent,
            market_confidence=listing.market_confidence,
            market_sample_size=listing.market_sample_size,
            manual_gender=listing.manual_gender,
            manual_nationality=listing.manual_nationality,
            owner_username=listing.owner_username,
            price=listing.price,
        )
        self.state.stats.record_market(listing.price, listing.market_value, listing.discount_percent)
        await self._enqueue(listing, profile)
        return True

    def _classify_signal(self, listing: Listing, existing: Optional[dict[str, Any]]) -> Optional[str]:
        if existing is None:
            return "new"
        old_price = existing.get("price")
        if listing.price is not None and old_price is not None and listing.price != old_price:
            listing.old_price = old_price
            self.db.record_price_change(listing.listing_key, old_price, listing.price)
            self.state.stats.inc("price_changes")
            log("LIVE", f"PRICE_CHANGED {listing.listing_key}: {old_price} -> {listing.price} (not a new listing)")
        if existing.get("sent_at") or existing.get("status") == STATUS_SENT:
            return None
        if existing.get("is_initial_snapshot") or existing.get("status") == STATUS_EXISTING:
            return None
        if existing.get("status") == STATUS_ERROR and not existing.get("is_initial_snapshot"):
            return "retry"
        return None

    def _skip(self, listing: Listing, reason: str) -> None:
        self.db.mark_status(listing.listing_key, STATUS_SKIPPED, reason)
        stat = classify_filter_stat(reason)
        if stat:
            self.state.stats.inc(stat)
        debug(
            "FILTER",
            (
                f"listing_key={listing.listing_key} gift_id={listing.gift_id} price={listing.price} "
                f"market_value={listing.market_value} floor={listing.floor_price} ratio={listing.price_ratio} "
                f"discount={listing.discount_percent} result={reason}"
            ),
        )

    async def _enqueue(self, listing: Listing, profile) -> None:
        if self.state.scanner_paused:
            log("QUEUE", f"Paused: not enqueueing {listing.listing_key}")
            self.db.mark_status(listing.listing_key, STATUS_EXISTING, "paused")
            return
        if self.db.was_sent(listing.listing_key):
            self.state.stats.inc("skip_duplicate")
            return
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
        self.db.enqueue_listing(listing.listing_key, listing.gift_id, priority)
        self.state.stats.inc("queued")
        debug(
            "QUEUE",
            f"Added listing_key={listing.listing_key} gift_id={listing.gift_id} size={self.state.queue.qsize()} priority={priority}",
        )
        log("QUEUE", "Added")
