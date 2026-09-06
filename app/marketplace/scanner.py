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
    RANK_PRIMARY,
    RANK_SECONDARY,
    RANK_SKIP,
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
    classify_collection,
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
    Profile,
    QueueItem,
    utc_now_iso,
)
from app.marketplace.parser import parse_listing
from app.notifications.alerts import AlertManager
from app.profile.analyzer import ProfileAnalyzer, detect_profile_language
from app.profile.gender import infer_gender
from app.storage.database import Database
from app.utils.logger import debug, log, redact_secrets
from app.utils.rate_limit import ApiLimiter, SessionInvalidError, invoke_telegram, next_backoff, sleep_seconds
from app.utils.state import AppState


def _norm_text(value: Optional[str]) -> str:
    return (value or "").strip().lower()


def _as_optional_int(value: Any) -> int | None:
    if value is None or value is False:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


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
        self._gift_catalog: list[tuple[int, int | None]] = []
        self._gift_hash = 0
        self._users: dict[int, User] = {}
        self._secondary_cursor = 0
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
        catalog = await self._load_catalog()
        snapshot = self.is_snapshot()
        primary, secondary, skipped = self._partition_catalog(catalog)
        self.state.catalog_primary = len(primary)
        self.state.catalog_secondary = len(secondary)
        self.state.catalog_skipped = len(skipped)
        if snapshot:
            to_scan = [gift_id for gift_id, _min in primary + secondary]
            log(
                "SNAPSHOT",
                f"Remembering current market ({len(to_scan)} gift types, skip={len(skipped)} out of band). "
                "Nothing is published yet.",
            )
        else:
            watched = self._rotate_secondary([gift_id for gift_id, _min in secondary], self.settings.secondary_gift_types_per_scan)
            to_scan = [gift_id for gift_id, _min in primary] + watched
            log(
                "SCANNER",
                f"Catalog primary={len(primary)} watch={len(watched)}/{len(secondary)} skip={len(skipped)}",
            )
        debug("SCANNER", f"Gift types this cycle: {len(to_scan)}")
        total = 0
        for index, gift_id in enumerate(to_scan, start=1):
            if self.state.shutdown:
                break
            total += await self._scan_gift(gift_id, snapshot=snapshot)
            if index == 1 or index % 10 == 0 or index == len(to_scan):
                tag = "SNAPSHOT" if snapshot else "SCANNER"
                log(tag, f"Progress {index}/{len(to_scan)} types, seen={total}")
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
        catalog = await self._load_catalog()
        return [gift_id for gift_id, _min in catalog]

    async def _load_catalog(self) -> list[tuple[int, int | None]]:
        async def _call():
            async with self.limiter:
                return await self.client(GetStarGiftsRequest(hash=self._gift_hash))

        result = await invoke_telegram(_call, stats=self.state.stats, max_backoff=self.settings.max_api_backoff)
        if isinstance(result, StarGiftsNotModified):
            debug("SCANNER", "Star gifts not modified")
            return list(self._gift_catalog or [(gift_id, None) for gift_id in self._gift_ids])
        if not isinstance(result, StarGifts):
            gifts = getattr(result, "gifts", None) or []
        else:
            gifts = result.gifts
            self._gift_hash = getattr(result, "hash", 0) or 0
        catalog: list[tuple[int, int | None]] = []
        all_ids: list[tuple[int, int | None]] = []
        for gift in gifts:
            if not isinstance(gift, StarGift) and type(gift).__name__ != "StarGift":
                continue
            gift_id = getattr(gift, "id", None)
            if gift_id is None:
                continue
            min_stars = _as_optional_int(getattr(gift, "resell_min_stars", None))
            all_ids.append((int(gift_id), min_stars))
            resale = getattr(gift, "availability_resale", None)
            if resale or min_stars:
                catalog.append((int(gift_id), min_stars))
        chosen = catalog or all_ids
        if chosen:
            self._gift_catalog = chosen
            self._gift_ids = [gift_id for gift_id, _min in chosen]
        debug("SCANNER", f"Resale gift_ids={len(self._gift_ids)}")
        return list(self._gift_catalog)

    def _partition_catalog(
        self,
        catalog: list[tuple[int, int | None]],
    ) -> tuple[list[tuple[int, int | None]], list[tuple[int, int | None]], list[tuple[int, int | None]]]:
        primary: list[tuple[int, int | None]] = []
        secondary: list[tuple[int, int | None]] = []
        skipped: list[tuple[int, int | None]] = []
        for gift_id, min_stars in catalog:
            cached = self.db.get_gift_market_value(gift_id)
            rank = classify_collection(min_stars, self.settings, cached)
            item = (gift_id, min_stars)
            if rank == RANK_SKIP:
                skipped.append(item)
            elif rank == RANK_PRIMARY:
                primary.append(item)
            elif rank == RANK_SECONDARY:
                secondary.append(item)
            else:
                secondary.append(item)
        return primary, secondary, skipped

    def _rotate_secondary(self, gift_ids: list[int], limit: int) -> list[int]:
        if not gift_ids or limit <= 0:
            return []
        if limit >= len(gift_ids):
            return list(gift_ids)
        start = self._secondary_cursor % len(gift_ids)
        self._secondary_cursor = (start + limit) % len(gift_ids)
        doubled = gift_ids + gift_ids
        return doubled[start : start + limit]

    async def _scan_gift(self, gift_id: int, snapshot: bool = False) -> int:
        if (
            not snapshot
            and not self.is_snapshot()
            and not self.db.gift_was_scanned(gift_id)
            and not self.db.gift_has_listings(gift_id)
        ):
            log("SNAPSHOT", f"New gift type {gift_id}: remember current lots, do not publish")
            snapshot = True
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
            observe = getattr(self.market, "observe_page", None)
            if callable(observe):
                observe(gift_id, gifts)
            if snapshot:
                for gift in gifts:
                    is_new_signal = await self._process_gift(gift, snapshot=True)
                    if is_new_signal is False:
                        known_streak += 1
                    else:
                        known_streak = 0
                hit_known = known_streak > 0
            else:
                hit_known = await self._process_live_page(gifts)
                if hit_known:
                    known_streak += 1
            next_offset = getattr(result, "next_offset", None) or ""
            self.db.set_scanner_offset(gift_id, next_offset)
            pages += 1
            if not next_offset:
                break
            if not snapshot and hit_known:
                debug("SCANNER", f"Stopping pagination for gift_id={gift_id}: reached known listings")
                break
            offset = next_offset
        return found

    async def _process_live_page(self, gifts: list[Any]) -> bool:
        """Evaluate unseen listings before the first known snapshot row.

        A page with no known ids is treated as a full candidate page so a cheap
        gift at the top cannot hide a 5-25k listing further down.
        """
        rows: list[tuple[Any, Listing, Optional[dict[str, Any]]]] = []
        first_known: int | None = None
        for gift in gifts:
            listing = parse_listing(gift)
            if listing is None:
                continue
            existing = self.db.get_listing(listing.listing_key)
            if self._is_known_barrier(existing) and first_known is None:
                first_known = len(rows)
            rows.append((gift, listing, existing))
        publish_until = first_known if first_known is not None else len(rows)
        hit_known = first_known is not None
        for index, (gift, listing, existing) in enumerate(rows):
            if self._is_known_barrier(existing):
                continue
            if index < publish_until:
                await self._process_gift(gift)
            else:
                self._remember_existing(listing, reason="behind_known")
        return hit_known

    def _is_known_barrier(self, existing: Optional[dict[str, Any]]) -> bool:
        if existing is None:
            return False
        if existing.get("skip_reason") == "live_overflow":
            return False
        return True

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

    async def _process_gift(self, gift: Any, snapshot: bool | None = None) -> Optional[bool]:
        listing = parse_listing(gift)
        if listing is None:
            return None
        existing = self.db.get_listing(listing.listing_key)
        if snapshot if snapshot is not None else self.is_snapshot():
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
        return self._remember_existing(listing, reason="initial_snapshot")

    def _remember_existing(self, listing: Listing, reason: str = "already_on_market") -> bool:
        listing.is_new = False
        listing.is_initial_snapshot = reason == "initial_snapshot"
        listing.status = STATUS_EXISTING
        listing.skip_reason = reason
        inserted = self.db.insert_listing(listing)
        if not inserted:
            self.state.stats.inc("duplicates")
            return False
        self.db.mark_existing(
            listing.listing_key,
            skip_reason=reason,
            is_initial_snapshot=reason == "initial_snapshot",
        )
        self.state.stats.inc("existing")
        log("SNAPSHOT" if reason == "initial_snapshot" else "LIVE", f"Existing listing -> SKIP {listing.listing_key}")
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
        preview = self._preview_owner(listing, owner)
        if _norm_text(preview.manual_tag) == "ignore":
            self._skip(listing, "manual_tag_ignore")
            return True
        gender_filter = self.settings.normalized_gender_filter()
        preview_gender = _norm_text(preview.manual_gender)
        if gender_filter and preview_gender and preview_gender != gender_filter:
            log("FILTER", f"Manual gender: {preview_gender} -> SKIP")
            self._skip(listing, "manual_gender_mismatch")
            return True

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

    def _preview_owner(self, listing: Listing, user: Any) -> Profile:
        profile = Profile(user_id=listing.owner_id)
        if user is not None:
            profile.user_id = getattr(user, "id", profile.user_id) or profile.user_id
            profile.username = getattr(user, "username", None) or None
            profile.first_name = getattr(user, "first_name", None) or None
            profile.last_name = getattr(user, "last_name", None) or None
            if not profile.username:
                extras = getattr(user, "usernames", None) or []
                for item in extras:
                    name = getattr(item, "username", None)
                    if name:
                        profile.username = name
                        break
        prefs: dict[str, Any] = {}
        if profile.user_id is not None:
            cached = self.db.get_profile(int(profile.user_id))
            if cached is not None:
                profile.first_name = profile.first_name or cached.first_name
                profile.last_name = profile.last_name or cached.last_name
                profile.username = profile.username or cached.username
                profile.bio = cached.bio
                profile.language = cached.language
            prefs = self.db.get_manual_profile_preferences(int(profile.user_id))
        profile.manual_gender = infer_gender(
            profile.first_name,
            prefs.get("manual_gender"),
            last_name=profile.last_name,
        )
        profile.manual_nationality = prefs.get("manual_nationality")
        profile.manual_tag = prefs.get("manual_tag")
        if not profile.language:
            language, score = detect_profile_language(profile)
            profile.language = language
            profile.language_score = score
        return profile

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
        if existing.get("status") == STATUS_EXISTING and existing.get("skip_reason") == "live_overflow":
            return "retry"
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
