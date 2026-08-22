#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
loadtest.py — authorized load / WAF / rate-limit filter validation tester.

Purpose
    Generate configurable HTTP load against a target you OWN (or are explicitly
    authorized to test) and measure how well the target's filtering blocks it.
    Optionally routes each request through a rotating pool of HTTP proxies so you
    can validate IP-based rate limiting / WAF rules across many source addresses
    and correlate the result with your own monitoring (e.g. Grafana).

    The primary output is *block effectiveness*: how many requests passed (2xx)
    versus were blocked (401/403/405/406/409/415/429/451) or rate-limited, per
    source, plus latency and throughput.

Authorized testing only
    Only run this against systems you own or have written permission to test.
    Load testing third-party sites without authorization is abuse and may be
    illegal. The tool sends an identifiable User-Agent by default and requires a
    one-time confirmation so test traffic is easy to attribute in your logs.

Usage
    Interactive TUI menu (no arguments):
        ./loadtest.sh

    Non-interactive (arguments may use --k v, --k=v or --k:v forms):
        ./loadtest.sh --url https://my.site --proxy:/path/proxies.txt \
                      --duration 60s --delay 0.1 --concurrency 50 --yes

Proxy file format
    One proxy per line, blank lines and lines starting with '#' ignored:
        login:passwd@ip:port
        ip:port                      (no auth)
        http://login:passwd@ip:port  (scheme optional; http assumed)
    Proxies are picked in random order for every request.

Options
    --url, --target       target URL (required)
    --proxy, --proxies    path to the proxy list txt file
    --no-proxy, --direct  send directly, no proxies (baseline / capacity test)
    --duration, --time    how long to run: 90, 90s, 5m, 1h   (default 30s)
    --delay, --sleep      pause between requests per worker, seconds (default 0)
    --concurrency, -c     number of parallel workers          (default 20)
    --method, -m          HTTP method                         (default GET)
    --timeout             per-request timeout, seconds         (default 10)
    --output, --result    results file (default loadtest-<timestamp>.txt)
    --user-agent, --ua    override the User-Agent header
    --insecure, -k        do not verify TLS certs (self-signed targets)
    --no-tui, --plain     disable the live TUI dashboard (plain log lines)
    --yes, --authorized   confirm you are authorized to test the target
    --help, -h            show this help
"""

from __future__ import annotations

import math
import os
import random
import signal
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

APP = "loadtest"
VERSION = "1.0"

# Status codes that typically indicate the request was actively blocked / limited.
BLOCK_CODES = {401, 403, 405, 406, 409, 415, 429, 451}

AUTH_NOTICE = (
    "Authorized testing only — run this against systems you own or have written\n"
    "permission to test. Unauthorized load testing may be illegal."
)


# --------------------------------------------------------------------------- #
# terminal colours
# --------------------------------------------------------------------------- #
if sys.stdout.isatty():
    C_I, C_OK, C_W, C_E, C_0 = "\033[1;34m", "\033[1;32m", "\033[1;33m", "\033[1;31m", "\033[0m"
else:
    C_I = C_OK = C_W = C_E = C_0 = ""


def info(msg): print(f"{C_I}[*]{C_0} {msg}")
def ok(msg):   print(f"{C_OK}[+]{C_0} {msg}")
def warn(msg): print(f"{C_W}[!]{C_0} {msg}", file=sys.stderr)
def die(msg):  print(f"{C_E}[x]{C_0} {msg}", file=sys.stderr); sys.exit(1)


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #
class Config:
    def __init__(self):
        self.url = ""
        self.method = "GET"
        self.proxies_file = None
        self.use_proxy = False
        self.duration = 30.0
        self.delay = 0.0
        self.concurrency = 20
        self.timeout = 10.0
        self.output = ""
        self.user_agent = f"{APP}/{VERSION} (authorized-testing)"
        self.insecure = False
        self.tui = True
        self.assume_yes = False


# --------------------------------------------------------------------------- #
# argument parsing  (supports --k v, --k=v and --k:v)
# --------------------------------------------------------------------------- #
VALUE_ALIASES = {
    "proxy": "proxies_file", "proxies": "proxies_file", "p": "proxies_file",
    "proxyfile": "proxies_file", "proxy-file": "proxies_file",
    "url": "url", "target": "url", "u": "url",
    "duration": "duration", "time": "duration", "t": "duration",
    "delay": "delay", "sleep": "delay",
    "concurrency": "concurrency", "threads": "concurrency", "parallel": "concurrency",
    "workers": "concurrency", "c": "concurrency",
    "method": "method", "m": "method",
    "timeout": "timeout",
    "output": "output", "out": "output", "o": "output", "result": "output", "results": "output",
    "user-agent": "user_agent", "ua": "user_agent", "useragent": "user_agent",
}
BOOL_ALIASES = {
    "no-proxy": "no_proxy", "direct": "no_proxy", "noproxy": "no_proxy",
    "insecure": "insecure", "k": "insecure",
    "no-tui": "no_tui", "plain": "no_tui", "notui": "no_tui",
    "yes": "yes", "authorized": "yes", "y": "yes",
    "help": "help", "h": "help",
}


def parse_args(argv):
    opts = {}
    i = 0
    while i < len(argv):
        tok = argv[i]
        if not tok.startswith("-"):
            opts.setdefault("url", tok)
            i += 1
            continue
        body = tok.lstrip("-")
        val = None
        sep = -1
        for j, ch in enumerate(body):
            if ch in "=:":
                sep = j
                break
        if sep >= 0:
            key, val = body[:sep], body[sep + 1:]
        else:
            key = body
        key = key.lower()
        if key in BOOL_ALIASES:
            opts[BOOL_ALIASES[key]] = True
            i += 1
            continue
        if key in VALUE_ALIASES:
            dest = VALUE_ALIASES[key]
            if val is None:
                if i + 1 >= len(argv) or argv[i + 1].startswith("--"):
                    die(f"Option --{key} requires a value")
                val = argv[i + 1]
                i += 1
            opts[dest] = val
            i += 1
            continue
        die(f"Unknown option: {tok}  (try --help)")
    return opts


def parse_duration(s):
    s = str(s).strip().lower()
    if not s:
        raise ValueError("empty duration")
    if s.endswith("ms"):
        return float(s[:-2]) / 1000.0
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if s[-1] in mult:
        return float(s[:-1]) * mult[s[-1]]
    return float(s)


def build_config(opts):
    cfg = Config()
    if "url" in opts:
        cfg.url = str(opts["url"]).strip()
    if "method" in opts:
        cfg.method = str(opts["method"]).strip().upper()
    if "proxies_file" in opts:
        cfg.proxies_file = str(opts["proxies_file"]).strip()
        cfg.use_proxy = True
    if opts.get("no_proxy"):
        cfg.use_proxy = False
        cfg.proxies_file = None
    if "duration" in opts:
        cfg.duration = parse_duration(opts["duration"])
    if "delay" in opts:
        cfg.delay = float(opts["delay"])
    if "concurrency" in opts:
        cfg.concurrency = int(opts["concurrency"])
    if "timeout" in opts:
        cfg.timeout = float(opts["timeout"])
    if "output" in opts:
        cfg.output = str(opts["output"]).strip()
    if "user_agent" in opts:
        cfg.user_agent = str(opts["user_agent"])
    if opts.get("insecure"):
        cfg.insecure = True
    if opts.get("no_tui"):
        cfg.tui = False
    if opts.get("yes"):
        cfg.assume_yes = True
    return cfg


def validate_config(cfg):
    if not cfg.url:
        die("No target --url given.")
    if "://" not in cfg.url:
        cfg.url = "http://" + cfg.url
    if cfg.duration <= 0:
        die("--duration must be > 0")
    if cfg.delay < 0:
        die("--delay must be >= 0")
    if cfg.concurrency < 1:
        die("--concurrency must be >= 1")
    if cfg.timeout <= 0:
        die("--timeout must be > 0")
    if cfg.use_proxy and not cfg.proxies_file:
        die("Proxy mode requested but no proxy file given.")
    if not cfg.use_proxy and not cfg.proxies_file:
        # neither proxy nor explicit direct: default to direct but warn.
        pass


# --------------------------------------------------------------------------- #
# proxies
# --------------------------------------------------------------------------- #
def load_proxies(path):
    if not os.path.isfile(path):
        die(f"Proxy file not found: {path}")
    proxies = []
    with open(path, "r", encoding="utf-8", errors="ignore") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            proxies.append(line)
    if not proxies:
        die(f"Proxy file is empty: {path}")
    return proxies


def normalize_proxy(p):
    p = p.strip()
    return p if "://" in p else "http://" + p


def display_proxy(p):
    """Hide credentials for display / reports."""
    p = p.split("://", 1)[-1]
    return p.split("@", 1)[1] if "@" in p else p


# --------------------------------------------------------------------------- #
# statistics (thread-safe)
# --------------------------------------------------------------------------- #
class Stats:
    CAP = 50000  # latency reservoir size

    def __init__(self):
        self.lock = threading.Lock()
        self.total = 0
        self.codes = {}
        self.errors = {}
        self.lat = []
        self.lat_seen = 0
        self.lat_max = 0.0
        self.bytes = 0
        self.proxy = {}  # display_proxy -> [sent, blocked]
        self.start = time.monotonic()

    def _reservoir(self, v):
        self.lat_seen += 1
        if len(self.lat) < self.CAP:
            self.lat.append(v)
        else:
            j = random.randint(0, self.lat_seen - 1)
            if j < self.CAP:
                self.lat[j] = v

    def record_status(self, code, latency, proxy, nbytes):
        with self.lock:
            self.total += 1
            self.codes[code] = self.codes.get(code, 0) + 1
            self._reservoir(latency)
            if latency > self.lat_max:
                self.lat_max = latency
            self.bytes += nbytes
            if proxy is not None:
                pb = self.proxy.setdefault(display_proxy(proxy), [0, 0])
                pb[0] += 1
                if code in BLOCK_CODES:
                    pb[1] += 1

    def record_error(self, kind, proxy):
        with self.lock:
            self.total += 1
            self.errors[kind] = self.errors.get(kind, 0) + 1
            if proxy is not None:
                pb = self.proxy.setdefault(display_proxy(proxy), [0, 0])
                pb[0] += 1

    def snapshot(self):
        with self.lock:
            return {
                "total": self.total,
                "codes": dict(self.codes),
                "errors": dict(self.errors),
                "lat": list(self.lat),
                "lat_max": self.lat_max,
                "bytes": self.bytes,
                "elapsed": time.monotonic() - self.start,
                "proxy": {k: list(v) for k, v in self.proxy.items()},
            }


def percentile(sorted_lat, p):
    if not sorted_lat:
        return 0.0
    k = int(math.ceil(p / 100.0 * len(sorted_lat))) - 1
    k = max(0, min(len(sorted_lat) - 1, k))
    return sorted_lat[k]


def derive(snap):
    codes = snap["codes"]
    total = snap["total"] or 1
    passed = sum(c for k, c in codes.items() if 200 <= k <= 299)
    redirect = sum(c for k, c in codes.items() if 300 <= k <= 399)
    c4 = sum(c for k, c in codes.items() if 400 <= k <= 499)
    server = sum(c for k, c in codes.items() if 500 <= k <= 599)
    blocked = sum(c for k, c in codes.items() if k in BLOCK_CODES)
    other4 = c4 - blocked
    errors = sum(snap["errors"].values())
    lat = sorted(snap["lat"])
    elapsed = max(1e-6, snap["elapsed"])
    return {
        "total": snap["total"],
        "passed": passed,
        "redirect": redirect,
        "blocked": blocked,
        "other4": other4,
        "server": server,
        "errors": errors,
        "rps": snap["total"] / elapsed,
        "block_rate": blocked / total * 100.0,
        "rejected_rate": (blocked + errors) / total * 100.0,
        "p50": percentile(lat, 50) * 1000,
        "p95": percentile(lat, 95) * 1000,
        "p99": percentile(lat, 99) * 1000,
        "max": snap["lat_max"] * 1000,
        "elapsed": snap["elapsed"],
    }


# --------------------------------------------------------------------------- #
# HTTP engine
# --------------------------------------------------------------------------- #
_tls = threading.local()


def get_opener(proxy, insecure):
    cache = getattr(_tls, "openers", None)
    if cache is None:
        cache = {}
        _tls.openers = cache
    key = proxy or "__direct__"
    op = cache.get(key)
    if op is None:
        handlers = []
        if insecure:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            handlers.append(urllib.request.HTTPSHandler(context=ctx))
        if proxy:
            purl = normalize_proxy(proxy)
            handlers.append(urllib.request.ProxyHandler({"http": purl, "https": purl}))
        else:
            handlers.append(urllib.request.ProxyHandler({}))  # force direct
        op = urllib.request.build_opener(*handlers)
        cache[key] = op
    return op


def classify_reason(reason):
    s = str(reason).lower()
    if isinstance(reason, ConnectionRefusedError) or "refused" in s:
        return "conn_refused"
    if "reset" in s:
        return "conn_reset"
    if "timed out" in s or isinstance(reason, socket.timeout):
        return "timeout"
    if "getaddrinfo" in s or "name or service" in s or "nodename" in s or "name resolution" in s:
        return "dns"
    if "proxy" in s or "tunnel" in s or "407" in s:
        return "proxy_error"
    if "certificate" in s or "ssl" in s:
        return "tls_error"
    return "conn_error"


def do_request(cfg, proxy):
    op = get_opener(proxy, cfg.insecure)
    req = urllib.request.Request(cfg.url, method=cfg.method,
                                 headers={"User-Agent": cfg.user_agent})
    try:
        resp = op.open(req, timeout=cfg.timeout)
        try:
            body = resp.read()
        finally:
            resp.close()
        return ("status", resp.getcode(), len(body) if body else 0)
    except urllib.error.HTTPError as e:
        try:
            body = e.read()
        except Exception:
            body = b""
        finally:
            try:
                e.close()
            except Exception:
                pass
        return ("status", e.code, len(body))
    except urllib.error.URLError as e:
        return ("error", classify_reason(e.reason), 0)
    except socket.timeout:
        return ("error", "timeout", 0)
    except ConnectionError as e:
        return ("error", classify_reason(e), 0)
    except Exception as e:  # noqa: BLE001 - defensive: never kill a worker
        return ("error", type(e).__name__.lower(), 0)


def worker(cfg, stats, stop_event, deadline, proxies):
    rnd = random.Random(os.urandom(16))
    while not stop_event.is_set() and time.monotonic() < deadline:
        proxy = rnd.choice(proxies) if proxies else None
        t0 = time.monotonic()
        kind, val, nbytes = do_request(cfg, proxy)
        lat = time.monotonic() - t0
        if kind == "status":
            stats.record_status(val, lat, proxy, nbytes)
        else:
            stats.record_error(val, proxy)
        if cfg.delay > 0:
            stop_event.wait(cfg.delay)


# --------------------------------------------------------------------------- #
# live displays
# --------------------------------------------------------------------------- #
def fmt_hms(sec):
    sec = int(max(0, sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def dashboard_lines(cfg, snap, now, deadline):
    d = derive(snap)
    remaining = max(0, deadline - now)
    mode = (f"proxied ({cfg._nproxies} proxies, random)" if cfg.use_proxy else "direct")
    codes = snap["codes"]
    top = sorted(codes.items(), key=lambda kv: -kv[1])[:6]
    code_str = "  ".join(f"{k}:{v}" for k, v in top) or "-"
    err_str = "  ".join(f"{k}:{v}" for k, v in sorted(snap["errors"].items(), key=lambda kv: -kv[1])[:5]) or "-"
    return [
        f"{APP} {VERSION}  —  load / WAF / rate-limit test   [authorized]",
        f"Target : {cfg.method} {cfg.url}",
        f"Mode   : {mode}   workers: {cfg.concurrency}   delay: {cfg.delay:g}s",
        f"Elapsed: {fmt_hms(d['elapsed'])} / {fmt_hms(cfg.duration)}   remaining: {fmt_hms(remaining)}",
        "-" * 60,
        f"Sent   : {d['total']:<10}  RPS: {d['rps']:.1f}",
        f"Passed : {d['passed']:<10}  2xx",
        f"Blocked: {d['blocked']:<10}  {sorted(k for k in codes if k in BLOCK_CODES)}",
        f"Other4x: {d['other4']:<10}  Server5xx: {d['server']}   Redirect: {d['redirect']}",
        f"Errors : {d['errors']:<10}  {err_str}",
        f"BLOCK RATE: {d['block_rate']:.1f}%   (blocked+errors: {d['rejected_rate']:.1f}%)",
        f"Latency: p50 {d['p50']:.0f}ms  p95 {d['p95']:.0f}ms  p99 {d['p99']:.0f}ms  max {d['max']:.0f}ms",
        "-" * 60,
        f"Codes  : {code_str}",
        "Press q / Ctrl-C to stop early and save the report.",
    ]


def run_dashboard(cfg, stats, stop_event, deadline):
    if not sys.stdout.isatty():
        return False  # piped/redirected output -> use the plain line printer
    try:
        import curses
    except Exception:
        return False

    def _loop(screen):
        curses.curs_set(0)
        screen.nodelay(True)
        screen.timeout(400)
        while not stop_event.is_set() and time.monotonic() < deadline:
            _paint(screen, dashboard_lines(cfg, stats.snapshot(), time.monotonic(), deadline))
            try:
                ch = screen.getch()
            except Exception:
                ch = -1
            if ch in (ord("q"), ord("Q")):
                stop_event.set()
                break
        _paint(screen, dashboard_lines(cfg, stats.snapshot(), time.monotonic(), deadline))
        screen.refresh()

    def _paint(screen, lines):
        try:
            screen.erase()
            h, w = screen.getmaxyx()
            for i, line in enumerate(lines):
                if i >= h:
                    break
                screen.addnstr(i, 0, line, max(0, w - 1))
            screen.refresh()
        except Exception:
            pass

    try:
        curses.wrapper(_loop)
        return True
    except KeyboardInterrupt:
        stop_event.set()
        return True
    except Exception:
        return False


def run_plain(cfg, stats, stop_event, deadline):
    last = 0.0
    while not stop_event.is_set() and time.monotonic() < deadline:
        now = time.monotonic()
        if now - last >= 1.0:
            d = derive(stats.snapshot())
            warn(f"[{fmt_hms(d['elapsed'])}/{fmt_hms(cfg.duration)}] "
                 f"sent={d['total']} rps={d['rps']:.0f} pass={d['passed']} "
                 f"blocked={d['blocked']} err={d['errors']} block_rate={d['block_rate']:.1f}%")
            last = now
        stop_event.wait(0.2)


# --------------------------------------------------------------------------- #
# run + report
# --------------------------------------------------------------------------- #
def run_test(cfg, proxies, holder=None):
    stats = Stats()
    stop_event = threading.Event()
    cfg._nproxies = len(proxies) if proxies else 0
    start_wall = datetime.now()
    stats.start = time.monotonic()
    deadline = stats.start + cfg.duration
    if holder is not None:
        holder["stats"] = stats
        holder["start"] = start_wall

    workers = []
    for _ in range(cfg.concurrency):
        t = threading.Thread(target=worker, args=(cfg, stats, stop_event, deadline, proxies), daemon=True)
        t.start()
        workers.append(t)

    def on_sigint(_sig, _frm):
        stop_event.set()

    # Keep our handler active through the whole run AND the join, so Ctrl-C only
    # sets the stop flag (never raises) until the report has been written.
    old = signal.signal(signal.SIGINT, on_sigint)
    try:
        ran = run_dashboard(cfg, stats, stop_event, deadline) if cfg.tui else False
        if not ran:
            run_plain(cfg, stats, stop_event, deadline)
    finally:
        stop_event.set()

    for t in workers:
        t.join(timeout=cfg.timeout + 1)
    try:
        signal.signal(signal.SIGINT, old)
    except Exception:
        pass
    return stats, start_wall, datetime.now()


def default_output():
    return f"{APP}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.txt"


def write_report(cfg, stats, start_wall, end_wall, proxies):
    snap = stats.snapshot()
    d = derive(snap)
    path = cfg.output or default_output()
    mode = f"proxied ({len(proxies)} proxies, random order)" if cfg.use_proxy else "direct (no proxy)"

    lines = []
    lines.append("=" * 62)
    lines.append(f" {APP} {VERSION} — LOAD / WAF / RATE-LIMIT TEST REPORT")
    for l in AUTH_NOTICE.splitlines():
        lines.append(" " + l)
    lines.append("=" * 62)
    lines.append(f" Started    : {start_wall.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f" Ended      : {end_wall.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f" Target     : {cfg.method} {cfg.url}")
    lines.append(f" Mode       : {mode}")
    lines.append(f" Duration   : requested {cfg.duration:g}s, actual {d['elapsed']:.1f}s")
    lines.append(f" Concurrency: {cfg.concurrency} workers   Delay: {cfg.delay:g}s   Timeout: {cfg.timeout:g}s")
    lines.append(f" User-Agent : {cfg.user_agent}")
    lines.append("-" * 62)
    lines.append(f" Total requests : {d['total']}")
    lines.append(f" Achieved RPS   : {d['rps']:.1f}")
    lines.append(f" Passed  (2xx)  : {d['passed']}")
    lines.append(f" Redirect(3xx)  : {d['redirect']}")
    lines.append(f" Blocked        : {d['blocked']}   codes {sorted(k for k in snap['codes'] if k in BLOCK_CODES)}")
    lines.append(f" Other 4xx      : {d['other4']}")
    lines.append(f" Server  (5xx)  : {d['server']}   (note: nginx limit_req/limit_conn may return 503)")
    lines.append(f" Errors         : {d['errors']}")
    lines.append(f" --> FILTER BLOCK RATE : {d['block_rate']:.1f}%")
    lines.append(f" --> blocked + errors  : {d['rejected_rate']:.1f}% of all requests")
    lines.append(f" Latency (ms)   : p50 {d['p50']:.0f}  p95 {d['p95']:.0f}  p99 {d['p99']:.0f}  max {d['max']:.0f}  (received responses; excludes timeouts/errors)")
    lines.append("-" * 62)
    lines.append(" Status code breakdown:")
    for code, cnt in sorted(snap["codes"].items()):
        lines.append(f"   {code}: {cnt}")
    if snap["errors"]:
        lines.append(" Error breakdown:")
        for kind, cnt in sorted(snap["errors"].items(), key=lambda kv: -kv[1]):
            lines.append(f"   {kind}: {cnt}")
    if snap["proxy"]:
        lines.append("-" * 62)
        lines.append(" Per-proxy (top 15 by requests)  —  proxy  sent  blocked  block%")
        rows = sorted(snap["proxy"].items(), key=lambda kv: -kv[1][0])[:15]
        for pxy, (sent, blk) in rows:
            pct = (blk / sent * 100.0) if sent else 0.0
            lines.append(f"   {pxy:<24} {sent:>7} {blk:>8}  {pct:5.1f}%")
    lines.append("=" * 62)
    report = "\n".join(lines) + "\n"

    with open(path, "w", encoding="utf-8") as fh:
        fh.write(report)
    return path, d


# --------------------------------------------------------------------------- #
# interactive menu
# --------------------------------------------------------------------------- #
def ask(prompt, default=""):
    suffix = f" [{default}]" if default != "" else ""
    try:
        s = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        return default
    return s or default


def interactive_menu():
    print()
    print("=" * 60)
    print(f" {APP} {VERSION} — interactive setup")
    print("=" * 60)
    for l in AUTH_NOTICE.splitlines():
        print(" " + l)
    print("=" * 60)

    cfg = Config()
    cfg.url = ask("Target URL (you must own / be authorized to test)")
    while not cfg.url:
        cfg.url = ask("Target URL")
    cfg.method = ask("HTTP method", "GET").upper()

    use = ask("Use a proxy list? (y/n)", "y").lower().startswith("y")
    if use:
        cfg.proxies_file = ask("Path to proxy txt file", "proxies.txt")
        cfg.use_proxy = True
    else:
        cfg.use_proxy = False

    cfg.duration = parse_duration(ask("Duration (e.g. 60s, 5m)", "30s"))
    cfg.delay = float(ask("Delay between requests per worker (seconds)", "0"))
    cfg.concurrency = int(ask("Parallel workers", "20"))
    cfg.timeout = float(ask("Per-request timeout (seconds)", "10"))
    if ask("Target uses a self-signed cert? (y/n)", "n").lower().startswith("y"):
        cfg.insecure = True
    cfg.output = ask("Results file", default_output())

    print("-" * 60)
    print(f" Target      : {cfg.method} {cfg.url}")
    print(f" Mode        : {'proxied' if cfg.use_proxy else 'direct'}"
          + (f"  ({cfg.proxies_file})" if cfg.use_proxy else ""))
    print(f" Duration    : {cfg.duration:g}s   Delay: {cfg.delay:g}s   Workers: {cfg.concurrency}")
    print(f" Results file: {cfg.output}")
    print("-" * 60)
    confirm = ask("Type YES to confirm you are authorized to test this target")
    if confirm != "YES":
        die("Not confirmed — aborting.")
    cfg.assume_yes = True
    return cfg


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def confirm_authorization(cfg):
    if cfg.assume_yes:
        return
    if sys.stdin and sys.stdin.isatty():
        print()
        print(AUTH_NOTICE)
        ans = ask(f"Type YES to confirm you are authorized to test {cfg.url}")
        if ans != "YES":
            die("Not confirmed — aborting.")
    else:
        die("Authorization not confirmed. Re-run with --yes to confirm you are "
            "authorized to test the target (or run interactively).")


def print_help():
    print(__doc__.strip())


def main(argv):
    if any(a in ("-h", "--h", "--help", "help") for a in argv):
        print_help()
        return 0

    if not argv:
        cfg = interactive_menu()
    else:
        cfg = build_config(parse_args(argv))
        validate_config(cfg)
        confirm_authorization(cfg)

    validate_config(cfg)

    proxies = []
    if cfg.use_proxy:
        proxies = load_proxies(cfg.proxies_file)
        random.shuffle(proxies)
        info(f"Loaded {len(proxies)} proxies from {cfg.proxies_file}")
    else:
        warn("Direct mode — no proxies. Traffic originates from this host's IP.")

    info(f"Starting: {cfg.method} {cfg.url}  for {cfg.duration:g}s  "
         f"x{cfg.concurrency} workers  delay={cfg.delay:g}s")

    holder = {}
    interrupted = False
    try:
        stats, start_wall, end_wall = run_test(cfg, proxies, holder)
    except KeyboardInterrupt:
        interrupted = True
        stats = holder.get("stats")
        start_wall = holder.get("start", datetime.now())
        end_wall = datetime.now()

    if stats is None:
        warn("Stopped before any data was collected — nothing to report.")
        return 130

    # Writing the report must not be interruptible, or we lose the results.
    old = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        path, d = write_report(cfg, stats, start_wall, end_wall, proxies)
    finally:
        try:
            signal.signal(signal.SIGINT, old)
        except Exception:
            pass

    print()
    if interrupted:
        warn("Interrupted — partial results saved.")
    ok(f"Done. {d['total']} requests, {d['rps']:.1f} rps, "
       f"block rate {d['block_rate']:.1f}% (blocked+errors {d['rejected_rate']:.1f}%).")
    ok(f"Report saved to: {path}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        print()
        warn("Interrupted.")
        sys.exit(130)
