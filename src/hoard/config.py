from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from platformdirs import user_data_dir

APP_NAME = "hoard"
LEGACY_APP_NAME = "money-mover"  # the app's name before it became Hoard


def data_dir() -> Path:
    """Per-user folder for the database, backups and (optionally) ``.env``.

    ``HOARD_HOME`` overrides it; otherwise it is the OS convention, e.g.
    ``%APPDATA%\\hoard`` or ``~/Library/Application Support/hoard``.
    """
    override = os.environ.get("HOARD_HOME")
    return Path(override).expanduser() if override else Path(user_data_dir(APP_NAME, False))


def _adopt_legacy_data_dir() -> None:
    """Move a pre-rename ``money-mover`` data folder to ``hoard``, once.

    Only when HOARD_HOME isn't set, the new folder doesn't exist yet and the old
    one does. The database inside is renamed to match the new default name.
    """
    if os.environ.get("HOARD_HOME"):
        return
    new = Path(user_data_dir(APP_NAME, False))
    old = Path(user_data_dir(LEGACY_APP_NAME, False))
    if new.exists() or not old.is_dir():
        return
    old.rename(new)
    legacy_db = new / f"{LEGACY_APP_NAME}.db"
    if legacy_db.exists() and not (new / f"{APP_NAME}.db").exists():
        legacy_db.rename(new / f"{APP_NAME}.db")


def find_env_file() -> Path | None:
    """The ``.env`` in the current folder (dev checkout) wins over the data folder's."""
    for candidate in (Path.cwd() / ".env", data_dir() / ".env"):
        if candidate.is_file():
            return candidate
    return None


_adopt_legacy_data_dir()
ENV_FILE = find_env_file()
if ENV_FILE is not None:
    load_dotenv(ENV_FILE)

# Relative paths in .env (e.g. DB_PATH=data/hoard.db) are relative to the
# folder the .env lives in, not to wherever the app happened to be launched.
BASE_DIR = ENV_FILE.parent if ENV_FILE is not None else data_dir()


@dataclass(frozen=True)
class Settings:
    plaid_client_id: str
    plaid_secret: str
    plaid_env: str
    app_host: str
    app_port: int
    db_path: Path
    agent_api_token: str
    agent_host: str
    agent_port: int
    agent_allow_writes: bool

    @property
    def plaid_env_value(self) -> str:
        return self.plaid_env.strip().lower()


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _plaid_secret(env: str) -> str:
    """Env-specific secret if set, else the generic PLAID_SECRET.

    ``env`` must already be normalized (lowercased/stripped).
    """
    specific = (
        _env("PLAID_SECRET_PRODUCTION")
        if env == "production"
        else _env("PLAID_SECRET_SANDBOX")
    )
    return specific or _env("PLAID_SECRET")


def _from_env_or_keystore(env_value: str, keystore_name: str) -> tuple[str, str]:
    """(value, source) where source is "env", "keychain" or "" (not set)."""
    if env_value:
        return env_value, "env"
    from . import keystore

    stored = keystore.get(keystore_name)
    return (stored, "keychain") if stored else ("", "")


def _resolve_path(raw: str, default: Path) -> Path:
    if not raw:
        return default
    path = Path(raw).expanduser()
    return path if path.is_absolute() else BASE_DIR / path


def agent_writes_explicitly_allowed() -> bool:
    """True only if AGENT_ALLOW_WRITES is set to a truthy value. The MCP server uses this."""
    return _env("AGENT_ALLOW_WRITES").strip().lower() in ("1", "true", "yes", "on")


def _agent_allow_writes() -> bool:
    """Writes for the HTTP agent API: AGENT_ALLOW_WRITES if set. Otherwise writes
    stay on only for a legacy MUSE_* setup (Muse records payments), and are off
    for new AGENT_* setups."""
    if _env("AGENT_ALLOW_WRITES").strip():
        return agent_writes_explicitly_allowed()
    return bool(_env("MUSE_API_TOKEN")) and not _env("AGENT_API_TOKEN")


_PLAID_ENV = _env("PLAID_ENV", "sandbox").strip().lower()
_CLIENT_ID, _CLIENT_ID_SOURCE = _from_env_or_keystore(_env("PLAID_CLIENT_ID"), "plaid_client_id")
_SECRET, _SECRET_SOURCE = _from_env_or_keystore(
    _plaid_secret(_PLAID_ENV), f"plaid_secret_{_PLAID_ENV}"
)
# Where the Plaid credentials came from, for the settings page.
PLAID_CREDENTIALS_SOURCE = _SECRET_SOURCE or _CLIENT_ID_SOURCE

settings = Settings(
    plaid_client_id=_CLIENT_ID,
    plaid_secret=_SECRET,
    plaid_env=_PLAID_ENV,
    app_host=_env("APP_HOST", "127.0.0.1"),
    app_port=int(_env("APP_PORT", "8000")),
    db_path=_resolve_path(_env("DB_PATH"), data_dir() / "hoard.db"),
    # AGENT_* names, with the older MUSE_* names still honoured.
    agent_api_token=_env("AGENT_API_TOKEN") or _env("MUSE_API_TOKEN"),
    agent_host=_env("AGENT_HOST") or _env("MUSE_HOST"),
    agent_port=int(_env("AGENT_PORT") or _env("MUSE_PORT") or "8001"),
    agent_allow_writes=_agent_allow_writes(),
)
