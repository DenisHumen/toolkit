#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Every screen of the launcher, driven through a real terminal."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import REPO, Suite, Term                                  # noqa: E402


def main():
    s = Suite("launcher TUI")
    with Term(["bash", "toolkit.sh"], cwd=REPO) as t:
        s.check("the browser paints", t.expect("system check", 25))
        first_screen = t.screen()
        s.check("the header describes this machine",
                "kernel" in first_screen and "systemd" in first_screen)
        s.check("scripts are grouped by category",
                any(word in first_screen for word in ("CONTAINERS", "SECURITY",
                                                      "DIAGNOSTICS")))
        s.check("the footer lists the keys",
                "quit" in first_screen and "docs" in first_screen)

        first = t.selection()
        s.check("a row is highlighted", first is not None)

        t.send("down")
        second = t.selection()
        s.check("down moves the cursor", second and second != first,
                f"{first!r} -> {second!r}")
        t.send("up")
        back_up = t.selection()
        s.check("up moves it back", back_up == first, f"{back_up!r} != {first!r}")

        # Terminals in application cursor mode send ESC O B instead of ESC [ B.
        t.send("ss3_down")
        s.check("application-mode arrows work too", t.selection() == second)

        t.send("enter")
        detail = t.screen(1.2)
        s.check("Enter opens the summary",
                "Command" in detail and "System check" in detail)
        s.check("the summary shows the exact command",
                "bash " in detail and ".sh" in detail)
        s.check("the summary states a verdict",
                any(w in detail for w in ("Ready", "does not meet", "DESTRUCTIVE")))
        s.check("the summary offers the next step", "start" in detail)

        t.send("o")
        options = t.screen(1.2)
        s.check("Options opens from the summary",
                "Options —" in options and "(extra)" in options)
        s.check("Options lists a free-form argument row", "Back to the summary" in options)
        t.send("esc")
        t.read(0.6)

        t.send("esc")
        s.check("Esc returns to the browser", t.expect("system check", 8))

        t.send("s")
        info = t.screen(1.2)
        s.check("the system screen opens",
                "This machine" in info and "Package manager" in info)
        s.check("it reports privileges and environment",
                "Privileges" in info and "Environment" in info)
        t.send("esc")
        t.read(0.6)

        t.send("?")
        helptext = t.screen(1.2)
        s.check("help opens", "What this is" in helptext)
        s.check("help explains the status glyphs", "already installed" in helptext)
        t.send("esc")
        t.read(0.6)

        t.send("d")
        docs = t.screen(1.5)
        s.check("the docs pager opens", "scroll" in docs)
        t.send("pgdn")
        s.check("the pager scrolls", "scroll" in t.screen(0.8))
        t.send("esc")
        t.read(0.6)

        t.send("/")
        t.read(0.5)
        t.type("harden")
        filtered = t.screen(1.2)
        s.check("the filter narrows the list",
                "harden" in filtered.lower() and "Proxmox" not in filtered)
        s.check("the filter is shown in the header", "filter:" in filtered)
        t.send("esc")
        s.check("Esc clears the filter", t.expect("PROXMOX", 8))

        s.check("still running after every screen", t.alive())
        t.send("q")
        s.check("q quits", t.wait(15))
        s.check("and exits cleanly", t.exit_code == 0, f"exit={t.exit_code}")
    return s.finish()


if __name__ == "__main__":
    sys.exit(main())
