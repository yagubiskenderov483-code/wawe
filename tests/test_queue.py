from __future__ import annotations

import asyncio
import unittest

from app.config import resolve_publish_chat_ids
from tests.helpers import passing_listing, passing_profile, settings
from app.marketplace.models import QueueItem
from app.marketplace.parser import build_listing_key, extract_stars_price, parse_listing
from app.notifications.publisher import format_listing_message, listing_keyboard, owner_profile_url
from app.profile.analyzer import detect_profile_language, detect_text_language
from app.utils.rate_limit import invoke_telegram, next_backoff
from app.utils.state import AppState, BoundedPriorityQueue
from app.utils.stats import RuntimeStats
from telethon.errors import FloodWaitError


class QueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_queue(self):
        queue = BoundedPriorityQueue(maxsize=3)
        first = QueueItem(listing=passing_listing(listing_key="a"), profile=passing_profile(), priority=1)
        await queue.put(first)
        self.assertEqual(queue.qsize(), 1)
        got = await queue.get()
        self.assertEqual(got.listing.listing_key, "a")
        queue.task_done()

    async def test_queue_limit(self):
        queue = BoundedPriorityQueue(maxsize=2)
        self.assertTrue(await queue.put(QueueItem(passing_listing(listing_key="1"), passing_profile(), 1)))
        self.assertTrue(await queue.put(QueueItem(passing_listing(listing_key="2"), passing_profile(), 2)))
        self.assertTrue(queue.is_full())
        self.assertFalse(await queue.put(QueueItem(passing_listing(listing_key="3"), passing_profile(), 99)))
        self.assertEqual(queue.qsize(), 2)

    async def test_priority_queue(self):
        queue = BoundedPriorityQueue(maxsize=10)
        low = QueueItem(passing_listing(listing_key="low"), passing_profile(), priority=1)
        high = QueueItem(passing_listing(listing_key="high"), passing_profile(), priority=50)
        mid = QueueItem(passing_listing(listing_key="mid"), passing_profile(), priority=10)
        await queue.put(low)
        await queue.put(high)
        await queue.put(mid)
        first = await queue.get()
        second = await queue.get()
        third = await queue.get()
        self.assertEqual(first.listing.listing_key, "high")
        self.assertEqual(second.listing.listing_key, "mid")
        self.assertEqual(third.listing.listing_key, "low")


class PauseResumeTests(unittest.TestCase):
    def test_pause(self):
        state = AppState()
        state.scanner_running = True
        state.pause()
        self.assertEqual(state.scanner_status(), "PAUSED")
        self.assertFalse(state.pause_event.is_set())

    def test_resume(self):
        state = AppState()
        state.scanner_running = True
        state.pause()
        state.resume()
        self.assertEqual(state.scanner_status(), "RUNNING")
        self.assertTrue(state.pause_event.is_set())


class ChannelAndMessageTests(unittest.TestCase):
    def test_multiple_channels(self):
        cfg = settings(target_channel_id=-1001, target_channels=(-1002, -1001, -1003))
        self.assertEqual(cfg.channel_ids, (-1001, -1002, -1003))

    def test_publish_skips_bot_and_admin(self):
        bot_id = 8825465611
        targets = resolve_publish_chat_ids((bot_id, -100123), (555001,), bot_id, admin_id=555001)
        self.assertEqual(targets, (-100123,))
        self.assertEqual(resolve_publish_chat_ids((bot_id,), (), bot_id), ())

    def test_positive_ids_never_marketplace_targets(self):
        targets = resolve_publish_chat_ids((8825465611, 555001), (555001,), bot_id=8825465611)
        self.assertEqual(targets, ())

    def test_snapshot_uses_one_page(self):
        cfg = settings()
        self.assertEqual(cfg.max_snapshot_pages_per_gift, 1)
        self.assertEqual(cfg.max_pages_per_gift, 1)

    def test_missing_fields_message(self):
        listing = passing_listing(gift_name=None, model=None, symbol=None, backdrop=None, slug=None, gift_number=None)
        profile = passing_profile(username=None, language="unknown", nft_count=None, account_level=None, free_messages=None)
        text = format_listing_message(listing, profile)
        self.assertIn("🎁 НОВЫЙ ЛОТ", text)
        self.assertIn("скрыт/недоступен", text)
        self.assertNotIn("None", text)
        self.assertNotIn("null", text)
        self.assertNotIn("unknown", text.lower())
        self.assertNotIn("https://t.me/nft/", text)

    def test_listing_buttons_open_nft_and_profile(self):
        listing = passing_listing(slug="desk-calendar-1", owner_id=111, owner_username="ivan")
        profile = passing_profile(username="ivan", user_id=111)
        keyboard = listing_keyboard(listing, profile)
        self.assertIsNotNone(keyboard)
        row = keyboard.inline_keyboard[0]
        self.assertEqual(row[0].text, "Открыть лот")
        self.assertEqual(row[0].url, "https://t.me/nft/desk-calendar-1")
        self.assertEqual(row[1].text, "Написать")
        self.assertEqual(row[1].url, "https://t.me/ivan")

    def test_write_button_uses_user_id_without_username(self):
        listing = passing_listing(slug="gift-9", owner_id=777, owner_username=None)
        profile = passing_profile(username=None, user_id=777)
        self.assertEqual(owner_profile_url(profile, listing), "tg://user?id=777")
        keyboard = listing_keyboard(listing, profile)
        self.assertEqual(keyboard.inline_keyboard[0][1].url, "tg://user?id=777")


class ParserTests(unittest.TestCase):
    def test_missing_fields_parser(self):
        class StarGiftUnique:
            def __init__(self):
                self.id = 55
                self.gift_id = 9
                self.title = "Test Gift"
                self.slug = None
                self.num = None
                self.owner_id = None
                self.attributes = []
                self.resell_amount = None

        listing = parse_listing(StarGiftUnique())
        self.assertIsNotNone(listing)
        self.assertEqual(listing.listing_key, "gift:55")
        self.assertIsNone(listing.price)
        self.assertIsNone(listing.slug)

    def test_listing_key_prefers_unique_id(self):
        self.assertEqual(build_listing_key(777, "slug", 1, 2, 3), "gift:777")

    def test_stars_price_ignores_ton(self):
        class StarsAmount:
            def __init__(self):
                self.amount = 12000
                self.nanos = 0

        class StarsTonAmount:
            def __init__(self):
                self.amount = 999999

        self.assertEqual(extract_stars_price([StarsTonAmount(), StarsAmount()]), 12000)


class LanguageDetectTests(unittest.TestCase):
    def test_detect_russian(self):
        lang, score = detect_text_language("Коллекционирую подарки и открытки")
        self.assertEqual(lang, "ru")
        self.assertGreater(score, 0.5)
        profile_lang, _ = detect_profile_language(passing_profile())
        self.assertEqual(profile_lang, "ru")


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_scanner_recovery(self):
        self.assertEqual(next_backoff(0), 2)
        self.assertEqual(next_backoff(2), 4)
        self.assertEqual(next_backoff(4), 8)
        self.assertEqual(next_backoff(8), 16)
        self.assertEqual(next_backoff(16), 32)
        self.assertEqual(next_backoff(32), 32)

        calls = {"n": 0}

        async def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise ConnectionError("temporary")
            return "recovered"

        slept = []

        async def fake_sleep(seconds):
            slept.append(seconds)

        result = await invoke_telegram(flaky, sleeper=fake_sleep, max_backoff=32)
        self.assertEqual(result, "recovered")
        self.assertEqual(slept, [2, 4])

    async def test_floodwait_does_not_stop(self):
        stats = RuntimeStats()
        calls = {"n": 0}

        async def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise FloodWaitError(None, 3)
            return "ok"

        slept = []

        async def fake_sleep(seconds):
            slept.append(seconds)

        result = await invoke_telegram(flaky, stats=stats, sleeper=fake_sleep)
        self.assertEqual(result, "ok")
        self.assertEqual(slept, [3])
        self.assertEqual(stats.get("floodwaits"), 1)


class StatsTests(unittest.TestCase):
    def test_stats(self):
        stats = RuntimeStats()
        stats.inc("total_scanned", 10)
        stats.inc("new_listings")
        stats.inc("price_filtered")
        stats.record_scan(1.5, "now")
        snap = stats.snapshot()
        self.assertEqual(snap["total_scanned"], 10)
        self.assertEqual(snap["new_listings"], 1)
        self.assertEqual(snap["filtered"], 1)
        self.assertEqual(snap["average_scan_time"], 1.5)


if __name__ == "__main__":
    unittest.main()
