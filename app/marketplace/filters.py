from __future__ import annotations

from typing import Iterable, Optional

from app.config import Settings
from app.marketplace.models import FilterResult, Listing, Profile
from app.utils.logger import log


def _ok(reason: str) -> FilterResult:
    return FilterResult(True, reason)


def _fail(reason: str) -> FilterResult:
    return FilterResult(False, reason)


def _norm(value: Optional[str]) -> str:
    return (value or "").strip().lower()


def _in_csv(value: Optional[str], allowed: Iterable[str]) -> bool:
    if value is None:
        return False
    needle = value.casefold()
    return any(item.casefold() == needle for item in allowed)


def check_collectible(listing: Listing) -> FilterResult:
    if listing.collectible or listing.resale:
        return _ok("collectible_resale")
    return _fail("not_collectible_resale")


def check_price(listing: Listing, settings: Settings) -> FilterResult:
    price = listing.price
    if price is None:
        log("FILTER", "Цена: неизвестна -> SKIP")
        return _fail("price_unknown")
    if price < settings.min_price:
        log("FILTER", f"Цена: {price} ⭐ -> SKIP")
        return _fail("price_below_min")
    if price > settings.max_price:
        log("FILTER", f"Цена: {price} ⭐ -> SKIP")
        return _fail("price_above_max")
    log("FILTER", f"Цена: {price} ⭐ -> PASS")
    return _ok("price_ok")


def check_model(listing: Listing, settings: Settings) -> FilterResult:
    if not settings.allowed_models:
        return _ok("model_filter_disabled")
    if listing.model is None:
        log("FILTER", "Model: missing -> SKIP")
        return _fail("model_missing")
    if _in_csv(listing.model, settings.allowed_models):
        log("FILTER", f"Model: {listing.model} -> PASS")
        return _ok("model_ok")
    log("FILTER", f"Model: {listing.model} -> SKIP")
    return _fail("model_not_allowed")


def check_symbol(listing: Listing, settings: Settings) -> FilterResult:
    if not settings.allowed_symbols:
        return _ok("symbol_filter_disabled")
    if listing.symbol is None:
        log("FILTER", "Symbol: missing -> SKIP")
        return _fail("symbol_missing")
    if _in_csv(listing.symbol, settings.allowed_symbols):
        log("FILTER", f"Symbol: {listing.symbol} -> PASS")
        return _ok("symbol_ok")
    log("FILTER", f"Symbol: {listing.symbol} -> SKIP")
    return _fail("symbol_not_allowed")


def check_backdrop(listing: Listing, settings: Settings) -> FilterResult:
    if not settings.allowed_backdrops:
        return _ok("backdrop_filter_disabled")
    if listing.backdrop is None:
        log("FILTER", "Backdrop: missing -> SKIP")
        return _fail("backdrop_missing")
    if _in_csv(listing.backdrop, settings.allowed_backdrops):
        log("FILTER", f"Backdrop: {listing.backdrop} -> PASS")
        return _ok("backdrop_ok")
    log("FILTER", f"Backdrop: {listing.backdrop} -> SKIP")
    return _fail("backdrop_not_allowed")


def check_gift_number(listing: Listing, settings: Settings) -> FilterResult:
    if settings.min_gift_number is None and settings.max_gift_number is None:
        return _ok("gift_number_filter_disabled")
    number = listing.gift_number
    if number is None:
        log("FILTER", "Gift number: unknown -> SKIP")
        return _fail("gift_number_unknown")
    if settings.min_gift_number is not None and number < settings.min_gift_number:
        log("FILTER", f"Gift number: #{number} -> SKIP")
        return _fail("gift_number_below_min")
    if settings.max_gift_number is not None and number > settings.max_gift_number:
        log("FILTER", f"Gift number: #{number} -> SKIP")
        return _fail("gift_number_above_max")
    log("FILTER", f"Gift number: #{number} -> PASS")
    return _ok("gift_number_ok")


def _owner_tokens(profile: Profile, listing: Listing | None = None) -> set[str]:
    tokens: set[str] = set()
    user_id = profile.user_id if profile.user_id is not None else (listing.owner_id if listing else None)
    if user_id is not None:
        tokens.add(str(user_id))
    username = profile.username or (listing.owner_username if listing else None)
    if username:
        tokens.add(_norm(username.lstrip("@")))
    return tokens


def check_blacklist(profile: Profile, settings: Settings, listing: Listing | None = None) -> FilterResult:
    if not settings.blacklist_users:
        return _ok("blacklist_disabled")
    tokens = _owner_tokens(profile, listing)
    blocked = set(settings.blacklist_users)
    if tokens & blocked:
        log("FILTER", "Blacklist -> SKIP")
        return _fail("blacklist_hit")
    return _ok("not_blacklisted")


def check_whitelist(profile: Profile, settings: Settings, listing: Listing | None = None) -> FilterResult:
    if not settings.whitelist_users:
        return _ok("whitelist_disabled")
    tokens = _owner_tokens(profile, listing)
    allowed = set(settings.whitelist_users)
    if tokens & allowed:
        log("FILTER", "Whitelist -> PASS")
        return _ok("whitelist_ok")
    log("FILTER", "Whitelist -> SKIP")
    return _fail("whitelist_miss")


def check_language(profile: Profile, settings: Settings) -> FilterResult:
    language = profile.language or "unknown"
    if not settings.russian_language_required:
        log("FILTER", f"Language: {language} -> PASS")
        return _ok("language_not_required")
    if language == "ru":
        log("FILTER", "Language: ru -> PASS")
        return _ok("language_ru")
    log("FILTER", f"Language: {language} -> SKIP")
    if language == "unknown":
        return _fail("language_unknown")
    return _fail("not_russian_language")


def check_nft_count(profile: Profile, settings: Settings) -> FilterResult:
    count = profile.nft_count
    if count is None:
        if settings.strict_nft_filter:
            log("FILTER", "NFT count: unavailable -> SKIP")
            return _fail("nft_count_unavailable")
        log("FILTER", "NFT count: unavailable -> PASS")
        return _ok("nft_count_unavailable_permissive")
    if count > settings.max_nft_count:
        log("FILTER", f"NFT count: {count} -> SKIP")
        return _fail("nft_count_above_12")
    log("FILTER", f"NFT count: {count} -> PASS")
    return _ok("nft_count_ok")


def check_free_messages(profile: Profile, settings: Settings) -> FilterResult:
    value = profile.free_messages
    if value is True:
        log("FILTER", "Free messages: true -> PASS")
        return _ok("free_messages_true")
    if value is False:
        log("FILTER", "Free messages: false -> SKIP")
        return _fail("free_messages_false")
    if settings.require_free_messages:
        log("FILTER", "Free messages: unknown -> SKIP")
        return _fail("free_messages_unknown")
    log("FILTER", "Free messages: unknown -> PASS")
    return _ok("free_messages_ignored")


def check_account_level(profile: Profile, settings: Settings) -> FilterResult:
    if not settings.enable_account_level_filter:
        return _ok("account_level_filter_disabled")
    level = profile.account_level
    if level is None:
        log("PROFILE", "Account level unavailable through Telegram API")
        return _ok("account_level_unavailable")
    if level > settings.max_account_level:
        log("FILTER", f"Account level: {level} -> SKIP")
        return _fail("account_level_too_high")
    log("FILTER", f"Account level: {level} -> PASS")
    return _ok("account_level_ok")


def check_manual_profile_tags(
    profile: Profile,
    preferences: dict | None = None,
    settings: Settings | None = None,
) -> FilterResult:
    prefs = preferences or {}
    gender = _norm(prefs.get("manual_gender", profile.manual_gender))
    nationality = _norm(prefs.get("manual_nationality", profile.manual_nationality))
    tag = _norm(prefs.get("manual_tag", profile.manual_tag))

    if tag == "ignore":
        log("FILTER", "Manual tag: ignore -> SKIP")
        return _fail("manual_tag_ignore")

    if settings is None:
        return _ok("manual_tags_ok")

    gender_filter = settings.normalized_gender_filter()
    if gender_filter:
        if gender != gender_filter:
            log("FILTER", f"Manual gender: {gender or 'unmarked'} -> SKIP")
            return _fail("manual_gender_mismatch")
        log("FILTER", f"Manual gender: {gender} -> PASS")

    nationality_filter = settings.normalized_nationality_filter()
    if nationality_filter:
        if nationality != nationality_filter:
            log("FILTER", f"Manual nationality: {nationality or 'unmarked'} -> SKIP")
            return _fail("manual_nationality_mismatch")
        log("FILTER", f"Manual nationality: {nationality} -> PASS")

    return _ok("manual_tags_ok")


def calculate_profile_score(profile: Profile) -> int:
    score = 0
    if profile.username:
        score += 2
    if profile.bio:
        score += 2
    if profile.language == "ru":
        score += 3
    if profile.public_channel:
        score += 1
    if profile.public_gifts:
        score += 1
    if profile.nft_count is not None and 0 <= profile.nft_count <= 12:
        score += 1
    if profile.free_messages is True:
        score += 1
    if profile.account_level is not None and profile.account_level <= 2:
        score += 1
    return score


def calculate_listing_score(listing: Listing, settings: Settings) -> int:
    score = 0
    if listing.price is not None and settings.min_price <= listing.price <= settings.max_price:
        score += 2
    if listing.collectible or listing.resale:
        score += 2
    if listing.is_new:
        score += 1
    if settings.is_favorite_model(listing.model):
        score += 2
    return score


def calculate_score(listing: Listing, profile: Profile, settings: Settings) -> tuple[int, int, int]:
    listing_score = calculate_listing_score(listing, settings)
    profile_score = calculate_profile_score(profile)
    total = listing_score + profile_score
    listing.score = total
    return total, listing_score, profile_score


def calculate_priority(listing: Listing, profile: Profile, settings: Settings) -> int:
    total, _listing_score, profile_score = calculate_score(listing, profile, settings)
    priority = total * 10 + profile_score
    if listing.price is not None:
        priority += max(0, (settings.max_price - listing.price) // 500)
    if listing.gift_number is not None:
        if listing.gift_number <= 50:
            priority += 8
        elif listing.gift_number <= 200:
            priority += 4
        elif listing.gift_number <= 500:
            priority += 2
    if settings.is_favorite_model(listing.model):
        priority += 12
    return priority


def should_publish(listing: Listing, profile: Profile, settings: Settings) -> FilterResult:
    checks = [
        check_collectible(listing),
        check_price(listing, settings),
        check_model(listing, settings),
        check_symbol(listing, settings),
        check_backdrop(listing, settings),
        check_gift_number(listing, settings),
        check_blacklist(profile, settings, listing),
        check_whitelist(profile, settings, listing),
        check_language(profile, settings),
        check_nft_count(profile, settings),
        check_free_messages(profile, settings),
        check_account_level(profile, settings),
        check_manual_profile_tags(profile, settings=settings),
    ]
    for result in checks:
        if not result.passed:
            return result

    total, _listing_score, profile_score = calculate_score(listing, profile, settings)
    if profile_score < settings.min_profile_score:
        log("FILTER", f"Profile score: {profile_score} -> SKIP")
        return _fail("profile_score_too_low")
    if total < settings.min_score:
        log("FILTER", f"Score: {total} -> SKIP")
        return _fail("score_too_low")
    log("FILTER", f"Score: {total} -> PASS")
    return _ok("publish_ok")


def classify_filter_stat(reason: str) -> str | None:
    mapping = {
        "price_unknown": "price_filtered",
        "price_below_min": "price_filtered",
        "price_above_max": "price_filtered",
        "model_missing": "rarity_filtered",
        "model_not_allowed": "rarity_filtered",
        "symbol_missing": "rarity_filtered",
        "symbol_not_allowed": "rarity_filtered",
        "backdrop_missing": "rarity_filtered",
        "backdrop_not_allowed": "rarity_filtered",
        "gift_number_unknown": "number_filtered",
        "gift_number_below_min": "number_filtered",
        "gift_number_above_max": "number_filtered",
        "blacklist_hit": "blacklist_filtered",
        "whitelist_miss": "whitelist_filtered",
        "language_unknown": "language_filtered",
        "not_russian_language": "language_filtered",
        "nft_count_unavailable": "nft_filtered",
        "nft_count_above_12": "nft_filtered",
        "free_messages_false": "free_message_filtered",
        "free_messages_unknown": "free_message_filtered",
        "account_level_too_high": "account_level_filtered",
        "manual_gender_mismatch": "manual_tag_filtered",
        "manual_nationality_mismatch": "manual_tag_filtered",
        "manual_tag_ignore": "manual_tag_filtered",
        "profile_score_too_low": "profile_filtered",
        "score_too_low": "profile_filtered",
        "not_collectible_resale": "profile_filtered",
    }
    return mapping.get(reason)
