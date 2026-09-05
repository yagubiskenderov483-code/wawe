from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.helpers import passing_listing, passing_profile
from app.marketplace.models import STATUS_NEW, STATUS_SENT, utc_now_iso
from app.storage.database import Database


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "tracker.db")
        self.db = Database(self.path)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_new_listing(self):
        listing = passing_listing(listing_key="gift:new")
        self.assertTrue(self.db.insert_listing(listing))
        row = self.db.get_listing("gift:new")
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], STATUS_NEW)

    def test_duplicate_listing(self):
        listing = passing_listing(listing_key="gift:dup")
        self.assertTrue(self.db.insert_listing(listing))
        self.assertFalse(self.db.insert_listing(listing))

    def test_database_persistence(self):
        listing = passing_listing(listing_key="gift:persist", price=7000)
        self.db.insert_listing(listing)
        self.db.mark_sent("gift:persist", 7000, score=8)
        self.db.close()
        reopened = Database(self.path)
        row = reopened.get_listing("gift:persist")
        self.assertEqual(row["status"], STATUS_SENT)
        self.assertIsNotNone(row["sent_at"])
        self.assertEqual(row["price"], 7000)
        reopened.close()
        self.db = Database(self.path)

    def test_price_history(self):
        listing = passing_listing(listing_key="gift:price", price=15000)
        self.db.insert_listing(listing)
        created = self.db.record_price_change("gift:price", 15000, 11500)
        self.assertTrue(created)
        history = self.db.get_price_history("gift:price")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["old_price"], 15000)
        self.assertEqual(history[0]["new_price"], 11500)
        self.assertEqual(history[0]["notified"], 0)

    def test_price_change(self):
        listing = passing_listing(listing_key="gift:change", price=20000)
        self.db.insert_listing(listing)
        self.assertTrue(self.db.record_price_change("gift:change", 20000, 8000))
        self.db.mark_price_change_notified("gift:change", 20000, 8000)
        again = self.db.record_price_change("gift:change", 20000, 8000)
        self.assertFalse(again)
        row = self.db.get_listing("gift:change")
        self.assertEqual(row["price"], 8000)

    def test_profile_cache(self):
        profile = passing_profile(user_id=42)
        profile.updated_at = utc_now_iso()
        self.db.upsert_profile(profile)
        self.assertTrue(self.db.is_profile_fresh(42, 300))
        self.assertFalse(self.db.is_profile_fresh(42, 0))
        loaded = self.db.get_profile(42)
        self.assertEqual(loaded.username, "ivan")
        self.assertTrue(loaded.cached)

    def test_next_offset(self):
        self.db.set_scanner_offset(99, "abc")
        self.assertEqual(self.db.get_scanner_offset(99), "abc")
        self.db.reset_scanner_offset(99)
        self.assertFalse(bool(self.db.get_scanner_offset(99)))

    def test_manual_tags(self):
        prefs = self.db.set_manual_profile_preference(7, gender="female", nationality="ru", tag="trusted")
        self.assertEqual(prefs["manual_gender"], "female")
        stored = self.db.get_manual_profile_preferences(7)
        self.assertEqual(stored["manual_tag"], "trusted")
        self.db.delete_manual_profile_preference(7)
        self.assertIsNone(self.db.get_manual_profile_preferences(7)["manual_gender"])

    def test_notify_chats(self):
        self.db.add_notify_chat(555001)
        self.db.add_notify_chat(555001)
        self.assertEqual(self.db.get_notify_chats(), (555001,))

    def test_error_listing_can_retry(self):
        from unittest.mock import MagicMock

        from app.marketplace.models import STATUS_ERROR
        from app.marketplace.scanner import MarketplaceScanner
        from tests.helpers import passing_listing, settings

        scanner = MarketplaceScanner(
            settings(),
            MagicMock(),
            self.db,
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
        )
        listing = passing_listing(listing_key="gift:err", price=12000)
        existing = {
            "price": 12000,
            "status": STATUS_ERROR,
            "sent_at": None,
            "last_notified_price": None,
            "skip_reason": "send_failed",
        }
        self.assertEqual(scanner._classify_signal(listing, existing), "retry")


if __name__ == "__main__":
    unittest.main()
