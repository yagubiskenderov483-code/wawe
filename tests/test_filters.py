from __future__ import annotations

import unittest

from tests.helpers import passing_listing, passing_profile, settings
from app.marketplace.filters import (
    calculate_priority,
    calculate_profile_score,
    calculate_score,
    check_account_level,
    check_backdrop,
    check_blacklist,
    check_free_messages,
    check_gift_number,
    check_language,
    check_manual_profile_tags,
    check_model,
    check_nft_count,
    check_price,
    check_symbol,
    check_whitelist,
    should_publish,
)


class PriceFilterTests(unittest.TestCase):
    def test_price_filter(self):
        result = check_price(passing_listing(price=12000), settings())
        self.assertTrue(result.passed)
        self.assertEqual(result.reason, "price_ok")

    def test_price_min(self):
        cfg = settings()
        self.assertTrue(check_price(passing_listing(price=5000), cfg).passed)
        self.assertFalse(check_price(passing_listing(price=4999), cfg).passed)
        self.assertEqual(check_price(passing_listing(price=4999), cfg).reason, "price_below_min")

    def test_price_max(self):
        cfg = settings()
        self.assertTrue(check_price(passing_listing(price=25000), cfg).passed)
        failed = check_price(passing_listing(price=25001), cfg)
        self.assertFalse(failed.passed)
        self.assertEqual(failed.reason, "price_above_max")

    def test_price_unknown(self):
        result = check_price(passing_listing(price=None), settings())
        self.assertFalse(result.passed)
        self.assertEqual(result.reason, "price_unknown")


class NftFilterTests(unittest.TestCase):
    def test_nft_limit_12(self):
        result = check_nft_count(passing_profile(nft_count=12), settings())
        self.assertTrue(result.passed)

    def test_nft_13_rejected(self):
        result = check_nft_count(passing_profile(nft_count=13), settings())
        self.assertFalse(result.passed)
        self.assertEqual(result.reason, "nft_count_above_12")

    def test_nft_unavailable_permissive(self):
        result = check_nft_count(passing_profile(nft_count=None), settings(strict_nft_filter=False))
        self.assertTrue(result.passed)

    def test_nft_unavailable_strict(self):
        result = check_nft_count(passing_profile(nft_count=None), settings(strict_nft_filter=True))
        self.assertFalse(result.passed)


class GiftNumberTests(unittest.TestCase):
    def test_gift_number_min(self):
        cfg = settings(min_gift_number=1, max_gift_number=500)
        self.assertTrue(check_gift_number(passing_listing(gift_number=1), cfg).passed)

    def test_gift_number_max(self):
        cfg = settings(min_gift_number=1, max_gift_number=500)
        self.assertTrue(check_gift_number(passing_listing(gift_number=500), cfg).passed)
        failed = check_gift_number(passing_listing(gift_number=501), cfg)
        self.assertFalse(failed.passed)
        self.assertEqual(failed.reason, "gift_number_above_max")


class RarityFilterTests(unittest.TestCase):
    def test_model_filter(self):
        cfg = settings(allowed_models=("ModelA", "ModelB"))
        self.assertTrue(check_model(passing_listing(model="ModelA"), cfg).passed)
        self.assertFalse(check_model(passing_listing(model="Other"), cfg).passed)

    def test_symbol_filter(self):
        cfg = settings(allowed_symbols=("Star",))
        self.assertTrue(check_symbol(passing_listing(symbol="Star"), cfg).passed)
        self.assertFalse(check_symbol(passing_listing(symbol="Moon"), cfg).passed)

    def test_backdrop_filter(self):
        cfg = settings(allowed_backdrops=("Sunset",))
        self.assertTrue(check_backdrop(passing_listing(backdrop="Sunset"), cfg).passed)
        self.assertFalse(check_backdrop(passing_listing(backdrop="Night"), cfg).passed)


class ListFilterTests(unittest.TestCase):
    def test_whitelist(self):
        cfg = settings(whitelist_users=("111",))
        self.assertTrue(check_whitelist(passing_profile(user_id=111), cfg).passed)
        missed = check_whitelist(passing_profile(user_id=222, username="other"), cfg)
        self.assertFalse(missed.passed)
        self.assertEqual(missed.reason, "whitelist_miss")

    def test_blacklist(self):
        cfg = settings(blacklist_users=("111", "spamuser"))
        blocked = check_blacklist(passing_profile(user_id=111), cfg)
        self.assertFalse(blocked.passed)
        self.assertEqual(blocked.reason, "blacklist_hit")
        self.assertTrue(check_blacklist(passing_profile(user_id=999, username="ok"), cfg).passed)
        by_name = check_blacklist(passing_profile(user_id=5, username="spamuser"), cfg)
        self.assertFalse(by_name.passed)
        listing = passing_listing(price=25000, market_value=600)
        result = should_publish(listing, passing_profile(user_id=111), cfg)
        self.assertFalse(result.passed)
        self.assertEqual(result.reason, "blacklist_hit")


class LanguageAndProfileTests(unittest.TestCase):
    def test_language_filter(self):
        cfg = settings(russian_language_required=True)
        self.assertTrue(check_language(passing_profile(language="ru"), cfg).passed)
        self.assertTrue(check_language(passing_profile(language="mixed"), cfg).passed)
        unknown = check_language(passing_profile(language="unknown"), cfg)
        self.assertFalse(unknown.passed)
        self.assertEqual(unknown.reason, "language_unknown")
        english = check_language(
            passing_profile(language="en", first_name="Anna", last_name=None, bio="Hello"),
            cfg,
        )
        self.assertFalse(english.passed)
        self.assertEqual(english.reason, "not_russian_language")

    def test_free_messages_filter(self):
        cfg = settings(require_free_messages=True)
        self.assertTrue(check_free_messages(passing_profile(free_messages=True), cfg).passed)
        self.assertFalse(check_free_messages(passing_profile(free_messages=False), cfg).passed)
        self.assertFalse(check_free_messages(passing_profile(free_messages=None), cfg).passed)

    def test_account_level_filter(self):
        enabled = settings(enable_account_level_filter=True, max_account_level=2)
        self.assertTrue(check_account_level(passing_profile(account_level=2), enabled).passed)
        self.assertFalse(check_account_level(passing_profile(account_level=3), enabled).passed)
        self.assertTrue(check_account_level(passing_profile(account_level=None), enabled).passed)
        disabled = settings(enable_account_level_filter=False)
        self.assertTrue(check_account_level(passing_profile(account_level=99), disabled).passed)

    def test_manual_gender_filter(self):
        cfg = settings(manual_gender_filter="female")
        self.assertTrue(check_manual_profile_tags(passing_profile(manual_gender="female"), settings=cfg).passed)
        failed = check_manual_profile_tags(passing_profile(manual_gender="male"), settings=cfg)
        self.assertFalse(failed.passed)
        self.assertEqual(failed.reason, "manual_gender_mismatch")
        missing = check_manual_profile_tags(
            passing_profile(manual_gender=None, first_name="Alex", last_name=None, language="en"),
            settings=cfg,
        )
        self.assertFalse(missing.passed)
        self.assertEqual(missing.reason, "manual_gender_missing")
        unmarked_ru = check_manual_profile_tags(
            passing_profile(manual_gender=None, first_name="Alex", last_name=None, language="ru"),
            settings=cfg,
        )
        self.assertTrue(unmarked_ru.passed)
        inferred = check_manual_profile_tags(
            passing_profile(manual_gender=None, first_name="Анна"),
            settings=cfg,
        )
        self.assertTrue(inferred.passed)
        emoji = check_manual_profile_tags(
            passing_profile(manual_gender=None, first_name="Катя 💕"),
            settings=cfg,
        )
        self.assertTrue(emoji.passed)
        last_name = check_manual_profile_tags(
            passing_profile(manual_gender=None, first_name="🌸", last_name="Петрова"),
            settings=cfg,
        )
        self.assertTrue(last_name.passed)

    def test_manual_nationality_filter(self):
        cfg = settings(manual_nationality_filter="ru")
        self.assertTrue(check_manual_profile_tags(passing_profile(manual_nationality="ru"), settings=cfg).passed)
        failed = check_manual_profile_tags(passing_profile(manual_nationality="other"), settings=cfg)
        self.assertFalse(failed.passed)
        unmarked = check_manual_profile_tags(passing_profile(manual_nationality=None), settings=cfg)
        self.assertFalse(unmarked.passed)

    def test_score(self):
        listing = passing_listing()
        profile = passing_profile()
        cfg = settings()
        total, listing_score, profile_score = calculate_score(listing, profile, cfg)
        self.assertGreaterEqual(profile_score, 5)
        self.assertGreaterEqual(total, 6)
        self.assertEqual(profile_score, calculate_profile_score(profile))

    def test_favorite_model(self):
        cfg = settings(favorite_models=("ModelA",))
        listing = passing_listing(model="ModelA", is_new=True, price=12000)
        other = passing_listing(model="Other", is_new=True, price=12000)
        total_fav, listing_fav, _ = calculate_score(listing, passing_profile(), cfg)
        total_other, listing_other, _ = calculate_score(other, passing_profile(), cfg)
        self.assertGreaterEqual(listing_fav - listing_other, 2)
        self.assertGreater(calculate_priority(listing, passing_profile(), cfg), calculate_priority(other, passing_profile(), cfg))
        self.assertGreaterEqual(total_fav, total_other)

    def test_missing_fields(self):
        listing = passing_listing(
            slug=None,
            gift_name=None,
            gift_number=None,
            model=None,
            symbol=None,
            backdrop=None,
            owner_id=None,
        )
        profile = passing_profile(
            username=None,
            first_name=None,
            last_name=None,
            bio="Текст на русском",
            nft_count=None,
            account_level=None,
            public_channel=None,
            public_gifts=None,
        )
        cfg = settings(strict_nft_filter=False, enable_account_level_filter=False)
        result = should_publish(listing, profile, cfg)
        self.assertIsInstance(result.passed, bool)
        self.assertTrue(result.reason)

    def test_typical_new_lot_is_not_overfiltered(self):
        listing = passing_listing(price=12000, market_value=15000)
        profile = passing_profile(
            first_name="Анна",
            last_name=None,
            username="anna_gift",
            bio="Коллекционирую подарки",
            language="ru",
            nft_count=4,
            free_messages=True,
            account_level=1,
            public_channel=True,
            public_gifts=True,
            manual_gender=None,
        )
        result = should_publish(listing, profile, settings())
        self.assertTrue(result.passed, result.reason)

    def test_cheap_lot_skipped(self):
        result = should_publish(passing_listing(price=800, market_value=900), passing_profile(), settings())
        self.assertFalse(result.passed)
        self.assertEqual(result.reason, "price_below_min")

    def test_male_profile_skipped(self):
        result = should_publish(
            passing_listing(),
            passing_profile(manual_gender=None, first_name="Дима", last_name="Иванов"),
            settings(),
        )
        self.assertFalse(result.passed)
        self.assertEqual(result.reason, "manual_gender_mismatch")

    def test_overpriced_vs_market_still_skipped(self):
        listing = passing_listing(price=7000, market_value=300)
        result = should_publish(listing, passing_profile(), settings())
        self.assertFalse(result.passed)
        self.assertEqual(result.reason, "market_ratio_too_high")


if __name__ == "__main__":
    unittest.main()
