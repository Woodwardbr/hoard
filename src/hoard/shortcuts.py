"""Desktop / Start menu / Applications shortcuts for Hoard (``hoard shortcuts``).

The installers call this after installing Hoard, so shortcut logic lives in one
place. Shortcuts launch Hoard with the interpreter Hoard is installed into
(``python -m hoard``), which stays put across ``uv tool`` upgrades.

* Windows: "Hoard" on the Desktop and Start menu (no console window), plus
  "Update Hoard" in the Start menu.
* macOS: ~/Applications/Hoard.app.
* Linux: a .desktop entry in ~/.local/share/applications.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__

ICONS = Path(__file__).parent / "static" / "icons"
INSTALL_PS1 = "https://github.com/Woodwardbr/hoard/releases/latest/download/install.ps1"


def _python(windowless: bool = False) -> Path:
    exe = Path(sys.executable)
    if windowless and os.name == "nt":
        pythonw = exe.with_name("pythonw.exe")
        if pythonw.exists():
            return pythonw
    return exe


# --- Windows -------------------------------------------------------------------

def _ps_quote(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _windows_folders() -> tuple[Path, Path]:
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "[Environment]::GetFolderPath('Desktop'); [Environment]::GetFolderPath('Programs')"],
        capture_output=True, text=True, check=True,
    ).stdout.split("\n")
    return Path(out[0].strip()), Path(out[1].strip())


def _windows_shortcut(path: Path, target: Path, args: str, description: str) -> None:
    script = (
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut(" + _ps_quote(path) + ");"
        f"$s.TargetPath = {_ps_quote(target)};"
        f"$s.Arguments = {_ps_quote(args)};"
        f"$s.WorkingDirectory = {_ps_quote(Path.home())};"
        f"$s.IconLocation = {_ps_quote(ICONS / 'hoard.ico')};"
        f"$s.Description = {_ps_quote(description)};"
        "$s.Save()"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", script], check=True)


def _windows(remove: bool) -> list[Path]:
    desktop, programs = _windows_folders()
    app_links = [desktop / "Hoard.lnk", programs / "Hoard.lnk"]
    update_link = programs / "Update Hoard.lnk"
    if remove:
        for p in [*app_links, update_link]:
            p.unlink(missing_ok=True)
        return []
    for link in app_links:
        _windows_shortcut(link, _python(windowless=True), "-m hoard", "Open Hoard")
    update_cmd = f"-NoProfile -ExecutionPolicy Bypass -Command \"irm {INSTALL_PS1} | iex\""
    _windows_shortcut(
        update_link,
        Path(os.environ.get("SystemRoot", r"C:\Windows"))
        / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe",
        update_cmd,
        "Update Hoard to the latest version",
    )
    return [*app_links, update_link]


# --- macOS -----------------------------------------------------------------------

def _mac_app() -> Path:
    return Path.home() / "Applications" / "Hoard.app"


def _mac(remove: bool) -> list[Path]:
    app = _mac_app()
    if app.exists():
        shutil.rmtree(app)
    if remove:
        return []
    macos = app / "Contents" / "MacOS"
    resources = app / "Contents" / "Resources"
    macos.mkdir(parents=True)
    resources.mkdir(parents=True)
    shutil.copy(ICONS / "hoard.icns", resources / "hoard.icns")
    log = Path.home() / "Library" / "Logs" / "Hoard.log"
    launcher = macos / "Hoard"
    launcher.write_text(
        "#!/bin/sh\n"
        f'mkdir -p "{log.parent}"\n'
        f'exec "{_python()}" -m hoard >> "{log}" 2>&1\n'
    )
    launcher.chmod(0o755)
    with open(app / "Contents" / "Info.plist", "wb") as fh:
        plistlib.dump(
            {
                "CFBundleName": "Hoard",
                "CFBundleDisplayName": "Hoard",
                "CFBundleIdentifier": "io.github.woodwardbr.hoard",
                "CFBundleExecutable": "Hoard",
                "CFBundleIconFile": "hoard.icns",
                "CFBundlePackageType": "APPL",
                "CFBundleShortVersionString": __version__,
                "LSMinimumSystemVersion": "11.0",
            },
            fh,
        )
    return [app]


# --- Linux -----------------------------------------------------------------------

def _linux_desktop_file() -> Path:
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "applications" / "hoard.desktop"


def _linux(remove: bool) -> list[Path]:
    entry = _linux_desktop_file()
    if remove:
        entry.unlink(missing_ok=True)
        return []
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Hoard\n"
        "Comment=Your finances, guarded on your own computer\n"
        f'Exec="{_python()}" -m hoard\n'
        f"Icon={ICONS / 'hoard.png'}\n"
        "Terminal=false\n"
        "Categories=Office;Finance;\n"
    )
    entry.chmod(0o755)
    return [entry]


def run(remove: bool = False) -> list[Path]:
    if os.name == "nt":
        made = _windows(remove)
    elif sys.platform == "darwin":
        made = _mac(remove)
    else:
        made = _linux(remove)
    if remove:
        print("Removed Hoard shortcuts.")
    else:
        for p in made:
            print(f"Created {p}")
    return made
