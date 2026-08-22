#!/usr/bin/env bash
#
# harden.sh — audit and harden a Linux server, without locking you out.
#
# toolkit-name: Server hardening (audit + apply)
# toolkit-kind: tool
# toolkit-category: Security
# toolkit-summary: Scores the machine's security posture, then fixes what is safe to fix.
# toolkit-os: debian, fedora, arch, suse, alpine
# toolkit-root: optional
# toolkit-needs: grep, awk, sed
# toolkit-optional: sshd, ufw, firewall-cmd, fail2ban-client, systemctl
# toolkit-run: --audit
# toolkit-preview: --apply --dry-run
# toolkit-writes: /etc/ssh/sshd_config.d/99-harden.conf, /etc/sysctl.d/99-harden.conf
# toolkit-order: 20
# toolkit-arg: --report | Write the audit to a Markdown file | path
# toolkit-arg: --ssh-port | SSH port to keep open in the firewall | number
# toolkit-arg: --allow-user | Restrict SSH logins to this user (AllowUsers) | text
#
# The default action is a READ-ONLY audit: it changes nothing, prints a scored
# report of what is weak, and tells you exactly which flag fixes each item.
#
#   ./harden.sh                     audit only (safe, changes nothing)
#   ./harden.sh --apply --dry-run   show every change it would make
#   ./harden.sh --apply             apply the safe fixes (asks first)
#   ./harden.sh --rollback          undo the last --apply from its backup
#
# Anti-lockout guarantees, because this runs on machines you reach over SSH:
#   * password logins are only disabled when a usable SSH key already exists for
#     root or for the user invoking the script — otherwise that fix is skipped;
#   * the firewall is opened for the real SSH port BEFORE it is enabled;
#   * the new sshd config is validated with `sshd -t` and reverted if it fails;
#   * sshd is reloaded, never restarted, so the session you are typing in survives;
#   * every file touched is copied to /var/backups/toolkit-harden/<timestamp>/
#     and can be restored with --rollback.
#
# Options:
#       --audit           read-only audit (default)
#       --apply           apply the safe fixes
#   -n, --dry-run         with --apply: print every change, do nothing
#   -y, --yes             skip the confirmation prompt
#       --strict          exit non-zero when the audit finds failures (for CI)
#       --rollback        restore the most recent backup and reload services
#       --report <file>   also write the audit as Markdown
#       --ssh-port <n>    SSH port to keep open (default: read from sshd_config)
#       --allow-user <u>  restrict SSH logins to this user (AllowUsers)
#       --no-ssh          skip the SSH section
#       --no-firewall     skip the firewall section
#       --no-updates      skip automatic security updates
#       --no-fail2ban     skip fail2ban
#       --no-sysctl       skip the kernel sysctl section
#   -h, --help            show this help
#
set -uo pipefail
export LC_ALL=C

VERSION="1.0"
ACTION="audit"
DRY_RUN=0; ASSUME_YES=0; STRICT=0
REPORT_FILE=""; SSH_PORT=""; ALLOW_USER=""
DO_SSH=1; DO_FW=1; DO_UPD=1; DO_F2B=1; DO_SYSCTL=1

# ---- pretty output ---------------------------------------------------------- #
if [ -t 1 ]; then
    C_B=$'\033[1m'; C_D=$'\033[2m'; C_0=$'\033[0m'
    C_I=$'\033[38;5;75m'; C_OK=$'\033[38;5;114m'; C_W=$'\033[38;5;221m'
    C_E=$'\033[38;5;203m'; C_G=$'\033[38;5;245m'; C_M=$'\033[38;5;177m'
else
    C_B=''; C_D=''; C_0=''; C_I=''; C_OK=''; C_W=''; C_E=''; C_G=''; C_M=''
fi
info() { printf '%s[*]%s %s\n' "$C_I" "$C_0" "$*"; }
ok()   { printf '%s[+]%s %s\n' "$C_OK" "$C_0" "$*"; }
warn() { printf '%s[!]%s %s\n' "$C_W" "$C_0" "$*" >&2; }
die()  { printf '%s[x]%s %s\n' "$C_E" "$C_0" "$*" >&2; exit 1; }
rule() { printf '%s%s%s\n' "$C_G" "$(printf '─%.0s' $(seq 1 "${1:-72}"))" "$C_0"; }

# ---- argument parsing -------------------------------------------------------- #
while [ $# -gt 0 ]; do
    case "$1" in
        --audit)        ACTION="audit" ;;
        --apply|--fix)  ACTION="apply" ;;
        --rollback)     ACTION="rollback" ;;
        -n|--dry-run)   DRY_RUN=1 ;;
        -y|--yes)       ASSUME_YES=1 ;;
        --strict)       STRICT=1 ;;
        --report)       shift; REPORT_FILE="${1:-}" ;;
        --report=*)     REPORT_FILE="${1#*=}" ;;
        --ssh-port)     shift; SSH_PORT="${1:-}" ;;
        --ssh-port=*)   SSH_PORT="${1#*=}" ;;
        --allow-user)   shift; ALLOW_USER="${1:-}" ;;
        --allow-user=*) ALLOW_USER="${1#*=}" ;;
        --no-ssh)       DO_SSH=0 ;;
        --no-firewall)  DO_FW=0 ;;
        --no-updates)   DO_UPD=0 ;;
        --no-fail2ban)  DO_F2B=0 ;;
        --no-sysctl)    DO_SYSCTL=0 ;;
        -h|--help)      grep '^#' "$0" | grep -v '^#!' | grep -v '^# toolkit-' \
                            | sed 's/^#\{1,\} \{0,1\}//'; exit 0 ;;
        *) die "Unknown option: $1  (try --help)" ;;
    esac
    shift
done

# ---- privileges -------------------------------------------------------------- #
IS_ROOT=0; SUDO=""
if [ "$(id -u)" -eq 0 ]; then
    IS_ROOT=1
elif command -v sudo >/dev/null 2>&1; then
    SUDO="sudo"
fi
run_priv() {
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '%s   would run:%s %s\n' "$C_D" "$C_0" "$*"
        return 0
    fi
    if [ "$IS_ROOT" -eq 1 ]; then "$@"; else $SUDO "$@"; fi
}
have() { command -v "$1" >/dev/null 2>&1; }
priv_capture() {   # run privileged, return stdout+stderr, keep the exit status
    if [ "$IS_ROOT" -eq 1 ]; then "$@" 2>&1; else $SUDO "$@" 2>&1; fi
}

# ---- distro detection --------------------------------------------------------- #
OS_ID=""; OS_LIKE=""; OS_NAME=""; FAMILY="unknown"; PKG=""
if [ -r /etc/os-release ]; then
    # Read it in subshells: os-release defines VERSION, NAME and friends, and
    # sourcing it here would quietly overwrite this script's own variables.
    OS_ID="$(. /etc/os-release 2>/dev/null; printf '%s' "${ID:-}")"
    OS_LIKE="$(. /etc/os-release 2>/dev/null; printf '%s' "${ID_LIKE:-}")"
    OS_NAME="$(. /etc/os-release 2>/dev/null; printf '%s' "${PRETTY_NAME:-${ID:-}}")"
fi
case "$OS_ID$OS_LIKE" in
    *debian*|*ubuntu*) FAMILY="debian"; PKG="apt" ;;
    *fedora*|*rhel*|*centos*) FAMILY="fedora"; PKG="dnf" ;;
    *arch*)  FAMILY="arch";  PKG="pacman" ;;
    *suse*)  FAMILY="suse";  PKG="zypper" ;;
    *alpine*) FAMILY="alpine"; PKG="apk" ;;
esac
have dnf || { [ "$PKG" = "dnf" ] && have yum && PKG="yum"; }

BACKUP_ROOT="/var/backups/toolkit-harden"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP_DIR="$BACKUP_ROOT/$STAMP"

# ---- check bookkeeping --------------------------------------------------------- #
# Each check appends one record: status<TAB>section<TAB>title<TAB>detail<TAB>fix
CHECKS_FILE="$(mktemp)"
trap 'rm -f "$CHECKS_FILE"' EXIT
N_PASS=0; N_WARN=0; N_FAIL=0; N_INFO=0
FIXABLE=0

record() {   # record STATUS SECTION TITLE DETAIL [FIX]
    printf '%s\t%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "$4" "${5:-}" >>"$CHECKS_FILE"
    case "$1" in
        PASS) N_PASS=$((N_PASS + 1)) ;;
        WARN) N_WARN=$((N_WARN + 1)); [ -n "${5:-}" ] && FIXABLE=$((FIXABLE + 1)) ;;
        FAIL) N_FAIL=$((N_FAIL + 1)); [ -n "${5:-}" ] && FIXABLE=$((FIXABLE + 1)) ;;
        *)    N_INFO=$((N_INFO + 1)) ;;
    esac
    return 0
}

status_icon() {
    case "$1" in
        PASS) printf '%s ✔ %s' "$C_OK" "$C_0" ;;
        WARN) printf '%s ▲ %s' "$C_W" "$C_0" ;;
        FAIL) printf '%s ✖ %s' "$C_E" "$C_0" ;;
        *)    printf '%s ▪ %s' "$C_I" "$C_0" ;;
    esac
}

# ---- sshd helpers ---------------------------------------------------------------- #
SSHD_BIN=""
for c in /usr/sbin/sshd /usr/bin/sshd sshd; do
    if command -v "$c" >/dev/null 2>&1 || [ -x "$c" ]; then SSHD_BIN="$c"; break; fi
done
SSHD_CONFIG="/etc/ssh/sshd_config"
SSHD_DROPIN="/etc/ssh/sshd_config.d/99-harden.conf"

sshd_effective() {   # the value sshd actually uses, includes and all
    local key="$1" val=""
    if [ -n "$SSHD_BIN" ]; then
        val="$($SUDO_MAYBE "$SSHD_BIN" -T 2>/dev/null | awk -v k="$(echo "$key" | tr '[:upper:]' '[:lower:]')" \
              '$1 == k {print $2; exit}')"
    fi
    if [ -z "$val" ] && [ -r "$SSHD_CONFIG" ]; then
        val="$(grep -rhiE "^[[:space:]]*${key}[[:space:]]+" "$SSHD_CONFIG" \
               /etc/ssh/sshd_config.d/*.conf 2>/dev/null | tail -1 | awk '{print $2}')"
    fi
    printf '%s' "$val"
}
# Read-only probes use non-interactive sudo. An audit that stops to ask for a
# password is useless in a pipeline — and invisible when stdout is redirected.
SUDO_MAYBE=""
PRIV_READS=1
if [ "$IS_ROOT" -eq 0 ]; then
    if [ -n "$SUDO" ] && sudo -n true 2>/dev/null; then
        SUDO_MAYBE="sudo -n"
    else
        PRIV_READS=0
    fi
fi

sshd_supports_include() {
    grep -qiE '^[[:space:]]*Include[[:space:]]+/etc/ssh/sshd_config\.d/' "$SSHD_CONFIG" 2>/dev/null
}

authorized_keys_for() {   # prints the number of keys for a user
    local user="$1" home keys=0 n=0
    home="$(getent passwd "$user" 2>/dev/null | cut -d: -f6)"
    [ -n "$home" ] || return 0
    for f in "$home/.ssh/authorized_keys" "$home/.ssh/authorized_keys2"; do
        [ -r "$f" ] || continue
        n="$(grep -cvE '^[[:space:]]*(#|$)' "$f" 2>/dev/null)"
        case "$n" in ''|*[!0-9]*) n=0 ;; esac
        keys=$((keys + n))
    done
    printf '%s' "$keys"
}

admin_users() {   # root plus whoever invoked sudo, plus other UID>=1000 shells
    { echo root
      [ -n "${SUDO_USER:-}" ] && echo "$SUDO_USER"
      getent passwd 2>/dev/null | awk -F: '$3>=1000 && $3<65000 && $7 !~ /(nologin|false)$/ {print $1}'
    } | awk 'NF' | sort -u
}

ssh_key_available() {
    local u n
    for u in $(admin_users); do
        n="$(authorized_keys_for "$u")"
        [ "${n:-0}" -gt 0 ] && { printf '%s' "$u"; return 0; }
    done
    return 1
}

detect_ssh_port() {
    local p
    p="$(sshd_effective Port)"
    [ -n "$p" ] || p=22
    printf '%s' "$p"
}

# =============================================================================== #
# AUDIT
# =============================================================================== #
audit_ssh() {
    [ "$DO_SSH" -eq 1 ] || return 0
    local sec="SSH"
    if [ -z "$SSHD_BIN" ] && [ ! -r "$SSHD_CONFIG" ]; then
        record INFO "$sec" "OpenSSH server" "not installed on this machine" ""
        return 0
    fi
    local keyuser=""
    keyuser="$(ssh_key_available || true)"
    if [ -n "$keyuser" ]; then
        record PASS "$sec" "SSH key present" "'$keyuser' has an authorized_keys entry" ""
    else
        record WARN "$sec" "No SSH key found" \
            "no authorized_keys for root or any regular user — password login cannot be disabled safely" \
            "add your public key first: ssh-copy-id user@host"
    fi

    local v
    v="$(sshd_effective PermitRootLogin)"
    case "${v:-yes}" in
        no|prohibit-password|without-password|forced-commands-only)
            record PASS "$sec" "Root login" "PermitRootLogin=${v}" "" ;;
        *)  record FAIL "$sec" "Root login over SSH" \
                "PermitRootLogin=${v:-yes} — root can log in with a password" \
                "--apply sets PermitRootLogin prohibit-password" ;;
    esac

    v="$(sshd_effective PasswordAuthentication)"
    if [ "${v:-yes}" = "no" ]; then
        record PASS "$sec" "Password authentication" "disabled — keys only" ""
    elif [ -n "$keyuser" ]; then
        record FAIL "$sec" "Password authentication" \
            "enabled — every login is brute-forceable" \
            "--apply disables it (a key for '$keyuser' already exists)"
    else
        record WARN "$sec" "Password authentication" \
            "enabled, and no SSH key exists yet — disabling it now would lock you out" \
            "add a key, then re-run --apply"
    fi

    v="$(sshd_effective PermitEmptyPasswords)"
    if [ "${v:-no}" = "no" ]; then
        record PASS "$sec" "Empty passwords" "rejected" ""
    else
        record FAIL "$sec" "Empty passwords" "PermitEmptyPasswords=yes" \
            "--apply sets it to no"
    fi

    v="$(sshd_effective MaxAuthTries)"
    if [ -n "$v" ] && [ "$v" -le 4 ] 2>/dev/null; then
        record PASS "$sec" "Auth attempts per connection" "MaxAuthTries=$v" ""
    else
        record WARN "$sec" "Auth attempts per connection" \
            "MaxAuthTries=${v:-6} — a single connection may guess many times" \
            "--apply sets MaxAuthTries 3"
    fi

    v="$(sshd_effective X11Forwarding)"
    if [ "${v:-no}" = "no" ]; then
        record PASS "$sec" "X11 forwarding" "disabled" ""
    else
        record WARN "$sec" "X11 forwarding" "enabled but rarely needed on a server" \
            "--apply disables it"
    fi

    v="$(sshd_effective LoginGraceTime)"
    if [ -n "$v" ] && [ "$v" -le 60 ] 2>/dev/null && [ "$v" -gt 0 ] 2>/dev/null; then
        record PASS "$sec" "Login grace time" "${v}s" ""
    else
        record WARN "$sec" "Login grace time" \
            "${v:-120}s — unauthenticated connections linger" "--apply sets 30s"
    fi

    local port; port="$(detect_ssh_port)"
    record INFO "$sec" "Listening port" "sshd is configured on port $port" ""

    v="$(sshd_effective AllowUsers)$(sshd_effective AllowGroups)"
    if [ -n "$v" ]; then
        record PASS "$sec" "Login allow-list" "restricted to: $v" ""
    else
        record INFO "$sec" "Login allow-list" \
            "any account with a shell may log in" \
            "use --allow-user <name> to restrict it"
    fi
}

audit_firewall() {
    [ "$DO_FW" -eq 1 ] || return 0
    local sec="Firewall" port; port="$(detect_ssh_port)"
    if have ufw; then
        if $SUDO_MAYBE ufw status 2>/dev/null | head -1 | grep -qi 'active'; then
            record PASS "$sec" "ufw" "active" ""
            if $SUDO_MAYBE ufw status verbose 2>/dev/null | grep -qi 'deny (incoming)'; then
                record PASS "$sec" "Default incoming policy" "deny" ""
            else
                record FAIL "$sec" "Default incoming policy" "not deny — everything is exposed" \
                    "--apply sets 'ufw default deny incoming'"
            fi
            if $SUDO_MAYBE ufw status 2>/dev/null | grep -qE "(^|[[:space:]])(${port}|OpenSSH|ssh)([[:space:]]|/|$)"; then
                record PASS "$sec" "SSH reachable" "port $port is allowed" ""
            else
                record WARN "$sec" "SSH rule" "no explicit rule for port $port" \
                    "--apply allows it before touching anything else"
            fi
        else
            record FAIL "$sec" "ufw" "installed but inactive — no packet filtering" \
                "--apply enables it (allowing SSH first)"
        fi
    elif have firewall-cmd; then
        if $SUDO_MAYBE firewall-cmd --state 2>/dev/null | grep -q running; then
            record PASS "$sec" "firewalld" "running" ""
            local zone; zone="$($SUDO_MAYBE firewall-cmd --get-default-zone 2>/dev/null)"
            record INFO "$sec" "Default zone" "${zone:-unknown}" ""
            if $SUDO_MAYBE firewall-cmd --list-services 2>/dev/null | grep -qw ssh \
               || $SUDO_MAYBE firewall-cmd --list-ports 2>/dev/null | grep -qw "${port}/tcp"; then
                record PASS "$sec" "SSH reachable" "allowed in zone ${zone:-default}" ""
            else
                record WARN "$sec" "SSH rule" "port $port not explicitly allowed" \
                    "--apply adds it"
            fi
        else
            record FAIL "$sec" "firewalld" "installed but not running" \
                "--apply starts it (allowing SSH first)"
        fi
    elif have nft && [ -n "$($SUDO_MAYBE nft list ruleset 2>/dev/null | head -c 1)" ]; then
        record INFO "$sec" "nftables" "a raw ruleset is loaded — not managed by this script" ""
    elif have iptables && [ "$($SUDO_MAYBE iptables -S 2>/dev/null | wc -l)" -gt 3 ]; then
        record INFO "$sec" "iptables" "custom rules present — not managed by this script" ""
    else
        record FAIL "$sec" "Firewall" "none installed — every open port is reachable" \
            "--apply installs and configures ufw (debian) or firewalld (fedora)"
    fi
}

audit_updates() {
    [ "$DO_UPD" -eq 1 ] || return 0
    local sec="Updates"
    case "$FAMILY" in
        debian)
            if [ -f /etc/apt/apt.conf.d/20auto-upgrades ] \
               && grep -q '"1"' /etc/apt/apt.conf.d/20auto-upgrades 2>/dev/null; then
                record PASS "$sec" "Automatic security updates" "unattended-upgrades is configured" ""
            else
                record FAIL "$sec" "Automatic security updates" \
                    "not configured — security patches wait for a human" \
                    "--apply installs and enables unattended-upgrades"
            fi ;;
        fedora)
            if systemctl is-enabled dnf-automatic.timer >/dev/null 2>&1 \
               || systemctl is-enabled dnf-automatic-install.timer >/dev/null 2>&1; then
                record PASS "$sec" "Automatic security updates" "dnf-automatic timer enabled" ""
            else
                record FAIL "$sec" "Automatic security updates" "not configured" \
                    "--apply installs and enables dnf-automatic"
            fi ;;
        *) record INFO "$sec" "Automatic security updates" \
               "no automation known for this distro family" "" ;;
    esac
    if [ -f /var/run/reboot-required ] || [ -f /run/reboot-required ]; then
        record WARN "$sec" "Reboot required" "a package update needs a reboot to take effect" \
            "schedule a reboot"
    fi
    if have needs-restarting && [ "$IS_ROOT" -eq 1 ]; then
        needs-restarting -r >/dev/null 2>&1 \
            || record WARN "$sec" "Reboot required" "the running kernel is older than the installed one" \
                      "schedule a reboot"
    fi
}

audit_fail2ban() {
    [ "$DO_F2B" -eq 1 ] || return 0
    local sec="Intrusion"
    if have fail2ban-client; then
        if systemctl is-active fail2ban >/dev/null 2>&1; then
            if $SUDO_MAYBE fail2ban-client status 2>/dev/null | grep -qi 'sshd'; then
                record PASS "$sec" "fail2ban" "running with an sshd jail" ""
            else
                record WARN "$sec" "fail2ban" "running, but no sshd jail is active" \
                    "--apply enables the sshd jail"
            fi
        else
            record WARN "$sec" "fail2ban" "installed but not running" "--apply starts it"
        fi
    else
        record WARN "$sec" "fail2ban" "not installed — brute-force attempts are never blocked" \
            "--apply installs it with an sshd jail"
    fi
}

SYSCTL_KEYS="net.ipv4.conf.all.rp_filter=1
net.ipv4.conf.all.accept_redirects=0
net.ipv4.conf.all.send_redirects=0
net.ipv4.conf.all.accept_source_route=0
net.ipv4.conf.default.accept_redirects=0
net.ipv4.tcp_syncookies=1
net.ipv6.conf.all.accept_redirects=0
net.ipv6.conf.all.accept_source_route=0
kernel.randomize_va_space=2
kernel.dmesg_restrict=1
kernel.kptr_restrict=1"

audit_sysctl() {
    [ "$DO_SYSCTL" -eq 1 ] || return 0
    local sec="Kernel" bad=0 total=0 key want got
    while IFS='=' read -r key want; do
        [ -n "$key" ] || continue
        total=$((total + 1))
        got="$(sysctl -n "$key" 2>/dev/null || true)"
        [ -n "$got" ] || continue
        if [ "$key" = "kernel.kptr_restrict" ]; then
            [ "${got:-0}" -ge 1 ] 2>/dev/null || bad=$((bad + 1))
        elif [ "$got" != "$want" ]; then
            bad=$((bad + 1))
        fi
    done <<EOF
$SYSCTL_KEYS
EOF
    if [ "$bad" -eq 0 ]; then
        record PASS "$sec" "Network and kernel sysctls" "all $total hardening values already set" ""
    else
        record WARN "$sec" "Network and kernel sysctls" \
            "$bad of $total values are weaker than recommended (redirects, source routing, ASLR, dmesg)" \
            "--apply writes /etc/sysctl.d/99-harden.conf"
    fi
}

audit_accounts() {
    local sec="Accounts" n
    if [ -r /etc/shadow ]; then
        n="$(awk -F: '($2 == "") {print $1}' /etc/shadow 2>/dev/null | wc -l)"
        if [ "${n:-0}" -eq 0 ]; then
            record PASS "$sec" "Empty passwords" "no account can log in without one" ""
        else
            record FAIL "$sec" "Empty passwords" "$n account(s) have an empty password" \
                "lock them: passwd -l <user>"
        fi
    else
        record INFO "$sec" "Empty passwords" "/etc/shadow not readable — run as root to check" ""
    fi
    n="$(awk -F: '($3 == 0) {print $1}' /etc/passwd 2>/dev/null | wc -l)"
    if [ "${n:-1}" -le 1 ]; then
        record PASS "$sec" "UID 0 accounts" "only root has uid 0" ""
    else
        record FAIL "$sec" "UID 0 accounts" "$n accounts share uid 0" \
            "give the extra accounts their own uid"
    fi
    n="$(admin_users | wc -l)"
    record INFO "$sec" "Login accounts" "$n account(s) with an interactive shell" ""
}

audit_services() {
    local sec="Exposure" list=""
    if have ss; then
        list="$($SUDO_MAYBE ss -H -tlnp 2>/dev/null \
                | awk '{print $4}' | grep -vE '^(127\.|\[::1\]|::1)' | sort -u | tr '\n' ' ')"
    elif have netstat; then
        list="$($SUDO_MAYBE netstat -tlnp 2>/dev/null | awk 'NR>2 {print $4}' \
                | grep -vE '^(127\.|::1)' | sort -u | tr '\n' ' ')"
    fi
    if [ -n "$list" ]; then
        record INFO "$sec" "Listening on all interfaces" "$list" \
            "close what you do not serve to the internet"
    else
        record INFO "$sec" "Listening sockets" "nothing bound to a public address (or ss/netstat missing)" ""
    fi
    if have timedatectl; then
        if timedatectl show -p NTPSynchronized --value 2>/dev/null | grep -qi yes; then
            record PASS "Time" "Clock synchronised" "NTP is in sync" ""
        else
            record WARN "Time" "Clock not synchronised" \
                "a wrong clock breaks TLS validation and log correlation" \
                "enable NTP: timedatectl set-ntp true"
        fi
    fi
}

run_audit() {
    if [ "$PRIV_READS" -eq 0 ]; then
        record INFO "Scope" "Running unprivileged" \
            "some checks need root and were skipped or reduced" \
            "re-run with sudo for the full picture"
    fi
    audit_ssh; audit_firewall; audit_updates; audit_fail2ban
    audit_sysctl; audit_accounts; audit_services
}

print_audit() {
    local width=76 last=""
    printf '\n'
    printf '%s%s harden %s — security audit %s\n' "$C_B" "$C_M" "$VERSION" "$C_0"
    printf '%s %s · kernel %s · %s%s\n' "$C_G" "${OS_NAME:-unknown}" "$(uname -r)" "$(uname -m)" "$C_0"
    rule "$width"
    while IFS=$'\t' read -r status sec title detail fix; do
        [ -n "$status" ] || continue
        if [ "$sec" != "$last" ]; then
            printf '\n%s%s%s\n' "$C_B" "$sec" "$C_0"
            last="$sec"
        fi
        printf '%s %-34s %s%s%s\n' "$(status_icon "$status")" "$title" "$C_G" "$detail" "$C_0"
        if [ -n "$fix" ] && [ "$status" != "PASS" ] && [ "$status" != "INFO" ]; then
            printf '     %s↳ %s%s\n' "$C_D" "$fix" "$C_0"
        fi
    done <"$CHECKS_FILE"
    rule "$width"

    local scored=$((N_PASS + N_WARN + N_FAIL)) score=100 grade colour
    [ "$scored" -gt 0 ] && score=$(( (N_PASS * 100 + N_WARN * 45) / scored ))
    if   [ "$score" -ge 90 ]; then grade="A"; colour="$C_OK"
    elif [ "$score" -ge 75 ]; then grade="B"; colour="$C_OK"
    elif [ "$score" -ge 60 ]; then grade="C"; colour="$C_W"
    elif [ "$score" -ge 40 ]; then grade="D"; colour="$C_W"
    else grade="F"; colour="$C_E"; fi
    printf ' %sScore %s/100 (grade %s)%s   %s%s passed%s  %s%s to review%s  %s%s failed%s\n' \
        "$colour$C_B" "$score" "$grade" "$C_0" \
        "$C_OK" "$N_PASS" "$C_0" "$C_W" "$N_WARN" "$C_0" "$C_E" "$N_FAIL" "$C_0"
    if [ "$FIXABLE" -gt 0 ]; then
        printf ' %s%s of them can be fixed automatically — re-run with:%s %s--apply --dry-run%s\n' \
            "$C_G" "$FIXABLE" "$C_0" "$C_B" "$C_0"
    else
        printf ' %sNothing left for this script to fix.%s\n' "$C_G" "$C_0"
    fi
    printf '\n'
}

write_report() {
    [ -n "$REPORT_FILE" ] || return 0
    local scored=$((N_PASS + N_WARN + N_FAIL)) score=100
    [ "$scored" -gt 0 ] && score=$(( (N_PASS * 100 + N_WARN * 45) / scored ))
    {
        printf '# Security audit — %s\n\n' "$(hostname)"
        printf '> %s · kernel %s · %s · generated %s by harden.sh %s\n\n' \
            "${OS_NAME:-unknown}" "$(uname -r)" "$(uname -m)" "$(date '+%Y-%m-%d %H:%M:%S')" "$VERSION"
        printf '**Score %s/100** — %s passed, %s to review, %s failed.\n\n' \
            "$score" "$N_PASS" "$N_WARN" "$N_FAIL"
        printf '| | Area | Check | Detail | Suggested fix |\n|---|---|---|---|---|\n'
        while IFS=$'\t' read -r status sec title detail fix; do
            [ -n "$status" ] || continue
            local icon
            case "$status" in
                PASS) icon="✅" ;; WARN) icon="🟠" ;; FAIL) icon="🔴" ;; *) icon="🔵" ;;
            esac
            printf '| %s | %s | %s | %s | %s |\n' "$icon" "$sec" "$title" "$detail" "${fix:-—}"
        done <"$CHECKS_FILE"
        # shellcheck disable=SC2016  # the backticks are Markdown, not a subshell
        printf '\nRun `harden.sh --apply --dry-run` to see exactly what would change.\n'
    } >"$REPORT_FILE" && ok "Report written to $REPORT_FILE"
}

# =============================================================================== #
# APPLY
# =============================================================================== #
backup_file() {
    local f="$1"
    [ -e "$f" ] || return 0
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '%s   would back up:%s %s\n' "$C_D" "$C_0" "$f"
        return 0
    fi
    run_priv mkdir -p "$BACKUP_DIR$(dirname "$f")"
    run_priv cp -a "$f" "$BACKUP_DIR$f"
    printf '%s\n' "$f" | run_priv tee -a "$BACKUP_DIR/manifest.txt" >/dev/null
}

write_priv() {   # write_priv <path> <<'EOF' ... EOF
    local path="$1" content
    content="$(cat)"
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '%s   would write %s:%s\n' "$C_D" "$path" "$C_0"
        printf '%s\n' "$content" | sed "s/^/${C_D}     │ /; s/$/${C_0}/"
        return 0
    fi
    run_priv mkdir -p "$(dirname "$path")"
    printf '%s\n' "$content" | run_priv tee "$path" >/dev/null
}

apply_ssh() {
    [ "$DO_SSH" -eq 1 ] || return 0
    [ -n "$SSHD_BIN" ] || { info "sshd not installed — skipping the SSH section"; return 0; }
    info "SSH"
    local baseline_err=""
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '%s   would verify the current config first:%s %s -t\n' \
            "$C_D" "$C_0" "$SSHD_BIN"
    elif ! baseline_err="$(priv_capture "$SSHD_BIN" -t)"; then
        warn "sshd cannot validate its current configuration:"
        warn "    ${baseline_err:-unknown error}"
        warn "Skipping the SSH section — this script only edits a config it can verify."
        warn "Fix the above (often: ssh-keygen -A, or mkdir /run/sshd) and re-run."
        return 0
    fi

    local keyuser disable_passwords="no"
    keyuser="$(ssh_key_available || true)"
    if [ -n "$keyuser" ]; then
        disable_passwords="yes"
    else
        warn "No authorized_keys found for root or any regular user."
        warn "Password authentication will be LEFT ENABLED so you are not locked out."
        warn "Add a key (ssh-copy-id user@host) and re-run to finish the job."
    fi

    local target="$SSHD_DROPIN"
    if ! sshd_supports_include; then
        target="$SSHD_CONFIG"
        info "This sshd does not Include sshd_config.d — appending to $SSHD_CONFIG instead"
    fi
    backup_file "$SSHD_CONFIG"
    [ -e "$SSHD_DROPIN" ] && backup_file "$SSHD_DROPIN"

    local allow_line=""
    [ -n "$ALLOW_USER" ] && allow_line="AllowUsers $ALLOW_USER"
    local body
    body="$(cat <<EOF
# Written by toolkit harden.sh on $(date '+%Y-%m-%d %H:%M:%S').
# Remove this file (or run harden.sh --rollback) to undo.
PermitRootLogin prohibit-password
PubkeyAuthentication yes
PermitEmptyPasswords no
MaxAuthTries 3
LoginGraceTime 30
X11Forwarding no
ClientAliveInterval 300
ClientAliveCountMax 2
$( [ "$disable_passwords" = "yes" ] && echo "PasswordAuthentication no" \
                                    || echo "# PasswordAuthentication left enabled: no SSH key was found" )
$( [ "$disable_passwords" = "yes" ] && echo "KbdInteractiveAuthentication no" )
$allow_line
EOF
)"
    if [ "$target" = "$SSHD_CONFIG" ]; then
        if [ "$DRY_RUN" -eq 1 ]; then
            printf '%s   would append to %s:%s\n' "$C_D" "$SSHD_CONFIG" "$C_0"
            printf '%s\n' "$body" | sed "s/^/${C_D}     │ /; s/$/${C_0}/"
        else
            run_priv sed -i '/# >>> toolkit harden >>>/,/# <<< toolkit harden <<</d' "$SSHD_CONFIG"
            { printf '\n# >>> toolkit harden >>>\n%s\n# <<< toolkit harden <<<\n' "$body"; } \
                | run_priv tee -a "$SSHD_CONFIG" >/dev/null
        fi
    else
        printf '%s\n' "$body" | write_priv "$target"
    fi

    if [ "$DRY_RUN" -eq 0 ]; then
        local new_err=""
        if ! new_err="$(priv_capture "$SSHD_BIN" -t)"; then
            warn "The new sshd configuration was rejected:"
            warn "    ${new_err:-unknown error}"
            warn "Restoring the previous config — SSH is left exactly as it was."
            [ -e "$BACKUP_DIR$SSHD_CONFIG" ] && run_priv cp -a "$BACKUP_DIR$SSHD_CONFIG" "$SSHD_CONFIG"
            [ "$target" = "$SSHD_DROPIN" ] && run_priv rm -f "$SSHD_DROPIN"
            run_priv sed -i '/# >>> toolkit harden >>>/,/# <<< toolkit harden <<</d' \
                "$SSHD_CONFIG" 2>/dev/null
            # One bad section is no reason to abandon the firewall and the rest.
            return 0
        fi
        local unit="sshd"
        systemctl list-unit-files 2>/dev/null | grep -q '^ssh\.service' && unit="ssh"
        # Reload, never restart: the session you are typing in stays connected.
        run_priv systemctl reload "$unit" 2>/dev/null || run_priv systemctl reload sshd 2>/dev/null \
            || warn "Could not reload sshd — apply it yourself: systemctl reload $unit"
        ok "SSH hardened (config validated, service reloaded, sessions kept)"
        if [ "$disable_passwords" = "yes" ]; then
            ok "Password logins disabled — key for '$keyuser' is now the only way in"
        else
            warn "Password logins still enabled (no key found)"
        fi
    fi
}

apply_firewall() {
    [ "$DO_FW" -eq 1 ] || return 0
    info "Firewall"
    local port="${SSH_PORT:-$(detect_ssh_port)}"
    if have ufw || [ "$FAMILY" = "debian" ]; then
        have ufw || run_priv env DEBIAN_FRONTEND=noninteractive apt-get install -y ufw
        # Order matters: the SSH rule goes in before the policy that would drop it.
        run_priv ufw allow "${port}/tcp"
        run_priv ufw default deny incoming
        run_priv ufw default allow outgoing
        if [ "$DRY_RUN" -eq 1 ]; then
            printf '%s   would run:%s ufw --force enable\n' "$C_D" "$C_0"
        else
            run_priv ufw --force enable && ok "ufw active, port $port/tcp allowed"
        fi
    elif have firewall-cmd || [ "$FAMILY" = "fedora" ]; then
        have firewall-cmd || run_priv "$PKG" -y install firewalld
        run_priv systemctl enable --now firewalld
        run_priv firewall-cmd --permanent --add-port="${port}/tcp"
        run_priv firewall-cmd --reload
        [ "$DRY_RUN" -eq 0 ] && ok "firewalld active, port $port/tcp allowed"
    else
        warn "No supported firewall for this distro family — skipping"
    fi
}

apply_updates() {
    [ "$DO_UPD" -eq 1 ] || return 0
    info "Automatic security updates"
    case "$FAMILY" in
        debian)
            run_priv env DEBIAN_FRONTEND=noninteractive apt-get install -y unattended-upgrades
            backup_file /etc/apt/apt.conf.d/20auto-upgrades
            write_priv /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF
            [ "$DRY_RUN" -eq 0 ] && ok "unattended-upgrades enabled" ;;
        fedora)
            run_priv "$PKG" -y install dnf-automatic
            run_priv systemctl enable --now dnf-automatic-install.timer 2>/dev/null \
                || run_priv systemctl enable --now dnf-automatic.timer
            [ "$DRY_RUN" -eq 0 ] && ok "dnf-automatic enabled" ;;
        *) warn "No update automation known for '$FAMILY' — skipping" ;;
    esac
}

apply_fail2ban() {
    [ "$DO_F2B" -eq 1 ] || return 0
    info "fail2ban"
    local port="${SSH_PORT:-$(detect_ssh_port)}"
    case "$FAMILY" in
        debian) have fail2ban-client || run_priv env DEBIAN_FRONTEND=noninteractive \
                    apt-get install -y fail2ban ;;
        fedora) have fail2ban-client || run_priv "$PKG" -y install fail2ban ;;
        *) warn "fail2ban install not automated for '$FAMILY'"; return 0 ;;
    esac
    backup_file /etc/fail2ban/jail.d/99-toolkit-sshd.local
    write_priv /etc/fail2ban/jail.d/99-toolkit-sshd.local <<EOF
# Written by toolkit harden.sh — remove this file or run harden.sh --rollback to undo.
[sshd]
enabled  = true
port     = $port
maxretry = 4
findtime = 10m
bantime  = 1h
backend  = systemd
EOF
    run_priv systemctl enable --now fail2ban
    [ "$DRY_RUN" -eq 0 ] && ok "fail2ban watching sshd on port $port"
}

apply_sysctl() {
    [ "$DO_SYSCTL" -eq 1 ] || return 0
    info "Kernel sysctls"
    backup_file /etc/sysctl.d/99-harden.conf
    {
        printf '# Written by toolkit harden.sh on %s.\n' "$(date '+%Y-%m-%d %H:%M:%S')"
        printf '# Remove this file (or run harden.sh --rollback) to undo.\n'
        printf '%s\n' "$SYSCTL_KEYS" | sed 's/=/ = /'
    } | write_priv /etc/sysctl.d/99-harden.conf
    run_priv sysctl --system >/dev/null 2>&1
    [ "$DRY_RUN" -eq 0 ] && ok "kernel hardening values applied"
}

confirm_apply() {
    [ "$ASSUME_YES" -eq 1 ] && return 0
    [ "$DRY_RUN" -eq 1 ] && return 0
    printf '\n%sThis will change system configuration.%s Backups go to %s%s%s\n' \
        "$C_B" "$C_0" "$C_B" "$BACKUP_DIR" "$C_0"
    printf 'Undo at any time with: %sharden.sh --rollback%s\n\n' "$C_B" "$C_0"
    printf 'Type %sYES%s to continue: ' "$C_B" "$C_0"
    local answer=""
    read -r answer </dev/tty 2>/dev/null || answer=""
    [ "$answer" = "YES" ] || die "Not confirmed — nothing was changed."
}

run_apply() {
    [ "$IS_ROOT" -eq 1 ] || [ -n "$SUDO" ] || die "Need root (or sudo) to apply changes."
    run_audit
    print_audit
    confirm_apply
    [ "$DRY_RUN" -eq 1 ] && info "DRY RUN — nothing below is actually executed."
    [ "$DRY_RUN" -eq 0 ] && run_priv mkdir -p "$BACKUP_DIR"
    apply_ssh
    apply_firewall
    apply_updates
    apply_fail2ban
    apply_sysctl
    printf '\n'
    if [ "$DRY_RUN" -eq 1 ]; then
        info "Dry run finished — re-run without --dry-run to apply."
    else
        ok "Hardening applied. Backup of every changed file: $BACKUP_DIR"
        info "Verify with: ./harden.sh   (a fresh audit)"
        warn "Before closing this session, open a SECOND SSH session to confirm you can still log in."
    fi
}

run_rollback() {
    [ -d "$BACKUP_ROOT" ] || die "No backups found in $BACKUP_ROOT"
    local latest="" candidate
    for candidate in "$BACKUP_ROOT"/*/; do
        [ -d "$candidate" ] && latest="$candidate"
    done
    [ -n "$latest" ] || die "No backups found in $BACKUP_ROOT"
    latest="${latest%/}"
    info "Restoring from $latest"
    if [ -r "$latest/manifest.txt" ]; then
        while IFS= read -r f; do
            [ -n "$f" ] || continue
            if [ -e "$latest$f" ]; then
                run_priv cp -a "$latest$f" "$f" && ok "restored $f"
            fi
        done <"$latest/manifest.txt"
    fi
    # Files this script creates from scratch are removed rather than restored.
    for f in "$SSHD_DROPIN" /etc/sysctl.d/99-harden.conf /etc/fail2ban/jail.d/99-toolkit-sshd.local; do
        if [ -e "$f" ] && [ ! -e "$latest$f" ]; then
            run_priv rm -f "$f" && ok "removed $f"
        fi
    done
    run_priv sed -i '/# >>> toolkit harden >>>/,/# <<< toolkit harden <<</d' "$SSHD_CONFIG" 2>/dev/null
    run_priv sysctl --system >/dev/null 2>&1
    if [ -n "$SSHD_BIN" ] && $SUDO_MAYBE "$SSHD_BIN" -t 2>/dev/null; then
        local unit="sshd"
        systemctl list-unit-files 2>/dev/null | grep -q '^ssh\.service' && unit="ssh"
        run_priv systemctl reload "$unit" 2>/dev/null || true
    fi
    systemctl is-active fail2ban >/dev/null 2>&1 && run_priv systemctl restart fail2ban
    ok "Rollback complete."
}

# =============================================================================== #
main() {
    case "$ACTION" in
        audit)
            run_audit
            print_audit
            write_report
            [ "$STRICT" -eq 1 ] && [ "$N_FAIL" -gt 0 ] && exit 1
            exit 0 ;;
        apply)
            run_apply
            exit 0 ;;
        rollback)
            run_rollback
            exit 0 ;;
    esac
}
main "$@"
