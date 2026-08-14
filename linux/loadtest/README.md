# loadtest — authorized load / WAF / rate-limit tester

> ⚠️ **Authorized testing only.** Run this **only** against servers and sites you own or have
> **written permission** to test. Load-testing third-party systems without authorization is abuse and
> is likely illegal. The tool sends an identifiable `User-Agent` and requires a one-time confirmation
> so your test traffic is easy to spot (and filter) in your own logs and dashboards.

A small, dependency-free (Python 3 standard library only) tool to generate configurable HTTP load
against your own site and **measure how well its filtering blocks the traffic** — pass rate vs.
`403`/`429`/`503`, per source IP — so you can validate nginx/Apache rate limiting and WAF rules and
correlate the result with your own monitoring (Grafana, etc.).

It can route each request through a **rotating pool of HTTP proxies** (random order), which is what
lets you exercise **IP-based** rate limits and blocklists from many source addresses. Point it at a
target you control, watch your defense trip, read the block rate.

## Run

```bash
chmod +x linux/loadtest/loadtest.sh

# Interactive TUI menu (asks for everything, then a live dashboard):
./linux/loadtest/loadtest.sh

# Non-interactive — args accept --k v, --k=v or --k:v (as in your example):
./linux/loadtest/loadtest.sh --url https://my.site --proxy:/path/proxies.txt \
    --duration 60s --delay 0.1 --concurrency 50 --yes
```

The launcher installs Python 3 automatically if it is missing (apt/dnf/yum/pacman/apk), then runs the
app. While a test runs you get a live **TUI dashboard** (requests, RPS, pass/blocked split, block
rate, latency percentiles). Press `q` or `Ctrl-C` to stop early — the report is still written.

## Proxy file format

One proxy per line; blank lines and `#` comments ignored. Proxies are used in **random order**:

```text
login:passwd@ip:port
ip:port                       # no auth
http://login:passwd@ip:port   # scheme optional, http assumed
```

## Options

| Flag | Description |
|---|---|
| `--url`, `--target` | Target URL (required). |
| `--proxy`, `--proxies` | Path to the proxy list `.txt` file (enables proxy mode). |
| `--no-proxy`, `--direct` | Send directly from this host — baseline / capacity test, no proxies. |
| `--duration`, `--time` | How long to run: `90`, `90s`, `5m`, `1h` (default `30s`). |
| `--delay`, `--sleep` | Pause between requests **per worker**, seconds (default `0`). |
| `--concurrency`, `-c` | Number of parallel workers (default `20`). |
| `--method`, `-m` | HTTP method (default `GET`). |
| `--timeout` | Per-request timeout, seconds (default `10`). |
| `--output`, `--result` | Results file (default `loadtest-<timestamp>.txt`). |
| `--user-agent`, `--ua` | Override the User-Agent (default `loadtest/1.0 (authorized-testing)`). |
| `--insecure`, `-k` | Do not verify TLS certs (self-signed targets, e.g. a Caddy internal cert). |
| `--no-tui`, `--plain` | Disable the live dashboard; print plain progress lines (for logs/CI). |
| `--yes`, `--authorized` | Confirm you are authorized to test the target (required non-interactively). |
| `--help`, `-h` | Full help. |

## What the report contains

A plain-text file with: the target and run config, total requests and achieved **RPS**, the split into
**passed (2xx)** / **blocked (401/403/405/406/409/415/429/451)** / other 4xx / **5xx** (note: nginx
`limit_req`/`limit_conn` often answer `503`) / connection errors, the **filter block rate**, latency
**p50/p95/p99/max**, a full status-code breakdown, and a **per-proxy** table (sent / blocked / block%).
Proxy credentials are **masked** in the report (only `ip:port` is written).

## How to read it for filter testing

- **Block rate climbs as you raise `--concurrency` / lower `--delay`** → your rate limiting is engaging.
- **Per-proxy block% roughly equal across proxies** → limits are per-IP and firing for each source.
- **Mostly `200` even at high load** → your filter is *not* catching this pattern — tune the rule.
- Watch the same window in Grafana; the identifiable User-Agent makes the test traffic easy to isolate.

> If you need traffic from genuinely different geographic sources (not just proxies), the legitimate
> approach is your **own** load-generator VMs in multiple regions — not third-party proxy services.
