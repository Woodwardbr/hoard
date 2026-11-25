from __future__ import annotations

from hoard import config


def test_data_dir_honours_override(tmp_path, monkeypatch):
    monkeypatch.setenv("HOARD_HOME", str(tmp_path / "home"))
    assert config.data_dir() == tmp_path / "home"


def test_env_file_in_cwd_wins_over_data_dir(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text("X=1\n")
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setenv("HOARD_HOME", str(home))
    monkeypatch.chdir(work)

    assert config.find_env_file() == home / ".env"

    (work / ".env").write_text("X=2\n")
    assert config.find_env_file() == work / ".env"


def test_no_env_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HOARD_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    assert config.find_env_file() is None


def test_relative_db_path_resolves_against_env_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BASE_DIR", tmp_path)
    default = tmp_path / "default.db"

    assert config._resolve_path("", default) == default
    assert config._resolve_path("data/mm.db", default) == tmp_path / "data" / "mm.db"
    assert config._resolve_path(str(tmp_path / "abs.db"), default) == tmp_path / "abs.db"


def test_agent_writes_default(monkeypatch):
    for k in ("AGENT_ALLOW_WRITES", "AGENT_API_TOKEN", "MUSE_API_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    assert config._agent_allow_writes() is False

    monkeypatch.setenv("MUSE_API_TOKEN", "t")  # legacy Muse setup keeps writes
    assert config._agent_allow_writes() is True

    monkeypatch.setenv("AGENT_API_TOKEN", "t")  # new setup: opt in explicitly
    assert config._agent_allow_writes() is False

    monkeypatch.setenv("AGENT_ALLOW_WRITES", "1")
    assert config._agent_allow_writes() is True
    monkeypatch.setenv("AGENT_ALLOW_WRITES", "0")
    monkeypatch.delenv("AGENT_API_TOKEN")
    assert config._agent_allow_writes() is False


def test_legacy_data_dir_is_adopted(tmp_path, monkeypatch):
    monkeypatch.delenv("HOARD_HOME", raising=False)
    monkeypatch.setattr(config, "user_data_dir", lambda name, _author: str(tmp_path / name))
    old = tmp_path / "money-mover"
    old.mkdir()
    (old / "money-mover.db").write_bytes(b"db")

    config._adopt_legacy_data_dir()

    assert not old.exists()
    assert (tmp_path / "hoard" / "hoard.db").read_bytes() == b"db"
    config._adopt_legacy_data_dir()  # idempotent
