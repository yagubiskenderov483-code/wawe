from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from telethon.errors import FloodWaitError

from app.config import resolve_publish_targets
from app.marketplace.models import QueueItem, STATUS_SENT
from app.notifications.publisher import Publisher
from app.storage.database import Database
from app.utils.state import AppState
from tests.helpers import passing_listing, passing_profile, settings


class FakeMessage:
    message_id = 77


class RecordingBot:
    def __init__(self):
        self.chat_ids: list[int] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.chat_ids.append(int(chat_id))
        return FakeMessage()


class FloodBot(RecordingBot):
    def __init__(self):
        super().__init__()
        self.calls = 0

    async def send_message(self, chat_id, text, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raise FloodWaitError(None, 3)
        return await super().send_message(chat_id, text, **kwargs)


class TargetTests(unittest.TestCase):
    def test_publisher_uses_target_channel_only(self):
        cfg = settings(target_channel_id=-100123, target_channels=(), admin_user_id=42, bot_token="8825465611:test")
        targets = cfg.publish_targets()
        self.assertEqual(targets, (-100123,))
        self.assertNotIn(42, targets)
        self.assertNotIn(8825465611, targets)

    def test_admin_and_bot_never_marketplace_targets(self):
        targets = resolve_publish_targets(
            (8825465611, 42, -100555),
            bot_id=8825465611,
            admin_id=42,
        )
        self.assertEqual(targets, (-100555,))

    def test_operator_chats_ignored(self):
        from app.config import resolve_publish_chat_ids

        targets = resolve_publish_chat_ids((-100123,), (555001,), bot_id=8825465611, admin_id=555001)
        self.assertEqual(targets, (-100123,))
        self.assertNotIn(555001, targets)


class PublisherSendTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.tmp.name) / "tracker.db"))
        self.state = AppState()
        self.cfg = settings(target_channel_id=-100123, admin_user_id=42, publish_delay=4, diversify_gifts=False)
        self.bot = RecordingBot()
        self.slept: list[float] = []

        async def fake_sleep(seconds):
            self.slept.append(float(seconds))

        self.publisher = Publisher(
            self.cfg,
            self.state,
            self.db,
            self.bot,
            MagicMock(),
            AsyncMock(),
            MagicMock(),
            market=MagicMock(),
            sleeper=fake_sleep,
        )
        self.publisher.market.estimate = AsyncMock()
        self.publisher.analyzer.get_profile = AsyncMock(return_value=passing_profile())

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    async def test_send_goes_to_target_channel(self):
        listing = passing_listing(listing_key="gift:pub")
        self.db.insert_listing(listing)
        item = QueueItem(listing, passing_profile(), 1)
        self.publisher._recheck = AsyncMock(return_value=(True, "ok"))
        published = await self.publisher._publish_item(item)
        self.assertTrue(published)
        self.assertEqual(self.bot.chat_ids, [-100123])
        self.assertNotIn(42, self.bot.chat_ids)
        row = self.db.get_listing("gift:pub")
        self.assertEqual(row["status"], STATUS_SENT)
        self.assertEqual(row["target_channel"], "-100123")

    async def test_delay_after_successful_publish(self):
        listing = passing_listing(listing_key="gift:delay")
        self.db.insert_listing(listing)
        item = QueueItem(listing, passing_profile(), 1)
        await self.state.queue.put(item)
        self.publisher._recheck = AsyncMock(return_value=(True, "ok"))

        async def stop_after(_item):
            result = await Publisher._publish_item(self.publisher, _item)
            self.state.request_shutdown()
            return result

        self.publisher._publish_item = stop_after
        await self.publisher.run()
        self.assertIn(4.0, self.slept)

    async def test_floodwait_sleeps_before_retry(self):
        self.bot = FloodBot()
        self.publisher.bot = self.bot
        listing = passing_listing(listing_key="gift:flood")
        self.db.insert_listing(listing)
        item = QueueItem(listing, passing_profile(), 1)
        self.publisher._recheck = AsyncMock(return_value=(True, "ok"))
        published = await self.publisher._publish_item(item)
        self.assertTrue(published)
        self.assertIn(3.0, self.slept)
        self.assertEqual(self.bot.chat_ids, [-100123])

    async def test_never_sends_to_bot_private_chat(self):
        self.publisher.settings = settings(
            target_channel_id=8825465611,
            admin_user_id=42,
            bot_token="8825465611:test",
        )
        listing = passing_listing(listing_key="gift:dm")
        self.db.insert_listing(listing)
        item = QueueItem(listing, passing_profile(), 1)
        self.publisher._recheck = AsyncMock(return_value=(True, "ok"))
        published = await self.publisher._publish_item(item)
        self.assertFalse(published)
        self.assertEqual(self.bot.chat_ids, [])

    async def test_owner_already_sent_skipped_on_recheck(self):
        first = passing_listing(listing_key="gift:own1", owner_id=111, seller_id=111)
        self.db.insert_listing(first)
        self.db.mark_sent("gift:own1", 12000)
        second = passing_listing(listing_key="gift:own2", owner_id=111, seller_id=111)
        self.db.insert_listing(second)
        item = QueueItem(second, passing_profile(user_id=111), 1)
        published = await self.publisher._publish_item(item)
        self.assertFalse(published)
        self.assertEqual(self.bot.chat_ids, [])

    async def test_already_sent_not_resent(self):
        listing = passing_listing(listing_key="gift:once")
        self.db.insert_listing(listing)
        self.db.mark_sent("gift:once", 12000)
        item = QueueItem(listing, passing_profile(), 1)
        published = await self.publisher._publish_item(item)
        self.assertFalse(published)
        self.assertEqual(self.bot.chat_ids, [])


if __name__ == "__main__":
    unittest.main()
