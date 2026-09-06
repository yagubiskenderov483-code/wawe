from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import test_settings
from app.marketplace.models import Listing, Profile


def passing_listing(**overrides) -> Listing:
    data = dict(
        listing_key="gift:1",
        gift_id=10,
        slug="desk-calendar-1",
        gift_name="Desk Calendar",
        gift_number=10,
        price=12000,
        model="ModelA",
        symbol="Star",
        backdrop="Sunset",
        owner_id=111,
        collectible=True,
        resale=True,
        is_new=True,
        market_value=18000,
        floor_price=15000,
        price_ratio=12000 / 18000,
        discount_percent=((18000 - 12000) / 18000) * 100,
        market_confidence="high",
        market_sample_size=10,
    )
    data.update(overrides)
    return Listing(**data)


def passing_profile(**overrides) -> Profile:
    data = dict(
        user_id=111,
        username="ivan",
        first_name="Ivan",
        last_name="Petrov",
        bio="Привет, коллекционирую подарки",
        language="ru",
        language_score=1.0,
        nft_count=2,
        free_messages=True,
        account_level=1,
        public_channel=True,
        public_gifts=True,
        manual_gender="female",
        manual_nationality="ru",
    )
    data.update(overrides)
    return Profile(**data)


def settings(**overrides):
    return test_settings(**overrides)
