from __future__ import annotations

import unittest

from app.marketplace.filters import check_market_value, check_manual_profile_tags, should_publish
from app.marketplace.market import apply_market_estimate, market_confidence, median_int
from app.marketplace.models import MarketEstimate
from tests.helpers import passing_listing, passing_profile, settings


class MedianTests(unittest.TestCase):
    def test_median_ignores_outlier(self):
        self.assertEqual(median_int([500, 550, 600, 620, 650, 700, 25000]), 620)

    def test_median_even(self):
        self.assertEqual(median_int([600, 620]), 610)

    def test_median_empty(self):
        self.assertIsNone(median_int([]))


class ConfidenceTests(unittest.TestCase):
    def test_confidence_bands(self):
        self.assertEqual(market_confidence(0), "none")
        self.assertEqual(market_confidence(4), "low")
        self.assertEqual(market_confidence(5), "medium")
        self.assertEqual(market_confidence(9), "medium")
        self.assertEqual(market_confidence(10), "high")


class MarketFilterTests(unittest.TestCase):
    def test_overpriced_listing_skipped(self):
        listing = passing_listing(price=25000, market_value=600, price_ratio=25000 / 600)
        result = check_market_value(listing, settings(max_market_ratio=3.0))
        self.assertFalse(result.passed)
        self.assertEqual(result.reason, "market_ratio_too_high")

    def test_discount_listing_passes(self):
        listing = passing_listing(price=12000, market_value=20000)
        result = check_market_value(listing, settings(max_market_ratio=3.0))
        self.assertTrue(result.passed)
        self.assertAlmostEqual(listing.discount_percent, 40.0)
        self.assertAlmostEqual(listing.price_ratio, 0.6)

    def test_missing_market_strict(self):
        listing = passing_listing(market_value=None)
        result = check_market_value(listing, settings(strict_market_filter=True))
        self.assertFalse(result.passed)
        self.assertEqual(result.reason, "market_unavailable")

    def test_apply_estimate(self):
        listing = passing_listing(price=12000, market_value=None)
        estimate = MarketEstimate(market_value=18000, floor_price=15000, sample_size=11, confidence="high")
        apply_market_estimate(listing, estimate)
        self.assertEqual(listing.market_value, 18000)
        self.assertEqual(listing.floor_price, 15000)
        self.assertAlmostEqual(listing.price_ratio, 12000 / 18000)
        self.assertEqual(listing.market_sample_size, 11)

    def test_score_cannot_bypass_market(self):
        listing = passing_listing(price=25000, market_value=600, model="ModelA")
        profile = passing_profile()
        cfg = settings(favorite_models=("ModelA",), max_market_ratio=3.0)
        result = should_publish(listing, profile, cfg)
        self.assertFalse(result.passed)
        self.assertEqual(result.reason, "market_ratio_too_high")


class GenderFilterTests(unittest.TestCase):
    def test_female_tag_pass(self):
        cfg = settings(manual_gender_filter="female")
        self.assertTrue(check_manual_profile_tags(passing_profile(manual_gender="female"), settings=cfg).passed)

    def test_missing_female_tag_skip(self):
        cfg = settings(manual_gender_filter="female")
        failed = check_manual_profile_tags(
            passing_profile(manual_gender=None, first_name="Alex", last_name=None, language="en"),
            settings=cfg,
        )
        self.assertFalse(failed.passed)
        self.assertEqual(failed.reason, "manual_gender_missing")


if __name__ == "__main__":
    unittest.main()
