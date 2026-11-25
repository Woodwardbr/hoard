#!/bin/sh
# Install or update Hoard on macOS or Linux.
#
#   curl -LsSf https://github.com/Woodwardbr/hoard/releases/latest/download/install.sh | sh
#
# Installs uv (https://docs.astral.sh/uv/) if needed, then the latest Hoard release
# with its own Python, and adds a Hoard app shortcut. Run it again to update.
# Your data is kept. HOARD_VERSION=v0.1.0 installs a specific release.
set -eu

REPO="Woodwardbr/hoard"

say() { printf '\033[1m%s\033[0m\n' "$*"; }
fail() { printf 'Error: %s\n' "$*" >&2; exit 1; }

command -v curl >/dev/null 2>&1 || fail "curl is required."

if ! command -v uv >/dev/null 2>&1; then
    say "Installing uv (the tool that installs Hoard and its Python)…"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    export PATH
    command -v uv >/dev/null 2>&1 || fail "uv was installed but isn't on PATH; open a new terminal and run this again."
fi

if [ -n "${HOARD_WHEEL:-}" ]; then
    wheel="$HOARD_WHEEL"  # testing: install a locally built wheel
elif [ -n "${HOARD_VERSION:-}" ]; then
    api="https://api.github.com/repos/$REPO/releases/tags/$HOARD_VERSION"
else
    api="https://api.github.com/repos/$REPO/releases/latest"
fi
if [ -z "${wheel:-}" ]; then
    say "Finding the latest Hoard release…"
    wheel=$(curl -fsSL "$api" | grep -o '"browser_download_url": *"[^"]*\.whl"' | head -n 1 | sed 's/.*"\(https[^"]*\)"/\1/')
    [ -n "$wheel" ] || fail "Couldn't find a Hoard release to download from $api"
fi

say "Installing ${wheel##*/}…"
uv tool install --force --python ">=3.13" "$wheel"

bindir=$(uv tool dir --bin)
say "Adding the Hoard shortcut…"
"$bindir/hoard" shortcuts

case ":$PATH:" in
    *":$bindir:"*) ;;
    *) uv tool update-shell >/dev/null 2>&1 || true ;;
esac

say "Done! Open Hoard from your applications (macOS: ~/Applications/Hoard), or run: hoard"
