"""Hoard: your finances, guarded on your own computer."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("hoard")
except PackageNotFoundError:  # running from a source tree without installing
    __version__ = "0.0.0+dev"
