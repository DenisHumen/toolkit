#!/usr/bin/env bash
#
# toolkit.sh — one entry point for every script in this repository.
#
# Discovers the scripts, checks whether this machine can run each of them, shows
# a short summary, and starts the one you pick after a single confirmation.
#
#   ./toolkit.sh                 interactive browser (arrow keys)
#   ./toolkit.sh --list          print what was discovered and exit
#   ./toolkit.sh --check         run every system check and exit
#   ./toolkit.sh --run netwatch  run one script directly
#   ./toolkit.sh --help          full help
#
# The launcher itself installs nothing and needs nothing but Python 3 (present on
# every mainstream distro; it offers to install it if it is somehow missing).
# Every script here still runs standalone — this is a convenience layer, not a
# dependency.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
APP="$SCRIPT_DIR/toolkit.py"

if [ -t 1 ]; then
    C_I=$'\033[38;5;75m'; C_W=$'\033[38;5;221m'; C_E=$'\033[38;5;203m'; C_0=$'\033[0m'
else
    C_I=''; C_W=''; C_E=''; C_0=''
fi
info() { printf '%s[*]%s %s\n' "$C_I" "$C_0" "$*"; }
warn() { printf '%s[!]%s %s\n' "$C_W" "$C_0" "$*" >&2; }
die()  { printf '%s[x]%s %s\n' "$C_E" "$C_0" "$*" >&2; exit 1; }

[ -f "$APP" ] || die "Cannot find toolkit.py next to this script ($APP)."

find_python() {
    for c in python3 python; do
        if command -v "$c" >/dev/null 2>&1; then
            "$c" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 8) else 1)' \
                >/dev/null 2>&1 && { echo "$c"; return 0; }
        fi
    done
    return 1
}

install_python() {
    warn "Python 3.8+ was not found — it is needed for the launcher itself."
    warn "Every script in this repo still runs on its own without it."
    if [ "$(id -u)" -eq 0 ]; then SUDO=""
    elif command -v sudo >/dev/null 2>&1; then SUDO="sudo"
    else die "Install Python 3 manually, or run the scripts directly (see README)."
    fi
    printf 'Install Python 3 now? [y/N] '
    read -r reply </dev/tty 2>/dev/null || reply=""
    case "$reply" in [yY]*) ;; *) die "Cancelled." ;; esac
    if   command -v apt-get >/dev/null 2>&1; then $SUDO apt-get update && $SUDO DEBIAN_FRONTEND=noninteractive apt-get install -y python3
    elif command -v dnf     >/dev/null 2>&1; then $SUDO dnf -y install python3
    elif command -v yum     >/dev/null 2>&1; then $SUDO yum -y install python3
    elif command -v pacman  >/dev/null 2>&1; then $SUDO pacman -Sy --noconfirm python
    elif command -v zypper  >/dev/null 2>&1; then $SUDO zypper -n install python3
    elif command -v apk     >/dev/null 2>&1; then $SUDO apk add --no-cache python3
    else die "Could not detect a package manager. Install Python 3 manually."
    fi
}

PY="$(find_python || true)"
if [ -z "${PY:-}" ]; then
    install_python
    PY="$(find_python || true)"
    [ -n "${PY:-}" ] || die "Python 3 still not available after the install."
fi

# Scripts are executed by name, so make sure they actually are executable.
find "$SCRIPT_DIR" -type f -name '*.sh' ! -perm -u+x -exec chmod +x {} + 2>/dev/null || true

exec "$PY" "$APP" "$@"
