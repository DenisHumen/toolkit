#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backup.sh — a backup nobody has restored is a rumour, so this restores one.

The round trip runs in a throwaway container: real files in, archive out, source
destroyed, archive back, checksums compared. It also truncates an archive to
confirm the verification actually notices.
"""

import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import REPO, Suite, have                                  # noqa: E402

BACKUP = os.path.join(REPO, "linux", "backup.sh")
FULL = os.environ.get("TOOLKIT_TEST_FULL") == "1"

ROUNDTRIP = r"""
set -u
apt-get -qq update >/dev/null 2>&1
DEBIAN_FRONTEND=noninteractive apt-get -qq install -y zstd >/dev/null 2>&1
cp /tk/backup.sh /b.sh && chmod +x /b.sh

mkdir -p /srv/data/nested /srv/data/empty
head -c 300000 /dev/urandom > /srv/data/blob.bin
printf 'important\n' > /srv/data/nested/note.txt
ln -s /srv/data/nested/note.txt /srv/data/link
BEFORE="$(cd /srv && find data -type f | sort | xargs sha256sum | sha256sum | cut -d' ' -f1)"
echo "SOURCE_SUM $BEFORE"

/b.sh --path /srv/data --dest /backups --name t --yes >/dev/null 2>&1
echo "CREATE_RC $?"
ARCHIVE="$(find /backups -name '*.tar.*' ! -name '*.meta' ! -name '*.sha256' | head -1)"
echo "ARCHIVE $(basename "$ARCHIVE")"
test -s "$ARCHIVE" && echo "ARCHIVE_NONEMPTY yes"
test -f "$ARCHIVE.meta" && echo "META yes"
test -f "$ARCHIVE.sha256" && echo "SUMFILE yes"
grep -q '^consistent=' "$ARCHIVE.meta" && echo "META_CONSISTENCY yes"
grep -q '^sources:' "$ARCHIVE.meta" && echo "META_SOURCES yes"

/b.sh --dest /backups --verify "$ARCHIVE" >/dev/null 2>&1 && echo "VERIFY_OK yes"

rm -rf /srv/data
/b.sh --dest /backups --restore "$ARCHIVE" --in-place --yes >/dev/null 2>&1
echo "RESTORE_RC $?"
AFTER="$(cd /srv && find data -type f | sort | xargs sha256sum | sha256sum | cut -d' ' -f1)"
echo "RESTORED_SUM $AFTER"
test -L /srv/data/link && echo "SYMLINK_KEPT yes"
test -d /srv/data/empty && echo "EMPTY_DIR_KEPT yes"

# A restore without --in-place must not touch the live tree.
printf 'live\n' > /srv/data/nested/note.txt
/b.sh --dest /backups --restore "$ARCHIVE" --to /tmp/staged --yes >/dev/null 2>&1
echo "STAGED_RC $?"
grep -q '^live$' /srv/data/nested/note.txt && echo "LIVE_UNTOUCHED yes"
test -f /tmp/staged/srv/data/nested/note.txt && echo "STAGED_FILES yes"

# Retention keeps the newest N and nothing else.
for i in 1 2 3 4; do
    /b.sh --path /srv/data --dest /backups --name t --keep 2 --yes >/dev/null 2>&1
done
echo "KEPT $(find /backups -name '*.tar.*' ! -name '*.meta' ! -name '*.sha256' | wc -l)"

# Verification has to notice a damaged archive, or it is decoration.
LAST="$(find /backups -name '*.tar.*' ! -name '*.meta' ! -name '*.sha256' | sort | tail -1)"
truncate -s 4096 "$LAST"
if /b.sh --dest /backups --verify "$LAST" >/dev/null 2>&1; then
    echo "CORRUPT_DETECTED no"
else
    echo "CORRUPT_DETECTED yes"
fi
"""


def sh(args, timeout=180, cwd=None):
    try:
        p = subprocess.run(args, capture_output=True, text=True, errors="replace",
                           timeout=timeout, cwd=cwd)
    except subprocess.TimeoutExpired as e:
        got = (e.stdout or "") + (e.stderr or "")
        if isinstance(got, bytes):
            got = got.decode("utf-8", "replace")
        return 124, got + f"\n[timed out after {timeout}s]"
    return p.returncode, p.stdout + p.stderr


def main():
    s = Suite("backup.sh")

    rc, out = sh(["bash", "-n", BACKUP])
    s.check("the script parses", rc == 0, out)

    rc, out = sh(["bash", BACKUP, "--help"])
    s.check("--help works", rc == 0)
    s.check("--help hides the launcher metadata", "toolkit-name" not in out)
    s.check("--help explains the consistency trade-off", "--stop" in out)

    with tempfile.TemporaryDirectory() as tmp:
        empty = os.path.join(tmp, "nothing-here")
        rc, out = sh(["bash", BACKUP, "--dest", empty, "--list"])
        s.check("--list on a fresh machine is not an error", rc == 0, out)
        s.check("--list says how to make the first one", "--auto" in out)

        src = os.path.join(tmp, "src")
        os.makedirs(os.path.join(src, "sub"))
        with open(os.path.join(src, "sub", "a.txt"), "w") as fh:
            fh.write("hello\n")
        dest = os.path.join(tmp, "dest")
        rc, out = sh(["bash", BACKUP, "--path", src, "--dest", dest,
                      "--dry-run", "--yes"])
        s.check("--dry-run exits cleanly", rc == 0, out[-300:])
        s.check("--dry-run reports the source size", "Source total" in out)
        s.check("--dry-run writes nothing", not os.path.exists(dest))
        s.check("--dry-run warns about a hot copy", "--stop" in out)

        rc, out = sh(["bash", BACKUP, "--path", os.path.join(tmp, "does-not-exist"),
                      "--dest", dest, "--yes"])
        s.check("a missing source is refused, not silently skipped", rc != 0)

    # Run bare, it explains itself; given flags but no source, it refuses —
    # a scheduled run that silently backs up nothing is the worst outcome.
    rc, out = sh(["bash", BACKUP])
    s.check("a bare run explains itself instead of failing", rc == 0, out[-200:])
    s.check("a bare run suggests where to start", "--auto" in out)

    rc, out = sh(["bash", BACKUP, "--keep", "3"])
    s.check("flags without a source are refused",
            rc != 0 and "Nothing to back up" in out)

    rc, out = sh(["bash", BACKUP, "--nonsense"])
    s.check("an unknown flag is rejected", rc != 0 and "Unknown option" in out)

    rc, out = sh(["bash", BACKUP, "--dest", "/tmp", "--verify", "/tmp/not-an-archive"])
    s.check("verifying a missing archive fails clearly",
            rc != 0 and "No such archive" in out)

    if not FULL:
        s.skip("backup/restore round trip", "set TOOLKIT_TEST_FULL=1")
        return s.finish()
    if not have("docker"):
        s.skip("backup/restore round trip", "docker not available")
        return s.finish()

    mount = f"{os.path.join(REPO, 'linux')}:/tk:ro"
    rc, out = sh(["docker", "run", "--rm", "-v", mount, "debian:12",
                  "bash", "-c", ROUNDTRIP], timeout=900)
    got = {}
    for line in out.splitlines():
        parts = line.split(" ", 1)
        if len(parts) == 2 and parts[0].isupper():
            got[parts[0]] = parts[1].strip()

    s.check("the container run finished", rc == 0, out[-600:])
    s.check("creating a backup succeeds", got.get("CREATE_RC") == "0", str(got))
    s.check("the archive is not empty", got.get("ARCHIVE_NONEMPTY") == "yes")
    s.check("a manifest is written next to it", got.get("META") == "yes")
    s.check("a checksum is written next to it", got.get("SUMFILE") == "yes")
    s.check("the manifest records whether the copy was consistent",
            got.get("META_CONSISTENCY") == "yes")
    s.check("the manifest records what went in", got.get("META_SOURCES") == "yes")
    s.check("the archive verifies", got.get("VERIFY_OK") == "yes")
    s.check("restoring succeeds", got.get("RESTORE_RC") == "0")
    s.check("the restored tree is byte-identical",
            got.get("SOURCE_SUM") and got.get("SOURCE_SUM") == got.get("RESTORED_SUM"),
            f"{got.get('SOURCE_SUM')} != {got.get('RESTORED_SUM')}")
    s.check("symlinks survive the round trip", got.get("SYMLINK_KEPT") == "yes")
    s.check("empty directories survive too", got.get("EMPTY_DIR_KEPT") == "yes")
    s.check("a staged restore leaves the live tree alone",
            got.get("LIVE_UNTOUCHED") == "yes")
    s.check("a staged restore does produce the files",
            got.get("STAGED_FILES") == "yes")
    s.check("retention keeps exactly what was asked for", got.get("KEPT") == "2",
            f"kept {got.get('KEPT')}")
    s.check("verification notices a damaged archive",
            got.get("CORRUPT_DETECTED") == "yes",
            "a truncated archive passed verification")
    return s.finish()


if __name__ == "__main__":
    sys.exit(main())
