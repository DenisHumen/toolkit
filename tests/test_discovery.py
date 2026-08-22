#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Discovery, metadata parsing and the system check — the launcher's brain.

Uses a throwaway directory of synthetic scripts rather than the real repository,
so these assertions keep meaning as the repository grows.
"""

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import REPO, Suite                                        # noqa: E402

sys.path.insert(0, REPO)
import toolkit as tk                                               # noqa: E402

FULL = """#!/usr/bin/env bash
#
# alpha.sh — installs the alpha widget.
#
# toolkit-name: Alpha Widget
# toolkit-kind: installer
# toolkit-category: Containers
# toolkit-summary: Installs the alpha widget from its official repository.
# toolkit-os: debian, fedora
# toolkit-root: yes
# toolkit-needs: sh
# toolkit-optional: definitely-not-a-real-binary
# toolkit-detect: true
# toolkit-preview: --dry-run
# toolkit-run: --yes
# toolkit-ports: 80
# toolkit-writes: /opt/alpha
# toolkit-order: 5
# toolkit-arg: --domain | Domain to serve on | required
# toolkit-arg: --email | Contact address | text
# toolkit-arg: --debug | Verbose output | flag
#
echo alpha
"""

PLAIN = """#!/usr/bin/env bash
#
# plain-backup.sh — snapshot /etc into a timestamped tarball.
#
# Creates a compressed copy of /etc so a bad edit can be undone. Nothing here
# tells the launcher anything, on purpose.
#
echo plain
"""

HIDDEN = """#!/usr/bin/env bash
# helper.sh — internal helper.
# toolkit-hidden: yes
echo helper
"""

WRAPPER_SH = """#!/usr/bin/env bash
# gamma.sh — launcher for the gamma engine.
# toolkit-name: Gamma
# toolkit-kind: tool
# toolkit-category: Diagnostics
echo gamma
"""
WRAPPER_PY = """#!/usr/bin/env python3
\"\"\"gamma.py — the gamma engine itself.\"\"\"
print("gamma")
"""

DESTRUCTIVE = """#!/usr/bin/env bash
# nuke.sh — erases things.
# toolkit-kind: destructive
# toolkit-danger: Erases every disk.
# toolkit-confirm: DO-IT
# toolkit-needs: definitely-not-a-real-binary
echo nuke
"""


class FakeSystem:
    """A machine with known properties, so verdicts are predictable."""

    def __init__(self, family="debian", is_root=True, can_sudo=True, internet=True):
        self.family = family
        self.distro = f"{family} test"
        self.is_root = is_root
        self.can_sudo = can_sudo
        self.internet = internet

    @property
    def root_ok(self):
        return self.is_root or self.can_sudo


def build_tree(root):
    def write(rel, body):
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(body)
        return path

    write("alpha.sh", FULL)
    write("plain-backup.sh", PLAIN)
    write("helper.sh", HIDDEN)
    write("gamma/gamma.sh", WRAPPER_SH)
    write("gamma/gamma.py", WRAPPER_PY)
    write("danger/nuke.sh", DESTRUCTIVE)
    write("notes.txt", "not a script")


def main():
    s = Suite("discovery + metadata + system check")
    tmp = tempfile.mkdtemp(prefix="toolkit-test-")
    try:
        build_tree(tmp)
        found = tk.discover(tmp)
        by_name = {x.name: x for x in found}

        s.check("finds every visible script", len(found) == 4,
                f"got {len(found)}: {sorted(x.name for x in found)}")
        s.check("hidden scripts stay hidden",
                not any("helper" in x.rel for x in found))
        s.check("non-scripts are ignored", not any(x.rel.endswith(".txt") for x in found))
        s.check("launcher + engine count as one entry",
                sum(1 for x in found if "gamma" in x.rel) == 1)
        s.check("the launcher is the entry point, not the engine",
                any(x.rel.endswith("gamma.sh") for x in found))

        alpha = by_name.get("Alpha Widget")
        s.check("reads toolkit-name", alpha is not None)
        if alpha:
            s.check("reads the summary",
                    alpha.summary.startswith("Installs the alpha widget"))
            s.check("reads kind and category",
                    (alpha.kind, alpha.category) == ("installer", "Containers"))
            s.check("reads supported systems", alpha.list_of("os") == ["debian", "fedora"])
            s.check("reads root requirement", alpha.needs_root == "yes")
            s.check("reads default and preview arguments",
                    alpha.base_args() == ["--yes"]
                    and alpha.base_args(preview=True) == ["--dry-run"])
            s.check("parses three declared arguments", len(alpha.args) == 3)
            flags = {a.flag: a for a in alpha.args}
            s.check("marks a required argument required",
                    flags.get("--domain") is not None and flags["--domain"].required)
            s.check("a flag argument contributes nothing while off",
                    flags.get("--debug") is not None and flags["--debug"].as_argv() == [])
            if "--debug" in flags:
                flags["--debug"].value = "yes"
                s.check("a flag argument appears once switched on",
                        flags["--debug"].as_argv() == ["--debug"])
                flags["--debug"].value = ""
            if "--domain" in flags:
                flags["--domain"].value = "example.com"
                argv = alpha.argv(FakeSystem(is_root=True), preview=False)
                s.check("builds the command with the filled-in argument",
                        argv[:1] == ["bash"] and "--yes" in argv
                        and argv[-2:] == ["--domain", "example.com"], " ".join(argv))
                argv_sudo = alpha.argv(FakeSystem(is_root=False, can_sudo=True))
                s.check("prefixes sudo when root is needed and available",
                        argv_sudo[0] == "sudo")
                flags["--domain"].value = ""

        plain = next((x for x in found if x.rel.endswith("plain-backup.sh")), None)
        s.check("a script with no metadata still appears", plain is not None)
        if plain:
            s.check("falls back to the filename",
                    plain.name == "plain-backup.sh")
            s.check("falls back to the header comment for the summary",
                    "snapshot /etc" in plain.summary, plain.summary)
            s.check("guesses the kind", plain.kind == "tool")
            s.check("knows it was guessing", plain.has_meta is False)

        nuke = next((x for x in found if x.rel.endswith("nuke.sh")), None)
        s.check("destructive scripts are recognised",
                nuke is not None and nuke.kind == "destructive")
        if nuke:
            s.check("keeps the confirmation word", nuke.get("confirm") == "DO-IT")

        # ---- the system check ------------------------------------------- #
        if alpha:
            tk.evaluate(alpha, FakeSystem(family="debian"), deep=True)
            s.check("a supported machine is ready",
                    alpha.verdict in ("ready", "attention"), alpha.verdict)
            s.check("notices what is already installed", bool(alpha.present))
            texts = " | ".join(t for _st, t in alpha.checks)
            s.check("mentions the missing optional command",
                    "definitely-not-a-real-binary" in texts, texts)

            tk.evaluate(alpha, FakeSystem(family="arch"), deep=False)
            s.check("the wrong distro blocks it", alpha.verdict == "blocked")
            s.check("and says which systems it wants",
                    any("debian" in t for st, t in alpha.checks if st == "bad"))

            tk.evaluate(alpha, FakeSystem(family="debian", is_root=False,
                                          can_sudo=False), deep=False)
            s.check("no root and no sudo blocks it", alpha.verdict == "blocked")

            tk.evaluate(alpha, FakeSystem(family="debian", internet=False), deep=False)
            s.check("an installer without internet is blocked",
                    alpha.verdict == "blocked")

        if nuke:
            tk.evaluate(nuke, FakeSystem(), deep=False)
            s.check("a missing required command blocks it", nuke.verdict == "blocked")
            s.check("and names the command",
                    any("definitely-not-a-real-binary" in t
                        for st, t in nuke.checks if st == "bad"))

        # ---- the real repository still parses ---------------------------- #
        real = tk.discover(REPO)
        s.check("the repository itself yields scripts", len(real) >= 5,
                f"{len(real)} found")
        s.check("every repository script has metadata",
                all(x.has_meta for x in real),
                ", ".join(x.rel for x in real if not x.has_meta))
        s.check("every repository script has a summary",
                all(x.summary.strip() for x in real))
        s.check("the launcher does not list itself",
                not any(os.path.basename(x.path).startswith("toolkit.") for x in real))
        s.check("tests are not offered as tools",
                not any("/tests/" in x.rel or x.rel.startswith("tests/") for x in real),
                ", ".join(x.rel for x in real if "tests" in x.rel))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return s.finish()


if __name__ == "__main__":
    sys.exit(main())
