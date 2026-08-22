#!/usr/bin/env bash
#
# loadtest.sh — launcher for the authorized load / WAF / rate-limit tester.
#
# toolkit-name: loadtest — WAF / rate-limit tester
# toolkit-kind: tool
# toolkit-category: Diagnostics
# toolkit-summary: Generates authorized HTTP load against your own site and measures how much of it your filtering blocks.
# toolkit-os: any
# toolkit-root: no
# toolkit-needs: python3
# toolkit-danger: Authorized testing only — run it against systems you own or may test.
# toolkit-docs: linux/loadtest/README.md
# toolkit-order: 40
# toolkit-arg: --url | Target URL you own or are authorized to test | text
# toolkit-arg: --duration | How long to run: 30s, 5m, 1h | text
# toolkit-arg: --concurrency | Parallel workers | number
# toolkit-arg: --proxy | Path to a proxy list file | path
#
# Ensures Python 3 is present (installing it via the distro package manager if
# needed) and then runs loadtest.py, forwarding all arguments to it.
#
#   ./loadtest.sh                         interactive TUI menu
#   ./loadtest.sh --url https://my.site --proxy:/path/proxies.txt \
#                 --duration 60s --delay 0.1 --concurrency 50 --yes
#   ./loadtest.sh --help                  full option list
#
# Authorized testing only — run against systems you own or are permitted to test.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
APP="$SCRIPT_DIR/loadtest.py"

# ---- pretty output ----
if [ -t 1 ]; then
    C_I=$'\033[1;34m'; C_W=$'\033[1;33m'; C_E=$'\033[1;31m'; C_0=$'\033[0m'
else
    C_I=''; C_W=''; C_E=''; C_0=''
fi
info() { printf '%s[*]%s %s\n' "$C_I" "$C_0" "$*"; }
warn() { printf '%s[!]%s %s\n' "$C_W" "$C_0" "$*" >&2; }
die()  { printf '%s[x]%s %s\n' "$C_E" "$C_0" "$*" >&2; exit 1; }

[ -f "$APP" ] || die "Cannot find loadtest.py next to this script ($APP)."

# ---- privilege helper (only needed to install python) ----
if [ "$(id -u)" -eq 0 ]; then SUDO=""; else SUDO="sudo"; fi

find_python() {
    for c in python3 python; do
        if command -v "$c" >/dev/null 2>&1; then
            "$c" -c 'import sys; raise SystemExit(0 if sys.version_info[0] == 3 else 1)' \
                >/dev/null 2>&1 && { echo "$c"; return 0; }
        fi
    done
    return 1
}

install_python() {
    warn "Python 3 not found — attempting to install it."
    command -v sudo >/dev/null 2>&1 || [ "$(id -u)" -eq 0 ] || die "Need root/sudo to install Python 3."
    if [ -r /etc/os-release ]; then . /etc/os-release; fi
    case "${ID:-}${ID_LIKE:-}" in
        *debian*|*ubuntu*) $SUDO apt-get update && $SUDO DEBIAN_FRONTEND=noninteractive apt-get install -y python3 ;;
        *fedora*|*rhel*|*centos*) $SUDO dnf -y install python3 ;;
        *)
            if   command -v apt-get >/dev/null 2>&1; then $SUDO apt-get update && $SUDO apt-get install -y python3
            elif command -v dnf     >/dev/null 2>&1; then $SUDO dnf -y install python3
            elif command -v yum     >/dev/null 2>&1; then $SUDO yum -y install python3
            elif command -v pacman  >/dev/null 2>&1; then $SUDO pacman -Sy --noconfirm python
            elif command -v apk     >/dev/null 2>&1; then $SUDO apk add --no-cache python3
            else die "Could not detect a package manager. Install Python 3 manually."
            fi ;;
    esac
}

PY="$(find_python || true)"
if [ -z "${PY:-}" ]; then
    install_python
    PY="$(find_python || true)"
    [ -n "${PY:-}" ] || die "Python 3 still not available after install."
fi

exec "$PY" "$APP" "$@"
