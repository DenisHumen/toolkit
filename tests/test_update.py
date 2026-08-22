#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The update notice: detecting it, refusing it when unsafe, and taking it.

The detection half talks to GitHub, so it is only exercised in --full mode and
tolerates being rate-limited. The parts that matter for safety — is the tree
clean, is the pull a fast-forward, does "skip" stick — run against a local git
fixture and need no network at all.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import REPO, Suite, Term, have                            # noqa: E402

sys.path.insert(0, REPO)
import toolkit as tk                                               # noqa: E402

FULL = os.environ.get("TOOLKIT_TEST_FULL") == "1"


def git(*args, cwd=None):
    return subprocess.run(["git"] + list(args), cwd=cwd, capture_output=True,
                          text=True, errors="replace")


def build_fixture(root):
    """A bare 'origin' with two commits, and a clone sitting one behind it."""
    origin = os.path.join(root, "origin.git")
    work = os.path.join(root, "work")
    clone = os.path.join(root, "clone")
    git("init", "--bare", "-q", "--initial-branch=main", origin)
    git("clone", "-q", origin, work)
    env = ["-c", "user.email=t@example", "-c", "user.name=test"]
    with open(os.path.join(work, "a.txt"), "w") as fh:
        fh.write("one\n")
    git("add", "-A", cwd=work)
    git(*env, "commit", "-qm", "first", cwd=work)
    git("push", "-q", "origin", "main", cwd=work)
    git("clone", "-q", origin, clone)
    with open(os.path.join(work, "b.txt"), "w") as fh:
        fh.write("two\n")
    git("add", "-A", cwd=work)
    git(*env, "commit", "-qm", "second", cwd=work)
    git("push", "-q", "origin", "main", cwd=work)
    return clone


def main():
    s = Suite("update notice")

    if not have("git"):
        s.skip("everything", "git is not installed")
        return s.finish()

    # ---- remote URL parsing --------------------------------------------- #
    with tempfile.TemporaryDirectory() as tmp:
        for url, expected in (
                ("git@github.com:DenisHumen/toolkit.git", "DenisHumen/toolkit"),
                ("https://github.com/DenisHumen/toolkit.git", "DenisHumen/toolkit"),
                ("https://github.com/DenisHumen/toolkit", "DenisHumen/toolkit"),
                ("ssh://git@github.com/owner/repo.git", "owner/repo")):
            repo = os.path.join(tmp, "r")
            shutil.rmtree(repo, ignore_errors=True)
            git("init", "-q", "--initial-branch=main", repo)
            git("remote", "add", "origin", url, cwd=repo)
            with open(os.path.join(repo, "f"), "w") as fh:
                fh.write("x")
            git("add", "-A", cwd=repo)
            git("-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "c",
                cwd=repo)
            saved = tk.REPO
            tk.REPO = repo
            try:
                got = tk.Updater(enabled=False).slug
            finally:
                tk.REPO = saved
            s.check(f"reads the repository from {url.split('github.com')[0] or 'ssh'}"
                    f"…{expected}", got == expected, f"got {got}")

    # ---- the safety rules, against a real (local) git repository --------- #
    with tempfile.TemporaryDirectory() as tmp:
        clone = build_fixture(tmp)
        cache = os.path.join(tmp, "cache")
        os.environ["TOOLKIT_CACHE_DIR"] = cache
        saved = tk.REPO
        tk.REPO = clone
        try:
            upd = tk.Updater(enabled=False)
            s.check("recognises a git checkout", upd.is_git)
            s.check("reads the current branch", upd.branch == "main", upd.branch)
            s.check("reads the local commit", len(upd.local_sha) == 40)
            possible, why = upd.can_update()
            s.check("a clean checkout may be updated", possible, why)

            with open(os.path.join(clone, "a.txt"), "a") as fh:
                fh.write("local edit\n")
            dirty = tk.Updater(enabled=False)
            s.check("a local edit is noticed", dirty.dirty)
            possible, why = dirty.can_update()
            s.check("and blocks the update", not possible)
            s.check("with a reason that says what to do",
                    "commit or stash" in why, why)
            git("checkout", "--", "a.txt", cwd=clone)

            before = tk.Updater(enabled=False).local_sha
            rc = tk.Updater(enabled=False).pull()
            after = git("rev-parse", "HEAD", cwd=clone).stdout.strip()
            s.check("the update fast-forwards the checkout", rc == 0)
            s.check("and the checkout actually moved", after != before)
            s.check("bringing the new file with it",
                    os.path.isfile(os.path.join(clone, "b.txt")))

            # A divergent local commit must not be steamrolled.
            with open(os.path.join(clone, "c.txt"), "w") as fh:
                fh.write("mine\n")
            git("add", "-A", cwd=clone)
            git("-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm",
                "local only", cwd=clone)
            git("-C", clone, "reset", "--hard", "-q", "HEAD~2")
            git("-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm",
                "divergent", "--allow-empty", cwd=clone)
            rc = tk.Updater(enabled=False).pull()
            s.check("a divergent history is refused, not rewritten", rc != 0)
        finally:
            tk.REPO = saved
            os.environ.pop("TOOLKIT_CACHE_DIR", None)

    # ---- the cache decides what the user is told ------------------------- #
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["TOOLKIT_CACHE_DIR"] = tmp
        try:
            head = git("rev-parse", "HEAD", cwd=REPO).stdout.strip()
            fake = {"checked_at": time.time(), "local_sha": head,
                    "remote_sha": "b" * 40, "ahead": 2, "remote_when": "",
                    "commits": [{"sha": "aaaaaaa", "message": "feat: something new",
                                 "when": "2 hours ago", "author": "someone"},
                                {"sha": "bbbbbbb", "message": "fix: something old",
                                 "when": "3 hours ago", "author": "someone"}],
                    "files": ["linux/thing.sh"], "skipped": ""}
            with open(os.path.join(tmp, "update.json"), "w") as fh:
                json.dump(fake, fh)
            upd = tk.Updater(enabled=False)
            s.check("a fresh cache answers without any network",
                    upd.status == "behind", upd.status)
            s.check("and knows how far behind", upd.ahead == 2)
            s.check("the header line advertises it", "Update available" in
                    upd.summary_line())

            upd.skip()
            again = tk.Updater(enabled=False)
            s.check("skipping a version silences the notice",
                    again.available is False)
            s.check("but the update is still there if asked for",
                    again.status == "behind")

            fake["skipped"] = ""
            fake["remote_sha"] = "c" * 40
            with open(os.path.join(tmp, "update.json"), "w") as fh:
                json.dump(fake, fh)
            s.check("a newer version speaks up again",
                    tk.Updater(enabled=False).available)

            stale = dict(fake, local_sha="0" * 40)
            with open(os.path.join(tmp, "update.json"), "w") as fh:
                json.dump(stale, fh)
            s.check("a cache from a different checkout is ignored",
                    tk.Updater(enabled=False).status != "behind")

            # Unpushed local commits are not an update, and must not nag.
            diverged = dict(fake, diverged=True, ahead=0, commits=[], skipped="")
            with open(os.path.join(tmp, "update.json"), "w") as fh:
                json.dump(diverged, fh)
            upd = tk.Updater(enabled=False)
            s.check("a checkout with its own commits is called diverged, not behind",
                    upd.status == "diverged", upd.status)
            s.check("and is never advertised as an update",
                    upd.summary_line() == "", repr(upd.summary_line()))
            possible, why = upd.can_update()
            s.check("and is not offered a fast-forward it cannot do",
                    not possible and "GitHub does not know" in why, why)
        finally:
            os.environ.pop("TOOLKIT_CACHE_DIR", None)

    # ---- the notice in the launcher itself -------------------------------- #
    cache = tempfile.mkdtemp(prefix="toolkit-update-cache-")
    try:
        head = git("rev-parse", "HEAD", cwd=REPO).stdout.strip()
        with open(os.path.join(cache, "update.json"), "w") as fh:
            json.dump({"checked_at": time.time(), "local_sha": head,
                       "remote_sha": "d" * 40, "ahead": 2, "remote_when": "",
                       "commits": [{"sha": "1234567",
                                    "message": "feat: a brand new script",
                                    "when": "2 hours ago", "author": "x"},
                                   {"sha": "89abcde", "message": "docs: explain it",
                                    "when": "3 hours ago", "author": "x"}],
                       "files": ["linux/new-thing.sh", "README.md"],
                       "skipped": ""}, fh)
        env = {"TOOLKIT_CACHE_DIR": cache}
        with Term(["bash", os.path.join(REPO, "toolkit.sh")], cwd="/tmp",
                  env=env) as t:
            s.check("the launcher starts", t.expect("system check", 25))
            screen = t.screen()
            s.check("the header carries the update notice",
                    "Update available" in screen, screen[-400:])
            s.check("it says how many commits", "2 new commits" in screen)
            s.check("and which key opens it", "press u" in screen)

            t.mark()
            t.send("u")
            details = t.screen(1.5)
            s.check("u opens the update screen", "What's new" in details, details[-400:])
            s.check("it lists the commits", "a brand new script" in details)
            s.check("it shows which files change", "new-thing.sh" in details)
            s.check("it explains the update is a fast-forward",
                    "--ff-only" in details)
            # What it offers depends on the checkout it is looking at: a clean
            # one can be updated, a modified one must be told why it cannot.
            blocked = "cannot update here" in details
            s.check("it always offers to skip the version",
                    "skip this version" in details, details[-300:])
            s.check("a clean checkout is offered the update, a dirty one an "
                    "explanation",
                    ("cannot update here" in details and "local change" in details)
                    if blocked else "update now" in details, details[-300:])

            t.mark()
            t.send("s")
            s.check("skipping returns to the browser", t.expect("system check", 12))
            s.check("and the notice is gone",
                    "Update available" not in t.screen(1.0))
            s.check("the launcher is still running", t.alive())
            t.send("q")
            s.check("it still quits cleanly", t.wait(15))
    finally:
        shutil.rmtree(cache, ignore_errors=True)

    # ---- turning it off ---------------------------------------------------- #
    rc = subprocess.run(["bash", os.path.join(REPO, "toolkit.sh"), "--list",
                         "--no-update-check"], capture_output=True, text=True,
                        timeout=120, cwd=REPO)
    s.check("--no-update-check still lists the scripts",
            rc.returncode == 0 and "harden.sh" in rc.stdout)

    # ---- the real thing ----------------------------------------------------- #
    if not FULL:
        s.skip("the live GitHub check", "set TOOLKIT_TEST_FULL=1")
        return s.finish()

    cache = tempfile.mkdtemp(prefix="toolkit-update-live-")
    try:
        env = dict(os.environ, TOOLKIT_CACHE_DIR=cache)
        p = subprocess.run(["bash", os.path.join(REPO, "toolkit.sh"),
                            "--check-update"], capture_output=True, text=True,
                           timeout=120, cwd=REPO, env=env)
        s.check("--check-update exits cleanly", p.returncode == 0, p.stdout + p.stderr)
        out = p.stdout + p.stderr
        s.check("--check-update reaches a conclusion",
                any(w in out for w in ("Up to date", "Update available",
                                       "Could not check", "newer version")), out)
        if "Could not check" in out:
            s.skip("comparing against GitHub", "the API was unreachable here")
        else:
            s.check("it names the repository", "toolkit" in out)
            s.check("a cache file is written",
                    os.path.isfile(os.path.join(cache, "update.json")))
    finally:
        shutil.rmtree(cache, ignore_errors=True)
    return s.finish()


if __name__ == "__main__":
    sys.exit(main())
