from __future__ import annotations

import re
from collections.abc import Awaitable, Callable

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramForbiddenError, TelegramUnauthorizedError
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message
from telethon import TelegramClient

from app.config import Settings
from app.marketplace.models import utc_now_iso
from app.profile.analyzer import ProfileAnalyzer
from app.storage.database import Database
from app.telegram.auth_flow import (
    BotTelethonAuth,
    describe_auth_error,
    normalize_login_code,
    normalize_phone,
)
from app.utils.logger import log
from app.utils.state import AppState

router = Router()

_TAG_RE = re.compile(r"(gender|nationality|tag)=([^\s]+)", re.IGNORECASE)


class LoginStates(StatesGroup):
    waiting_phone = State()
    waiting_code = State()
    waiting_password = State()


class BotContext:
    settings: Settings
    state: AppState
    db: Database
    analyzer: ProfileAnalyzer | None
    client: TelegramClient | None
    auth: BotTelethonAuth | None
    on_authorized: Callable[[], Awaitable[None]] | None


ctx = BotContext()
ctx.client = None
ctx.auth = None
ctx.on_authorized = None
ctx.analyzer = None


def setup_bot(
    settings: Settings,
    state: AppState,
    db: Database,
    analyzer: ProfileAnalyzer | None = None,
    client: TelegramClient | None = None,
    on_authorized: Callable[[], Awaitable[None]] | None = None,
) -> tuple[Bot, Dispatcher]:
    ctx.settings = settings
    ctx.state = state
    ctx.db = db
    ctx.analyzer = analyzer
    ctx.client = client
    ctx.auth = BotTelethonAuth(client) if client is not None else None
    ctx.on_authorized = on_authorized
    bot = Bot(token=settings.bot_token)
    dp = Dispatcher(storage=MemoryStorage())
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


def _is_private(message: Message) -> bool:
    chat_type = getattr(message.chat, "type", None)
    return chat_type in {ChatType.PRIVATE, "private"}


def _is_operator(message: Message) -> bool:
    admin = ctx.settings.admin_user_id
    if admin is None:
        return True
    user = message.from_user
    return user is not None and int(user.id) == int(admin)


async def _ensure_login_allowed(message: Message) -> bool:
    if not _is_private(message):
        await message.answer("Вход доступен только в личке с ботом.")
        return False
    if not _is_operator(message):
        await message.answer("Недостаточно прав для /login.")
        return False
    if ctx.client is None or ctx.auth is None:
        await message.answer("User client ещё не подключен. Перезапустите tracker.")
        return False
    return True


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    if ctx.auth is not None:
        ctx.auth.clear()
    await message.answer("Вход отменён. Чтобы начать снова, отправьте /login")


@router.message(Command("login"))
async def cmd_login(message: Message, state: FSMContext, command: CommandObject) -> None:
    await _register_operator_chat(message)
    if not await _ensure_login_allowed(message):
        return
    if await ctx.client.is_user_authorized():
        ctx.state.user_authorized = True
        await message.answer("User-сессия уже авторизована. Сканер может работать.")
        if ctx.on_authorized is not None:
            await ctx.on_authorized()
        return
    phone = normalize_phone(command.args or "")
    if phone:
        await _send_login_code(message, state, phone)
        return
    await state.set_state(LoginStates.waiting_phone)
    await message.answer(
        "Вход в Telethon через бота.\n\n"
        "Отправьте номер телефона одним сообщением, например:\n"
        "+79001234567"
    )


async def _send_login_code(message: Message, state: FSMContext, phone: str) -> None:
    try:
        await ctx.auth.request_code(phone)
    except Exception as error:
        await state.clear()
        ctx.auth.clear()
        await message.answer(describe_auth_error(error))
        return
    await state.set_state(LoginStates.waiting_code)
    await message.answer(
        "Код отправлен в Telegram на этот номер.\n"
        "Пришлите код одним сообщением.\n\n"
        "Код в чат с ботом вводите только вы. Если передумали: /cancel"
    )


@router.message(StateFilter(LoginStates.waiting_phone), F.text)
async def login_phone_received(message: Message, state: FSMContext) -> None:
    if not await _ensure_login_allowed(message):
        return
    text = (message.text or "").strip()
    if text.startswith("/"):
        return
    phone = normalize_phone(text)
    if phone is None:
        await message.answer("Не понял номер. Пример: +79001234567")
        return
    await _send_login_code(message, state, phone)


@router.message(StateFilter(LoginStates.waiting_code), F.text)
async def login_code_received(message: Message, state: FSMContext) -> None:
    if not await _ensure_login_allowed(message):
        return
    text = (message.text or "").strip()
    if text.startswith("/login"):
        return
    if text.startswith("/cancel"):
        return
    raw = text[6:].strip() if text.lower().startswith("/code ") else text
    code = normalize_login_code(raw)
    if code is None:
        await message.answer("Пришлите код цифрами, например 12345")
        return
    try:
        result = await ctx.auth.sign_in_code(code)
    except Exception as error:
        await message.answer(describe_auth_error(error))
        if isinstance(error, Exception) and "Expired" in type(error).__name__:
            await state.clear()
        return
    if result == "2fa":
        await state.set_state(LoginStates.waiting_password)
        await message.answer("Включён облачный пароль 2FA. Отправьте пароль одним сообщением.")
        return
    await state.clear()
    ctx.state.user_authorized = True
    if ctx.on_authorized is not None:
        await ctx.on_authorized()
    await message.answer("Вход выполнен. Scanner запущен.")


@router.message(StateFilter(LoginStates.waiting_password), F.text)
async def login_password_received(message: Message, state: FSMContext) -> None:
    if not await _ensure_login_allowed(message):
        return
    password = (message.text or "").strip()
    if password.startswith("/"):
        return
    if not password:
        await message.answer("Пароль пустой. Отправьте ещё раз или /cancel")
        return
    try:
        await ctx.auth.sign_in_password(password)
    except Exception as error:
        await message.answer(describe_auth_error(error))
        return
    await state.clear()
    ctx.state.user_authorized = True
    if ctx.on_authorized is not None:
        await ctx.on_authorized()
    await message.answer("Вход выполнен. Scanner запущен.")


async def _register_operator_chat(message: Message) -> None:
    if not _is_private(message) or not _is_operator(message):
        return
    chat_id = getattr(message.chat, "id", None)
    if chat_id is None:
        return
    bot_id = None
    token = ctx.settings.bot_token or ""
    prefix = token.split(":", 1)[0]
    if prefix.isdigit():
        bot_id = int(prefix)
    if bot_id is not None and int(chat_id) == bot_id:
        return
    ctx.db.add_notify_chat(int(chat_id))
    log("BOT", "Operator chat registered for listing delivery")


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await _register_operator_chat(message)
    username = ctx.settings.bot_username.lstrip("@")
    authorized = ctx.state.user_authorized
    targets = ctx.db.get_notify_chats()
    login_hint = (
        "User-сессия уже активна."
        if authorized
        else (
            "Сначала вход в Telethon через бота:\n"
            "1. /login\n"
            "2. номер телефона\n"
            "3. код из Telegram\n"
            "4. пароль 2FA, если спросит"
        )
    )
    await message.answer(
        "Marketplace Tracker запущен.\n"
        f"Бот: @{username}\n"
        f"Лоты будут приходить сюда, в этот чат.\n"
        f"Получатели: {', '.join(str(item) for item in targets) or 'этот чат'}\n"
        "Пауза между лотами: 4 секунды.\n\n"
        f"{login_hint}"
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
    await _register_operator_chat(message)
    stats = ctx.state.stats
    targets = ctx.db.get_notify_chats()
    text = (
        f"Scanner: {ctx.state.scanner_status()}\n"
        f"Publisher: {ctx.state.publisher_status()}\n"
        f"User session: {'AUTHORIZED' if ctx.state.user_authorized else 'WAITING_LOGIN'}\n"
        f"Deliver to: {', '.join(str(item) for item in targets) or '-'}\n"
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
