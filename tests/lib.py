# -*- coding: utf-8 -*-
"""Shared test helpers: a tiny assertion harness and a pseudo-terminal driver.

No test framework on purpose — the repository has no dependencies, and neither
does its test suite. Everything here is the Python standard library.

The pty driver matters more than it looks: the interactive parts of this
repository only misbehave on a *real* terminal (raw mode, escape sequences,
alternate screen), so testing them through a pipe would prove nothing. The
arrow-key bug that shipped in netwatch 1.0 was invisible to every other kind of
test and obvious to this one.
"""

import os
import re
import select
import signal
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
SELECTED = "\x1b[48;5;24m"          # the highlight both TUIs use for the cursor row

KEY = {
    "up": b"\x1b[A", "down": b"\x1b[B", "right": b"\x1b[C", "left": b"\x1b[D",
    "ss3_up": b"\x1bOA", "ss3_down": b"\x1bOB",
    "enter": b"\r", "esc": b"\x1b", "space": b" ",
    "pgup": b"\x1b[5~", "pgdn": b"\x1b[6~",
}

_GREEN, _RED, _YELLOW, _GREY, _OFF = "", "", "", "", ""
if sys.stdout.isatty():
    _GREEN, _RED, _YELLOW = "\033[32m", "\033[31m", "\033[33m"
    _GREY, _OFF = "\033[90m", "\033[0m"


class Suite:
    """Collects results and prints them as they happen."""

    def __init__(self, name):
        self.name = name
        self.passed = 0
        self.failed = []
        self.skipped = []
        self.started = time.monotonic()
        print(f"\n{_GREY}── {name} {'─' * max(0, 60 - len(name))}{_OFF}")

    def check(self, label, condition, detail=""):
        if condition:
            self.passed += 1
            print(f"  {_GREEN}pass{_OFF}  {label}")
        else:
            self.failed.append(label)
            print(f"  {_RED}FAIL{_OFF}  {label}" + (f"\n        {detail}" if detail else ""))
        return bool(condition)

    def skip(self, label, why):
        self.skipped.append(label)
        print(f"  {_YELLOW}skip{_OFF}  {label} {_GREY}({why}){_OFF}")

    def finish(self):
        took = time.monotonic() - self.started
        if self.failed:
            print(f"  {_RED}{len(self.failed)} failed{_OFF}, {self.passed} passed"
                  f" {_GREY}({took:.1f}s){_OFF}")
            return 1
        print(f"  {_GREEN}{self.passed} passed{_OFF}"
              + (f", {len(self.skipped)} skipped" if self.skipped else "")
              + f" {_GREY}({took:.1f}s){_OFF}")
        return 0


class Term:
    """Runs a program on a real pseudo-terminal and lets a test type at it."""

    def __init__(self, argv, cwd=None, env=None, cols=110, rows=32):
        self.argv = argv
        self.buffer = ""          # everything received, escape codes and all
        self.text = ""            # the same, with the escape codes stripped
        self._cursor = 0          # how far expect() has already matched
        self._reaped = False
        self._status = None
        environ = dict(os.environ)
        environ.update({"TERM": "xterm-256color", "COLUMNS": str(cols),
                        "LINES": str(rows)})
        environ.update(env or {})
        import pty
        self.pid, self.fd = pty.fork()
        if self.pid == 0:                       # child
            try:
                if cwd:
                    os.chdir(cwd)
                os.execvpe(argv[0], argv, environ)
            except Exception:                   # pragma: no cover - child only
                os._exit(127)
        self._set_size(rows, cols)

    def _set_size(self, rows, cols):
        try:
            import fcntl
            import struct
            import termios
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ,
                        struct.pack("HHHH", rows, cols, 0, 0))
        except Exception:
            pass

    # -- reading ---------------------------------------------------------- #
    def raw(self, seconds=1.0):
        """Everything received within `seconds`, escape codes included."""
        out = ""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            ready, _, _ = select.select([self.fd], [], [], 0.05)
            if not ready:
                continue
            try:
                chunk = os.read(self.fd, 262144)
            except OSError:
                break
            if not chunk:
                break
            out += chunk.decode("utf-8", "replace")
        self.buffer += out
        self.text += ANSI.sub("", out)
        return out

    def read(self, seconds=1.0):
        return ANSI.sub("", self.raw(seconds))

    def expect(self, needle, timeout=15.0, poll=0.2):
        """Wait until `needle` appears, and remember where it did.

        Matching runs against everything received so far, from just after the
        previous match. Two strings that arrive in the same read would otherwise
        be lost to whichever expect() happened to consume the chunk — which
        looks exactly like a broken program and is not.
        """
        end = time.monotonic() + timeout
        while True:
            idx = self.text.find(needle, self._cursor)
            if idx >= 0:
                self._cursor = idx + len(needle)
                return True
            if time.monotonic() >= end:
                return False
            if not self.alive():
                self.read(poll)
                idx = self.text.find(needle, self._cursor)
                if idx >= 0:
                    self._cursor = idx + len(needle)
                    return True
                return False
            self.read(poll)

    def screen(self, settle=0.8):
        """Read for a moment, then return the plain text of what arrived."""
        return ANSI.sub("", self.raw(settle))

    def selection(self, settle=0.8):
        """The label of the row the TUI is currently highlighting."""
        text = self.raw(settle)
        idx = text.rfind(SELECTED)
        if idx < 0:
            return None
        tail = ANSI.sub("", text[idx:idx + 160]).strip()
        return tail.splitlines()[0].strip() if tail else None

    # -- writing ---------------------------------------------------------- #
    def send(self, data):
        if isinstance(data, str):
            data = KEY.get(data, data.encode() if not isinstance(data, bytes) else data)
        os.write(self.fd, data)
        return self

    def keys(self, *names, pause=0.35):
        for name in names:
            self.send(name)
            time.sleep(pause)
        return self

    def type(self, text, enter=True):
        os.write(self.fd, text.encode() + (b"\r" if enter else b""))
        time.sleep(0.4)
        return self

    # -- lifecycle -------------------------------------------------------- #
    def alive(self):
        if self._reaped:
            return False
        try:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            self._reaped = True
            return False
        if (pid, status) != (0, 0):
            self._reaped = True
            self._status = status
            return False
        return True

    def wait(self, timeout=15.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end and self.alive():
            self.read(0.3)
        return not self.alive()

    @property
    def exit_code(self):
        if self._status is None:
            return None
        return os.waitstatus_to_exitcode(self._status) if hasattr(os, "waitstatus_to_exitcode") \
            else (self._status >> 8)

    def close(self):
        try:
            os.kill(self.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def have(binary):
    from shutil import which
    return which(binary) is not None
