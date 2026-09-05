from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from app.marketplace.models import (
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
    first_seen_at TEXT,
    sent_at TEXT,
    score INTEGER,
    status TEXT NOT NULL DEFAULT 'NEW',
    skip_reason TEXT,
    last_notified_price INTEGER,
    unique_id INTEGER
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

CREATE TABLE IF NOT EXISTS scanner_state (
    gift_id INTEGER PRIMARY KEY,
    next_offset TEXT,
    updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_listings_listing_key ON listings(listing_key);
CREATE INDEX IF NOT EXISTS idx_listings_slug ON listings(slug);
CREATE INDEX IF NOT EXISTS idx_listings_owner_id ON listings(owner_id);
CREATE INDEX IF NOT EXISTS idx_listings_first_seen_at ON listings(first_seen_at);
CREATE INDEX IF NOT EXISTS idx_listings_sent_at ON listings(sent_at);
CREATE INDEX IF NOT EXISTS idx_price_history_listing_key ON price_history(listing_key);
"""


def _bool_to_db(value: Optional[bool]) -> Optional[int]:
    if value is None:
        return None
    return 1 if value else 0


def _db_to_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    return bool(value)


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
        self._ensure_column("listings", "status", "TEXT NOT NULL DEFAULT 'NEW'")
        self._ensure_column("listings", "skip_reason", "TEXT")
        self._ensure_column("listings", "last_notified_price", "INTEGER")
        self._ensure_column("listings", "unique_id", "INTEGER")
        self._ensure_column("profile_preferences", "manual_tag", "TEXT")
        self.conn.commit()

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        rows = self.conn.execute(f"PRAGMA table_info({table})").fetchall()
        names = {row["name"] for row in rows}
        if column not in names:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

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

    def get_listing(self, listing_key: str) -> Optional[dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM listings WHERE listing_key = ?",
            (listing_key,),
        ).fetchone()
        return dict(row) if row else None

    def insert_listing(self, listing: Listing) -> bool:
        debug("DB", f"INSERT listing {listing.listing_key}")
        try:
            self.conn.execute(
                """
                INSERT INTO listings (
                    listing_key, gift_id, slug, gift_name, gift_number, price,
                    model, symbol, backdrop, owner_id, first_seen_at, sent_at,
                    score, status, skip_reason, last_notified_price, unique_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    listing.first_seen_at or utc_now_iso(),
                    listing.sent_at,
                    listing.score,
                    listing.status or STATUS_NEW,
                    listing.skip_reason,
                    listing.last_notified_price,
                    listing.unique_id,
                ),
            )
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def update_listing(self, listing_key: str, **fields: Any) -> None:
        if not fields:
            return
        assignments = ", ".join(f"{key} = ?" for key in fields)
        values = list(fields.values())
        values.append(listing_key)
        debug("DB", f"UPDATE listing {listing_key} fields={tuple(fields)}")
        self.conn.execute(f"UPDATE listings SET {assignments} WHERE listing_key = ?", values)
        self.conn.commit()

    def mark_status(self, listing_key: str, status: str, skip_reason: str | None = None) -> None:
        self.update_listing(listing_key, status=status, skip_reason=skip_reason)

    def mark_sent(self, listing_key: str, price: Optional[int], score: Optional[int] = None) -> None:
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
        self.update_listing(listing_key, **payload)

    def record_price_change(
        self,
        listing_key: str,
        old_price: Optional[int],
        new_price: Optional[int],
    ) -> bool:
        changed_at = utc_now_iso()
        debug("DB", f"price_history {listing_key}: {old_price} -> {new_price}")
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
            return True
        self.conn.execute(
            """
            INSERT INTO price_history (listing_key, old_price, new_price, changed_at, notified)
            VALUES (?, ?, ?, ?, 0)
            """,
            (listing_key, old_price, new_price, changed_at),
        )
        self.update_listing(listing_key, price=new_price)
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

    def upsert_profile(self, profile: Profile) -> None:
        if profile.user_id is None:
            return
        debug("DB", f"UPSERT profile {profile.user_id}")
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
        try:
            raw = str(row["updated_at"]).replace("Z", "+00:00")
            updated = datetime.fromisoformat(raw)
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
        except ValueError:
            return False
        return datetime.now(timezone.utc) - updated < timedelta(seconds=max(0, ttl_seconds))

    def get_manual_profile_preferences(self, user_id: int) -> dict[str, Optional[str]]:
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
        self.conn.execute(
            """
            INSERT INTO profile_preferences (user_id, manual_gender, manual_nationality, manual_tag, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                manual_gender=excluded.manual_gender,
                manual_nationality=excluded.manual_nationality,
                manual_tag=excluded.manual_tag,
                updated_at=excluded.updated_at
            """,
            (
                user_id,
                current["manual_gender"],
                current["manual_nationality"],
                current["manual_tag"],
                current["updated_at"],
            ),
        )
        self.conn.commit()
        return current

    def delete_manual_profile_preference(self, user_id: int) -> None:
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
        debug("DB", f"scanner_state gift_id={gift_id} next_offset={next_offset or ''}")
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
