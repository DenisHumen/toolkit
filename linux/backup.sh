#!/usr/bin/env bash
#
# backup.sh — verified, restorable backups of the things a small server actually loses.
#
# toolkit-name: Backup (create, verify, restore)
# toolkit-kind: tool
# toolkit-category: Maintenance
# toolkit-summary: Archives chosen paths and Docker volumes, verifies every archive, prunes old ones and restores them.
# toolkit-os: any
# toolkit-root: optional
# toolkit-needs: tar
# toolkit-optional: zstd, docker, sha256sum, systemctl, rsync
# toolkit-preview: --auto --dry-run
# toolkit-writes: /var/backups/toolkit
# toolkit-order: 25
# toolkit-arg: --path | Directory to back up (repeat for more) | text
# toolkit-arg: --volume | Docker volume to back up (repeat for more) | text
# toolkit-arg: --dest | Where archives are kept | path
# toolkit-arg: --keep | How many archives to keep | number
# toolkit-arg: --stop | Compose project directory to stop while copying | path
#
# A backup nobody has restored is a rumour, so this writes a plain `tar` archive —
# restorable with `tar` alone if this script is nowhere to be found — reads it back
# to prove it is not truncated, records a SHA-256 next to it, and ships a restore
# path that defaults to a staging directory instead of overwriting anything.
#
# What it can back up
#   --path <dir>         any directory (repeat the flag for several)
#   --volume <name>      a Docker named volume, by name
#   --auto               every Docker Compose project under /opt plus /etc
#
# Consistency
#   Copying a database while it is being written to can capture a torn file. Use
#   --stop <dir> to bring that Compose project down for the copy and back up
#   afterwards; without it the copy is "hot" and the script says so.
#
# Usage
#   ./backup.sh --auto --dry-run            # what would be archived, and how big
#   ./backup.sh --path /opt/pingvin-share --stop /opt/pingvin-share
#   ./backup.sh --list                      # what exists, with sizes and dates
#   ./backup.sh --verify <archive>          # re-check an old archive
#   ./backup.sh --restore <archive>         # unpack into a staging directory
#   ./backup.sh --restore <archive> --in-place   # put it back where it came from
#   ./backup.sh --install-timer daily       # run it every night via systemd
#
# Options
#       --path <dir>       add a directory to the backup (repeatable)
#       --volume <name>    add a Docker named volume (repeatable)
#       --auto             detect Compose projects under /opt, plus /etc
#       --name <label>     archive name prefix (default: the hostname)
#       --dest <dir>       where archives live (default: /var/backups/toolkit)
#       --stop <dir>       docker compose stop this project during the copy
#       --exclude <glob>   skip matching paths (repeatable)
#       --keep <n>         keep this many archives, delete older (default: 7)
#       --keep-days <n>    also delete anything older than n days
#       --rsync <target>   rsync the archive to another host afterwards
#       --list             list the archives in --dest
#       --verify <file>    check an archive reads back and matches its checksum
#       --restore <file>   restore an archive (staging directory by default)
#       --to <dir>         where --restore unpacks (default: a staging directory)
#       --in-place         --restore writes back to the original paths
#       --install-timer <daily|weekly|hourly>   install a systemd timer
#       --uninstall-timer  remove the timer
#   -n, --dry-run          print what would happen, change nothing
#   -y, --yes              do not ask for confirmation
#   -h, --help             this text
#
set -uo pipefail
export LC_ALL=C

BACKUP_VERSION="1.0"
INVOKED_BARE=$([ "$#" -eq 0 ] && echo 1 || echo 0)
ACTION="create"
DEST="/var/backups/toolkit"
NAME=""
KEEP=7
KEEP_DAYS=0
DRY_RUN=0
ASSUME_YES=0
AUTO=0
IN_PLACE=0
STOP_DIR=""
RSYNC_TARGET=""
RESTORE_TO=""
TARGET_FILE=""
TIMER_WHEN="daily"
PATHS=()
VOLUMES=()
EXCLUDES=()

# ---- pretty output ---------------------------------------------------------- #
if [ -t 1 ]; then
    C_B=$'\033[1m'; C_D=$'\033[2m'; C_0=$'\033[0m'
    C_I=$'\033[38;5;75m'; C_OK=$'\033[38;5;114m'; C_W=$'\033[38;5;221m'
    C_E=$'\033[38;5;203m'; C_G=$'\033[38;5;245m'
else
    C_B=''; C_D=''; C_0=''; C_I=''; C_OK=''; C_W=''; C_E=''; C_G=''
fi
info() { printf '%s[*]%s %s\n' "$C_I" "$C_0" "$*"; }
ok()   { printf '%s[+]%s %s\n' "$C_OK" "$C_0" "$*"; }
warn() { printf '%s[!]%s %s\n' "$C_W" "$C_0" "$*" >&2; }
die()  { printf '%s[x]%s %s\n' "$C_E" "$C_0" "$*" >&2; exit 1; }
step() { printf '%s──%s %s\n' "$C_G" "$C_0" "$*"; }

# ---- arguments ---------------------------------------------------------------- #
while [ $# -gt 0 ]; do
    case "$1" in
        --path)          shift; PATHS+=("${1:-}") ;;
        --path=*)        PATHS+=("${1#*=}") ;;
        --volume)        shift; VOLUMES+=("${1:-}") ;;
        --volume=*)      VOLUMES+=("${1#*=}") ;;
        --exclude)       shift; EXCLUDES+=("${1:-}") ;;
        --exclude=*)     EXCLUDES+=("${1#*=}") ;;
        --auto)          AUTO=1 ;;
        --name)          shift; NAME="${1:-}" ;;
        --name=*)        NAME="${1#*=}" ;;
        --dest)          shift; DEST="${1:-}" ;;
        --dest=*)        DEST="${1#*=}" ;;
        --stop)          shift; STOP_DIR="${1:-}" ;;
        --stop=*)        STOP_DIR="${1#*=}" ;;
        --keep)          shift; KEEP="${1:-7}" ;;
        --keep=*)        KEEP="${1#*=}" ;;
        --keep-days)     shift; KEEP_DAYS="${1:-0}" ;;
        --keep-days=*)   KEEP_DAYS="${1#*=}" ;;
        --rsync)         shift; RSYNC_TARGET="${1:-}" ;;
        --rsync=*)       RSYNC_TARGET="${1#*=}" ;;
        --list)          ACTION="list" ;;
        --verify)        ACTION="verify"; shift; TARGET_FILE="${1:-}" ;;
        --verify=*)      ACTION="verify"; TARGET_FILE="${1#*=}" ;;
        --restore)       ACTION="restore"; shift; TARGET_FILE="${1:-}" ;;
        --restore=*)     ACTION="restore"; TARGET_FILE="${1#*=}" ;;
        --to)            shift; RESTORE_TO="${1:-}" ;;
        --to=*)          RESTORE_TO="${1#*=}" ;;
        --in-place)      IN_PLACE=1 ;;
        --install-timer) ACTION="timer"
                         case "${2:-}" in daily|weekly|hourly) TIMER_WHEN="$2"; shift ;; esac ;;
        --uninstall-timer) ACTION="untimer" ;;
        -n|--dry-run)    DRY_RUN=1 ;;
        -y|--yes)        ASSUME_YES=1 ;;
        -h|--help)       grep '^#' "$0" | grep -v '^#!' | grep -v '^# toolkit-' \
                             | sed 's/^#\{1,\} \{0,1\}//'; exit 0 ;;
        *) die "Unknown option: $1  (try --help)" ;;
    esac
    shift
done

have() { command -v "$1" >/dev/null 2>&1; }

IS_ROOT=0; SUDO=""
if [ "$(id -u)" -eq 0 ]; then
    IS_ROOT=1
elif have sudo; then
    SUDO="sudo"
fi
run() {
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '%s   would run:%s %s\n' "$C_D" "$C_0" "$*"
        return 0
    fi
    if [ "$IS_ROOT" -eq 1 ]; then "$@"; else $SUDO "$@"; fi
}

human() {   # bytes -> human readable
    awk -v b="${1:-0}" 'BEGIN{
        split("B KiB MiB GiB TiB", u, " "); i = 1
        while (b >= 1024 && i < 5) { b /= 1024; i++ }
        printf (i == 1 ? "%d %s" : "%.1f %s"), b, u[i]
    }'
}

dir_size() {   # bytes used by a path, 0 when unreadable
    local total
    total="$(du -sb "$1" 2>/dev/null | cut -f1)"
    printf '%s' "${total:-0}"
}

# ---- compression --------------------------------------------------------------- #
if have zstd; then
    COMP_CMD="zstd -T0 -3 -q"; COMP_EXT="tar.zst"; DECOMP_CMD="zstd -d -q -c"
elif have pigz; then
    COMP_CMD="pigz"; COMP_EXT="tar.gz"; DECOMP_CMD="pigz -d -c"
else
    COMP_CMD="gzip"; COMP_EXT="tar.gz"; DECOMP_CMD="gzip -d -c"
fi

# ---- sources -------------------------------------------------------------------- #
compose_file_in() {
    local dir="$1" f
    for f in compose.yaml compose.yml docker-compose.yml docker-compose.yaml; do
        [ -f "$dir/$f" ] && { printf '%s' "$dir/$f"; return 0; }
    done
    return 1
}

collect_auto() {
    local dir
    for dir in /opt/*/; do
        [ -d "$dir" ] || continue
        if compose_file_in "${dir%/}" >/dev/null; then
            PATHS+=("${dir%/}")
            info "auto: Compose project ${dir%/}"
        fi
    done
    if [ -d /etc ]; then
        PATHS+=("/etc")
        info "auto: /etc"
    fi
}

volume_path() {   # docker volume name -> host path
    have docker || return 1
    $SUDO docker volume inspect -f '{{ .Mountpoint }}' "$1" 2>/dev/null
}

# ---- create --------------------------------------------------------------------- #
compose_cmd() {
    if $SUDO docker compose version >/dev/null 2>&1; then
        printf 'docker compose'
    elif have docker-compose; then
        printf 'docker-compose'
    else
        return 1
    fi
}

stack_stop() {
    [ -n "$STOP_DIR" ] || return 0
    local cc; cc="$(compose_cmd)" || { warn "docker compose not found — copying hot"; return 0; }
    step "Stopping the stack in $STOP_DIR for a consistent copy"
    # shellcheck disable=SC2086  # cc is "docker compose" or "docker-compose"
    run env -C "$STOP_DIR" $cc stop
}

stack_start() {
    [ -n "$STOP_DIR" ] || return 0
    local cc; cc="$(compose_cmd)" || return 0
    step "Starting the stack in $STOP_DIR again"
    # shellcheck disable=SC2086
    run env -C "$STOP_DIR" $cc start
}

do_create() {
    [ "$AUTO" -eq 1 ] && collect_auto

    local vol path resolved
    for vol in ${VOLUMES+"${VOLUMES[@]}"}; do
        [ -n "$vol" ] || continue
        resolved="$(volume_path "$vol")"
        [ -n "$resolved" ] || die "Docker volume '$vol' not found (or docker needs root)."
        PATHS+=("$resolved")
        info "volume $vol -> $resolved"
    done

    if [ "${#PATHS[@]}" -eq 0 ]; then
        if [ "$INVOKED_BARE" -eq 1 ]; then
            do_list
            return 0
        fi
        die "Nothing to back up. Use --path, --volume or --auto."
    fi

    local existing=() missing=() total=0 size
    for path in "${PATHS[@]}"; do
        if [ -e "$path" ]; then
            existing+=("$path")
            size="$(dir_size "$path")"
            total=$((total + size))
            printf '  %s%-46s%s %s\n' "$C_B" "$path" "$C_0" "$(human "$size")"
        else
            missing+=("$path")
            warn "not found, skipping: $path"
        fi
    done
    [ "${#existing[@]}" -gt 0 ] || die "None of the requested paths exist."

    local stamp label archive serial=1
    stamp="$(date +%Y%m%d-%H%M%S)"
    label="${NAME:-$(hostname -s 2>/dev/null || echo backup)}"
    archive="$DEST/${label}-${stamp}.${COMP_EXT}"
    # Two runs in the same second must not overwrite each other's archive.
    while [ -e "$archive" ]; do
        archive="$DEST/${label}-${stamp}-${serial}.${COMP_EXT}"
        serial=$((serial + 1))
    done

    printf '\n'
    step "Source total $(human "$total")  →  $archive"
    [ -n "$STOP_DIR" ] || warn "Copying while services run: a database being written to may be captured mid-write. Use --stop <compose-dir> for a consistent copy."

    if [ "$ASSUME_YES" -eq 0 ] && [ "$DRY_RUN" -eq 0 ] && [ -t 0 ]; then
        printf 'Continue? [Y/n] '
        local reply=""
        read -r reply </dev/tty 2>/dev/null || reply="y"
        case "${reply:-y}" in [nN]*) die "Cancelled." ;; esac
    fi

    run mkdir -p "$DEST"

    local tar_args=(--warning=no-file-changed --warning=no-file-ignored -cf -)
    local ex
    for ex in ${EXCLUDES+"${EXCLUDES[@]}"}; do
        [ -n "$ex" ] && tar_args+=("--exclude=$ex")
    done
    # Never let a backup swallow itself.
    tar_args+=("--exclude=${DEST%/}/*")
    tar_args+=(--absolute-names "${existing[@]}")

    local started ended elapsed
    started="$(date +%s)"
    stack_stop
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '%s   would run:%s tar %s | %s > %s\n' \
            "$C_D" "$C_0" "${tar_args[*]}" "$COMP_CMD" "$archive"
    else
        step "Archiving"
        # tar exits 1 for "file changed as we read it", which is expected on a
        # live system and not a reason to throw the archive away; 2 is fatal.
        if [ "$IS_ROOT" -eq 1 ]; then
            tar "${tar_args[@]}" 2>/dev/null | $COMP_CMD > "$archive"
        else
            $SUDO tar "${tar_args[@]}" 2>/dev/null | $COMP_CMD | $SUDO tee "$archive" >/dev/null
        fi
        local rc=${PIPESTATUS[0]}
        [ "$rc" -le 1 ] || { stack_start; die "tar failed with status $rc — archive discarded."; }
    fi
    stack_start
    ended="$(date +%s)"
    elapsed=$((ended - started))

    if [ "$DRY_RUN" -eq 1 ]; then
        printf '\n'
        info "Dry run — nothing was written."
        return 0
    fi

    local archive_size
    archive_size="$(stat -c %s "$archive" 2>/dev/null || echo 0)"
    [ "$archive_size" -gt 0 ] || die "The archive is empty — something went wrong."

    verify_archive "$archive" || die "The archive did not read back cleanly — it was NOT kept."
    write_meta "$archive" "$archive_size" "$total" "$elapsed" "${existing[@]}"

    ok "Backup complete: $archive"
    printf '    %s%s from %s of source, %ss, ratio %s%s\n' "$C_G" \
        "$(human "$archive_size")" "$(human "$total")" "$elapsed" \
        "$(awk -v a="$archive_size" -v b="$total" 'BEGIN{ printf (b>0 ? "%.1fx" : "-"), b/a }')" \
        "$C_0"

    prune
    push_remote "$archive"
}

write_meta() {
    local archive="$1" archive_size="$2" source_size="$3" elapsed="$4"
    shift 4
    local sum="" tmp
    if have sha256sum; then
        sum="$(sha256sum "$archive" 2>/dev/null | awk '{print $1}')"
    fi
    tmp="$(mktemp)"
    {
        printf 'archive=%s\n' "$(basename "$archive")"
        printf 'created=%s\n' "$(date '+%Y-%m-%d %H:%M:%S %z')"
        printf 'host=%s\n' "$(hostname)"
        printf 'tool=backup.sh %s\n' "$BACKUP_VERSION"
        printf 'archive_bytes=%s\n' "$archive_size"
        printf 'source_bytes=%s\n' "$source_size"
        printf 'seconds=%s\n' "$elapsed"
        printf 'consistent=%s\n' "$([ -n "$STOP_DIR" ] && echo "yes (stack stopped)" || echo "no (hot copy)")"
        [ -n "$sum" ] && printf 'sha256=%s\n' "$sum"
        printf 'sources:\n'
        printf '  %s\n' "$@"
    } >"$tmp"
    run cp "$tmp" "${archive}.meta"
    rm -f "$tmp"
    [ -n "$sum" ] && printf '%s  %s\n' "$sum" "$(basename "$archive")" \
        | run tee "${archive}.sha256" >/dev/null
    return 0
}

verify_archive() {
    local archive="$1"
    step "Verifying $(basename "$archive")"
    local listing files rc
    listing="$(mktemp)"
    # pipefail is on, so this catches a decompressor that died mid-stream as
    # well as a tar that hit an unexpected end of archive.
    $DECOMP_CMD <"$archive" 2>/dev/null | tar -tf - >"$listing" 2>/dev/null
    rc=$?
    files="$(wc -l <"$listing" 2>/dev/null)"
    rm -f "$listing"
    if [ "$rc" -ne 0 ]; then
        warn "the archive did not read back cleanly (status $rc after ${files:-0} entries)"
        return 1
    fi
    if [ "${files:-0}" -lt 1 ]; then
        warn "could not list any files inside the archive"
        return 1
    fi
    if [ -f "${archive}.sha256" ] && have sha256sum; then
        if (cd "$(dirname "$archive")" && sha256sum -c "$(basename "${archive}.sha256")" \
                >/dev/null 2>&1); then
            ok "$files files, checksum matches"
        else
            warn "checksum does NOT match — the archive changed after it was written"
            return 1
        fi
    else
        ok "$files files readable"
    fi
    return 0
}

prune() {
    [ "$KEEP" -gt 0 ] 2>/dev/null || return 0
    local archives=() f
    while IFS= read -r f; do archives+=("$f"); done < <(
        find "$DEST" -maxdepth 1 -name "*.tar.zst" -o -maxdepth 1 -name "*.tar.gz" \
            2>/dev/null | sort)
    local count=${#archives[@]}
    [ "$count" -gt "$KEEP" ] || return 0
    step "Keeping the newest $KEEP of $count archives"
    local drop=$((count - KEEP)) i=0
    for f in "${archives[@]}"; do
        [ "$i" -lt "$drop" ] || break
        i=$((i + 1))
        info "removing $(basename "$f")"
        run rm -f "$f" "${f}.meta" "${f}.sha256"
    done
    if [ "$KEEP_DAYS" -gt 0 ] 2>/dev/null; then
        while IFS= read -r f; do
            info "removing (older than ${KEEP_DAYS}d) $(basename "$f")"
            run rm -f "$f" "${f}.meta" "${f}.sha256"
        done < <(find "$DEST" -maxdepth 1 \( -name '*.tar.zst' -o -name '*.tar.gz' \) \
                     -mtime "+$KEEP_DAYS" 2>/dev/null)
    fi
}

push_remote() {
    [ -n "$RSYNC_TARGET" ] || return 0
    have rsync || { warn "rsync not installed — skipping the remote copy"; return 0; }
    step "Copying to $RSYNC_TARGET"
    run rsync -a --partial "$1" "${1}.meta" "$RSYNC_TARGET" \
        && ok "Remote copy done"
}

# ---- list / verify / restore ------------------------------------------------------ #
do_list() {
    # Having nothing yet is the normal state of a fresh machine, not an error —
    # this is what the launcher runs when someone opens the entry to look at it.
    if [ ! -d "$DEST" ]; then
        printf '\n  %sNo backups yet.%s Nothing has been written to %s.\n\n' \
            "$C_B" "$C_0" "$DEST"
        printf '  %sMake the first one with:%s ./backup.sh --auto\n' "$C_G" "$C_0"
        printf '  %sOr pick what to keep:%s   ./backup.sh --path /opt/my-stack\n\n' "$C_G" "$C_0"
        return 0
    fi
    local found=0 f size when
    printf '\n%sArchives in %s%s\n\n' "$C_B" "$DEST" "$C_0"
    while IFS= read -r f; do
        found=$((found + 1))
        size="$(stat -c %s "$f" 2>/dev/null || echo 0)"
        when="$(date -r "$f" '+%Y-%m-%d %H:%M' 2>/dev/null)"
        printf '  %s%-44s%s %10s  %s' "$C_B" "$(basename "$f")" "$C_0" \
            "$(human "$size")" "$when"
        if [ -f "${f}.meta" ]; then
            printf '  %s%s%s' "$C_G" "$(awk -F= '/^consistent=/{print $2}' "${f}.meta")" "$C_0"
        fi
        printf '\n'
    done < <(find "$DEST" -maxdepth 1 \( -name '*.tar.zst' -o -name '*.tar.gz' \) \
                 2>/dev/null | sort -r)
    [ "$found" -gt 0 ] || warn "no archives yet — run without --list to make one"
    printf '\n'
    if [ "$found" -gt 0 ]; then
        printf '  %sRestore with:%s ./backup.sh --restore %s/<archive>\n\n' \
            "$C_G" "$C_0" "$DEST"
    fi
}

resolve_archive() {
    local f="$1"
    [ -n "$f" ] || die "Which archive? Pass a path (see --list)."
    [ -f "$f" ] && { printf '%s' "$f"; return 0; }
    [ -f "$DEST/$f" ] && { printf '%s' "$DEST/$f"; return 0; }
    die "No such archive: $f"
}

do_verify() {
    local archive; archive="$(resolve_archive "$TARGET_FILE")" || exit 1
    if verify_archive "$archive"; then
        [ -f "${archive}.meta" ] && { printf '\n'; cat "${archive}.meta"; }
        ok "Archive is intact."
        exit 0
    fi
    die "Archive FAILED verification."
}

do_restore() {
    local archive; archive="$(resolve_archive "$TARGET_FILE")" || exit 1
    verify_archive "$archive" || die "Refusing to restore an archive that does not verify."

    local target
    if [ "$IN_PLACE" -eq 1 ]; then
        target="/"
        warn "IN-PLACE restore: files will overwrite what is on disk right now."
    else
        target="${RESTORE_TO:-/var/tmp/toolkit-restore-$(date +%Y%m%d-%H%M%S)}"
        info "Restoring into $target (nothing on the live system is touched)."
        info "Add --in-place once you have checked the contents."
    fi

    if [ "$ASSUME_YES" -eq 0 ] && [ "$DRY_RUN" -eq 0 ] && [ -t 0 ]; then
        if [ "$IN_PLACE" -eq 1 ]; then
            printf 'Type %sRESTORE%s to overwrite the live system: ' "$C_B" "$C_0"
            local reply=""
            read -r reply </dev/tty 2>/dev/null || reply=""
            [ "$reply" = "RESTORE" ] || die "Not confirmed — nothing was changed."
        else
            printf 'Continue? [Y/n] '
            local reply=""
            read -r reply </dev/tty 2>/dev/null || reply="y"
            case "${reply:-y}" in [nN]*) die "Cancelled." ;; esac
        fi
    fi

    if [ "$DRY_RUN" -eq 1 ]; then
        printf '%s   would extract %s into %s%s\n' "$C_D" "$archive" "$target" "$C_0"
        $DECOMP_CMD <"$archive" 2>/dev/null | tar -tf - 2>/dev/null | head -20
        return 0
    fi

    run mkdir -p "$target"
    step "Extracting"
    # Only an in-place restore may write to the absolute paths inside the
    # archive; anywhere else, tar must strip the leading / and stay under
    # --to, or "staging" would mean "overwrite the live system".
    local extract_args=(-xf - -C "$target")
    [ "$IN_PLACE" -eq 1 ] && extract_args+=(--absolute-names)
    if [ "$IS_ROOT" -eq 1 ]; then
        $DECOMP_CMD <"$archive" | tar "${extract_args[@]}" 2>/dev/null
    else
        $DECOMP_CMD <"$archive" | $SUDO tar "${extract_args[@]}" 2>/dev/null
    fi
    local rc=$?
    [ "$rc" -le 1 ] || die "Extraction failed with status $rc."
    ok "Restored into $target"
    [ "$IN_PLACE" -eq 1 ] || info "Check it, then copy what you need — or re-run with --in-place."
}

# ---- systemd timer ------------------------------------------------------------------ #
SERVICE_UNIT="/etc/systemd/system/toolkit-backup.service"
TIMER_UNIT="/etc/systemd/system/toolkit-backup.timer"

do_timer() {
    have systemctl || die "systemd is not available on this machine."
    local self; self="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
    local args="--yes --keep $KEEP --dest $DEST"
    [ "$AUTO" -eq 1 ] && args="$args --auto"
    local p
    for p in ${PATHS+"${PATHS[@]}"}; do args="$args --path $p"; done
    for p in ${VOLUMES+"${VOLUMES[@]}"}; do args="$args --volume $p"; done
    [ -n "$STOP_DIR" ] && args="$args --stop $STOP_DIR"
    [ -n "$RSYNC_TARGET" ] && args="$args --rsync $RSYNC_TARGET"

    local tmp_service tmp_timer
    tmp_service="$(mktemp)"; tmp_timer="$(mktemp)"
    cat >"$tmp_service" <<EOF
[Unit]
Description=toolkit backup
Documentation=https://github.com/DenisHumen/toolkit
After=network-online.target docker.service

[Service]
Type=oneshot
ExecStart=$self $args
Nice=10
IOSchedulingClass=idle
EOF
    cat >"$tmp_timer" <<EOF
[Unit]
Description=toolkit backup ($TIMER_WHEN)

[Timer]
OnCalendar=$TIMER_WHEN
RandomizedDelaySec=15m
Persistent=true

[Install]
WantedBy=timers.target
EOF
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '%s   would write %s:%s\n' "$C_D" "$SERVICE_UNIT" "$C_0"
        sed 's/^/     │ /' "$tmp_service"
        printf '%s   would write %s:%s\n' "$C_D" "$TIMER_UNIT" "$C_0"
        sed 's/^/     │ /' "$tmp_timer"
        rm -f "$tmp_service" "$tmp_timer"
        return 0
    fi
    run cp "$tmp_service" "$SERVICE_UNIT"
    run cp "$tmp_timer" "$TIMER_UNIT"
    rm -f "$tmp_service" "$tmp_timer"
    run systemctl daemon-reload
    run systemctl enable --now toolkit-backup.timer
    ok "Timer installed — running $TIMER_WHEN"
    info "Next run: $($SUDO systemctl list-timers toolkit-backup.timer --no-pager 2>/dev/null | sed -n 2p)"
    info "Run it now with: systemctl start toolkit-backup.service"
}

do_untimer() {
    have systemctl || die "systemd is not available on this machine."
    run systemctl disable --now toolkit-backup.timer 2>/dev/null
    run rm -f "$TIMER_UNIT" "$SERVICE_UNIT"
    run systemctl daemon-reload
    ok "Timer removed. Existing archives were left alone."
}

# ==================================================================================== #
have tar || die "tar is required and was not found."

case "$ACTION" in
    create)  do_create ;;
    list)    do_list ;;
    verify)  do_verify ;;
    restore) do_restore ;;
    timer)   do_timer ;;
    untimer) do_untimer ;;
esac
