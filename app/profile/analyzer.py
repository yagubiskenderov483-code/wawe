from __future__ import annotations

import re
from typing import Any, Optional

from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.tl.functions.payments import GetSavedStarGiftsRequest
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.types import InputPeerUser, InputUser, StarGiftUnique, User, UserEmpty

from app.config import Settings
from app.marketplace.models import Profile, utc_now_iso
from app.profile.gender import infer_gender
from app.storage.database import Database
from app.utils.logger import debug, log
from app.utils.rate_limit import ApiLimiter, invoke_telegram
from app.utils.stats import RuntimeStats

CYRILLIC_RE = re.compile(r"[А-Яа-яЁёІіЇїЄєҐґ]")
LATIN_RE = re.compile(r"[A-Za-z]")


def detect_profile_language(profile: Profile | dict | str | None) -> tuple[str, float]:
    if isinstance(profile, Profile):
        text = " ".join(part for part in (profile.first_name, profile.last_name, profile.bio) if part)
    elif isinstance(profile, dict):
        text = " ".join(str(profile.get(key) or "") for key in ("first_name", "last_name", "bio", "about", "description"))
    else:
        text = str(profile or "")
    return detect_text_language(text)


def detect_text_language(text: str | None) -> tuple[str, float]:
    source = text or ""
    cyrillic = len(CYRILLIC_RE.findall(source))
    latin = len(LATIN_RE.findall(source))
    total = cyrillic + latin
    if total == 0:
        return "unknown", 0.0
    if cyrillic and not latin:
        return "ru", 1.0
    if latin and not cyrillic:
        return "en", 1.0
    ratio = cyrillic / total
    if ratio >= 0.7:
        return "ru", round(ratio, 2)
    if ratio <= 0.3:
        return "en", round(1.0 - ratio, 2)
    return "mixed", round(ratio, 2)


def interpret_free_messages(user: Any, full: Any) -> Optional[bool]:
    value = None
    for obj in (full, user):
        if obj is None:
            continue
        if hasattr(obj, "send_paid_messages_stars"):
            raw = getattr(obj, "send_paid_messages_stars")
            if raw is not None:
                value = raw
                break
    if value is None:
        return True
    try:
        amount = int(value)
    except (TypeError, ValueError):
        return None
    if amount > 0:
        return False
    return True


def extract_account_level(full: Any) -> Optional[int]:
    rating = getattr(full, "stars_rating", None) if full is not None else None
    if rating is None:
        log("PROFILE", "Account level unavailable through Telegram API")
        return None
    level = getattr(rating, "level", None)
    if level is None:
        log("PROFILE", "Account level unavailable through Telegram API")
        return None
    try:
        return int(level)
    except (TypeError, ValueError):
        log("PROFILE", "Account level unavailable through Telegram API")
        return None


class ProfileAnalyzer:
    def __init__(
        self,
        client: TelegramClient,
        db: Database,
        settings: Settings,
        limiter: ApiLimiter,
        stats: RuntimeStats,
    ) -> None:
        self.client = client
        self.db = db
        self.settings = settings
        self.limiter = limiter
        self.stats = stats

    async def ensure_nft_count(self, profile: Profile, user: Any = None) -> Profile:
        """Fill in the gift count for a profile fetched with with_nft=False.

        Counting unique gifts costs a paginated API call per owner, so the
        scanner defers it until the free checks (gender, language, level)
        have already passed.
        """
        if profile.user_id is None or profile.nft_count is not None:
            return profile
        profile.nft_count = await self._count_unique_gifts(profile.user_id, user)
        profile.updated_at = utc_now_iso()
        self.db.upsert_profile(profile)
        return profile

    async def get_profile(
        self,
        user_id: Optional[int],
        user: Any = None,
        force_refresh: bool = False,
        with_nft: bool = True,
    ) -> Profile:
        profile = Profile(user_id=user_id)
        if user_id is not None and not force_refresh and self.db.is_profile_fresh(user_id, self.settings.profile_cache_ttl):
            cached = self.db.get_profile(user_id)
            if cached is not None:
                debug("PROFILE", f"Using cached profile {user_id}")
                return cached

        entity = user
        full = None
        if user_id is not None:
            try:
                entity, full = await self._fetch_full_user(user_id, user)
            except Exception as error:
                log("ERROR", f"Failed to fetch public profile {user_id}: {type(error).__name__}")
                entity = user

        if isinstance(entity, User) and not isinstance(entity, UserEmpty):
            profile.user_id = entity.id
            profile.username = getattr(entity, "username", None) or None
            profile.first_name = getattr(entity, "first_name", None) or None
            profile.last_name = getattr(entity, "last_name", None) or None
            if not profile.username:
                extras = getattr(entity, "usernames", None) or []
                for item in extras:
                    name = getattr(item, "username", None)
                    if name:
                        profile.username = name
                        break

        if full is not None:
            profile.bio = getattr(full, "about", None) or None
            profile.public_channel = bool(getattr(full, "personal_channel_id", None))
            gifts_count = getattr(full, "stargifts_count", None)
            profile.public_gifts = bool(gifts_count) if gifts_count is not None else None
            profile.account_level = extract_account_level(full)
            profile.free_messages = interpret_free_messages(entity, full)
        else:
            profile.free_messages = interpret_free_messages(entity, None)
            if profile.account_level is None:
                log("PROFILE", "Account level unavailable through Telegram API")

        language, score = detect_profile_language(profile)
        profile.language = language
        profile.language_score = score

        if profile.user_id is not None:
            if with_nft:
                profile.nft_count = await self._count_unique_gifts(profile.user_id, entity)
            prefs = self.db.get_manual_profile_preferences(profile.user_id)
            # Keep the operator's /tag value raw: filters infer from the name
            # themselves, and a strict filter must be able to tell an explicit
            # tag apart from a guess.
            profile.manual_gender = prefs.get("manual_gender")
            profile.manual_nationality = prefs.get("manual_nationality")
            profile.manual_tag = prefs.get("manual_tag")
            profile.updated_at = utc_now_iso()
            self.db.upsert_profile(profile)
        return profile

    async def _fetch_full_user(self, user_id: int, user: Any) -> tuple[Any, Any]:
        async def _call():
            async with self.limiter:
                input_user = self._to_input_user(user_id, user)
                if input_user is None:
                    entity = await self.client.get_entity(user_id)
                    input_user = self._to_input_user(user_id, entity)
                    user_obj = entity
                else:
                    user_obj = user
                result = await self.client(GetFullUserRequest(id=input_user))
                full = getattr(result, "full_user", None) or getattr(result, "full_user", result)
                users = getattr(result, "users", None) or []
                for item in users:
                    if getattr(item, "id", None) == user_id:
                        user_obj = item
                        break
                return user_obj, full

        return await invoke_telegram(_call, stats=self.stats, max_backoff=self.settings.max_api_backoff)

    def _to_input_user(self, user_id: int, user: Any) -> InputUser | None:
        if isinstance(user, User) and getattr(user, "access_hash", None) is not None:
            return InputUser(user_id=user.id, access_hash=user.access_hash)
        return None

    async def _count_unique_gifts(self, user_id: int, user: Any) -> Optional[int]:
        limit = self.settings.max_nft_count + 1
        offset = ""
        unique = 0
        pages = 0
        peer = self._to_input_peer(user_id, user)
        if peer is None:
            try:
                entity = await self.client.get_input_entity(user_id)
                peer = entity
            except Exception:
                debug("PROFILE", f"NFT count unavailable for {user_id}: no input peer")
                return None
        seen_offsets: set[str] = set()
        while unique < limit and pages < 20:
            if offset in seen_offsets:
                break
            seen_offsets.add(offset)

            async def _call(current_offset=offset, current_peer=peer):
                async with self.limiter:
                    return await self.client(
                        GetSavedStarGiftsRequest(
                            peer=current_peer,
                            offset=current_offset,
                            limit=13,
                            exclude_unlimited=True,
                        )
                    )

            try:
                result = await invoke_telegram(
                    _call,
                    stats=self.stats,
                    max_backoff=self.settings.max_api_backoff,
                )
            except FloodWaitError:
                raise
            except Exception as error:
                debug("PROFILE", f"NFT count unavailable: {type(error).__name__}")
                return unique if unique else None

            gifts = getattr(result, "gifts", None) or []
            debug("PROFILE", f"NFT page gifts={len(gifts)} offset={offset!r}")
            for saved in gifts:
                gift = getattr(saved, "gift", saved)
                if isinstance(gift, StarGiftUnique) or type(gift).__name__ == "StarGiftUnique":
                    unique += 1
                    if unique >= limit:
                        return unique
            next_offset = getattr(result, "next_offset", None) or ""
            pages += 1
            if not next_offset:
                break
            offset = next_offset
        return unique

    def _to_input_peer(self, user_id: int, user: Any) -> InputPeerUser | None:
        if isinstance(user, User) and getattr(user, "access_hash", None) is not None:
            return InputPeerUser(user_id=user.id, access_hash=user.access_hash)
        return None
