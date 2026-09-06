from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from app.marketplace.models import (
    SCANNER_MODE_LIVE,
    STATUS_EXISTING,
    STATUS_NEW,
    STATUS_QUEUED,
    STATUS_SENT,
    STATUS_SKIPPED,
    QueueItem,
)
from app.marketplace.parser import parse_listing
from app.marketplace.scanner import MarketplaceScanner
from app.storage.database import Database
from app.utils.state import AppState, pick_diversified_index
from tests.helpers import passing_listing, passing_profile, settings


class StarGiftUnique:
    def __init__(self, unique_id: int, gift_id: int = 10, price: int = 12000, num: int = 10, owner_id: int = 111):
        self.id = unique_id
        self.gift_id = gift_id
        self.title = "Desk Calendar"
        self.slug = f"desk-calendar-{unique_id}"
        self.num = num
        self.owner_id = type("Peer", (), {"user_id": owner_id})()
        self.attributes = []
        self.resell_amount = [type("StarsAmount", (), {"amount": price, "nanos": 0})()]


class FakeMarket:
    def __init__(self, value: int = 18000):
        self.value = value

    async def estimate(self, listing, force_refresh: bool = False):
        listing.market_value = self.value
        listing.floor_price = max(1, self.value - 1000)
        listing.market_sample_size = 12
        listing.market_confidence = "high"
        if listing.price:
            listing.price_ratio = listing.price / self.value
            listing.discount_percent = ((self.value - listing.price) / self.value) * 100
        return listing


def _scanner(db: Database, state: AppState | None = None, paused: bool = False) -> MarketplaceScanner:
    cfg = settings(manual_gender_filter="")
    state = state or AppState()
    if paused:
        state.pause()
    analyzer = AsyncMock()
    analyzer.get_profile.return_value = passing_profile()
    scanner = MarketplaceScanner(
        cfg,
        state,
        db,
        MagicMock(),
        analyzer,
        MagicMock(),
        MagicMock(),
        market=FakeMarket(),
    )
    return scanner


class SnapshotAndRestartTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "tracker.db")
        self.db = Database(self.path)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    async def test_old_listing_existing_not_queued(self):
        scanner = _scanner(self.db)
        self.assertEqual(scanner.state.scanner_mode, "INITIAL_SNAPSHOT")
        gift = StarGiftUnique(1)
        queued = await scanner._process_gift(gift)
        self.assertFalse(queued)
        row = self.db.get_listing("gift:1")
        self.assertEqual(row["status"], STATUS_EXISTING)
        self.assertEqual(self.db.was_sent("gift:1"), False)
        self.assertTrue(scanner.state.queue.empty())

    async def test_new_listing_after_snapshot(self):
        scanner = _scanner(self.db)
        await scanner._process_gift(StarGiftUnique(1))
        scanner._finish_snapshot()
        self.assertEqual(scanner.state.scanner_mode, SCANNER_MODE_LIVE)
        await scanner._process_gift(StarGiftUnique(2))
        row = self.db.get_listing("gift:2")
        self.assertEqual(row["status"], STATUS_QUEUED)
        self.assertEqual(scanner.state.queue.qsize(), 1)
        self.assertTrue(row["status"] != STATUS_EXISTING)

    async def test_duplicate_key_not_new(self):
        scanner = _scanner(self.db)
        scanner._finish_snapshot()
        await scanner._process_gift(StarGiftUnique(3))
        size = scanner.state.queue.qsize()
        again = await scanner._process_gift(StarGiftUnique(3))
        self.assertFalse(again)
        self.assertEqual(scanner.state.queue.qsize(), size)

    async def test_restart_does_not_publish_old_listings(self):
        first = _scanner(self.db)
        await first._process_gift(StarGiftUnique(8))
        first._finish_snapshot()
        await first._process_gift(StarGiftUnique(9))
        self.db.close()
        reopened = Database(self.path)
        self.db = reopened
        state = AppState()
        restarted = _scanner(reopened, state=state)
        self.assertEqual(restarted.state.scanner_mode, SCANNER_MODE_LIVE)
        result = await restarted._process_gift(StarGiftUnique(8))
        self.assertFalse(result)
        self.assertTrue(reopened.was_sent("gift:8") is False)
        self.assertEqual(reopened.get_listing("gift:8")["status"], STATUS_EXISTING)
        self.assertTrue(state.queue.empty())

    async def test_price_change_is_not_a_new_listing(self):
        scanner = _scanner(self.db)
        scanner._finish_snapshot()
        await scanner._process_gift(StarGiftUnique(4, price=12000))
        item = await scanner.state.queue.get()
        scanner.state.queue.task_done()
        self.db.mark_sent("gift:4", 12000)
        signal = scanner._classify_signal(parse_listing(StarGiftUnique(4, price=9000)), self.db.get_listing("gift:4"))
        self.assertIsNone(signal)
        history = self.db.get_listing_price_history("gift:4")
        self.assertGreaterEqual(len(history), 1)

    async def test_price_changed_unsent_is_not_a_new_listing(self):
        scanner = _scanner(self.db)
        scanner._finish_snapshot()
        listing = passing_listing(listing_key="gift:44", price=12000, gift_id=10)
        self.db.insert_listing(listing)
        existing = self.db.get_listing("gift:44")
        listing.price = 11000
        signal = scanner._classify_signal(listing, existing)
        self.assertIsNone(signal)
        self.assertTrue(scanner.state.queue.empty())

    async def test_same_owner_not_queued_twice(self):
        scanner = _scanner(self.db)
        scanner._finish_snapshot()
        await scanner._process_gift(StarGiftUnique(20, price=12000))
        await scanner._process_gift(StarGiftUnique(21, price=13000))
        self.assertEqual(scanner.state.queue.qsize(), 1)
        row = self.db.get_listing("gift:21")
        self.assertEqual(row["skip_reason"], "owner_already_queued")

    async def test_same_owner_not_published_again(self):
        scanner = _scanner(self.db)
        scanner._finish_snapshot()
        listing = passing_listing(listing_key="gift:30", owner_id=111, seller_id=111, price=12000)
        self.db.insert_listing(listing)
        self.db.mark_sent("gift:30", 12000)
        await scanner._process_gift(StarGiftUnique(31, price=14000))
        row = self.db.get_listing("gift:31")
        self.assertEqual(row["skip_reason"], "owner_already_published")
        self.assertTrue(scanner.state.queue.empty())
        self.assertEqual(scanner.state.stats.get("skip_owner"), 1)

    async def test_different_owners_both_queued(self):
        scanner = _scanner(self.db)
        scanner._finish_snapshot()
        await scanner._process_gift(StarGiftUnique(40, price=12000, owner_id=111))
        await scanner._process_gift(StarGiftUnique(41, price=13000, owner_id=222))
        self.assertEqual(scanner.state.queue.qsize(), 2)

    async def test_live_scan_publishes_only_leading_new_listing(self):
        scanner = _scanner(self.db)
        await scanner._process_gift(StarGiftUnique(1))
        scanner._finish_snapshot()

        class Page:
            gifts = [StarGiftUnique(100, price=12000), StarGiftUnique(1), StarGiftUnique(200, price=13000)]
            users = []
            next_offset = ""

        scanner._fetch_resale_page = AsyncMock(return_value=Page())
        await scanner._scan_gift(10, snapshot=False)
        self.assertEqual(self.db.get_listing("gift:100")["status"], STATUS_QUEUED)
        self.assertEqual(self.db.get_listing("gift:1")["status"], STATUS_EXISTING)
        trailing = self.db.get_listing("gift:200")
        self.assertEqual(trailing["status"], STATUS_EXISTING)
        self.assertEqual(scanner.state.queue.qsize(), 1)

    async def test_live_scan_still_checks_later_new_lots_before_known(self):
        scanner = _scanner(self.db)
        await scanner._process_gift(StarGiftUnique(1))
        scanner._finish_snapshot()

        class Page:
            gifts = [
                StarGiftUnique(100, price=800),
                StarGiftUnique(101, price=12000, owner_id=222),
                StarGiftUnique(1),
                StarGiftUnique(200, price=13000, owner_id=333),
            ]
            users = []
            next_offset = ""

        scanner._fetch_resale_page = AsyncMock(return_value=Page())
        await scanner._scan_gift(10, snapshot=False)
        cheap = self.db.get_listing("gift:100")
        self.assertEqual(cheap["status"], STATUS_SKIPPED)
        self.assertEqual(cheap["skip_reason"], "price_below_min")
        self.assertEqual(self.db.get_listing("gift:101")["status"], STATUS_QUEUED)
        self.assertEqual(self.db.get_listing("gift:200")["status"], STATUS_EXISTING)
        self.assertEqual(self.db.get_listing("gift:200")["skip_reason"], "behind_known")
        self.assertEqual(scanner.state.queue.qsize(), 1)

    async def test_live_scan_checks_whole_page_when_nothing_is_known(self):
        scanner = _scanner(self.db)
        scanner._finish_snapshot()

        class Page:
            gifts = [
                StarGiftUnique(100, price=800),
                StarGiftUnique(101, price=12000, owner_id=222),
            ]
            users = []
            next_offset = ""

        scanner._fetch_resale_page = AsyncMock(return_value=Page())
        await scanner._scan_gift(10, snapshot=False)
        self.assertEqual(self.db.get_listing("gift:100")["skip_reason"], "price_below_min")
        self.assertEqual(self.db.get_listing("gift:101")["status"], STATUS_QUEUED)
        self.assertEqual(scanner.state.queue.qsize(), 1)

    async def test_live_overflow_is_retried(self):
        scanner = _scanner(self.db)
        scanner._finish_snapshot()
        listing = passing_listing(listing_key="gift:77", price=12000, owner_id=222, seller_id=222)
        listing.is_initial_snapshot = False
        listing.status = STATUS_EXISTING
        listing.skip_reason = "live_overflow"
        self.db.insert_listing(listing)
        self.db.mark_existing("gift:77", skip_reason="live_overflow", is_initial_snapshot=False)

        class Page:
            gifts = [StarGiftUnique(77, price=12000, owner_id=222)]
            users = []
            next_offset = ""

        scanner._fetch_resale_page = AsyncMock(return_value=Page())
        await scanner._scan_gift(10, snapshot=False)
        self.assertEqual(self.db.get_listing("gift:77")["status"], STATUS_QUEUED)
        self.assertEqual(scanner.state.queue.qsize(), 1)

    async def test_unique_owners_can_be_disabled(self):
        scanner = _scanner(self.db)
        scanner.settings = settings(
            target_channel_id=-100123,
            unique_owners=False,
            diversify_gifts=False,
        )
        scanner._finish_snapshot()
        await scanner._process_gift(StarGiftUnique(50, price=12000, owner_id=111))
        await scanner._process_gift(StarGiftUnique(51, price=13000, owner_id=111))
        self.assertEqual(scanner.state.queue.qsize(), 2)

    async def test_pause_does_not_enqueue(self):
        state = AppState()
        scanner = _scanner(self.db, state=state)
        scanner._finish_snapshot()
        state.pause()
        await scanner._process_gift(StarGiftUnique(5))
        self.assertTrue(state.queue.empty())
        row = self.db.get_listing("gift:5")
        self.assertEqual(row["status"], STATUS_EXISTING)


class DiversifyTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_gift_not_consecutive(self):
        state = AppState()
        a1 = QueueItem(passing_listing(listing_key="a1", gift_id=1), passing_profile(), 50)
        b1 = QueueItem(passing_listing(listing_key="b1", gift_id=2), passing_profile(), 40)
        a2 = QueueItem(passing_listing(listing_key="a2", gift_id=1), passing_profile(), 30)
        await state.queue.put(a1)
        await state.queue.put(b1)
        await state.queue.put(a2)
        first = await state.queue.get_diversified(None, 0, 1, True)
        self.assertEqual(first.listing.gift_id, 1)
        state.note_published_gift(first.listing.gift_id)
        second = await state.queue.get_diversified(state.last_published_gift_id, state.same_gift_streak, 1, True)
        self.assertEqual(second.listing.gift_id, 2)

    def test_pick_index_allows_repeat_when_no_alternative(self):
        self.assertEqual(pick_diversified_index([10, 10], 10, 1, 1, True), 0)

    async def test_unique_owner_not_consecutive_when_alternative_exists(self):
        state = AppState()
        a = QueueItem(passing_listing(listing_key="o1", gift_id=1, owner_id=111, seller_id=111), passing_profile(), 50)
        b = QueueItem(
            passing_listing(listing_key="o2", gift_id=2, owner_id=222, seller_id=222),
            passing_profile(user_id=222),
            40,
        )
        await state.queue.put(a)
        await state.queue.put(b)
        first = await state.queue.get_diversified(None, 0, 1, False, last_owner_id=111, unique_owners=True)
        self.assertEqual(first.listing.owner_id, 222)

    async def test_same_model_not_consecutive(self):
        state = AppState()
        first = QueueItem(passing_listing(listing_key="m1", gift_id=1, model="Cat"), passing_profile(), 50)
        other = QueueItem(passing_listing(listing_key="m2", gift_id=2, model="Dog"), passing_profile(), 40)
        same = QueueItem(passing_listing(listing_key="m3", gift_id=3, model="Cat"), passing_profile(), 30)
        await state.queue.put(first)
        await state.queue.put(other)
        await state.queue.put(same)
        taken = await state.queue.get_diversified(None, 0, 1, True)
        self.assertEqual(taken.listing.model, "Cat")
        state.note_published_gift(taken.listing.gift_id, taken.listing.owner_id, taken.listing.model)
        next_item = await state.queue.get_diversified(
            state.last_published_gift_id,
            state.same_gift_streak,
            1,
            True,
            last_model=state.last_published_model,
        )
        self.assertEqual(next_item.listing.model, "Dog")


class StatusHelpersTests(unittest.TestCase):
    def test_new_status_constant(self):
        self.assertEqual(STATUS_NEW, "NEW")
        self.assertEqual(STATUS_SENT, "SENT")


if __name__ == "__main__":
    unittest.main()
