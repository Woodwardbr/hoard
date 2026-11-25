"""Secrets in the OS keychain (Windows Credential Manager, macOS Keychain, Secret Service).

Every call degrades gracefully: if no usable backend exists (e.g. a headless
Linux box) ``available()`` is False, ``get`` returns None and ``set`` returns
False, and callers keep the secret where it was.
"""

from __future__ import annotations

import logging

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

log = logging.getLogger(__name__)

SERVICE = "hoard"
LEGACY_SERVICE = "money-mover"  # secrets saved before the app became Hoard


def available() -> bool:
    try:
        backend = keyring.get_keyring()
    except Exception:  # noqa: BLE001 - any backend init failure means "unavailable"
        return False
    module = type(backend).__module__
    if module.endswith((".fail", ".null")):
        return False
    return getattr(backend, "priority", 1) > 0


def get(name: str) -> str | None:
    """The secret, or None. A secret still saved under the old app name is moved
    to the new one the first time it's read."""
    if not available():
        return None
    try:
        value = keyring.get_password(SERVICE, name)
        if value is None:
            value = keyring.get_password(LEGACY_SERVICE, name)
            if value is not None:
                keyring.set_password(SERVICE, name, value)
                _delete_quietly(LEGACY_SERVICE, name)
        return value
    except KeyringError as exc:
        log.warning("Keyring read failed for %s: %s", name, exc)
        return None


def set(name: str, value: str) -> bool:  # noqa: A001 - mirrors keyring's API
    if not available():
        return False
    try:
        keyring.set_password(SERVICE, name, value)
    except KeyringError as exc:
        log.warning("Keyring write failed for %s: %s", name, exc)
        return False
    return True


def _delete_quietly(service: str, name: str) -> None:
    try:
        keyring.delete_password(service, name)
    except (PasswordDeleteError, KeyringError):
        pass


def delete(name: str) -> None:
    if not available():
        return
    _delete_quietly(SERVICE, name)
    _delete_quietly(LEGACY_SERVICE, name)


def item_key(item_id: str) -> str:
    return f"item:{item_id}"
