#!/usr/bin/env bash
#
# proxmox-wipe.sh — destroy all guests and ZERO every NON-system disk on this host,
#                   with a live progress bar + ETA (dd) for the zeroing phase.
#
# SAFETY MODEL:
#   * System disk(s) backing / /boot /boot/efi are auto-detected by TWO independent
#     methods and PROTECTED. If detection yields nothing valid -> ABORT.
#   * Use --only to name EXACTLY which disks to wipe (recommended). A system disk in
#     that list is rejected.
#   * Always --dry-run first and verify the [KEEP]/[WIPE] lists.
#   * If /etc/pve is read-only (cluster node without quorum), the script tries
#     'pvecm expected 1' so guest .conf files can be removed.
#
# WIPE METHOD:
#   * Default: dd if=/dev/zero  -> shows a live progress bar + per-disk & overall ETA.
#   * --discard: use blkdiscard -z (fast hardware zero, NO progress bar). Falls back
#     to dd if the controller doesn't support discard.
#
# Usage:
#   ./proxmox-wipe.sh --dry-run
#   ./proxmox-wipe.sh --only sdb,sdc,sdd,sde --dry-run
#   ./proxmox-wipe.sh --only sdb,sdc,sdd,sde
#   ./proxmox-wipe.sh                 (wipe ALL non-system disks)
#   options: --yes (skip prompt)   --discard (fast, no bar)
#
set -uo pipefail
export LC_ALL=C

DRY_RUN=0; ASSUME_YES=0; ONLY_LIST=""; USE_DISCARD=0
while [ $# -gt 0 ]; do
    case "$1" in
        -n|--dry-run) DRY_RUN=1 ;;
        -y|--yes)     ASSUME_YES=1 ;;
        --discard)    USE_DISCARD=1 ;;
        --only)       shift; ONLY_LIST="${1:-}" ;;
        --only=*)     ONLY_LIST="${1#*=}" ;;
        -h|--help)    grep '^#' "$0" | sed 's/^#\{1,\} \{0,1\}//'; exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

LOG="/var/log/proxmox-wipe-$(date +%Y%m%d-%H%M%S).log"
log()  { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG"; }
warn() { echo "[$(date +%H:%M:%S)] WARN: $*" | tee -a "$LOG" >&2; }
die()  { echo "[$(date +%H:%M:%S)] ERROR: $*" | tee -a "$LOG" >&2; exit 1; }
run()  { if [ "$DRY_RUN" -eq 1 ]; then echo "    DRY: $*" | tee -a "$LOG";
         else log "RUN: $*"; eval "$@"; fi; }
trap 'echo; warn "Interrupted by user."; exit 130' INT

[ "$(id -u)" -eq 0 ] || die "Must run as root."
command -v lsblk >/dev/null 2>&1 || die "lsblk not found."
command -v qm    >/dev/null 2>&1 || warn "qm not found — is this Proxmox? Guest destruction will be skipped."

norm_disk() { case "$1" in /dev/*) echo "$1";; *) echo "/dev/$1";; esac; }

# ---- ASCII UI helpers (render fine on iLO/IPMI consoles) ----
human() { awk -v b="$1" 'BEGIN{split("B KiB MiB GiB TiB PiB",u," ");i=1;
          while(b>=1024&&i<6){b/=1024;i++} printf "%.2f %s",b,u[i]}'; }
hms()   { local s=$1; [ "$s" -lt 0 ] 2>/dev/null && s=0
          printf "%02d:%02d:%02d" $((s/3600)) $(((s%3600)/60)) $((s%60)); }
bar()   { local pct="$1" w="$2" out="" i f
          f=$(( pct * w / 100 ))
          for ((i=0; i<w; i++)); do if [ "$i" -lt "$f" ]; then out+="#"; else out+="."; fi; done
          printf "%s" "$out"; }

###############################################################################
# 1. Detect & protect the system disk(s) — TWO independent methods
###############################################################################
declare -A SYSTEM_DISKS
add_disks_under() { local dev="$1" name type
    while read -r name type; do [ "$type" = "disk" ] && SYSTEM_DISKS["$name"]=1
    done < <(lsblk -nslpo NAME,TYPE "$dev" 2>/dev/null); }
add_zpool_disks() { local pool="$1" dev real
    while read -r dev; do [ -n "$dev" ] || continue
        real="$(readlink -f "$dev" 2>/dev/null || echo "$dev")"; add_disks_under "$real"
    done < <(zpool status -P "$pool" 2>/dev/null | grep -oE '/dev/[^[:space:]]+'); }

root_pool=""
for mp in / /boot /boot/efi; do
    src=""; fstype=""
    read -r src fstype < <(findmnt -nM "$mp" -o SOURCE,FSTYPE 2>/dev/null)
    [ -n "$src" ] || continue
    if [ "$fstype" = "zfs" ] || { [[ "$src" != /* ]] && [[ "$src" == *"/"* ]]; }; then
        pool="${src%%/*}"; [ "$mp" = "/" ] && root_pool="$pool"; add_zpool_disks "$pool"
    elif [[ "$src" == /* ]]; then add_disks_under "$src"; fi
done
cur=""
while IFS= read -r line; do
    NAME=""; TYPE=""; MOUNTPOINT=""; eval "$line"
    [ "$TYPE" = "disk" ] && cur="/dev/$NAME"
    case "$MOUNTPOINT" in /|/boot|/boot/efi) [ -n "$cur" ] && SYSTEM_DISKS["$cur"]=1 ;; esac
done < <(lsblk -Pno NAME,TYPE,MOUNTPOINT 2>/dev/null)

###############################################################################
# 2. Enumerate disks, validate protection, compute data set
###############################################################################
ALL_DISKS=()
while read -r name type; do
    case "$name" in /dev/zd*|/dev/zram*|/dev/rbd*|/dev/nbd*|/dev/loop*|/dev/sr*|/dev/fd*) continue ;; esac
    [ "$type" = "disk" ] && ALL_DISKS+=("$name")
done < <(lsblk -dnpo NAME,TYPE 2>/dev/null)

for k in "${!SYSTEM_DISKS[@]}"; do
    keep=0; for a in "${ALL_DISKS[@]}"; do [ "$k" = "$a" ] && keep=1 && break; done
    [ "$keep" -eq 1 ] || unset 'SYSTEM_DISKS[$k]'
done
[ "${#SYSTEM_DISKS[@]}" -gt 0 ] || die "No VALID system disk detected — refusing. Run: lsblk ; findmnt -no SOURCE /"

DATA_DISKS=()
for d in "${ALL_DISKS[@]}"; do [ -n "${SYSTEM_DISKS[$d]:-}" ] && continue; DATA_DISKS+=("$d"); done

if [ -n "$ONLY_LIST" ]; then
    REQ=()
    for x in ${ONLY_LIST//,/ }; do REQ+=("$(norm_disk "$x")"); done
    for r in "${REQ[@]}"; do
        ok=0; for a in "${ALL_DISKS[@]}"; do [ "$a" = "$r" ] && ok=1; done
        [ "$ok" -eq 1 ] || die "--only: '$r' is not a physical disk here."
        [ -n "${SYSTEM_DISKS[$r]:-}" ] && die "--only: '$r' is a SYSTEM disk — refusing."
    done
    DATA_DISKS=("${REQ[@]}")
fi

echo
echo "============================================================"
echo " HOST: $(hostname)   $( [ "$DRY_RUN" -eq 1 ] && echo '   *** DRY-RUN ***' )"
echo "============================================================"
echo "PROTECTED system disk(s) — will NOT be touched:"
for d in "${!SYSTEM_DISKS[@]}"; do lsblk -dno NAME,SIZE,MODEL "$d" 2>/dev/null | sed 's/^/   [KEEP] /'; done
echo
echo "DATA disk(s) — will be ERASED (zeroed):"
if [ "${#DATA_DISKS[@]}" -eq 0 ]; then echo "   (none)"; else
    for d in "${DATA_DISKS[@]}"; do lsblk -dno NAME,SIZE,MODEL "$d" 2>/dev/null | sed 's/^/   [WIPE] /'; done
fi
echo
echo "Guests to be DESTROYED:"
qm  list 2>/dev/null | awk 'NR>1{printf "   VM  %-6s %s\n",$1,$2}'
pct list 2>/dev/null | awk 'NR>1{printf "   CT  %-6s\n",$1}'
echo

if [ "$DRY_RUN" -eq 1 ]; then log "DRY-RUN: previewing only; nothing will be changed.";
elif [ "$ASSUME_YES" -ne 1 ]; then
    echo "This PERMANENTLY destroys all guests and ZEROES the [WIPE] disks above. NO undo."
    read -r -p 'Type exactly  ERASE-ALL-DATA  to proceed: ' ans
    [ "$ans" = "ERASE-ALL-DATA" ] || die "Confirmation mismatch — aborted."
fi
log "Logging to $LOG"

###############################################################################
# 3. Destroy all guests (restore /etc/pve write access first if needed)
###############################################################################
ensure_pmxcfs_writable() {
    [ -d /etc/pve ] || return 0
    if touch /etc/pve/.wipe_wtest 2>/dev/null; then rm -f /etc/pve/.wipe_wtest; return 0; fi
    warn "/etc/pve is read-only (no quorum) — guest configs can't be removed."
    if command -v pvecm >/dev/null 2>&1; then
        log "Restoring local quorum: pvecm expected 1"
        pvecm expected 1 >>"$LOG" 2>&1 || true; sleep 2
        if touch /etc/pve/.wipe_wtest 2>/dev/null; then rm -f /etc/pve/.wipe_wtest
            log "/etc/pve is now writable."; return 0; fi
    fi
    warn "Still read-only — disks will be wiped, but .conf files may remain."
}
[ "$DRY_RUN" -eq 0 ] && ensure_pmxcfs_writable

if command -v pct >/dev/null 2>&1; then
    mapfile -t CTS < <(pct list 2>/dev/null | awk 'NR>1{print $1}')
    gi=0; gn=${#CTS[@]}; [ "$gn" -gt 0 ] && log "Destroying $gn LXC container(s)..."
    for id in "${CTS[@]}"; do [ -n "$id" ] || continue; gi=$((gi+1)); log "  CT $id ($gi/$gn)"
        run "pct stop $id --skiplock || true"
        run "pct destroy $id --force --purge || true"
    done
fi
if command -v qm >/dev/null 2>&1; then
    mapfile -t VMS < <(qm list 2>/dev/null | awk 'NR>1{print $1}')
    gi=0; gn=${#VMS[@]}; [ "$gn" -gt 0 ] && log "Destroying $gn virtual machine(s)..."
    for id in "${VMS[@]}"; do [ -n "$id" ] || continue; gi=$((gi+1)); log "  VM $id ($gi/$gn)"
        run "qm stop $id --skiplock || true"
        run "qm destroy $id --purge --destroy-unreferenced-disks --skiplock || true"
    done
fi

###############################################################################
# 4. Best-effort teardown of data storage on the [WIPE] disks
###############################################################################
in_data_set() { local x; for x in "${DATA_DISKS[@]}"; do [ "$x" = "$1" ] && return 0; done; return 1; }

if command -v zpool >/dev/null 2>&1; then
    while read -r pool; do [ -n "$pool" ] || continue; [ "$pool" = "$root_pool" ] && continue
        only=1; found=0
        while read -r dev; do [ -n "$dev" ] || continue; found=1
            real="$(readlink -f "$dev" 2>/dev/null || echo "$dev")"
            while read -r nm tp; do [ "$tp" = "disk" ] || continue; in_data_set "$nm" || only=0
            done < <(lsblk -nslpo NAME,TYPE "$real" 2>/dev/null)
        done < <(zpool status -P "$pool" 2>/dev/null | grep -oE '/dev/[^[:space:]]+')
        if [ "$found" -eq 1 ] && [ "$only" -eq 1 ]; then log "Destroying ZFS pool: $pool"
            run "zpool destroy -f '$pool' || zpool export -f '$pool' || true"
        else warn "Skipping ZFS pool '$pool' (touches a protected disk)."; fi
    done < <(zpool list -H -o name 2>/dev/null)
fi
if command -v vgs >/dev/null 2>&1; then
    while read -r vg; do [ -n "$vg" ] || continue; only=1; found=0
        while read -r pv; do [ -n "$pv" ] || continue; found=1
            real="$(readlink -f "$pv" 2>/dev/null || echo "$pv")"
            while read -r nm tp; do [ "$tp" = "disk" ] || continue; in_data_set "$nm" || only=0
            done < <(lsblk -nslpo NAME,TYPE "$real" 2>/dev/null)
        done < <(pvs --noheadings -o pv_name,vg_name 2>/dev/null | awk -v v="$vg" '$2==v{print $1}')
        if [ "$found" -eq 1 ] && [ "$only" -eq 1 ]; then log "Removing LVM VG: $vg"
            run "vgchange -an '$vg' || true"; run "vgremove -ff '$vg' || true"
        else warn "Skipping VG '$vg' (touches a protected disk)."; fi
    done < <(vgs --noheadings -o vg_name 2>/dev/null | tr -d ' ')
fi
for d in "${DATA_DISKS[@]}"; do
    while IFS= read -r line; do
        NAME=""; MOUNTPOINT=""; FSTYPE=""; TYPE=""; eval "$line"
        if [ "${FSTYPE:-}" = "swap" ] && grep -q "^${NAME} " /proc/swaps 2>/dev/null; then
            run "swapoff '$NAME' || true"
        fi
        [ -n "${MOUNTPOINT:-}" ] && run "umount -lf '$MOUNTPOINT' || true"
        [ "${TYPE:-}" = "crypt" ] && run "cryptsetup close '$(basename "$NAME")' || true"
    done < <(lsblk -Ppno NAME,MOUNTPOINT,FSTYPE,TYPE "$d" 2>/dev/null)
done

###############################################################################
# 5. Zero the data disks — with live progress + ETA
###############################################################################
GRAND_TOTAL=0
for d in "${DATA_DISKS[@]}"; do
    s=$(blockdev --getsize64 "$d" 2>/dev/null || echo 0); GRAND_TOTAL=$((GRAND_TOTAL+s))
done
GLOBAL_DONE=0
[ "${#DATA_DISKS[@]}" -gt 0 ] && [ "$DRY_RUN" -eq 0 ] && \
    log "Zeroing ${#DATA_DISKS[@]} disk(s), total $(human "$GRAND_TOTAL")"

zero_with_progress() {           # disk size idx total
    local disk="$1" size="$2" idx="$3" total="$4"
    local tmp; tmp="$(mktemp)"
    local tty=/dev/tty; [ -w /dev/tty ] || tty=/dev/stderr
    dd if=/dev/zero of="$disk" bs=8M conv=fdatasync,noerror status=progress 2>"$tmp" &
    local pid=$! prev_b=0 prev_t rate=0; prev_t=$(date +%s)
    while kill -0 "$pid" 2>/dev/null; do
        sleep 2
        local b; b=$(tr '\r' '\n' < "$tmp" | grep -oE '[0-9]+ bytes' | tail -1 | awk '{print $1}')
        [ -z "$b" ] && b=0
        local now dt inst; now=$(date +%s); dt=$((now-prev_t)); [ "$dt" -le 0 ] && dt=1
        inst=$(( (b-prev_b)/dt ))
        if [ "$rate" -le 0 ]; then rate=$inst; else rate=$(( (rate*2+inst)/3 )); fi
        [ "$rate" -le 0 ] && rate=1
        prev_b=$b; prev_t=$now
        local dpct=$(( b*100/size )); [ "$dpct" -gt 100 ] && dpct=100
        local gdone=$((GLOBAL_DONE+b)) gpct=0
        [ "$GRAND_TOTAL" -gt 0 ] && gpct=$(( gdone*100/GRAND_TOTAL ))
        local deta geta
        if [ "$b" -gt 0 ] && [ "$rate" -gt 1 ]; then
            deta=$(hms $(( (size-b)/rate ))); geta=$(hms $(( (GRAND_TOTAL-gdone)/rate )))
        else deta="--:--:--"; geta="--:--:--"; fi
        printf '\r[disk %d/%d] %s %3d%% [%s] %s / %s  %s/s  ETA %s | all %d%% ETA %s      ' \
            "$idx" "$total" "$(basename "$disk")" "$dpct" "$(bar "$dpct" 16)" \
            "$(human "$b")" "$(human "$size")" "$(human "$rate")" "$deta" "$gpct" "$geta" >"$tty"
    done
    wait "$pid"; local rc=$?
    rm -f "$tmp"; printf '\r%*s\r' 100 '' >"$tty"
    GLOBAL_DONE=$((GLOBAL_DONE+size)); return $rc
}

wipe_disk() {                    # disk idx total
    local disk="$1" idx="$2" total="$3"
    [ -n "${SYSTEM_DISKS[$disk]:-}" ] && { warn "REFUSING to wipe protected $disk"; return; }
    local size; size=$(blockdev --getsize64 "$disk" 2>/dev/null || echo 0)
    log ">>> Wiping $disk ($(lsblk -dno SIZE,MODEL "$disk" 2>/dev/null))  [$idx/$total]"
    if [ "$DRY_RUN" -eq 1 ]; then echo "    DRY: would wipefs + zero $disk"; return; fi
    wipefs -a "$disk" >>"$LOG" 2>&1 || warn "wipefs issues on $disk"
    if [ "$USE_DISCARD" -eq 1 ] && command -v blkdiscard >/dev/null 2>&1; then
        log "    blkdiscard -z (fast hardware zero, no progress bar)"
        if blkdiscard -fz "$disk" 2>>"$LOG"; then GLOBAL_DONE=$((GLOBAL_DONE+size))
            blockdev --rereadpt "$disk" 2>/dev/null || partprobe "$disk" 2>/dev/null || true
            log "<<< Finished $disk"; return
        else warn "blkdiscard unsupported here -> dd"; fi
    fi
    log "    dd zero (live progress bar below)"
    zero_with_progress "$disk" "$size" "$idx" "$total" || warn "dd reported errors on $disk"
    blockdev --rereadpt "$disk" 2>/dev/null || partprobe "$disk" 2>/dev/null || true
    log "<<< Finished $disk"
}

i=0; n=${#DATA_DISKS[@]}
for d in "${DATA_DISKS[@]}"; do i=$((i+1)); wipe_disk "$d" "$i" "$n"; done

run "sync"
echo
if [ "$DRY_RUN" -eq 1 ]; then log "DRY-RUN complete. Nothing changed.";
else log "All operations complete. Log: $LOG"
    echo "Wiped: ${DATA_DISKS[*]:-none}"
    echo "Tip: clean stale storages with:  pvesm status   then   pvesm remove <id>"
fi
