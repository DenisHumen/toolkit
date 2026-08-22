#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The one-click path, and the non-interactive entry points.

Runs the security audit, because it is the only script here that changes nothing
while still doing real work.
"""

import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import REPO, Suite, Term                                  # noqa: E402

LAUNCHER = os.path.join(REPO, "toolkit.sh")


def run(args, timeout=120):
    """Run the launcher and never hang the suite: a timeout is just a failure."""
    try:
        p = subprocess.run(["bash", os.path.join(REPO, "toolkit.sh")] + args,
                           capture_output=True, text=True, errors="replace",
                           timeout=timeout, cwd=REPO)
    except subprocess.TimeoutExpired as e:
        got = (e.stdout or "") + (e.stderr or "")
        if isinstance(got, bytes):
            got = got.decode("utf-8", "replace")
        return 124, got + f"\n[timed out after {timeout}s]"
    return p.returncode, p.stdout + p.stderr


def main():
    s = Suite("launcher: running things")

    # ---- non-interactive ------------------------------------------------- #
    rc, out = run(["--list"])
    s.check("--list exits cleanly", rc == 0, out[-400:])
    s.check("--list names the scripts",
            "harden.sh" in out and "netwatch" in out)
    s.check("--list marks each with a status",
            any(g in out for g in ("✔", "▲", "✖", "●")))

    rc, out = run(["--check"])
    s.check("--check exits cleanly", rc == 0)
    s.check("--check explains each requirement",
            "supported system" in out or "required commands" in out)

    rc, out = run(["--help"])
    s.check("--help exits cleanly", rc == 0)
    s.check("--help documents the entry points", "--run" in out and "--list" in out)

    rc, out = run(["--run", "harden"])
    s.check("--run executes the script", rc == 0, out[-400:])
    s.check("--run shows the command it used", "harden.sh --audit" in out)
    s.check("--run passes the script's own output through",
            "security audit" in out and "Score" in out)
    s.check("--run does not wait for a keypress",
            "Press Enter" not in out)

    rc, out = run(["--run", "no-such-script-anywhere"])
    s.check("an unknown name is rejected", rc != 0)
    s.check("and points at --list", "--list" in out)

    # ---- the interactive one-click path ---------------------------------- #
    with Term(["bash", LAUNCHER], cwd="/tmp") as t:
        s.check("the browser paints", t.expect("system check", 25))
        t.keys("down", "down")
        s.check("reached the hardening entry", "Server hardening" in t.screen())

        t.send("enter")
        summary = t.screen(1.2)
        s.check("the summary names the command", "harden.sh --audit" in summary)
        s.check("the summary shows the system check", "System check" in summary)
        s.check("the summary says it is ready", "Ready" in summary)

        t.send("enter")
        s.check("the script really runs", t.expect("security audit", 60))
        s.check("the launcher reports the outcome",
                t.expect("finished successfully", 60))
        s.check("and waits before returning", t.expect("Press Enter to return", 20))

        t.send("enter")
        s.check("control returns to the browser", t.expect("system check", 20))
        s.check("still alive afterwards", t.alive())

        t.send("q")
        s.check("quits cleanly", t.wait(15) and t.exit_code == 0)

    # ---- preview mode ---------------------------------------------------- #
    with Term(["bash", LAUNCHER], cwd="/tmp") as t:
        t.expect("system check", 25)
        t.keys("down", "down")
        t.send("enter")
        t.read(0.8)
        t.send("p")
        preview = t.screen(1.2)
        s.check("preview switches to the dry-run arguments",
                "--apply --dry-run" in preview, preview[-300:])
        s.check("preview says nothing will change",
                "changes nothing" in preview)
        t.send("esc")
        t.send("q")
        s.check("quits from preview too", t.wait(15))
    return s.finish()


if __name__ == "__main__":
    sys.exit(main())
