#!/bin/sh
# Remove Hoard from macOS or Linux. Your data is kept unless you pass --purge.
#
#   curl -LsSf https://github.com/Woodwardbr/hoard/releases/latest/download/uninstall.sh | sh
#   curl -LsSf https://github.com/Woodwardbr/hoard/releases/latest/download/uninstall.sh | sh -s -- --purge
set -eu

purge=0
[ "${1:-}" = "--purge" ] && purge=1

if command -v uv >/dev/null 2>&1; then
    bindir=$(uv tool dir --bin)
    [ -x "$bindir/hoard" ] && "$bindir/hoard" shortcuts --remove || true
    uv tool uninstall hoard || true
fi

if [ "$purge" -eq 1 ]; then
    case "$(uname -s)" in
        Darwin) data="$HOME/Library/Application Support/hoard" ;;
        *) data="${XDG_DATA_HOME:-$HOME/.local/share}/hoard" ;;
    esac
    if [ -d "$data" ]; then
        rm -r "$data"
        echo "Deleted your Hoard data in $data"
    fi
    echo "Bank keys saved in your system keychain stay until you delete 'hoard' entries there."
else
    echo "Hoard removed. Your data is still in your user data folder (run with --purge to delete it)."
fi
