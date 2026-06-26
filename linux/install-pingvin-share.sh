#!/usr/bin/env bash
#
# install-pingvin-share.sh — universal Pingvin Share installer via Docker.
#
# Deploys Pingvin Share (a self-hosted file-sharing platform) with Docker
# Compose on any of the common Linux families, ready to be reached over the
# internet through your own domain with automatic HTTPS — and lets you
# reinstall, uninstall or check its status with the same script.
#
# What it does (install, the default action):
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
#   5. Pulls the images, starts the stack and verifies the TLS certificate,
#      printing actionable diagnostics if Let's Encrypt cannot reach the host.
#
# Actions (pick one; default is install):
#       --reinstall       tear the stack down and install it again from scratch
#       --uninstall       stop and remove the stack (keeps ./data unless --purge-data)
#       --status          show container, certificate and DNS/reachability status
#
# Install options:
#   -d, --domain <fqdn>   domain to serve Pingvin Share on (required unless --no-proxy)
#   -e, --email  <addr>   email for Let's Encrypt / ACME (recommended in proxy mode)
#   -p, --port   <port>   host port for direct mode (default: 3000, only with --no-proxy)
#       --dir    <path>   install directory (default: /opt/pingvin-share)
#       --image  <ref>    container image (default: stonith404/pingvin-share)
#       --no-proxy        skip Caddy/HTTPS, publish Pingvin Share directly on --port
#       --no-firewall     do not touch the firewall
#       --staging         use the Let's Encrypt STAGING CA (no rate limits, for testing)
#       --self-signed     use Caddy's internal CA (instant HTTPS, browser warning;
#                         handy behind a CDN or when public ACME is impossible)
#       --purge-data      with --uninstall/--reinstall, also delete uploads + database
#   -n, --dry-run         preview every step, change NOTHING
#   -y, --yes             non-interactive (assume "yes")
#   -h, --help            show this help
#
# Examples:
#   ./install-pingvin-share.sh --domain share.example.com --email you@example.com
#   ./install-pingvin-share.sh --reinstall --domain share.example.com --staging
#   ./install-pingvin-share.sh --reinstall --self-signed --domain share.example.com
#   ./install-pingvin-share.sh --status --domain share.example.com
#   ./install-pingvin-share.sh --uninstall --purge-data
#
# Run as root, or as a user with sudo privileges.
#
set -euo pipefail

ACTION="install"   # install | reinstall | uninstall | status
DRY_RUN=0; ASSUME_YES=0; USE_PROXY=1; CONFIGURE_FW=1
STAGING=0; SELF_SIGNED=0; PURGE_DATA=0
DOMAIN=""; EMAIL=""; PORT="3000"
INSTALL_DIR="/opt/pingvin-share"
IMAGE="stonith404/pingvin-share"

# Where to fetch install-docker.sh from if it is not sitting next to this script.
REPO_RAW="https://raw.githubusercontent.com/DenisHumen/toolkit/main/linux"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"

while [ $# -gt 0 ]; do
    case "$1" in
        --reinstall)  ACTION="reinstall" ;;
        --uninstall)  ACTION="uninstall" ;;
        --status)     ACTION="status" ;;
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
        --staging)    STAGING=1 ;;
        --self-signed) SELF_SIGNED=1 ;;
        --purge-data) PURGE_DATA=1 ;;
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

# Compose wrapper bound to the install dir's file.
COMPOSE="$SUDO docker compose -f '$INSTALL_DIR/docker-compose.yml'"

# ---- validate arguments ----
if [ "$ACTION" = "install" ] || [ "$ACTION" = "reinstall" ]; then
    if [ "$USE_PROXY" -eq 1 ] && [ -z "$DOMAIN" ]; then
        die "A --domain is required for HTTPS via the reverse proxy. Pass --domain <fqdn>, or use --no-proxy to publish Pingvin Share directly on a port without TLS."
    fi
    case "$PORT" in
        ''|*[!0-9]*) die "Invalid --port: '$PORT' (must be a number)." ;;
    esac
    if [ "$STAGING" -eq 1 ] && [ "$SELF_SIGNED" -eq 1 ]; then
        die "Use either --staging or --self-signed, not both."
    fi
fi

# ---- detect distribution (needed for install/reinstall; best-effort otherwise) ----
PM=""; FW=""; OS_PRETTY=""
if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    OS_PRETTY="${PRETTY_NAME:-${ID:-unknown}}"
    case "${ID:-}" in
        ubuntu|debian)                      PM="apt"; FW="ufw" ;;
        fedora|rhel|centos|rocky|almalinux) PM="dnf"; FW="firewalld" ;;
        *)
            case " ${ID_LIKE:-} " in
                *ubuntu*|*debian*)        PM="apt"; FW="ufw" ;;
                *fedora*|*rhel*|*centos*) PM="dnf"; FW="firewalld" ;;
            esac ;;
    esac
fi
if [ "$ACTION" = "install" ] || [ "$ACTION" = "reinstall" ]; then
    [ -n "$PM" ] || die "Unsupported distribution: ${OS_PRETTY:-unknown}. Supported: Ubuntu/Debian, Fedora/RHEL/CentOS."
fi

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

# ---- public IP / DNS helpers ----
public_ip() { curl -fsSL --max-time 5 https://api.ipify.org 2>/dev/null || true; }
resolve_ip() { command -v getent >/dev/null 2>&1 && getent ahostsv4 "$1" 2>/dev/null | awk 'NR==1{print $1}'; }

# ---- best-effort DNS sanity check (non-fatal) ----
dns_check() {
    [ "$USE_PROXY" -eq 1 ] || return 0
    [ "$DRY_RUN" -eq 0 ] || return 0
    local resolved public
    resolved="$(resolve_ip "$DOMAIN")" || true
    public="$(public_ip)"
    if [ -n "$resolved" ] && [ -n "$public" ] && [ "$resolved" != "$public" ]; then
        warn "DNS: $DOMAIN -> $resolved, but this host's public IP looks like $public."
        warn "     HTTPS issuance will fail until the domain's A/AAAA record points here."
    elif [ -z "$resolved" ]; then
        warn "DNS: could not resolve $DOMAIN — make sure its A/AAAA record points to this server."
    fi
}

# ---- Caddyfile content ----
build_caddyfile() {
    if [ "$SELF_SIGNED" -eq 1 ]; then
        printf '%s {\n    tls internal\n    reverse_proxy pingvin-share:3000\n}\n' "$DOMAIN"
        return
    fi
    if [ -n "$EMAIL" ] || [ "$STAGING" -eq 1 ]; then
        printf '{\n'
        [ -n "$EMAIL" ]    && printf '    email %s\n' "$EMAIL"
        [ "$STAGING" -eq 1 ] && printf '    acme_ca https://acme-staging-v02.api.letsencrypt.org/directory\n'
        printf '}\n\n'
    fi
    printf '%s {\n    reverse_proxy pingvin-share:3000\n}\n' "$DOMAIN"
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
        build_caddyfile | write_file "$INSTALL_DIR/Caddyfile"
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
        command -v ufw >/dev/null 2>&1 || run "$SUDO apt-get update && $SUDO DEBIAN_FRONTEND=noninteractive apt-get install -y ufw"
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
    run "$COMPOSE pull"
    run "$COMPOSE up -d"
}

# ---- tear the stack down ----
stop_stack() {
    if [ -f "$INSTALL_DIR/docker-compose.yml" ]; then
        run "$COMPOSE down -v --remove-orphans || true"
    else
        info "No compose file in $INSTALL_DIR — removing containers by name (if any)."
        run "$SUDO docker rm -f pingvin-share pingvin-caddy >/dev/null 2>&1 || true"
    fi
}

# ---- diagnostics when the certificate cannot be issued ----
tls_troubleshoot() {
    local resolved public
    resolved="$(resolve_ip "$DOMAIN")" || true
    public="$(public_ip)"
    warn "----------------------------------------------------------------"
    warn "HTTPS certificate could not be issued for $DOMAIN yet."
    warn "Caddy keeps retrying in the background. Most common causes:"
    warn "  1) A CLOUD firewall / security group blocks inbound 80 and 443."
    warn "     This script opened the OS firewall, but your provider (AWS,"
    warn "     GCP, Oracle, Hetzner, Azure, ...) usually has a SEPARATE"
    warn "     firewall you must open in their web console. Allow TCP 80+443."
    warn "  2) The domain does not point to THIS server:"
    warn "        $DOMAIN -> ${resolved:-<unresolved>}"
    warn "        this host public IP -> ${public:-<unknown>}"
    warn "     These two must match."
    warn "  3) Let's Encrypt rate-limited you after repeated failures."
    warn ""
    warn "  Re-check status:  sudo $0 --status --domain $DOMAIN"
    warn "  Test, no limits:  sudo $0 --reinstall --staging --domain $DOMAIN${EMAIL:+ --email $EMAIL}"
    warn "  Instant HTTPS  :  sudo $0 --reinstall --self-signed --domain $DOMAIN"
    warn "----------------------------------------------------------------"
}

# ---- wait for and verify the certificate ----
verify_deploy() {
    [ "$DRY_RUN" -eq 0 ] || return 0
    [ "$USE_PROXY" -eq 1 ] || return 0
    if [ "$SELF_SIGNED" -eq 1 ]; then
        ok "Self-signed TLS is active (Caddy internal CA). Browsers will warn — expected."
        return 0
    fi
    info "Waiting for Caddy to obtain the TLS certificate for $DOMAIN (up to ~90s)..."
    local n=0 got=0
    while [ "$n" -lt 30 ]; do
        if $SUDO docker exec pingvin-caddy sh -c 'find /data/caddy/certificates -name "*.crt" 2>/dev/null | grep -q .' 2>/dev/null; then
            got=1; break
        fi
        n=$((n + 1)); sleep 3
    done
    if [ "$got" -eq 1 ]; then
        ok "TLS certificate obtained$( [ "$STAGING" -eq 1 ] && echo ' (STAGING — not trusted by browsers, for testing only)' )."
    else
        tls_troubleshoot
    fi
}

# ---- status / doctor ----
do_status() {
    echo
    echo "=== Pingvin Share status ==="
    echo "Install dir : $INSTALL_DIR  ($( [ -f "$INSTALL_DIR/docker-compose.yml" ] && echo 'compose present' || echo 'no compose file' ))"
    echo
    echo "Containers:"
    $SUDO docker ps -a --filter name=pingvin-share --filter name=pingvin-caddy \
        --format '  {{.Names}}  {{.Status}}  {{.Ports}}' 2>/dev/null || warn "  (could not query docker)"
    echo
    if $SUDO docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^pingvin-caddy$'; then
        if $SUDO docker exec pingvin-caddy sh -c 'find /data/caddy/certificates -name "*.crt" 2>/dev/null | grep -q .' 2>/dev/null; then
            ok "TLS certificate is present in Caddy."
        else
            warn "No TLS certificate stored yet."
        fi
    fi
    if [ -n "$DOMAIN" ]; then
        local resolved public
        resolved="$(resolve_ip "$DOMAIN")" || true
        public="$(public_ip)"
        echo
        echo "DNS / reachability:"
        echo "  $DOMAIN -> ${resolved:-<unresolved>}"
        echo "  this host public IP -> ${public:-<unknown>}"
        if [ -n "$resolved" ] && [ -n "$public" ] && [ "$resolved" = "$public" ]; then
            ok "DNS points at this host."
        elif [ -n "$resolved" ] && [ -n "$public" ]; then
            warn "DNS does NOT match this host's public IP — fix the A/AAAA record."
        fi
    fi
    echo
    info "Logs:  sudo docker logs -f pingvin-caddy   |   sudo docker logs -f pingvin-share"
}

# ============================ dispatch ============================

# ---- status: read-only, no banner/confirmation ----
if [ "$ACTION" = "status" ]; then
    do_status
    exit 0
fi

# ---- banner ----
echo
echo "============================================================"
echo " Pingvin Share $ACTION$( [ "$DRY_RUN" -eq 1 ] && echo '   *** DRY-RUN ***' )"
echo "============================================================"
echo "  Distribution : ${OS_PRETTY:-unknown}$( [ -n "$PM" ] && echo "  (pkg: $PM, fw: $FW)" )"
echo "  Install dir  : $INSTALL_DIR"
case "$ACTION" in
    install|reinstall)
        echo "  Image        : $IMAGE"
        if [ "$USE_PROXY" -eq 1 ]; then
            if [ "$SELF_SIGNED" -eq 1 ]; then
                echo "  Mode         : reverse proxy (Caddy) + self-signed TLS (internal CA)"
            elif [ "$STAGING" -eq 1 ]; then
                echo "  Mode         : reverse proxy (Caddy) + Let's Encrypt STAGING"
            else
                echo "  Mode         : reverse proxy (Caddy) + automatic HTTPS"
            fi
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
        [ "$ACTION" = "reinstall" ] && echo "  Existing data: $( [ "$PURGE_DATA" -eq 1 ] && echo 'DELETE (--purge-data)' || echo 'keep ./data' )"
        ;;
    uninstall)
        echo "  Data         : $( [ "$PURGE_DATA" -eq 1 ] && echo 'DELETE uploads + database (--purge-data)' || echo 'keep ./data' )"
        ;;
esac
echo "============================================================"
echo

# ---- confirmation ----
if [ "$DRY_RUN" -eq 0 ] && [ "$ASSUME_YES" -eq 0 ]; then
    if [ "$ACTION" = "uninstall" ] || [ "$ACTION" = "reinstall" ]; then
        [ "$PURGE_DATA" -eq 1 ] && warn "This will PERMANENTLY DELETE all uploaded files and the database."
    fi
    printf 'Proceed with %s? [y/N] ' "$ACTION"
    read -r ans
    case "$ans" in
        y|Y|yes|YES) ;;
        *) die "Aborted by user." ;;
    esac
fi

# ---- run the chosen action ----
case "$ACTION" in
    uninstall)
        stop_stack
        if [ "$PURGE_DATA" -eq 1 ]; then
            run "$SUDO rm -rf '$INSTALL_DIR'"
        else
            run "$SUDO rm -f '$INSTALL_DIR/docker-compose.yml' '$INSTALL_DIR/Caddyfile'"
            info "Kept data in $INSTALL_DIR/data (use --purge-data to remove it)."
        fi
        [ "$DRY_RUN" -eq 1 ] && { ok "Dry-run complete. Nothing was changed."; exit 0; }
        ok "Pingvin Share removed."
        exit 0
        ;;
    reinstall)
        ensure_docker
        stop_stack
        [ "$PURGE_DATA" -eq 1 ] && run "$SUDO rm -rf '$INSTALL_DIR/data'"
        dns_check
        write_stack
        configure_firewall
        start_stack
        ;;
    install)
        ensure_docker
        dns_check
        write_stack
        configure_firewall
        start_stack
        ;;
esac

if [ "$DRY_RUN" -eq 1 ]; then
    ok "Dry-run complete. Nothing was changed."
    exit 0
fi

verify_deploy

echo
ok "Pingvin Share is up."
if [ "$USE_PROXY" -eq 1 ]; then
    info "Open:  https://$DOMAIN"
    [ "$SELF_SIGNED" -eq 1 ] && info "(self-signed — your browser will warn; click through to proceed)"
    [ "$STAGING" -eq 1 ]     && info "(staging cert — browsers won't trust it; re-run without --staging once DNS/firewall are confirmed)"
else
    info "Open:  http://<server-ip>:$PORT   (no TLS — put it behind a proxy for internet use)"
fi
echo
info "First account you register becomes the admin."
info "Then open Configuration -> set the App URL$( [ "$USE_PROXY" -eq 1 ] && echo " to https://$DOMAIN" ) and the max share size."
info "Manage the stack (compose file lives in $INSTALL_DIR):"
info "  sudo docker compose -f $INSTALL_DIR/docker-compose.yml logs -f      # follow logs"
info "  sudo docker compose -f $INSTALL_DIR/docker-compose.yml pull && \\"
info "  sudo docker compose -f $INSTALL_DIR/docker-compose.yml up -d        # update"
info "  sudo $0 --status --domain ${DOMAIN:-<domain>}                       # health/cert check"
