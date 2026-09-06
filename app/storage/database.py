from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from app.marketplace.models import (
    STATUS_EXISTING,
    STATUS_NEW,
    Listing,
    Profile,
    utc_now_iso,
)
from app.utils.logger import debug, log


SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_key TEXT UNIQUE NOT NULL,
    gift_id INTEGER,
    slug TEXT,
    gift_name TEXT,
    gift_number INTEGER,
    price INTEGER,
    model TEXT,
    symbol TEXT,
    backdrop TEXT,
    owner_id INTEGER,
    seller_id INTEGER,
    first_seen_at TEXT,
    sent_at TEXT,
    score INTEGER,
    status TEXT NOT NULL DEFAULT 'NEW',
    skip_reason TEXT,
    last_notified_price INTEGER,
    unique_id INTEGER,
    owner_username TEXT,
    market_value INTEGER,
    floor_price INTEGER,
    price_ratio REAL,
    discount_percent REAL,
    market_confidence TEXT,
    market_sample_size INTEGER,
    profile_score INTEGER,
    manual_gender TEXT,
    manual_nationality TEXT,
    is_initial_snapshot INTEGER NOT NULL DEFAULT 0,
    queued_at TEXT,
    target_channel TEXT,
    message_id INTEGER,
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS profiles (
    user_id INTEGER UNIQUE NOT NULL,
    username TEXT,
    first_name TEXT,
    last_name TEXT,
    bio TEXT,
    language TEXT,
    language_score REAL,
    nft_count INTEGER,
    free_messages INTEGER,
    account_level INTEGER,
    public_channel INTEGER,
    public_gifts INTEGER,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS profile_preferences (
    user_id INTEGER UNIQUE NOT NULL,
    manual_gender TEXT,
    manual_nationality TEXT,
    manual_tag TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS manual_tags (
    user_id INTEGER UNIQUE NOT NULL,
    manual_gender TEXT,
    manual_nationality TEXT,
    manual_tag TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS stats (
    key TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_key TEXT NOT NULL,
    old_price INTEGER,
    new_price INTEGER,
    changed_at TEXT NOT NULL,
    notified INTEGER NOT NULL DEFAULT 0,
    UNIQUE(listing_key, old_price, new_price, changed_at)
);

CREATE TABLE IF NOT EXISTS listing_price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_key TEXT NOT NULL,
    price INTEGER,
    recorded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS market_prices (
    cache_key TEXT PRIMARY KEY,
    gift_id INTEGER,
    model TEXT,
    symbol TEXT,
    backdrop TEXT,
    market_value INTEGER,
    floor_price INTEGER,
    sample_size INTEGER,
    confidence TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS scanner_state (
    gift_id INTEGER PRIMARY KEY,
    next_offset TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS scanner_meta (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS queue (
    listing_key TEXT PRIMARY KEY,
    gift_id INTEGER,
    priority INTEGER,
    queued_at TEXT,
    status TEXT NOT NULL DEFAULT 'PENDING'
);

CREATE TABLE IF NOT EXISTS notify_chats (
    chat_id INTEGER PRIMARY KEY,
    added_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS publish_channels (
    chat_id INTEGER PRIMARY KEY,
    title TEXT,
    added_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_listings_listing_key ON listings(listing_key);
CREATE INDEX IF NOT EXISTS idx_listings_gift_id ON listings(gift_id);
CREATE INDEX IF NOT EXISTS idx_listings_slug ON listings(slug);
CREATE INDEX IF NOT EXISTS idx_listings_owner_id ON listings(owner_id);
CREATE INDEX IF NOT EXISTS idx_listings_seller_id ON listings(seller_id);
CREATE INDEX IF NOT EXISTS idx_listings_status ON listings(status);
CREATE INDEX IF NOT EXISTS idx_listings_first_seen_at ON listings(first_seen_at);
CREATE INDEX IF NOT EXISTS idx_listings_sent_at ON listings(sent_at);
CREATE INDEX IF NOT EXISTS idx_price_history_listing_key ON price_history(listing_key);
CREATE INDEX IF NOT EXISTS idx_listing_price_history_key ON listing_price_history(listing_key);
CREATE INDEX IF NOT EXISTS idx_queue_status ON queue(status);
"""

_LISTING_EXTRA_COLUMNS = (
    ("seller_id", "INTEGER"),
    ("owner_username", "TEXT"),
    ("market_value", "INTEGER"),
    ("floor_price", "INTEGER"),
    ("price_ratio", "REAL"),
    ("discount_percent", "REAL"),
    ("market_confidence", "TEXT"),
    ("market_sample_size", "INTEGER"),
    ("profile_score", "INTEGER"),
    ("manual_gender", "TEXT"),
    ("manual_nationality", "TEXT"),
    ("is_initial_snapshot", "INTEGER NOT NULL DEFAULT 0"),
    ("queued_at", "TEXT"),
    ("target_channel", "TEXT"),
    ("message_id", "INTEGER"),
    ("created_at", "TEXT"),
    ("updated_at", "TEXT"),
    ("status", "TEXT NOT NULL DEFAULT 'NEW'"),
    ("skip_reason", "TEXT"),
    ("last_notified_price", "INTEGER"),
    ("unique_id", "INTEGER"),
)


def _bool_to_db(value: Optional[bool]) -> Optional[int]:
    if value is None:
        return None
    return 1 if value else 0


def _db_to_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    return bool(value)


def market_cache_key(
    gift_id: int | None,
    model: str | None,
    symbol: str | None,
    backdrop: str | None,
) -> str:
    return "|".join(
        [
            str(gift_id if gift_id is not None else ""),
            (model or "").strip(),
            (symbol or "").strip(),
            (backdrop or "").strip(),
        ]
    )


class Database:
    def __init__(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(SCHEMA)
        for column, definition in _LISTING_EXTRA_COLUMNS:
            self._ensure_column("listings", column, definition)
        self._ensure_column("profile_preferences", "manual_tag", "TEXT")
        self._migrate_manual_tags()
        self.conn.commit()

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        rows = self.conn.execute(f"PRAGMA table_info({table})").fetchall()
        names = {row["name"] for row in rows}
        if column not in names:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _migrate_manual_tags(self) -> None:
        self.conn.execute(
            """
            INSERT OR IGNORE INTO manual_tags (user_id, manual_gender, manual_nationality, manual_tag, updated_at)
            SELECT user_id, manual_gender, manual_nationality, manual_tag, updated_at
            FROM profile_preferences
            """
        )

    def close(self) -> None:
        try:
            self.conn.commit()
        finally:
            self.conn.close()

    def backup_to(self, dest_path: str) -> None:
        Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
        dest = sqlite3.connect(dest_path)
        try:
            with dest:
                self.conn.backup(dest)
        finally:
            dest.close()

    def listing_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS n FROM listings").fetchone()
        return int(row["n"] if row else 0)

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM scanner_meta WHERE key = ?",
            (key,),
        ).fetchone()
        if not row or row["value"] is None:
            return default
        return str(row["value"])

    def set_meta(self, key: str, value: str | None) -> None:
        debug("DATABASE", f"meta {key}={value}")
        self.conn.execute(
            """
            INSERT INTO scanner_meta (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value=excluded.value,
                updated_at=excluded.updated_at
            """,
            (key, value, utc_now_iso()),
        )
        self.conn.commit()

    def get_scanner_mode(self) -> str | None:
        return self.get_meta("scanner_mode")

    def set_scanner_mode(self, mode: str) -> None:
        self.set_meta("scanner_mode", mode)
        log("DATABASE", f"scanner_mode={mode}")

    def get_listing(self, listing_key: str) -> Optional[dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM listings WHERE listing_key = ?",
            (listing_key,),
        ).fetchone()
        return dict(row) if row else None

    def has_listing(self, listing_key: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM listings WHERE listing_key = ? LIMIT 1",
            (listing_key,),
        ).fetchone()
        return row is not None

    def was_sent(self, listing_key: str) -> bool:
        row = self.conn.execute(
            "SELECT sent_at, status FROM listings WHERE listing_key = ?",
            (listing_key,),
        ).fetchone()
        if not row:
            return False
        if row["sent_at"]:
            return True
        return str(row["status"] or "") == "SENT"

    def insert_listing(self, listing: Listing) -> bool:
        debug("DATABASE", f"INSERT listing {listing.listing_key}")
        now = utc_now_iso()
        try:
            self.conn.execute(
                """
                INSERT INTO listings (
                    listing_key, gift_id, slug, gift_name, gift_number, price,
                    model, symbol, backdrop, owner_id, seller_id, first_seen_at, sent_at,
                    score, status, skip_reason, last_notified_price, unique_id,
                    owner_username, market_value, floor_price, price_ratio, discount_percent,
                    market_confidence, market_sample_size, profile_score, manual_gender,
                    manual_nationality, is_initial_snapshot, queued_at, target_channel,
                    message_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    listing.listing_key,
                    listing.gift_id,
                    listing.slug,
                    listing.gift_name,
                    listing.gift_number,
                    listing.price,
                    listing.model,
                    listing.symbol,
                    listing.backdrop,
                    listing.owner_id,
                    listing.seller_id if listing.seller_id is not None else listing.owner_id,
                    listing.first_seen_at or now,
                    listing.sent_at,
                    listing.score,
                    listing.status or STATUS_NEW,
                    listing.skip_reason,
                    listing.last_notified_price,
                    listing.unique_id,
                    listing.owner_username,
                    listing.market_value,
                    listing.floor_price,
                    listing.price_ratio,
                    listing.discount_percent,
                    listing.market_confidence,
                    listing.market_sample_size,
                    listing.profile_score,
                    listing.manual_gender,
                    listing.manual_nationality,
                    1 if listing.is_initial_snapshot else 0,
                    listing.queued_at,
                    listing.target_channel,
                    listing.message_id,
                    listing.created_at or now,
                    listing.updated_at or now,
                ),
            )
            self.conn.commit()
            if listing.price is not None:
                self.record_listing_price(listing.listing_key, listing.price, listing.first_seen_at or now)
            return True
        except sqlite3.IntegrityError:
            return False

    def update_listing(self, listing_key: str, **fields: Any) -> None:
        if not fields:
            return
        if "updated_at" not in fields:
            fields = dict(fields)
            fields["updated_at"] = utc_now_iso()
        assignments = ", ".join(f"{key} = ?" for key in fields)
        values = list(fields.values())
        values.append(listing_key)
        debug("DATABASE", f"UPDATE listing {listing_key} fields={tuple(fields)}")
        self.conn.execute(f"UPDATE listings SET {assignments} WHERE listing_key = ?", values)
        self.conn.commit()

    def mark_status(self, listing_key: str, status: str, skip_reason: str | None = None) -> None:
        self.update_listing(listing_key, status=status, skip_reason=skip_reason)

    def mark_existing(self, listing_key: str) -> None:
        self.update_listing(
            listing_key,
            status=STATUS_EXISTING,
            is_initial_snapshot=1,
            skip_reason="initial_snapshot",
        )

    def mark_sent(
        self,
        listing_key: str,
        price: Optional[int],
        score: Optional[int] = None,
        target_channel: Optional[str] = None,
        message_id: Optional[int] = None,
    ) -> None:
        payload: dict[str, Any] = {
            "status": "SENT",
            "sent_at": utc_now_iso(),
            "last_notified_price": price,
            "skip_reason": None,
        }
        if score is not None:
            payload["score"] = score
        if price is not None:
            payload["price"] = price
        if target_channel is not None:
            payload["target_channel"] = target_channel
        if message_id is not None:
            payload["message_id"] = message_id
        self.update_listing(listing_key, **payload)
        self.remove_queue(listing_key)

    def record_listing_price(self, listing_key: str, price: Optional[int], recorded_at: str | None = None) -> None:
        if price is None:
            return
        last = self.conn.execute(
            """
            SELECT price FROM listing_price_history
            WHERE listing_key = ?
            ORDER BY id DESC LIMIT 1
            """,
            (listing_key,),
        ).fetchone()
        if last is not None and last["price"] == price:
            return
        self.conn.execute(
            """
            INSERT INTO listing_price_history (listing_key, price, recorded_at)
            VALUES (?, ?, ?)
            """,
            (listing_key, price, recorded_at or utc_now_iso()),
        )
        self.conn.commit()

    def record_price_change(
        self,
        listing_key: str,
        old_price: Optional[int],
        new_price: Optional[int],
    ) -> bool:
        changed_at = utc_now_iso()
        debug("DATABASE", f"price_history {listing_key}: {old_price} -> {new_price}")
        existing = self.conn.execute(
            """
            SELECT id, notified FROM price_history
            WHERE listing_key = ? AND old_price IS ? AND new_price IS ?
            ORDER BY id DESC LIMIT 1
            """,
            (listing_key, old_price, new_price),
        ).fetchone()
        if existing and existing["notified"]:
            return False
        if existing and not existing["notified"]:
            self.update_listing(listing_key, price=new_price)
            self.record_listing_price(listing_key, new_price, changed_at)
            return True
        self.conn.execute(
            """
            INSERT INTO price_history (listing_key, old_price, new_price, changed_at, notified)
            VALUES (?, ?, ?, ?, 0)
            """,
            (listing_key, old_price, new_price, changed_at),
        )
        self.update_listing(listing_key, price=new_price)
        self.record_listing_price(listing_key, new_price, changed_at)
        self.conn.commit()
        return True

    def mark_price_change_notified(self, listing_key: str, old_price: Optional[int], new_price: Optional[int]) -> None:
        self.conn.execute(
            """
            UPDATE price_history
            SET notified = 1
            WHERE listing_key = ? AND old_price IS ? AND new_price IS ? AND notified = 0
            """,
            (listing_key, old_price, new_price),
        )
        self.conn.commit()

    def get_price_history(self, listing_key: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM price_history WHERE listing_key = ? ORDER BY id ASC",
            (listing_key,),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_listing_price_history(self, listing_key: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM listing_price_history WHERE listing_key = ? ORDER BY id ASC",
            (listing_key,),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_market_cache(self, cache_key: str, ttl_seconds: int) -> Optional[dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM market_prices WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()
        if not row:
            return None
        if not self._is_fresh(row["updated_at"], ttl_seconds):
            return None
        return dict(row)

    def set_market_cache(
        self,
        cache_key: str,
        gift_id: int | None,
        model: str | None,
        symbol: str | None,
        backdrop: str | None,
        market_value: int | None,
        floor_price: int | None,
        sample_size: int,
        confidence: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO market_prices (
                cache_key, gift_id, model, symbol, backdrop, market_value,
                floor_price, sample_size, confidence, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                gift_id=excluded.gift_id,
                model=excluded.model,
                symbol=excluded.symbol,
                backdrop=excluded.backdrop,
                market_value=excluded.market_value,
                floor_price=excluded.floor_price,
                sample_size=excluded.sample_size,
                confidence=excluded.confidence,
                updated_at=excluded.updated_at
            """,
            (
                cache_key,
                gift_id,
                model,
                symbol,
                backdrop,
                market_value,
                floor_price,
                sample_size,
                confidence,
                utc_now_iso(),
            ),
        )
        self.conn.commit()

    def _is_fresh(self, stamp: str | None, ttl_seconds: int) -> bool:
        if not stamp:
            return False
        try:
            raw = str(stamp).replace("Z", "+00:00")
            updated = datetime.fromisoformat(raw)
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
        except ValueError:
            return False
        return datetime.now(timezone.utc) - updated < timedelta(seconds=max(0, ttl_seconds))

    def upsert_profile(self, profile: Profile) -> None:
        if profile.user_id is None:
            return
        debug("DATABASE", f"UPSERT profile {profile.user_id}")
        self.conn.execute(
            """
            INSERT INTO profiles (
                user_id, username, first_name, last_name, bio, language,
                language_score, nft_count, free_messages, account_level,
                public_channel, public_gifts, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name,
                last_name=excluded.last_name,
                bio=excluded.bio,
                language=excluded.language,
                language_score=excluded.language_score,
                nft_count=excluded.nft_count,
                free_messages=excluded.free_messages,
                account_level=excluded.account_level,
                public_channel=excluded.public_channel,
                public_gifts=excluded.public_gifts,
                updated_at=excluded.updated_at
            """,
            (
                profile.user_id,
                profile.username,
                profile.first_name,
                profile.last_name,
                profile.bio,
                profile.language,
                profile.language_score,
                profile.nft_count,
                _bool_to_db(profile.free_messages),
                profile.account_level,
                _bool_to_db(profile.public_channel),
                _bool_to_db(profile.public_gifts),
                profile.updated_at or utc_now_iso(),
            ),
        )
        self.conn.commit()

    def get_profile(self, user_id: int) -> Optional[Profile]:
        row = self.conn.execute("SELECT * FROM profiles WHERE user_id = ?", (user_id,)).fetchone()
        if not row:
            return None
        prefs = self.get_manual_profile_preferences(user_id)
        return Profile(
            user_id=row["user_id"],
            username=row["username"],
            first_name=row["first_name"],
            last_name=row["last_name"],
            bio=row["bio"],
            language=row["language"],
            language_score=row["language_score"],
            nft_count=row["nft_count"],
            free_messages=_db_to_bool(row["free_messages"]),
            account_level=row["account_level"],
            public_channel=_db_to_bool(row["public_channel"]),
            public_gifts=_db_to_bool(row["public_gifts"]),
            updated_at=row["updated_at"],
            manual_gender=prefs.get("manual_gender") if prefs else None,
            manual_nationality=prefs.get("manual_nationality") if prefs else None,
            manual_tag=prefs.get("manual_tag") if prefs else None,
            cached=True,
        )

    def is_profile_fresh(self, user_id: int, ttl_seconds: int) -> bool:
        row = self.conn.execute("SELECT updated_at FROM profiles WHERE user_id = ?", (user_id,)).fetchone()
        if not row or not row["updated_at"]:
            return False
        return self._is_fresh(row["updated_at"], ttl_seconds)

    def get_manual_profile_preferences(self, user_id: int) -> dict[str, Optional[str]]:
        row = self.conn.execute(
            "SELECT * FROM manual_tags WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if not row:
            row = self.conn.execute(
                "SELECT * FROM profile_preferences WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if not row:
            return {
                "user_id": user_id,
                "manual_gender": None,
                "manual_nationality": None,
                "manual_tag": None,
                "updated_at": None,
            }
        return {
            "user_id": row["user_id"],
            "manual_gender": row["manual_gender"],
            "manual_nationality": row["manual_nationality"],
            "manual_tag": row["manual_tag"],
            "updated_at": row["updated_at"],
        }

    def set_manual_profile_preference(
        self,
        user_id: int,
        gender: Optional[str] = None,
        nationality: Optional[str] = None,
        tag: Optional[str] = None,
    ) -> dict[str, Optional[str]]:
        current = self.get_manual_profile_preferences(user_id)
        if gender is not None:
            current["manual_gender"] = gender.strip().lower() or None
        if nationality is not None:
            current["manual_nationality"] = nationality.strip().lower() or None
        if tag is not None:
            current["manual_tag"] = tag.strip().lower() or None
        current["updated_at"] = utc_now_iso()
        payload = (
            user_id,
            current["manual_gender"],
            current["manual_nationality"],
            current["manual_tag"],
            current["updated_at"],
        )
        for table in ("manual_tags", "profile_preferences"):
            self.conn.execute(
                f"""
                INSERT INTO {table} (user_id, manual_gender, manual_nationality, manual_tag, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    manual_gender=excluded.manual_gender,
                    manual_nationality=excluded.manual_nationality,
                    manual_tag=excluded.manual_tag,
                    updated_at=excluded.updated_at
                """,
                payload,
            )
        self.conn.commit()
        return current

    def delete_manual_profile_preference(self, user_id: int) -> None:
        self.conn.execute("DELETE FROM manual_tags WHERE user_id = ?", (user_id,))
        self.conn.execute("DELETE FROM profile_preferences WHERE user_id = ?", (user_id,))
        self.conn.commit()

    def get_scanner_offset(self, gift_id: int) -> Optional[str]:
        row = self.conn.execute(
            "SELECT next_offset FROM scanner_state WHERE gift_id = ?",
            (gift_id,),
        ).fetchone()
        if not row:
            return None
        value = row["next_offset"]
        return str(value) if value else None

    def set_scanner_offset(self, gift_id: int, next_offset: Optional[str]) -> None:
        debug("DATABASE", f"scanner_state gift_id={gift_id} next_offset={next_offset or ''}")
        self.conn.execute(
            """
            INSERT INTO scanner_state (gift_id, next_offset, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(gift_id) DO UPDATE SET
                next_offset=excluded.next_offset,
                updated_at=excluded.updated_at
            """,
            (gift_id, next_offset or "", utc_now_iso()),
        )
        self.conn.commit()

    def reset_scanner_offset(self, gift_id: int) -> None:
        self.set_scanner_offset(gift_id, "")

    def enqueue_listing(self, listing_key: str, gift_id: int | None, priority: int) -> None:
        now = utc_now_iso()
        self.conn.execute(
            """
            INSERT INTO queue (listing_key, gift_id, priority, queued_at, status)
            VALUES (?, ?, ?, ?, 'PENDING')
            ON CONFLICT(listing_key) DO UPDATE SET
                gift_id=excluded.gift_id,
                priority=excluded.priority,
                queued_at=excluded.queued_at,
                status='PENDING'
            """,
            (listing_key, gift_id, priority, now),
        )
        self.update_listing(listing_key, queued_at=now, status="QUEUED", skip_reason=None)
        self.conn.commit()

    def remove_queue(self, listing_key: str) -> None:
        self.conn.execute("DELETE FROM queue WHERE listing_key = ?", (listing_key,))
        self.conn.commit()

    def cancel_queue(self, listing_key: str, reason: str = "pause_drained") -> None:
        self.conn.execute(
            "UPDATE queue SET status = 'CANCELLED' WHERE listing_key = ?",
            (listing_key,),
        )
        self.mark_status(listing_key, STATUS_EXISTING, reason)
        self.remove_queue(listing_key)

    def add_notify_chat(self, chat_id: int) -> None:
        self.conn.execute(
            """
            INSERT INTO notify_chats (chat_id, added_at)
            VALUES (?, ?)
            ON CONFLICT(chat_id) DO NOTHING
            """,
            (int(chat_id), utc_now_iso()),
        )
        self.conn.commit()

    def get_notify_chats(self) -> tuple[int, ...]:
        rows = self.conn.execute("SELECT chat_id FROM notify_chats ORDER BY added_at ASC").fetchall()
        return tuple(int(row["chat_id"]) for row in rows)

    def add_publish_channel(self, chat_id: int, title: str | None = None) -> None:
        if int(chat_id) >= 0:
            return
        self.conn.execute(
            """
            INSERT INTO publish_channels (chat_id, title, added_at)
            VALUES (?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                title=COALESCE(excluded.title, publish_channels.title)
            """,
            (int(chat_id), title, utc_now_iso()),
        )
        self.conn.commit()
        log("PUBLISHER", f"Target channel registered: {chat_id}")

    def remove_publish_channel(self, chat_id: int) -> None:
        self.conn.execute("DELETE FROM publish_channels WHERE chat_id = ?", (int(chat_id),))
        self.conn.commit()

    def get_publish_channels(self) -> tuple[int, ...]:
        rows = self.conn.execute(
            "SELECT chat_id FROM publish_channels WHERE chat_id < 0 ORDER BY added_at ASC"
        ).fetchall()
        return tuple(int(row["chat_id"]) for row in rows)

    def seller_was_published(self, seller_id: int | None) -> bool:
        if seller_id is None:
            return False
        row = self.conn.execute(
            """
            SELECT 1 FROM listings
            WHERE (seller_id = ? OR owner_id = ?)
              AND (status = 'SENT' OR sent_at IS NOT NULL)
            LIMIT 1
            """,
            (int(seller_id), int(seller_id)),
        ).fetchone()
        return row is not None

    def seller_is_queued(self, seller_id: int | None, except_key: str | None = None) -> bool:
        if seller_id is None:
            return False
        sql = """
            SELECT 1 FROM listings
            WHERE (seller_id = ? OR owner_id = ?)
              AND status = 'QUEUED'
        """
        params: list[Any] = [int(seller_id), int(seller_id)]
        if except_key:
            sql += " AND listing_key != ?"
            params.append(except_key)
        sql += " LIMIT 1"
        return self.conn.execute(sql, params).fetchone() is not None


def get_manual_profile_preferences(db: Database, user_id: int) -> dict[str, Optional[str]]:
    return db.get_manual_profile_preferences(user_id)


def set_manual_profile_preference(
    db: Database,
    user_id: int,
    gender: Optional[str] = None,
    nationality: Optional[str] = None,
    tag: Optional[str] = None,
) -> dict[str, Optional[str]]:
    return db.set_manual_profile_preference(user_id, gender=gender, nationality=nationality, tag=tag)
