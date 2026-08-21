# netwatch — continuous internet quality monitor + diagnostic report

Watch a connection for as long as you like, store **every** measurement in a local SQLite file, then
turn the capture into a Markdown report with SVG charts and a verdict that names the layer at fault:
your Wi-Fi or cable, the router, a second router / load balancer, the ISP's access network, upstream
peering, DNS, or just the far end.

Dependency-free (Python 3 standard library only) and binary-free: `ping` and `traceroute` are used
when they exist, and when they don't — a minimal container, for instance — netwatch does ICMP echo,
traceroute and path-MTU discovery **itself** over a socket.

It exists for the problem that is hard to catch by hand: *"the internet drops for a couple of
seconds and I have no idea why."* One probe per second, a per-second availability model, and a
public-IP watcher fast enough to see a dual-WAN balancer flip between providers.

## Run

```bash
chmod +x linux/netwatch/netwatch.sh

# interactive TUI menu (choose duration, then a live dashboard):
./linux/netwatch/netwatch.sh

# one-shot 90 s diagnostic with an immediate verdict:
./linux/netwatch/netwatch.sh --quick

# unattended capture — args accept --k v, --k=v or --k:v:
./linux/netwatch/netwatch.sh --duration 8h --plan 100 --label "evening test" --yes --no-tui

# rebuild the report from a capture you already have:
./linux/netwatch/netwatch.sh --analyze ./netwatch-20260821-120000
```

The launcher installs Python 3 if it is missing. While a capture runs you get a live dashboard —
per-target latency sparklines, loss, uptime, the active uplink and an event log. Press `q` (or
`Ctrl-C`) to finish early: the report is still written. `s` runs a speed test immediately, `t` a
traceroute.

## What it measures

| Probe | Default rate | What it tells you |
|---|---|---|
| **ICMP** to gateway → second router → ISP edge → 3 public anchors | 1 / s | Where loss *starts*. Sub-second dropouts, jitter, and the reply **TTL** of every packet. |
| **Public IP** over DNS (`myip.opendns.com`, `whoami.cloudflare`, Google's TXT oracle) | every 2 s | Dual-WAN / load-balancer failovers, including short ones. Each address is labelled with its real operator via an ASN lookup. |
| **DNS** against several resolvers, over UDP, TCP and DoH | every 30 s | Slow or failing resolution, and NXDOMAIN hijacking. |
| **HTTP(S)**, phase-timed | every 30 s | DNS / TCP / TLS / time-to-first-byte split, captive portals, TLS version, certificate expiry. |
| **Speed**, multi-stream down + up | every 15 min | Throughput, its stability, and — because the ICMP stream keeps running — **latency under load**, i.e. a real bufferbloat grade. |
| **Path** — traceroute, MTU, TCP ports, NTP | 15–30 min | Route changes, PPPoE/VPN MTU, ISP port filtering, UDP reachability. |
| **Local link** — interface counters, Wi-Fi signal | every 10 s | Cable/NIC errors, dropped frames, carrier loss, and whether loss tracks the Wi-Fi signal. |

Every sample goes straight into SQLite through a single writer thread — nothing is accumulated in
RAM, so a multi-day capture costs a few tens of MB on disk and almost nothing in memory.

## Multi-WAN / load balancer

If two providers sit behind a balancer, netwatch is built to catch the switching:

- the **public IP is re-checked every couple of seconds** with a single UDP DNS packet, and a change
  is confirmed against a second, independent oracle before it counts as a failover;
- the **reply TTL of every ICMP probe** is recorded — a different provider means a different hop
  count, so TTL catches switches *shorter than the IP polling interval*;
- each address gets an **ASN lookup**, so the report says `AS64500 PROVIDER-ALPHA` rather than a bare
  IP;
- the report then measures **loss, latency, jitter and MOS separately for each uplink**, lists every
  switch with how much connectivity it cost, and flags flapping (many short stints) and a weak
  uplink that the balancer keeps choosing.

```text
| Uplink                            | Share | Stints | Loss  | p50  | p95  | MOS  | TTL |
| 203.0.113.10 · AS64500 ALPHA      | 90.3% |      3 | 0.31% | 24.1 | 31.0 | 4.37 |  56 |
| 198.51.100.20 · AS64501 BETA      |  9.7% |      2 | 2.30% | 59.8 | 75.8 | 4.23 |  52 |
```

Lower `--wan-interval` to 1 s to catch even shorter flips (it costs one small UDP packet per second).

## What you get

A directory per capture:

```text
netwatch-20260821-120000/
├── report.md          # the whole analysis, in Markdown
├── summary.json       # the same verdict as machine-readable JSON
├── netwatch.db        # every raw sample, queryable with plain SQL
└── charts/            # up to 14 self-contained SVGs, light + dark aware
```

`report.md` contains, in order: a **stability score** (0–100 with a letter grade) and the findings
sorted by severity; **availability** with a status-page ribbon and a table of every interruption
(including whether your own router was still answering); **latency, jitter and loss per hop**;
**uplink / balancer behaviour**; **DNS**; **HTTP/TLS** with a phase breakdown; **throughput and
bufferbloat**; **path, MTU and reachability**; **local link**; **time-of-day patterns** with an
hourly heatmap; the **event log**; and the raw-data section with example SQL.

The verdict compares layers instead of quoting one number:

- loss at the **gateway** → your cable or Wi-Fi;
- clean gateway, loss at the **second private hop** → your own firewall / balancer;
- clean up to there, loss at the **ISP edge** → their access network;
- clean ISP edge, loss on **all three public anchors** → their transit or your line;
- loss on **one anchor only** → that operator, not you;
- latency fine when idle but exploding under load → **bufferbloat**, not the ISP;
- outages spaced evenly → something **scheduled** (DHCP lease, PPPoE re-dial, a reboot);
- loss that correlates with the **Wi-Fi signal** → move the access point, not the contract.

## Options

| Flag | Description |
|---|---|
| `--duration`, `--time` | How long to monitor: `90s`, `30m`, `2h`, `1d`. `0` = until you press `q`. |
| `--interval` | ICMP probe interval, seconds (default `1.0`). |
| `--wan-interval` | Public-IP / failover probe interval, seconds (default `2.0`). |
| `--dns-interval` · `--http-interval` · `--link-interval` | Probe intervals for DNS, HTTP and the local interface. |
| `--speed-interval` | Seconds between speed tests (default `900`); `0` disables them. |
| `--trace-interval` | Seconds between traceroutes (default `1800`); `0` disables them. |
| `--speed-max-mb` · `--speed-seconds` · `--streams` | Data cap per test, duration per direction, parallel streams. |
| `--plan` | Your subscribed speed in Mbps — the report then judges what you actually get. |
| `--targets` | Extra ping targets, comma separated: `host` or `name=host`. |
| `--urls` · `--resolvers` · `--domains` | Extra HTTP endpoints, DNS servers and test domains. |
| `--outage-ticks` | Consecutive lost seconds before it counts as an outage (default `2`). |
| `--no-speed` · `--no-upload` · `--no-trace` · `--no-ipv6` · `--no-mtu` | Turn individual probes off (metered links, restricted networks). |
| `--out`, `--output` | Output directory (default `./netwatch-<timestamp>`). |
| `--db` | Append to an explicit SQLite file instead of a fresh one. |
| `--label` | A short name for the capture, shown in the report title. |
| `--analyze <path>` | Analyse an existing capture directory or `.db` and exit. |
| `--runs <path>` | List the runs stored in a database. |
| `--quick`, `-q` | 90-second diagnostic with one speed test. |
| `--no-tui`, `--plain` | No dashboard, plain progress lines (cron / CI friendly). |
| `--yes`, `-y` | Start immediately, skip the confirmation. |
| `--help`, `-h` | Full help. |

## Querying the raw data

```sql
-- the ten worst minutes of the capture
SELECT datetime(CAST(ts/60 AS INT)*60,'unixepoch','localtime') AS minute,
       COUNT(*) AS probes, SUM(ok) AS answered,
       ROUND(100.0*(COUNT(*)-SUM(ok))/COUNT(*),2) AS loss_pct
FROM ping_samples WHERE target NOT IN ('gateway','lan-hop','isp-edge')
GROUP BY minute ORDER BY loss_pct DESC LIMIT 10;

-- how long each uplink was in use
SELECT ip, COUNT(*) AS lookups,
       datetime(MIN(ts),'unixepoch','localtime') AS first_seen,
       datetime(MAX(ts),'unixepoch','localtime') AS last_seen
FROM wan_samples WHERE ok=1 GROUP BY ip ORDER BY lookups DESC;
```

## Notes

- **ICMP without root.** netwatch prefers unprivileged ICMP (`SOCK_DGRAM`), which most distros allow.
  Where the kernel forbids it, it falls back to the `ping` binary and then to TCP connect probes —
  the report always states which mode was used. To enable the good path:
  `sudo sysctl -w net.ipv4.ping_group_range='0 2147483647'`.
- **Data usage.** Only the speed tests move real traffic (`--speed-max-mb` caps each one; `--no-speed`
  turns them off). Everything else is a few packets per second.
- **Long captures.** A day at the default rate is roughly 500 k rows — a few tens of MB. Run it under
  `tmux`/`screen` or with `--no-tui` from a systemd unit or `nohup`.
- **Suspended laptops** do not count against availability: a second only counts as *down* if probes
  were actually sent in it and none came back.
- Tested on Ubuntu (WSL2) and in a `python:3.12-slim` container with neither `ping` nor `traceroute`
  installed.
