#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""What you type at a prompt must show up, whoever started the prompt.

The launcher drives the terminal in cbreak mode (no echo, no line editing) so
that single keys arrive at once. Anything that read a line while that mode was
still on — the launcher's own prompts, and every script it started — got the
keys but echoed nothing and could not erase, so netwatch's "Start now? (y/n)"
looked as if it refused to take a 'y'. Checks that only look at the value that
came back cannot see that; these watch what the terminal actually echoes, before
Enter is pressed, and press Enter the way a terminal does (a carriage return).
"""

import os
import sys
import textwrap

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import REPO, Suite, Term                                  # noqa: E402

MODE = ("import termios; a = termios.tcgetattr(0); "
        "print('{tag} echo=%d icanon=%d' % (bool(a[3] & termios.ECHO), "
        "bool(a[3] & termios.ICANON)), flush=True)")


def harness(body):
    """A python -c program that imports the launcher and runs `body` the way the
    browser does: with its KeyReader holding the terminal in cbreak mode."""
    lines = ["import sys", f"sys.path.insert(0, {REPO!r})", "import toolkit",
             "with toolkit.KeyReader() as keys:", "    toolkit.KEYS = keys"]
    lines += ["    " + line for line in textwrap.dedent(body).strip("\n").splitlines()]
    lines.append("    " + MODE.format(tag="AFTER"))
    return ["python3", "-c", "\n".join(lines)]


def typed_line_is_visible(s, t, what, text="zq7"):
    """Type without Enter and see whether the terminal echoes it."""
    t.mark()
    t.send(text)
    s.check(f"{what}: typed characters are echoed", t.expect(text, 4),
            repr(t.text[-200:]))


def main():
    s = Suite("terminal modes: prompts and child scripts can be typed into")

    # ---- a script started from the launcher ----------------------------- #
    child = ("import termios; a = termios.tcgetattr(0); "
             "print('CHILD echo=%d icanon=%d' % (bool(a[3] & termios.ECHO), "
             "bool(a[3] & termios.ICANON)), flush=True); "
             "print('GOT=%r' % input('answer: '), flush=True)")
    body = f"""
        class Probe:
            name = 'probe'
            def argv(self, system, preview, extra):
                return [sys.executable, '-c', {child!r}]
            def pretty_command(self, system, preview, extra):
                return 'probe'
        toolkit.run_script(Probe(), None, pause_after=False)
    """
    with Term(harness(body), cwd=REPO) as t:
        s.check("a child script gets echo and line mode",
                t.expect("CHILD echo=1 icanon=1", 15), t.text[-300:])
        t.expect("answer:", 5)
        typed_line_is_visible(s, t, "a child script's prompt")
        t.send("\x7f8\r")                          # Backspace, then a real Enter
        s.check("Backspace edits the line and Enter ends it",
                t.expect("GOT='zq8'", 5), t.text[-200:])
        s.check("the browser gets its single-key mode back afterwards",
                t.expect("AFTER echo=0 icanon=0", 5), t.text[-200:])

    # ---- the launcher's own prompt --------------------------------------- #
    body = """
        value = toolkit.prompt("Value")
        print('GOT=%r' % value, flush=True)
    """
    with Term(harness(body), cwd=REPO) as t:
        t.expect("Value", 10)
        typed_line_is_visible(s, t, "the launcher's prompt")
        t.send("\x7f8\r")
        s.check("the prompt returns the edited value", t.expect("GOT='zq8'", 5),
                t.text[-200:])
        s.check("and the browser's mode is restored",
                t.expect("AFTER echo=0 icanon=0", 5), t.text[-200:])

    # ---- netwatch, started by something that left cbreak mode on ---------- #
    code = textwrap.dedent(f"""
        import sys, tty
        tty.setcbreak(0)                     # what the old launcher handed over
        sys.path.insert(0, {os.path.join(REPO, 'linux', 'netwatch')!r})
        import netwatch
        print('GOT=%r' % netwatch.ask('Start now? (y/n)', 'y'), flush=True)
        {MODE.format(tag='AFTER')}
    """)
    with Term(["python3", "-c", code], cwd=REPO) as t:
        t.expect("Start now?", 10)
        typed_line_is_visible(s, t, "netwatch under an inherited cbreak terminal", "nx")
        t.send("\x7f\r")
        s.check("netwatch reads the edited answer", t.expect("GOT='n'", 5),
                t.text[-200:])
        s.check("and leaves the inherited mode as it found it",
                t.expect("AFTER echo=0 icanon=0", 5), t.text[-200:])

    # ---- the whole path the bug report describes -------------------------- #
    # launcher -> netwatch -> Settings -> "How long to monitor" -> type a value
    with Term(["bash", "toolkit.sh"], cwd=REPO, cols=120, rows=40) as t:
        s.check("the launcher starts", t.expect("system check", 25))
        t.send("/")
        t.read(0.5)
        t.type("netwatch")
        t.read(0.8)
        t.mark()
        t.send("enter")
        s.check("netwatch's summary opens", t.expect("netwatch", 8))
        t.mark()
        t.send("enter")
        s.check("netwatch starts from the launcher", t.expect("Main menu", 20),
                t.text[-400:])
        # Wait for text only the next screen has before each key: "Settings" is
        # also an item of the main menu, and a key sent before a menu is listening
        # lands in the previous one.
        t.mark()
        t.send("3")                                 # Settings
        s.check("its settings open",
                t.expect("Enter changes the highlighted value", 8), t.text[-300:])
        t.mark()
        t.send("1")                                 # How long to monitor
        s.check("the duration prompt appears", t.expect("? How long to monitor", 8),
                t.text[-300:])
        t.read(0.5)
        typed_line_is_visible(s, t, "netwatch's settings prompt via the launcher", "45m")
        t.mark()
        t.send("\r")
        s.check("the new value is taken", t.expect("45 m", 8), t.text[-300:])
        t.mark()
        t.send("q")                                 # leave Settings
        t.expect("Main menu", 8)
        t.mark()
        t.send("q")                                 # leave netwatch
        s.check("netwatch returns to the launcher",
                t.expect("Press Enter to return to the launcher", 15), t.text[-300:])
        t.send("\r")
        s.check("back in the browser", t.expect("system check", 10))
        t.send("q")
        s.check("and it quits", t.wait(15))
    return s.finish()


if __name__ == "__main__":
    sys.exit(main())
