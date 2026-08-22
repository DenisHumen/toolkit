#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""netwatch's menu — the regression test for the arrow-key bug.

A cursor key arrives as three bytes. Version 1.0 read the first one through the
buffered `sys.stdin`, so `select()` reported "nothing more to read" while the
rest of the sequence sat in Python's buffer; the key was decoded as a bare
Escape, and Escape means "leave the menu". Pressing Down quit the program.
Nothing but a real terminal reproduces that, which is why this test exists.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import REPO, Suite, Term                                  # noqa: E402

NETWATCH = os.path.join(REPO, "linux", "netwatch", "netwatch.sh")
FULL = os.environ.get("TOOLKIT_TEST_FULL") == "1"


def main():
    s = Suite("netwatch TUI")
    with Term(["bash", NETWATCH], cwd="/tmp") as t:
        s.check("the menu paints", t.expect("Main menu", 25))
        first = t.selection()
        s.check("a row is highlighted", first is not None)

        t.send("down")
        second = t.selection()
        s.check("Down moves the cursor instead of quitting",
                t.alive() and second and second != first, f"{first!r} -> {second!r}")
        t.send("down")
        third = t.selection()
        s.check("Down again keeps moving", third and third != second)
        t.send("up")
        s.check("Up moves back", t.selection() == second)
        t.send("ss3_down")
        s.check("application-mode arrows work", t.selection() == third)

        # Two sequences in one write: what a held-down key actually delivers.
        t.send(b"\x1b[A\x1b[A")
        s.check("a repeated key is not swallowed", t.selection() == first,
                "both arrows in a single read must register")

        t.mark()
        t.keys("down", "down")            # -> Settings
        t.send("enter")
        s.check("Settings opens", t.expect("keep these settings", 10))
        settings = t.screen(0.8)
        s.check("Settings shows current values",
                "How long to monitor" in settings and "Public-IP" in settings)

        t.mark()
        t.send("enter")
        s.check("editing a field prompts", t.expect("How long to monitor", 8))
        t.mark()
        # 7 minutes: no other default formats to a string that contains it, so a
        # match cannot come from a frame painted before the edit.
        t.type("7m")
        s.check("the new value is applied", t.expect("7 m 0 s", 8))
        s.check("survives leaving raw mode for the prompt", t.alive())
        s.check("the settings list comes back", t.expect("keep these settings", 8))

        t.mark()
        t.send("esc")
        s.check("Esc returns to the main menu", t.expect("Main menu", 10))
        s.check("the edited value stuck", t.expect("7 m 0 s", 8))

        t.send("q")
        s.check("q quits", t.wait(15))
        s.check("and exits cleanly", t.exit_code == 0, f"exit={t.exit_code}")

    if not FULL:
        s.skip("live dashboard capture", "set TOOLKIT_TEST_FULL=1 (needs network)")
        return s.finish()

    with Term(["bash", NETWATCH, "--duration", "600", "--yes", "--no-speed",
               "--no-trace", "--out", "/tmp/netwatch-test-run"], cwd="/tmp") as t:
        s.check("the dashboard paints", t.expect("targets", 40))
        t.send("down")
        s.check("stray arrows do not stop a capture", t.alive())
        t.send("q")
        s.check("q ends the capture early", t.expect("Analysing the capture", 60))
        s.check("a report is still written", t.expect("Report :", 90))
        s.check("exits cleanly", t.wait(30))
    return s.finish()


if __name__ == "__main__":
    sys.exit(main())
