"""Daily SQLite snapshots next to the database, so a bad sync or migration is recoverable."""

from __future__ import annotations

import logging
import sqlite3
from datetime import date
from pathlib import Path

from . import db

log = logging.getLogger(__name__)

KEEP = 14
PREFIX = "hoard-"


def backup_dir() -> Path:
    return Path(db.settings.db_path).parent / "backups"


def list_backups() -> list[Path]:
    d = backup_dir()
    return sorted(d.glob(f"{PREFIX}*.db")) if d.is_dir() else []


def backup_now(tag: str | None = None, keep: int = KEEP) -> Path | None:
    """Copy the live DB to ``backups/hoard-YYYYMMDD[-tag].db``.

    Untagged backups are taken at most once per day. Returns the path written,
    or None if the DB doesn't exist yet or today's backup already exists.
    """
    src = Path(db.settings.db_path)
    if not src.exists():
        return None
    name = f"{PREFIX}{date.today():%Y%m%d}{'-' + tag if tag else ''}.db"
    dest = backup_dir() / name
    if dest.exists():
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(src) as source, sqlite3.connect(dest) as target:
        source.backup(target)
    log.info("Backed up database to %s", dest)
    _prune(keep)
    return dest


def _prune(keep: int) -> None:
    for old in list_backups()[:-keep]:
        old.unlink(missing_ok=True)
