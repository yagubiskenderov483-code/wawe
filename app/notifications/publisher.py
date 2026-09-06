from __future__ import annotations

import asyncio
import time
from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.tl.functions.payments import GetUniqueStarGiftRequest

from app.config import Settings, resolve_publish_targets
from app.marketplace.filters import calculate_score, check_blacklist, classify_filter_stat, should_publish
from app.marketplace.market import MarketEstimator
from app.marketplace.models import STATUS_ERROR, STATUS_NEW, STATUS_SKIPPED, Listing, Profile, QueueItem, utc_now_iso
from app.marketplace.parser import listing_still_on_sale, parse_listing
from app.notifications.alerts import AlertManager
from app.profile.analyzer import ProfileAnalyzer
from app.storage.database import Database
from app.utils.logger import debug, log, redact_secrets
from app.utils.rate_limit import invoke_telegram, sleep_seconds
from app.utils.state import AppState


def _clean(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if text == "" or text.lower() in {"none", "null", "unknown"}:
        return None
    return text


def _owner_line(profile: Profile, listing: Listing) -> str:
    username = _clean(profile.username) or _clean(listing.owner_username)
    if username:
        return f"👤 Владелец: @{username.lstrip('@')}"
    return "👤 Владелец: скрыт/недоступен"


def _price_change_block(listing: Listing) -> str | None:
    if not listing.is_price_change:
        return None
    if listing.old_price is None or listing.price is None:
        return None
    if listing.old_price == listing.price:
        return None
    percent = listing.price_change_percent
    percent_text = ""
    if percent is not None:
        sign = "+" if percent > 0 else ""
        percent_text = f" ({sign}{percent:.1f}%)"
    return (
        "📈 Изменение цены:\n"
        f"{listing.old_price} → {listing.price} ⭐{percent_text}"
    )


def owner_profile_url(profile: Profile, listing: Listing) -> str | None:
    username = _clean(profile.username) or _clean(listing.owner_username)
    if username:
        handle = username.lstrip("@")
        if handle:
            return f"https://t.me/{handle}"
    user_id = profile.user_id if profile.user_id is not None else listing.owner_id
    if user_id is not None:
        return f"tg://user?id={int(user_id)}"
    return None


def listing_keyboard(listing: Listing, profile: Profile) -> InlineKeyboardMarkup | None:
    row: list[InlineKeyboardButton] = []
    nft_url = listing.nft_url
    if nft_url:
        row.append(InlineKeyboardButton(text="Открыть лот", url=nft_url))
    profile_url = owner_profile_url(profile, listing)
    if profile_url:
        row.append(InlineKeyboardButton(text="Написать", url=profile_url))
    if not row:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[row])


def format_listing_message(listing: Listing, profile: Profile) -> str:
    lines = ["🎁 НОВЫЙ ЛОТ", ""]
    if _clean(listing.gift_name):
        lines.append(f"💎 Gift: {listing.gift_name}")
    if listing.price is not None:
        lines.append(f"💰 Цена: {listing.price} ⭐")
        if listing.market_value is not None:
            lines.append(f"📊 Market: {listing.market_value} ⭐")
        if listing.discount_percent is not None:
            lines.append(f"🏷 Discount: {listing.discount_percent:.0f}%")
        if listing.is_price_change and listing.old_price is not None and listing.old_price != listing.price:
            delta_percent = listing.price_change_percent
            extra = ""
            if delta_percent is not None:
                extra = f" ({delta_percent:+.1f}%)"
            lines.append(f"📉 Изменение: {listing.old_price} → {listing.price} ⭐{extra}")
    if listing.gift_number is not None:
        lines.append(f"🔢 Number: #{listing.gift_number}")
    lines.append("")
    if _clean(listing.model):
        lines.append(f"🎨 Model: {listing.model}")
    if _clean(listing.symbol):
        lines.append(f"🔹 Symbol: {listing.symbol}")
    if _clean(listing.backdrop):
        lines.append(f"🖼 Backdrop: {listing.backdrop}")
    if any(_clean(value) for value in (listing.model, listing.symbol, listing.backdrop)):
        lines.append("")
    lines.append(_owner_line(profile, listing))
    nft_url = listing.nft_url
    if nft_url:
        lines.append(f"🔗 Лот: {nft_url}")
    seller_url = owner_profile_url(profile, listing)
    if seller_url:
        lines.append(f"✉️ Написать: {seller_url}")
    lines.append("")
    if _clean(profile.language) and profile.language != "unknown":
        lines.append(f"🌐 Язык профиля: {profile.language}")
    if profile.nft_count is not None:
        lines.append(f"🧩 NFT: {profile.nft_count}")
    if profile.free_messages is True:
        lines.append("💬 Free messages: true")
    elif profile.free_messages is False:
        lines.append("💬 Free messages: false")
    if profile.account_level is not None:
        lines.append(f"📊 Account level: {profile.account_level}")
    if _clean(listing.manual_gender) or _clean(profile.manual_gender):
        lines.append(f"♀♂ Gender tag: {_clean(listing.manual_gender) or _clean(profile.manual_gender)}")
    if listing.score is not None:
        lines.append(f"⭐ Score: {listing.score}")
    change = _price_change_block(listing)
    if change and "📉 Изменение:" not in "\n".join(lines):
        lines.append("")
        lines.append(change)
    detected = _clean(listing.first_seen_at)
    if detected:
        lines.append("")
        lines.append("🕐 Обнаружен:")
        lines.append(detected)
    text = "\n".join(lines).strip()
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    return text


class Publisher:
    def __init__(
        self,
        settings: Settings,
        state: AppState,
        db: Database,
        bot: Bot,
        client: TelegramClient,
        analyzer: ProfileAnalyzer,
        alerts: AlertManager,
        market: MarketEstimator | None = None,
        sleeper=None,
    ) -> None:
        self.settings = settings
        self.state = state
        self.db = db
        self.bot = bot
        self.client = client
        self.analyzer = analyzer
        self.alerts = alerts
        self.market = market or MarketEstimator(settings, db, client, None, state.stats)
        self._sleep = sleeper or sleep_seconds
        self._no_target_warned = False

    async def run(self) -> None:
        self.state.publisher_running = True
        backoff = 2
        log("PUBLISHER", "Publisher started")
        try:
            while not self.state.shutdown:
                try:
                    if self.state.scanner_paused:
                        await self._sleep(0.3)
                        continue
                    item = await self._get_item()
                    if item is None:
                        continue
                    started = time.monotonic()
                    published = await self._publish_item(item)
                    if published:
                        self.state.stats.record_publish(time.monotonic() - started, utc_now_iso())
                        await self._sleep(self.settings.publish_delay)
                    backoff = 2
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    self.state.stats.inc("errors")
                    self.state.stats.inc("send_errors")
                    log("ERROR", f"Publisher error: {type(error).__name__}: {redact_secrets(str(error))}")
                    await self.alerts.notify("Publisher", error)
                    await self._sleep(backoff)
                    backoff = min(self.settings.max_api_backoff, backoff * 2)
        finally:
            self.state.publisher_running = False
            log("PUBLISHER", "Publisher stopped")

    async def _get_item(self) -> QueueItem | None:
        if self.state.scanner_paused or self.state.queue.empty():
            await self._sleep(0.3)
            return None
        try:
            return await asyncio.wait_for(
                self.state.queue.get_diversified(
                    self.state.last_published_gift_id,
                    self.state.same_gift_streak,
                    self.settings.max_same_gift_streak,
                    self.settings.diversify_gifts,
                    last_owner_id=self.state.last_published_owner_id,
                    unique_owners=self.settings.unique_owners,
                    last_model=self.state.last_published_model,
                ),
                timeout=0.5,
            )
        except asyncio.TimeoutError:
            return None
        except Exception:
            return None

    def _targets(self) -> tuple[int, ...]:
        return resolve_publish_targets(
            (*self.settings.channel_ids, *self.db.get_publish_channels()),
            bot_id=self.settings.bot_user_id,
            admin_id=self.settings.admin_user_id,
        )

    async def _warn_no_target(self) -> None:
        if self._no_target_warned:
            return
        self._no_target_warned = True
        message = (
            "Marketplace listings are not published: TARGET_CHANNEL_ID is missing or invalid. "
            "Use a real channel/supergroup id (usually -100...), add the bot as admin with post rights. "
            "Do not use the bot id, ADMIN_USER_ID, or a private chat."
        )
        log("ERROR", message)
        notify = getattr(self.alerts, "notify", None)
        if notify is None:
            return
        result = notify("Publisher", message)
        if asyncio.iscoroutine(result):
            await result

    async def _publish_item(self, item: QueueItem) -> bool:
        listing = item.listing
        try:
            targets = self._targets()
            if not targets:
                await self._warn_no_target()
                self.db.mark_status(listing.listing_key, STATUS_NEW, "no_target_channel")
                log("PUBLISHER", "No TARGET_CHANNEL_ID; listing not sent")
                return False

            allowed, reason = await self._recheck(listing, item)
            if not allowed:
                log("FILTER", f"Recheck {reason} -> SKIP")
                self.db.mark_status(listing.listing_key, STATUS_SKIPPED, reason)
                stat = classify_filter_stat(reason)
                if stat:
                    self.state.stats.inc(stat)
                return False

            text = format_listing_message(listing, item.profile)
            keyboard = listing_keyboard(listing, item.profile)
            success = 0
            last_message_id = None
            sent_to: list[int] = []
            for chat_id in targets:
                if int(chat_id) >= 0:
                    log("ERROR", f"Refusing to publish listing to private/bot chat {chat_id}")
                    continue
                log("PUBLISHER", f"Sending to TARGET_CHANNEL_ID {chat_id}")
                debug(
                    "PUBLISHER",
                    (
                        f"listing_key={listing.listing_key} gift_id={listing.gift_id} price={listing.price} "
                        f"market_value={listing.market_value} floor={listing.floor_price} "
                        f"ratio={listing.price_ratio} discount={listing.discount_percent} "
                        f"target={chat_id}"
                    ),
                )
                try:
                    message = await self._send_listing(chat_id, text, keyboard)
                    success += 1
                    sent_to.append(chat_id)
                    last_message_id = getattr(message, "message_id", None)
                    log("PUBLISHER", "Sent successfully")
                except Exception as error:
                    self.state.stats.inc("send_errors")
                    log("ERROR", f"Channel failed: {chat_id} {type(error).__name__}: {redact_secrets(str(error))}")
                    await self.alerts.notify("Publisher", error)

            if success <= 0:
                self.db.mark_status(listing.listing_key, STATUS_ERROR, "send_failed")
                self.state.stats.inc("errors")
                return False

            target_text = ",".join(str(item) for item in sent_to)
            self.db.mark_sent(
                listing.listing_key,
                listing.price,
                listing.score,
                target_channel=target_text,
                message_id=last_message_id,
            )
            if listing.is_price_change:
                self.db.mark_price_change_notified(listing.listing_key, listing.old_price, listing.price)
            self.state.stats.inc("sent")
            self.state.stats.record_market(listing.price, listing.market_value, listing.discount_percent)
            self.state.note_published_gift(
                listing.gift_id,
                listing.seller_id or listing.owner_id,
                listing.model,
            )
            return True
        finally:
            self.state.queue.task_done()
            self.db.remove_queue(listing.listing_key)

    async def _send_listing(self, chat_id: int, text: str, reply_markup=None):
        while True:
            try:
                return await self.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    disable_web_page_preview=True,
                    reply_markup=reply_markup,
                )
            except TelegramRetryAfter as error:
                seconds = int(getattr(error, "retry_after", 0) or 0)
                self.state.stats.inc("floodwaits")
                log("ERROR", f"FloodWait {seconds}s before sending to {chat_id}")
                await self._sleep(seconds)
            except FloodWaitError as error:
                seconds = int(getattr(error, "seconds", 0) or 0)
                self.state.stats.inc("floodwaits")
                log("ERROR", f"FloodWait {seconds}s before sending to {chat_id}")
                await self._sleep(seconds)

    async def _recheck(self, listing: Listing, item: QueueItem) -> tuple[bool, str]:
        row = self.db.get_listing(listing.listing_key)
        if row is None:
            return False, "listing_missing"
        if self.db.was_sent(listing.listing_key):
            return False, "already_sent"
        if listing.price is None or listing.price < self.settings.min_price or listing.price > self.settings.max_price:
            return False, "price_out_of_range"

        original_price = listing.price
        if listing.slug:
            still = await self._still_available(listing)
            if still is False:
                return False, "listing_unavailable"
        if listing.price is not None and original_price is not None and listing.price != original_price:
            log("FILTER", f"PRICE_CHANGED {listing.listing_key}: {original_price} -> {listing.price}")
            listing.old_price = original_price
            listing.is_price_change = True
            self.db.record_price_change(listing.listing_key, original_price, listing.price)

        profile = await self.analyzer.get_profile(listing.owner_id, force_refresh=True)
        listing.owner_username = profile.username
        listing.manual_gender = profile.manual_gender
        listing.manual_nationality = profile.manual_nationality
        item.profile = profile

        blocked = check_blacklist(profile, self.settings, listing)
        if not blocked.passed:
            return False, blocked.reason

        if self.settings.unique_owners:
            seller_id = listing.seller_id or listing.owner_id
            if self.db.seller_was_published(seller_id):
                return False, "owner_already_published"

        await self.market.estimate(listing, force_refresh=False)
        result = should_publish(listing, profile, self.settings)
        if not result.passed:
            if result.reason in {"market_ratio_too_high", "market_unavailable"}:
                log("MARKET", f"Recheck failed: {result.reason}")
            elif "profile" in result.reason or result.reason.endswith("mismatch"):
                log("FILTER", "Profile changed -> SKIP")
            return False, result.reason
        total, _, profile_score = calculate_score(listing, profile, self.settings)
        listing.score = total
        listing.profile_score = profile_score
        debug(
            "PUBLISHER",
            (
                f"recheck PASS listing_key={listing.listing_key} gift_id={listing.gift_id} "
                f"price={listing.price} market_value={listing.market_value} ratio={listing.price_ratio} "
                f"gender={listing.manual_gender} nft={profile.nft_count} language={profile.language} "
                f"level={profile.account_level} score={listing.score}"
            ),
        )
        return True, "ok"

    async def _still_available(self, listing: Listing) -> bool | None:
        slug = listing.slug
        if not slug:
            return None

        async def _call():
            return await self.client(GetUniqueStarGiftRequest(slug=slug))

        try:
            result = await invoke_telegram(_call, stats=self.state.stats, max_backoff=self.settings.max_api_backoff)
        except Exception:
            debug("PUBLISHER", "Availability check failed")
            return None
        gift = getattr(result, "gift", result)
        parsed = parse_listing(gift)
        if parsed and parsed.price is not None:
            listing.price = parsed.price
        return listing_still_on_sale(gift)
