#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
toolkit.py — the control panel for this repository.

Finds every script in the repo, works out what each one needs, checks whether
*this* machine satisfies it, shows a short summary, and runs the chosen one
after a single confirmation.

    ./toolkit.sh                  browse and run
    ./toolkit.sh --list           print the discovered scripts and exit
    ./toolkit.sh --check          run the system checks for everything and exit
    ./toolkit.sh --run <id> [..]  run one script non-interactively
    ./toolkit.sh --help           this text

Scripts describe themselves with `# toolkit-*:` comment lines in their header
(see README). A script without them still shows up — the launcher falls back to
reading its shebang, its filename and its first comment block — so dropping a new
file into the repo is enough to make it appear here.
"""

from __future__ import annotations

import os
import re
import select
import shutil
import socket
import subprocess
import sys
import textwrap
import time

APP = "toolkit"
VERSION = "1.0"
REPO = os.path.dirname(os.path.abspath(__file__))

SKIP_DIRS = {".git", ".github", "assets", "node_modules", "__pycache__", ".venv",
             "tests"}
SCRIPT_EXT = {".sh", ".py"}


# --------------------------------------------------------------------------- #
# terminal
# --------------------------------------------------------------------------- #
def _enable_vt():
    if os.name != "nt":
        return True
    try:
        import ctypes
        k = ctypes.windll.kernel32
        for handle in (-11, -12):
            h = k.GetStdHandle(handle)
            mode = ctypes.c_uint32()
            if k.GetConsoleMode(h, ctypes.byref(mode)):
                k.SetConsoleMode(h, mode.value | 0x0004)
        return True
    except Exception:
        return False


_VT = _enable_vt()
_TTY = sys.stdout.isatty()
COLOR = _TTY and _VT and os.environ.get("NO_COLOR") is None
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    UNI = True
except Exception:
    UNI = (sys.stdout.encoding or "").lower().replace("-", "") in ("utf8", "utf")


def _c(code):
    return code if COLOR else ""


C0 = _c("\033[0m")
BOLD = _c("\033[1m")
DIM = _c("\033[2m")
RED = _c("\033[38;5;203m")
GREEN = _c("\033[38;5;114m")
YELLOW = _c("\033[38;5;221m")
BLUE = _c("\033[38;5;75m")
CYAN = _c("\033[38;5;80m")
MAGENTA = _c("\033[38;5;177m")
GREY = _c("\033[38;5;245m")
FAINT = _c("\033[38;5;240m")
WHITE = _c("\033[38;5;255m")
SEL = _c("\033[48;5;24m")

BOX = ({"tl": "╭", "tr": "╮", "bl": "╰", "br": "╯", "h": "─", "v": "│"} if UNI
       else {"tl": "+", "tr": "+", "bl": "+", "br": "+", "h": "-", "v": "|"})
GLYPH = ({"ok": "✔", "warn": "▲", "bad": "✖", "note": "●", "dot": "▪",
          "arrow": "▸", "run": "▶"} if UNI
         else {"ok": "+", "warn": "!", "bad": "x", "note": "*", "dot": "-",
               "arrow": ">", "run": ">"})
STATUS_COLOR = {"ok": GREEN, "warn": YELLOW, "bad": RED, "note": BLUE, "dot": GREY}

ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def vlen(s):
    return len(ANSI_RE.sub("", s))


def clip(s, width):
    """Truncate to `width` visible columns, keeping colour codes intact."""
    if vlen(s) <= width:
        return s
    out, seen, i = [], 0, 0
    while i < len(s) and seen < width:
        if s[i] == "\033":
            j = s.find("m", i)
            if j == -1:
                break
            out.append(s[i:j + 1])
            i = j + 1
            continue
        out.append(s[i])
        seen += 1
        i += 1
    return "".join(out) + C0


def pad(s, width):
    return s + " " * max(0, width - vlen(s))


def fit(s, width):
    return pad(clip(s, width), width)


def ellipsis(s, width):
    """Clip, but say so — a sentence cut mid-word reads like a bug."""
    if vlen(s) <= width:
        return s
    return clip(s, max(1, width - 1)) + ("…" if UNI else "~")


def wrap(text, width, indent=""):
    out = []
    for para in (text or "").split("\n"):
        if not para.strip():
            out.append("")
            continue
        out.extend(textwrap.wrap(para, width=max(10, width - len(indent)),
                                 initial_indent=indent, subsequent_indent=indent)
                   or [""])
    return out


def term_size():
    try:
        s = shutil.get_terminal_size((100, 30))
        return max(60, min(s.columns, 220)), max(16, min(s.lines, 80))
    except Exception:
        return 100, 30


def panel(title, lines, width, height=None, accent=GREY):
    """A titled box of exactly `width` columns (and `height` rows when given)."""
    inner = width - 4
    head = f"{BOX['tl']}{BOX['h']} {title} " if title else BOX["tl"] + BOX["h"]
    head += BOX["h"] * max(0, width - vlen(head) - 1) + BOX["tr"]
    out = [f"{accent}{head}{C0}"]
    body = list(lines)
    if height is not None:
        body = body[:height - 2] + [""] * max(0, (height - 2) - len(body))
    for ln in body:
        out.append(f"{accent}{BOX['v']}{C0} {fit(ln, inner)} {accent}{BOX['v']}{C0}")
    out.append(f"{accent}{BOX['bl']}{BOX['h'] * (width - 2)}{BOX['br']}{C0}")
    return out


def side_by_side(left, right, gap=0):
    """Zip two rendered panels into one column of lines."""
    height = max(len(left), len(right))
    left += [""] * (height - len(left))
    right += [""] * (height - len(right))
    lw = max((vlen(x) for x in left), default=0)
    return [pad(a, lw) + " " * gap + b for a, b in zip(left, right)]


CURSOR_KEYS = {b"A": "up", b"B": "down", b"C": "right", b"D": "left"}


class KeyReader:
    """Single-key reader. Reads the descriptor directly: sys.stdin buffers ahead,
    which makes select() and the actual bytes disagree and turns an arrow key
    into a stray Escape."""

    def __init__(self):
        self.enabled = False
        self._fd = None
        self._old = None
        self._buf = b""

    def __enter__(self):
        self._buf = b""
        if not sys.stdin.isatty():
            return self
        if os.name == "nt":
            try:
                import msvcrt
                self.enabled = hasattr(msvcrt, "getwch")
            except Exception:
                pass
            return self
        try:
            import termios
            import tty
            self._fd = sys.stdin.fileno()
            self._old = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
            self.enabled = True
        except Exception:
            self.enabled = False
        return self

    def __exit__(self, *exc):
        if self._old is not None:
            try:
                import termios
                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old)
            except Exception:
                pass
        self.enabled = False
        return False

    def get(self, timeout=0.3):
        if not self.enabled:
            time.sleep(timeout)
            return None
        if os.name == "nt":
            import msvcrt
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if msvcrt.kbhit():
                    ch = msvcrt.getwch()
                    if ch in ("\x00", "\xe0"):
                        return {"H": "up", "P": "down", "K": "left",
                                "M": "right"}.get(msvcrt.getwch())
                    if ch in ("\r", "\n"):
                        return "enter"
                    if ch == "\x1b":
                        return "esc"
                    if ch == "\x03":
                        raise KeyboardInterrupt
                    return ch
                time.sleep(0.02)
            return None
        key = self._pop()
        if key is not None:
            return key
        self._buf += self._read(timeout)
        key = self._pop()
        if key is not None:
            return key
        if self._buf:
            self._buf += self._read(0.05)
            return self._pop(final=True)
        return None

    def _read(self, timeout):
        try:
            ready, _, _ = select.select([self._fd], [], [], timeout)
        except Exception:
            return b""
        if not ready:
            return b""
        try:
            return os.read(self._fd, 64)
        except (OSError, InterruptedError, ValueError):
            return b""

    def _pop(self, final=False):
        while self._buf:
            buf = self._buf
            if buf[:1] != b"\x1b":
                ch, self._buf = buf[:1], buf[1:]
                if ch == b"\x03":
                    raise KeyboardInterrupt
                if ch in (b"\r", b"\n"):
                    return "enter"
                if ch in (b"\x7f", b"\x08"):
                    return "backspace"
                decoded = ch.decode("utf-8", "ignore")
                if decoded:
                    return decoded
                continue
            if len(buf) == 1:
                if not final:
                    return None
                self._buf = b""
                return "esc"
            if buf[1:2] == b"O":
                if len(buf) < 3:
                    if not final:
                        return None
                    self._buf = b""
                    return "esc"
                key, self._buf = CURSOR_KEYS.get(buf[2:3]), buf[3:]
                if key:
                    return key
                continue
            if buf[1:2] != b"[":
                self._buf = buf[1:]
                return "esc"
            i = 2
            while i < len(buf) and 0x30 <= buf[i] <= 0x3F:
                i += 1
            while i < len(buf) and 0x20 <= buf[i] <= 0x2F:
                i += 1
            if i >= len(buf):
                if not final:
                    return None
                self._buf = b""
                return "esc"
            seq, self._buf = buf[2:i + 1], buf[i + 1:]
            key = CURSOR_KEYS.get(seq[-1:])
            if key:
                return key
            if seq in (b"5~", b"6~"):
                return "pgup" if seq == b"5~" else "pgdn"
            if seq in (b"H", b"1~"):
                return "home"
            if seq in (b"F", b"4~"):
                return "end"
        return None


class Screen:
    def __init__(self):
        self.active = False

    def enter(self):
        if _TTY:
            sys.stdout.write("\033[?25l\033[2J")
            sys.stdout.flush()
            self.active = True

    def leave(self):
        if self.active:
            sys.stdout.write("\033[?25h\033[0m\033[2J\033[H")
            sys.stdout.flush()
            self.active = False

    def paint(self, lines):
        if not _TTY:
            return
        w, h = term_size()
        out = ["\033[H"]
        for i, line in enumerate(lines[:h - 1]):
            out.append(clip(line, w) + "\033[K")
            if i < min(len(lines), h - 1) - 1:
                out.append("\r\n")
        out.append("\033[J")
        sys.stdout.write("".join(out))
        sys.stdout.flush()


def info(msg):
    print(f"{BLUE}[*]{C0} {msg}", flush=True)


def ok(msg):
    print(f"{GREEN}[+]{C0} {msg}", flush=True)


def warn(msg):
    print(f"{YELLOW}[!]{C0} {msg}", file=sys.stderr, flush=True)


def die(msg, code=1):
    print(f"{RED}[x]{C0} {msg}", file=sys.stderr, flush=True)
    sys.exit(code)


# --------------------------------------------------------------------------- #
# this machine
# --------------------------------------------------------------------------- #
def _run(args, timeout=6):
    try:
        p = subprocess.run(args, capture_output=True, timeout=timeout, text=True,
                           errors="replace")
        return p.returncode, (p.stdout or "").strip()
    except Exception:
        return 127, ""


def _sh(snippet, timeout=6):
    """Run a small shell snippet; returns (rc, first line of output)."""
    shell = shutil.which("bash") or shutil.which("sh")
    if not shell:
        return 127, ""
    rc, out = _run([shell, "-c", snippet], timeout=timeout)
    return rc, (out.splitlines()[0].strip() if out else "")


class System:
    """Everything the launcher needs to decide whether a script fits here."""

    def __init__(self):
        self.hostname = socket.gethostname()
        self.os = sys.platform
        self.distro = ""
        self.distro_id = ""
        self.family = "unknown"
        self.version = ""
        self.kernel = ""
        self.arch = ""
        self.pkg = ""
        self.is_root = False
        self.can_sudo = False
        self.systemd = False
        self.container = ""
        self.cpus = 0
        self.mem_gb = 0.0
        self.free_gb = 0.0
        self.internet = None
        self.probe()

    def probe(self):
        import platform
        self.kernel = platform.release()
        self.arch = platform.machine()
        self.cpus = os.cpu_count() or 1
        try:
            self.is_root = os.geteuid() == 0
        except AttributeError:
            self.is_root = False
        if os.path.exists("/etc/os-release"):
            data = {}
            try:
                with open("/etc/os-release", encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        k, _, v = line.strip().partition("=")
                        if k:
                            data[k] = v.strip().strip('"')
            except OSError:
                pass
            self.distro = data.get("PRETTY_NAME", "") or data.get("NAME", "")
            self.distro_id = data.get("ID", "")
            self.version = data.get("VERSION_ID", "")
            blob = f"{self.distro_id} {data.get('ID_LIKE', '')}".lower()
            for family, keys in (("debian", ("debian", "ubuntu")),
                                 ("fedora", ("fedora", "rhel", "centos")),
                                 ("arch", ("arch",)), ("suse", ("suse",)),
                                 ("alpine", ("alpine",))):
                if any(k in blob for k in keys):
                    self.family = family
                    break
        elif self.os == "darwin":
            self.family, self.distro = "macos", "macOS"
        elif os.name == "nt":
            self.family, self.distro = "windows", "Windows"
        for manager in ("apt-get", "dnf", "yum", "pacman", "zypper", "apk"):
            if shutil.which(manager):
                self.pkg = "apt" if manager == "apt-get" else manager
                break
        self.systemd = os.path.isdir("/run/systemd/system")
        if not self.is_root and shutil.which("sudo"):
            self.can_sudo = _sh("sudo -n true", timeout=4)[0] == 0 or True
        if os.path.exists("/.dockerenv"):
            self.container = "docker"
        else:
            try:
                with open("/proc/1/cgroup", encoding="utf-8", errors="replace") as fh:
                    blob = fh.read()
                for marker in ("docker", "containerd", "lxc", "kubepods"):
                    if marker in blob:
                        self.container = marker
                        break
            except OSError:
                pass
            if not self.container and "microsoft" in self.kernel.lower():
                self.container = "wsl"
        try:
            with open("/proc/meminfo", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("MemTotal:"):
                        self.mem_gb = int(line.split()[1]) / 1048576.0
                        break
        except OSError:
            pass
        try:
            usage = shutil.disk_usage("/")
            self.free_gb = usage.free / 1073741824.0
        except OSError:
            pass

    def check_internet(self):
        for host, port in (("1.1.1.1", 443), ("8.8.8.8", 53)):
            try:
                s = socket.create_connection((host, port), timeout=2.0)
                s.close()
                self.internet = True
                return True
            except OSError:
                continue
        self.internet = False
        return False

    @property
    def root_ok(self):
        return self.is_root or self.can_sudo

    def summary_line(self):
        bits = [f"{WHITE}{self.distro or self.os}{C0}"]
        if self.kernel:
            bits.append(f"kernel {self.kernel.split('-')[0]}")
        if self.arch:
            bits.append(self.arch)
        if self.pkg:
            bits.append(self.pkg)
        bits.append(f"systemd {GREEN + GLYPH['ok'] if self.systemd else GREY + '—'}{C0}")
        if self.is_root:
            bits.append(f"{GREEN}root{C0}")
        elif self.can_sudo:
            bits.append(f"{GREEN}sudo{C0}")
        else:
            bits.append(f"{RED}no root{C0}")
        if self.internet is True:
            bits.append(f"net {GREEN}{GLYPH['ok']}{C0}")
        elif self.internet is False:
            bits.append(f"net {RED}{GLYPH['bad']}{C0}")
        if self.container:
            bits.append(f"{MAGENTA}{self.container}{C0}")
        return f" {GREY}·{C0} ".join(bits)

    def details(self):
        return [
            ("Host", self.hostname),
            ("Distribution", f"{self.distro or 'unknown'}"
                             + (f"  (family: {self.family})" if self.family != "unknown" else "")),
            ("Kernel / arch", f"{self.kernel}  {self.arch}"),
            ("Package manager", self.pkg or "none detected"),
            ("Init system", "systemd" if self.systemd
             else "not systemd (or not booted with it)"),
            ("Privileges", "running as root" if self.is_root
             else ("sudo available" if self.can_sudo else "unprivileged, no sudo")),
            ("Environment", self.container or "bare metal / VM"),
            ("CPU / memory", f"{self.cpus} cores"
             + (f", {self.mem_gb:.1f} GiB RAM" if self.mem_gb else "")),
            ("Free space on /", f"{self.free_gb:.1f} GiB" if self.free_gb else "unknown"),
            ("Internet", {True: "reachable", False: "unreachable",
                          None: "not checked"}[self.internet]),
            ("Repository", REPO),
        ]


# --------------------------------------------------------------------------- #
# script discovery
# --------------------------------------------------------------------------- #
META_RE = re.compile(r"^#\s*toolkit-([a-z-]+)\s*:\s*(.*?)\s*$", re.I)
TITLE_RE = re.compile(r"^\s*([\w.-]+\.(?:sh|py))\s*[—-]{1,2}\s*(.+)$")

KIND_LABEL = {"installer": "installer", "tool": "tool", "destructive": "destructive",
              "service": "service"}
KIND_COLOR = {"installer": CYAN, "tool": GREEN, "destructive": RED, "service": MAGENTA}


class Arg:
    def __init__(self, flag, label, kind="text", value=""):
        self.flag = flag
        self.label = label
        self.kind = (kind or "text").lower()
        self.value = value

    @property
    def required(self):
        return self.kind == "required"

    def as_argv(self):
        if self.kind == "flag":
            return [self.flag] if str(self.value).lower() in ("1", "true", "yes", "on") else []
        return [self.flag, str(self.value)] if str(self.value).strip() else []

    def display(self):
        if self.kind == "flag":
            return "on" if str(self.value).lower() in ("1", "true", "yes", "on") else "off"
        return str(self.value) if str(self.value).strip() else (
            "(required)" if self.required else "(unset)")


class Script:
    def __init__(self, path):
        self.path = path
        self.rel = os.path.relpath(path, REPO).replace(os.sep, "/")
        self.id = re.sub(r"[^a-z0-9]+", "-", os.path.splitext(os.path.basename(path))[0].lower())
        self.meta = {}
        self.args = []
        self.hidden = False
        self.has_meta = False
        self.description = ""
        self._parse()
        self.checks = []
        self.verdict = "unknown"
        self.present = ""

    # -- parsing --------------------------------------------------------- #
    def _parse(self):
        lines = []
        try:
            with open(self.path, encoding="utf-8", errors="replace") as fh:
                for i, line in enumerate(fh):
                    if i > 160:
                        break
                    lines.append(line.rstrip("\n"))
        except OSError:
            lines = []
        prose = []
        in_header = True
        for line in lines:
            m = META_RE.match(line)
            if m:
                self.has_meta = True
                key, value = m.group(1).lower(), m.group(2)
                if key == "arg":
                    parts = [p.strip() for p in value.split("|")]
                    if parts and parts[0]:
                        self.args.append(Arg(parts[0],
                                             parts[1] if len(parts) > 1 else parts[0],
                                             parts[2] if len(parts) > 2 else "text"))
                else:
                    self.meta[key] = value
                continue
            if not in_header:
                continue
            if line.startswith("#!") or line.startswith("# -*-"):
                continue
            if line.startswith("#"):
                prose.append(line[1:].lstrip() if len(line) > 1 else "")
            elif line.strip() in ('"""', "'''"):
                continue
            elif line.strip() == "":
                prose.append("")
            else:
                in_header = False
        # Python tools keep their prose in the module docstring instead.
        if not any(p.strip() for p in prose):
            prose = self._docstring(lines)
        self.description, fallback_summary = self._prose_to_text(prose)
        self.meta.setdefault("summary", fallback_summary)
        self.hidden = self.meta.get("hidden", "").lower() in ("1", "yes", "true")

    @staticmethod
    def _docstring(lines):
        body, started = [], False
        for line in lines:
            if not started:
                if line.strip().startswith(('"""', "'''")):
                    started = True
                    rest = line.strip()[3:]
                    if rest:
                        body.append(rest)
                continue
            if line.strip().endswith(('"""', "'''")):
                break
            body.append(line)
        return body

    def _prose_to_text(self, prose):
        summary = ""
        cleaned = []
        for line in prose:
            m = TITLE_RE.match(line)
            if m and not summary:
                summary = m.group(2).strip().rstrip(".")
                continue
            cleaned.append(line)
        while cleaned and not cleaned[0].strip():
            cleaned.pop(0)
        # Keep the first paragraph or two — enough to explain, short enough to read.
        para, out = [], []
        for line in cleaned:
            low = line.strip().lower()
            if low.startswith(("usage", "options", "usage:", "actions", "what it does",
                               "install options", "commands")):
                break
            if not line.strip():
                if para:
                    out.append(" ".join(para))
                    para = []
                    if len(out) >= 2:
                        break
                continue
            if line.strip().startswith(("*", "-", "1.", "2.", "3.")):
                if para:
                    out.append(" ".join(para))
                    para = []
                if len(out) >= 2:
                    break
                continue
            para.append(line.strip())
        if para:
            out.append(" ".join(para))
        text = "\n\n".join(out[:2]).strip()
        if not summary:
            summary = (text.split(". ")[0][:110] if text else
                       os.path.basename(self.path))
        return text, summary

    # -- accessors ------------------------------------------------------- #
    def get(self, key, default=""):
        return self.meta.get(key, default)

    def list_of(self, key):
        raw = self.meta.get(key, "")
        return [x.strip() for x in re.split(r"[,\s]+", raw) if x.strip()]

    @property
    def name(self):
        return self.get("name") or os.path.basename(self.path)

    @property
    def summary(self):
        return self.get("summary", "")

    @property
    def detail_text(self):
        """The prose minus whatever merely repeats the one-line summary."""
        desc = (self.description or "").strip()
        summary = (self.summary or "").strip()
        if desc and summary and desc[:40].lower() == summary[:40].lower():
            parts = desc.split("\n\n")
            desc = "\n\n".join(parts[1:]) if len(parts) > 1 else ""
        return desc.strip()

    @property
    def kind(self):
        k = self.get("kind", "").lower()
        if k in KIND_LABEL:
            return k
        base = os.path.basename(self.path).lower()
        if any(w in base for w in ("wipe", "destroy", "purge", "erase")):
            return "destructive"
        if base.startswith("install") or "install" in base:
            return "installer"
        return "tool"

    @property
    def category(self):
        cat = self.get("category")
        if cat:
            return cat
        parent = os.path.basename(os.path.dirname(self.path))
        if parent in ("", "."):
            return "General"
        return parent.capitalize()

    @property
    def order(self):
        try:
            return int(self.get("order", "50"))
        except ValueError:
            return 50

    @property
    def needs_root(self):
        value = self.get("root", "").lower()
        if value in ("yes", "true", "1", "required"):
            return "yes"
        if value in ("no", "false", "0"):
            return "no"
        if value in ("optional", "maybe"):
            return "optional"
        try:
            with open(self.path, encoding="utf-8", errors="replace") as fh:
                blob = fh.read(20000)
        except OSError:
            return "no"
        if re.search(r"\bid -u\b|\bEUID\b|\bsudo\b|geteuid", blob):
            return "optional" if self.kind == "tool" else "yes"
        return "no"

    def base_args(self, preview=False):
        raw = self.get("preview" if preview else "run", "")
        return raw.split() if raw else []

    def argv(self, system, preview=False, extra=""):
        interp = ["bash"] if self.path.endswith(".sh") else [sys.executable or "python3"]
        cmd = interp + [self.path] + self.base_args(preview)
        for a in self.args:
            cmd += a.as_argv()
        if extra.strip():
            cmd += extra.split()
        if self.needs_root == "yes" and not system.is_root and system.can_sudo:
            cmd = ["sudo"] + cmd
        return cmd

    def pretty_command(self, system, preview=False, extra=""):
        argv = self.argv(system, preview, extra)
        shown = []
        for part in argv:
            shown.append(part.replace(REPO + os.sep, "").replace(REPO + "/", "")
                         if part.startswith(REPO) else part)
        return " ".join(shown)


def discover(repo=REPO):
    """Every runnable script in the repo, newest ones included automatically."""
    found = []
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for fname in files:
            if os.path.splitext(fname)[1] not in SCRIPT_EXT:
                continue
            path = os.path.join(root, fname)
            if os.path.abspath(path) == os.path.abspath(__file__):
                continue
            if fname in ("toolkit.sh", "toolkit.py"):
                continue
            found.append(path)
    # A tool shipped as launcher + engine (x.sh + x.py) is one entry, not two.
    stems = {}
    for path in found:
        stem = os.path.splitext(path)[0]
        stems.setdefault(stem, []).append(path)
    entries = []
    for stem, paths in stems.items():
        if len(paths) > 1:
            paths = [p for p in paths if p.endswith(".sh")] or paths
        entries.append(paths[0])
    scripts = []
    for path in sorted(entries):
        try:
            s = Script(path)
        except Exception:
            continue
        if not s.hidden:
            scripts.append(s)
    scripts.sort(key=lambda s: (CATEGORY_ORDER.get(s.category, 50), s.category,
                                s.order, s.name.lower()))
    return scripts


CATEGORY_ORDER = {"Containers": 10, "Security": 20, "Diagnostics": 30,
                  "Networking": 35, "Linux": 40, "Proxmox": 60, "General": 70}


# --------------------------------------------------------------------------- #
# preflight — does this machine fit the script?
# --------------------------------------------------------------------------- #
def port_in_use(port):
    try:
        s = socket.create_connection(("127.0.0.1", int(port)), timeout=0.4)
        s.close()
        return True
    except OSError:
        return False


def evaluate(script, system, deep=True):
    """Fill script.checks / script.verdict with the result of the system check."""
    checks = []
    blockers = 0
    warnings = 0

    families = [f.lower() for f in script.list_of("os")]
    if families and "any" not in families:
        if system.family in families:
            checks.append(("ok", f"{system.distro or system.family} is a supported system"))
        elif system.family == "unknown":
            checks.append(("warn", f"could not identify this system; the script targets "
                                   f"{', '.join(families)}"))
            warnings += 1
        else:
            checks.append(("bad", f"targets {', '.join(families)} — this is "
                                  f"{system.family}"))
            blockers += 1
    else:
        checks.append(("ok", "runs on any Linux system"))

    root = script.needs_root
    if root == "yes":
        if system.is_root:
            checks.append(("ok", "running as root"))
        elif system.can_sudo:
            checks.append(("ok", "needs root — will be run through sudo"))
        else:
            checks.append(("bad", "needs root, and sudo is not available here"))
            blockers += 1
    elif root == "optional":
        checks.append(("ok" if system.root_ok else "warn",
                       "root not required, but some checks need it"
                       if not system.root_ok else "root available for the parts that need it"))
        warnings += 0 if system.root_ok else 1

    missing = [c for c in script.list_of("needs") if not shutil.which(c)]
    present = [c for c in script.list_of("needs") if shutil.which(c)]
    if missing:
        checks.append(("bad", f"missing required command(s): {', '.join(missing)}"))
        blockers += 1
    elif present:
        checks.append(("ok", f"required commands present: {', '.join(present)}"))
    soft_missing = [c for c in script.list_of("optional") if not shutil.which(c)]
    if soft_missing:
        checks.append(("note", f"optional and not installed: {', '.join(soft_missing)}"
                               f" — the script works without "
                               f"{'them' if len(soft_missing) > 1 else 'it'}"))

    if deep and script.get("detect"):
        rc, out = _sh(script.get("detect"), timeout=6)
        if rc == 0:
            script.present = out or "already present"
            checks.append(("note", f"already installed: {script.present}"))
        else:
            script.present = ""

    for port in script.list_of("ports"):
        if port.isdigit() and port_in_use(port):
            checks.append(("warn", f"port {port} is already in use by something else"))
            warnings += 1

    if script.kind == "installer":
        if system.internet is False:
            checks.append(("bad", "no internet connection — packages cannot be downloaded"))
            blockers += 1
        elif system.internet:
            checks.append(("ok", "internet reachable for downloads"))

    if script.kind == "destructive":
        checks.append(("bad" if not system.root_ok else "warn",
                       script.get("danger") or "destroys data — read the docs first"))
        warnings += 1

    missing_required = [a.flag for a in script.args if a.required and not str(a.value).strip()]
    if missing_required:
        checks.append(("warn", f"set {', '.join(missing_required)} in Options before running"))
        warnings += 1

    script.checks = checks
    script.verdict = "blocked" if blockers else ("attention" if warnings else "ready")
    return script.verdict


def verdict_glyph(script):
    if script.present and script.verdict != "blocked":
        return "note", GLYPH["note"]
    return ({"ready": ("ok", GLYPH["ok"]),
             "attention": ("warn", GLYPH["warn"]),
             "blocked": ("bad", GLYPH["bad"])}.get(script.verdict, ("dot", GLYPH["dot"])))


# --------------------------------------------------------------------------- #
# screens
# --------------------------------------------------------------------------- #
INVOKE_CWD = os.getcwd()
SCREEN = Screen()


def footer(pairs):
    out = []
    for key, label in pairs:
        out.append(f"{WHITE}{key}{C0} {GREY}{label}{C0}")
    return "  " + f"{FAINT}·{C0} ".join(x + " " for x in out)


def render_browser(scripts, system, cursor, scroll, flt):
    w, h = term_size()
    w = min(w, 200)
    kinds = {}
    for s in scripts:
        kinds[s.kind] = kinds.get(s.kind, 0) + 1
    counts = "  ".join(f"{KIND_COLOR.get(k, GREY)}{n} {KIND_LABEL.get(k, k)}"
                       f"{'s' if n != 1 else ''}{C0}" for k, n in sorted(kinds.items()))
    head = [system.summary_line(),
            f"{GREY}{len(scripts)} script{'s' if len(scripts) != 1 else ''} discovered"
            f"{C0}   {counts}"
            + (f"   {YELLOW}filter: {flt}{C0}" if flt else "")]
    lines = panel(f"{BOLD}{APP} {VERSION}{C0}", head, w, accent=BLUE)

    body_h = max(8, h - len(lines) - 2)
    left_w = max(34, min(48, int(w * 0.42)))
    right_w = w - left_w

    rows = []                                   # (kind, payload)
    last_cat = None
    for i, s in enumerate(scripts):
        if s.category != last_cat:
            rows.append(("cat", s.category))
            last_cat = s.category
        rows.append(("script", i))
    sel_row = next((r for r, (t, p) in enumerate(rows) if t == "script" and p == cursor), 0)
    view = body_h - 2
    if sel_row < scroll:
        scroll = sel_row
    elif sel_row >= scroll + view:
        scroll = sel_row - view + 1
    scroll = max(0, min(scroll, max(0, len(rows) - view)))

    left = []
    for t, payload in rows[scroll:scroll + view]:
        if t == "cat":
            left.append(f"{FAINT}{payload.upper()}{C0}")
            continue
        s = scripts[payload]
        state, glyph = verdict_glyph(s)
        colour = STATUS_COLOR[state]
        label = s.name
        if payload == cursor:
            left.append(f"{SEL}{WHITE} {GLYPH['arrow']} {colour}{glyph}{WHITE} "
                        f"{fit(label, left_w - 11)}{C0}")
        else:
            left.append(f"   {colour}{glyph}{C0} {GREY}{clip(label, left_w - 11)}{C0}")
    if not left:
        left = [f"{GREY}nothing matches the filter{C0}"]

    right = []
    if scripts:
        s = scripts[cursor]
        inner = right_w - 4
        right.append(f"{KIND_COLOR.get(s.kind, GREY)}{KIND_LABEL.get(s.kind, s.kind)}{C0}"
                     f" {GREY}·{C0} {s.category}"
                     + (f"  {GREY}·{C0} {MAGENTA}{s.present}{C0}" if s.present else ""))
        right.append(f"{FAINT}{s.rel}{C0}")
        right.append("")
        for line in wrap(s.summary, inner):
            right.append(f"{WHITE}{line}{C0}")
        if s.detail_text:
            right.append("")
            for line in wrap(s.detail_text, inner)[:6]:
                right.append(f"{GREY}{line}{C0}")
        right.append("")
        right.append(f"{FAINT}{BOX['h'] * 3} system check {BOX['h'] * max(0, inner - 17)}{C0}")
        for state, text in s.checks:
            colour = STATUS_COLOR.get(state, GREY)
            body = wrap(text, inner - 3)
            right.append(f" {colour}{GLYPH[state]}{C0} {body[0] if body else ''}")
            for extra_line in body[1:]:
                right.append(f"   {GREY}{extra_line}{C0}")
        right.append("")
        verdicts = {
            "ready": f"{GREEN}{GLYPH['ok']} ready to run{C0}",
            "attention": f"{YELLOW}{GLYPH['warn']} runnable — read the notes above{C0}",
            "blocked": f"{RED}{GLYPH['bad']} cannot run on this machine{C0}",
        }
        right.append(verdicts.get(s.verdict, ""))
        extras = []
        if s.get("writes"):
            extras.append(f"writes {s.get('writes')}")
        if s.get("ports"):
            extras.append(f"opens ports {s.get('ports')}")
        if s.get("docs"):
            extras.append(f"docs {s.get('docs')}")
        if not s.has_meta:
            extras.append("no toolkit metadata — details were guessed from the file")
        for line in extras:
            for chunk in wrap(line, inner):
                right.append(f"{FAINT}{chunk}{C0}")
    else:
        right = [f"{GREY}No scripts found under {REPO}{C0}"]

    lines += side_by_side(panel("scripts", left, left_w, body_h),
                          panel(scripts[cursor].name if scripts else "details",
                                right, right_w, body_h,
                                accent=STATUS_COLOR.get(verdict_glyph(scripts[cursor])[0], GREY)
                                if scripts else GREY))
    lines.append(footer([("↑↓", "move"), ("⏎", "open"), ("p", "preview"),
                         ("d", "docs"), ("o", "options"), ("/", "filter"),
                         ("s", "system"), ("?", "help"), ("q", "quit")]))
    return lines, scroll


def prompt(label, default=""):
    """Leave the alt screen for a real readline prompt, then come back."""
    was = SCREEN.active
    SCREEN.leave()
    try:
        suffix = f" {GREY}[{default}]{C0}" if default else ""
        value = input(f"{BLUE}?{C0} {label}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        value = ""
    if was:
        SCREEN.enter()
    return value or default


def pause(message="Press Enter to continue…"):
    try:
        input(f"\n{GREY}{message}{C0}")
    except (EOFError, KeyboardInterrupt):
        pass


def confirm_screen(script, system, preview=False, extra=""):
    """The one screen that has to be right: what will happen, and can it."""
    w, _h = term_size()
    w = min(w, 110)
    inner = w - 4
    action = "Preview" if preview else ("Run" if script.kind != "installer" else "Install")
    lines = []
    lines.append("")
    for line in wrap(script.summary, inner - 2):
        lines.append(f"{WHITE}{line}{C0}")
    if script.detail_text:
        lines.append("")
        for line in wrap(script.detail_text, inner - 2)[:5]:
            lines.append(f"{GREY}{line}{C0}")
    lines.append("")
    lines.append(f"{BOLD}Command{C0}")
    for line in wrap(script.pretty_command(system, preview, extra), inner - 4, "  "):
        lines.append(f"{CYAN}{line}{C0}")
    facts = []
    if script.needs_root == "yes":
        facts.append("runs as root" + ("" if system.is_root else " (via sudo — it will ask)"))
    if preview:
        facts.append("dry run: prints every step and changes nothing")
    if script.get("writes"):
        facts.append(f"writes to {script.get('writes')}")
    if script.get("ports"):
        facts.append(f"opens firewall ports {script.get('ports')}")
    if script.present:
        facts.append(f"already present: {script.present}")
    if script.kind == "installer":
        facts.append("downloads packages from the internet")
    if facts:
        lines.append("")
        lines.append(f"{BOLD}What it will do{C0}")
        for f in facts:
            for i, chunk in enumerate(wrap(f, inner - 6)):
                lines.append(f"  {GREY}{'•' if i == 0 else ' '} {chunk}{C0}")
    lines.append("")
    lines.append(f"{BOLD}System check{C0}")
    for state, text in script.checks:
        colour = STATUS_COLOR.get(state, GREY)
        chunks = wrap(text, inner - 6)
        lines.append(f"  {colour}{GLYPH[state]}{C0} {chunks[0] if chunks else ''}")
        for chunk in chunks[1:]:
            lines.append(f"    {GREY}{chunk}{C0}")
    lines.append("")
    if script.verdict == "blocked":
        lines.append(f"{RED}{GLYPH['bad']} This machine does not meet the requirements.{C0}")
    elif script.kind == "destructive":
        lines.append(f"{RED}{BOLD}{GLYPH['warn']} DESTRUCTIVE — type the confirmation to continue.{C0}")
    elif script.verdict == "attention":
        lines.append(f"{YELLOW}{GLYPH['warn']} Ready, with the notes above.{C0}")
    else:
        lines.append(f"{GREEN}{GLYPH['ok']} Ready.{C0}")
    lines.append("")

    accent = {"ready": GREEN, "attention": YELLOW, "blocked": RED}.get(script.verdict, GREY)
    frame = panel(f"{BOLD}{action}: {script.name}{C0}", lines, w, accent=accent)
    keys = [("⏎", "start"), ("p", "toggle dry run"), ("o", "options"),
            ("d", "docs"), ("Esc", "back")]
    if script.verdict == "blocked":
        keys = [("f", "run anyway"), ("o", "options"), ("d", "docs"), ("Esc", "back")]
    frame.append(footer(keys))
    return frame


def options_screen(script, extra):
    """Edit the script's arguments the way its own metadata describes them."""
    cursor = 0
    while True:
        rows = [(a.flag, a.label, a.display()) for a in script.args]
        rows.append(("(extra)", "Any other arguments, passed through verbatim",
                     extra or "(none)"))
        rows.append(("(done)", "Back to the summary", ""))
        w, _h = term_size()
        w = min(w, 110)
        body = []
        for i, (flag, label, value) in enumerate(rows):
            marker = GLYPH["arrow"] if i == cursor else " "
            name = f"{flag:<16}"
            shown = f"{WHITE}{value}{C0}" if value not in ("(unset)", "(none)", "") \
                else f"{FAINT}{value}{C0}"
            line = f" {marker} {CYAN}{name}{C0} {shown}"
            if i == cursor:
                line = f"{SEL}{line}{C0}"
            body.append(line)
            body.append(f"     {FAINT}{label}{C0}")
        frame = panel(f"Options — {script.name}", body, w, accent=CYAN)
        frame.append(footer([("↑↓", "move"), ("⏎", "edit"), ("Esc", "back")]))
        SCREEN.paint(frame)
        key = KEYS.get(0.4)
        if key == "up":
            cursor = (cursor - 1) % len(rows)
        elif key == "down":
            cursor = (cursor + 1) % len(rows)
        elif key in ("esc", "q"):
            return extra
        elif key == "enter":
            if cursor == len(rows) - 1:
                return extra
            if cursor == len(rows) - 2:
                extra = prompt("Extra arguments", extra)
                continue
            arg = script.args[cursor]
            if arg.kind == "flag":
                on = str(arg.value).lower() in ("1", "true", "yes", "on")
                arg.value = "no" if on else "yes"
            else:
                arg.value = prompt(f"{arg.flag}  ({arg.label})", str(arg.value))


def pager(title, text):
    lines = text.splitlines() or ["(empty)"]
    top = 0
    while True:
        w, h = term_size()
        w = min(w, 120)
        view = max(5, h - 4)
        body = []
        for raw in lines[top:top + view]:
            body.append(f"{GREY}{raw}{C0}" if raw.startswith(("#", ">", "|")) else raw)
        frame = panel(f"{title}   {FAINT}{top + 1}-{min(top + view, len(lines))}"
                      f"/{len(lines)}{C0}", body, w, accent=MAGENTA)
        frame.append(footer([("↑↓", "scroll"), ("PgUp/PgDn", "page"), ("Esc", "back")]))
        SCREEN.paint(frame)
        key = KEYS.get(0.4)
        if key == "up":
            top = max(0, top - 1)
        elif key == "down":
            top = min(max(0, len(lines) - view), top + 1)
        elif key == "pgup":
            top = max(0, top - view)
        elif key == "pgdn":
            top = min(max(0, len(lines) - view), top + view)
        elif key == "home":
            top = 0
        elif key == "end":
            top = max(0, len(lines) - view)
        elif key in ("esc", "q", "enter"):
            return


def docs_screen(script):
    doc = script.get("docs")
    if doc:
        path = os.path.join(REPO, doc)
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    pager(doc, fh.read())
                    return
            except OSError:
                pass
    sibling = os.path.join(os.path.dirname(script.path), "README.md")
    if os.path.isfile(sibling):
        try:
            with open(sibling, encoding="utf-8", errors="replace") as fh:
                pager(os.path.relpath(sibling, REPO), fh.read())
                return
        except OSError:
            pass
    # No README: show the script's own header, which is documentation too.
    try:
        with open(script.path, encoding="utf-8", errors="replace") as fh:
            head = []
            for i, line in enumerate(fh):
                if i > 120:
                    break
                head.append(line.rstrip())
    except OSError:
        head = ["(cannot read the script)"]
    pager(f"{script.rel} — header", "\n".join(head))


def system_screen(system):
    w, _h = term_size()
    w = min(w, 100)
    body = [""]
    for label, value in system.details():
        body.append(f" {GREY}{label:<18}{C0}{WHITE}{value}{C0}")
    body.append("")
    body.append(f" {FAINT}The launcher uses these facts to decide whether each script"
                f" fits this machine.{C0}")
    body.append("")
    frame = panel("This machine", body, w, accent=BLUE)
    frame.append(footer([("Esc", "back")]))
    SCREEN.paint(frame)
    while True:
        key = KEYS.get(0.4)
        if key in ("esc", "q", "enter"):
            return


HELP_TEXT = f"""
 {BOLD}What this is{C0}

   A single entry point for every script in this repository. It reads each
   script's own header, checks whether this machine can run it, shows you what
   will happen, and starts it after one confirmation.

 {BOLD}The list{C0}

   {GREEN}{GLYPH['ok']}{C0}  ready — every requirement is met
   {YELLOW}{GLYPH['warn']}{C0}  runnable, but read the notes first
   {RED}{GLYPH['bad']}{C0}  blocked — this machine does not meet the requirements
   {BLUE}{GLYPH['note']}{C0}  already installed / already present on this machine

 {BOLD}Keys{C0}

   ↑ ↓        move between scripts
   ⏎          open the summary, then ⏎ again to start
   p          preview: run the script's own dry-run mode, changing nothing
   o          options: fill in the arguments a script asks for
   d          docs: the script's README or its header
   /          filter the list by name
   s          what this launcher detected about the machine
   r          re-scan the repository and re-run the checks
   q          quit

 {BOLD}Adding your own script{C0}

   Drop it anywhere in the repo — it appears here on the next scan. To control
   how it is presented, add comment lines to its header:

     # toolkit-name: Human readable name
     # toolkit-kind: installer | tool | destructive
     # toolkit-category: Containers
     # toolkit-summary: One line describing what it does.
     # toolkit-os: debian, fedora        (which systems it supports)
     # toolkit-root: yes | no | optional
     # toolkit-needs: curl, systemctl    (commands that must exist)
     # toolkit-detect: command -v docker (is it already installed?)
     # toolkit-preview: --dry-run        (arguments for a safe preview)
     # toolkit-run: --yes                (default arguments)
     # toolkit-arg: --domain | Domain to serve on | required
     # toolkit-ports: 80,443
     # toolkit-writes: /opt/thing
     # toolkit-docs: linux/thing/README.md
"""


def help_screen():
    pager("Help", HELP_TEXT)


def run_script(script, system, preview=False, extra="", pause_after=True):
    """Hand the terminal over to the script, then report how it went."""
    argv = script.argv(system, preview, extra)
    SCREEN.leave()
    w, _h = term_size()
    rule = BOX["h"] * min(w - 1, 100)
    print(f"\n{BLUE}{rule}{C0}")
    print(f" {BOLD}{'Preview' if preview else 'Running'}: {script.name}{C0}")
    print(f" {GREY}{script.pretty_command(system, preview, extra)}{C0}")
    print(f"{BLUE}{rule}{C0}\n", flush=True)
    started = time.monotonic()
    try:
        rc = subprocess.call(argv, cwd=INVOKE_CWD)
    except KeyboardInterrupt:
        rc = 130
    except FileNotFoundError as e:
        print(f"{RED}[x]{C0} cannot start: {e}")
        rc = 127
    took = time.monotonic() - started
    print(f"\n{BLUE}{rule}{C0}")
    if rc == 0:
        print(f" {GREEN}{GLYPH['ok']} finished successfully{C0} {GREY}in {took:.1f}s{C0}")
    elif rc == 130:
        print(f" {YELLOW}{GLYPH['warn']} interrupted{C0} {GREY}after {took:.1f}s{C0}")
    else:
        print(f" {RED}{GLYPH['bad']} exited with status {rc}{C0} {GREY}after {took:.1f}s{C0}")
    print(f"{BLUE}{rule}{C0}")
    if pause_after:
        pause("Press Enter to return to the launcher…")
    return rc


def item_screen(script, system):
    """Summary -> confirm -> run. This is the 'one click' path."""
    preview = False
    extra = ""
    while True:
        SCREEN.paint(confirm_screen(script, system, preview, extra))
        key = KEYS.get(0.4)
        if key in ("esc", "q"):
            return
        if key in ("p", "P"):
            preview = not preview
            continue
        if key in ("o", "O"):
            extra = options_screen(script, extra)
            evaluate(script, system, deep=False)
            continue
        if key in ("d", "D"):
            docs_screen(script)
            continue
        force = key in ("f", "F") and script.verdict == "blocked"
        if key == "enter" or force:
            if script.verdict == "blocked" and not force:
                continue
            missing = [a.flag for a in script.args
                       if a.required and not str(a.value).strip()]
            if missing:
                SCREEN.leave()
                warn(f"{', '.join(missing)} must be set first — opening Options.")
                time.sleep(1.2)
                SCREEN.enter()
                extra = options_screen(script, extra)
                evaluate(script, system, deep=False)
                continue
            if script.kind == "destructive" and not preview:
                word = script.get("confirm") or "PROCEED"
                answer = prompt(f"{RED}This is destructive.{C0} Type {BOLD}{word}{C0}"
                                f" to continue")
                if answer != word:
                    SCREEN.leave()
                    warn("Not confirmed — nothing was run.")
                    time.sleep(1.2)
                    SCREEN.enter()
                    continue
            run_script(script, system, preview, extra)
            evaluate(script, system, deep=True)
            SCREEN.enter()
            return


def browse(all_scripts, system):
    cursor, scroll, flt = 0, 0, ""
    scripts = all_scripts
    SCREEN.enter()
    try:
        while True:
            if flt:
                needle = flt.lower()
                scripts = [s for s in all_scripts
                           if needle in s.name.lower() or needle in s.rel.lower()
                           or needle in s.summary.lower() or needle in s.category.lower()]
            else:
                scripts = all_scripts
            cursor = max(0, min(cursor, len(scripts) - 1)) if scripts else 0
            frame, scroll = render_browser(scripts, system, cursor, scroll, flt)
            SCREEN.paint(frame)
            key = KEYS.get(0.4)
            if key is None:
                continue
            if key in ("q", "Q"):
                return 0
            if key == "esc":
                if flt:
                    flt = ""
                    continue
                return 0
            if not scripts:
                if key == "/":
                    flt = prompt("Filter", flt)
                continue
            if key == "up":
                cursor = (cursor - 1) % len(scripts)
            elif key == "down":
                cursor = (cursor + 1) % len(scripts)
            elif key == "pgup":
                cursor = max(0, cursor - 5)
            elif key == "pgdn":
                cursor = min(len(scripts) - 1, cursor + 5)
            elif key == "home":
                cursor = 0
            elif key == "end":
                cursor = len(scripts) - 1
            elif key in ("enter", "right"):
                item_screen(scripts[cursor], system)
            elif key in ("p", "P"):
                s = scripts[cursor]
                if s.get("preview"):
                    run_script(s, system, preview=True)
                    evaluate(s, system, deep=True)
                    SCREEN.enter()
                else:
                    SCREEN.leave()
                    warn(f"{s.name} has no preview mode.")
                    time.sleep(1.2)
                    SCREEN.enter()
            elif key in ("o", "O"):
                options_screen(scripts[cursor], "")
                evaluate(scripts[cursor], system, deep=False)
            elif key in ("d", "D"):
                docs_screen(scripts[cursor])
            elif key in ("s", "S"):
                system_screen(system)
            elif key in ("?", "h", "H"):
                help_screen()
            elif key in ("r", "R"):
                SCREEN.leave()
                info("Re-scanning the repository…")
                all_scripts = discover()
                system.check_internet()
                for s in all_scripts:
                    evaluate(s, system)
                SCREEN.enter()
            elif key == "/":
                flt = prompt("Filter (empty to clear)", "")
    finally:
        SCREEN.leave()


# --------------------------------------------------------------------------- #
# non-interactive entry points
# --------------------------------------------------------------------------- #
def print_list(scripts, system):
    print()
    print(f" {BOLD}{APP} {VERSION}{C0} {GREY}·{C0} {system.summary_line()}")
    print()
    last = None
    for s in scripts:
        if s.category != last:
            print(f" {FAINT}{s.category.upper()}{C0}")
            last = s.category
        state, glyph = verdict_glyph(s)
        colour = STATUS_COLOR[state]
        print(f"   {colour}{glyph}{C0} {WHITE}{fit(ellipsis(s.name, 34), 34)}{C0} "
              f"{KIND_COLOR.get(s.kind, GREY)}{s.kind:<12}{C0}{GREY}{s.rel}{C0}")
        print(f"     {GREY}{ellipsis(s.summary, 94)}{C0}")
    print()
    print(f" {GREY}run one with:{C0} ./toolkit.sh --run <name>   "
          f"{GREY}or just{C0} ./toolkit.sh")
    print()


def print_checks(scripts, system):
    print()
    for s in scripts:
        state, glyph = verdict_glyph(s)
        colour = STATUS_COLOR[state]
        print(f" {colour}{glyph}{C0} {BOLD}{s.name}{C0} {GREY}({s.rel}){C0}")
        for st, text in s.checks:
            print(f"     {STATUS_COLOR.get(st, GREY)}{GLYPH[st]}{C0} {text}")
        print()


def find_script(scripts, needle):
    needle = needle.lower()
    exact = [s for s in scripts if s.id == needle
             or os.path.basename(s.path).lower() == needle
             or s.rel.lower() == needle]
    if exact:
        return exact[0]
    partial = [s for s in scripts if needle in s.id or needle in s.name.lower()]
    return partial[0] if len(partial) == 1 else None


def main(argv):
    global KEYS
    if any(a in ("-h", "--help", "help") for a in argv):
        print(__doc__.strip())
        return 0
    if "--version" in argv:
        print(f"{APP} {VERSION}")
        return 0

    system = System()
    system.check_internet()
    scripts = discover()
    if not scripts:
        die(f"No scripts found under {REPO}")
    deep = "--no-detect" not in argv
    for s in scripts:
        evaluate(s, system, deep=deep)

    if "--list" in argv or "-l" in argv:
        print_list(scripts, system)
        return 0
    if "--check" in argv:
        print_checks(scripts, system)
        return 0
    if "--run" in argv:
        idx = argv.index("--run")
        if idx + 1 >= len(argv):
            die("--run needs a script name (see --list)")
        target = find_script(scripts, argv[idx + 1])
        if not target:
            die(f"No single script matches '{argv[idx + 1]}' — see --list")
        rest = argv[idx + 2:]
        if target.verdict == "blocked" and "--force" not in rest:
            for state, text in target.checks:
                if state == "bad":
                    warn(text)
            die("This machine does not meet the requirements (add --force to override)")
        return run_script(target, system, preview="--preview" in rest,
                          extra=" ".join(x for x in rest
                                         if x not in ("--force", "--preview")),
                          pause_after=False)

    if not sys.stdin.isatty() or not _TTY:
        print_list(scripts, system)
        warn("Not a terminal — showing the list instead of the interactive browser.")
        return 0

    with KeyReader() as keys:
        KEYS = keys
        return browse(scripts, system)


KEYS = None

if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        SCREEN.leave()
        print()
        warn("Interrupted.")
        sys.exit(130)
