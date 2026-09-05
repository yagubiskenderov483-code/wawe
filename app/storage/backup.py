from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from app.storage.database import Database
from app.utils.logger import log


def create_backup(db: Database, backup_dir: str) -> str:
    Path(backup_dir).mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    dest = str(Path(backup_dir) / f"tracker_{stamp}.db")
    db.backup_to(dest)
    log("DATABASE", f"SQLite backup saved to {dest}")
    return dest
