from __future__ import annotations

import keyring
import pytest
from keyring.backends import fail

from hoard import backup, db, keystore, sync


def _insert_item(item_id: str, token: str) -> None:
    import sqlite3

    # Raw connection: get_conn() would run the migration before we can look.
    with sqlite3.connect(db.settings.db_path) as conn:
        conn.execute(
            "INSERT INTO items (item_id, access_token, institution) VALUES (?, ?, 'Bank')",
            (item_id, token),
        )


def _column(item_id: str) -> str:
    import sqlite3

    with sqlite3.connect(db.settings.db_path) as conn:
        return conn.execute(
            "SELECT access_token FROM items WHERE item_id = ?", (item_id,)
        ).fetchone()[0]


def test_migration_moves_tokens_into_keychain(memory_keyring):
    _insert_item("it1", "access-prod-secret")

    db.init_db()

    assert _column("it1") == ""
    assert memory_keyring.store[("hoard", "item:it1")] == "access-prod-secret"
    assert sync.access_token_for("it1") == "access-prod-secret"
    assert any(p.name.endswith("-pre-keyring.db") for p in backup.list_backups())


def test_migration_is_noop_without_keychain():
    keyring.set_keyring(fail.Keyring())
    _insert_item("it1", "access-prod-secret")

    db.init_db()

    assert _column("it1") == "access-prod-secret"
    assert sync.access_token_for("it1") == "access-prod-secret"


def test_store_token_falls_back_to_column_without_keychain():
    assert sync.store_token("it1", "tok") == ""
    keyring.set_keyring(fail.Keyring())
    assert keystore.available() is False
    assert sync.store_token("it2", "tok") == "tok"


def test_missing_token_raises():
    _insert_item("it1", "")
    with pytest.raises(sync.MissingTokenError):
        sync.access_token_for("it1")


def test_unlink_deletes_keychain_entry(memory_keyring):
    _insert_item("it1", "tok")
    db.init_db()
    assert ("hoard", "item:it1") in memory_keyring.store

    sync.unlink_item("it1")

    assert ("hoard", "item:it1") not in memory_keyring.store


def test_legacy_money_mover_secret_is_adopted(memory_keyring):
    memory_keyring.store[("money-mover", "item:old")] = "tok-old"

    assert keystore.get("item:old") == "tok-old"
    assert memory_keyring.store[("hoard", "item:old")] == "tok-old"
    assert ("money-mover", "item:old") not in memory_keyring.store
