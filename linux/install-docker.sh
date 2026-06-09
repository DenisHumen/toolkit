#!/usr/bin/env bash
#
# install-docker.sh — universal Docker Engine + Docker Compose installer.
#
# One script that detects the host distribution and runs the matching official
# install path:
#   * Ubuntu / Debian (and derivatives) -> Docker's apt repository
#   * Fedora / RHEL / CentOS / Rocky / Alma -> Docker's dnf (rpm) repository
#
# It follows the exact steps from https://docs.docker.com/engine/install/ and
# installs the same package set on every distro:
#     docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
# (Docker Compose v2 ships as the 'docker-compose-plugin' -> use it as `docker compose`.)
#
# What it does:
#   1. Detects the distro family from /etc/os-release.
#   2. Removes conflicting distro Docker packages (best-effort).
#   3. Adds Docker's official GPG key + stable repository.
#   4. Installs Docker Engine, CLI, containerd, Buildx and the Compose plugin.
#   5. Enables and starts the docker service (systemd).
#   6. Adds the invoking user to the 'docker' group (so sudo isn't needed later).
#   7. Verifies with `docker --version` and `docker compose version`.
#
# Usage:
#   ./install-docker.sh                 install (asks for confirmation)
#   ./install-docker.sh --dry-run       print every step, change NOTHING
#   ./install-docker.sh --yes           non-interactive (assume "yes")
#   options:
#     -n, --dry-run   preview only, make no changes
#     -y, --yes       skip the confirmation prompt
#         --no-start  do not enable/start the docker service
#         --no-group  do not add the current user to the 'docker' group
#     -h, --help      show this help
#
# Run as root, or as a user with sudo privileges.
#
set -euo pipefail

DRY_RUN=0; ASSUME_YES=0; DO_START=1; ADD_GROUP=1
GROUP_USER=""

while [ $# -gt 0 ]; do
    case "$1" in
        -n|--dry-run) DRY_RUN=1 ;;
        -y|--yes)     ASSUME_YES=1 ;;
        --no-start)   DO_START=0 ;;
        --no-group)   ADD_GROUP=0 ;;
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

# ---- privilege helper ----
if [ "$(id -u)" -eq 0 ]; then
    SUDO=""
else
    command -v sudo >/dev/null 2>&1 || die "Run as root, or install 'sudo' first."
    SUDO="sudo"
fi

# ---- detect distribution ----
[ -r /etc/os-release ] || die "Cannot read /etc/os-release — unsupported system."
# shellcheck disable=SC1091
. /etc/os-release
OS_ID="${ID:-}"
OS_LIKE="${ID_LIKE:-}"
CODENAME="${UBUNTU_CODENAME:-${VERSION_CODENAME:-}}"

PM=""            # package manager: apt | dnf
DOCKER_DISTRO="" # path segment on download.docker.com/linux/<slug>
case "$OS_ID" in
    ubuntu)                  PM="apt"; DOCKER_DISTRO="ubuntu" ;;
    debian)                  PM="apt"; DOCKER_DISTRO="debian" ;;
    fedora)                  PM="dnf"; DOCKER_DISTRO="fedora" ;;
    rhel)                    PM="dnf"; DOCKER_DISTRO="rhel" ;;
    centos|rocky|almalinux)  PM="dnf"; DOCKER_DISTRO="centos" ;;
    *)
        case " $OS_LIKE " in
            *ubuntu*)         PM="apt"; DOCKER_DISTRO="ubuntu" ;;
            *debian*)         PM="apt"; DOCKER_DISTRO="debian" ;;
            *fedora*)         PM="dnf"; DOCKER_DISTRO="fedora" ;;
            *rhel*|*centos*)  PM="dnf"; DOCKER_DISTRO="centos" ;;
        esac ;;
esac
[ -n "$PM" ] || die "Unsupported distribution: ${PRETTY_NAME:-$OS_ID}. Supported: Ubuntu, Debian, Fedora, RHEL/CentOS."
[ "$PM" != "apt" ] || [ -n "$CODENAME" ] || die "Could not determine the distro codename from /etc/os-release."

DOCKER_PKGS="docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin"

# ---- install paths ----
install_apt() {
    export DEBIAN_FRONTEND=noninteractive
    local arch; arch="$(dpkg --print-architecture)"
    info "Setting up Docker's apt repository for '$DOCKER_DISTRO' ($CODENAME, $arch)..."
    run "$SUDO apt-get update"
    run "$SUDO apt-get install -y ca-certificates curl"
    run "$SUDO install -m 0755 -d /etc/apt/keyrings"
    run "$SUDO curl -fsSL https://download.docker.com/linux/$DOCKER_DISTRO/gpg -o /etc/apt/keyrings/docker.asc"
    run "$SUDO chmod a+r /etc/apt/keyrings/docker.asc"
    run "echo 'deb [arch=$arch signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/$DOCKER_DISTRO $CODENAME stable' | $SUDO tee /etc/apt/sources.list.d/docker.list >/dev/null"
    run "$SUDO apt-get update"
    info "Removing conflicting packages (if any)..."
    run "$SUDO apt-get remove -y docker.io docker-compose docker-compose-v2 docker-doc podman-docker || true"
    info "Installing Docker Engine + Compose plugin..."
    run "$SUDO apt-get install -y $DOCKER_PKGS"
}

install_dnf() {
    info "Setting up Docker's dnf repository for '$DOCKER_DISTRO'..."
    command -v curl >/dev/null 2>&1 || run "$SUDO dnf -y install curl"
    run "$SUDO curl -fsSL https://download.docker.com/linux/$DOCKER_DISTRO/docker-ce.repo -o /etc/yum.repos.d/docker-ce.repo"
    info "Removing conflicting packages (if any)..."
    run "$SUDO dnf -y remove docker docker-client docker-client-latest docker-common docker-latest docker-latest-logrotate docker-logrotate docker-selinux docker-engine-selinux docker-engine || true"
    info "Installing Docker Engine + Compose plugin..."
    run "$SUDO dnf -y install $DOCKER_PKGS"
}

post_install() {
    if [ "$DO_START" -eq 1 ]; then
        if command -v systemctl >/dev/null 2>&1; then
            info "Enabling and starting the docker service..."
            run "$SUDO systemctl enable --now docker"
        else
            warn "systemd not detected — start the Docker daemon manually."
        fi
    fi
    if [ "$ADD_GROUP" -eq 1 ]; then
        local target="${SUDO_USER:-$USER}"
        if [ -n "$target" ] && [ "$target" != "root" ]; then
            info "Adding user '$target' to the 'docker' group..."
            run "$SUDO usermod -aG docker '$target'"
            GROUP_USER="$target"
        fi
    fi
}

verify() {
    info "Verifying installation..."
    if docker --version; then ok "Docker CLI OK"; else warn "'docker --version' failed."; fi
    if docker compose version; then ok "Docker Compose v2 OK"; else warn "'docker compose version' failed."; fi
}

# ---- banner + confirmation ----
echo
echo "============================================================"
echo " Universal Docker installer$( [ "$DRY_RUN" -eq 1 ] && echo '   *** DRY-RUN ***' )"
echo "============================================================"
echo "  Distribution : ${PRETTY_NAME:-$OS_ID}"
echo "  Repo source  : download.docker.com/linux/$DOCKER_DISTRO  (via $PM)"
echo "  Packages     : $DOCKER_PKGS"
echo "  Start service: $( [ "$DO_START"  -eq 1 ] && echo yes || echo no )"
echo "  Add to group : $( [ "$ADD_GROUP" -eq 1 ] && echo yes || echo no )"
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

case "$PM" in
    apt) install_apt ;;
    dnf) install_dnf ;;
esac
post_install

if [ "$DRY_RUN" -eq 1 ]; then
    ok "Dry-run complete. Nothing was changed."
    exit 0
fi

verify
echo
ok "Docker Engine and Docker Compose are installed."
if [ -n "$GROUP_USER" ]; then
    warn "Log out and back in (or run: newgrp docker) so '$GROUP_USER' can use Docker without sudo."
fi
info "Quick test:  docker run hello-world"
