from __future__ import annotations

from typing import Any, Optional

from app.marketplace.models import Listing, utc_now_iso
from app.utils.logger import debug


def _getattr(obj: Any, *names: str, default=None):
    for name in names:
        if obj is None:
            return default
        if hasattr(obj, name):
            value = getattr(obj, name)
            if value is not None:
                return value
    return default


def extract_peer_id(peer: Any) -> Optional[int]:
    if peer is None:
        return None
    for name in ("user_id", "channel_id", "chat_id"):
        value = getattr(peer, name, None)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
    return None


def extract_stars_price(resell_amount: Any) -> Optional[int]:
    if not resell_amount:
        return None
    try:
        amounts = list(resell_amount)
    except TypeError:
        amounts = [resell_amount]
    for amount in amounts:
        type_name = type(amount).__name__
        if type_name == "StarsTonAmount":
            continue
        nanos = getattr(amount, "nanos", None)
        stars = getattr(amount, "amount", None)
        if type_name == "StarsAmount" or nanos is not None:
            try:
                return int(stars)
            except (TypeError, ValueError):
                continue
    return None


def extract_attributes(gift: Any) -> tuple[Optional[str], Optional[str], Optional[str]]:
    model = symbol = backdrop = None
    attributes = getattr(gift, "attributes", None) or []
    for attr in attributes:
        name = type(attr).__name__
        label = getattr(attr, "name", None)
        if name == "StarGiftAttributeModel":
            model = label
        elif name == "StarGiftAttributePattern":
            symbol = label
        elif name == "StarGiftAttributeBackdrop":
            backdrop = label
    return model, symbol, backdrop


def build_listing_key(unique_id: Any, slug: Any, owner_id: Any, gift_number: Any, gift_id: Any) -> str:
    if unique_id is not None:
        return f"gift:{unique_id}"
    parts = [
        str(slug or ""),
        str(owner_id or ""),
        str(gift_number or ""),
        str(gift_id or ""),
    ]
    return "slug:" + ":".join(parts)


def parse_listing(gift: Any) -> Optional[Listing]:
    if gift is None:
        return None
    type_name = type(gift).__name__
    collectible = type_name == "StarGiftUnique"
    unique_id = _getattr(gift, "id") if collectible else None
    gift_id = _getattr(gift, "gift_id", "id")
    slug = _getattr(gift, "slug")
    gift_name = _getattr(gift, "title", "gift_name")
    gift_number = _getattr(gift, "num", "gift_num")
    if gift_number is not None:
        try:
            gift_number = int(gift_number)
        except (TypeError, ValueError):
            gift_number = None
    owner_id = extract_peer_id(_getattr(gift, "owner_id", "host_id"))
    model, symbol, backdrop = extract_attributes(gift)
    price = extract_stars_price(_getattr(gift, "resell_amount"))
    listing_key = build_listing_key(unique_id, slug, owner_id, gift_number, gift_id)
    if not listing_key or listing_key == "slug::::":
        debug("PARSE", "Cannot build listing_key, skipping object")
        return None
    resale = price is not None or bool(_getattr(gift, "resell_amount"))
    return Listing(
        listing_key=listing_key,
        gift_id=int(gift_id) if gift_id is not None else None,
        slug=str(slug) if slug else None,
        gift_name=str(gift_name) if gift_name else None,
        gift_number=gift_number,
        price=price,
        model=str(model) if model else None,
        symbol=str(symbol) if symbol else None,
        backdrop=str(backdrop) if backdrop else None,
        owner_id=owner_id,
        unique_id=int(unique_id) if unique_id is not None else None,
        collectible=collectible or bool(slug),
        resale=resale or collectible,
        first_seen_at=utc_now_iso(),
    )


def listing_still_on_sale(gift: Any) -> bool:
    if gift is None:
        return False
    price = extract_stars_price(_getattr(gift, "resell_amount"))
    return price is not None
