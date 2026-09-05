from __future__ import annotations

import re

from aiogram import Bot, Dispatcher, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from aiogram.exceptions import TelegramForbiddenError, TelegramUnauthorizedError

from app.config import Settings
from app.marketplace.models import utc_now_iso
from app.profile.analyzer import ProfileAnalyzer
from app.storage.database import Database
from app.utils.logger import log
from app.utils.state import AppState

router = Router()

_TAG_RE = re.compile(r"(gender|nationality|tag)=([^\s]+)", re.IGNORECASE)


class BotContext:
    settings: Settings
    state: AppState
    db: Database
    analyzer: ProfileAnalyzer | None


ctx = BotContext()


def setup_bot(settings: Settings, state: AppState, db: Database, analyzer: ProfileAnalyzer | None = None) -> tuple[Bot, Dispatcher]:
    ctx.settings = settings
    ctx.state = state
    ctx.db = db
    ctx.analyzer = analyzer
    bot = Bot(token=settings.bot_token)
    dp = Dispatcher()
    dp.include_router(router)
    return bot, dp


async def verify_target_channels(bot: Bot, settings: Settings) -> None:
    if not settings.channel_ids:
        raise RuntimeError("[ERROR] TARGET_CHANNEL_ID is not configured")
    me = await bot.me()
    bot_id = int(me.id)
    failures: list[str] = []
    usable = 0
    for chat_id in settings.channel_ids:
        if int(chat_id) == bot_id:
            log("BOT", f"Target chat {chat_id} matches bot id @{settings.bot_username}; posting to this chat_id")
            usable += 1
            continue
        try:
            chat = await bot.get_chat(chat_id)
            chat_type = getattr(chat, "type", None)
            if chat_type in {ChatType.CHANNEL, ChatType.SUPERGROUP, "channel", "supergroup"}:
                member = await bot.get_chat_member(chat_id, bot_id)
                can_post = bool(getattr(member, "can_post_messages", False))
                status = getattr(member, "status", None)
                if status in {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR, "administrator", "creator"}:
                    can_post = True if getattr(member, "can_post_messages", True) else can_post
                    if status == ChatMemberStatus.CREATOR or status == "creator":
                        can_post = True
                if not can_post:
                    failures.append(f"{chat_id}: bot cannot post messages")
                    continue
            usable += 1
            log("BOT", f"Target chat ready: {chat_id} ({chat_type})")
        except (TelegramForbiddenError, TelegramUnauthorizedError):
            failures.append(f"{chat_id}: bot cannot post messages")
        except Exception as error:
            failures.append(f"{chat_id}: {type(error).__name__}")
    if usable <= 0:
        detail = "; ".join(failures) or "unknown reason"
        raise RuntimeError(f"[ERROR] Bot cannot post messages to target channel ({detail})")
    for item in failures:
        log("ERROR", f"Bot cannot post messages to target channel: {item}")


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    username = ctx.settings.bot_username.lstrip("@")
    await message.answer(
        "Marketplace Tracker запущен и готов к работе.\n"
        f"Бот: @{username}\n"
        f"Публикация: chat_id {ctx.settings.channel_ids[0] if ctx.settings.channel_ids else '-'}\n"
        "Пауза между лотами: 4 секунды."
    )


@router.message(Command("pause"))
async def cmd_pause(message: Message) -> None:
    ctx.state.pause()
    await message.answer("Scanner: PAUSED\nPublisher дошлёт очередь и будет ждать.")


@router.message(Command("resume"))
async def cmd_resume(message: Message) -> None:
    ctx.state.resume()
    await message.answer("Scanner: RUNNING")


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    stats = ctx.state.stats
    text = (
        f"Scanner: {ctx.state.scanner_status()}\n"
        f"Publisher: {ctx.state.publisher_status()}\n"
        f"Queue: {ctx.state.queue.qsize()}\n"
        f"Last scan: {stats.last_scan or '-'}\n"
        f"Last publish: {stats.last_publish or '-'}\n"
        f"Scanned: {stats.get('total_scanned')}\n"
        f"New: {stats.get('new_listings')}\n"
        f"Filtered: {stats.get('filtered')}\n"
        f"Sent: {stats.get('sent')}\n"
        f"Errors: {stats.get('errors')}"
    )
    await message.answer(text)


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    snap = ctx.state.stats.snapshot()
    lines = ["Статистика текущего запуска:", ""]
    order = [
        "total_scanned",
        "new_listings",
        "duplicates",
        "price_filtered",
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
        "send_errors",
        "price_changes",
        "floodwaits",
        "average_scan_time",
        "average_publish_time",
    ]
    for key in order:
        value = snap.get(key)
        if value is None:
            value = "-"
        lines.append(f"{key}: {value}")
    await message.answer("\n".join(lines))


@router.message(Command("tag"))
async def cmd_tag(message: Message, command: CommandObject) -> None:
    args = (command.args or "").strip().split()
    if not args:
        await message.answer("Использование: /tag USER_ID gender=female nationality=ru tag=trusted")
        return
    try:
        user_id = int(args[0].lstrip("@"))
    except ValueError:
        await message.answer("USER_ID должен быть числом.")
        return
    raw = " ".join(args[1:])
    updates = {key.lower(): value.lower() for key, value in _TAG_RE.findall(raw)}
    if not updates:
        await message.answer("Укажите gender=, nationality= или tag=")
        return
    prefs = ctx.db.set_manual_profile_preference(
        user_id,
        gender=updates.get("gender"),
        nationality=updates.get("nationality"),
        tag=updates.get("tag"),
    )
    await message.answer(
        "Сохранены ручные метки:\n"
        f"user_id: {user_id}\n"
        f"gender: {prefs.get('manual_gender') or '-'}\n"
        f"nationality: {prefs.get('manual_nationality') or '-'}\n"
        f"tag: {prefs.get('manual_tag') or '-'}\n"
        f"updated_at: {prefs.get('updated_at') or utc_now_iso()}"
    )


@router.message(Command("untag"))
async def cmd_untag(message: Message, command: CommandObject) -> None:
    args = (command.args or "").strip()
    if not args:
        await message.answer("Использование: /untag USER_ID")
        return
    try:
        user_id = int(args.split()[0])
    except ValueError:
        await message.answer("USER_ID должен быть числом.")
        return
    ctx.db.delete_manual_profile_preference(user_id)
    await message.answer(f"Ручные метки для {user_id} удалены.")


@router.message(Command("profile"))
async def cmd_profile(message: Message, command: CommandObject) -> None:
    args = (command.args or "").strip()
    if not args:
        await message.answer("Использование: /profile USER_ID")
        return
    try:
        user_id = int(args.split()[0])
    except ValueError:
        await message.answer("USER_ID должен быть числом.")
        return
    profile = ctx.db.get_profile(user_id)
    prefs = ctx.db.get_manual_profile_preferences(user_id)
    if profile is None and ctx.analyzer is not None:
        try:
            profile = await ctx.analyzer.get_profile(user_id, force_refresh=True)
        except Exception:
            profile = None
    if profile is None:
        await message.answer(
            f"Профиль {user_id} ещё не сохранялся.\n"
            f"gender: {prefs.get('manual_gender') or '-'}\n"
            f"nationality: {prefs.get('manual_nationality') or '-'}\n"
            f"tag: {prefs.get('manual_tag') or '-'}"
        )
        return
    lines = [
        f"user_id: {profile.user_id}",
        f"username: @{profile.username}" if profile.username else "username: -",
        f"first_name: {profile.first_name or '-'}",
        f"last_name: {profile.last_name or '-'}",
        f"bio: {profile.bio or '-'}",
        f"language: {profile.language or '-'}",
        f"nft_count: {profile.nft_count if profile.nft_count is not None else '-'}",
        f"free_messages: {profile.free_messages if profile.free_messages is not None else '-'}",
        f"account_level: {profile.account_level if profile.account_level is not None else '-'}",
        f"public_channel: {profile.public_channel if profile.public_channel is not None else '-'}",
        f"public_gifts: {profile.public_gifts if profile.public_gifts is not None else '-'}",
        f"manual_gender: {prefs.get('manual_gender') or '-'}",
        f"manual_nationality: {prefs.get('manual_nationality') or '-'}",
        f"manual_tag: {prefs.get('manual_tag') or '-'}",
    ]
    await message.answer("\n".join(lines))
