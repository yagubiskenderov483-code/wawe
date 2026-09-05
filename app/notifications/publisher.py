from __future__ import annotations

import time
from aiogram import Bot
from telethon import TelegramClient
from telethon.tl.functions.payments import GetUniqueStarGiftRequest

from app.config import Settings, resolve_publish_chat_ids
from app.marketplace.filters import calculate_score, classify_filter_stat, should_publish
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


def format_listing_message(listing: Listing, profile: Profile) -> str:
    lines = ["🎁 НОВЫЙ ЛОТ", ""]
    if _clean(listing.gift_name):
        lines.append(f"💎 Gift: {listing.gift_name}")
    if listing.price is not None:
        lines.append(f"💰 Цена: {listing.price} ⭐")
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
    if listing.score is not None:
        lines.append(f"⭐ Score: {listing.score}")
    change = _price_change_block(listing)
    if change and "📉 Изменение:" not in "\n".join(lines):
        lines.append("")
        lines.append(change)
    url = listing.nft_url
    if url:
        lines.append("")
        lines.append("🔗 Открыть подарок:")
        lines.append(url)
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
    ) -> None:
        self.settings = settings
        self.state = state
        self.db = db
        self.bot = bot
        self.client = client
        self.analyzer = analyzer
        self.alerts = alerts

    async def run(self) -> None:
        self.state.publisher_running = True
        backoff = 2
        log("SEND", "Publisher started")
        try:
            while not self.state.shutdown:
                try:
                    item = await self._get_item()
                    if item is None:
                        continue
                    started = time.monotonic()
                    published = await self._publish_item(item)
                    if published:
                        self.state.stats.record_publish(time.monotonic() - started, utc_now_iso())
                    backoff = 2
                except Exception as error:
                    self.state.stats.inc("errors")
                    self.state.stats.inc("send_errors")
                    log("ERROR", f"Publisher error: {type(error).__name__}: {redact_secrets(str(error))}")
                    await self.alerts.notify("Publisher", error)
                    await sleep_seconds(backoff)
                    backoff = min(self.settings.max_api_backoff, backoff * 2)
        finally:
            self.state.publisher_running = False
            log("SEND", "Publisher stopped")

    async def _get_item(self) -> QueueItem | None:
        if self.state.scanner_paused and self.state.queue.empty():
            await sleep_seconds(0.3)
            return None
        try:
            return await self.state.queue.get()
        finally:
            pass

    def _bot_id(self) -> int | None:
        token = self.settings.bot_token or ""
        prefix = token.split(":", 1)[0]
        if prefix.isdigit():
            return int(prefix)
        return None

    def _targets(self) -> tuple[int, ...]:
        return resolve_publish_chat_ids(
            self.settings.channel_ids,
            self.db.get_notify_chats(),
            self._bot_id(),
        )

    async def _publish_item(self, item: QueueItem) -> bool:
        listing = item.listing
        try:
            allowed, reason = await self._recheck(listing)
            if not allowed:
                log("FILTER", f"Profile changed -> SKIP" if "profile" in reason or reason.endswith("mismatch") else f"Recheck {reason} -> SKIP")
                self.db.mark_status(listing.listing_key, STATUS_SKIPPED, reason)
                stat = classify_filter_stat(reason)
                if stat:
                    self.state.stats.inc(stat)
                return False

            targets = self._targets()
            if not targets:
                log("SEND", "No publish chat yet. Send /start to the bot in private chat")
                queued = await self.state.queue.put(item)
                if not queued:
                    self.db.mark_status(listing.listing_key, STATUS_NEW, "queue_full")
                await sleep_seconds(2)
                return False

            text = format_listing_message(listing, item.profile)
            success = 0
            for chat_id in targets:
                log("SEND", "Отправка в канал")
                try:
                    await self.bot.send_message(chat_id=chat_id, text=text, disable_web_page_preview=False)
                    success += 1
                    log("SEND", "Успешно")
                    await sleep_seconds(self.settings.publish_delay)
                except Exception as error:
                    self.state.stats.inc("send_errors")
                    log("SEND", f"Channel failed: {chat_id} {type(error).__name__}: {redact_secrets(str(error))}")
                    await self.alerts.notify("Publisher", error)

            if success <= 0:
                self.db.mark_status(listing.listing_key, STATUS_ERROR, "send_failed")
                self.state.stats.inc("errors")
                return False

            self.db.mark_sent(listing.listing_key, listing.price, listing.score)
            if listing.is_price_change:
                self.db.mark_price_change_notified(listing.listing_key, listing.old_price, listing.price)
            self.state.stats.inc("sent")
            return True
        finally:
            self.state.queue.task_done()

    async def _recheck(self, listing: Listing) -> tuple[bool, str]:
        row = self.db.get_listing(listing.listing_key)
        if row is None:
            return False, "listing_missing"
        if row.get("sent_at") and not listing.is_price_change:
            return False, "already_sent"
        if listing.is_price_change and row.get("last_notified_price") == listing.price:
            return False, "duplicate_price_change"
        if listing.price is None or listing.price < self.settings.min_price or listing.price > self.settings.max_price:
            return False, "price_out_of_range"

        if listing.slug:
            still = await self._still_available(listing)
            if still is False:
                return False, "listing_unavailable"

        profile = await self.analyzer.get_profile(listing.owner_id, force_refresh=True)
        listing.owner_username = profile.username
        result = should_publish(listing, profile, self.settings)
        if not result.passed:
            log("FILTER", "Profile changed -> SKIP")
            return False, result.reason
        total, _, _ = calculate_score(listing, profile, self.settings)
        listing.score = total
        return True, "ok"

    async def _still_available(self, listing: Listing) -> bool | None:
        slug = listing.slug
        if not slug:
            return None

        async def _call():
            return await self.client(GetUniqueStarGiftRequest(slug=slug))

        try:
            result = await invoke_telegram(_call, stats=self.state.stats, max_backoff=self.settings.max_api_backoff)
        except Exception as error:
            debug("SEND", f"Availability check failed: {type(error).__name__}")
            return None
        gift = getattr(result, "gift", result)
        parsed = parse_listing(gift)
        if parsed and parsed.price is not None:
            listing.price = parsed.price
        return listing_still_on_sale(gift)
