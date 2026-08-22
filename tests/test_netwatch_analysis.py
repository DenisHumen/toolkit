#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""netwatch's analysis, fed a capture with a known answer.

A dual-provider load balancer is the one thing that cannot be reproduced on
demand against the real internet, so the capture is synthesised: two uplinks,
four failovers, three seconds of loss around each, and one provider measurably
worse than the other. The analysis has to find exactly that.
"""

import json
import os
import random
import shutil
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import REPO, Suite                                        # noqa: E402

sys.path.insert(0, os.path.join(REPO, "linux", "netwatch"))
import netwatch as nw                                              # noqa: E402

WAN_A, WAN_B = "203.0.113.10", "198.51.100.20"
DURATION = 1200
STINTS = [(0, 400, WAN_A), (400, 500, WAN_B), (500, 900, WAN_A),
          (900, 925, WAN_B), (925, DURATION, WAN_A)]
SWITCH_AT = [s[0] for s in STINTS[1:]]
PROFILE = {WAN_A: {"rtt": 12.0, "loss": 0.002, "ttl": 56},
           WAN_B: {"rtt": 46.0, "loss": 0.035, "ttl": 52}}
SWITCH_COST = 3          # seconds of total loss around every failover


def active(offset):
    for a, b, ip in STINTS:
        if a <= offset < b:
            return ip
    return WAN_A


def synthesise(out_dir):
    """Write a capture whose correct interpretation we already know."""
    random.seed(7)
    db = os.path.join(out_dir, "netwatch.db")
    t0 = time.time() - DURATION
    storage = nw.Storage(db)
    storage.open()
    cfg = nw.Config()
    cfg.duration = DURATION
    cfg.wan_interval = 2.0
    cfg.label = "synthetic dual-WAN"
    net = {"gateway": "192.168.1.1", "iface": "eth0", "local_ip": "192.168.1.50",
           "lan_hop": "192.168.88.1", "isp_hop": "212.8.50.65", "wifi": "",
           "targets": [
               {"name": "gateway", "host": "192.168.1.1", "role": "gateway"},
               {"name": "lan-hop", "host": "192.168.88.1", "role": "lan"},
               {"name": "isp-edge", "host": "212.8.50.65", "role": "isp"},
               {"name": "google-dns", "host": "8.8.8.8", "role": "anchor"},
               {"name": "cloudflare", "host": "1.1.1.1", "role": "anchor"},
               {"name": "quad9", "host": "9.9.9.9", "role": "anchor"}]}
    run_id = storage.start_run(cfg, nw.host_info(), net)
    conn = sqlite3.connect(db)

    pings, wans = [], []
    for sec in range(DURATION):
        ts = t0 + sec
        ip = active(sec)
        prof = PROFILE[ip]
        in_switch = any(0 <= sec - x < SWITCH_COST for x in SWITCH_AT)
        for name, base, ttl in (("gateway", 0.6, 64), ("lan-hop", 1.1, 63),
                                ("isp-edge", 3.4, 253)):
            pings.append((run_id, ts, name, 1, round(base * random.uniform(0.7, 1.9), 3),
                          ttl, sec, None))
        for name, extra in (("google-dns", 11.0), ("cloudflare", 0.0), ("quad9", 12.0)):
            lost = in_switch or random.random() < prof["loss"]
            rtt = None if lost else round((prof["rtt"] + extra)
                                          * random.uniform(0.92, 1.35), 3)
            pings.append((run_id, ts, name, 0 if lost else 1, rtt,
                          None if lost else prof["ttl"], sec,
                          "timeout" if lost else None))
        if sec % 2 == 0:
            ok = not in_switch
            wans.append((run_id, ts, "opendns", ip if ok else None, 1 if ok else 0,
                         round(random.uniform(18, 40), 2), None if ok else "timeout"))

    conn.executemany(nw.INSERTS["ping_samples"], pings)
    conn.executemany(nw.INSERTS["wan_samples"], wans)
    for ip, asn, name in ((WAN_A, "64500", "PROVIDER-ALPHA, UA"),
                          (WAN_B, "64501", "PROVIDER-BETA, UA")):
        conn.execute("INSERT INTO wan_ips VALUES (?,?,?,?,?,?,?,?,?)",
                     (run_id, ip, t0, t0 + DURATION, 100, asn, name, "UA",
                      "WAN-A" if ip == WAN_A else "WAN-B"))
    for direction, mbps in (("download", 92.0), ("upload", 41.0)):
        conn.execute(nw.INSERTS["speed_tests"],
                     (run_id, t0 + 600, t0 + 612, direction, 140_000_000, 12.0,
                      mbps, 4, "speed.cloudflare.com", None))
        conn.execute(nw.INSERTS["phases"],
                     (run_id, t0 + 600, t0 + 612, f"speed-{direction}"))
    for i in range(40):
        conn.execute(nw.INSERTS["dns_samples"],
                     (run_id, t0 + i * 30, "cloudflare", "1.1.1.1", "udp",
                      "github.com", 1, round(random.uniform(9, 30), 2),
                      "NOERROR", "140.82.121.4", None))
        conn.execute(nw.INSERTS["http_samples"],
                     (run_id, t0 + i * 30, "https://www.google.com/generate_204",
                      1, 204, 4.0, 11.0, 22.0, 38.0, 61.0, 320, "TLSv1.3", 60, None))
    conn.execute("UPDATE runs SET ended_at=?, status='finished' WHERE id=?",
                 (t0 + DURATION, run_id))
    conn.commit()
    conn.close()
    return db, run_id


def main():
    s = Suite("netwatch analysis (synthetic dual-WAN capture)")
    out = tempfile.mkdtemp(prefix="netwatch-test-")
    try:
        db, run_id = synthesise(out)
        A = nw.analyze(db, run_id)

        s.check("reads the capture back", A["duration"] > DURATION - 5)
        s.check("finds every failover", len(A["wan_switches"]) == len(SWITCH_AT),
                f"{len(A['wan_switches'])} != {len(SWITCH_AT)}")
        s.check("splits the capture into uplink stints",
                len(A["wan_stints"]) == len(STINTS),
                f"{len(A['wan_stints'])} != {len(STINTS)}")
        s.check("identifies both uplinks", set(A["wan_quality"]) == {WAN_A, WAN_B})

        a, b = A["wan_quality"].get(WAN_A, {}), A["wan_quality"].get(WAN_B, {})
        s.check("attributes most of the time to the primary uplink",
                85 < (a.get("share_pct") or 0) < 95, f"{a.get('share_pct')}")
        s.check("measures each uplink's latency separately",
                (a.get("p50") or 0) < (b.get("p50") or 0) / 1.5,
                f"A p50={a.get('p50')}  B p50={b.get('p50')}")
        s.check("measures each uplink's loss separately",
                (b.get("loss_pct") or 0) > (a.get("loss_pct") or 0) * 3,
                f"A loss={a.get('loss_pct')}  B loss={b.get('loss_pct')}")
        s.check("labels the uplinks with their operator",
                a.get("as_name") == "PROVIDER-ALPHA, UA")
        s.check("records the reply TTL per uplink",
                a.get("ttl") == 56 and b.get("ttl") == 52)
        s.check("estimates what a failover costs",
                2.0 <= (A["wan_switch_cost"] or 0) <= 4.5,
                f"{A['wan_switch_cost']}")

        s.check("counts the outages the failovers caused",
                len(A["outages"]) == len(SWITCH_AT),
                f"{len(A['outages'])} outages")
        s.check("computes availability from the anchors only",
                98.0 < A["uptime_pct"] < 99.6, f"{A['uptime_pct']}")
        s.check("blames the path beyond the router",
                all(o["gateway"] == "up" for o in A["outages"]))
        s.check("the local hops stay clean",
                A["targets"]["gateway"]["loss_pct"] == 0.0)

        titles = [f["title"] for f in A["findings"]]
        severities = {f["title"]: f["severity"] for f in A["findings"]}
        s.check("reports the switching in the verdict",
                any("switched uplink" in t for t in titles), str(titles))
        s.check("and treats frequent switching as serious",
                any(severities[t] == "critical" for t in titles
                    if "switched uplink" in t))
        s.check("names the weaker uplink",
                any("not equivalent" in t for t in titles), str(titles))
        s.check("uses TTL as corroboration",
                any("TTL" in t for t in titles), str(titles))
        s.check("scores the connection below perfect", 60 < A["score"] < 90,
                f"score={A['score']}")

        report, charts = nw.build_report(A, out)
        s.check("writes the report", os.path.isfile(report))
        s.check("writes the charts", len(charts) >= 8 and all(
            os.path.isfile(os.path.join(out, c)) for c in charts))
        body = open(report, encoding="utf-8").read()
        s.check("the report has an uplink section", "Uplink / balancer behaviour" in body)
        s.check("the report lists every switch", body.count("→") >= len(SWITCH_AT))
        s.check("the report names both operators",
                "PROVIDER-ALPHA" in body and "PROVIDER-BETA" in body)
        s.check("the charts are valid SVG",
                all(open(os.path.join(out, c), encoding="utf-8").read().startswith("<svg")
                    for c in charts))
        summary = json.load(open(os.path.join(out, "summary.json"), encoding="utf-8"))
        s.check("summary.json carries the verdict",
                summary["wan_switches"] == len(SWITCH_AT) and summary["grade"])

        # Re-analysing the same database must produce the same answer.
        again = nw.analyze(db, run_id)
        s.check("analysis is deterministic",
                (again["score"], len(again["wan_switches"]), again["uptime_pct"])
                == (A["score"], len(A["wan_switches"]), A["uptime_pct"]))
    finally:
        shutil.rmtree(out, ignore_errors=True)
    return s.finish()


if __name__ == "__main__":
    sys.exit(main())
