#!/usr/bin/env bash
#
# install-pingvin-share.sh — universal Pingvin Share installer via Docker.
#
# Deploys Pingvin Share (a self-hosted file-sharing platform) with Docker
# Compose on any of the common Linux families, ready to be reached over the
# internet through your own domain with automatic HTTPS.
#
# What it does:
#   1. Detects the distro family from /etc/os-release (Ubuntu/Debian, Fedora/RHEL).
#   2. Ensures Docker Engine + Compose v2 are present — if not, it runs
#      install-docker.sh from this same repository (the local sibling file if
#      available, otherwise it is downloaded from GitHub).
#   3. Writes a docker-compose stack into the install directory:
#        * default  : Pingvin Share + Caddy reverse proxy with automatic
#                      Let's Encrypt HTTPS for your --domain (ports 80/443).
#        * --no-proxy: Pingvin Share only, published directly on --port (no TLS).
#   4. Opens the required ports in the firewall (ufw on apt distros, firewalld
#      on dnf distros) without locking out SSH, and labels volumes for SELinux.
#   5. Pulls the images and starts the stack with `docker compose up -d`.
#
# Usage:
#   ./install-pingvin-share.sh --domain share.example.com --email you@example.com
#   ./install-pingvin-share.sh --no-proxy --port 3000
#   ./install-pingvin-share.sh --domain share.example.com --dry-run
#
# Options:
#   -d, --domain <fqdn>   domain to serve Pingvin Share on (required unless --no-proxy)
#   -e, --email  <addr>   email for Let's Encrypt / ACME (recommended in proxy mode)
#   -p, --port   <port>   host port for direct mode (default: 3000, only with --no-proxy)
#       --dir    <path>   install directory (default: /opt/pingvin-share)
#       --image  <ref>    container image (default: stonith404/pingvin-share)
#       --no-proxy        skip Caddy/HTTPS, publish Pingvin Share directly on --port
#       --no-firewall     do not touch the firewall
#   -n, --dry-run         preview every step, change NOTHING
#   -y, --yes             non-interactive (assume "yes")
#   -h, --help            show this help
#
# Run as root, or as a user with sudo privileges.
#
set -euo pipefail

DRY_RUN=0; ASSUME_YES=0; USE_PROXY=1; CONFIGURE_FW=1
DOMAIN=""; EMAIL=""; PORT="3000"
INSTALL_DIR="/opt/pingvin-share"
IMAGE="stonith404/pingvin-share"

# Where to fetch install-docker.sh from if it is not sitting next to this script.
REPO_RAW="https://raw.githubusercontent.com/DenisHumen/toolkit/main/linux"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"

while [ $# -gt 0 ]; do
    case "$1" in
        -d|--domain)  DOMAIN="${2:?--domain requires a value}"; shift ;;
        --domain=*)   DOMAIN="${1#*=}" ;;
        -e|--email)   EMAIL="${2:?--email requires a value}"; shift ;;
        --email=*)    EMAIL="${1#*=}" ;;
        -p|--port)    PORT="${2:?--port requires a value}"; shift ;;
        --port=*)     PORT="${1#*=}" ;;
        --dir)        INSTALL_DIR="${2:?--dir requires a value}"; shift ;;
        --dir=*)      INSTALL_DIR="${1#*=}" ;;
        --image)      IMAGE="${2:?--image requires a value}"; shift ;;
        --image=*)    IMAGE="${1#*=}" ;;
        --no-proxy)   USE_PROXY=0 ;;
        --no-firewall) CONFIGURE_FW=0 ;;
        -n|--dry-run) DRY_RUN=1 ;;
        -y|--yes)     ASSUME_YES=1 ;;
        -h|--help)    grep '^#' "$0" | sed 's/^#\{1,\} \{0,1\}//'; exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

# ---- pretty output (colours only on a real terminal) ----
if [ -t 1 ]; then
    C_I=$'\033[1;34m'; C_OK=$'\033[1;32m'; C_W=$'\033[1;33m'; C_E=$'\033[1;31m'; C_0=$'\033[0m'
else
    C_I=''; C_OK=''; C_W=''; C_E=''; C_0=''
fi
info() { printf '%s[*]%s %s\n' "$C_I"  "$C_0" "$*"; }
ok()   { printf '%s[+]%s %s\n' "$C_OK" "$C_0" "$*"; }
warn() { printf '%s[!]%s %s\n' "$C_W"  "$C_0" "$*" >&2; }
die()  { printf '%s[x]%s %s\n' "$C_E"  "$C_0" "$*" >&2; exit 1; }

# Execute a command, or just print it in --dry-run mode.
run() {
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '    DRY: %s\n' "$*"
    else
        info "run: $*"
        eval "$@"
    fi
}

# Write a file from stdin, or just preview it in --dry-run mode.
write_file() {
    local path="$1"
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '    DRY: write %s:\n' "$path"
        sed 's/^/        | /'
    else
        info "writing $path"
        $SUDO tee "$path" >/dev/null
    fi
}

# ---- privilege helper ----
if [ "$(id -u)" -eq 0 ]; then
    SUDO=""
else
    command -v sudo >/dev/null 2>&1 || die "Run as root, or install 'sudo' first."
    SUDO="sudo"
fi

# ---- validate arguments ----
if [ "$USE_PROXY" -eq 1 ] && [ -z "$DOMAIN" ]; then
    die "A --domain is required for HTTPS via the reverse proxy. Pass --domain <fqdn>, or use --no-proxy to publish Pingvin Share directly on a port without TLS."
fi
case "$PORT" in
    ''|*[!0-9]*) die "Invalid --port: '$PORT' (must be a number)." ;;
esac

# ---- detect distribution ----
[ -r /etc/os-release ] || die "Cannot read /etc/os-release — unsupported system."
# shellcheck disable=SC1091
. /etc/os-release
OS_ID="${ID:-}"
OS_LIKE="${ID_LIKE:-}"

PM=""            # package manager: apt | dnf
FW=""            # firewall front-end: ufw | firewalld
case "$OS_ID" in
    ubuntu|debian)                  PM="apt"; FW="ufw" ;;
    fedora|rhel|centos|rocky|almalinux) PM="dnf"; FW="firewalld" ;;
    *)
        case " $OS_LIKE " in
            *ubuntu*|*debian*)        PM="apt"; FW="ufw" ;;
            *fedora*|*rhel*|*centos*) PM="dnf"; FW="firewalld" ;;
        esac ;;
esac
[ -n "$PM" ] || die "Unsupported distribution: ${PRETTY_NAME:-$OS_ID}. Supported: Ubuntu/Debian, Fedora/RHEL/CentOS."

# ---- SELinux-aware volume label (no-op on systems without SELinux) ----
VOL_OPT=""
if command -v getenforce >/dev/null 2>&1 && [ "$(getenforce 2>/dev/null)" != "Disabled" ]; then
    VOL_OPT=":z"
fi

# ---- ensure Docker + Compose v2 ----
ensure_docker() {
    if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
        ok "Docker Engine + Compose v2 already present."
        return
    fi
    warn "Docker (or Compose v2) not found — installing it via install-docker.sh from this repo."
    # Parent already confirmed, so the sub-install always runs non-interactively.
    local extra=" --yes"
    [ "$DRY_RUN" -eq 1 ] && extra="$extra --dry-run"
    if [ -f "$SCRIPT_DIR/install-docker.sh" ]; then
        info "Using local installer: $SCRIPT_DIR/install-docker.sh"
        run "bash '$SCRIPT_DIR/install-docker.sh'$extra"
    else
        local tmp; tmp="$(mktemp)"
        info "Downloading installer from $REPO_RAW/install-docker.sh"
        run "curl -fsSL '$REPO_RAW/install-docker.sh' -o '$tmp'"
        run "bash '$tmp'$extra"
    fi
    if [ "$DRY_RUN" -eq 0 ] && ! docker compose version >/dev/null 2>&1; then
        die "Docker still not usable in this shell. If it was just installed you may need to re-login (newgrp docker) and re-run this script."
    fi
}

# ---- best-effort DNS sanity check (non-fatal) ----
dns_check() {
    [ "$USE_PROXY" -eq 1 ] || return 0
    [ "$DRY_RUN" -eq 0 ] || return 0
    command -v getent >/dev/null 2>&1 || return 0
    local resolved public
    resolved="$(getent ahostsv4 "$DOMAIN" 2>/dev/null | awk 'NR==1{print $1}')" || true
    public="$(curl -fsSL --max-time 5 https://api.ipify.org 2>/dev/null)" || true
    if [ -n "$resolved" ] && [ -n "$public" ] && [ "$resolved" != "$public" ]; then
        warn "DNS: $DOMAIN -> $resolved, but this host's public IP looks like $public."
        warn "     HTTPS issuance will fail until the domain's A/AAAA record points here."
    elif [ -z "$resolved" ]; then
        warn "DNS: could not resolve $DOMAIN — make sure its A/AAAA record points to this server."
    fi
}

# ---- compose + config generation ----
write_stack() {
    run "$SUDO mkdir -p '$INSTALL_DIR/data/images'"
    # Match the container's default PUID/PGID so it can write to the data dir.
    run "$SUDO chown -R 1000:1000 '$INSTALL_DIR/data'"

    if [ "$USE_PROXY" -eq 1 ]; then
        write_file "$INSTALL_DIR/docker-compose.yml" <<EOF
services:
  pingvin-share:
    image: $IMAGE
    container_name: pingvin-share
    restart: unless-stopped
    environment:
      - TRUST_PROXY=true
      - PUID=1000
      - PGID=1000
    volumes:
      - "./data:/opt/app/backend/data$VOL_OPT"
      - "./data/images:/opt/app/frontend/public/img$VOL_OPT"
    expose:
      - "3000"
    networks:
      - pingvin

  caddy:
    image: caddy:2-alpine
    container_name: pingvin-caddy
    restart: unless-stopped
    depends_on:
      - pingvin-share
    ports:
      - "80:80"
      - "443:443"
      - "443:443/udp"
    volumes:
      - "./Caddyfile:/etc/caddy/Caddyfile$VOL_OPT"
      - "caddy_data:/data"
      - "caddy_config:/config"
    networks:
      - pingvin

networks:
  pingvin:

volumes:
  caddy_data:
  caddy_config:
EOF

        local email_block=""
        [ -n "$EMAIL" ] && email_block=$'{\n    email '"$EMAIL"$'\n}\n\n'
        write_file "$INSTALL_DIR/Caddyfile" <<EOF
$email_block$DOMAIN {
    reverse_proxy pingvin-share:3000
}
EOF
    else
        write_file "$INSTALL_DIR/docker-compose.yml" <<EOF
services:
  pingvin-share:
    image: $IMAGE
    container_name: pingvin-share
    restart: unless-stopped
    environment:
      - TRUST_PROXY=false
      - PUID=1000
      - PGID=1000
    ports:
      - "$PORT:3000"
    volumes:
      - "./data:/opt/app/backend/data$VOL_OPT"
      - "./data/images:/opt/app/frontend/public/img$VOL_OPT"
EOF
    fi
}

# ---- firewall ----
configure_firewall() {
    [ "$CONFIGURE_FW" -eq 1 ] || { info "Skipping firewall configuration (--no-firewall)."; return; }
    if [ "$FW" = "ufw" ]; then
        command -v ufw >/dev/null 2>&1 || run "$SUDO apt-get update && DEBIAN_FRONTEND=noninteractive $SUDO apt-get install -y ufw"
        info "Allowing SSH first so we don't lock ourselves out..."
        run "$SUDO ufw allow OpenSSH || $SUDO ufw allow 22/tcp"
        if [ "$USE_PROXY" -eq 1 ]; then
            run "$SUDO ufw allow 80/tcp"
            run "$SUDO ufw allow 443/tcp"
            run "$SUDO ufw allow 443/udp"
        else
            run "$SUDO ufw allow ${PORT}/tcp"
        fi
        run "$SUDO ufw --force enable"
    else
        command -v firewall-cmd >/dev/null 2>&1 || run "$SUDO dnf -y install firewalld"
        run "$SUDO systemctl enable --now firewalld"
        if [ "$USE_PROXY" -eq 1 ]; then
            run "$SUDO firewall-cmd --permanent --add-service=http"
            run "$SUDO firewall-cmd --permanent --add-service=https"
        else
            run "$SUDO firewall-cmd --permanent --add-port=${PORT}/tcp"
        fi
        run "$SUDO firewall-cmd --reload"
    fi
}

# ---- bring the stack up ----
start_stack() {
    run "$SUDO docker compose -f '$INSTALL_DIR/docker-compose.yml' pull"
    run "$SUDO docker compose -f '$INSTALL_DIR/docker-compose.yml' up -d"
}

# ---- banner + confirmation ----
echo
echo "============================================================"
echo " Pingvin Share installer$( [ "$DRY_RUN" -eq 1 ] && echo '   *** DRY-RUN ***' )"
echo "============================================================"
echo "  Distribution : ${PRETTY_NAME:-$OS_ID}  (pkg: $PM, fw: $FW)"
echo "  Image        : $IMAGE"
echo "  Install dir  : $INSTALL_DIR"
if [ "$USE_PROXY" -eq 1 ]; then
echo "  Mode         : reverse proxy (Caddy) + automatic HTTPS"
echo "  Domain       : $DOMAIN"
echo "  ACME email   : ${EMAIL:-<none>}"
echo "  Ports opened : 80/tcp, 443/tcp, 443/udp"
else
echo "  Mode         : direct port publish (no TLS)"
echo "  Host port    : $PORT  ->  container 3000"
echo "  Ports opened : ${PORT}/tcp"
fi
echo "  Firewall     : $( [ "$CONFIGURE_FW" -eq 1 ] && echo "configure ($FW)" || echo "leave untouched" )"
echo "  SELinux label: $( [ -n "$VOL_OPT" ] && echo "yes (:z)" || echo "no" )"
echo "============================================================"
echo

if [ "$DRY_RUN" -eq 0 ] && [ "$ASSUME_YES" -eq 0 ]; then
    printf 'Proceed with the installation? [y/N] '
    read -r ans
    case "$ans" in
        y|Y|yes|YES) ;;
        *) die "Aborted by user." ;;
    esac
fi

ensure_docker
dns_check
write_stack
configure_firewall
start_stack

if [ "$DRY_RUN" -eq 1 ]; then
    ok "Dry-run complete. Nothing was changed."
    exit 0
fi

echo
ok "Pingvin Share is up."
if [ "$USE_PROXY" -eq 1 ]; then
    info "Open:  https://$DOMAIN"
    info "Caddy will obtain a Let's Encrypt certificate on first request — give it"
    info "a few seconds, and make sure $DOMAIN resolves to this server and that"
    info "ports 80/443 are reachable from the internet."
else
    info "Open:  http://<server-ip>:$PORT   (no TLS — put it behind a proxy for internet use)"
fi
echo
info "First account you register becomes the admin."
info "Then open Configuration -> set the App URL$( [ "$USE_PROXY" -eq 1 ] && echo " to https://$DOMAIN" ) and the max share size."
info "Manage the stack from $INSTALL_DIR:"
info "  docker compose logs -f          # follow logs"
info "  docker compose pull && docker compose up -d   # update to the latest image"
info "  docker compose down             # stop"
