#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
netwatch.py — continuous internet quality monitor with a diagnostic Markdown report.

Purpose
    Watch a connection for a configurable period, storing every measurement in a
    local SQLite database (not in RAM), then analyse the capture and write a
    self-contained Markdown report with SVG charts, per-layer statistics and a
    verdict that says *where* the problem actually is: your Wi-Fi/cable, the
    router, the ISP access network, upstream peering, DNS, or the far end.

What it measures
    ping        ICMP echo to the gateway, the ISP's first hop and several public
                anchors, once per second, with reply TTL — catches sub-second
                dropouts, jitter and route changes.
    wan         Public IP over DNS (single UDP packet, high frequency) to catch
                short multi-WAN / load-balancer failovers, with ASN lookup so
                each uplink is labelled with its real provider.
    dns         UDP/TCP/DoH queries against several resolvers, plus NXDOMAIN
                hijack detection.
    http        Phase-timed HTTPS requests (DNS / TCP / TLS / TTFB / total),
                captive-portal detection, TLS version and certificate expiry.
    speed       Multi-stream download + upload throughput, and the latency
                measured *under load* — i.e. a real bufferbloat grade.
    path        traceroute snapshots, path-change detection, path MTU probing,
                TCP port reachability, NTP/UDP reachability.
    link        Local interface counters (errors, drops, carrier) and Wi-Fi
                signal strength, so weak Wi-Fi can be correlated with the drops.

Usage
    Interactive TUI menu (no arguments):
        ./netwatch.sh

    Non-interactive (arguments may use --k v, --k=v or --k:v forms):
        ./netwatch.sh --duration 2h --out ./reports --plan 100 --yes
        ./netwatch.sh --quick                      one-shot 60 s diagnostic
        ./netwatch.sh --analyze run-dir-or.db      rebuild a report from a capture

Options
    --duration, --time    how long to monitor: 90, 90s, 30m, 2h, 1d (0 = until Ctrl-C)
    --interval            ICMP probe interval in seconds            (default 1.0)
    --wan-interval        public-IP / failover probe interval        (default 2.0)
    --dns-interval        DNS probe interval                         (default 30)
    --http-interval       HTTP probe interval                        (default 30)
    --speed-interval      seconds between speed tests, 0 = off       (default 900)
    --trace-interval      seconds between traceroutes, 0 = off       (default 1800)
    --link-interval       local interface sampling interval          (default 10)
    --targets             extra ping targets, comma separated (host or name=host)
    --urls                extra HTTP endpoints to probe, comma separated
    --resolvers           DNS resolvers to test, comma separated
    --plan                your subscribed speed in Mbps, for the verdict
    --speed-max-mb        hard cap of MB per speed test              (default 200)
    --no-speed            disable speed tests (metered / capped links)
    --no-trace            disable traceroute snapshots
    --no-ipv6             skip all IPv6 checks
    --out, --output       output directory (default ./netwatch-<timestamp>)
    --db                  reuse/append to an explicit SQLite file
    --label               a short name for this run (shown in the report)
    --analyze <path>      analyse an existing run directory or .db and exit
    --quick               one-shot 60 s diagnostic with an immediate report
    --no-tui, --plain     no live dashboard, plain log lines (cron/CI friendly)
    --yes                 start immediately, skip the confirmation
    --help, -h            show this help
"""

from __future__ import annotations

import collections
import html
import http.client
import json
import math
import os
import platform
import queue
import random
import re
import select
import shutil
import signal
import socket
import sqlite3
import ssl
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

APP = "netwatch"
VERSION = "1.0"
DB_SCHEMA = 1
UA = f"{APP}/{VERSION} (+network-diagnostics)"

# --------------------------------------------------------------------------- #
# defaults
# --------------------------------------------------------------------------- #
# Public anchors kept deliberately diverse: three different operators on three
# different networks, so a loss pattern common to all of them is ours, not theirs.
DEFAULT_ANCHORS = [
    ("google-dns", "8.8.8.8"),
    ("cloudflare", "1.1.1.1"),
    ("quad9", "9.9.9.9"),
]

DEFAULT_RESOLVERS = [
    ("system", None),
    ("google", "8.8.8.8"),
    ("cloudflare", "1.1.1.1"),
    ("quad9", "9.9.9.9"),
]

DEFAULT_DOMAINS = [
    "google.com", "cloudflare.com", "github.com", "wikipedia.org", "youtube.com",
]

DEFAULT_URLS = [
    "https://www.google.com/generate_204",
    "https://cloudflare.com/cdn-cgi/trace",
    "https://www.gstatic.com/generate_204",
    "http://detectportal.firefox.com/success.txt",
]

DEFAULT_PORTS = [
    ("1.1.1.1", 53), ("8.8.8.8", 53), ("github.com", 22),
    ("github.com", 443), ("example.com", 80), ("smtp.gmail.com", 587),
]

DEFAULT_NTP = ["time.cloudflare.com", "pool.ntp.org"]

SPEED_DOWN_URL = "https://speed.cloudflare.com/__down?bytes={bytes}"
SPEED_UP_URL = "https://speed.cloudflare.com/__up"
SPEED_FALLBACK_DOWN = "https://proof.ovh.net/files/100Mb.dat"

# Grade thresholds for the composite stability score.
GRADES = [(97, "A+"), (93, "A"), (88, "B+"), (82, "B"), (75, "C+"),
          (68, "C"), (58, "D"), (45, "E"), (0, "F")]


# --------------------------------------------------------------------------- #
# terminal helpers
# --------------------------------------------------------------------------- #
def _enable_vt():
    """Turn on ANSI escape processing on Windows consoles; no-op elsewhere."""
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

try:  # box drawing needs a UTF-8 capable stdout
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
WHITE = _c("\033[38;5;255m")
BG_SEL = _c("\033[48;5;24m")

SEV_COLOR = {"critical": RED, "warning": YELLOW, "info": BLUE, "ok": GREEN}
SEV_ICON = {"critical": "🔴", "warning": "🟠", "info": "🔵", "ok": "🟢"}


def info(msg):
    print(f"{BLUE}[*]{C0} {msg}", flush=True)


def ok(msg):
    print(f"{GREEN}[+]{C0} {msg}", flush=True)


def warn(msg):
    print(f"{YELLOW}[!]{C0} {msg}", file=sys.stderr, flush=True)


def die(msg, code=1):
    print(f"{RED}[x]{C0} {msg}", file=sys.stderr, flush=True)
    sys.exit(code)


def term_size():
    try:
        s = shutil.get_terminal_size((100, 30))
        return max(60, min(s.columns, 200)), max(16, s.lines)
    except Exception:
        return 100, 30


def visible_len(s):
    return len(re.sub(r"\033\[[0-9;]*m", "", s))


def pad(s, width):
    n = visible_len(s)
    return s + " " * max(0, width - n)


def clip(s, width):
    """Truncate honouring ANSI sequences (they cost no visible columns)."""
    if visible_len(s) <= width:
        return s
    out, seen = [], 0
    i = 0
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


CURSOR_KEYS = {b"A": "up", b"B": "down", b"C": "right", b"D": "left"}


class KeyReader:
    """Non-blocking single-key reader: POSIX termios / Windows msvcrt."""

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

    def get(self, timeout=0.2):
        """Return 'up'/'down'/'left'/'right'/'enter'/'esc' or a single char."""
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
                        ch2 = msvcrt.getwch()
                        return {"H": "up", "P": "down", "K": "left", "M": "right"}.get(ch2)
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
            # Only part of an escape sequence arrived; give the rest a moment
            # before concluding that the user pressed a bare Escape.
            self._buf += self._read(0.05)
            return self._pop(final=True)
        return None

    def _read(self, timeout):
        """Read straight from the descriptor.

        sys.stdin is a buffered text stream: one read(1) can pull a whole escape
        sequence into Python's buffer while select() still reports the descriptor
        as empty, so the rest of the sequence becomes invisible. Reading the fd
        directly keeps select() and the bytes in agreement.
        """
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
        """Take one keypress off the pending bytes, or None if none is complete.

        Held-down keys and fast typing deliver several sequences in a single read,
        so whatever is left over stays buffered for the next call instead of being
        dropped. `final` means no more bytes are coming: a lone ESC really is the
        Escape key by then.
        """
        while self._buf:
            buf = self._buf
            if buf[:1] != b"\x1b":
                ch, self._buf = buf[:1], buf[1:]
                if ch == b"\x03":
                    raise KeyboardInterrupt
                if ch in (b"\r", b"\n"):
                    return "enter"
                decoded = ch.decode("utf-8", "ignore")
                if decoded:
                    return decoded
                continue                      # part of a multi-byte character
            if len(buf) == 1:
                if not final:
                    return None
                self._buf = b""
                return "esc"
            if buf[1:2] == b"O":              # SS3: ESC O A, application cursor mode
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
                self._buf = buf[1:]           # ESC + a plain key = Escape
                return "esc"
            # CSI: parameter bytes, then intermediates, then one final byte.
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
            key, self._buf = CURSOR_KEYS.get(buf[i:i + 1]), buf[i + 1:]
            if key:
                return key
        return None


class Screen:
    """Flicker-free full-screen repaint using absolute cursor positioning."""

    def __init__(self):
        self.active = False

    def enter(self):
        if _TTY:
            sys.stdout.write("\033[?25l\033[2J")
            sys.stdout.flush()
            self.active = True

    def leave(self):
        if self.active:
            sys.stdout.write("\033[?25h\033[0m\n")
            sys.stdout.flush()
            self.active = False

    def paint(self, lines):
        if not _TTY:
            return
        w, h = term_size()
        out = ["\033[H"]
        for i, line in enumerate(lines[:h - 1]):
            out.append(clip(line, w - 1) + "\033[K")
            if i < min(len(lines), h - 1) - 1:
                out.append("\r\n")
        out.append("\033[J")
        sys.stdout.write("".join(out))
        sys.stdout.flush()


SPARK = "▁▂▃▄▅▆▇█" if UNI else ".:-=+*#@"
BOX = {"tl": "╭", "tr": "╮", "bl": "╰", "br": "╯", "h": "─", "v": "│",
       "lt": "├", "rt": "┤"} if UNI else {
    "tl": "+", "tr": "+", "bl": "+", "br": "+", "h": "-", "v": "|",
    "lt": "+", "rt": "+"}


def sparkline(values, width=None, lo=None, hi=None):
    """Unicode sparkline; None values render as a gap marker."""
    vals = list(values)
    if width and len(vals) > width:
        step = len(vals) / width
        vals = [vals[int(i * step)] for i in range(width)]
    good = [v for v in vals if v is not None]
    if not good:
        return "×" * len(vals) if UNI else "x" * len(vals)
    lo = min(good) if lo is None else lo
    hi = max(good) if hi is None else hi
    span = (hi - lo) or 1.0
    out = []
    for v in vals:
        if v is None:
            out.append("×" if UNI else "x")
            continue
        idx = int(round((v - lo) / span * (len(SPARK) - 1)))
        out.append(SPARK[max(0, min(len(SPARK) - 1, idx))])
    return "".join(out)


def bar(fraction, width, fill="█", empty="░"):
    if not UNI:
        fill, empty = "#", "."
    n = int(round(max(0.0, min(1.0, fraction)) * width))
    return fill * n + empty * (width - n)


def box(title, lines, width):
    """Draw a titled box around already-coloured content lines."""
    inner = width - 2
    head = f"{BOX['tl']}{BOX['h']} {title} " if title else f"{BOX['tl']}{BOX['h']}"
    head += BOX["h"] * max(0, width - visible_len(head) - 1) + BOX["tr"]
    out = [f"{GREY}{head}{C0}"]
    for ln in lines:
        out.append(f"{GREY}{BOX['v']}{C0} " + pad(clip(ln, inner - 2), inner - 2) + f" {GREY}{BOX['v']}{C0}")
    out.append(f"{GREY}{BOX['bl']}{BOX['h'] * (width - 2)}{BOX['br']}{C0}")
    return out


# --------------------------------------------------------------------------- #
# small utilities
# --------------------------------------------------------------------------- #
def now_ts():
    return time.time()


def parse_duration(s, default=0.0):
    """'90' '90s' '30m' '2h' '1d' -> seconds. '0'/'' -> 0 (unlimited)."""
    if s is None:
        return default
    s = str(s).strip().lower().replace(" ", "")
    if not s:
        return default
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([smhd]?)", s)
    if not m:
        # allow composite forms such as 1h30m
        total, found = 0.0, False
        for num, unit in re.findall(r"(\d+(?:\.\d+)?)([smhd])", s):
            total += float(num) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
            found = True
        if found:
            return total
        raise ValueError(f"cannot parse duration: {s!r}")
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def fmt_hms(sec):
    sec = int(max(0, sec or 0))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h >= 24:
        d, h = divmod(h, 24)
        return f"{d}d {h}h {m:02d}m"
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def fmt_dur(sec):
    """Human duration for report prose (2.4 s / 1 m 12 s / 3 h 4 m)."""
    sec = float(sec or 0)
    if sec < 1:
        return f"{sec * 1000:.0f} ms"
    if sec < 60:
        return f"{sec:.1f} s"
    if sec < 3600:
        return f"{int(sec // 60)} m {int(sec % 60)} s"
    return f"{int(sec // 3600)} h {int((sec % 3600) // 60)} m"


def fmt_bytes(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def fmt_ms(v, nd=1):
    return "—" if v is None else f"{v:.{nd}f}"


def fmt_pct(v, nd=2):
    return "—" if v is None else f"{v:.{nd}f}%"


def ts_str(ts, with_date=True):
    if not ts:
        return "—"
    dt = datetime.fromtimestamp(ts)
    return dt.strftime("%Y-%m-%d %H:%M:%S" if with_date else "%H:%M:%S")


def percentile(sorted_vals, p):
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    k = (len(sorted_vals) - 1) * (p / 100.0)
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return float(sorted_vals[int(k)])
    return float(sorted_vals[lo]) * (hi - k) + float(sorted_vals[hi]) * (k - lo)


def mean(vals):
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def stdev(vals):
    vals = [v for v in vals if v is not None]
    if len(vals) < 2:
        return None
    m = sum(vals) / len(vals)
    return math.sqrt(sum((v - m) ** 2 for v in vals) / (len(vals) - 1))


def grade_for(score):
    for threshold, letter in GRADES:
        if score >= threshold:
            return letter
    return "F"


def safe_name(s):
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(s)).strip("-") or "x"


def _octets(ip):
    try:
        parts = [int(x) for x in ip.split(".")]
    except Exception:
        return None
    return parts if len(parts) == 4 else None


def is_private_ip(ip):
    """RFC1918 / loopback / link-local — an address inside your own network."""
    parts = _octets(ip)
    if not parts:
        return False
    a, b = parts[0], parts[1]
    return (a == 10 or a == 127 or (a == 172 and 16 <= b <= 31)
            or (a == 192 and b == 168) or (a == 169 and b == 254))


def is_cgnat_ip(ip):
    """100.64/10 — carrier-grade NAT, i.e. already the provider's equipment."""
    parts = _octets(ip)
    return bool(parts and parts[0] == 100 and 64 <= parts[1] <= 127)


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #
class Config:
    def __init__(self):
        self.label = ""
        self.duration = 1800.0            # 0 = until stopped
        self.ping_interval = 1.0
        self.ping_timeout = 2.0
        self.wan_interval = 2.0           # public-IP / failover watch
        self.dns_interval = 30.0
        self.http_interval = 30.0
        self.speed_interval = 900.0       # 0 = off
        self.trace_interval = 1800.0      # 0 = off
        self.link_interval = 10.0
        self.port_interval = 900.0
        self.ntp_interval = 300.0

        self.extra_targets = []           # [(name, host)]
        self.urls = list(DEFAULT_URLS)
        self.resolvers = list(DEFAULT_RESOLVERS)
        self.domains = list(DEFAULT_DOMAINS)
        self.ports = list(DEFAULT_PORTS)
        self.ntp_servers = list(DEFAULT_NTP)

        self.speed_seconds = 12.0         # per direction
        self.speed_streams = 4
        self.speed_max_mb = 200.0
        self.speed_upload = True
        self.plan_mbps = 0.0              # subscribed speed, for the verdict

        self.ipv6 = True
        self.mtu_probe = True
        self.out_dir = ""
        self.db_path = ""
        self.tui = True
        self.assume_yes = False
        self.outage_ticks = 2             # consecutive failed ticks = outage

    def to_json(self):
        return {k: v for k, v in self.__dict__.items() if not k.startswith("_")}

    @classmethod
    def from_json(cls, d):
        cfg = cls()
        for k, v in (d or {}).items():
            if hasattr(cfg, k):
                setattr(cfg, k, v)
        return cfg


# --------------------------------------------------------------------------- #
# host / network discovery
# --------------------------------------------------------------------------- #
def run_cmd(args, timeout=8):
    """Run a command, return (rc, stdout+stderr). Never raises."""
    try:
        p = subprocess.run(args, capture_output=True, timeout=timeout,
                           text=True, errors="replace")
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except FileNotFoundError:
        return 127, ""
    except subprocess.TimeoutExpired:
        return 124, ""
    except Exception as e:
        return 1, str(e)


def have(binary):
    return shutil.which(binary) is not None


def default_gateway():
    """Return (gateway_ip, interface) for the IPv4 default route."""
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/net/route", "r") as fh:
                for line in fh.readlines()[1:]:
                    f = line.split()
                    if len(f) > 2 and f[1] == "00000000":
                        gw = socket.inet_ntoa(struct.pack("<L", int(f[2], 16)))
                        return gw, f[0]
        except Exception:
            pass
    if os.name == "nt":
        rc, out = run_cmd(["route", "print", "-4", "0.0.0.0"])
        if rc == 0:
            for line in out.splitlines():
                f = line.split()
                if len(f) >= 5 and f[0] == "0.0.0.0" and f[1] == "0.0.0.0":
                    return f[2], f[3]
        return None, None
    rc, out = run_cmd(["ip", "route", "show", "default"])
    if rc == 0 and out.strip():
        m = re.search(r"default via (\S+).*?dev (\S+)", out)
        if m:
            return m.group(1), m.group(2)
    rc, out = run_cmd(["route", "-n", "get", "default"])
    if rc == 0:
        gw = re.search(r"gateway:\s*(\S+)", out)
        dev = re.search(r"interface:\s*(\S+)", out)
        return (gw.group(1) if gw else None), (dev.group(1) if dev else None)
    return None, None


def local_ip_for(host="1.1.1.1"):
    """Source address the kernel would pick for that destination (no traffic sent)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect((host, 53))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


def local_ip6():
    """Global IPv6 source address, or None when the host has no usable IPv6."""
    try:
        s = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(("2606:4700:4700::1111", 53))
        ip = s.getsockname()[0]
        s.close()
        return None if ip.startswith(("fe80", "::1", "::")) else ip
    except Exception:
        return None


def detect_container():
    if os.path.exists("/.dockerenv"):
        return "docker"
    try:
        with open("/proc/1/cgroup") as fh:
            body = fh.read()
        for marker in ("docker", "containerd", "lxc", "kubepods"):
            if marker in body:
                return marker
    except Exception:
        pass
    if "microsoft" in platform.release().lower():
        return "wsl"
    return ""


def host_info():
    d = {
        "hostname": socket.gethostname(),
        "os": f"{platform.system()} {platform.release()}",
        "platform": platform.platform(),
        "python": platform.python_version(),
        "machine": platform.machine(),
        "container": detect_container(),
        "tz": time.strftime("%Z %z"),
    }
    if sys.platform.startswith("linux"):
        try:
            with open("/etc/os-release") as fh:
                kv = dict(re.findall(r'^(\w+)=("?)(.*?)\2$', fh.read(), re.M))
            d["distro"] = kv.get("PRETTY_NAME", "")
        except Exception:
            pass
    return d


def wifi_info(iface):
    """Return (signal_dbm, quality, essid) for a Wi-Fi interface."""
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/net/wireless") as fh:
                for line in fh.readlines()[2:]:
                    name, _, rest = line.partition(":")
                    if name.strip() != iface:
                        continue
                    f = rest.split()
                    qual = float(f[1].rstrip("."))
                    dbm = float(f[2].rstrip("."))
                    return dbm, qual, ""
        except Exception:
            pass
        rc, out = run_cmd(["iwgetid", "-r"], timeout=3)
        if rc == 0 and out.strip():
            return None, None, out.strip()
        return None, None, ""
    if os.name == "nt":
        rc, out = run_cmd(["netsh", "wlan", "show", "interfaces"], timeout=6)
        if rc == 0:
            sig = re.search(r"Signal\s*:\s*(\d+)%", out)
            ssid = re.search(r"^\s*SSID\s*:\s*(.+)$", out, re.M)
            if sig:
                pct = float(sig.group(1))
                # Microsoft's percentage maps linearly onto -100..-50 dBm.
                return -100 + pct / 2.0, pct, (ssid.group(1).strip() if ssid else "")
    return None, None, ""


def iface_counters(iface):
    """Raw interface counters; returns a dict or None."""
    if not iface:
        return None
    if sys.platform.startswith("linux"):
        base = f"/sys/class/net/{iface}/statistics"
        if not os.path.isdir(base):
            return None
        out = {}
        for key in ("rx_bytes", "tx_bytes", "rx_errors", "tx_errors",
                    "rx_dropped", "tx_dropped"):
            try:
                with open(f"{base}/{key}") as fh:
                    out[key] = int(fh.read().strip())
            except Exception:
                out[key] = 0
        for key, path in (("carrier", "carrier"), ("speed", "speed")):
            try:
                with open(f"/sys/class/net/{iface}/{path}") as fh:
                    out[key] = int(fh.read().strip())
            except Exception:
                out[key] = None
        return out
    return None


# --------------------------------------------------------------------------- #
# storage — one writer thread, batched transactions, nothing kept in RAM
# --------------------------------------------------------------------------- #
SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS runs(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at REAL, ended_at REAL, label TEXT, status TEXT,
    config_json TEXT, host_json TEXT, net_json TEXT);
CREATE TABLE IF NOT EXISTS ping_samples(
    run_id INTEGER, ts REAL, target TEXT, ok INTEGER, rtt_ms REAL,
    ttl INTEGER, seq INTEGER, err TEXT);
CREATE INDEX IF NOT EXISTS ix_ping ON ping_samples(run_id, ts);
CREATE INDEX IF NOT EXISTS ix_ping_t ON ping_samples(run_id, target, ts);
CREATE TABLE IF NOT EXISTS wan_samples(
    run_id INTEGER, ts REAL, method TEXT, ip TEXT, ok INTEGER, rtt_ms REAL, err TEXT);
CREATE INDEX IF NOT EXISTS ix_wan ON wan_samples(run_id, ts);
CREATE TABLE IF NOT EXISTS wan_ips(
    run_id INTEGER, ip TEXT, first_seen REAL, last_seen REAL, samples INTEGER,
    asn TEXT, as_name TEXT, cc TEXT, label TEXT,
    PRIMARY KEY(run_id, ip));
CREATE TABLE IF NOT EXISTS dns_samples(
    run_id INTEGER, ts REAL, resolver TEXT, server TEXT, proto TEXT, domain TEXT,
    ok INTEGER, rtt_ms REAL, rcode TEXT, answer TEXT, err TEXT);
CREATE INDEX IF NOT EXISTS ix_dns ON dns_samples(run_id, ts);
CREATE TABLE IF NOT EXISTS http_samples(
    run_id INTEGER, ts REAL, url TEXT, ok INTEGER, status INTEGER,
    dns_ms REAL, tcp_ms REAL, tls_ms REAL, ttfb_ms REAL, total_ms REAL,
    bytes INTEGER, tls_ver TEXT, cert_days INTEGER, err TEXT);
CREATE INDEX IF NOT EXISTS ix_http ON http_samples(run_id, ts);
CREATE TABLE IF NOT EXISTS speed_tests(
    run_id INTEGER, ts_start REAL, ts_end REAL, direction TEXT, bytes INTEGER,
    seconds REAL, mbps REAL, streams INTEGER, server TEXT, err TEXT);
CREATE TABLE IF NOT EXISTS speed_series(
    run_id INTEGER, ts REAL, direction TEXT, mbps REAL);
CREATE TABLE IF NOT EXISTS trace_hops(
    run_id INTEGER, ts REAL, target TEXT, hop INTEGER, ip TEXT, rtt_ms REAL);
CREATE TABLE IF NOT EXISTS iface_samples(
    run_id INTEGER, ts REAL, iface TEXT, rx_mbps REAL, tx_mbps REAL,
    rx_err INTEGER, tx_err INTEGER, rx_drop INTEGER, tx_drop INTEGER,
    wifi_dbm REAL, wifi_qual REAL, carrier INTEGER, link_mbps REAL);
CREATE INDEX IF NOT EXISTS ix_iface ON iface_samples(run_id, ts);
CREATE TABLE IF NOT EXISTS port_checks(
    run_id INTEGER, ts REAL, host TEXT, port INTEGER, ok INTEGER, ms REAL, err TEXT);
CREATE TABLE IF NOT EXISTS ntp_samples(
    run_id INTEGER, ts REAL, server TEXT, ok INTEGER, offset_ms REAL,
    rtt_ms REAL, err TEXT);
CREATE TABLE IF NOT EXISTS events(
    run_id INTEGER, ts REAL, kind TEXT, severity TEXT, message TEXT, details TEXT);
CREATE INDEX IF NOT EXISTS ix_events ON events(run_id, ts);
CREATE TABLE IF NOT EXISTS phases(
    run_id INTEGER, ts_start REAL, ts_end REAL, kind TEXT);
"""

INSERTS = {
    "ping_samples": "INSERT INTO ping_samples VALUES (?,?,?,?,?,?,?,?)",
    "wan_samples": "INSERT INTO wan_samples VALUES (?,?,?,?,?,?,?)",
    "dns_samples": "INSERT INTO dns_samples VALUES (?,?,?,?,?,?,?,?,?,?,?)",
    "http_samples": "INSERT INTO http_samples VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
    "speed_tests": "INSERT INTO speed_tests VALUES (?,?,?,?,?,?,?,?,?,?)",
    "speed_series": "INSERT INTO speed_series VALUES (?,?,?,?)",
    "trace_hops": "INSERT INTO trace_hops VALUES (?,?,?,?,?,?)",
    "iface_samples": "INSERT INTO iface_samples VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
    "port_checks": "INSERT INTO port_checks VALUES (?,?,?,?,?,?,?)",
    "ntp_samples": "INSERT INTO ntp_samples VALUES (?,?,?,?,?,?,?)",
    "events": "INSERT INTO events VALUES (?,?,?,?,?,?)",
    "phases": "INSERT INTO phases VALUES (?,?,?,?)",
}


class Storage:
    """Async SQLite writer. Probe threads only ever touch the queue."""

    def __init__(self, path):
        self.path = path
        self.run_id = None
        self._q = queue.Queue(maxsize=200000)
        self._stop = threading.Event()
        self._thread = None
        self.written = 0
        self.dropped = 0
        self.error = None

    def open(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.path)) or ".", exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.executescript(SCHEMA)
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('schema', ?)", (str(DB_SCHEMA),))
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('app', ?)", (f"{APP} {VERSION}",))
        conn.commit()
        conn.close()

    def start_run(self, cfg, host, net):
        conn = sqlite3.connect(self.path)
        cur = conn.execute(
            "INSERT INTO runs(started_at, ended_at, label, status, config_json,"
            " host_json, net_json) VALUES (?,?,?,?,?,?,?)",
            (now_ts(), None, cfg.label, "running", json.dumps(cfg.to_json()),
             json.dumps(host), json.dumps(net)))
        self.run_id = cur.lastrowid
        conn.commit()
        conn.close()
        self._thread = threading.Thread(target=self._writer, name="db-writer", daemon=True)
        self._thread.start()
        return self.run_id

    def put(self, table, row):
        """Queue one row; never blocks a probe thread for long."""
        try:
            self._q.put_nowait((table, row))
        except queue.Full:
            self.dropped += 1

    def _writer(self):
        conn = sqlite3.connect(self.path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        pending = []
        last_commit = time.monotonic()
        try:
            while not (self._stop.is_set() and self._q.empty()):
                try:
                    pending.append(self._q.get(timeout=0.25))
                except queue.Empty:
                    pass
                if pending and (len(pending) >= 400
                                or time.monotonic() - last_commit >= 2.0
                                or (self._stop.is_set() and self._q.empty())):
                    by_table = {}
                    for table, row in pending:
                        by_table.setdefault(table, []).append(row)
                    try:
                        for table, rows in by_table.items():
                            conn.executemany(INSERTS[table], rows)
                        conn.commit()
                        self.written += len(pending)
                    except Exception as e:      # keep monitoring even if a write fails
                        self.error = str(e)
                    pending = []
                    last_commit = time.monotonic()
        finally:
            try:
                conn.commit()
            except Exception:
                pass
            conn.close()

    def finish(self, status="finished"):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=20)
        try:
            conn = sqlite3.connect(self.path)
            conn.execute("UPDATE runs SET ended_at=?, status=? WHERE id=?",
                         (now_ts(), status, self.run_id))
            conn.commit()
            conn.close()
        except Exception:
            pass

    def upsert_wan_ip(self, ip, ts, asn=None, as_name=None, cc=None, label=None):
        """Small, rare write — done inline, outside the queue."""
        try:
            conn = sqlite3.connect(self.path, timeout=10)
            conn.execute(
                "INSERT INTO wan_ips(run_id, ip, first_seen, last_seen, samples,"
                " asn, as_name, cc, label) VALUES (?,?,?,?,1,?,?,?,?)"
                " ON CONFLICT(run_id, ip) DO UPDATE SET last_seen=excluded.last_seen,"
                " samples=samples+1,"
                " asn=COALESCE(excluded.asn, asn),"
                " as_name=COALESCE(excluded.as_name, as_name),"
                " cc=COALESCE(excluded.cc, cc),"
                " label=COALESCE(label, excluded.label)",
                (self.run_id, ip, ts, ts, asn, as_name, cc, label))
            conn.commit()
            conn.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# ICMP — raw/dgram socket ping with reply TTL
# --------------------------------------------------------------------------- #
ICMP_ECHO, ICMP_ECHOREPLY = 8, 0
ICMP6_ECHO, ICMP6_ECHOREPLY = 128, 129
ICMP_UNREACH, ICMP_TIMEEXCEEDED = 3, 11
IP_RECVTTL = getattr(socket, "IP_RECVTTL", 12)
IPV6_RECVHOPLIMIT = getattr(socket, "IPV6_RECVHOPLIMIT", 51)
IPV6_HOPLIMIT = getattr(socket, "IPV6_HOPLIMIT", 52)

_resolve_cache = {}


def resolve_host(host, family=socket.AF_INET):
    """Resolve once and cache; returns an IP string or None."""
    key = (host, family)
    if key in _resolve_cache:
        return _resolve_cache[key]
    try:
        socket.inet_pton(family, host)
        _resolve_cache[key] = host
        return host
    except (OSError, ValueError):
        pass
    try:
        infos = socket.getaddrinfo(host, None, family, socket.SOCK_DGRAM)
        ip = infos[0][4][0]
    except Exception:
        ip = None
    _resolve_cache[key] = ip
    return ip


def icmp_checksum(data):
    if len(data) % 2:
        data += b"\x00"
    total = 0
    for i in range(0, len(data), 2):
        total += (data[i] << 8) + data[i + 1]
    total = (total >> 16) + (total & 0xFFFF)
    total += total >> 16
    return ~total & 0xFFFF


class IcmpSocket:
    """Unprivileged (SOCK_DGRAM) ICMP where the kernel allows it, raw otherwise."""

    def __init__(self, family=socket.AF_INET):
        self.family = family
        self.sock = None
        self.mode = None
        self.ident = os.getpid() & 0xFFFF

    def open(self):
        v4 = self.family == socket.AF_INET
        proto = socket.IPPROTO_ICMP if v4 else socket.IPPROTO_ICMPV6
        for kind, name in ((socket.SOCK_DGRAM, "dgram"), (socket.SOCK_RAW, "raw")):
            try:
                s = socket.socket(self.family, kind, proto)
            except Exception:
                continue
            try:
                if v4:
                    s.setsockopt(socket.IPPROTO_IP, IP_RECVTTL, 1)
                else:
                    s.setsockopt(socket.IPPROTO_IPV6, IPV6_RECVHOPLIMIT, 1)
            except Exception:
                pass
            s.setblocking(False)
            self.sock, self.mode = s, name
            return True
        return False

    def close(self):
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass

    def send(self, ip, seq, size=56):
        v4 = self.family == socket.AF_INET
        etype = ICMP_ECHO if v4 else ICMP6_ECHO
        stamp = b"netwatch" + struct.pack("!d", time.time())
        payload = (stamp + bytes(max(0, size - len(stamp))))[:max(len(stamp), size)]
        head = struct.pack("!BBHHH", etype, 0, 0, self.ident, seq)
        if v4:
            chk = icmp_checksum(head + payload)
            head = struct.pack("!BBHHH", etype, 0, chk, self.ident, seq)
        # ICMPv6 checksums are filled in by the kernel.
        self.sock.sendto(head + payload, (ip, 0))

    def recv(self):
        """Return (src_ip, kind, seq, ttl) or None. kind: 'reply'/'unreach'/'ttl-exceeded'."""
        ttl = None
        try:
            if hasattr(self.sock, "recvmsg"):
                data, anc, _flags, addr = self.sock.recvmsg(2048, socket.CMSG_SPACE(64))
                for lvl, typ, val in anc:
                    if (lvl, typ) in ((socket.IPPROTO_IP, socket.IP_TTL),
                                      (socket.IPPROTO_IPV6, IPV6_HOPLIMIT)) and val:
                        ttl = val[0] if len(val) == 1 else struct.unpack("=i", val[:4])[0]
            else:
                data, addr = self.sock.recvfrom(2048)
        except (BlockingIOError, InterruptedError):
            return None
        except Exception:
            return None
        src = addr[0]
        if self.family == socket.AF_INET and len(data) >= 20 and (data[0] >> 4) == 4:
            ihl = (data[0] & 0x0F) * 4
            ttl = data[8]
            data = data[ihl:]
        if len(data) < 8:
            return None
        itype = data[0]
        reply_type = ICMP_ECHOREPLY if self.family == socket.AF_INET else ICMP6_ECHOREPLY
        if itype == reply_type:
            ident, seq = struct.unpack("!HH", data[4:8])
            if self.mode == "raw" and ident != self.ident:
                return None
            return src, "reply", seq, ttl
        if itype in (ICMP_UNREACH, ICMP_TIMEEXCEEDED):
            # The quoted packet holds the original IP header + our ICMP header.
            inner = data[8:]
            if len(inner) >= 28 and (inner[0] >> 4) == 4:
                ihl = (inner[0] & 0x0F) * 4
                if len(inner) >= ihl + 8:
                    seq = struct.unpack("!H", inner[ihl + 6:ihl + 8])[0]
                    kind = "unreach" if itype == ICMP_UNREACH else "ttl-exceeded"
                    return src, kind, seq, ttl
        return None


IP_RECVERR = 11
IP_MTU_DISCOVER = 10
IP_PMTUDISC_DO = 2
IP_MTU = 14


def _read_errqueue(sock):
    """Pull the offending router's address out of the socket error queue (Linux)."""
    try:
        _data, anc, _flags, _addr = sock.recvmsg(512, 1024, socket.MSG_ERRQUEUE)
    except (BlockingIOError, InterruptedError, OSError, AttributeError, ValueError):
        return None
    for lvl, typ, val in anc:
        # struct sock_extended_err is 16 bytes, followed by the offender sockaddr.
        if lvl == socket.IPPROTO_IP and typ == IP_RECVERR and len(val) >= 24:
            if struct.unpack_from("=H", val, 16)[0] == socket.AF_INET:
                return socket.inet_ntoa(val[20:24])
    return None


def icmp_traceroute(target, max_hops=12, timeout=1.2):
    """Traceroute with no external binary: TTL-limited echoes + the ICMP error queue.

    Minimal container images ship neither traceroute nor ping, and the ISP's first
    hop is the single most useful probe target there is — so discover it ourselves.
    """
    if not sys.platform.startswith("linux"):
        return []
    sock = IcmpSocket(socket.AF_INET)
    if not sock.open():
        return []
    hops = []
    try:
        try:
            sock.sock.setsockopt(socket.IPPROTO_IP, IP_RECVERR, 1)
        except OSError:
            return []
        for ttl in range(1, max_hops + 1):
            try:
                sock.sock.setsockopt(socket.IPPROTO_IP, socket.IP_TTL, ttl)
                sock.send(target, ttl)
            except OSError:
                break
            started = time.monotonic()
            deadline = started + timeout
            ip, rtt, reached = None, None, False
            while time.monotonic() < deadline:
                got = sock.recv()
                if got and got[1] == "reply":
                    ip, rtt, reached = got[0], (time.monotonic() - started) * 1000.0, True
                    break
                offender = _read_errqueue(sock.sock)
                if offender:
                    ip, rtt = offender, (time.monotonic() - started) * 1000.0
                    break
                time.sleep(0.01)
            hops.append((ttl, ip, rtt))
            if reached:
                break
    finally:
        sock.close()
    return hops


def path_mtu_socket(target, hi=1500, lo=1200, timeout=1.2):
    """Ask the kernel for the path MTU using DF-marked echoes; no ping binary needed."""
    if not sys.platform.startswith("linux"):
        return None, None
    sock = IcmpSocket(socket.AF_INET)
    if not sock.open():
        return None, None
    seq = 1000
    try:
        try:
            sock.sock.setsockopt(socket.IPPROTO_IP, IP_MTU_DISCOVER, IP_PMTUDISC_DO)
        except OSError:
            return None, None

        def probe(mtu):
            """True = a packet of this size came back, False = too big, None = no answer."""
            nonlocal seq
            seq = (seq + 1) & 0xFFFF
            try:
                sock.send(target, seq, size=max(16, mtu - 28))
            except OSError as e:
                if getattr(e, "errno", None) in (90, 40):        # EMSGSIZE
                    return False
                return None
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                got = sock.recv()
                if got and got[1] == "reply" and got[2] == seq:
                    return True
                time.sleep(0.01)
            return None

        for _ in range(3):
            got = probe(hi)
            if got:
                return hi, "no fragmentation needed up to 1500"
            if got is False:
                break
        try:
            learned = sock.sock.getsockopt(socket.IPPROTO_IP, IP_MTU)
            if 576 <= learned < 1500:
                return learned, "reported by the kernel after a fragmentation-needed reply"
        except OSError:
            pass
        if probe(lo) is not True:
            return None, None
        best, a, b = lo, lo, hi
        while a <= b:
            mid = (a + b) // 2
            if probe(mid) is True:
                best, a = mid, mid + 1
            else:
                b = mid - 1
        return best, "discovered by binary search"
    finally:
        sock.close()


class PingEngine(threading.Thread):
    """One probe per target per interval, delivered through on_sample()."""

    def __init__(self, targets, cfg, on_sample, stop_event, log=None):
        super().__init__(name="ping", daemon=True)
        self.targets = targets            # [{'name','host','ip','family'}]
        self.cfg = cfg
        self.on_sample = on_sample
        self.stop = stop_event
        self.log = log or (lambda *a, **k: None)
        self.mode = "none"
        self._threads = []

    # -- mode selection ---------------------------------------------------- #
    def pick_mode(self):
        probe = IcmpSocket(socket.AF_INET)
        if probe.open():
            probe.close()
            self.mode = "icmp"
        elif have("ping"):
            self.mode = "exec"
        else:
            self.mode = "tcp"
        return self.mode

    def run(self):
        if self.mode == "none":
            self.pick_mode()
        try:
            if self.mode == "icmp":
                self._run_icmp()
            elif self.mode == "exec":
                self._run_exec()
            else:
                self._run_tcp()
        except Exception as e:
            self.log("error", f"ping engine stopped: {e}")

    # -- ICMP -------------------------------------------------------------- #
    def _run_icmp(self):
        socks, pend = {}, {}
        for fam in {t["family"] for t in self.targets}:
            s = IcmpSocket(fam)
            if s.open():
                socks[fam] = s
        if not socks:
            self.mode = "exec" if have("ping") else "tcp"
            return self._run_exec() if self.mode == "exec" else self._run_tcp()

        seq_counter = 0
        interval = max(0.1, float(self.cfg.ping_interval))
        timeout = max(0.2, float(self.cfg.ping_timeout))
        next_tick = time.monotonic()
        by_ip = {}
        while not self.stop.is_set():
            now = time.monotonic()
            if now >= next_tick:
                for t in self.targets:
                    fam, ip = t["family"], t["ip"]
                    if not ip or fam not in socks:
                        continue
                    seq_counter = (seq_counter + 1) & 0xFFFF
                    try:
                        socks[fam].send(ip, seq_counter)
                    except Exception as e:
                        self.on_sample(t["name"], now_ts(), False, None, None,
                                       seq_counter, f"send:{type(e).__name__}")
                        continue
                    pend[(fam, seq_counter)] = (t["name"], ip, now, now_ts())
                    by_ip.setdefault((fam, ip), t["name"])
                next_tick += interval
                if next_tick < now:            # laptop resumed from sleep
                    next_tick = now + interval
            wait = max(0.0, min(next_tick - time.monotonic(), 0.25))
            try:
                readable, _, _ = select.select([s.sock for s in socks.values()], [], [], wait)
            except Exception:
                readable = []
                time.sleep(wait)
            for raw in readable:
                fam = next((f for f, s in socks.items() if s.sock is raw), None)
                if fam is None:
                    continue
                while True:
                    got = socks[fam].recv()
                    if not got:
                        break
                    src, kind, seq, ttl = got
                    key = (fam, seq)
                    if key not in pend:
                        continue
                    name, ip, sent_mono, sent_wall = pend.pop(key)
                    rtt = (time.monotonic() - sent_mono) * 1000.0
                    if kind == "reply":
                        self.on_sample(name, sent_wall, True, rtt, ttl, seq, None)
                    else:
                        self.on_sample(name, sent_wall, False, None, ttl, seq, kind)
            cutoff = time.monotonic() - timeout
            for key in [k for k, v in pend.items() if v[2] < cutoff]:
                name, ip, sent_mono, sent_wall = pend.pop(key)
                self.on_sample(name, sent_wall, False, None, None, key[1], "timeout")
        for s in socks.values():
            s.close()

    # -- system ping binary ------------------------------------------------ #
    def _run_exec(self):
        for t in self.targets:
            th = threading.Thread(target=self._exec_one, args=(t,),
                                  name=f"ping-{t['name']}", daemon=True)
            th.start()
            self._threads.append(th)
        while not self.stop.is_set():
            time.sleep(0.25)

    def _exec_one(self, t):
        interval = max(0.2, float(self.cfg.ping_interval))
        timeout = max(1, int(round(float(self.cfg.ping_timeout))))
        host = t["ip"] or t["host"]
        if os.name == "nt":
            args = ["ping", "-t", "-w", str(int(timeout * 1000)), host]
        elif sys.platform == "darwin":
            args = ["ping", "-n", "-i", str(interval), "-W", str(int(timeout * 1000)), host]
        else:
            args = ["ping", "-n", "-O", "-i", str(interval), "-W", str(timeout), host]
        proc = None
        try:
            proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, errors="replace", bufsize=1)
        except Exception as e:
            self.log("error", f"ping {host}: {e}")
            return
        last_seq = None
        try:
            for line in proc.stdout:
                if self.stop.is_set():
                    break
                ts = now_ts()
                low = line.lower()
                m_rtt = re.search(r"time[=<]\s*([\d.]+)\s*ms", low)
                m_seq = re.search(r"(?:icmp_seq|seq)[= ](\d+)", low)
                m_ttl = re.search(r"ttl[=](\d+)", low)
                seq = int(m_seq.group(1)) if m_seq else None
                if m_rtt:
                    if seq is not None and last_seq is not None and seq > last_seq + 1:
                        for missing in range(last_seq + 1, seq):
                            self.on_sample(t["name"], ts, False, None, None, missing, "timeout")
                    if seq is not None:
                        last_seq = seq
                    self.on_sample(t["name"], ts, True, float(m_rtt.group(1)),
                                   int(m_ttl.group(1)) if m_ttl else None, seq, None)
                elif "no answer yet" in low:
                    continue                     # confirmed later by the seq gap
                elif ("timed out" in low or "unreachable" in low
                      or "100% packet loss" in low or "failure" in low):
                    self.on_sample(t["name"], ts, False, None, None, seq,
                                   "unreachable" if "unreachable" in low else "timeout")
        except Exception:
            pass
        finally:
            try:
                proc.kill()
            except Exception:
                pass

    # -- TCP connect fallback ---------------------------------------------- #
    def _run_tcp(self):
        for t in self.targets:
            th = threading.Thread(target=self._tcp_one, args=(t,),
                                  name=f"tcp-{t['name']}", daemon=True)
            th.start()
            self._threads.append(th)
        while not self.stop.is_set():
            time.sleep(0.25)

    def _tcp_one(self, t):
        interval = max(0.2, float(self.cfg.ping_interval))
        timeout = float(self.cfg.ping_timeout)
        port = t.get("port") or (53 if t["name"] in ("google-dns", "cloudflare", "quad9") else 443)
        seq = 0
        while not self.stop.is_set():
            started = time.monotonic()
            ts = now_ts()
            seq += 1
            s = None
            try:
                s = socket.socket(t["family"], socket.SOCK_STREAM)
                s.settimeout(timeout)
                s.connect((t["ip"] or t["host"], port))
                self.on_sample(t["name"], ts, True, (time.monotonic() - started) * 1000.0,
                               None, seq, None)
            except socket.timeout:
                self.on_sample(t["name"], ts, False, None, None, seq, "timeout")
            except Exception as e:
                # A refused connection still proves the host is reachable.
                if isinstance(e, ConnectionRefusedError):
                    self.on_sample(t["name"], ts, True, (time.monotonic() - started) * 1000.0,
                                   None, seq, None)
                else:
                    self.on_sample(t["name"], ts, False, None, None, seq, type(e).__name__)
            finally:
                try:
                    if s:
                        s.close()
                except Exception:
                    pass
            self.stop.wait(max(0.0, interval - (time.monotonic() - started)))


# --------------------------------------------------------------------------- #
# DNS — minimal resolver client (UDP / TCP) plus DoH
# --------------------------------------------------------------------------- #
RCODES = {0: "NOERROR", 1: "FORMERR", 2: "SERVFAIL", 3: "NXDOMAIN",
          4: "NOTIMP", 5: "REFUSED"}
QTYPE = {"A": 1, "NS": 2, "CNAME": 5, "PTR": 12, "TXT": 16, "AAAA": 28}


def _dns_encode_name(name):
    out = b""
    for label in name.rstrip(".").split("."):
        try:
            raw = label.encode("idna") if any(ord(c) > 127 for c in label) else label.encode()
        except Exception:
            raw = label.encode("utf-8", "ignore")
        out += bytes([len(raw)]) + raw
    return out + b"\x00"


def _dns_parse_name(data, offset):
    labels, jumped, after, hops = [], False, offset, 0
    while offset < len(data):
        length = data[offset]
        if length == 0:
            offset += 1
            break
        if length & 0xC0 == 0xC0:
            if offset + 1 >= len(data):
                break
            ptr = ((length & 0x3F) << 8) | data[offset + 1]
            if not jumped:
                after = offset + 2
            offset, jumped = ptr, True
            hops += 1
            if hops > 20:
                break
            continue
        labels.append(data[offset + 1:offset + 1 + length].decode("latin-1"))
        offset += 1 + length
    return ".".join(labels), (after if jumped else offset)


def dns_query(server, name, qtype="A", qclass=1, timeout=2.0, proto="udp", port=53):
    """Single DNS query. Returns dict(ok, rtt_ms, rcode, answers, err)."""
    qt = QTYPE.get(qtype, 1) if isinstance(qtype, str) else int(qtype)
    txid = random.randint(0, 0xFFFF)
    query = struct.pack("!HHHHHH", txid, 0x0100, 1, 0, 0, 0)
    query += _dns_encode_name(name) + struct.pack("!HH", qt, qclass)
    res = {"ok": False, "rtt_ms": None, "rcode": None, "answers": [], "err": None}
    started = time.monotonic()
    sock = None
    try:
        family = socket.AF_INET6 if ":" in server else socket.AF_INET
        if proto == "tcp":
            sock = socket.socket(family, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            sock.connect((server, port))
            sock.sendall(struct.pack("!H", len(query)) + query)
            head = sock.recv(2)
            if len(head) < 2:
                raise OSError("short read")
            need = struct.unpack("!H", head)[0]
            data = b""
            while len(data) < need:
                chunk = sock.recv(need - len(data))
                if not chunk:
                    break
                data += chunk
        else:
            sock = socket.socket(family, socket.SOCK_DGRAM)
            sock.settimeout(timeout)
            sock.sendto(query, (server, port))
            deadline = time.monotonic() + timeout
            while True:
                data, _addr = sock.recvfrom(4096)
                if len(data) >= 2 and struct.unpack("!H", data[:2])[0] == txid:
                    break
                if time.monotonic() > deadline:
                    raise socket.timeout("no matching response")
        res["rtt_ms"] = (time.monotonic() - started) * 1000.0
        flags, qd, an = struct.unpack("!HHH", data[2:8])
        res["rcode"] = RCODES.get(flags & 0xF, str(flags & 0xF))
        offset = 12
        for _ in range(qd):
            _n, offset = _dns_parse_name(data, offset)
            offset += 4
        for _ in range(an):
            _n, offset = _dns_parse_name(data, offset)
            if offset + 10 > len(data):
                break
            rtype, _rclass, _ttl, rdlen = struct.unpack("!HHIH", data[offset:offset + 10])
            offset += 10
            rdata = data[offset:offset + rdlen]
            offset += rdlen
            try:
                if rtype == 1 and rdlen == 4:
                    res["answers"].append(socket.inet_ntoa(rdata))
                elif rtype == 28 and rdlen == 16:
                    res["answers"].append(socket.inet_ntop(socket.AF_INET6, rdata))
                elif rtype == 16:
                    pos, parts = 0, []
                    while pos < len(rdata):
                        ln = rdata[pos]
                        parts.append(rdata[pos + 1:pos + 1 + ln].decode("latin-1"))
                        pos += 1 + ln
                    res["answers"].append("".join(parts))
                elif rtype in (2, 5, 12):
                    nm, _ = _dns_parse_name(data, offset - rdlen)
                    res["answers"].append(nm)
            except Exception:
                pass
        res["ok"] = res["rcode"] == "NOERROR"
    except socket.timeout:
        res["err"] = "timeout"
    except Exception as e:
        res["err"] = f"{type(e).__name__}: {e}"
    finally:
        try:
            if sock:
                sock.close()
        except Exception:
            pass
    if res["rtt_ms"] is None:
        res["rtt_ms"] = (time.monotonic() - started) * 1000.0
    return res


def dns_query_doh(url, name, qtype="A", timeout=4.0):
    """DoH via the JSON API — proves DNS works even when port 53 is filtered."""
    res = {"ok": False, "rtt_ms": None, "rcode": None, "answers": [], "err": None}
    started = time.monotonic()
    try:
        req = urllib.request.Request(
            f"{url}?name={urllib.parse.quote(name)}&type={qtype}",
            headers={"Accept": "application/dns-json", "User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read(65536).decode("utf-8", "replace"))
        res["rtt_ms"] = (time.monotonic() - started) * 1000.0
        res["rcode"] = RCODES.get(body.get("Status", 0), str(body.get("Status")))
        res["answers"] = [a.get("data", "") for a in body.get("Answer", [])]
        res["ok"] = body.get("Status", 1) == 0
    except Exception as e:
        res["rtt_ms"] = (time.monotonic() - started) * 1000.0
        res["err"] = f"{type(e).__name__}: {e}"
    return res


def dns_query_system(name, timeout=5.0):
    """Time the OS resolver itself — that is what applications actually use."""
    res = {"ok": False, "rtt_ms": None, "rcode": None, "answers": [], "err": None}
    started = time.monotonic()
    try:
        infos = socket.getaddrinfo(name, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        res["answers"] = sorted({i[4][0] for i in infos})
        res["ok"] = True
        res["rcode"] = "NOERROR"
    except socket.gaierror as e:
        res["err"] = f"gaierror: {e.strerror or e}"
        res["rcode"] = "NXDOMAIN" if getattr(e, "errno", None) in (-2, -5) else "ERROR"
    except Exception as e:
        res["err"] = f"{type(e).__name__}: {e}"
    res["rtt_ms"] = (time.monotonic() - started) * 1000.0
    return res


# --------------------------------------------------------------------------- #
# WAN watcher — public IP over DNS, fast enough to catch a balancer failover
# --------------------------------------------------------------------------- #
WAN_METHODS = [
    # (name, server, qname, qtype, qclass) — each is a single UDP round trip.
    ("opendns", "208.67.222.222", "myip.opendns.com", "A", 1),
    ("cloudflare", "1.1.1.1", "whoami.cloudflare", "TXT", 3),
    ("google", "216.239.32.10", "o-o.myaddr.l.google.com", "TXT", 1),
    ("opendns2", "208.67.220.220", "myip.opendns.com", "A", 1),
]

_IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


def wan_ip_once(method, timeout=2.0):
    """Query one public-IP oracle; returns (ip|None, rtt_ms, err)."""
    name, server, qname, qtype, qclass = method
    r = dns_query(server, qname, qtype=qtype, qclass=qclass, timeout=timeout)
    ip = None
    for ans in r["answers"]:
        cand = ans.strip().strip('"')
        if _IPV4_RE.match(cand) or ":" in cand:
            ip = cand
            break
    if ip is None and r["err"] is None:
        r["err"] = f"no address in answer ({r['rcode']})"
    return ip, r["rtt_ms"], r["err"]


def asn_lookup(ip, timeout=3.0):
    """Team Cymru's DNS interface — labels each uplink with its real operator."""
    out = {"asn": None, "as_name": None, "cc": None}
    try:
        if ":" in ip:
            return out
        rev = ".".join(reversed(ip.split(".")))
        r = dns_query("1.1.1.1", f"{rev}.origin.asn.cymru.com", qtype="TXT", timeout=timeout)
        if not r["answers"]:
            return out
        parts = [p.strip() for p in r["answers"][0].split("|")]
        if parts:
            out["asn"] = parts[0].split()[0] if parts[0] else None
        if len(parts) > 2:
            out["cc"] = parts[2]
        if out["asn"]:
            r2 = dns_query("1.1.1.1", f"AS{out['asn']}.asn.cymru.com",
                           qtype="TXT", timeout=timeout)
            if r2["answers"]:
                bits = [p.strip() for p in r2["answers"][0].split("|")]
                if bits:
                    out["as_name"] = bits[-1]
    except Exception:
        pass
    return out


# --------------------------------------------------------------------------- #
# HTTP — phase-timed request (DNS / TCP / TLS / TTFB / total)
# --------------------------------------------------------------------------- #
def http_probe(url, timeout=10.0, max_body=65536, insecure=False):
    """Time every phase of one HTTP(S) request separately."""
    res = {"url": url, "ok": False, "status": None, "dns_ms": None, "tcp_ms": None,
           "tls_ms": None, "ttfb_ms": None, "total_ms": None, "bytes": 0,
           "tls_ver": None, "cert_days": None, "body": "", "headers": {}, "err": None}
    parts = urllib.parse.urlsplit(url)
    https = parts.scheme == "https"
    host = parts.hostname
    port = parts.port or (443 if https else 80)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    sock = None
    t0 = time.monotonic()
    try:
        infos = socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
        t_dns = time.monotonic()
        res["dns_ms"] = (t_dns - t0) * 1000.0
        family, socktype, proto, _canon, sa = infos[0]
        sock = socket.socket(family, socktype, proto)
        sock.settimeout(timeout)
        sock.connect(sa)
        t_tcp = time.monotonic()
        res["tcp_ms"] = (t_tcp - t_dns) * 1000.0
        if https:
            ctx = ssl.create_default_context()
            if insecure:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            sock = ctx.wrap_socket(sock, server_hostname=host)
            t_tls = time.monotonic()
            res["tls_ms"] = (t_tls - t_tcp) * 1000.0
            res["tls_ver"] = sock.version()
            try:
                cert = sock.getpeercert()
                if cert and cert.get("notAfter"):
                    left = ssl.cert_time_to_seconds(cert["notAfter"]) - time.time()
                    res["cert_days"] = int(left // 86400)
            except Exception:
                pass
        else:
            t_tls = t_tcp
        req = (f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUser-Agent: {UA}\r\n"
               "Accept: */*\r\nAccept-Encoding: identity\r\nConnection: close\r\n\r\n")
        sock.sendall(req.encode())
        chunks, first_at = [], None
        got = 0
        while got < max_body:
            try:
                chunk = sock.recv(16384)
            except socket.timeout:
                break
            if not chunk:
                break
            if first_at is None:
                first_at = time.monotonic()
                res["ttfb_ms"] = (first_at - t_tls) * 1000.0
            chunks.append(chunk)
            got += len(chunk)
        res["total_ms"] = (time.monotonic() - t0) * 1000.0
        raw = b"".join(chunks)
        res["bytes"] = len(raw)
        head, _, body = raw.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        if lines and lines[0].startswith("HTTP/"):
            bits = lines[0].split(None, 2)
            if len(bits) >= 2 and bits[1].isdigit():
                res["status"] = int(bits[1])
        for line in lines[1:]:
            k, _, v = line.partition(":")
            if k:
                res["headers"][k.strip().lower()] = v.strip()
        res["body"] = body[:4096].decode("utf-8", "replace")
        res["ok"] = res["status"] is not None and res["status"] < 400
    except socket.timeout:
        res["err"] = "timeout"
        res["total_ms"] = (time.monotonic() - t0) * 1000.0
    except ssl.SSLError as e:
        res["err"] = f"tls: {e.reason or e}"
        res["total_ms"] = (time.monotonic() - t0) * 1000.0
    except socket.gaierror as e:
        res["err"] = f"dns: {e.strerror or e}"
        res["total_ms"] = (time.monotonic() - t0) * 1000.0
    except Exception as e:
        res["err"] = f"{type(e).__name__}: {e}"
        res["total_ms"] = (time.monotonic() - t0) * 1000.0
    finally:
        try:
            if sock:
                sock.close()
        except Exception:
            pass
    return res


def parse_cf_trace(body):
    """cloudflare.com/cdn-cgi/trace returns key=value lines: ip, colo, loc, warp."""
    out = {}
    for line in (body or "").splitlines():
        k, _, v = line.partition("=")
        if k:
            out[k.strip()] = v.strip()
    return out


# --------------------------------------------------------------------------- #
# speed test — multi-stream throughput (latency under load is measured by the
# ICMP engine that keeps running, which is what makes the bufferbloat grade real)
# --------------------------------------------------------------------------- #
class _Counter:
    def __init__(self):
        self.value = 0
        self._lock = threading.Lock()

    def add(self, n):
        with self._lock:
            self.value += n

    def get(self):
        with self._lock:
            return self.value


class _UploadBody:
    """File-like source of random-ish bytes so uploads never buffer in RAM."""

    def __init__(self, total, counter, stop):
        self.left = total
        self.counter = counter
        self.stop = stop
        self.buf = os.urandom(65536)

    def read(self, size=65536):
        if self.left <= 0 or self.stop.is_set():
            return b""
        n = min(size, self.left, len(self.buf))
        self.left -= n
        self.counter.add(n)
        return self.buf[:n]


def speed_download(cfg, stop, on_series=None, log=None):
    """Parallel-stream download; returns dict(mbps, bytes, seconds, err, server)."""
    streams = max(1, int(cfg.speed_streams))
    budget = int(float(cfg.speed_max_mb) * 1024 * 1024)
    per_stream = max(1024 * 1024, budget // streams)
    counter = _Counter()
    local_stop = threading.Event()
    errors = []
    # SPEED_DOWN_URL carries a {bytes} placeholder for documentation; the workers
    # build their own query so they can keep the connection alive between requests.
    parts = urllib.parse.urlsplit(SPEED_DOWN_URL)
    host, path = parts.hostname, (parts.path or "/")
    server = host
    chunk_bytes = max(1024 * 1024, min(per_stream, 50 * 1024 * 1024))

    def worker(idx):
        conn, failures = None, 0
        while not (stop.is_set() or local_stop.is_set()) and counter.get() < budget:
            try:
                if conn is None:
                    conn = http.client.HTTPSConnection(host, 443, timeout=15,
                                                       blocksize=65536)
                conn.request("GET", f"{path}?bytes={chunk_bytes}",
                             headers={"User-Agent": UA, "Accept-Encoding": "identity"})
                resp = conn.getresponse()
                if resp.status != 200:
                    resp.read()
                    errors.append(f"HTTP {resp.status} from {host}")
                    failures += 1
                    if failures >= 3:
                        break
                    continue
                while not (stop.is_set() or local_stop.is_set()):
                    data = resp.read(65536)
                    if not data:
                        break
                    counter.add(len(data))
            except Exception as e:
                errors.append(f"{type(e).__name__}: {e}")
                failures += 1
                try:
                    if conn:
                        conn.close()
                except Exception:
                    pass
                conn = None
                if failures >= 3:
                    break
        try:
            if conn:
                conn.close()
        except Exception:
            pass

    threads = [threading.Thread(target=worker, args=(i,), daemon=True,
                                name=f"dl-{i}") for i in range(streams)]
    started = time.monotonic()
    for th in threads:
        th.start()
    duration = max(3.0, float(cfg.speed_seconds))
    warmup = min(1.5, duration / 3.0)
    warm_bytes, warm_at = None, None
    last_bytes, last_at = 0, started
    capped = False
    while time.monotonic() - started < duration:
        if stop.is_set():
            break
        time.sleep(0.25)
        now = time.monotonic()
        total = counter.get()
        if on_series:
            dt = now - last_at
            if dt > 0:
                on_series("download", (total - last_bytes) * 8 / dt / 1e6)
        last_bytes, last_at = total, now
        if warm_bytes is None and now - started >= warmup:
            warm_bytes, warm_at = total, now
        if total >= budget:
            capped = True
            break
        if len(errors) >= streams:
            break
    local_stop.set()
    ended = time.monotonic()
    for th in threads:
        th.join(timeout=3)
    total = counter.get()
    if warm_bytes is None:
        warm_bytes, warm_at = 0, started
    seconds = max(0.001, ended - warm_at)
    measured = max(0, total - warm_bytes)
    mbps = measured * 8 / seconds / 1e6
    return {"mbps": mbps if measured > 0 else 0.0, "bytes": total,
            "seconds": ended - started, "streams": streams, "server": server,
            "capped": capped, "window": seconds,
            "err": (errors[0] if errors and total == 0 else None)}


def speed_upload(cfg, stop, on_series=None, log=None):
    """Parallel-stream upload against Cloudflare's __up endpoint."""
    streams = max(1, int(cfg.speed_streams))
    budget = int(float(cfg.speed_max_mb) * 1024 * 1024 / 2)
    per_stream = max(512 * 1024, budget // streams)
    counter = _Counter()
    local_stop = threading.Event()
    errors = []
    parts = urllib.parse.urlsplit(SPEED_UP_URL)
    host, path = parts.hostname, (parts.path or "/")

    chunk_bytes = max(512 * 1024, min(per_stream, 25 * 1024 * 1024))

    def worker(idx):
        conn, failures = None, 0
        while not (stop.is_set() or local_stop.is_set()) and counter.get() < budget:
            try:
                if conn is None:
                    conn = http.client.HTTPSConnection(host, 443, timeout=20,
                                                       blocksize=65536)
                conn.request("POST", path,
                             body=_UploadBody(chunk_bytes, counter, local_stop),
                             headers={"Content-Length": str(chunk_bytes),
                                      "Content-Type": "application/octet-stream",
                                      "User-Agent": UA})
                resp = conn.getresponse()
                resp.read(4096)
                if resp.status >= 400:
                    errors.append(f"HTTP {resp.status} from {host}")
                    failures += 1
                    if failures >= 3:
                        break
            except Exception as e:
                errors.append(f"{type(e).__name__}: {e}")
                failures += 1
                try:
                    if conn:
                        conn.close()
                except Exception:
                    pass
                conn = None
                if failures >= 3:
                    break
        try:
            if conn:
                conn.close()
        except Exception:
            pass

    threads = [threading.Thread(target=worker, args=(i,), daemon=True,
                                name=f"ul-{i}") for i in range(streams)]
    started = time.monotonic()
    for th in threads:
        th.start()
    duration = max(3.0, float(cfg.speed_seconds))
    warmup = min(1.5, duration / 3.0)
    warm_bytes, warm_at = None, None
    last_bytes, last_at = 0, started
    capped = False
    while time.monotonic() - started < duration:
        if stop.is_set():
            break
        time.sleep(0.25)
        now = time.monotonic()
        total = counter.get()
        if on_series:
            dt = now - last_at
            if dt > 0:
                on_series("upload", (total - last_bytes) * 8 / dt / 1e6)
        last_bytes, last_at = total, now
        if warm_bytes is None and now - started >= warmup:
            warm_bytes, warm_at = total, now
        if total >= budget:
            capped = True
            break
        if all(not th.is_alive() for th in threads):
            break
    local_stop.set()
    ended = time.monotonic()
    for th in threads:
        th.join(timeout=3)
    total = counter.get()
    if warm_bytes is None:
        warm_bytes, warm_at = 0, started
    seconds = max(0.001, ended - warm_at)
    measured = max(0, total - warm_bytes)
    mbps = measured * 8 / seconds / 1e6
    return {"mbps": mbps if measured > 0 else 0.0, "bytes": total,
            "seconds": ended - started, "streams": streams, "server": host,
            "capped": capped, "window": seconds,
            "err": (errors[0] if errors and total == 0 else None)}


# --------------------------------------------------------------------------- #
# path probes — traceroute, MTU, TCP ports, NTP
# --------------------------------------------------------------------------- #
def traceroute(target, max_hops=20, timeout=2, log=None):
    """Return [(hop, ip|None, rtt_ms|None)] using whatever tracer exists."""
    hops = []
    if os.name == "nt":
        rc, out = run_cmd(["tracert", "-d", "-w", str(int(timeout * 1000)),
                           "-h", str(max_hops), target], timeout=max_hops * timeout + 20)
        if rc in (0, 1):
            for line in out.splitlines():
                m = re.match(r"\s*(\d+)\s+(.*)$", line)
                if not m:
                    continue
                hop = int(m.group(1))
                rest = m.group(2)
                ip = re.search(r"(\d{1,3}(?:\.\d{1,3}){3})", rest)
                rtt = re.search(r"(\d+)\s*ms", rest)
                hops.append((hop, ip.group(1) if ip else None,
                             float(rtt.group(1)) if rtt else None))
        return hops
    for args in (["traceroute", "-n", "-q", "1", "-w", str(timeout),
                  "-m", str(max_hops), target],
                 ["tracepath", "-n", "-m", str(max_hops), target]):
        if not have(args[0]):
            continue
        rc, out = run_cmd(args, timeout=max_hops * timeout + 25)
        if rc not in (0, 1):
            continue
        for line in out.splitlines():
            m = re.match(r"\s*(\d+)[:\s]\s*(.*)$", line)
            if not m:
                continue
            hop, rest = int(m.group(1)), m.group(2)
            if "no reply" in rest or rest.strip().startswith("*"):
                hops.append((hop, None, None))
                continue
            ip = re.search(r"(\d{1,3}(?:\.\d{1,3}){3})", rest)
            rtt = re.search(r"([\d.]+)\s*ms", rest)
            hops.append((hop, ip.group(1) if ip else None,
                         float(rtt.group(1)) if rtt else None))
        if hops:
            return hops
    return icmp_traceroute(target, max_hops=min(max_hops, 14), timeout=float(timeout))


def path_mtu(target="1.1.1.1", lo=1200, hi=1500, log=None):
    """Binary-search the largest unfragmented payload; returns (mtu, note)."""
    mtu, note = path_mtu_socket(target, hi=hi, lo=lo)
    if mtu:
        return mtu, note
    if not have("ping"):
        return None, ("no answer to DF-marked probes and no ping binary to retry with"
                      if note is None else note)

    def try_size(payload):
        if os.name == "nt":
            args = ["ping", "-f", "-l", str(payload), "-n", "1", "-w", "1500", target]
        elif sys.platform == "darwin":
            args = ["ping", "-D", "-s", str(payload), "-c", "1", "-t", "2", target]
        else:
            args = ["ping", "-M", "do", "-s", str(payload), "-c", "1", "-W", "2", target]
        rc, out = run_cmd(args, timeout=8)
        low = out.lower()
        if "1 received" in low or "bytes from" in low or "reply from" in low:
            if "needs to be fragmented" in low or "too long" in low:
                return False
            return True
        return False

    lo_pl, hi_pl = lo - 28, hi - 28
    if try_size(hi_pl):
        return hi, "no fragmentation needed up to 1500"
    if not try_size(lo_pl):
        return None, "ICMP with DF blocked or MTU below 1200 — PMTUD may be broken"
    best = lo_pl
    a, b = lo_pl, hi_pl
    while a <= b:
        mid = (a + b) // 2
        if try_size(mid):
            best, a = mid, mid + 1
        else:
            b = mid - 1
    return best + 28, "discovered by binary search"


def tcp_port_check(host, port, timeout=4.0):
    started = time.monotonic()
    s = None
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        return True, (time.monotonic() - started) * 1000.0, None
    except socket.timeout:
        return False, (time.monotonic() - started) * 1000.0, "timeout"
    except ConnectionRefusedError:
        return False, (time.monotonic() - started) * 1000.0, "refused"
    except Exception as e:
        return False, (time.monotonic() - started) * 1000.0, f"{type(e).__name__}"
    finally:
        try:
            if s:
                s.close()
        except Exception:
            pass


NTP_EPOCH_DELTA = 2208988800


def ntp_probe(server, timeout=3.0):
    """UDP/123 round trip — also proves plain UDP is not being blocked."""
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(timeout)
        t0 = time.time()
        s.sendto(b"\x1b" + 47 * b"\0", (server, 123))
        data, _ = s.recvfrom(1024)
        t3 = time.time()
        if len(data) < 48:
            return False, None, None, "short reply"
        t1 = struct.unpack("!I", data[32:36])[0] - NTP_EPOCH_DELTA
        t1 += struct.unpack("!I", data[36:40])[0] / 2 ** 32
        t2 = struct.unpack("!I", data[40:44])[0] - NTP_EPOCH_DELTA
        t2 += struct.unpack("!I", data[44:48])[0] / 2 ** 32
        offset = ((t1 - t0) + (t2 - t3)) / 2.0
        return True, offset * 1000.0, (t3 - t0) * 1000.0, None
    except socket.timeout:
        return False, None, None, "timeout"
    except Exception as e:
        return False, None, None, f"{type(e).__name__}"
    finally:
        try:
            if s:
                s.close()
        except Exception:
            pass


class LinkSampler:
    """Local interface counters + Wi-Fi signal, sampled as deltas."""

    def __init__(self, iface):
        self.iface = iface
        self._prev = None
        self._prev_t = None

    def sample(self):
        counters = iface_counters(self.iface)
        now = time.monotonic()
        dbm, qual, _essid = wifi_info(self.iface) if self.iface else (None, None, "")
        out = {"rx_mbps": None, "tx_mbps": None, "rx_err": None, "tx_err": None,
               "rx_drop": None, "tx_drop": None, "wifi_dbm": dbm, "wifi_qual": qual,
               "carrier": None, "link_mbps": None}
        if not counters:
            return out
        out["carrier"] = counters.get("carrier")
        out["link_mbps"] = counters.get("speed")
        if self._prev and self._prev_t and now > self._prev_t:
            dt = now - self._prev_t
            out["rx_mbps"] = max(0, counters["rx_bytes"] - self._prev["rx_bytes"]) * 8 / dt / 1e6
            out["tx_mbps"] = max(0, counters["tx_bytes"] - self._prev["tx_bytes"]) * 8 / dt / 1e6
            out["rx_err"] = max(0, counters["rx_errors"] - self._prev["rx_errors"])
            out["tx_err"] = max(0, counters["tx_errors"] - self._prev["tx_errors"])
            out["rx_drop"] = max(0, counters["rx_dropped"] - self._prev["rx_dropped"])
            out["tx_drop"] = max(0, counters["tx_dropped"] - self._prev["tx_dropped"])
        self._prev, self._prev_t = counters, now
        return out


# --------------------------------------------------------------------------- #
# live state — small rolling window kept in RAM purely to drive the dashboard
# --------------------------------------------------------------------------- #
class TargetLive:
    def __init__(self, name, host):
        self.name = name
        self.host = host
        self.sent = 0
        self.recv = 0
        self.rtts = collections.deque(maxlen=600)
        self.recent = collections.deque(maxlen=90)   # rtt or None, for the sparkline
        self.last_rtt = None
        self.last_ttl = None
        self.ttl_seen = {}
        self.last_ok_ts = 0.0
        self.last_ts = 0.0

    def add(self, ts, ok, rtt, ttl):
        self.sent += 1
        self.last_ts = ts
        if ok:
            self.recv += 1
            self.last_rtt = rtt
            self.last_ok_ts = ts
            self.rtts.append(rtt)
            self.recent.append(rtt)
            if ttl:
                self.last_ttl = ttl
                self.ttl_seen[ttl] = self.ttl_seen.get(ttl, 0) + 1
        else:
            self.recent.append(None)

    @property
    def loss_pct(self):
        return 0.0 if not self.sent else (self.sent - self.recv) * 100.0 / self.sent

    def stats(self):
        vals = sorted(self.rtts)
        return (mean(vals), percentile(vals, 95) if vals else None)


class LiveState:
    def __init__(self, targets):
        self.lock = threading.Lock()
        self.targets = {name: TargetLive(name, host) for name, host in targets}
        self.events = collections.deque(maxlen=200)
        self.started = now_ts()
        self.online = True
        self.outages = 0
        self.outage_total = 0.0
        self.outage_max = 0.0
        self.down_since = None
        self.wan_ip = None
        self.wan_label = ""
        self.wan_since = None
        self.wan_switches = 0
        self.wan_fail = 0
        self.wan_labels = {}
        self.wan_history = collections.deque(maxlen=50)   # (ts, ip)
        self.dns_last = {}
        self.http_last = {}
        self.speed_last = {}
        self.speed_running = ""
        self.link_last = {}
        self.mtu = None
        self.bytes_used = 0
        self.samples = 0
        self.note = ""

    def add_event(self, ts, kind, severity, message):
        with self.lock:
            self.events.append((ts, kind, severity, message))


# --------------------------------------------------------------------------- #
# monitor — owns every probe thread and the event log
# --------------------------------------------------------------------------- #
class Monitor:
    def __init__(self, cfg, storage, net):
        self.cfg = cfg
        self.st = storage
        self.net = net
        self.stop = threading.Event()
        self.targets = net["targets"]                    # [{'name','host','ip','family'}]
        self.live = LiveState([(t["name"], t["host"]) for t in self.targets])
        # Only the public anchors decide whether "the internet" is up: the gateway and
        # the first hops answer even while the line beyond them is dead.
        self.internet = [t["name"] for t in self.targets
                         if t.get("role", "anchor") in ("anchor", "custom")]
        self.local_targets = [t["name"] for t in self.targets
                              if t.get("role") in ("gateway", "lan", "isp")]
        self.threads = []
        self.speed_now = threading.Event()
        self.trace_now = threading.Event()
        self.ping_mode = "?"
        self._asn_cache = {}
        self._last_trace = None
        self.stopped_reason = ""

    # -- helpers ----------------------------------------------------------- #
    def event(self, kind, severity, message, details=None):
        ts = now_ts()
        self.st.put("events", (self.st.run_id, ts, kind, severity, message,
                               json.dumps(details) if details else None))
        self.live.add_event(ts, kind, severity, message)

    def log(self, level, message):
        self.event("log", "warning" if level == "error" else "info", message)

    # -- ping -------------------------------------------------------------- #
    def on_ping(self, name, ts, ok, rtt, ttl, seq, err):
        self.st.put("ping_samples", (self.st.run_id, ts, name, 1 if ok else 0,
                                     rtt, ttl, seq, err))
        prev_ttl = None
        with self.live.lock:
            tl = self.live.targets.get(name)
            if tl:
                prev_ttl = tl.last_ttl
                tl.add(ts, ok, rtt, ttl)
                self.live.samples += 1
        if ok and ttl and prev_ttl and ttl != prev_ttl:
            self.event("ttl_change", "warning",
                       f"{name}: reply TTL {prev_ttl} → {ttl} (route length changed)",
                       {"target": name, "from": prev_ttl, "to": ttl})
            self.trace_now.set()

    # -- outage watchdog --------------------------------------------------- #
    def _watchdog(self):
        threshold = float(self.cfg.ping_timeout) + float(self.cfg.ping_interval) * \
            max(0, int(self.cfg.outage_ticks) - 1)
        grace = time.monotonic() + 5.0
        while not self.stop.wait(0.3):
            if time.monotonic() < grace:
                continue
            now = now_ts()
            with self.live.lock:
                internet_ok = max((self.live.targets[n].last_ok_ts
                                   for n in self.internet if n in self.live.targets),
                                  default=0.0)
                gw = self.live.targets.get("gateway")
                gw_ok = gw.last_ok_ts if gw else None
                online = (now - internet_ok) <= threshold
                was_online = self.live.online
                if online and not was_online:
                    dur = now - (self.live.down_since or now)
                    self.live.online = True
                    self.live.outages += 1
                    self.live.outage_total += dur
                    self.live.outage_max = max(self.live.outage_max, dur)
                    self.live.down_since = None
                elif not online and was_online:
                    self.live.online = False
                    self.live.down_since = internet_ok or now
            if online and not was_online:
                self.event("outage_end", "warning",
                           f"connectivity restored after {fmt_dur(dur)}", {"seconds": dur})
            elif not online and was_online:
                scope = "whole link (gateway unreachable too)" if (
                    gw_ok is not None and (now - gw_ok) > threshold) else \
                    "internet only (gateway still answers)"
                self.event("outage_start", "critical", f"connectivity lost — {scope}",
                           {"scope": scope})

    # -- WAN / failover watcher -------------------------------------------- #
    def _wan_loop(self):
        methods = list(WAN_METHODS)
        primary = 0
        fails = 0
        current = None
        while not self.stop.is_set():
            started = time.monotonic()
            method = methods[primary % len(methods)]
            ip, rtt, err = wan_ip_once(method, timeout=max(1.0, self.cfg.wan_interval))
            ts = now_ts()
            self.st.put("wan_samples", (self.st.run_id, ts, method[0], ip,
                                        1 if ip else 0, rtt, err))
            if not ip:
                fails += 1
                with self.live.lock:
                    self.live.wan_fail += 1
                if fails >= 3:
                    primary += 1
                    fails = 0
                self.stop.wait(max(0.0, self.cfg.wan_interval - (time.monotonic() - started)))
                continue
            fails = 0
            if ip != current:
                confirmed_by = None
                if current is not None:
                    # A single oracle can glitch; ask a different one before calling it
                    # a failover — and believe the second answer, whichever way it goes.
                    for alt in methods:
                        if alt[0] == method[0]:
                            continue
                        alt_ip, alt_rtt, alt_err = wan_ip_once(alt, timeout=2.0)
                        self.st.put("wan_samples", (self.st.run_id, now_ts(), alt[0],
                                                    alt_ip, 1 if alt_ip else 0,
                                                    alt_rtt, alt_err))
                        if alt_ip:
                            confirmed_by = alt[0] if alt_ip == ip else None
                            ip = alt_ip
                            break
                if ip != current:
                    self._on_wan_ip(ip, ts, previous=current,
                                    confirmed_by=confirmed_by, method=method[0])
                current = ip
            else:
                self.st.upsert_wan_ip(ip, ts)
            self.stop.wait(max(0.0, self.cfg.wan_interval - (time.monotonic() - started)))

    def _on_wan_ip(self, ip, ts, previous=None, confirmed_by=None, method=""):
        with self.live.lock:
            label = self.live.wan_labels.get(ip)
            if label is None:
                n = len(self.live.wan_labels)
                label = f"WAN-{chr(ord('A') + n)}" if n < 26 else f"WAN-{n + 1}"
                self.live.wan_labels[ip] = label
            self.live.wan_ip = ip
            self.live.wan_since = ts
            self.live.wan_label = ""          # refilled by the ASN lookup below
            self.live.wan_history.append((ts, ip))
            if previous is not None:
                self.live.wan_switches += 1
        self.st.upsert_wan_ip(ip, ts, label=label)
        if previous is None:
            self.event("wan_ip", "info", f"public IP {ip} (via {method})", {"ip": ip})
        else:
            self.event("wan_switch", "warning",
                       f"uplink switched: {previous} → {ip}"
                       + (f" (confirmed by {confirmed_by})" if confirmed_by else ""),
                       {"from": previous, "to": ip, "confirmed_by": confirmed_by})
            self.trace_now.set()
        threading.Thread(target=self._label_ip, args=(ip, ts), daemon=True).start()

    def _label_ip(self, ip, ts):
        if ip in self._asn_cache:
            info_d = self._asn_cache[ip]
        else:
            info_d = asn_lookup(ip)
            self._asn_cache[ip] = info_d
        if info_d.get("asn"):
            self.st.upsert_wan_ip(ip, ts, asn=info_d["asn"], as_name=info_d["as_name"],
                                  cc=info_d["cc"])
            name = info_d.get("as_name") or ""
            with self.live.lock:
                if self.live.wan_ip == ip:
                    self.live.wan_label = f"AS{info_d['asn']} {name}".strip()
            self.event("wan_asn", "info",
                       f"{ip} belongs to AS{info_d['asn']} {name}".strip(), info_d)

    # -- DNS --------------------------------------------------------------- #
    def _dns_loop(self):
        self.stop.wait(2.0)
        idx = 0
        while not self.stop.is_set():
            started = time.monotonic()
            domain = self.cfg.domains[idx % len(self.cfg.domains)]
            idx += 1
            for name, server in self.cfg.resolvers:
                if self.stop.is_set():
                    break
                if server is None:
                    r = dns_query_system(domain)
                    proto = "system"
                else:
                    r = dns_query(server, domain, qtype="A", timeout=3.0)
                    proto = "udp"
                self.st.put("dns_samples", (
                    self.st.run_id, now_ts(), name, server or "os", proto, domain,
                    1 if r["ok"] else 0, r["rtt_ms"], r["rcode"],
                    ",".join(r["answers"][:4]) or None, r["err"]))
                with self.live.lock:
                    self.live.dns_last[name] = (r["ok"], r["rtt_ms"], r["err"] or r["rcode"])
                if not r["ok"]:
                    self.event("dns_fail", "warning",
                               f"DNS {name} failed for {domain}: {r['err'] or r['rcode']}",
                               {"resolver": name, "domain": domain})
            if idx % 4 == 1:
                self._dns_extra(domain)
            self.stop.wait(max(0.0, self.cfg.dns_interval - (time.monotonic() - started)))

    def _dns_extra(self, domain):
        """TCP/53, DoH and an NXDOMAIN hijack check."""
        r = dns_query("1.1.1.1", domain, qtype="A", timeout=4.0, proto="tcp")
        self.st.put("dns_samples", (self.st.run_id, now_ts(), "cloudflare", "1.1.1.1",
                                    "tcp", domain, 1 if r["ok"] else 0, r["rtt_ms"],
                                    r["rcode"], ",".join(r["answers"][:4]) or None,
                                    r["err"]))
        d = dns_query_doh("https://cloudflare-dns.com/dns-query", domain)
        self.st.put("dns_samples", (self.st.run_id, now_ts(), "cloudflare-doh",
                                    "cloudflare-dns.com", "doh", domain,
                                    1 if d["ok"] else 0, d["rtt_ms"], d["rcode"],
                                    ",".join(d["answers"][:4]) or None, d["err"]))
        bogus = f"nxdomain-{random.randint(10**9, 10**10)}.{APP}-probe.invalid"
        h = dns_query_system(bogus, timeout=5.0)
        hijacked = bool(h["ok"] and h["answers"])
        self.st.put("dns_samples", (self.st.run_id, now_ts(), "system", "os", "nxcheck",
                                    bogus, 1 if h["ok"] else 0, h["rtt_ms"],
                                    h["rcode"], ",".join(h["answers"][:4]) or None,
                                    h["err"]))
        if hijacked:
            self.event("dns_hijack", "warning",
                       f"resolver answers a non-existent domain with {h['answers'][0]}"
                       " — NXDOMAIN hijacking is on", {"answers": h["answers"]})

    # -- HTTP -------------------------------------------------------------- #
    def _http_loop(self):
        self.stop.wait(3.0)
        while not self.stop.is_set():
            started = time.monotonic()
            for url in self.cfg.urls:
                if self.stop.is_set():
                    break
                r = http_probe(url, timeout=10.0)
                self.st.put("http_samples", (
                    self.st.run_id, now_ts(), url, 1 if r["ok"] else 0, r["status"],
                    r["dns_ms"], r["tcp_ms"], r["tls_ms"], r["ttfb_ms"], r["total_ms"],
                    r["bytes"], r["tls_ver"], r["cert_days"], r["err"]))
                with self.live.lock:
                    self.live.http_last[url] = (r["ok"], r["ttfb_ms"], r["status"], r["err"])
                if "cdn-cgi/trace" in url and r["body"]:
                    self._on_trace_body(parse_cf_trace(r["body"]))
                if "generate_204" in url and r["status"] not in (204, None):
                    self.event("captive_portal", "critical",
                               f"{url} answered {r['status']} instead of 204 —"
                               " a captive portal or transparent proxy is intercepting",
                               {"url": url, "status": r["status"]})
                if not r["ok"] and r["err"]:
                    self.event("http_fail", "warning", f"HTTP {url}: {r['err']}",
                               {"url": url})
            self.stop.wait(max(0.0, self.cfg.http_interval - (time.monotonic() - started)))

    def _on_trace_body(self, tr):
        ip = tr.get("ip")
        colo = tr.get("colo")
        if not ip:
            return
        with self.live.lock:
            known = self.live.wan_ip
            prev_colo = self.live.link_last.get("colo")
            self.live.link_last["colo"] = colo
        if colo and prev_colo and colo != prev_colo:
            self.event("colo_change", "info",
                       f"CDN edge changed {prev_colo} → {colo} (often follows an uplink switch)",
                       {"from": prev_colo, "to": colo})
        if known and ip != known:
            # Record the observation — the analysis builds the uplink timeline from
            # these rows — but let the WAN watcher be the one that raises the event,
            # so a switch is never counted twice.
            self.st.put("wan_samples", (self.st.run_id, now_ts(), "cf-trace", ip, 1,
                                        None, None))
            self.event("wan_hint", "info",
                       f"the CDN sees this connection as {ip}, not {known}"
                       " — an uplink switch is in progress", {"ip": ip, "was": known})

    # -- speed ------------------------------------------------------------- #
    def _speed_loop(self):
        if not self.cfg.speed_interval:
            while not self.stop.is_set():
                if self.speed_now.wait(0.5):
                    self.speed_now.clear()
                    self._run_speed()
            return
        self.stop.wait(20.0)                    # let an idle latency baseline form
        while not self.stop.is_set():
            if not self.stop.is_set():
                self._run_speed()
            waited = 0.0
            while waited < self.cfg.speed_interval and not self.stop.is_set():
                if self.speed_now.wait(0.5):
                    self.speed_now.clear()
                    break
                waited += 0.5

    def _run_speed(self):
        def series(direction, mbps):
            self.st.put("speed_series", (self.st.run_id, now_ts(), direction, mbps))
            with self.live.lock:
                self.live.speed_running = f"{direction} {mbps:.1f} Mbps"

        for direction, fn in (("download", speed_download),
                              ("upload", speed_upload if self.cfg.speed_upload else None)):
            if fn is None or self.stop.is_set():
                continue
            t0 = now_ts()
            with self.live.lock:
                self.live.speed_running = f"{direction}…"
            try:
                r = fn(self.cfg, self.stop, on_series=series, log=self.log)
            except Exception as e:
                r = {"mbps": 0.0, "bytes": 0, "seconds": 0, "streams": 0,
                     "server": "", "capped": False, "window": 0,
                     "err": f"{type(e).__name__}: {e}"}
            t1 = now_ts()
            self.st.put("phases", (self.st.run_id, t0, t1, f"speed-{direction}"))
            self.st.put("speed_tests", (self.st.run_id, t0, t1, direction, r["bytes"],
                                        r["seconds"], r["mbps"], r["streams"],
                                        r["server"], r["err"]))
            with self.live.lock:
                self.live.speed_last[direction] = (r["mbps"], t1, r["err"])
                self.live.bytes_used += r["bytes"]
                self.live.speed_running = ""
            sev = "warning" if r["err"] else "info"
            self.event("speedtest", sev,
                       f"{direction}: {r['mbps']:.1f} Mbps ({fmt_bytes(r['bytes'])}"
                       f" in {r['seconds']:.1f}s)" + (f" — {r['err']}" if r["err"] else ""),
                       {"direction": direction, "mbps": r["mbps"]})
            if r.get("capped") and r.get("window", 99) < 4.0:
                self.event("speed_capped", "info",
                           f"the {fmt_bytes(self.cfg.speed_max_mb * 1024 * 1024)} data cap ended the "
                           f"{direction} test after {r['window']:.1f}s of measurement — raise "
                           f"--speed-max-mb for a more accurate figure on a link this fast",
                           {"direction": direction, "cap_mb": self.cfg.speed_max_mb})
            self.stop.wait(2.0)

    # -- path / link ------------------------------------------------------- #
    def _path_loop(self):
        self.stop.wait(4.0)
        if self.cfg.mtu_probe:
            v4 = [t["ip"] for t in self.targets
                  if t["family"] == socket.AF_INET and t["name"] not in ("gateway", "isp-hop")]
            mtu, note = path_mtu(v4[-1] if v4 else "1.1.1.1")
            with self.live.lock:
                self.live.mtu = mtu
            self.event("mtu", "info" if (mtu or 0) >= 1500 else "warning",
                       f"path MTU {mtu or 'unknown'} — {note}", {"mtu": mtu, "note": note})
        last_trace = 0.0
        last_ports = 0.0
        last_ntp = 0.0
        while not self.stop.is_set():
            now = time.monotonic()
            forced = self.trace_now.is_set()
            if forced:
                self.trace_now.clear()
            if self.cfg.trace_interval and (forced or now - last_trace >= self.cfg.trace_interval
                                            or last_trace == 0.0):
                last_trace = now
                self._do_trace()
            if self.cfg.port_interval and (now - last_ports >= self.cfg.port_interval
                                           or last_ports == 0.0):
                last_ports = now
                for host, port in self.cfg.ports:
                    if self.stop.is_set():
                        break
                    okp, ms, err = tcp_port_check(host, port, timeout=5.0)
                    self.st.put("port_checks", (self.st.run_id, now_ts(), host, port,
                                                1 if okp else 0, ms, err))
            if self.cfg.ntp_interval and (now - last_ntp >= self.cfg.ntp_interval
                                          or last_ntp == 0.0):
                last_ntp = now
                for server in self.cfg.ntp_servers:
                    if self.stop.is_set():
                        break
                    okn, offset, rtt, err = ntp_probe(server)
                    self.st.put("ntp_samples", (self.st.run_id, now_ts(), server,
                                                1 if okn else 0, offset, rtt, err))
            self.stop.wait(1.0)

    def _do_trace(self):
        target = "1.1.1.1"
        hops = traceroute(target, max_hops=18, timeout=2)
        ts = now_ts()
        for hop, ip, rtt in hops:
            self.st.put("trace_hops", (self.st.run_id, ts, target, hop, ip, rtt))
        path = [ip for _h, ip, _r in hops if ip]
        if self._last_trace is not None and path and path != self._last_trace:
            before = " → ".join(self._last_trace[:4])
            after = " → ".join(path[:4])
            self.event("route_change", "warning",
                       f"route to {target} changed: {before} ⇒ {after}",
                       {"before": self._last_trace, "after": path})
        if path:
            self._last_trace = path

    def _link_loop(self):
        sampler = LinkSampler(self.net.get("iface"))
        prev_carrier = None
        while not self.stop.is_set():
            started = time.monotonic()
            s = sampler.sample()
            self.st.put("iface_samples", (
                self.st.run_id, now_ts(), self.net.get("iface"), s["rx_mbps"], s["tx_mbps"],
                s["rx_err"], s["tx_err"], s["rx_drop"], s["tx_drop"],
                s["wifi_dbm"], s["wifi_qual"], s["carrier"], s["link_mbps"]))
            with self.live.lock:
                self.live.link_last.update(s)
            if s["carrier"] is not None and prev_carrier is not None \
                    and s["carrier"] != prev_carrier:
                self.event("carrier", "critical" if not s["carrier"] else "warning",
                           f"link carrier {'lost' if not s['carrier'] else 'restored'}"
                           f" on {self.net.get('iface')}", {"carrier": s["carrier"]})
            prev_carrier = s["carrier"]
            if (s["rx_err"] or 0) + (s["tx_err"] or 0) > 0:
                self.event("iface_errors", "warning",
                           f"interface errors: rx={s['rx_err']} tx={s['tx_err']}"
                           " — suspect cable, NIC or driver", s)
            self.stop.wait(max(0.0, self.cfg.link_interval - (time.monotonic() - started)))

    # -- lifecycle --------------------------------------------------------- #
    def start(self):
        engine = PingEngine(self.targets, self.cfg, self.on_ping, self.stop, self.log)
        self.ping_mode = engine.pick_mode()
        self.event("start", "info",
                   f"monitoring started — ping mode: {self.ping_mode}, "
                   f"{len(self.targets)} targets", {"mode": self.ping_mode})
        engine.start()
        self.threads.append(engine)
        loops = [("watchdog", self._watchdog), ("wan", self._wan_loop),
                 ("dns", self._dns_loop), ("http", self._http_loop),
                 ("path", self._path_loop), ("link", self._link_loop),
                 ("speed", self._speed_loop)]
        for name, fn in loops:
            th = threading.Thread(target=self._guard(fn, name), name=name, daemon=True)
            th.start()
            self.threads.append(th)

    def _guard(self, fn, name):
        def wrapper():
            try:
                fn()
            except Exception as e:
                self.event("thread_error", "warning", f"{name} probe stopped: {e}")
        return wrapper

    def shutdown(self, reason="finished"):
        self.stopped_reason = reason
        self.stop.set()
        for th in self.threads:
            th.join(timeout=6)
        with self.live.lock:
            if not self.live.online and self.live.down_since:
                dur = now_ts() - self.live.down_since
                self.live.outages += 1
                self.live.outage_total += dur
                self.live.outage_max = max(self.live.outage_max, dur)
        self.event("stop", "info", f"monitoring stopped ({reason})")


# --------------------------------------------------------------------------- #
# live dashboard
# --------------------------------------------------------------------------- #
def _rtt_color(ms):
    if ms is None:
        return GREY
    if ms < 30:
        return GREEN
    if ms < 80:
        return CYAN
    if ms < 150:
        return YELLOW
    return RED


def _loss_color(pct):
    if pct <= 0.01:
        return GREEN
    if pct < 1:
        return CYAN
    if pct < 5:
        return YELLOW
    return RED


def dashboard_lines(mon, deadline, width):
    cfg, live = mon.cfg, mon.live
    now = now_ts()
    elapsed = now - live.started
    total = cfg.duration
    lines = []

    head = (f"{BOLD}{WHITE}{APP} {VERSION}{C0} {GREY}·{C0} live network capture"
            f"   {GREY}{ts_str(now)}{C0}")
    if total:
        frac = min(1.0, elapsed / total)
        prog = (f"{fmt_hms(elapsed)} / {fmt_hms(total)}  {BLUE}{bar(frac, 24)}{C0} "
                f"{frac * 100:5.1f}%   left {fmt_hms(total - elapsed)}")
    else:
        prog = f"{fmt_hms(elapsed)} elapsed  {GREY}(no time limit — press q to finish){C0}"
    lines += box("session", [head, prog], width)

    with live.lock:
        online = live.online
        outages, omax, ototal = live.outages, live.outage_max, live.outage_total
        wan_ip, wan_label, wan_switches = live.wan_ip, live.wan_label, live.wan_switches
        wan_since, wan_fail = live.wan_since, live.wan_fail
        samples = live.samples
        tstats = [(t.name, t.host, t.last_rtt, t.loss_pct, t.last_ttl,
                   list(t.recent), t.stats(), t.sent, t.recv)
                  for t in live.targets.values()]
        dns_last = dict(live.dns_last)
        http_last = dict(live.http_last)
        speed_last = dict(live.speed_last)
        speed_running = live.speed_running
        link = dict(live.link_last)
        mtu = live.mtu
        used = live.bytes_used
        events = list(live.events)[-8:]

    uptime = 100.0 * (elapsed - ototal) / elapsed if elapsed > 0 else 100.0
    state = f"{GREEN}● ONLINE {C0}" if online else f"{RED}● OFFLINE{C0}"
    status = (f"{state}  uptime {_loss_color(100 - uptime)}{uptime:6.3f}%{C0}"
              f"   outages {outages}"
              f"   worst {fmt_dur(omax) if omax else '—'}"
              f"   probes {samples}")
    wan_line = (f"{GREY}WAN{C0} {WHITE}{wan_ip or '…'}{C0} "
                f"{MAGENTA}{wan_label or ''}{C0}"
                f"   switches {YELLOW if wan_switches else GREY}{wan_switches}{C0}"
                f"   on this uplink {fmt_hms(now - wan_since) if wan_since else '—'}"
                + (f"   {GREY}lookup fails {wan_fail}{C0}" if wan_fail else ""))
    extra = []
    if mtu:
        extra.append(f"MTU {mtu}")
    if link.get("wifi_dbm") is not None:
        dbm = link["wifi_dbm"]
        col = GREEN if dbm > -60 else (YELLOW if dbm > -72 else RED)
        extra.append(f"Wi-Fi {col}{dbm:.0f} dBm{C0}")
    if link.get("rx_mbps") is not None:
        extra.append(f"link ↓{link['rx_mbps']:.1f} ↑{link['tx_mbps']:.1f} Mbps")
    if link.get("colo"):
        extra.append(f"edge {link['colo']}")
    if used:
        extra.append(f"data used {fmt_bytes(used)}")
    lines += box("status", [status, wan_line, "   ".join(extra) or f"{GREY}—{C0}"], width)

    spark_w = max(10, width - 60)
    def ms(v):
        return f"{v:.1f}ms" if v is not None else "—"

    rows = [f"{GREY}{'target':<12}{'last':>10}{'avg':>9}{'p95':>9}{'loss':>8}"
            f"{'ttl':>6}  recent{C0}"]
    for name, host, last, loss, ttl, recent, (avg, p95), s_, r_ in tstats:
        rows.append(
            f"{WHITE}{name:<12}{C0}"
            f"{_rtt_color(last)}{ms(last):>10}{C0}"
            f"{ms(avg):>9}{ms(p95):>9}"
            f"{_loss_color(loss)}{loss:>7.2f}%{C0}"
            f"{GREY}{(ttl if ttl else '—'):>6}{C0}  "
            f"{_rtt_color(last)}{sparkline(recent, width=spark_w)}{C0}")
    lines += box("targets — one ICMP probe per second", rows, width)

    svc = []
    dns_bits = []
    for name, (okd, ms, note) in list(dns_last.items())[:6]:
        col = GREEN if okd else RED
        dns_bits.append(f"{col}{name}{C0} {fmt_ms(ms, 0)}ms" if okd else f"{col}{name} ✗{C0}")
    svc.append(f"{GREY}DNS {C0}" + "  ".join(dns_bits) if dns_bits else f"{GREY}DNS  —{C0}")
    http_bits = []
    for url, (okh, ttfb, status_code, err) in list(http_last.items())[:5]:
        short = urllib.parse.urlsplit(url).hostname or url
        col = GREEN if okh else RED
        http_bits.append(f"{col}{short}{C0} {fmt_ms(ttfb, 0)}ms" if okh
                         else f"{col}{short} ✗{C0}")
    svc.append(f"{GREY}HTTP{C0} " + "  ".join(http_bits) if http_bits else f"{GREY}HTTP —{C0}")
    dl = speed_last.get("download")
    ul = speed_last.get("upload")
    speed_txt = []
    if dl:
        speed_txt.append(f"↓ {BOLD}{dl[0]:.1f}{C0} Mbps {GREY}({ts_str(dl[1], False)}){C0}")
    if ul:
        speed_txt.append(f"↑ {BOLD}{ul[0]:.1f}{C0} Mbps {GREY}({ts_str(ul[1], False)}){C0}")
    if speed_running:
        speed_txt.append(f"{YELLOW}running: {speed_running}{C0}")
    if cfg.plan_mbps and dl:
        pct = dl[0] / cfg.plan_mbps * 100
        col = GREEN if pct >= 80 else (YELLOW if pct >= 50 else RED)
        speed_txt.append(f"{col}{pct:.0f}% of plan{C0}")
    svc.append(f"{GREY}SPD {C0}" + "   ".join(speed_txt) if speed_txt
               else f"{GREY}SPD  no speed test yet{C0}")
    lines += box("services", svc, width)

    ev_rows = []
    for ts, kind, sev, message in reversed(events):
        col = SEV_COLOR.get(sev, GREY)
        ev_rows.append(f"{GREY}{ts_str(ts, False)}{C0} {col}{kind:<14}{C0} {message}")
    if not ev_rows:
        ev_rows = [f"{GREY}no events yet — that is good news{C0}"]
    lines += box("events", ev_rows, width)

    lines.append(f"{GREY} q{C0} finish + build report   {GREY}s{C0} speed test now   "
                 f"{GREY}t{C0} traceroute now   {GREY}Ctrl-C{C0} same as q")
    return lines


def run_dashboard(mon, deadline):
    screen = Screen()
    screen.enter()
    try:
        with KeyReader() as keys:
            while not mon.stop.is_set():
                width, _h = term_size()
                screen.paint(dashboard_lines(mon, deadline, width - 1))
                if deadline and time.monotonic() >= deadline:
                    mon.stopped_reason = "duration reached"
                    break
                key = keys.get(0.5)
                if key in ("q", "Q"):
                    mon.stopped_reason = "stopped by user"
                    break
                if key in ("s", "S"):
                    mon.speed_now.set()
                if key in ("t", "T"):
                    mon.trace_now.set()
    finally:
        screen.leave()


def run_plain(mon, deadline):
    last = 0.0
    while not mon.stop.is_set():
        if deadline and time.monotonic() >= deadline:
            mon.stopped_reason = "duration reached"
            break
        now = time.monotonic()
        if now - last >= 10.0:
            live = mon.live
            anchors = list(mon.internet)
            with live.lock:
                sent = sum(live.targets[n].sent for n in anchors if n in live.targets)
                recv = sum(live.targets[n].recv for n in anchors if n in live.targets)
                online, outages = live.online, live.outages
                wan = live.wan_ip
            loss = 0.0 if not sent else (sent - recv) * 100.0 / sent
            elapsed = now_ts() - mon.live.started
            info(f"[{fmt_hms(elapsed)}] {'online' if online else 'OFFLINE'} "
                 f"probes={sent} loss={loss:.2f}% outages={outages} wan={wan or '—'}")
            last = now
        time.sleep(0.5)


# --------------------------------------------------------------------------- #
# capture orchestration
# --------------------------------------------------------------------------- #
def discover_network(cfg):
    """Everything we need to know before the first probe leaves the machine."""
    gw, iface = default_gateway()
    net = {"gateway": gw, "iface": iface, "local_ip": local_ip_for(),
           "isp_hop": None, "lan_hop": None, "targets": [], "tracer": None, "wifi": ""}
    targets = []
    if gw:
        targets.append({"name": "gateway", "host": gw, "ip": gw,
                        "family": socket.AF_INET, "role": "gateway"})
    # Split the first hops by ownership: another RFC1918 router after the gateway is
    # still yours (a second router, a load balancer); the first public or CGNAT hop
    # is where the provider's network begins. Watching both separates "my kit broke"
    # from "their line broke".
    hops = traceroute("1.1.1.1", max_hops=6, timeout=1)
    for hop, ip, _rtt in hops:
        if not ip or ip == gw:
            continue
        if is_private_ip(ip):
            if net["lan_hop"] is None:
                net["lan_hop"] = ip
                targets.append({"name": "lan-hop", "host": ip, "ip": ip,
                                "family": socket.AF_INET, "role": "lan"})
            continue
        net["isp_hop"] = ip
        targets.append({"name": "isp-edge", "host": ip, "ip": ip,
                        "family": socket.AF_INET, "role": "isp"})
        break
    net["tracer"] = "traceroute" if hops else None
    for name, host in DEFAULT_ANCHORS:
        ip = resolve_host(host)
        targets.append({"name": name, "host": host, "ip": ip,
                        "family": socket.AF_INET, "role": "anchor"})
    for name, host in cfg.extra_targets:
        ip = resolve_host(host)
        targets.append({"name": name, "host": host, "ip": ip,
                        "family": socket.AF_INET, "role": "custom"})
    net["ipv6_local"] = local_ip6()
    if cfg.ipv6 and net["ipv6_local"]:
        ip6 = resolve_host("2606:4700:4700::1111", socket.AF_INET6)
        probe6 = IcmpSocket(socket.AF_INET6)
        if ip6 and probe6.open():
            probe6.close()
            targets.append({"name": "ipv6", "host": "2606:4700:4700::1111",
                            "ip": ip6, "family": socket.AF_INET6, "role": "anchor6"})
    net["targets"] = [t for t in targets if t["ip"]]
    if iface:
        _dbm, _q, essid = wifi_info(iface)
        net["wifi"] = essid
    return net


def run_capture(cfg):
    """Run one full capture; returns (db_path, run_id, out_dir)."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = cfg.out_dir or os.path.join(os.getcwd(), f"{APP}-{stamp}")
    os.makedirs(out_dir, exist_ok=True)
    db_path = cfg.db_path or os.path.join(out_dir, f"{APP}.db")

    info("Discovering the local network…")
    net = discover_network(cfg)
    if not net["targets"]:
        die("No reachable probe targets could be resolved — is this machine online at all?")
    info(f"Gateway {net['gateway'] or 'unknown'} on {net['iface'] or '?'}"
         + (f", ISP first hop {net['isp_hop']}" if net["isp_hop"] else "")
         + (f", Wi-Fi {net['wifi']}" if net["wifi"] else ""))

    storage = Storage(db_path)
    storage.open()
    storage.start_run(cfg, host_info(), net)
    mon = Monitor(cfg, storage, net)

    deadline = (time.monotonic() + cfg.duration) if cfg.duration else 0
    mon.start()
    info(f"Ping mode: {mon.ping_mode}   database: {db_path}")
    if mon.ping_mode == "tcp":
        warn("ICMP is unavailable here — falling back to TCP connect probes. "
             "Run as root (or allow unprivileged ping) for real ICMP timing.")

    interrupted = False
    try:
        if cfg.tui and _TTY:
            run_dashboard(mon, deadline)
        else:
            run_plain(mon, deadline)
    except KeyboardInterrupt:
        interrupted = True
        mon.stopped_reason = "interrupted"
    finally:
        # Shutting down and flushing the database must not be interruptible.
        old = None
        try:
            old = signal.signal(signal.SIGINT, signal.SIG_IGN)
        except Exception:
            pass
        info("Stopping probes and flushing the database…")
        mon.shutdown(mon.stopped_reason or ("interrupted" if interrupted else "finished"))
        storage.finish("interrupted" if interrupted else "finished")
        if old is not None:
            try:
                signal.signal(signal.SIGINT, old)
            except Exception:
                pass
    ok(f"Capture complete — {storage.written} rows written to {db_path}")
    if storage.dropped:
        warn(f"{storage.dropped} samples were dropped because the write queue was full.")
    return db_path, storage.run_id, out_dir


# --------------------------------------------------------------------------- #
# analysis
# --------------------------------------------------------------------------- #
RTT_CAP = 400000          # per target; beyond this the series is decimated


class Decimator:
    """Keeps a bounded, distribution-preserving sample of a long series."""

    def __init__(self, cap=RTT_CAP):
        self.cap = cap
        self.vals = []
        self.stride = 1
        self._n = 0

    def add(self, v):
        self._n += 1
        if self._n % self.stride:
            return
        self.vals.append(v)
        if len(self.vals) > self.cap:
            self.vals = self.vals[::2]
            self.stride *= 2

    def sorted(self):
        return sorted(self.vals)


def pearson(xs, ys):
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    if len(pairs) < 5:
        return None
    xs2 = [p[0] for p in pairs]
    ys2 = [p[1] for p in pairs]
    mx, my = sum(xs2) / len(xs2), sum(ys2) / len(ys2)
    num = sum((x - mx) * (y - my) for x, y in pairs)
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs2))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys2))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def mos_score(rtt_ms, jitter_ms, loss_pct):
    """ITU-T G.107 E-model approximation — 'how good is this for calls/gaming'."""
    if rtt_ms is None:
        return None
    eff = rtt_ms / 2.0 + (jitter_ms or 0) * 2.0 + 10.0
    r = 93.2 - ((eff - 120) / 10.0 if eff > 160 else eff / 40.0)
    r -= (loss_pct or 0) * 2.5
    r = max(0.0, min(100.0, r))
    mos = 1 + 0.035 * r + 7e-6 * r * (r - 60) * (100 - r)
    return max(1.0, min(4.5, mos))


def _q(conn, sql, args=()):
    try:
        return conn.execute(sql, args).fetchall()
    except sqlite3.Error:
        return []


def analyze(db_path, run_id=None):
    """Read a capture back out of SQLite and turn it into a full picture."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    A = {"db": db_path}

    runs = _q(conn, "SELECT * FROM runs ORDER BY id")
    if not runs:
        raise SystemExit(f"No runs found in {db_path}")
    row = next((r for r in runs if r["id"] == run_id), runs[-1])
    run_id = row["id"]
    A["run_id"] = run_id
    A["label"] = row["label"] or ""
    A["started"] = row["started_at"]
    A["ended"] = row["ended_at"] or row["started_at"]
    A["status"] = row["status"]
    A["config"] = json.loads(row["config_json"] or "{}")
    A["host"] = json.loads(row["host_json"] or "{}")
    A["net"] = json.loads(row["net_json"] or "{}")
    cfg = Config.from_json(A["config"])

    span = _q(conn, "SELECT MIN(ts) AS lo, MAX(ts) AS hi FROM ping_samples WHERE run_id=?",
              (run_id,))
    if span and span[0]["hi"]:
        A["started"] = min(A["started"] or span[0]["lo"], span[0]["lo"])
        A["ended"] = max(A["ended"] or 0, span[0]["hi"])
    A["duration"] = max(0.0, (A["ended"] or 0) - (A["started"] or 0))

    # ---- single pass over every ICMP sample -------------------------------- #
    targets = {}
    # Roles come from the capture itself, so availability is decided by the public
    # anchors alone — the gateway and the first hops answer even when the line is dead.
    roles = {t["name"]: t.get("role", "anchor")
             for t in (A["net"].get("targets") or []) if t.get("name")}
    legacy = {"gateway": "gateway", "isp-hop": "isp", "lan-hop": "lan",
              "isp-edge": "isp", "ipv6": "anchor6"}

    def role_of(name):
        if name not in roles:
            roles[name] = legacy.get(name, "anchor")
        return roles[name]

    slots = {}            # int(ts) -> [anchor_n, anchor_ok, gateway_n, gateway_ok]
    minutes = {}          # (target, minute) -> [n, ok, sum, max, list-ish]
    prev_rtt = {}
    interval = float(A["config"].get("ping_interval") or 1.0)
    cur = conn.execute("SELECT ts, target, ok, rtt_ms, ttl FROM ping_samples"
                       " WHERE run_id=? ORDER BY ts", (run_id,))
    for ts, target, okv, rtt, ttl in cur:
        t = targets.get(target)
        if t is None:
            t = targets[target] = {
                "name": target, "sent": 0, "recv": 0, "rtts": Decimator(),
                "sum": 0.0, "sumsq": 0.0, "min": None, "max": None,
                "jit_sum": 0.0, "jit_n": 0, "streak": 0, "max_streak": 0,
                "episodes": 0, "ttl": {}, "first": ts, "last": ts}
        t["sent"] += 1
        t["last"] = ts
        minute = int(ts // 60) * 60
        mk = (target, minute)
        m = minutes.get(mk)
        if m is None:
            m = minutes[mk] = [0, 0, 0.0, None]
        m[0] += 1
        if okv:
            t["recv"] += 1
            t["rtts"].add(rtt)
            t["sum"] += rtt
            t["sumsq"] += rtt * rtt
            t["min"] = rtt if t["min"] is None else min(t["min"], rtt)
            t["max"] = rtt if t["max"] is None else max(t["max"], rtt)
            pr = prev_rtt.get(target)
            if pr is not None:
                t["jit_sum"] += abs(rtt - pr)
                t["jit_n"] += 1
            prev_rtt[target] = rtt
            if t["streak"]:
                t["streak"] = 0
            if ttl:
                t["ttl"][ttl] = t["ttl"].get(ttl, 0) + 1
            m[1] += 1
            m[2] += rtt
            m[3] = rtt if m[3] is None else max(m[3], rtt)
        else:
            prev_rtt.pop(target, None)
            if t["streak"] == 0:
                t["episodes"] += 1
            t["streak"] += 1
            t["max_streak"] = max(t["max_streak"], t["streak"])
        role = role_of(target)
        if role in ("gateway", "anchor", "custom"):
            sl = slots.get(int(ts))
            if sl is None:
                sl = slots[int(ts)] = [0, 0, 0, 0]
            if role == "gateway":
                sl[2] += 1
                sl[3] += 1 if okv else 0
            else:
                sl[0] += 1
                sl[1] += 1 if okv else 0

    for t in targets.values():
        vals = t["rtts"].sorted()
        n_ok = t["recv"]
        t["loss_pct"] = 0.0 if not t["sent"] else (t["sent"] - n_ok) * 100.0 / t["sent"]
        t["avg"] = (t["sum"] / n_ok) if n_ok else None
        t["p50"] = percentile(vals, 50)
        t["p90"] = percentile(vals, 90)
        t["p95"] = percentile(vals, 95)
        t["p99"] = percentile(vals, 99)
        t["jitter"] = (t["jit_sum"] / t["jit_n"]) if t["jit_n"] else None
        if n_ok > 1:
            var = max(0.0, t["sumsq"] / n_ok - (t["sum"] / n_ok) ** 2)
            t["stdev"] = math.sqrt(var)
        else:
            t["stdev"] = None
        t["max_gap_s"] = t["max_streak"] * interval
        t["mos"] = mos_score(t["avg"], t["jitter"], t["loss_pct"])
        t["ttl_main"] = max(t["ttl"], key=t["ttl"].get) if t["ttl"] else None
        t["ttl_changes"] = max(0, len(t["ttl"]) - 1)
    A["targets"] = targets
    A["ping_mode"] = A["net"].get("ping_mode", "")

    # ---- availability, outages, brownouts ---------------------------------- #
    for name in targets:
        role_of(name)
    A["roles"] = roles
    inet_names = [n for n in targets if roles.get(n) in ("anchor", "custom")]
    up = down = unknown = 0
    outages = []
    cur_out = None
    if slots:
        s0, s1 = min(slots), max(slots)
        for sec in range(s0, s1 + 1):
            sl = slots.get(sec)
            if not sl or sl[0] == 0:
                state = "unknown"
            elif sl[1] > 0:
                state = "up"
            else:
                state = "down"
            if state == "up":
                up += 1
                if cur_out:
                    cur_out["end"] = sec
                    cur_out["seconds"] = cur_out["end"] - cur_out["start"]
                    if cur_out["seconds"] >= max(1, int(cfg.outage_ticks)):
                        outages.append(cur_out)
                    cur_out = None
            elif state == "down":
                down += 1
                if not cur_out:
                    gw_state = "unknown"
                    if sl[2]:
                        gw_state = "up" if sl[3] else "down"
                    cur_out = {"start": sec, "end": sec, "seconds": 0,
                               "gateway": gw_state}
                else:
                    if sl[2] and not sl[3]:
                        cur_out["gateway"] = "down"
            else:
                unknown += 1
                if cur_out:
                    cur_out["end"] = sec           # keep the outage open across a gap
        if cur_out:
            cur_out["end"] = s1 + 1
            cur_out["seconds"] = cur_out["end"] - cur_out["start"]
            if cur_out["seconds"] >= max(1, int(cfg.outage_ticks)):
                outages.append(cur_out)
    total_slots = up + down
    A["uptime_pct"] = (100.0 * up / total_slots) if total_slots else None
    A["slots"] = {"up": up, "down": down, "unknown": unknown}
    A["outages"] = outages
    A["outage_total_s"] = sum(o["seconds"] for o in outages)
    A["outage_max_s"] = max([o["seconds"] for o in outages], default=0)
    A["mttr_s"] = (A["outage_total_s"] / len(outages)) if outages else 0.0

    # ---- per-minute series for the charts ---------------------------------- #
    all_minutes = sorted({k[1] for k in minutes})
    A["minutes"] = all_minutes
    series = {}
    for name in targets:
        rtt_line, loss_line = [], []
        for minute in all_minutes:
            m = minutes.get((name, minute))
            if not m or m[0] == 0:
                rtt_line.append((minute, None))
                loss_line.append((minute, None))
                continue
            rtt_line.append((minute, (m[2] / m[1]) if m[1] else None))
            loss_line.append((minute, (m[0] - m[1]) * 100.0 / m[0]))
        series[name] = {"rtt": rtt_line, "loss": loss_line}
    A["series"] = series

    global_loss, global_rtt, quality = [], [], []
    for minute in all_minutes:
        n = ok_n = 0
        rsum = 0.0
        rn = 0
        for name in inet_names:
            m = minutes.get((name, minute))
            if not m:
                continue
            n += m[0]
            ok_n += m[1]
            rsum += m[2]
            rn += m[1]
        loss = ((n - ok_n) * 100.0 / n) if n else None
        global_loss.append((minute, loss))
        global_rtt.append((minute, (rsum / rn) if rn else None))
        quality.append((minute, (ok_n / n) if n else None))
    A["global_loss"] = global_loss
    A["global_rtt"] = global_rtt
    A["quality"] = quality

    ok_rtts = [v for _t, v in global_rtt if v is not None]
    A["baseline_rtt"] = percentile(sorted(ok_rtts), 25) if ok_rtts else None
    base = A["baseline_rtt"] or 0
    A["brownouts"] = [
        {"minute": mn, "loss": ls,
         "rtt": next((v for t2, v in global_rtt if t2 == mn), None)}
        for mn, ls in global_loss
        if ls is not None and (ls >= 2.0 or (base and (
            next((v for t2, v in global_rtt if t2 == mn), 0) or 0) > base * 3))]

    # ---- hour-of-day heatmap ----------------------------------------------- #
    hours = {}
    for minute, loss in global_loss:
        h = datetime.fromtimestamp(minute).hour
        rec = hours.setdefault(h, [0, 0.0, 0, 0.0])
        if loss is not None:
            rec[0] += 1
            rec[1] += loss
    for minute, rtt in global_rtt:
        h = datetime.fromtimestamp(minute).hour
        rec = hours.setdefault(h, [0, 0.0, 0, 0.0])
        if rtt is not None:
            rec[2] += 1
            rec[3] += rtt
    A["hours"] = {h: {"loss": (r[1] / r[0]) if r[0] else None,
                      "rtt": (r[3] / r[2]) if r[2] else None,
                      "minutes": r[0]} for h, r in sorted(hours.items())}

    # ---- outage periodicity ------------------------------------------------ #
    A["periodic"] = None
    if len(outages) >= 3:
        gaps = [outages[i + 1]["start"] - outages[i]["start"] for i in range(len(outages) - 1)]
        gm, gs = mean(gaps), stdev(gaps)
        if gm and gs is not None and gm > 20 and gs / gm < 0.25:
            A["periodic"] = {"period_s": gm, "cv": gs / gm, "count": len(outages)}

    # ---- WAN / failover ----------------------------------------------------- #
    A.update(_analyze_wan(conn, run_id, A, targets, interval))

    # ---- DNS ---------------------------------------------------------------- #
    dns = {}
    for r in _q(conn, "SELECT resolver, proto, ok, rtt_ms, rcode, err FROM dns_samples"
                      " WHERE run_id=? AND proto!='nxcheck'", (run_id,)):
        key = (r["resolver"], r["proto"])
        d = dns.setdefault(key, {"resolver": r["resolver"], "proto": r["proto"],
                                 "n": 0, "ok": 0, "rtts": [], "errors": {}})
        d["n"] += 1
        if r["ok"]:
            d["ok"] += 1
            if r["rtt_ms"] is not None:
                d["rtts"].append(r["rtt_ms"])
        else:
            key2 = (r["err"] or r["rcode"] or "error").split(":")[0]
            d["errors"][key2] = d["errors"].get(key2, 0) + 1
    for d in dns.values():
        vals = sorted(d["rtts"])
        d["p50"] = percentile(vals, 50)
        d["p95"] = percentile(vals, 95)
        d["max"] = vals[-1] if vals else None
        d["fail_pct"] = 0.0 if not d["n"] else (d["n"] - d["ok"]) * 100.0 / d["n"]
    A["dns"] = dns
    nx = _q(conn, "SELECT ok, answer FROM dns_samples WHERE run_id=? AND proto='nxcheck'",
            (run_id,))
    A["dns_hijack"] = [r["answer"] for r in nx if r["ok"] and r["answer"]]
    A["dns_nxchecks"] = len(nx)

    # ---- HTTP --------------------------------------------------------------- #
    http = {}
    for r in _q(conn, "SELECT url, ok, status, dns_ms, tcp_ms, tls_ms, ttfb_ms,"
                      " total_ms, tls_ver, cert_days, err FROM http_samples"
                      " WHERE run_id=?", (run_id,)):
        h = http.setdefault(r["url"], {"url": r["url"], "n": 0, "ok": 0, "ttfb": [],
                                       "total": [], "phases": [0.0, 0.0, 0.0, 0.0, 0],
                                       "status": {}, "errors": {}, "tls": set(),
                                       "cert_days": None})
        h["n"] += 1
        if r["ok"]:
            h["ok"] += 1
            if r["ttfb_ms"] is not None:
                h["ttfb"].append(r["ttfb_ms"])
            if r["total_ms"] is not None:
                h["total"].append(r["total_ms"])
            ph = h["phases"]
            ph[0] += r["dns_ms"] or 0
            ph[1] += r["tcp_ms"] or 0
            ph[2] += r["tls_ms"] or 0
            ph[3] += max(0.0, (r["ttfb_ms"] or 0))
            ph[4] += 1
            if r["tls_ver"]:
                h["tls"].add(r["tls_ver"])
            if r["cert_days"] is not None:
                h["cert_days"] = r["cert_days"]
        else:
            k = (r["err"] or f"HTTP {r['status']}").split(":")[0]
            h["errors"][k] = h["errors"].get(k, 0) + 1
        if r["status"]:
            h["status"][r["status"]] = h["status"].get(r["status"], 0) + 1
    for h in http.values():
        h["ok_pct"] = (100.0 * h["ok"] / h["n"]) if h["n"] else None
        h["ttfb_p50"] = percentile(sorted(h["ttfb"]), 50)
        h["ttfb_p95"] = percentile(sorted(h["ttfb"]), 95)
        h["total_p50"] = percentile(sorted(h["total"]), 50)
        n = h["phases"][4] or 1
        h["phase_avg"] = [h["phases"][i] / n for i in range(4)]
        h["tls"] = sorted(h["tls"])
    A["http"] = http

    # ---- speed + bufferbloat ------------------------------------------------ #
    speed = {"download": [], "upload": []}
    for r in _q(conn, "SELECT ts_start, ts_end, direction, bytes, seconds, mbps, streams,"
                      " server, err FROM speed_tests WHERE run_id=? ORDER BY ts_start",
                (run_id,)):
        speed.setdefault(r["direction"], []).append(dict(r))
    A["speed"] = speed
    A["speed_stats"] = {}
    for direction, rows in speed.items():
        vals = sorted([r["mbps"] for r in rows if r["mbps"] and not r["err"]])
        if vals:
            A["speed_stats"][direction] = {
                "n": len(vals), "avg": mean(vals), "min": vals[0], "max": vals[-1],
                "p50": percentile(vals, 50), "p10": percentile(vals, 10),
                "cv": (stdev(vals) / mean(vals)) if len(vals) > 1 and mean(vals) else None}
    A["speed_series"] = [(r["ts"], r["direction"], r["mbps"]) for r in
                         _q(conn, "SELECT ts, direction, mbps FROM speed_series"
                                  " WHERE run_id=? ORDER BY ts", (run_id,))]
    A["total_bytes"] = sum(r["bytes"] or 0 for rows in speed.values() for r in rows)

    phases = [(r["ts_start"], r["ts_end"], r["kind"]) for r in
              _q(conn, "SELECT ts_start, ts_end, kind FROM phases WHERE run_id=?", (run_id,))]
    A["phases"] = phases
    A["bufferbloat"] = _bufferbloat(conn, run_id, phases, inet_names)
    A["anchor_names"] = inet_names

    # ---- ports / NTP / route / link ----------------------------------------- #
    ports = {}
    for r in _q(conn, "SELECT host, port, ok, ms, err FROM port_checks WHERE run_id=?",
                (run_id,)):
        p = ports.setdefault((r["host"], r["port"]),
                             {"host": r["host"], "port": r["port"], "n": 0,
                              "ok": 0, "ms": [], "err": {}})
        p["n"] += 1
        if r["ok"]:
            p["ok"] += 1
            p["ms"].append(r["ms"])
        else:
            p["err"][r["err"] or "error"] = p["err"].get(r["err"] or "error", 0) + 1
    for p in ports.values():
        p["ok_pct"] = 100.0 * p["ok"] / p["n"] if p["n"] else None
        p["avg_ms"] = mean(p["ms"])
    A["ports"] = ports

    ntp = _q(conn, "SELECT server, ok, offset_ms, rtt_ms FROM ntp_samples WHERE run_id=?",
             (run_id,))
    A["ntp"] = {"n": len(ntp), "ok": sum(1 for r in ntp if r["ok"]),
                "offset": mean([r["offset_ms"] for r in ntp if r["ok"]]),
                "rtt": mean([r["rtt_ms"] for r in ntp if r["ok"]])}

    traces = {}
    for r in _q(conn, "SELECT ts, target, hop, ip, rtt_ms FROM trace_hops"
                      " WHERE run_id=? ORDER BY ts, hop", (run_id,)):
        traces.setdefault(r["ts"], []).append((r["hop"], r["ip"], r["rtt_ms"]))
    A["traces"] = traces
    paths = []
    for ts in sorted(traces):
        path = tuple(ip for _h, ip, _r in traces[ts] if ip)
        if not paths or paths[-1][1] != path:
            paths.append((ts, path))
    A["paths"] = paths

    link_rows = _q(conn, "SELECT ts, rx_mbps, tx_mbps, rx_err, tx_err, rx_drop, tx_drop,"
                         " wifi_dbm, carrier, link_mbps FROM iface_samples"
                         " WHERE run_id=? ORDER BY ts", (run_id,))
    A["link"] = {
        "rows": [(r["ts"], r["rx_mbps"], r["tx_mbps"], r["wifi_dbm"]) for r in link_rows],
        "rx_err": sum(r["rx_err"] or 0 for r in link_rows),
        "tx_err": sum(r["tx_err"] or 0 for r in link_rows),
        "rx_drop": sum(r["rx_drop"] or 0 for r in link_rows),
        "tx_drop": sum(r["tx_drop"] or 0 for r in link_rows),
        "wifi": [r["wifi_dbm"] for r in link_rows if r["wifi_dbm"] is not None],
        "carrier_drops": sum(1 for i, r in enumerate(link_rows)
                             if r["carrier"] == 0 and i and link_rows[i - 1]["carrier"] == 1),
        "link_mbps": next((r["link_mbps"] for r in reversed(link_rows) if r["link_mbps"]), None),
        "peak_rx": max([r["rx_mbps"] or 0 for r in link_rows], default=0),
        "peak_tx": max([r["tx_mbps"] or 0 for r in link_rows], default=0),
    }
    # Does the Wi-Fi signal explain the packet loss?
    if A["link"]["wifi"]:
        by_min_sig, by_min_loss = {}, dict(global_loss)
        for r in link_rows:
            if r["wifi_dbm"] is None:
                continue
            minute = int(r["ts"] // 60) * 60
            by_min_sig.setdefault(minute, []).append(r["wifi_dbm"])
        xs, ys = [], []
        for minute, vals in by_min_sig.items():
            if by_min_loss.get(minute) is not None:
                xs.append(sum(vals) / len(vals))
                ys.append(by_min_loss[minute])
        A["link"]["wifi_loss_r"] = pearson(xs, ys)
    else:
        A["link"]["wifi_loss_r"] = None

    A["events"] = [dict(r) for r in _q(
        conn, "SELECT ts, kind, severity, message, details FROM events"
              " WHERE run_id=? ORDER BY ts", (run_id,))]
    A["event_counts"] = collections.Counter(e["kind"] for e in A["events"])
    A["mtu"] = next((json.loads(e["details"] or "{}").get("mtu")
                     for e in A["events"] if e["kind"] == "mtu"), None)
    A["rows"] = {}
    for table in ("ping_samples", "wan_samples", "dns_samples", "http_samples",
                  "speed_tests", "speed_series", "trace_hops", "iface_samples",
                  "port_checks", "ntp_samples", "events"):
        got = _q(conn, f"SELECT COUNT(*) AS c FROM {table} WHERE run_id=?", (run_id,))
        A["rows"][table] = got[0]["c"] if got else 0

    A["score"], A["score_parts"] = _score(A)
    A["grade"] = grade_for(A["score"])
    A["findings"] = _findings(A, cfg)
    conn.close()
    return A


def _bufferbloat(conn, run_id, phases, inet_names):
    """Latency while the link is saturated versus latency while it is idle."""
    down = [(a, b) for a, b, kind in phases if kind == "speed-download"]
    up = [(a, b) for a, b, kind in phases if kind == "speed-upload"]
    out = {"idle_p50": None, "down_p50": None, "up_p50": None,
           "delta_down": None, "delta_up": None, "grade": None, "n": len(down) + len(up)}
    if not inet_names:
        return out
    placeholders = ",".join("?" * len(inet_names))
    rows = _q(conn, f"SELECT ts, rtt_ms FROM ping_samples WHERE run_id=? AND ok=1"
                    f" AND target IN ({placeholders})", (run_id, *inet_names))
    if not rows:
        return out
    busy = down + up
    idle, in_down, in_up = [], [], []
    for r in rows:
        ts, rtt = r["ts"], r["rtt_ms"]
        if any(a <= ts <= b for a, b in down):
            in_down.append(rtt)
        elif any(a <= ts <= b for a, b in up):
            in_up.append(rtt)
        elif not any(a - 2 <= ts <= b + 2 for a, b in busy):
            idle.append(rtt)
    out["idle_p50"] = percentile(sorted(idle), 50)
    out["down_p50"] = percentile(sorted(in_down), 50)
    out["up_p50"] = percentile(sorted(in_up), 50)
    if out["idle_p50"] is not None:
        if out["down_p50"] is not None:
            out["delta_down"] = out["down_p50"] - out["idle_p50"]
        if out["up_p50"] is not None:
            out["delta_up"] = out["up_p50"] - out["idle_p50"]
        worst = max([d for d in (out["delta_down"], out["delta_up"]) if d is not None],
                    default=None)
        if worst is not None:
            out["worst_delta"] = worst
            for limit, letter in ((5, "A+"), (30, "A"), (60, "B"), (200, "C"),
                                  (400, "D"), (float("inf"), "F")):
                if worst < limit:
                    out["grade"] = letter
                    break
    return out


def _analyze_wan(conn, run_id, A, targets, interval):
    """Turn the public-IP samples into uplink stints and per-provider quality."""
    out = {"wan_stints": [], "wan_ips": {}, "wan_switches": [], "wan_quality": {},
           "wan_enabled": False, "wan_samples": 0, "wan_fail_pct": None,
           "wan_switch_cost": None, "ttl_switches": {}}
    rows = _q(conn, "SELECT ts, method, ip, ok FROM wan_samples WHERE run_id=? ORDER BY ts",
              (run_id,))
    out["wan_samples"] = len(rows)
    if not rows:
        return out
    out["wan_enabled"] = True
    fails = sum(1 for r in rows if not r["ok"])
    out["wan_fail_pct"] = 100.0 * fails / len(rows)
    for r in _q(conn, "SELECT ip, first_seen, last_seen, samples, asn, as_name, cc, label"
                      " FROM wan_ips WHERE run_id=?", (run_id,)):
        out["wan_ips"][r["ip"]] = dict(r)

    stints = []
    current = None
    last_ok_ts = None
    for r in rows:
        if not r["ok"] or not r["ip"]:
            continue
        ip, ts = r["ip"], r["ts"]
        if current is None:
            current = {"ip": ip, "start": ts, "end": ts, "samples": 1}
        elif ip == current["ip"]:
            current["end"] = ts
            current["samples"] += 1
        else:
            current["end"] = last_ok_ts if last_ok_ts else current["end"]
            stints.append(current)
            out["wan_switches"].append({
                "ts": ts, "from": current["ip"], "to": ip,
                "gap_s": max(0.0, ts - (last_ok_ts or ts))})
            current = {"ip": ip, "start": ts, "end": ts, "samples": 1}
        last_ok_ts = ts
    if current:
        current["end"] = max(current["end"], A["ended"] or current["end"])
        stints.append(current)
    for s in stints:
        s["seconds"] = max(0.0, s["end"] - s["start"])
    out["wan_stints"] = stints

    per_ip = {}
    for s in stints:
        p = per_ip.setdefault(s["ip"], {"ip": s["ip"], "seconds": 0.0, "stints": 0})
        p["seconds"] += s["seconds"]
        p["stints"] += 1
    span = sum(p["seconds"] for p in per_ip.values()) or 1.0
    for p in per_ip.values():
        p["share_pct"] = 100.0 * p["seconds"] / span
        meta = out["wan_ips"].get(p["ip"], {})
        p["asn"] = meta.get("asn")
        p["as_name"] = meta.get("as_name")
        p["cc"] = meta.get("cc")
        p["label"] = meta.get("label")

    # Quality of the connection while each uplink was the active one.
    inet = [n for n in targets
            if (A.get("roles") or {}).get(n, "anchor") in ("anchor", "custom")]
    if inet and per_ip:
        placeholders = ",".join("?" * len(inet))
        samples = _q(conn, f"SELECT ts, ok, rtt_ms, ttl FROM ping_samples"
                           f" WHERE run_id=? AND target IN ({placeholders}) ORDER BY ts",
                     (run_id, *inet))
        windows = sorted(((s["start"], s["end"], s["ip"]) for s in stints))
        idx = 0
        acc = {ip: {"n": 0, "ok": 0, "rtts": [], "ttls": {}} for ip in per_ip}
        for r in samples:
            ts = r["ts"]
            while idx < len(windows) - 1 and ts > windows[idx][1]:
                idx += 1
            a, b, ip = windows[idx]
            if not (a <= ts <= b) or ip not in acc:
                continue
            rec = acc[ip]
            rec["n"] += 1
            if r["ok"]:
                rec["ok"] += 1
                if r["rtt_ms"] is not None:
                    rec["rtts"].append(r["rtt_ms"])
                if r["ttl"]:
                    rec["ttls"][r["ttl"]] = rec["ttls"].get(r["ttl"], 0) + 1
        for ip, rec in acc.items():
            vals = sorted(rec["rtts"])
            jit = None
            if len(rec["rtts"]) > 1:
                jit = mean([abs(rec["rtts"][i] - rec["rtts"][i - 1])
                            for i in range(1, len(rec["rtts"]))])
            per_ip[ip].update({
                "probes": rec["n"],
                "loss_pct": ((rec["n"] - rec["ok"]) * 100.0 / rec["n"]) if rec["n"] else None,
                "avg": mean(vals), "p50": percentile(vals, 50),
                "p95": percentile(vals, 95), "jitter": jit,
                "ttl": max(rec["ttls"], key=rec["ttls"].get) if rec["ttls"] else None,
                "mos": mos_score(mean(vals), jit,
                                 ((rec["n"] - rec["ok"]) * 100.0 / rec["n"]) if rec["n"] else 0)})
    out["wan_quality"] = per_ip

    # How much connectivity does a failover actually cost?
    if out["wan_switches"] and inet:
        costs = []
        placeholders = ",".join("?" * len(inet))
        for sw in out["wan_switches"]:
            rows2 = _q(conn, f"SELECT ok FROM ping_samples WHERE run_id=? AND ts BETWEEN ? AND ?"
                             f" AND target IN ({placeholders})",
                       (run_id, sw["ts"] - 8, sw["ts"] + 8, *inet))
            if rows2:
                lost = sum(1 for r in rows2 if not r["ok"])
                sw["lost_probes"] = lost
                sw["probes"] = len(rows2)
                sw["lost_s"] = lost * interval / max(1, len(inet))
                costs.append(sw["lost_s"])
        if costs:
            out["wan_switch_cost"] = mean(costs)

    # TTL step changes corroborate a path swap even if the IP oracle missed it.
    for name, t in targets.items():
        if (A.get("roles") or {}).get(name) in LOCAL_ROLES or len(t["ttl"]) < 2:
            continue
        out["ttl_switches"][name] = {"values": dict(t["ttl"]), "changes": t["ttl_changes"]}
    return out


# --------------------------------------------------------------------------- #
# scoring + the verdict engine
# --------------------------------------------------------------------------- #
def _curve(value, points):
    """Piecewise-linear mapping; points = [(value, score)] with value ascending."""
    if value is None:
        return None
    if value <= points[0][0]:
        return points[0][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if value <= x1:
            span = (x1 - x0) or 1
            return y0 + (y1 - y0) * (value - x0) / span
    return points[-1][1]


UPTIME_CURVE = [(90, 0), (95, 25), (98, 50), (99, 68), (99.5, 80),
                (99.9, 92), (99.99, 100), (100, 100)]
LOSS_CURVE = [(0, 100), (0.1, 92), (0.5, 80), (1, 68), (3, 40), (5, 20), (10, 0)]
JITTER_CURVE = [(1, 100), (5, 90), (10, 75), (20, 55), (50, 25), (100, 0)]
BLOAT_SCORE = {"A+": 100, "A": 92, "B": 78, "C": 55, "D": 30, "F": 5}


LOCAL_ROLES = ("gateway", "lan", "isp")


def target_roles(A):
    roles = {t["name"]: t.get("role", "anchor")
             for t in (A.get("net", {}).get("targets") or []) if t.get("name")}
    for name in A.get("targets", {}):
        if name not in roles:            # captures written before roles existed
            roles[name] = {"gateway": "gateway", "isp-hop": "isp", "lan-hop": "lan",
                           "isp-edge": "isp", "ipv6": "anchor6"}.get(name, "anchor")
    return roles


def _internet_targets(A):
    """Targets that represent 'the public internet' — never the local hops."""
    roles = target_roles(A)
    return {n: t for n, t in A["targets"].items()
            if roles.get(n, "anchor") in ("anchor", "custom", "anchor6")}


def _anchor_targets(A):
    roles = target_roles(A)
    return {n: t for n, t in A["targets"].items()
            if roles.get(n, "anchor") in ("anchor", "custom")}


def _score(A):
    parts, weights = {}, {}
    inet = _internet_targets(A)
    if A.get("uptime_pct") is not None:
        parts["availability"] = _curve(A["uptime_pct"], UPTIME_CURVE)
        weights["availability"] = 40
    if inet:
        losses = [t["loss_pct"] for t in inet.values() if t["sent"]]
        # The best-performing anchor is what your connection can actually do.
        if losses:
            parts["packet loss"] = _curve(min(losses), LOSS_CURVE)
            weights["packet loss"] = 20
        jits = [t["jitter"] for t in inet.values() if t["jitter"] is not None]
        if jits:
            parts["jitter"] = _curve(min(jits), JITTER_CURVE)
            weights["jitter"] = 15
    if A.get("dns"):
        fails = [d["fail_pct"] for d in A["dns"].values() if d["n"] >= 3]
        p95s = [d["p95"] for d in A["dns"].values() if d["p95"] is not None]
        if fails:
            sc = max(0.0, 100.0 - min(fails) * 6.0)
            if p95s and min(p95s) > 200:
                sc -= min(25.0, (min(p95s) - 200) / 20.0)
            parts["DNS"] = max(0.0, sc)
            weights["DNS"] = 10
    if A.get("http"):
        oks = [h["ok_pct"] for h in A["http"].values() if h["n"] >= 2 and h["ok_pct"] is not None]
        if oks:
            parts["HTTP"] = _curve(100 - max(oks), LOSS_CURVE)
            weights["HTTP"] = 10
    bb = A.get("bufferbloat") or {}
    if bb.get("grade"):
        parts["bufferbloat"] = BLOAT_SCORE.get(bb["grade"], 50)
        weights["bufferbloat"] = 5
    if not weights:
        return 0.0, {}
    total_w = sum(weights.values())
    score = sum(parts[k] * weights[k] for k in parts) / total_w
    detail = {k: {"score": parts[k], "weight": 100.0 * weights[k] / total_w} for k in parts}
    return round(max(0.0, min(100.0, score)), 1), detail


def _f(severity, title, detail, fix=None, evidence=None):
    return {"severity": severity, "title": title, "detail": detail,
            "fix": fix, "evidence": evidence or []}


def _findings(A, cfg):
    out = []
    gw = A["targets"].get("gateway")
    lan = A["targets"].get("lan-hop")
    isp = A["targets"].get("isp-edge") or A["targets"].get("isp-hop")
    anchors = _anchor_targets(A)
    best_loss = min([t["loss_pct"] for t in anchors.values()], default=None)
    worst_loss = max([t["loss_pct"] for t in anchors.values()], default=None)
    uptime = A.get("uptime_pct")

    if not A["targets"]:
        return [_f("critical", "No measurements were captured",
                   "The capture contains no ICMP samples at all.",
                   "Check that the tool could open an ICMP socket or run the ping binary.")]

    # -- availability ------------------------------------------------------- #
    if uptime is not None:
        n_out = len(A["outages"])
        if uptime >= 99.99 and not n_out:
            out.append(_f("ok", "Connection stayed up for the whole capture",
                          f"Availability {uptime:.4f}% with no interruption longer than "
                          f"{max(1, int(cfg.outage_ticks))} s."))
        else:
            sev = "ok" if uptime >= 99.99 else ("info" if uptime >= 99.9
                                                else ("warning" if uptime >= 99 else "critical"))
            out.append(_f(sev, f"Availability {uptime:.4f}% — {n_out} interruption(s)",
                          f"Total downtime {fmt_dur(A['outage_total_s'])} across {n_out} "
                          f"outage(s); the longest lasted {fmt_dur(A['outage_max_s'])}, "
                          f"the average {fmt_dur(A['mttr_s'])}. "
                          f"Extrapolated over 24 h that is "
                          f"{fmt_dur(A['outage_total_s'] / max(1, A['duration']) * 86400)} "
                          f"of downtime per day.",
                          "Use the outage table below: the *gateway* column tells you whether "
                          "the router was still answering while the internet was gone."))
        local = [o for o in A["outages"] if o["gateway"] == "down"]
        remote = [o for o in A["outages"] if o["gateway"] == "up"]
        if local:
            out.append(_f("critical",
                          f"{len(local)} outage(s) took the router down too",
                          "During these the local gateway stopped answering ICMP as well, so the "
                          "fault is on your side of the demarcation point — the cable, the "
                          "Wi-Fi link, the switch or the router itself.",
                          "Test with a wired connection; check the router's logs and its uptime "
                          "counter to see whether it is rebooting.",
                          [f"{ts_str(o['start'])} for {fmt_dur(o['seconds'])}" for o in local[:5]]))
        if remote:
            out.append(_f("warning",
                          f"{len(remote)} outage(s) happened beyond the router",
                          "The gateway kept answering while every public anchor went silent — "
                          "the break was in the ISP's access network or further upstream, not in "
                          "your LAN.",
                          "This is evidence to hand to the ISP: timestamps and durations are in "
                          "the outage table below.",
                          [f"{ts_str(o['start'])} for {fmt_dur(o['seconds'])}" for o in remote[:5]]))

    # -- where does the loss start? ----------------------------------------- #
    if gw and gw["sent"] > 20:
        if gw["loss_pct"] >= 2:
            out.append(_f("critical", f"The router itself drops {gw['loss_pct']:.2f}% of packets",
                          "Loss on the very first hop is always local. Wi-Fi interference, a bad "
                          "Ethernet cable, a dying port or an overloaded router cause this.",
                          "Move to Ethernet (or closer to the AP), swap the cable, and re-run. "
                          "If loss to the gateway disappears, everything downstream will improve."))
        elif gw["loss_pct"] >= 0.5:
            out.append(_f("warning", f"Mild loss to the router ({gw['loss_pct']:.2f}%)",
                          "Small but real loss on the local link. On Wi-Fi this is usually "
                          "interference or distance.",
                          "Compare a wired run against this one to confirm."))
    if lan and gw and lan["sent"] > 20 and gw["sent"] > 20:
        if lan["loss_pct"] - gw["loss_pct"] >= 1.0:
            out.append(_f("critical",
                          f"Loss appears at the second router in your own network "
                          f"({lan['loss_pct']:.2f}% vs {gw['loss_pct']:.2f}% at the gateway)",
                          f"`{A['net'].get('lan_hop')}` is still a private address, so it is "
                          "your equipment — a second router, a firewall or the load balancer. "
                          "The path is clean up to it and lossy from it onwards.",
                          "Check that device first: its CPU/session table, its uplink port, and "
                          "its own logs."))
    upstream_ref = lan or gw
    if isp and upstream_ref and isp["sent"] > 20 and upstream_ref["sent"] > 20:
        if isp["loss_pct"] - upstream_ref["loss_pct"] >= 1.0:
            out.append(_f("critical",
                          f"Loss appears at the ISP's first hop ({isp['loss_pct']:.2f}% vs "
                          f"{upstream_ref['loss_pct']:.2f}% at your last router)",
                          f"Your router is clean but the provider's edge router "
                          f"({A['net'].get('isp_hop')}) already loses packets. That is the "
                          f"provider's access network — the segment between your flat and their "
                          f"aggregation point.",
                          "Report it to the ISP with these numbers; ask them to check the port, "
                          "the line quality and the aggregation uplink."))
    if best_loss is not None and isp and best_loss - isp["loss_pct"] >= 1.0:
        out.append(_f("warning", "Loss starts beyond the ISP's first hop",
                      "The provider's edge answers cleanly, but traffic to the public internet "
                      "still loses packets — the problem is upstream: their transit, peering or "
                      "the route to those destinations.",
                      "Attach the traceroute snapshots below when you open the ticket."))
    if worst_loss is not None and best_loss is not None and worst_loss - best_loss >= 2.0:
        bad = max(anchors.items(), key=lambda kv: kv[1]["loss_pct"])[0]
        good = min(anchors.items(), key=lambda kv: kv[1]["loss_pct"])[0]
        out.append(_f("info", f"Only the route to {bad} is degraded",
                      f"{bad} loses {worst_loss:.2f}% while {good} loses only {best_loss:.2f}%. "
                      "When one anchor is much worse than the others the fault is that particular "
                      "path or that operator, not your connection.",
                      "Nothing to fix locally — but avoid using that host as a health check."))
    if best_loss is not None and best_loss >= 1.0 and (not gw or gw["loss_pct"] < 0.5):
        out.append(_f("critical", f"Every public anchor loses packets (best {best_loss:.2f}%)",
                      "Loss that shows up on all three independent operators at once is yours, "
                      "not theirs — the common element is your line.",
                      "Combined with a clean gateway, this points squarely at the ISP link."))

    # -- latency / jitter ---------------------------------------------------- #
    jitters = {n: t["jitter"] for n, t in anchors.items() if t["jitter"] is not None}
    if jitters:
        jmin = min(jitters.values())
        if jmin >= 30:
            out.append(_f("critical", f"Very high jitter ({jmin:.1f} ms)",
                          "Latency swings this wide break calls, video and games even when no "
                          "packet is lost. Typical causes: a saturated uplink, Wi-Fi contention, "
                          "or an overloaded ISP segment.",
                          "Check the bufferbloat section — if latency explodes only under load, "
                          "it is queue management, not the line."))
        elif jmin >= 12:
            out.append(_f("warning", f"Elevated jitter ({jmin:.1f} ms)",
                          "Noticeable for real-time traffic; fine for browsing and downloads."))
    lat = {n: t["p50"] for n, t in anchors.items() if t["p50"] is not None}
    if lat:
        lmin = min(lat.values())
        spikes = max([(t["p99"] or 0) - (t["p50"] or 0) for t in anchors.values()], default=0)
        if lmin > 120:
            out.append(_f("warning", f"Baseline latency is high ({lmin:.0f} ms median)",
                          "This is the floor of your connection — satellite, mobile or a very "
                          "distant route would explain it."))
        if spikes > 150:
            out.append(_f("warning", f"Latency spikes up to {spikes:.0f} ms above the median",
                          "Occasional large spikes usually mean something else is filling the "
                          "link, or the Wi-Fi is retransmitting.",
                          "Correlate with the local-link throughput chart below."))
    mos = [t["mos"] for t in anchors.values() if t.get("mos")]
    if mos:
        best_mos = max(mos)
        if best_mos < 3.6:
            out.append(_f("warning", f"Estimated call quality MOS {best_mos:.2f}/4.5",
                          "Derived from latency, jitter and loss with the ITU E-model. Below 3.6 "
                          "voice calls sound rough."))

    # -- multi-WAN / balancer ------------------------------------------------ #
    out.extend(_wan_findings(A))

    # -- bufferbloat --------------------------------------------------------- #
    bb = A.get("bufferbloat") or {}
    if bb.get("worst_delta") is not None:
        d = bb["worst_delta"]
        if d >= 200:
            out.append(_f("critical", f"Severe bufferbloat — latency grade {bb['grade']}",
                          f"Under load the median round trip rises from "
                          f"{bb['idle_p50']:.0f} ms to {bb['idle_p50'] + d:.0f} ms "
                          f"(+{d:.0f} ms). Every call, game and page load stalls whenever "
                          f"anything is downloading.",
                          "Enable SQM/QoS on the router (fq_codel or cake) and set the shaper to "
                          "about 90% of the measured throughput."))
        elif d >= 60:
            out.append(_f("warning", f"Bufferbloat under load (+{d:.0f} ms, grade {bb['grade']})",
                          "Latency grows noticeably while the link is busy — the classic symptom "
                          "of oversized buffers in the modem or router.",
                          "Turning on fq_codel/cake usually removes it completely."))
        else:
            out.append(_f("ok", f"Latency stays flat under load (grade {bb['grade']})",
                          f"Only +{d:.0f} ms while saturated — queue management is working."))

    # -- DNS ------------------------------------------------------------------ #
    if A.get("dns"):
        worst = [d for d in A["dns"].values() if d["n"] >= 3 and d["fail_pct"] > 2]
        slow = [d for d in A["dns"].values() if d["n"] >= 3 and (d["p95"] or 0) > 300]
        usable = [d for d in A["dns"].values()
                  if d["n"] >= 3 and d["fail_pct"] < 1 and d["p50"] is not None]
        fastest = min(usable, key=lambda d: d["p50"]) if usable else None
        for d in worst:
            out.append(_f("critical" if d["fail_pct"] > 10 else "warning",
                          f"DNS resolver '{d['resolver']}' ({d['proto']}) failed "
                          f"{d['fail_pct']:.1f}% of queries",
                          f"{d['n'] - d['ok']} of {d['n']} lookups failed"
                          + (f" — {', '.join(f'{k}×{v}' for k, v in d['errors'].items())}"
                             if d["errors"] else "") +
                          ". Failing DNS looks exactly like 'the internet is down' to a browser, "
                          "even when packets flow fine.",
                          (f"Switch the router/clients to {fastest['resolver']} "
                           f"({fastest['p50']:.0f} ms median, {fastest['fail_pct']:.1f}% failures)."
                           if fastest else "Try a different resolver.")))
        for d in slow:
            if d in worst:
                continue
            out.append(_f("warning", f"DNS resolver '{d['resolver']}' is slow "
                                     f"(p95 {d['p95']:.0f} ms)",
                          "Slow name resolution delays the first byte of every new connection.",
                          (f"{fastest['resolver']} answered in {fastest['p50']:.0f} ms median "
                           "during the same period." if fastest else None)))
        if fastest and not worst and not slow:
            out.append(_f("ok", "DNS is healthy",
                          f"Fastest resolver: {fastest['resolver']} at {fastest['p50']:.0f} ms "
                          f"median, {fastest['fail_pct']:.1f}% failures."))
    if A.get("dns_hijack"):
        out.append(_f("warning", "The resolver hijacks NXDOMAIN",
                      f"A deliberately non-existent name resolved to "
                      f"{', '.join(sorted(set(A['dns_hijack'])))}. Your resolver redirects typos "
                      "to an ad/search page instead of returning NXDOMAIN — it also breaks some "
                      "software's failure detection.",
                      "Use 1.1.1.1 / 8.8.8.8 / 9.9.9.9 directly, or DoH."))

    # -- HTTP / interception --------------------------------------------------- #
    if A.get("http"):
        broken = [h for h in A["http"].values() if h["n"] >= 2 and (h["ok_pct"] or 0) < 95]
        if broken and best_loss is not None and best_loss < 1:
            out.append(_f("warning", f"{len(broken)} HTTP endpoint(s) failed while ICMP was fine",
                          "Ping works but HTTPS does not — that pattern means filtering, a "
                          "transparent proxy, an MTU/PMTUD problem or TLS interception rather "
                          "than a broken line.",
                          "Check the MTU finding below and try the same URLs from another network.",
                          [f"{h['url']} — {h['ok_pct']:.0f}% ok, "
                           f"{', '.join(f'{k}×{v}' for k, v in h['errors'].items()) or 'no error text'}"
                           for h in broken[:5]]))
        certs = [(h["url"], h["cert_days"]) for h in A["http"].values()
                 if h.get("cert_days") is not None and h["cert_days"] < 20]
        if certs:
            out.append(_f("info", "A TLS certificate on the probe path expires soon",
                          ", ".join(f"{u} in {d} days" for u, d in certs)))
    if A["event_counts"].get("captive_portal"):
        out.append(_f("critical", "A captive portal or transparent proxy is intercepting traffic",
                      f"The 204 probe returned a real page {A['event_counts']['captive_portal']} "
                      "time(s). Something between you and the internet answers on behalf of the "
                      "destination.",
                      "Open any http:// page in a browser and complete the portal login, or "
                      "find the middlebox on the path."))

    # -- path / MTU / ports ---------------------------------------------------- #
    if A.get("mtu") and A["mtu"] < 1500:
        out.append(_f("warning", f"Path MTU is {A['mtu']}, not 1500",
                      "PPPoE (1492), a VPN or a tunnel shortens the usable packet size. It is "
                      "harmless *if* PMTUD works; if ICMP is filtered somewhere you get the "
                      "classic 'small pages load, big ones hang' symptom.",
                      f"If large transfers stall, clamp MSS to {A['mtu'] - 40} on the router."))
    elif A.get("mtu") is None and A["event_counts"].get("mtu"):
        note = next((json.loads(e["details"] or "{}").get("note") or ""
                     for e in A["events"] if e["kind"] == "mtu"), "")
        out.append(_f("info", "Path MTU could not be measured",
                      f"{note.capitalize() or 'The DF-marked probes got no answers'}. When "
                      "DF-marked packets are silently dropped instead of answered, path-MTU "
                      "discovery cannot work end to end either — which is what causes "
                      "'small pages load, large downloads hang'.",
                      "If large transfers stall, clamp MSS on the router (1400 is a safe value)."))
    blocked = [p for p in A.get("ports", {}).values() if p["n"] >= 2 and p["ok_pct"] == 0]
    if blocked:
        out.append(_f("info", f"{len(blocked)} TCP port(s) never connected",
                      "These could be blocked by the ISP, by a local firewall, or simply closed "
                      "on the far side.",
                      None,
                      [f"{p['host']}:{p['port']} — {', '.join(p['err'])}" for p in blocked]))
    v6 = A["targets"].get("ipv6")
    if v6 and v6["sent"] > 10:
        if v6["loss_pct"] > 90:
            out.append(_f("warning", "IPv6 is configured but does not work",
                          f"{v6['loss_pct']:.0f}% loss to a public IPv6 anchor while IPv4 works. "
                          "Broken IPv6 makes some sites slow because clients try v6 first.",
                          "Either fix IPv6 upstream or disable it on the router."))
        elif v6["loss_pct"] < 2:
            out.append(_f("ok", "IPv6 works",
                          f"{v6['p50']:.0f} ms median, {v6['loss_pct']:.2f}% loss."))
    if A["ntp"]["n"] and A["ntp"]["ok"] == 0:
        out.append(_f("warning", "NTP (UDP/123) never answered",
                      "Plain UDP to the internet may be blocked or intercepted, which breaks time "
                      "sync, some VPNs and QUIC."))
    elif A["ntp"]["ok"] and A["ntp"]["offset"] is not None and abs(A["ntp"]["offset"]) > 2000:
        out.append(_f("warning", f"System clock is off by {A['ntp']['offset'] / 1000:.1f} s",
                      "A wrong clock breaks TLS certificate validation and log correlation.",
                      "Enable NTP time sync on this machine."))
    if len(A.get("paths", [])) > 1:
        out.append(_f("info", f"The route changed {len(A['paths']) - 1} time(s) during the capture",
                      "Traceroute snapshots show more than one path to 1.1.1.1. With a "
                      "load balancer this is expected; otherwise it means upstream reconvergence.",
                      None,
                      [f"{ts_str(ts)}: {' → '.join(p[:6]) or '(no hops)'}" for ts, p in A["paths"][:6]]))

    # -- local link ------------------------------------------------------------ #
    link = A.get("link", {})
    if link.get("rx_err") or link.get("tx_err"):
        out.append(_f("critical", "The network interface is counting errors",
                      f"rx_errors +{link['rx_err']}, tx_errors +{link['tx_err']} during the "
                      "capture. Interface errors mean physical trouble: cable, connector, port "
                      "or driver.",
                      "Replace the cable, try another port, and check the duplex/speed setting."))
    if link.get("carrier_drops"):
        out.append(_f("critical", f"The link went down {link['carrier_drops']} time(s)",
                      "The interface lost carrier — the cable was unplugged, the switch port "
                      "flapped, or the Wi-Fi association dropped."))
    r = link.get("wifi_loss_r")
    if r is not None and r < -0.35:
        wifi_vals = link.get("wifi", [])
        out.append(_f("warning", "Packet loss tracks the Wi-Fi signal strength",
                      f"Correlation r = {r:.2f} between signal level and per-minute loss "
                      f"(signal ranged {min(wifi_vals):.0f}…{max(wifi_vals):.0f} dBm). When the "
                      "signal drops, packets drop — this is a Wi-Fi problem, not an ISP problem.",
                      "Move closer to the AP, change channel/band, or use Ethernet."))
    elif link.get("wifi"):
        w = link["wifi"]
        if mean(w) is not None and mean(w) < -70:
            out.append(_f("warning", f"Weak Wi-Fi signal (average {mean(w):.0f} dBm)",
                          "Below about −70 dBm the link starts retransmitting heavily.",
                          "Move closer to the access point or add one."))

    # -- speed ----------------------------------------------------------------- #
    ss = A.get("speed_stats", {})
    if ss.get("download"):
        d = ss["download"]
        if cfg.plan_mbps:
            pct = 100.0 * d["avg"] / cfg.plan_mbps
            sev = "ok" if pct >= 80 else ("warning" if pct >= 50 else "critical")
            out.append(_f(sev, f"Download averages {d['avg']:.1f} Mbps — {pct:.0f}% of your "
                               f"{cfg.plan_mbps:g} Mbps plan",
                          f"Across {d['n']} test(s): min {d['min']:.1f}, median {d['p50']:.1f}, "
                          f"max {d['max']:.1f} Mbps.",
                          None if pct >= 80 else
                          "Test again over Ethernet with nothing else running; if it stays low, "
                          "quote these numbers to the ISP."))
        else:
            out.append(_f("info", f"Download averages {d['avg']:.1f} Mbps",
                          f"min {d['min']:.1f}, median {d['p50']:.1f}, max {d['max']:.1f} Mbps "
                          f"over {d['n']} test(s). Re-run with --plan to compare against what "
                          "you pay for."))
        if d.get("cv") and d["cv"] > 0.3 and d["n"] >= 3:
            out.append(_f("warning", f"Throughput is unstable (±{d['cv'] * 100:.0f}%)",
                          f"Speed ranged from {d['min']:.1f} to {d['max']:.1f} Mbps between "
                          "tests. Consistent shaping or congestion looks like this."))
    if ss.get("upload"):
        u = ss["upload"]
        out.append(_f("info", f"Upload averages {u['avg']:.1f} Mbps",
                      f"min {u['min']:.1f}, max {u['max']:.1f} Mbps over {u['n']} test(s)."))
    if A["event_counts"].get("speed_capped"):
        out.append(_f("info", "The speed tests hit their data cap before finishing",
                      f"This link is fast enough that the "
                      f"{A['config'].get('speed_max_mb', 200):g} MB per-test cap was reached in "
                      "under four seconds of measurement, so the figures above include TCP "
                      "slow start and understate the real throughput.",
                      "Re-run with a larger --speed-max-mb (say 1000) for an accurate number, "
                      "or leave it as is if you are on a metered connection."))

    # -- patterns -------------------------------------------------------------- #
    if A.get("periodic"):
        p = A["periodic"]
        out.append(_f("critical", f"Outages repeat about every {fmt_dur(p['period_s'])}",
                      f"{p['count']} interruptions spaced almost evenly (variation "
                      f"{p['cv'] * 100:.0f}%). Regular timing points at something scheduled: a "
                      "DHCP lease renewal, a PPPoE re-dial, a scheduled router reboot or a "
                      "watchdog.",
                      "Check the router's DHCP lease time and any scheduled reboot; compare the "
                      "outage timestamps below with the router log."))
    if A.get("hours") and A["duration"] > 4 * 3600:
        hs = {h: v for h, v in A["hours"].items() if v["loss"] is not None and v["minutes"] >= 5}
        if len(hs) >= 4:
            overall = mean([v["loss"] for v in hs.values()]) or 0
            worst_h = max(hs.items(), key=lambda kv: kv[1]["loss"])
            if overall > 0 and worst_h[1]["loss"] > max(1.0, overall * 2.5):
                out.append(_f("warning",
                              f"Quality is clearly worse around {worst_h[0]:02d}:00",
                              f"Loss in that hour averaged {worst_h[1]['loss']:.2f}% against "
                              f"{overall:.2f}% overall. Time-of-day degradation is congestion — "
                              "the segment is oversubscribed at peak hours.",
                              "This is a strong argument when reporting to the ISP."))
    if not any(x["severity"] in ("critical", "warning") for x in out):
        out.insert(0, _f("ok", "No problems detected",
                         "Every layer measured clean for the whole capture: availability, loss, "
                         "jitter, DNS, HTTP and throughput all stayed within healthy limits."))
    order = {"critical": 0, "warning": 1, "info": 2, "ok": 3}
    out.sort(key=lambda x: order.get(x["severity"], 9))
    return out


def _wan_findings(A):
    """Everything about the load balancer / dual-ISP behaviour."""
    out = []
    if not A.get("wan_enabled"):
        return out
    stints = A.get("wan_stints") or []
    switches = A.get("wan_switches") or []
    quality = A.get("wan_quality") or {}
    duration = A.get("duration") or 1
    if not stints:
        out.append(_f("warning", "The public IP could never be determined",
                      f"All {A['wan_samples']} public-IP lookups failed, so uplink switching "
                      "could not be tracked.",
                      "Check that UDP/53 to 208.67.222.222 and 1.1.1.1 is allowed."))
        return out
    if (A.get("wan_fail_pct") or 0) > 20:
        out.append(_f("info", f"{A['wan_fail_pct']:.0f}% of public-IP lookups failed",
                      "The uplink timeline below has gaps; short switches may have been missed."))

    if len(quality) <= 1:
        ip = next(iter(quality)) if quality else stints[0]["ip"]
        meta = quality.get(ip, {})
        label = _wan_name(ip, meta)
        out.append(_f("ok", f"A single uplink was used the whole time — {label}",
                      f"The public IP stayed {ip} for {fmt_dur(duration)}; the balancer did not "
                      "fail over during this capture."))
        return out

    per_hour = len(switches) / (duration / 3600.0) if duration else 0
    sev = "critical" if per_hour >= 6 else ("warning" if per_hour >= 1 else "info")
    cost = A.get("wan_switch_cost")
    out.append(_f(sev, f"The balancer switched uplink {len(switches)} time(s) "
                       f"({per_hour:.1f}/hour)",
                  f"{len(quality)} different public addresses were seen over "
                  f"{fmt_dur(duration)}."
                  + (f" Each switch cost on average {cost:.1f} s of lost packets."
                     if cost else "")
                  + " Detection resolution is one public-IP probe every "
                    f"{A['config'].get('wan_interval', 2)} s, so switches shorter than that can "
                    "still slip through — the reply-TTL column corroborates them.",
                  "If the switching is not intentional, look for what makes the balancer think "
                  "an uplink is unhealthy: its probe target, its timeout, and the loss figures "
                  "per uplink below.",
                  [f"{ts_str(s['ts'])}  {s['from']} → {s['to']}"
                   + (f"  (lost ≈{s.get('lost_s', 0):.1f} s)" if s.get("lost_s") else "")
                   for s in switches[:8]]))

    brief = [s for s in stints if s["seconds"] < 30]
    if len(brief) >= 2:
        out.append(_f("warning", f"{len(brief)} of the uplink stints lasted under 30 s",
                      "Very short stints mean the balancer is flapping: it moves traffic, decides "
                      "the new path is also bad (or the old one recovered) and moves back. Every "
                      "flap resets NAT state, so TCP sessions, calls and VPNs break.",
                      "Raise the balancer's failover thresholds (more consecutive failures, "
                      "longer hold-down / hysteresis) so a couple of lost probes cannot trigger "
                      "a switch.",
                      [f"{ts_str(s['start'])} on {s['ip']} for {fmt_dur(s['seconds'])}"
                       for s in brief[:6]]))

    ranked = [q for q in quality.values() if q.get("probes", 0) > 30]
    if len(ranked) >= 2:
        ranked.sort(key=lambda q: (q.get("loss_pct") or 0, q.get("p50") or 0))
        best, worst = ranked[0], ranked[-1]
        detail = []
        for q in sorted(quality.values(), key=lambda q: -(q.get("share_pct") or 0)):
            detail.append(
                f"{_wan_name(q['ip'], q)} — {q.get('share_pct', 0):.1f}% of the time, "
                f"{q.get('stints', 0)} stint(s), loss {fmt_pct(q.get('loss_pct'))}, "
                f"median {fmt_ms(q.get('p50'))} ms, jitter {fmt_ms(q.get('jitter'))} ms")
        gap_loss = (worst.get("loss_pct") or 0) - (best.get("loss_pct") or 0)
        gap_rtt = (worst.get("p50") or 0) - (best.get("p50") or 0)
        if gap_loss >= 1.0 or gap_rtt >= 20:
            out.append(_f("warning", f"The two uplinks are not equivalent — "
                                     f"{_wan_name(worst['ip'], worst)} is the weak one",
                          f"While {_wan_name(worst['ip'], worst)} was active the connection lost "
                          f"{fmt_pct(worst.get('loss_pct'))} of packets at "
                          f"{fmt_ms(worst.get('p50'))} ms median; on "
                          f"{_wan_name(best['ip'], best)} it was "
                          f"{fmt_pct(best.get('loss_pct'))} at {fmt_ms(best.get('p50'))} ms. "
                          "Traffic that lands on the weak uplink gets a visibly worse connection.",
                          "Make the good uplink primary and the weak one standby, or fix/replace "
                          "the weak provider.", detail))
        else:
            out.append(_f("info", "Both uplinks perform about the same", "\n".join(detail)))

    ttl = A.get("ttl_switches") or {}
    hinted = {n: v for n, v in ttl.items() if v["changes"] >= 1}
    if hinted:
        worst_n = max(hinted.items(), key=lambda kv: kv[1]["changes"])
        out.append(_f("info", "Reply TTL confirms the path really changed",
                      f"{worst_n[0]} answered with {len(worst_n[1]['values'])} different TTL "
                      f"values ({', '.join(str(k) for k in sorted(worst_n[1]['values']))}). A "
                      "different TTL means a different number of hops — i.e. genuinely a "
                      "different route, which is exactly what a provider switch looks like. "
                      "TTL is sampled every ICMP probe, so it catches switches shorter than the "
                      "public-IP polling interval."))
    return out


def _wan_name(ip, meta=None):
    meta = meta or {}
    bits = [ip]
    if meta.get("as_name"):
        bits.append(f"AS{meta.get('asn')} {meta['as_name']}")
    elif meta.get("asn"):
        bits.append(f"AS{meta['asn']}")
    if meta.get("label"):
        bits.append(f"[{meta['label']}]")
    return " ".join(bits)


# --------------------------------------------------------------------------- #
# SVG charts — dependency-free, theme-aware, embedded in the Markdown report
# --------------------------------------------------------------------------- #
PALETTE = ["#2f6fed", "#17a673", "#e8a33d", "#d64545", "#7a5af8", "#16a2b8",
           "#c2569a", "#5b6472"]
C_GOOD, C_WARN, C_BAD = "#17a673", "#e8a33d", "#d64545"

SVG_CSS = """
  .bg{fill:#ffffff}
  .plot{fill:#f7f8fb}
  .grid{stroke:#e4e8f0;stroke-width:1}
  .axis{stroke:#c3cad6;stroke-width:1}
  .ink{fill:#141a24}
  .muted{fill:#657084}
  .title{font:600 15px system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
  .sub{font:400 11.5px system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
  .lbl{font:400 11px system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
  .val{font:600 12px system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
  .big{font:700 34px system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
  .shade{fill:#d64545;opacity:.13}
  @media (prefers-color-scheme: dark){
    .bg{fill:#11151d} .plot{fill:#161b25}
    .grid{stroke:#242c39} .axis{stroke:#3a4557}
    .ink{fill:#e8ecf3} .muted{fill:#93a0b4}
    .shade{opacity:.22}
  }
"""


def _esc(s):
    return html.escape(str(s), quote=True)


def _n(v):
    """Compact number formatting for SVG coordinates."""
    return f"{v:.2f}".rstrip("0").rstrip(".")


class Svg:
    def __init__(self, width, height):
        self.w = width
        self.h = height
        self.parts = []

    def add(self, markup):
        self.parts.append(markup)
        return self

    def text(self, x, y, s, cls="lbl ink", anchor="start", extra=""):
        self.add(f'<text x="{_n(x)}" y="{_n(y)}" class="{cls}" '
                 f'text-anchor="{anchor}"{extra}>{_esc(s)}</text>')

    def rect(self, x, y, w, h, fill, extra=""):
        if w <= 0 or h <= 0:
            return self
        return self.add(f'<rect x="{_n(x)}" y="{_n(y)}" width="{_n(w)}" '
                        f'height="{_n(h)}" fill="{fill}"{extra}/>')

    def line(self, x1, y1, x2, y2, cls="grid"):
        return self.add(f'<line x1="{_n(x1)}" y1="{_n(y1)}" x2="{_n(x2)}" '
                        f'y2="{_n(y2)}" class="{cls}"/>')

    def save(self, path, title=""):
        body = "".join(self.parts)
        doc = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {self.w} {self.h}" '
               f'width="{self.w}" height="{self.h}" role="img" '
               f'aria-label="{_esc(title)}"><title>{_esc(title)}</title>'
               f'<style>{SVG_CSS}</style>'
               f'<rect width="{self.w}" height="{self.h}" rx="10" class="bg"/>'
               f'{body}</svg>')
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(doc)
        return path


def downsample(points, limit, how="mean"):
    """Fold a long series into `limit` buckets so week-long captures still plot.

    A multi-day capture holds thousands of per-minute points; drawing them all
    produces a multi-megabyte path that renders no better than a folded one.
    """
    if len(points) <= limit:
        return points
    step = len(points) / float(limit)
    out = []
    for i in range(limit):
        chunk = points[int(i * step):max(int(i * step) + 1, int((i + 1) * step))]
        vals = [v for _t, v in chunk if v is not None]
        x = chunk[0][0]
        if not vals:
            out.append((x, None))
        elif how == "min":
            out.append((x, min(vals)))
        elif how == "max":
            out.append((x, max(vals)))
        else:
            out.append((x, sum(vals) / len(vals)))
    return out


def nice_ticks(lo, hi, count=5):
    if hi <= lo:
        hi = lo + 1
    raw = (hi - lo) / max(1, count)
    mag = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1
    for mult in (1, 2, 2.5, 5, 10):
        step = mag * mult
        if raw <= step:
            break
    start = math.floor(lo / step) * step
    out = []
    v = start
    while v <= hi + step * 0.5:
        if v >= lo - step * 0.001:
            out.append(round(v, 10))
        v += step
    return out


TIME_STEPS = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600,
              7200, 10800, 21600, 43200, 86400, 172800]


def time_ticks(t0, t1, count=7):
    span = max(1.0, t1 - t0)
    ideal = span / count
    step = next((s for s in TIME_STEPS if s >= ideal), TIME_STEPS[-1])
    fmt = "%H:%M:%S" if step < 60 else ("%H:%M" if span < 86400 else "%d.%m %H:%M")
    start = math.floor(t0 / step) * step
    out = []
    v = start
    while v <= t1:
        if v >= t0:
            out.append((v, datetime.fromtimestamp(v).strftime(fmt)))
        v += step
    return out


def _legend_width(name):
    return 15 + int(len(str(name)) * 6.1) + 18


def legend_rows(items, max_width):
    """How many rows this legend needs — charts grow to fit it instead of clipping."""
    cx, rows = 0, 1
    for name, _color in items:
        w = _legend_width(name)
        if cx + w > max_width and cx > 0:
            cx, rows = 0, rows + 1
        cx += w
    return rows


def _legend(svg, x, y, items, max_width):
    cx, cy = x, y
    for name, color in items:
        w = _legend_width(name)
        if cx + w > x + max_width and cx > x:
            cx, cy = x, cy + 16
        svg.rect(cx, cy - 8, 10, 10, color, ' rx="2"')
        svg.text(cx + 15, cy + 1, name, cls="lbl muted")
        cx += w


def chart_timeline(path, series, title, subtitle="", y_label="", width=1000, height=330,
                   shades=None, y_min=None, y_max=None, y_fmt="{:.0f}", fill=False,
                   markers=None):
    """Multi-series time chart. series: [{'name','color','points':[(ts, y|None)]}]."""
    ml, mr, mt, mb = 62, 18, 46, 46
    pw = width - ml - mr
    height += (legend_rows([(s["name"], None) for s in series], pw) - 1) * 16
    ph = height - mt - mb
    svg = Svg(width, height)
    svg.text(14, 22, title, cls="title ink")
    if subtitle:
        svg.text(14, 38, subtitle, cls="sub muted")
    series = [dict(s, points=downsample(s["points"], int(pw * 1.5))) for s in series]
    xs = [p[0] for s in series for p in s["points"]]
    ys = [p[1] for s in series for p in s["points"] if p[1] is not None]
    if not xs or not ys:
        svg.text(width / 2, height / 2, "no data", cls="lbl muted", anchor="middle")
        return svg.save(path, title)
    t0, t1 = min(xs), max(xs)
    lo = 0.0 if y_min is None else y_min
    hi = max(ys) if y_max is None else y_max
    if hi <= lo:
        hi = lo + 1
    hi *= 1.08
    svg.rect(ml, mt, pw, ph, "none", ' class="plot"')

    def px(t):
        return ml + (t - t0) / max(1e-9, (t1 - t0)) * pw

    def py(v):
        return mt + ph - (v - lo) / max(1e-9, (hi - lo)) * ph

    for a, b in (shades or []):
        x0, x1 = px(max(a, t0)), px(min(b, t1))
        if x1 > x0:
            svg.rect(x0, mt, max(1.5, x1 - x0), ph, "none", ' class="shade"')
    for v in nice_ticks(lo, hi, 5):
        if v < lo or v > hi:
            continue
        y = py(v)
        svg.line(ml, y, ml + pw, y)
        svg.text(ml - 8, y + 4, y_fmt.format(v), cls="lbl muted", anchor="end")
    for t, label in time_ticks(t0, t1):
        x = px(t)
        svg.line(x, mt, x, mt + ph)
        svg.text(x, mt + ph + 16, label, cls="lbl muted", anchor="middle")
    svg.line(ml, mt + ph, ml + pw, mt + ph, cls="axis")
    svg.line(ml, mt, ml, mt + ph, cls="axis")
    if y_label:
        svg.text(14, mt + ph / 2, y_label, cls="lbl muted", anchor="middle",
                 extra=f' transform="rotate(-90 14 {_n(mt + ph / 2)})"')

    for s in series:
        color = s.get("color", PALETTE[0])
        segs, cur = [], []
        for t, v in s["points"]:
            if v is None:
                if len(cur) > 1:
                    segs.append(cur)
                cur = []
            else:
                cur.append((px(t), py(max(lo, min(hi, v)))))
        if len(cur) > 1:
            segs.append(cur)
        elif len(cur) == 1:
            svg.add(f'<circle cx="{_n(cur[0][0])}" cy="{_n(cur[0][1])}" r="2.4" '
                    f'fill="{color}"/>')
        for seg in segs:
            d = "M" + " L".join(f"{_n(x)} {_n(y)}" for x, y in seg)
            if fill:
                area = (d + f" L{_n(seg[-1][0])} {_n(mt + ph)} "
                            f"L{_n(seg[0][0])} {_n(mt + ph)} Z")
                svg.add(f'<path d="{area}" fill="{color}" opacity="0.14"/>')
            svg.add(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="1.7" '
                    f'stroke-linejoin="round" stroke-linecap="round"/>')
    last_label_x = -1e9
    for m in (markers or []):
        x = px(m["ts"])
        svg.add(f'<line x1="{_n(x)}" y1="{mt}" x2="{_n(x)}" y2="{mt + ph}" '
                f'stroke="{m.get("color", C_BAD)}" stroke-width="1.2" '
                f'stroke-dasharray="3 3" opacity="0.75"/>')
        # Two switches minutes apart would otherwise print their labels on top of
        # each other; the dashed lines still mark every one of them.
        if m.get("label") and x - last_label_x > 58:
            svg.text(x + 3, mt + 11, m["label"], cls="lbl muted")
            last_label_x = x
    _legend(svg, ml, height - 12,
            [(s["name"], s.get("color", PALETTE[0])) for s in series], pw)
    return svg.save(path, title)


def chart_hbars(path, rows, title, subtitle="", value_fmt="{:.2f}", width=1000,
                unit="", colors=None):
    """rows: [(label, value, optional_note)] drawn as horizontal bars."""
    rows = [r for r in rows if r[1] is not None]
    height = 60 + max(1, len(rows)) * 26 + 18
    svg = Svg(width, height)
    svg.text(14, 22, title, cls="title ink")
    if subtitle:
        svg.text(14, 38, subtitle, cls="sub muted")
    if not rows:
        svg.text(width / 2, height / 2, "no data", cls="lbl muted", anchor="middle")
        return svg.save(path, title)
    ml, mr = 150, 230
    pw = width - ml - mr
    top = 56
    hi = max(r[1] for r in rows) or 1
    for i, row in enumerate(rows):
        label, value = row[0], row[1]
        note = row[2] if len(row) > 2 else ""
        y = top + i * 26
        color = (colors or {}).get(label) or PALETTE[i % len(PALETTE)]
        svg.text(ml - 10, y + 13, label, cls="lbl ink", anchor="end")
        svg.rect(ml, y + 3, pw, 15, "none", ' class="plot" rx="3"')
        svg.rect(ml, y + 3, max(2.0, pw * value / hi), 15, color, ' rx="3"')
        svg.text(ml + pw + 12, y + 15, value_fmt.format(value) + unit, cls="val ink")
        if note:
            svg.text(ml + pw + 92, y + 15, str(note)[:26], cls="lbl muted")
    return svg.save(path, title)


def chart_strip(path, points, title, subtitle="", width=1000, height=124):
    """Status-page ribbon: one block per bucket, coloured by quality 0..1."""
    if not points:
        svg = Svg(width, height)
        svg.text(14, 22, title, cls="title ink")
        svg.text(width / 2, height / 2, "no data", cls="lbl muted", anchor="middle")
        return svg.save(path, title)
    ml, mr, top, bh = 14, 14, 50, 34
    pw = width - ml - mr
    height += (legend_rows([("100%", 0), ("≥98%", 0), ("≥90%", 0), (">0%", 0),
                            ("total loss", 0), ("no data", 0)], pw) - 1) * 16
    svg = Svg(width, height)
    svg.text(14, 22, title, cls="title ink")
    if subtitle:
        svg.text(14, 38, subtitle, cls="sub muted")
    # Fold to at most one block per pixel, keeping the *worst* value in each bucket
    # so a single bad minute inside a long capture is still visible.
    points = downsample(points, int(pw), how="min")
    n = len(points)
    bw = pw / n
    for i, (_t, q) in enumerate(points):
        if q is None:
            color = "#9aa4b5"
        elif q >= 0.999:
            color = C_GOOD
        elif q >= 0.98:
            color = "#7ac26a"
        elif q >= 0.9:
            color = C_WARN
        elif q > 0:
            color = "#e2703a"
        else:
            color = C_BAD
        svg.rect(ml + i * bw, top, max(0.6, bw - (0.5 if bw > 3 else 0)), bh, color)
    t0, t1 = points[0][0], points[-1][0]
    for t, label in time_ticks(t0, t1, 8):
        x = ml + (t - t0) / max(1e-9, t1 - t0) * pw
        svg.text(x, top + bh + 16, label, cls="lbl muted", anchor="middle")
    _legend(svg, ml, height - 8,
            [("100%", C_GOOD), ("≥98%", "#7ac26a"), ("≥90%", C_WARN),
             (">0%", "#e2703a"), ("total loss", C_BAD), ("no data", "#9aa4b5")], pw)
    return svg.save(path, title)


def chart_wan_timeline(path, stints, ips, title, subtitle="", width=1000, height=150):
    """One coloured band per active uplink, so failovers are visible at a glance."""
    if not stints:
        svg = Svg(width, height)
        svg.text(14, 22, title, cls="title ink")
        svg.text(width / 2, height / 2, "no data", cls="lbl muted", anchor="middle")
        return svg.save(path, title)
    ml, mr, top, bh = 14, 14, 52, 36
    pw = width - ml - mr
    t0 = min(s["start"] for s in stints)
    t1 = max(s["end"] for s in stints)
    order = []
    for s in stints:
        if s["ip"] not in order:
            order.append(s["ip"])
    color_of = {ip: PALETTE[i % len(PALETTE)] for i, ip in enumerate(order)}
    items = []
    for ip in order:
        meta = (ips or {}).get(ip, {})
        items.append((ip + (f" · AS{meta['asn']}" if meta.get("asn") else ""), color_of[ip]))
    height += (legend_rows(items, pw) - 1) * 16
    svg = Svg(width, height)
    svg.text(14, 22, title, cls="title ink")
    if subtitle:
        svg.text(14, 38, subtitle, cls="sub muted")
    svg.rect(ml, top, pw, bh, "none", ' class="plot" rx="4"')
    for s in stints:
        x0 = ml + (s["start"] - t0) / max(1e-9, t1 - t0) * pw
        x1 = ml + (s["end"] - t0) / max(1e-9, t1 - t0) * pw
        svg.rect(x0, top, max(1.2, x1 - x0), bh, color_of[s["ip"]])
    for t, label in time_ticks(t0, t1, 8):
        x = ml + (t - t0) / max(1e-9, t1 - t0) * pw
        svg.text(x, top + bh + 16, label, cls="lbl muted", anchor="middle")
    _legend(svg, ml, height - 8, items, pw)
    return svg.save(path, title)


def chart_heatmap(path, matrix, row_labels, col_labels, title, subtitle="",
                  width=1000, value_fmt="{:.2f}", high_is_bad=True, unit=""):
    """matrix[r][c] = value or None."""
    rows, cols = len(matrix), len(col_labels)
    cell = min(38, max(18, (width - 130) / max(1, cols)))
    height = 70 + rows * cell + 34
    svg = Svg(width, height)
    svg.text(14, 22, title, cls="title ink")
    if subtitle:
        svg.text(14, 38, subtitle, cls="sub muted")
    vals = [v for r in matrix for v in r if v is not None]
    if not vals:
        svg.text(width / 2, height / 2, "no data", cls="lbl muted", anchor="middle")
        return svg.save(path, title)
    lo, hi = min(vals), max(vals)
    ml, top = 110, 60
    for c, label in enumerate(col_labels):
        svg.text(ml + c * cell + cell / 2, top - 8, label, cls="lbl muted", anchor="middle")
    for r in range(rows):
        svg.text(ml - 10, top + r * cell + cell / 2 + 4, row_labels[r],
                 cls="lbl ink", anchor="end")
        for c in range(cols):
            v = matrix[r][c]
            x, y = ml + c * cell, top + r * cell
            if v is None:
                svg.rect(x + 1, y + 1, cell - 2, cell - 2, "#aeb6c4", ' opacity="0.25" rx="3"')
                continue
            frac = 0.0 if hi == lo else (v - lo) / (hi - lo)
            if not high_is_bad:
                frac = 1 - frac
            red = int(23 + frac * (214 - 23))
            green = int(166 - frac * (166 - 69))
            blue = int(115 - frac * (115 - 69))
            svg.rect(x + 1, y + 1, cell - 2, cell - 2, f"rgb({red},{green},{blue})", ' rx="3"')
            if cell >= 30:
                svg.text(x + cell / 2, y + cell / 2 + 4, value_fmt.format(v),
                         cls="lbl", anchor="middle",
                         extra=' fill="#ffffff" opacity="0.92"')
    svg.text(ml, height - 12, f"low {value_fmt.format(lo)}{unit}"
                              f"   →   high {value_fmt.format(hi)}{unit}",
             cls="lbl muted")
    return svg.save(path, title)


def chart_gauge(path, score, grade, parts, title="Stability score", width=520, height=250):
    svg = Svg(width, height)
    svg.text(14, 22, title, cls="title ink")
    cx, cy, r = 130, 168, 92
    svg.add(f'<path d="M {cx - r} {cy} A {r} {r} 0 0 1 {cx + r} {cy}" fill="none" '
            f'stroke="#dde2ea" stroke-width="18" stroke-linecap="round"/>')
    frac = max(0.0, min(1.0, score / 100.0))
    angle = math.pi * (1 - frac)
    x = cx + r * math.cos(angle)
    y = cy - r * math.sin(angle)
    color = C_GOOD if score >= 88 else (C_WARN if score >= 68 else C_BAD)
    large = 0
    svg.add(f'<path d="M {cx - r} {cy} A {r} {r} 0 {large} 1 {_n(x)} {_n(y)}" fill="none" '
            f'stroke="{color}" stroke-width="18" stroke-linecap="round"/>')
    svg.text(cx, cy - 12, f"{score:.1f}", cls="big ink", anchor="middle")
    svg.text(cx, cy + 12, f"grade {grade}", cls="sub muted", anchor="middle")
    y0 = 60
    for i, (name, d) in enumerate(sorted(parts.items(), key=lambda kv: -kv[1]["weight"])):
        yy = y0 + i * 26
        svg.text(258, yy, f"{name} ({d['weight']:.0f}%)", cls="lbl ink")
        svg.rect(258, yy + 6, 220, 9, "none", ' class="plot" rx="4"')
        col = C_GOOD if d["score"] >= 88 else (C_WARN if d["score"] >= 68 else C_BAD)
        svg.rect(258, yy + 6, max(2.0, 220 * d["score"] / 100.0), 9, col, ' rx="4"')
        svg.text(486, yy + 15, f"{d['score']:.0f}", cls="val ink")
    return svg.save(path, title)


def chart_hist(path, values, title, subtitle="", width=1000, height=300, unit="ms"):
    svg = Svg(width, height)
    svg.text(14, 22, title, cls="title ink")
    if subtitle:
        svg.text(14, 38, subtitle, cls="sub muted")
    vals = sorted(v for v in values if v is not None)
    if len(vals) < 5:
        svg.text(width / 2, height / 2, "not enough data", cls="lbl muted", anchor="middle")
        return svg.save(path, title)
    lo = vals[0]
    hi = percentile(vals, 99.5) or vals[-1]
    if hi <= lo:
        hi = lo + 1
    bins = 48
    counts = [0] * bins
    for v in vals:
        idx = int((min(v, hi) - lo) / (hi - lo) * (bins - 1))
        counts[max(0, min(bins - 1, idx))] += 1
    ml, mr, mt, mb = 56, 56, 52, 40
    pw, ph = width - ml - mr, height - mt - mb
    top = max(counts) or 1
    svg.rect(ml, mt, pw, ph, "none", ' class="plot"')
    bw = pw / bins
    for i, c in enumerate(counts):
        h = ph * c / top
        svg.rect(ml + i * bw, mt + ph - h, max(1.0, bw - 1), h, PALETTE[0], ' rx="1"')
    cum, pts = 0, []
    for i, c in enumerate(counts):
        cum += c
        pts.append((ml + i * bw + bw / 2, mt + ph - ph * cum / len(vals)))
    svg.add('<path d="M' + " L".join(f"{_n(x)} {_n(y)}" for x, y in pts) +
            f'" fill="none" stroke="{PALETTE[3]}" stroke-width="1.6"/>')
    for frac, label in ((0.5, "p50"), (0.95, "p95"), (0.99, "p99")):
        v = percentile(vals, frac * 100)
        if v is None or v > hi:
            continue
        x = ml + (v - lo) / (hi - lo) * pw
        svg.add(f'<line x1="{_n(x)}" y1="{mt}" x2="{_n(x)}" y2="{mt + ph}" '
                f'stroke="{C_BAD}" stroke-width="1" stroke-dasharray="4 3"/>')
        svg.text(x + 4, mt + 12, f"{label} {v:.0f}", cls="lbl muted")
    for i in range(6):
        v = lo + (hi - lo) * i / 5
        x = ml + pw * i / 5
        svg.text(x, mt + ph + 16, f"{v:.0f}", cls="lbl muted", anchor="middle")
    svg.text(ml, mt + ph + 32, f"round-trip time, {unit}", cls="lbl muted")
    svg.text(ml + pw, mt + ph + 32, "line = cumulative share", cls="lbl muted", anchor="end")
    return svg.save(path, title)


def chart_stacked(path, categories, layers, title, subtitle="", width=1000, unit="ms"):
    """layers: [(name, color, [values per category])] drawn as stacked h-bars."""
    height = 66 + len(categories) * 30 + 26
    svg = Svg(width, height)
    svg.text(14, 22, title, cls="title ink")
    if subtitle:
        svg.text(14, 38, subtitle, cls="sub muted")
    if not categories:
        svg.text(width / 2, height / 2, "no data", cls="lbl muted", anchor="middle")
        return svg.save(path, title)
    ml, mr, top = 210, 92, 58
    pw = width - ml - mr
    totals = [sum(l[2][i] or 0 for l in layers) for i in range(len(categories))]
    hi = max(totals) or 1
    for i, cat in enumerate(categories):
        y = top + i * 30
        svg.text(ml - 10, y + 15, cat, cls="lbl ink", anchor="end")
        svg.rect(ml, y + 3, pw, 18, "none", ' class="plot" rx="3"')
        x = ml
        for name, color, values in layers:
            v = values[i] or 0
            w = pw * v / hi
            if w > 0.5:
                svg.rect(x, y + 3, w, 18, color)
            x += w
        svg.text(ml + pw + 10, y + 17, f"{totals[i]:.0f} {unit}", cls="val ink")
    _legend(svg, ml, height - 8, [(l[0], l[1]) for l in layers], pw)
    return svg.save(path, title)


# --------------------------------------------------------------------------- #
# Markdown report
# --------------------------------------------------------------------------- #
def md_table(headers, rows, align=None):
    if not rows:
        return "_no data_\n"
    align = align or ["---"] * len(headers)
    out = ["| " + " | ".join(str(h) for h in headers) + " |",
           "|" + "|".join(align) + "|"]
    for r in rows:
        out.append("| " + " | ".join("—" if c is None else str(c) for c in r) + " |")
    return "\n".join(out) + "\n"


def ascii_spark(points, width=64):
    return sparkline([v for _t, v in points], width=width)


def _sev_badge(sev):
    return {"critical": "🔴 **critical**", "warning": "🟠 **warning**",
            "info": "🔵 info", "ok": "🟢 ok"}.get(sev, sev)


def _uplink_title(ip, meta):
    meta = meta or {}
    name = meta.get("as_name") or ""
    if meta.get("asn"):
        return f"`{ip}` · AS{meta['asn']}" + (f" {name}" if name else "")
    return f"`{ip}`"


def build_report(A, out_dir, chart_dir="charts"):
    """Render the whole analysis into report.md plus a folder of SVG charts."""
    os.makedirs(out_dir, exist_ok=True)
    cdir = os.path.join(out_dir, chart_dir)
    os.makedirs(cdir, exist_ok=True)

    def cpath(name):
        return os.path.join(cdir, name)

    def cref(name):
        return f"{chart_dir}/{name}"

    md = []
    W = md.append
    started, ended = A["started"], A["ended"]
    host = A["host"]
    net = A["net"]
    inet = _internet_targets(A)
    anchors = {n: t for n, t in inet.items() if n not in ("isp-hop", "ipv6")}
    charts = []

    # ---------------------------------------------------------------- header
    title = A["label"] or host.get("hostname") or "connection"
    W(f"# 🌐 netwatch report — {title}\n")
    W(f"> Capture from **{ts_str(started)}** to **{ts_str(ended)}** "
      f"({fmt_dur(A['duration'])}) · generated by `{APP} {VERSION}` on "
      f"{ts_str(now_ts())}\n")
    meta_rows = [
        ("Host", f"{host.get('hostname', '?')} · {host.get('distro') or host.get('os', '')}"
                 + (f" · {host['container']}" if host.get("container") else "")),
        ("Interface", f"`{net.get('iface') or '?'}`"
                      + (f" · Wi-Fi `{net['wifi']}`" if net.get("wifi") else "")
                      + (f" · link {A['link']['link_mbps']:.0f} Mbps"
                         if A.get("link", {}).get("link_mbps") else "")),
        ("Gateway", f"`{net.get('gateway') or 'unknown'}`"),
        ("Routers on the way out",
         " → ".join(f"`{h}`" for h in
                    [net.get("gateway"), net.get("lan_hop"), net.get("isp_hop")] if h)
         + ("  (last one is the provider's edge)" if net.get("isp_hop") else "")),
        ("Local address", f"`{net.get('local_ip') or '?'}`"),
        ("Path MTU", str(A.get("mtu") or "not measured")),
        ("Samples stored", f"{sum(A['rows'].values()):,} rows in `{os.path.basename(A['db'])}`"),
    ]
    uplinks = A.get("wan_quality") or {}
    if uplinks:
        meta_rows.insert(3, ("Public uplink(s)",
                             " · ".join(_uplink_title(ip, q) for ip, q in
                                        sorted(uplinks.items(),
                                               key=lambda kv: -(kv[1].get("share_pct") or 0)))))
    W(md_table(["Property", "Value"], meta_rows))

    # ---------------------------------------------------------------- verdict
    W("\n## 🧾 Verdict\n")
    gauge = cpath("score.svg")
    chart_gauge(gauge, A["score"], A["grade"], A["score_parts"])
    charts.append(cref("score.svg"))
    W(f'<img src="{cref("score.svg")}" alt="stability score" width="520">\n')
    crit = [f for f in A["findings"] if f["severity"] == "critical"]
    warn_f = [f for f in A["findings"] if f["severity"] == "warning"]
    if crit:
        headline = (f"**{len(crit)} critical problem(s)** and {len(warn_f)} warning(s) were "
                    f"found. Start with the first block below — the findings are ordered by "
                    f"severity.")
    elif warn_f:
        headline = (f"No critical faults, but **{len(warn_f)} warning(s)** worth acting on.")
    else:
        headline = "**Everything measured clean.** No faults were detected in this capture."
    W(f"\n**Stability score {A['score']:.1f}/100 — grade {A['grade']}.** {headline}\n")
    if A.get("uptime_pct") is not None:
        W(f"\nAvailability **{A['uptime_pct']:.4f}%** · {len(A['outages'])} outage(s) totalling "
          f"**{fmt_dur(A['outage_total_s'])}** · longest **{fmt_dur(A['outage_max_s'])}**"
          + (f" · uplink switched **{len(A.get('wan_switches') or [])}** time(s)"
             if A.get("wan_enabled") else "") + ".\n")

    W("\n| | Finding |\n|---|---|\n")
    for f in A["findings"]:
        W(f"| {_sev_badge(f['severity'])} | {f['title']} |\n")

    W("\n### Details\n")
    for f in A["findings"]:
        W(f"\n#### {SEV_ICON.get(f['severity'], '•')} {f['title']}\n")
        W(f"\n{f['detail']}\n")
        if f.get("evidence"):
            W("\n")
            for e in f["evidence"]:
                W(f"- `{e}`\n")
        if f.get("fix"):
            W(f"\n> **What to do:** {f['fix']}\n")

    # ------------------------------------------------------------- availability
    W("\n---\n\n## 📶 Availability\n")
    if A["quality"]:
        chart_strip(cpath("availability.svg"), A["quality"],
                    "Availability over time — one block per minute",
                    f"green = every probe answered · red = total loss · "
                    f"{fmt_dur(A['duration'])} of capture")
        charts.append(cref("availability.svg"))
        W(f"\n![availability]({cref('availability.svg')})\n")
    W(md_table(
        ["Metric", "Value"],
        [("Availability", fmt_pct(A["uptime_pct"], 4)),
         ("Seconds up / down", f"{A['slots']['up']:,} / {A['slots']['down']:,}"),
         ("Outages (≥ %d s)" % max(1, int(A["config"].get("outage_ticks", 2))),
          f"{len(A['outages'])}"),
         ("Total downtime", fmt_dur(A["outage_total_s"])),
         ("Longest outage", fmt_dur(A["outage_max_s"])),
         ("Average outage", fmt_dur(A["mttr_s"])),
         ("Downtime per 24 h (extrapolated)",
          fmt_dur(A["outage_total_s"] / max(1.0, A["duration"]) * 86400)),
         ("Gaps with no data", f"{A['slots']['unknown']:,} s")]))
    if A["outages"]:
        W("\n**Every interruption, in order.** The *gateway* column is the key one: `down` "
          "means your own router stopped answering too (local fault), `up` means only the "
          "path beyond it broke (ISP side).\n\n")
        rows = []
        for o in A["outages"][:200]:
            rows.append((ts_str(o["start"]), ts_str(o["end"], False),
                         fmt_dur(o["seconds"]),
                         {"down": "🔴 down", "up": "🟢 up", "unknown": "—"}.get(o["gateway"]),
                         "local link / router" if o["gateway"] == "down"
                         else ("beyond the router" if o["gateway"] == "up" else "unclear")))
        W(md_table(["Started", "Ended", "Duration", "Gateway", "Most likely scope"], rows))
        if len(A["outages"]) > 200:
            W(f"\n_…and {len(A['outages']) - 200} more — query the database for the full list._\n")

    # ------------------------------------------------------------------ latency
    W("\n---\n\n## ⏱ Latency, jitter and loss per target\n")
    W("\nEach target isolates one segment of the path, in order of distance: `gateway` is "
      "your own router, `lan-hop` is a second router still inside your network (a firewall or "
      "the load balancer), `isp-edge` is the first address that belongs to the provider, and "
      "the public anchors sit on three independent operators. Read the table downwards: the "
      "row where loss first appears is where the fault begins.\n\n")
    rows = []
    role_order = {"gateway": 0, "lan": 1, "isp": 2, "anchor": 3, "custom": 4, "anchor6": 5}
    roles = A.get("roles") or {}
    addr = {x["name"]: x.get("host") for x in (net.get("targets") or []) if x.get("name")}
    for name, t in sorted(A["targets"].items(),
                          key=lambda kv: (role_order.get(roles.get(kv[0]), 9),
                                          -(kv[1]["p50"] or 0) if roles.get(kv[0]) == "anchor"
                                          else 0, kv[0])):
        rows.append((f"`{name}`", f"`{addr.get(name) or '?'}`",
                     f"{t['sent']:,}", fmt_pct(t["loss_pct"]),
                     fmt_ms(t["min"]), fmt_ms(t["p50"]), fmt_ms(t["p95"]),
                     fmt_ms(t["p99"]), fmt_ms(t["max"]), fmt_ms(t["jitter"]),
                     fmt_dur(t["max_gap_s"]) if t["max_gap_s"] else "—",
                     f"{t['mos']:.2f}" if t.get("mos") else "—",
                     t.get("ttl_main") or "—"))
    W(md_table(["Target", "Address", "Probes", "Loss", "min", "p50", "p95", "p99", "max",
                "Jitter", "Longest gap", "MOS", "TTL"], rows))
    W("\n_MOS is the ITU E-model estimate of voice quality (4.4 excellent, below 3.6 rough). "
      "TTL is the most common reply hop-limit — a change there means the route changed._\n")

    if A["series"]:
        series = []
        for i, (name, s) in enumerate(A["series"].items()):
            series.append({"name": name, "color": PALETTE[i % len(PALETTE)],
                           "points": s["rtt"]})
        shades = [(o["start"], o["end"]) for o in A["outages"]]
        markers = [{"ts": s["ts"], "label": "switch", "color": PALETTE[4]}
                   for s in (A.get("wan_switches") or [])[:40]]
        chart_timeline(cpath("latency.svg"), series,
                       "Round-trip time per target (per-minute average)",
                       "red bands = outages · dashed lines = uplink switches",
                       y_label="ms", shades=shades, markers=markers)
        charts.append(cref("latency.svg"))
        W(f"\n![latency]({cref('latency.svg')})\n")

        loss_series = [{"name": name, "color": PALETTE[i % len(PALETTE)],
                        "points": s["loss"]}
                       for i, (name, s) in enumerate(A["series"].items())]
        chart_timeline(cpath("loss.svg"), loss_series,
                       "Packet loss per target (per-minute %)", "lower is better",
                       y_label="% lost", y_fmt="{:.1f}", fill=True, shades=shades)
        charts.append(cref("loss.svg"))
        W(f"\n![packet loss]({cref('loss.svg')})\n")

    if anchors:
        best = min(anchors.items(), key=lambda kv: (kv[1]["loss_pct"], kv[1]["p50"] or 1e9))
        chart_hist(cpath("latency-distribution.svg"), best[1]["rtts"].vals,
                   f"Latency distribution — {best[0]}",
                   "bars = how often each latency occurred · line = cumulative share")
        charts.append(cref("latency-distribution.svg"))
        W(f"\n![latency distribution]({cref('latency-distribution.svg')})\n")
    chart_hbars(cpath("loss-by-target.svg"),
                [(n, t["loss_pct"], f"{t['sent']:,} probes") for n, t in A["targets"].items()],
                "Packet loss by target", "percentage of probes that never came back",
                value_fmt="{:.3f}", unit=" %")
    charts.append(cref("loss-by-target.svg"))
    W(f"\n![loss by target]({cref('loss-by-target.svg')})\n")
    W(f"\nGlobal per-minute loss, as text: `{ascii_spark(A['global_loss'])}`\n")
    W(f"\nGlobal per-minute latency, as text: `{ascii_spark(A['global_rtt'])}`\n")

    # ---------------------------------------------------------------- failover
    if A.get("wan_enabled"):
        W("\n---\n\n## 🔀 Uplink / balancer behaviour\n")
        W(f"\nThe public address was sampled every "
          f"**{A['config'].get('wan_interval', 2):g} s** with a single DNS packet "
          f"(`myip.opendns.com`, `whoami.cloudflare`, Google's TXT oracle in rotation), and "
          f"every ICMP reply's TTL was recorded as a second, higher-resolution signal. "
          f"{A['wan_samples']:,} lookups were made"
          + (f", {A['wan_fail_pct']:.1f}% of which failed" if A.get("wan_fail_pct") else "")
          + ".\n")
        stints = A.get("wan_stints") or []
        if stints:
            chart_wan_timeline(cpath("wan-timeline.svg"), stints, A.get("wan_ips"),
                               "Which uplink carried the traffic",
                               "each colour is one public IP — every colour change is a failover")
            charts.append(cref("wan-timeline.svg"))
            W(f"\n![uplink timeline]({cref('wan-timeline.svg')})\n")
        q = A.get("wan_quality") or {}
        if q:
            rows = []
            for ip, v in sorted(q.items(), key=lambda kv: -(kv[1].get("share_pct") or 0)):
                rows.append((_uplink_title(ip, v), v.get("cc") or "—",
                             f"{v.get('share_pct', 0):.1f}%", fmt_dur(v.get("seconds")),
                             v.get("stints"), f"{v.get('probes', 0):,}",
                             fmt_pct(v.get("loss_pct")), fmt_ms(v.get("p50")),
                             fmt_ms(v.get("p95")), fmt_ms(v.get("jitter")),
                             f"{v['mos']:.2f}" if v.get("mos") else "—",
                             v.get("ttl") or "—"))
            W("\n**Connection quality measured separately for each uplink** — this is what the "
              "balancer is actually choosing between:\n\n")
            W(md_table(["Uplink", "CC", "Share", "Time active", "Stints", "Probes", "Loss",
                        "p50", "p95", "Jitter", "MOS", "TTL"], rows))
            chart_hbars(cpath("wan-quality.svg"),
                        [(ip, v.get("loss_pct") or 0.0,
                          f"{fmt_ms(v.get('p50'))} ms median") for ip, v in q.items()],
                        "Packet loss while each uplink was active",
                        "measured from the ICMP stream, split by the active public IP",
                        value_fmt="{:.3f}", unit=" %")
            charts.append(cref("wan-quality.svg"))
            W(f"\n![uplink quality]({cref('wan-quality.svg')})\n")
        sw = A.get("wan_switches") or []
        if sw:
            W(f"\n**Every switch detected ({len(sw)}).** *Gap* is the time between the last "
              "confirmation of the old address and the first of the new one — the upper bound on "
              "how quickly the change was noticed. *Lost* estimates the connectivity actually "
              "missing around the switch.\n\n")
            W(md_table(["When", "From", "To", "Gap", "Lost"],
                       [(ts_str(s["ts"]), f"`{s['from']}`", f"`{s['to']}`",
                         f"{s['gap_s']:.1f} s",
                         f"{s.get('lost_s', 0):.1f} s" if s.get("lost_s") is not None else "—")
                        for s in sw[:200]]))
        if stints:
            W("\n**How long each stint lasted** (short stints = flapping):\n\n")
            W(md_table(["Started", "Uplink", "Duration", "Confirmations"],
                       [(ts_str(s["start"]), f"`{s['ip']}`", fmt_dur(s["seconds"]),
                         s["samples"]) for s in stints[:200]]))
        ttl = A.get("ttl_switches") or {}
        if ttl:
            W("\n**Reply TTL seen per target** — an independent witness of path changes, at full "
              "ICMP resolution:\n\n")
            W(md_table(["Target", "TTL values seen (count)", "Changes"],
                       [(f"`{n}`", ", ".join(f"{k}×{c}" for k, c in sorted(v["values"].items())),
                         v["changes"]) for n, v in ttl.items()]))

    # --------------------------------------------------------------------- DNS
    W("\n---\n\n## 🌐 DNS\n")
    if A.get("dns"):
        rows = []
        for (name, proto), d in sorted(A["dns"].items(),
                                       key=lambda kv: (kv[1]["fail_pct"], kv[1]["p50"] or 1e9)):
            rows.append((f"`{name}`", proto, f"{d['n']:,}", fmt_pct(d["fail_pct"], 1),
                         fmt_ms(d["p50"]), fmt_ms(d["p95"]), fmt_ms(d["max"]),
                         ", ".join(f"{k}×{v}" for k, v in d["errors"].items()) or "—"))
        W(md_table(["Resolver", "Proto", "Queries", "Failed", "p50 ms", "p95 ms",
                    "max ms", "Errors"], rows))
        chart_hbars(cpath("dns.svg"),
                    [(f"{n} ({p})", d["p50"] or 0, f"{d['fail_pct']:.1f}% failed")
                     for (n, p), d in A["dns"].items() if d["p50"] is not None],
                    "DNS response time by resolver (median)",
                    "lower is better — the fastest healthy resolver is the one to configure",
                    value_fmt="{:.0f}", unit=" ms")
        charts.append(cref("dns.svg"))
        W(f"\n![dns]({cref('dns.svg')})\n")
    W(f"\nNXDOMAIN hijack checks: **{A['dns_nxchecks']}** performed, "
      f"**{len(A['dns_hijack'])}** answered with an address"
      + (f" (`{', '.join(sorted(set(A['dns_hijack'])))}`)" if A["dns_hijack"] else "") + ".\n")

    # -------------------------------------------------------------------- HTTP
    W("\n---\n\n## 🔗 HTTP / TLS\n")
    if A.get("http"):
        rows = []
        for url, h in A["http"].items():
            rows.append((f"`{url}`", f"{h['n']:,}", fmt_pct(h["ok_pct"], 1),
                         fmt_ms(h["ttfb_p50"], 0), fmt_ms(h["ttfb_p95"], 0),
                         fmt_ms(h["total_p50"], 0),
                         ", ".join(f"{k}×{v}" for k, v in sorted(h["status"].items())) or "—",
                         ", ".join(h["tls"]) or "—",
                         h["cert_days"] if h["cert_days"] is not None else "—",
                         ", ".join(f"{k}×{v}" for k, v in h["errors"].items()) or "—"))
        W(md_table(["Endpoint", "Requests", "Success", "TTFB p50", "TTFB p95", "Total p50",
                    "Status codes", "TLS", "Cert days", "Errors"], rows))
        cats, layers = [], [("DNS", PALETTE[0], []), ("TCP", PALETTE[1], []),
                            ("TLS", PALETTE[2], []), ("server wait", PALETTE[3], [])]
        for url, h in A["http"].items():
            if not h["phase_avg"]:
                continue
            cats.append(urllib.parse.urlsplit(url).hostname or url)
            for i in range(4):
                layers[i][2].append(h["phase_avg"][i])
        if cats:
            chart_stacked(cpath("http-phases.svg"), cats, layers,
                          "Where the time goes in each request",
                          "average milliseconds per phase — a fat TLS or DNS band is a "
                          "different problem from a fat server-wait band")
            charts.append(cref("http-phases.svg"))
            W(f"\n![http phases]({cref('http-phases.svg')})\n")

    # ------------------------------------------------------------------- speed
    W("\n---\n\n## 🚀 Throughput and bufferbloat\n")
    ss = A.get("speed_stats") or {}
    if ss:
        W(md_table(["Direction", "Tests", "Average", "Median", "Min", "Max", "Spread"],
                   [(d.capitalize(), v["n"], f"{v['avg']:.1f} Mbps", f"{v['p50']:.1f}",
                     f"{v['min']:.1f}", f"{v['max']:.1f}",
                     f"±{v['cv'] * 100:.0f}%" if v.get("cv") else "—")
                    for d, v in ss.items()]))
        plan = A["config"].get("plan_mbps") or 0
        if plan and ss.get("download"):
            W(f"\nYou pay for **{plan:g} Mbps**; the measured average is "
              f"**{ss['download']['avg']:.1f} Mbps** — "
              f"**{100 * ss['download']['avg'] / plan:.0f}%** of the plan.\n")
    if A.get("speed"):
        rows = []
        for direction, tests in A["speed"].items():
            for t in tests:
                rows.append((ts_str(t["ts_start"]), direction, f"{t['mbps']:.1f}",
                             fmt_bytes(t["bytes"]), f"{t['seconds']:.1f} s",
                             t["streams"], t["err"] or "—"))
        if rows:
            W("\n")
            W(md_table(["When", "Direction", "Mbps", "Transferred", "Duration",
                        "Streams", "Error"], rows[:100]))
    series = {}
    for ts, direction, mbps in A.get("speed_series", []):
        series.setdefault(direction, []).append((ts, mbps))
    if series:
        chart_timeline(cpath("speed.svg"),
                       [{"name": d, "color": PALETTE[i], "points": pts}
                        for i, (d, pts) in enumerate(series.items())],
                       "Throughput during each speed test",
                       "sampled twice a second — a sawtooth here means shaping or congestion",
                       y_label="Mbps", y_fmt="{:.0f}")
        charts.append(cref("speed.svg"))
        W(f"\n![speed]({cref('speed.svg')})\n")
    bb = A.get("bufferbloat") or {}
    if bb.get("idle_p50") is not None:
        W("\n### Latency under load\n")
        W("\nThis is what actually breaks calls and games: how far the round trip rises "
          "while the link is saturated.\n\n")
        W(md_table(["State", "Median RTT", "Increase"],
                   [("Idle", f"{bb['idle_p50']:.1f} ms", "—"),
                    ("During download",
                     f"{bb['down_p50']:.1f} ms" if bb["down_p50"] is not None else "—",
                     f"+{bb['delta_down']:.0f} ms" if bb.get("delta_down") is not None else "—"),
                    ("During upload",
                     f"{bb['up_p50']:.1f} ms" if bb["up_p50"] is not None else "—",
                     f"+{bb['delta_up']:.0f} ms" if bb.get("delta_up") is not None else "—")]))
        if bb.get("grade"):
            W(f"\n**Bufferbloat grade: {bb['grade']}** "
              f"(A+ under 5 ms, A under 30 ms, B under 60 ms, C under 200 ms, D under 400 ms).\n")
        chart_hbars(cpath("bufferbloat.svg"),
                    [("idle", bb["idle_p50"], ""),
                     ("downloading", bb["down_p50"] or 0, ""),
                     ("uploading", bb["up_p50"] or 0, "")],
                    "Round-trip time: idle versus saturated",
                    "if the loaded bars tower over the idle one, enable SQM on the router",
                    value_fmt="{:.0f}", unit=" ms",
                    colors={"idle": C_GOOD, "downloading": C_WARN, "uploading": C_WARN})
        charts.append(cref("bufferbloat.svg"))
        W(f"\n![bufferbloat]({cref('bufferbloat.svg')})\n")

    # -------------------------------------------------------------------- path
    W("\n---\n\n## 🧭 Path, MTU and reachability\n")
    if A.get("paths"):
        W(f"\n**{len(A['paths'])} distinct route(s)** to `1.1.1.1` were recorded.\n\n")
        for ts, path in A["paths"][:12]:
            W(f"- `{ts_str(ts)}` — " + (" → ".join(f"`{h}`" for h in path) or "_no hops answered_")
              + "\n")
    last_ts = max(A["traces"]) if A.get("traces") else None
    if last_ts:
        W("\n**Last traceroute in full:**\n\n")
        W(md_table(["Hop", "Address", "RTT"],
                   [(h, f"`{ip}`" if ip else "`*`", f"{r:.1f} ms" if r else "—")
                    for h, ip, r in A["traces"][last_ts]]))
    W(f"\nPath MTU: **{A.get('mtu') or 'not measured'}**"
      + (" (1500 is a clean Ethernet path; 1492 means PPPoE; lower usually means a tunnel)"
         if A.get("mtu") else "") + ".\n")
    if A.get("ports"):
        W("\n**TCP reachability** — a port that never connects may be filtered by the ISP:\n\n")
        W(md_table(["Endpoint", "Attempts", "Reachable", "Average", "Errors"],
                   [(f"`{p['host']}:{p['port']}`", p["n"], fmt_pct(p["ok_pct"], 0),
                     f"{p['avg_ms']:.0f} ms" if p["avg_ms"] else "—",
                     ", ".join(f"{k}×{v}" for k, v in p["err"].items()) or "—")
                    for p in A["ports"].values()]))
    if A["ntp"]["n"]:
        W(f"\n**NTP (UDP/123):** {A['ntp']['ok']}/{A['ntp']['n']} replies"
          + (f", clock offset {A['ntp']['offset']:.0f} ms, round trip "
             f"{A['ntp']['rtt']:.0f} ms" if A["ntp"]["offset"] is not None else "")
          + ".\n")

    # -------------------------------------------------------------- local link
    link = A.get("link") or {}
    if link.get("rows"):
        W("\n---\n\n## 📡 Local link\n")
        W(md_table(["Counter", "Value"],
                   [("Interface", f"`{net.get('iface') or '?'}`"),
                    ("Negotiated link speed", f"{link['link_mbps']:.0f} Mbps"
                     if link.get("link_mbps") else "—"),
                    ("Peak throughput", f"↓ {link['peak_rx']:.1f} / ↑ {link['peak_tx']:.1f} Mbps"),
                    ("Interface errors", f"rx {link['rx_err']} · tx {link['tx_err']}"),
                    ("Dropped frames", f"rx {link['rx_drop']} · tx {link['tx_drop']}"),
                    ("Carrier losses", link["carrier_drops"]),
                    ("Wi-Fi signal",
                     f"{mean(link['wifi']):.0f} dBm average "
                     f"({min(link['wifi']):.0f} … {max(link['wifi']):.0f})"
                     if link.get("wifi") else "not a Wi-Fi interface"),
                    ("Signal ↔ loss correlation",
                     f"r = {link['wifi_loss_r']:.2f}" if link.get("wifi_loss_r") is not None
                     else "—")]))
        s = [{"name": "download", "color": PALETTE[0],
              "points": [(r[0], r[1]) for r in link["rows"]]},
             {"name": "upload", "color": PALETTE[1],
              "points": [(r[0], r[2]) for r in link["rows"]]}]
        if link.get("wifi"):
            s.append({"name": "Wi-Fi dBm (+100)", "color": PALETTE[3],
                      "points": [(r[0], (r[3] + 100) if r[3] is not None else None)
                                 for r in link["rows"]]})
        chart_timeline(cpath("link.svg"), s, "Local interface throughput",
                       "latency spikes that line up with these peaks are your own traffic, "
                       "not the ISP", y_label="Mbps", y_fmt="{:.1f}")
        charts.append(cref("link.svg"))
        W(f"\n![link]({cref('link.svg')})\n")

    # ---------------------------------------------------------------- patterns
    W("\n---\n\n## 🕓 Time-of-day patterns\n")
    hours = A.get("hours") or {}
    if hours:
        cols = [f"{h:02d}" for h in sorted(hours)]
        matrix = [[hours[h]["loss"] for h in sorted(hours)],
                  [hours[h]["rtt"] for h in sorted(hours)]]
        chart_heatmap(cpath("hourly.svg"), matrix, ["loss %", "RTT ms"], cols,
                      "Quality by hour of day",
                      "green = good, red = worst hour in this capture",
                      value_fmt="{:.1f}")
        charts.append(cref("hourly.svg"))
        W(f"\n![hourly]({cref('hourly.svg')})\n")
        W(md_table(["Hour", "Minutes sampled", "Average loss", "Average RTT"],
                   [(f"{h:02d}:00", v["minutes"], fmt_pct(v["loss"]), fmt_ms(v["rtt"]))
                    for h, v in sorted(hours.items())]))
    if A.get("periodic"):
        p = A["periodic"]
        W(f"\n⚠️ **The outages repeat on a schedule** — roughly every "
          f"{fmt_dur(p['period_s'])} (variation {p['cv'] * 100:.0f}% across {p['count']} "
          f"events). Something timed is behind them.\n")
    if A.get("brownouts"):
        W(f"\n**{len(A['brownouts'])} degraded minute(s)** (loss ≥ 2% or latency more than "
          f"three times the {fmt_ms(A['baseline_rtt'])} ms baseline):\n\n")
        W(md_table(["Minute", "Loss", "Average RTT"],
                   [(ts_str(b["minute"]), fmt_pct(b["loss"]), fmt_ms(b["rtt"]))
                    for b in A["brownouts"][:80]]))

    # ------------------------------------------------------------------ events
    W("\n---\n\n## 📋 Event log\n")
    counts = ", ".join(f"`{k}`×{v}" for k, v in A["event_counts"].most_common())
    W(f"\n{len(A['events'])} events recorded: {counts or '—'}\n\n")
    notable = [e for e in A["events"] if e["severity"] in ("critical", "warning")]
    shown = notable[:150] if notable else A["events"][:80]
    W(md_table(["When", "Severity", "Kind", "Message"],
               [(ts_str(e["ts"]), _sev_badge(e["severity"]), f"`{e['kind']}`", e["message"])
                for e in shown]))
    if len(notable) > 150:
        W(f"\n_…and {len(notable) - 150} more in the database._\n")

    # ---------------------------------------------------------------- raw data
    W("\n---\n\n## 🗂 Raw data\n")
    W(f"\nEverything above was computed from `{os.path.basename(A['db'])}` — a plain SQLite "
      f"file you can query yourself. Nothing was kept in memory during the capture.\n\n")
    W(md_table(["Table", "Rows", "What it holds"],
               [("ping_samples", f"{A['rows']['ping_samples']:,}",
                 "one row per ICMP probe: timestamp, target, success, RTT, reply TTL"),
                ("wan_samples", f"{A['rows']['wan_samples']:,}",
                 "public-IP lookups used to detect uplink switches"),
                ("dns_samples", f"{A['rows']['dns_samples']:,}",
                 "per-resolver query results and timings"),
                ("http_samples", f"{A['rows']['http_samples']:,}",
                 "per-request phase timings, status, TLS details"),
                ("speed_tests / speed_series", f"{A['rows']['speed_tests']:,} / "
                                               f"{A['rows']['speed_series']:,}",
                 "throughput results and the per-sample curve"),
                ("trace_hops", f"{A['rows']['trace_hops']:,}", "traceroute snapshots"),
                ("iface_samples", f"{A['rows']['iface_samples']:,}",
                 "local interface counters and Wi-Fi signal"),
                ("port_checks / ntp_samples",
                 f"{A['rows']['port_checks']:,} / {A['rows']['ntp_samples']:,}",
                 "TCP reachability and UDP/NTP checks"),
                ("events", f"{A['rows']['events']:,}",
                 "outages, switches, route changes, everything notable")]))
    W("\nA couple of queries to start with:\n\n```sql\n"
      "-- the ten worst minutes of the capture\n"
      "SELECT datetime(CAST(ts/60 AS INT)*60,'unixepoch','localtime') AS minute,\n"
      "       COUNT(*) AS probes, SUM(ok) AS answered,\n"
      "       ROUND(100.0*(COUNT(*)-SUM(ok))/COUNT(*),2) AS loss_pct\n"
      "FROM ping_samples WHERE target <> 'gateway'\n"
      "GROUP BY minute ORDER BY loss_pct DESC, probes DESC LIMIT 10;\n\n"
      "-- how long each public IP was in use\n"
      "SELECT ip, COUNT(*) AS lookups, datetime(MIN(ts),'unixepoch','localtime') AS first_seen,\n"
      "       datetime(MAX(ts),'unixepoch','localtime') AS last_seen\n"
      "FROM wan_samples WHERE ok=1 GROUP BY ip ORDER BY lookups DESC;\n```\n")

    # ---------------------------------------------------------------- appendix
    W("\n---\n\n## ⚙️ How this was measured\n")
    cfgd = A["config"]
    W(md_table(["Setting", "Value"],
               [("Planned duration", fmt_dur(cfgd.get("duration")) if cfgd.get("duration")
                 else "until stopped"),
                ("Actual duration", fmt_dur(A["duration"])),
                ("ICMP interval / timeout",
                 f"{cfgd.get('ping_interval')} s / {cfgd.get('ping_timeout')} s"),
                ("Public-IP probe interval", f"{cfgd.get('wan_interval')} s"),
                ("DNS / HTTP interval",
                 f"{cfgd.get('dns_interval')} s / {cfgd.get('http_interval')} s"),
                ("Speed test interval",
                 f"every {fmt_dur(cfgd.get('speed_interval'))}" if cfgd.get("speed_interval")
                 else "disabled"),
                ("Traceroute interval",
                 f"every {fmt_dur(cfgd.get('trace_interval'))}" if cfgd.get("trace_interval")
                 else "disabled"),
                ("Outage threshold", f"{cfgd.get('outage_ticks')} consecutive lost seconds"),
                ("Data transferred by speed tests", fmt_bytes(A.get("total_bytes"))),
                ("Capture status", A["status"])]))
    W("\n**How to read the verdict.** Availability is computed per second: a second counts as "
      "*up* if any public anchor answered in it, and *down* only if probes were sent and none "
      "came back — so a two-second dropout is visible, while a suspended laptop is not counted "
      "against the connection. The score weights availability 40%, packet loss 20%, jitter 15%, "
      "DNS 10%, HTTP 10% and bufferbloat 5%; loss and jitter use the *best* public anchor, "
      "because a fault common to three independent networks is yours and a fault on one of them "
      "is theirs.\n")

    report_path = os.path.join(out_dir, "report.md")
    with open(report_path, "w", encoding="utf-8") as fh:
        fh.write("".join(md))
    summary = {
        "score": A["score"], "grade": A["grade"], "uptime_pct": A["uptime_pct"],
        "outages": len(A["outages"]), "downtime_s": A["outage_total_s"],
        "duration_s": A["duration"], "wan_switches": len(A.get("wan_switches") or []),
        "uplinks": {ip: {k: v for k, v in q.items() if k != "rtts"}
                    for ip, q in (A.get("wan_quality") or {}).items()},
        "findings": [{k: f[k] for k in ("severity", "title", "fix")} for f in A["findings"]],
        "speed": A.get("speed_stats"), "bufferbloat": A.get("bufferbloat"),
        "db": A["db"],
    }
    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, default=str)
    return report_path, charts


# --------------------------------------------------------------------------- #
# interactive TUI
# --------------------------------------------------------------------------- #
BANNER = r"""
                 _                    _         _
   _ __    ___ | |_ __      __ __ _ | |_  ___ | |__
  | '_ \  / _ \| __|\ \ /\ / // _` || __|/ __|| '_ \
  | | | ||  __/| |_  \ V  V /| (_| || |_| (__ | | | |
  |_| |_| \___| \__|  \_/\_/  \__,_| \__|\___||_| |_|
"""


def _menu_frame(title, subtitle, items, idx, footer):
    width, _h = term_size()
    width = min(width, 108)
    lines = []
    for row in BANNER.strip("\n").splitlines():
        lines.append(f"{CYAN}{row}{C0}")
    lines.append(f"{GREY}  continuous internet quality monitor · v{VERSION}{C0}")
    lines.append("")
    body = []
    if subtitle:
        body.append(f"{GREY}{subtitle}{C0}")
        body.append("")
    for i, (label, hint) in enumerate(items):
        marker = "▸" if i == idx else " "
        if i == idx:
            body.append(f"{BG_SEL}{WHITE}{marker} {label:<34}{C0} {GREY}{hint}{C0}")
        else:
            body.append(f"{GREY}{marker}{C0} {WHITE}{label:<34}{C0} {GREY}{hint}{C0}")
    lines += box(title, body, width)
    lines.append(f"{GREY}  {footer}{C0}")
    return lines


def menu_select(title, items, subtitle="", footer="↑/↓ move · Enter choose · q quit",
                initial=0):
    """items: [(label, hint)] — returns the chosen index, or None."""
    if not (_TTY and sys.stdin.isatty()):
        print(f"\n{title}")
        for i, (label, hint) in enumerate(items, 1):
            print(f"  {i}. {label}  — {hint}")
        try:
            raw = input("Choose: ").strip()
        except EOFError:
            return None
        return int(raw) - 1 if raw.isdigit() and 1 <= int(raw) <= len(items) else None
    screen = Screen()
    screen.enter()
    idx = initial
    try:
        with KeyReader() as keys:
            if not keys.enabled:
                screen.leave()
                for i, (label, hint) in enumerate(items, 1):
                    print(f"  {i}. {label}  — {hint}")
                raw = input("Choose: ").strip()
                return int(raw) - 1 if raw.isdigit() and 1 <= int(raw) <= len(items) else None
            while True:
                screen.paint(_menu_frame(title, subtitle, items, idx, footer))
                key = keys.get(0.4)
                if key == "up":
                    idx = (idx - 1) % len(items)
                elif key == "down":
                    idx = (idx + 1) % len(items)
                elif key == "enter":
                    return idx
                elif key in ("q", "Q", "esc"):
                    return None
                elif key and key.isdigit():
                    n = int(key)
                    if 1 <= n <= len(items):
                        return n - 1
    finally:
        screen.leave()


def ask(prompt, default=""):
    suffix = f" {GREY}[{default}]{C0}" if default != "" else ""
    try:
        raw = input(f"{BLUE}?{C0} {prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        return default
    return raw or default


def ask_bool(prompt, default=True):
    raw = ask(f"{prompt} (y/n)", "y" if default else "n").lower()
    return raw.startswith("y")


SETTING_FIELDS = [
    ("duration", "How long to monitor", "duration",
     "0 = run until you press q · 90s, 30m, 2h, 1d"),
    ("ping_interval", "ICMP probe interval", "float",
     "seconds between pings to every target — 1 s catches 2-second dropouts"),
    ("wan_interval", "Public-IP / failover probe", "float",
     "seconds between public-IP lookups — lower catches shorter uplink switches"),
    ("dns_interval", "DNS probe interval", "float", "seconds"),
    ("http_interval", "HTTP probe interval", "float", "seconds"),
    ("speed_interval", "Speed test interval", "duration", "0 disables speed tests"),
    ("trace_interval", "Traceroute interval", "duration", "0 disables traceroutes"),
    ("speed_max_mb", "Data cap per speed test", "float", "megabytes, both directions"),
    ("plan_mbps", "Your subscribed speed", "float",
     "Mbps — used to judge the measured throughput (0 = unknown)"),
    ("outage_ticks", "Outage threshold", "int",
     "consecutive lost seconds before it counts as an outage"),
    ("ipv6", "Test IPv6", "bool", ""),
    ("mtu_probe", "Probe the path MTU", "bool", ""),
    ("label", "Label for this capture", "str", "shown in the report title"),
    ("out_dir", "Output directory", "str", "empty = ./netwatch-<timestamp>"),
]


def _fmt_setting(cfg, key, kind):
    v = getattr(cfg, key)
    if kind == "bool":
        return "yes" if v else "no"
    if kind == "duration":
        return fmt_dur(v) if v else "off / unlimited"
    if kind in ("float", "int"):
        return f"{v:g}"
    return str(v) if v else "(default)"


def settings_menu(cfg):
    while True:
        items = [(f"{label}", f"{_fmt_setting(cfg, key, kind)}  — {hint}")
                 for key, label, kind, hint in SETTING_FIELDS]
        items.append(("Extra ping targets", ", ".join(h for _n, h in cfg.extra_targets)
                      or "none — add your own hosts to watch"))
        items.append(("← Back", "keep these settings"))
        idx = menu_select("Settings", items, "Enter changes the highlighted value.")
        if idx is None or idx == len(items) - 1:
            return cfg
        if idx == len(items) - 2:
            raw = ask("Extra targets (comma separated, host or name=host)",
                      ",".join(h for _n, h in cfg.extra_targets))
            cfg.extra_targets = _parse_targets(raw)
            continue
        key, label, kind, hint = SETTING_FIELDS[idx]
        print()
        if kind == "bool":
            setattr(cfg, key, ask_bool(label, bool(getattr(cfg, key))))
            continue
        raw = ask(f"{label} ({hint})" if hint else label, _fmt_setting(cfg, key, kind))
        try:
            if kind == "duration":
                setattr(cfg, key, parse_duration(raw, getattr(cfg, key)))
            elif kind == "float":
                setattr(cfg, key, float(raw))
            elif kind == "int":
                setattr(cfg, key, int(float(raw)))
            else:
                setattr(cfg, key, raw if raw != "(default)" else "")
        except ValueError:
            warn(f"'{raw}' is not a valid value — keeping the previous one.")
            time.sleep(1.2)


def _parse_targets(raw):
    out = []
    for i, chunk in enumerate(x.strip() for x in (raw or "").split(",")):
        if not chunk:
            continue
        if "=" in chunk:
            name, host = chunk.split("=", 1)
        else:
            name, host = f"custom{i + 1}", chunk
        out.append((safe_name(name), host.strip()))
    return out


def summarize_plan(cfg):
    modules = []
    modules.append(f"ICMP every {cfg.ping_interval:g}s")
    modules.append(f"public IP every {cfg.wan_interval:g}s")
    modules.append(f"DNS every {cfg.dns_interval:g}s")
    modules.append(f"HTTP every {cfg.http_interval:g}s")
    modules.append(f"speed {'every ' + fmt_dur(cfg.speed_interval) if cfg.speed_interval else 'off'}")
    modules.append(f"traceroute {'every ' + fmt_dur(cfg.trace_interval) if cfg.trace_interval else 'off'}")
    est = 0
    if cfg.speed_interval and cfg.duration:
        # Each cycle downloads up to the cap and uploads up to half of it.
        per_cycle = cfg.speed_max_mb * (1.5 if cfg.speed_upload else 1.0)
        est = max(1.0, cfg.duration / cfg.speed_interval) * per_cycle
    print()
    print(f"{BOLD}Plan{C0}")
    print(f"  Duration : {fmt_dur(cfg.duration) if cfg.duration else 'until you press q'}")
    print(f"  Probes   : {', '.join(modules)}")
    print(f"  Output   : {cfg.out_dir or './' + APP + '-<timestamp>'}")
    if est:
        print(f"  Data use : up to ~{est:.0f} MB from speed tests")
    print()


def print_summary(A, report_path, out_dir):
    print()
    color = GREEN if A["score"] >= 88 else (YELLOW if A["score"] >= 68 else RED)
    print(f"{BOLD}  Stability score {color}{A['score']:.1f}/100  (grade {A['grade']}){C0}")
    if A.get("uptime_pct") is not None:
        print(f"  Availability {A['uptime_pct']:.4f}%   "
              f"outages {len(A['outages'])}   "
              f"downtime {fmt_dur(A['outage_total_s'])}   "
              f"captured {fmt_dur(A['duration'])}")
    if A.get("wan_enabled") and A.get("wan_quality"):
        print(f"  Uplinks {len(A['wan_quality'])}   "
              f"switches {len(A.get('wan_switches') or [])}"
              + (f"   avg cost {A['wan_switch_cost']:.1f}s"
                 if A.get("wan_switch_cost") else ""))
    print()
    for f in A["findings"][:8]:
        col = SEV_COLOR.get(f["severity"], GREY)
        print(f"  {col}{SEV_ICON.get(f['severity'], '•')} {f['title']}{C0}")
    if len(A["findings"]) > 8:
        print(f"  {GREY}…and {len(A['findings']) - 8} more in the report{C0}")
    print()
    ok(f"Report : {report_path}")
    ok(f"Charts : {os.path.join(out_dir, 'charts')}")
    ok(f"Data   : {A['db']}")
    print(f"{GREY}  Open the report in any Markdown viewer (the charts are plain SVG files "
          f"next to it).{C0}")


def do_capture_and_report(cfg):
    db_path, run_id, out_dir = run_capture(cfg)
    info("Analysing the capture…")
    A = analyze(db_path, run_id)
    info("Rendering charts and the report…")
    report_path, charts = build_report(A, out_dir)
    print_summary(A, report_path, out_dir)
    return report_path


def resolve_db(path):
    """Accept a run directory, a .db file, or a parent directory of runs."""
    if os.path.isdir(path):
        direct = os.path.join(path, f"{APP}.db")
        if os.path.isfile(direct):
            return direct
        candidates = []
        for entry in sorted(os.listdir(path)):
            sub = os.path.join(path, entry, f"{APP}.db")
            if os.path.isfile(sub):
                candidates.append(sub)
            elif entry.endswith(".db"):
                candidates.append(os.path.join(path, entry))
        if candidates:
            return candidates[-1]
        return None
    return path if os.path.isfile(path) else None


def list_runs(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = _q(conn, "SELECT id, started_at, ended_at, label, status FROM runs ORDER BY id")
    counts = {r["id"]: r["c"] for r in _q(
        conn, "SELECT run_id AS id, COUNT(*) AS c FROM ping_samples GROUP BY run_id")}
    conn.close()
    return [(r["id"], r["started_at"], r["ended_at"], r["label"], r["status"],
             counts.get(r["id"], 0)) for r in rows]


def analyze_menu():
    raw = ask("Path to a capture directory or .db file", os.getcwd())
    db_path = resolve_db(os.path.expanduser(raw))
    if not db_path:
        warn(f"No {APP} database found under {raw}")
        return
    runs = list_runs(db_path)
    if not runs:
        warn("That database contains no runs.")
        return
    if len(runs) > 1:
        items = [(f"run #{rid} · {ts_str(st)}",
                  f"{fmt_dur((en or st) - st)} · {n:,} probes · {status}"
                  + (f" · {label}" if label else ""))
                 for rid, st, en, label, status, n in runs]
        idx = menu_select("Pick a run to analyse", items)
        if idx is None:
            return
        run_id = runs[idx][0]
    else:
        run_id = runs[0][0]
    out_dir = os.path.dirname(os.path.abspath(db_path))
    info(f"Analysing run #{run_id} from {db_path}…")
    A = analyze(db_path, run_id)
    report_path, _charts = build_report(A, out_dir)
    print_summary(A, report_path, out_dir)


HELP_TEXT = f"""
{BOLD}What netwatch does{C0}

  It watches your connection continuously and stores every measurement in a local
  SQLite file, then turns that capture into a Markdown report with charts and a
  verdict that names the layer at fault.

{BOLD}The probes{C0}

  ICMP        one echo per second to your router, the ISP's first hop and three
              public anchors on independent networks. Reply TTL is recorded too.
  Public IP   a single DNS packet every couple of seconds asks "what is my address?" —
              that is how a balancer failover between two providers is caught.
  DNS         several resolvers over UDP, TCP and DoH, plus an NXDOMAIN hijack test.
  HTTP        phase-timed HTTPS requests: DNS, TCP, TLS, time-to-first-byte.
  Speed       multi-stream download and upload; latency is measured while the link
              is saturated, which is what produces the bufferbloat grade.
  Path        traceroute snapshots, path-MTU probing, TCP port reachability, NTP.
  Link        interface errors, dropped frames, carrier losses, Wi-Fi signal.

{BOLD}Reading the result{C0}

  The verdict compares layers rather than looking at one number: loss at the router
  is a local fault, loss that only starts at the ISP's hop is theirs, and loss on all
  three public anchors at once is your line. Availability is computed per second, so
  a two-second dropout shows up.

{BOLD}Command line{C0}

  netwatch.sh --duration 2h --plan 100 --yes
  netwatch.sh --quick
  netwatch.sh --analyze ./netwatch-20260821-120000
  netwatch.sh --help
"""


def interactive():
    cfg = Config()
    while True:
        items = [
            ("▶  Start monitoring", f"capture for {fmt_dur(cfg.duration) if cfg.duration else '∞'}"
                                    ", then build the report"),
            ("⚡ Quick diagnostic", "90 seconds, one speed test, immediate verdict"),
            ("🛠  Settings", "duration, intervals, targets, speed plan"),
            ("📊 Analyse a previous capture", "rebuild a report from a saved database"),
            ("❓ Help", "what each probe measures and how to read the report"),
            ("⏻  Quit", ""),
        ]
        idx = menu_select("Main menu", items,
                          f"Output goes to {cfg.out_dir or './' + APP + '-<timestamp>'}")
        if idx is None or idx == 5:
            return 0
        if idx == 0:
            summarize_plan(cfg)
            if not ask_bool("Start now?", True):
                continue
            do_capture_and_report(cfg)
            input(f"\n{GREY}Press Enter to return to the menu…{C0}")
        elif idx == 1:
            quick = Config()
            quick.duration = 90.0
            quick.label = cfg.label or "quick diagnostic"
            quick.dns_interval = 12.0
            quick.http_interval = 12.0
            quick.speed_interval = 60.0
            quick.speed_seconds = 8.0
            quick.trace_interval = 0.0
            quick.plan_mbps = cfg.plan_mbps
            quick.out_dir = cfg.out_dir
            do_capture_and_report(quick)
            input(f"\n{GREY}Press Enter to return to the menu…{C0}")
        elif idx == 2:
            cfg = settings_menu(cfg)
        elif idx == 3:
            analyze_menu()
            input(f"\n{GREY}Press Enter to return to the menu…{C0}")
        elif idx == 4:
            print(HELP_TEXT)
            input(f"\n{GREY}Press Enter to return to the menu…{C0}")


# --------------------------------------------------------------------------- #
# command line
# --------------------------------------------------------------------------- #
FLAGS = {"quick", "yes", "no-tui", "plain", "no-speed", "no-trace", "no-ipv6",
         "no-upload", "no-mtu", "help", "h"}


def parse_args(argv):
    opts = {}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("--"):
            body = a[2:]
            if "=" in body:
                k, v = body.split("=", 1)
            elif ":" in body:
                k, v = body.split(":", 1)
            else:
                k = body
                if k.lower() in FLAGS:
                    v = "true"
                elif i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                    v = argv[i + 1]
                    i += 1
                else:
                    v = "true"
            opts[k.strip().lower()] = v
        elif a in ("-h", "-?"):
            opts["help"] = "true"
        elif a == "-y":
            opts["yes"] = "true"
        elif a == "-q":
            opts["quick"] = "true"
        i += 1
    return opts


def build_config(opts):
    cfg = Config()
    g = lambda *keys: next((opts[k] for k in keys if k in opts), None)  # noqa: E731
    if g("duration", "time", "for") is not None:
        cfg.duration = parse_duration(g("duration", "time", "for"))
    for key, attr in (("interval", "ping_interval"), ("wan-interval", "wan_interval"),
                      ("dns-interval", "dns_interval"), ("http-interval", "http_interval"),
                      ("link-interval", "link_interval"), ("timeout", "ping_timeout")):
        if key in opts:
            setattr(cfg, attr, float(opts[key]))
    for key, attr in (("speed-interval", "speed_interval"),
                      ("trace-interval", "trace_interval"),
                      ("port-interval", "port_interval"), ("ntp-interval", "ntp_interval")):
        if key in opts:
            setattr(cfg, attr, parse_duration(opts[key]))
    if "speed-max-mb" in opts:
        cfg.speed_max_mb = float(opts["speed-max-mb"])
    if "speed-seconds" in opts:
        cfg.speed_seconds = float(opts["speed-seconds"])
    if "streams" in opts:
        cfg.speed_streams = int(opts["streams"])
    if g("plan", "plan-mbps"):
        cfg.plan_mbps = float(g("plan", "plan-mbps"))
    if "outage-ticks" in opts:
        cfg.outage_ticks = int(float(opts["outage-ticks"]))
    if g("targets", "target"):
        cfg.extra_targets = _parse_targets(g("targets", "target"))
    if g("urls", "url"):
        cfg.urls = list(DEFAULT_URLS) + [u.strip() for u in g("urls", "url").split(",") if u.strip()]
    if "resolvers" in opts:
        extra = []
        for item in opts["resolvers"].split(","):
            item = item.strip()
            if item:
                extra.append((item, item))
        if extra:
            cfg.resolvers = [("system", None)] + extra
    if "domains" in opts:
        cfg.domains = [d.strip() for d in opts["domains"].split(",") if d.strip()]
    if g("out", "output", "out-dir"):
        cfg.out_dir = os.path.abspath(os.path.expanduser(g("out", "output", "out-dir")))
    if "db" in opts:
        cfg.db_path = os.path.abspath(os.path.expanduser(opts["db"]))
    if "label" in opts:
        cfg.label = opts["label"]
    if "no-speed" in opts:
        cfg.speed_interval = 0.0
    if "no-upload" in opts:
        cfg.speed_upload = False
    if "no-trace" in opts:
        cfg.trace_interval = 0.0
    if "no-ipv6" in opts:
        cfg.ipv6 = False
    if "no-mtu" in opts:
        cfg.mtu_probe = False
    if "no-tui" in opts or "plain" in opts:
        cfg.tui = False
    if "yes" in opts:
        cfg.assume_yes = True
    if "quick" in opts:
        cfg.duration = 90.0
        cfg.dns_interval = 12.0
        cfg.http_interval = 12.0
        cfg.speed_interval = 60.0 if cfg.speed_interval else 0.0
        cfg.speed_seconds = 8.0
        cfg.trace_interval = 0.0
        cfg.label = cfg.label or "quick diagnostic"
    return cfg


def main(argv):
    opts = parse_args(argv)
    if "help" in opts or "h" in opts:
        print(__doc__.strip())
        return 0
    if "version" in opts:
        print(f"{APP} {VERSION}")
        return 0

    if "analyze" in opts or "analyse" in opts or "report" in opts:
        raw = opts.get("analyze") or opts.get("analyse") or opts.get("report")
        db_path = resolve_db(os.path.abspath(os.path.expanduser(raw)))
        if not db_path:
            die(f"No {APP} database found at {raw}")
        run_id = int(opts["run"]) if "run" in opts else None
        A = analyze(db_path, run_id)
        out_dir = opts.get("out") or os.path.dirname(os.path.abspath(db_path))
        report_path, _charts = build_report(A, out_dir)
        print_summary(A, report_path, out_dir)
        return 0

    if "runs" in opts:
        db_path = resolve_db(os.path.abspath(os.path.expanduser(opts["runs"])))
        if not db_path:
            die(f"No {APP} database found at {opts['runs']}")
        for rid, st, en, label, status, n in list_runs(db_path):
            print(f"  #{rid:<4} {ts_str(st)}  {fmt_dur((en or st) - st):>10}  "
                  f"{n:>10,} probes  {status:<10} {label or ''}")
        return 0

    if not argv:
        if sys.stdin.isatty():
            return interactive()
        die("No arguments and no terminal — pass --duration and --yes, or --help.")

    cfg = build_config(opts)
    if not cfg.assume_yes and sys.stdin.isatty():
        summarize_plan(cfg)
        if not ask_bool("Start now?", True):
            return 1
    do_capture_and_report(cfg)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        print()
        warn("Interrupted.")
        sys.exit(130)
