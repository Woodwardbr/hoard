from __future__ import annotations

import plistlib
import sys

import pytest

from hoard import shortcuts


@pytest.mark.skipif(sys.platform in ("win32", "darwin"), reason="Linux desktop entry")
def test_linux_desktop_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    made = shortcuts.run()
    entry = tmp_path / "applications" / "hoard.desktop"
    assert made == [entry]
    text = entry.read_text()
    assert "Name=Hoard" in text and f'Exec="{sys.executable}" -m hoard' in text
    assert "hoard.png" in text
    shortcuts.run(remove=True)
    assert not entry.exists()


def test_mac_app_bundle(tmp_path, monkeypatch):
    monkeypatch.setattr(shortcuts.Path, "home", classmethod(lambda cls: tmp_path))
    [app] = shortcuts._mac(remove=False)
    plist = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
    assert plist["CFBundleExecutable"] == "Hoard"
    launcher = app / "Contents" / "MacOS" / "Hoard"
    assert "-m hoard" in launcher.read_text() and launcher.stat().st_mode & 0o111
    assert (app / "Contents" / "Resources" / "hoard.icns").exists()
    shortcuts._mac(remove=True)
    assert not app.exists()


def test_icons_ship_with_package():
    for name in ("hoard.ico", "hoard.icns", "hoard.png", "hoard.svg"):
        assert (shortcuts.ICONS / name).stat().st_size > 0
