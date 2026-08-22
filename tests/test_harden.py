#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""harden.sh — the audit is read-only, the dry run is inert, and apply reverses.

The apply/rollback half runs inside a throwaway container, because that is the
only honest way to test a script whose job is to edit sshd and the firewall.
"""

import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import REPO, Suite, have                                  # noqa: E402

HARDEN = os.path.join(REPO, "linux", "harden.sh")
FULL = os.environ.get("TOOLKIT_TEST_FULL") == "1"

# Everything --apply is allowed to create or edit.
TOUCHED = ["/etc/ssh/sshd_config", "/etc/ssh/sshd_config.d/99-harden.conf",
           "/etc/sysctl.d/99-harden.conf", "/etc/apt/apt.conf.d/20auto-upgrades",
           "/etc/fail2ban/jail.d/99-toolkit-sshd.local"]

CONTAINER_SCRIPT = r"""
set -e
apt-get -qq update >/dev/null 2>&1
DEBIAN_FRONTEND=noninteractive apt-get -qq install -y openssh-server procps iproute2 \
    >/dev/null 2>&1
ssh-keygen -A >/dev/null 2>&1
mkdir -p /run/sshd /root/.ssh
echo 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAItest test@example' > /root/.ssh/authorized_keys
cp /tk/harden.sh /h.sh && chmod +x /h.sh

echo "BASELINE $(/usr/sbin/sshd -T 2>/dev/null | grep -ci '^passwordauthentication yes')"
/h.sh --apply --yes >/dev/null 2>&1 || true
echo "APPLY_PASSWORD $(/usr/sbin/sshd -T 2>/dev/null | grep -i '^passwordauthentication' | awk '{print $2}')"
echo "APPLY_ROOTLOGIN $(/usr/sbin/sshd -T 2>/dev/null | grep -i '^permitrootlogin' | awk '{print $2}')"
echo "APPLY_MAXAUTH $(/usr/sbin/sshd -T 2>/dev/null | grep -i '^maxauthtries' | awk '{print $2}')"
if /usr/sbin/sshd -t 2>/dev/null; then echo "APPLY_CONFIG valid"; else echo "APPLY_CONFIG broken"; fi
test -f /etc/sysctl.d/99-harden.conf && echo "APPLY_SYSCTL present"
test -f /etc/fail2ban/jail.d/99-toolkit-sshd.local && echo "APPLY_FAIL2BAN present"
ls -d /var/backups/toolkit-harden/*/ >/dev/null 2>&1 && echo "APPLY_BACKUP present"

/h.sh --rollback >/dev/null 2>&1 || true
echo "ROLLBACK_PASSWORD $(/usr/sbin/sshd -T 2>/dev/null | grep -i '^passwordauthentication' | awk '{print $2}')"
if /usr/sbin/sshd -t 2>/dev/null; then echo "ROLLBACK_CONFIG valid"; else echo "ROLLBACK_CONFIG broken"; fi
test -f /etc/ssh/sshd_config.d/99-harden.conf && echo "ROLLBACK_DROPIN present" || echo "ROLLBACK_DROPIN gone"
test -f /etc/sysctl.d/99-harden.conf && echo "ROLLBACK_SYSCTL present" || echo "ROLLBACK_SYSCTL gone"
grep -q 'toolkit harden' /etc/ssh/sshd_config && echo "ROLLBACK_MARKER left" || echo "ROLLBACK_MARKER clean"
"""

NO_KEY_SCRIPT = r"""
set -e
apt-get -qq update >/dev/null 2>&1
DEBIAN_FRONTEND=noninteractive apt-get -qq install -y openssh-server >/dev/null 2>&1
ssh-keygen -A >/dev/null 2>&1
mkdir -p /run/sshd
rm -f /root/.ssh/authorized_keys
cp /tk/harden.sh /h.sh && chmod +x /h.sh
/h.sh --apply --yes >/dev/null 2>&1 || true
echo "NOKEY_PASSWORD $(/usr/sbin/sshd -T 2>/dev/null | grep -i '^passwordauthentication' | awk '{print $2}')"
"""


def sh(args, timeout=180, cwd=None):
    """Run something and never hang the suite: a timeout is just a failure."""
    try:
        p = subprocess.run(args, capture_output=True, text=True, errors="replace",
                           timeout=timeout, cwd=cwd)
    except subprocess.TimeoutExpired as e:
        got = (e.stdout or "") + (e.stderr or "")
        if isinstance(got, bytes):
            got = got.decode("utf-8", "replace")
        return 124, got + f"\n[timed out after {timeout}s]"
    return p.returncode, p.stdout + p.stderr


def snapshot():
    state = {}
    for path in TOUCHED:
        try:
            st = os.stat(path)
            state[path] = (st.st_mtime_ns, st.st_size)
        except OSError:
            state[path] = None
    return state


def main():
    s = Suite("harden.sh")

    rc, out = sh(["bash", "-n", HARDEN])
    s.check("the script parses", rc == 0, out)

    rc, out = sh(["bash", HARDEN, "--help"])
    s.check("--help works", rc == 0)
    s.check("--help hides the launcher metadata", "toolkit-name" not in out)
    s.check("--help documents the safety model", "lock" in out.lower())

    rc, out = sh(["bash", HARDEN, "--audit"], timeout=180)
    s.check("--audit exits cleanly even with findings", rc == 0, out[-300:])
    s.check("--audit scores the machine", re.search(r"Score \d+/100", out) is not None)
    s.check("--audit covers every area",
            all(area in out for area in ("SSH", "Firewall", "Updates",
                                         "Intrusion", "Kernel", "Accounts")))
    s.check("--audit suggests the flag that fixes each finding",
            "--apply" in out)
    s.check("--audit never blocks on a password prompt", "password for" not in out.lower())

    with tempfile.NamedTemporaryFile(suffix=".md", delete=False) as fh:
        report = fh.name
    try:
        rc, out = sh(["bash", HARDEN, "--audit", "--report", report], timeout=180)
        s.check("--report writes a file", rc == 0 and os.path.getsize(report) > 200)
        body = open(report, encoding="utf-8").read()
        s.check("the report is a Markdown table", "| Area | Check |" in body)
        s.check("the report carries the score", "**Score" in body)
    finally:
        os.unlink(report)

    rc, out = sh(["bash", HARDEN, "--audit", "--no-ssh", "--no-firewall"], timeout=180)
    s.check("sections can be skipped",
            rc == 0 and "\nSSH\n" not in out and "\nFirewall\n" not in out)

    rc, out = sh(["bash", HARDEN, "--nonsense-flag"])
    s.check("an unknown flag is rejected", rc != 0 and "Unknown option" in out)

    before = snapshot()
    rc, out = sh(["bash", HARDEN, "--apply", "--dry-run", "--yes"], timeout=240)
    s.check("--apply --dry-run exits cleanly", rc == 0, out[-300:])
    s.check("--apply --dry-run says it changed nothing", "Dry run finished" in out)
    s.check("--apply --dry-run shows the commands it would run", "would run:" in out)
    s.check("--apply --dry-run shows the files it would write", "would write" in out)
    s.check("--apply --dry-run really touches nothing", snapshot() == before,
            "a file under /etc changed during a dry run")

    if not FULL:
        s.skip("apply + rollback in a container", "set TOOLKIT_TEST_FULL=1")
        return s.finish()
    if not have("docker"):
        s.skip("apply + rollback in a container", "docker not available")
        return s.finish()

    mount = f"{os.path.join(REPO, 'linux')}:/tk:ro"
    rc, out = sh(["docker", "run", "--rm", "-v", mount, "debian:12",
                  "bash", "-c", CONTAINER_SCRIPT], timeout=600)
    got = dict(line.split(" ", 1) for line in out.splitlines() if " " in line
               and line.split(" ", 1)[0].isupper())
    s.check("the container run finished", rc == 0, out[-500:])
    s.check("apply disables password logins when a key exists",
            got.get("APPLY_PASSWORD") == "no", str(got))
    s.check("apply restricts root login",
            got.get("APPLY_ROOTLOGIN") in ("prohibit-password", "without-password"))
    s.check("apply tightens the auth attempt limit", got.get("APPLY_MAXAUTH") == "3")
    s.check("apply leaves sshd with a valid config",
            got.get("APPLY_CONFIG") == "valid")
    s.check("apply writes the sysctl file", got.get("APPLY_SYSCTL") == "present")
    s.check("apply configures a fail2ban jail", got.get("APPLY_FAIL2BAN") == "present")
    s.check("apply keeps a backup", got.get("APPLY_BACKUP") == "present")
    s.check("rollback restores password logins",
            got.get("ROLLBACK_PASSWORD") == "yes")
    s.check("rollback leaves a valid config", got.get("ROLLBACK_CONFIG") == "valid")
    s.check("rollback removes the drop-in", got.get("ROLLBACK_DROPIN") == "gone")
    s.check("rollback removes the sysctl file", got.get("ROLLBACK_SYSCTL") == "gone")
    s.check("rollback leaves no marker behind", got.get("ROLLBACK_MARKER") == "clean")

    rc, out = sh(["docker", "run", "--rm", "-v", mount, "debian:12",
                  "bash", "-c", NO_KEY_SCRIPT], timeout=600)
    s.check("without an SSH key, password login is left ENABLED",
            "NOKEY_PASSWORD yes" in out,
            "anti-lockout guarantee broken: " + out[-300:])
    return s.finish()


if __name__ == "__main__":
    sys.exit(main())
