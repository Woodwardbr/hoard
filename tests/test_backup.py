from __future__ import annotations

import sqlite3
from datetime import date

from hoard import backup, db


def test_backup_copies_db_once_per_day():
    with db.get_conn() as conn:
        conn.execute("INSERT INTO budgets (category, monthly_limit) VALUES ('Food', 100)")

    first = backup.backup_now()
    assert first is not None and first.name == f"hoard-{date.today():%Y%m%d}.db"
    with sqlite3.connect(first) as conn:
        assert conn.execute("SELECT monthly_limit FROM budgets").fetchone() == (100,)

    assert backup.backup_now() is None


def test_tagged_backup_is_separate():
    assert backup.backup_now() is not None
    tagged = backup.backup_now(tag="pre-keyring")
    assert tagged is not None and tagged.name.endswith("-pre-keyring.db")


def test_prunes_to_keep():
    d = backup.backup_dir()
    d.mkdir(parents=True)
    for day in range(1, 6):
        (d / f"hoard-202601{day:02d}.db").write_bytes(b"")
    backup.backup_now(keep=3)
    names = [p.name for p in backup.list_backups()]
    assert len(names) == 3
    assert names[-1] == f"hoard-{date.today():%Y%m%d}.db"


def test_no_db_no_backup(tmp_path, monkeypatch):
    import dataclasses

    monkeypatch.setattr(
        db, "settings", dataclasses.replace(db.settings, db_path=tmp_path / "none" / "x.db")
    )
    assert backup.backup_now() is None
