#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""netwatch's rhythm check: loss that comes back on a schedule.

A 12-hour capture once came back "A+, everything clean" although 47 of its 54
lost seconds sat in the same 30 seconds of every ten minutes — the fingerprint
of a scheduled job on the router, and the reason a trading platform kept
dropping its feed. These checks feed the detector schedules with a known
answer (and non-schedules that must stay silent), then run the whole analysis
over synthetic captures and read the report.
"""

import json
import os
import random
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import REPO, Suite                                        # noqa: E402

sys.path.insert(0, os.path.join(REPO, "linux", "netwatch"))
import netwatch as nw                                              # noqa: E402

T0 = 1_790_000_000 // 3600 * 3600        # a capture that starts on the hour
HOURS = 6


def scheduled(period, lo, hi, share, hours=HOURS, seed=1, drift=0.0, burst=(1, 4)):
    """Lost seconds from a job that fires every `period` s, lo..hi s into the cycle."""
    rnd = random.Random(seed)
    lost, k = set(), 0
    while True:
        start = T0 + k * (period + drift)
        if start > T0 + hours * 3600:
            return lost
        if rnd.random() < share:
            at = int(start + rnd.uniform(lo, hi))
            lost.update(range(at, at + rnd.randint(*burst)))
        k += 1


def noise(count, hours=HOURS, seed=2):
    rnd = random.Random(seed)
    return {T0 + rnd.randrange(hours * 3600) for _ in range(count)}


def detector(s):
    end = T0 + HOURS * 3600
    R = nw.find_rhythm(scheduled(600, 25, 45, 0.5) | noise(8), T0, end)
    s.check("finds a 10-minute job", R["found"] and R["period_s"] == 600, str(R.get("period_s")))
    s.check("says it follows the clock", R.get("locked") is True)
    # The job fires 25-44 s into the cycle. A stray burst that happens to land a
    # few seconds beyond is indistinguishable from the job's own jitter, so ask
    # for a slice that covers the job and is not much wider — not an exact edge.
    s.check("puts the slice over the job, and not much wider",
            R["found"] and R["window_start"] <= 25
            and R["window_start"] + R["window_width"] >= 44 and R["window_width"] <= 40,
            f"{R.get('window_start')} + {R.get('window_width')}")
    s.check("keeps the stray bursts out of the slice",
            R["found"] and R["bursts"] - R["in_window"] <= 8,
            f"{R.get('in_window')} of {R.get('bursts')}")
    s.check("counts the cycles it showed up in",
            R["found"] and 0.3 < R["affected"] / R["cycles"] < 0.7,
            f"{R.get('affected')}/{R.get('cycles')}")
    s.check("describes the slice on the clock",
            R["found"] and nw.cycle_clock(R).endswith("after hh:00, hh:10, hh:20 …"),
            R["found"] and nw.cycle_clock(R))

    # Found by a sweep over seeds: here the stray bursts all land in the half of
    # the cycle opposite the job. They drag the 10-minute Rayleigh test below
    # significance while folding neatly onto the job at 5 minutes.
    R = nw.find_rhythm(scheduled(600, 25, 45, 0.5, seed=1) | noise(8, seed=101), T0, end)
    s.check("strays opposite the job do not turn 10 minutes into 5",
            R["found"] and R["period_s"] == 600, str(R.get("period_s")))
    # ...and here a job that jitters by ±10 s looked like a timer drifting 3.6 s a cycle
    R = nw.find_rhythm(scheduled(600, 25, 45, 0.5, seed=27) | noise(8, seed=127), T0, end)
    s.check("a job that merely jitters stays locked to the clock",
            R["found"] and R["locked"] and R["period_s"] == 600,
            f"{R.get('period_s')} locked={R.get('locked')}")

    R = nw.find_rhythm(scheduled(300, 100, 115, 0.6, seed=3), T0, end)
    s.check("a 5-minute job is not mistaken for 10 minutes",
            R["found"] and R["period_s"] == 300, str(R.get("period_s")))
    R = nw.find_rhythm(scheduled(900, 400, 420, 0.7, seed=4), T0, end)
    s.check("a 15-minute job is not mistaken for 5 minutes",
            R["found"] and R["period_s"] == 900, str(R.get("period_s")))

    R = nw.find_rhythm(scheduled(600, 30, 36, 0.8, seed=5, drift=3.0), T0, end)
    s.check("a timer that re-arms after each run is still found",
            R["found"] and abs(R["period_s"] - 603) < 0.6, str(R.get("period_s")))
    s.check("and is recognised as drifting against the clock", R.get("locked") is False)

    R = nw.find_rhythm(noise(80, seed=6), T0, end)
    s.check("random loss is not a schedule", not R["found"], str(R.get("period_s")))
    outage = set(range(T0 + 5000, T0 + 5060))          # one minute dark, once
    R = nw.find_rhythm(outage | noise(20, seed=7), T0, end)
    s.check("one long outage counts once, not sixty times", not R["found"])
    five = scheduled(600, 25, 45, 1.0, hours=0.7)       # five perfectly aligned bursts
    R = nw.find_rhythm(five, T0, T0 + 2520)
    s.check("five bursts are too few to call it, however aligned",
            R["bursts"] == 5 and not R["found"], str(R.get("bursts")))


NET = {"gateway": "192.168.1.1", "iface": "eth0", "local_ip": "192.168.1.50",
       "lan_hop": "10.0.0.100", "isp_hop": "212.8.50.65", "wifi": "",
       "targets": [
           {"name": "gateway", "host": "192.168.1.1", "role": "gateway"},
           {"name": "lan-hop", "host": "10.0.0.100", "role": "lan"},
           {"name": "isp-edge", "host": "212.8.50.65", "role": "isp"},
           {"name": "google-dns", "host": "8.8.8.8", "role": "anchor"},
           {"name": "cloudflare", "host": "1.1.1.1", "role": "anchor"},
           {"name": "quad9", "host": "9.9.9.9", "role": "anchor"}]}


def synthesise(out_dir, lost, hours=4, speed_every=900, speed_loss=0.4,
               trace_every=0, seed=11):
    """A capture whose anchors lose exactly `lost` (plus speed-test loss)."""
    rnd = random.Random(seed)
    duration = hours * 3600
    db = os.path.join(out_dir, "netwatch.db")
    storage = nw.Storage(db)
    storage.open()
    cfg = nw.Config()
    cfg.duration = duration
    cfg.speed_interval = speed_every
    run_id = storage.start_run(cfg, nw.host_info(), NET)
    conn = sqlite3.connect(db)
    speed = [(T0 + x, T0 + x + 12) for x in range(300, duration, speed_every)] \
        if speed_every else []
    pings, wans = [], []
    for sec in range(duration):
        ts = T0 + sec
        for name, rtt, ttl in (("gateway", 0.5, 64), ("lan-hop", 0.9, 63),
                               ("isp-edge", 2.4, 252)):
            pings.append((run_id, ts, name, 1, rtt, ttl, sec, None))
        in_speed = any(a <= ts <= b for a, b in speed)
        # like the real office line: in a bad second one or two anchors go quiet,
        # never all three — partial loss, not an outage
        for idx, (name, base) in enumerate((("google-dns", 25.0), ("cloudflare", 12.0),
                                            ("quad9", 22.0))):
            dead = (ts in lost and (ts + idx) % 3 != 0) or (in_speed and rnd.random() < speed_loss)
            pings.append((run_id, ts, name, 0 if dead else 1,
                          None if dead else round(base * rnd.uniform(0.95, 1.1), 2),
                          None if dead else 56, sec, "timeout" if dead else None))
        if sec % 2 == 0:
            ok = ts not in lost
            wans.append((run_id, ts, "opendns", "203.0.113.10" if ok else None,
                         1 if ok else 0, 20.0, None if ok else "timeout"))
    conn.executemany(nw.INSERTS["ping_samples"], pings)
    conn.executemany(nw.INSERTS["wan_samples"], wans)
    conn.execute("INSERT INTO wan_ips VALUES (?,?,?,?,?,?,?,?,?)",
                 (run_id, "203.0.113.10", T0, T0 + duration, len(wans), "64500",
                  "PROVIDER-ALPHA, UA", "UA", "WAN-A"))
    for a, b in speed:
        conn.execute(nw.INSERTS["phases"], (run_id, a, b, "speed-download"))
        conn.execute(nw.INSERTS["speed_tests"], (run_id, a, b, "download", 100_000_000,
                                                 12.0, 90.0, 4, "speed.test", None))
    if trace_every:
        for x in range(200, duration, trace_every):
            for hop, ip in enumerate(("192.168.1.1", "10.0.0.100", "212.8.50.65"), 1):
                conn.execute(nw.INSERTS["trace_hops"],
                             (run_id, T0 + x, "1.1.1.1", hop, ip, 1.0 * hop))
    conn.execute("UPDATE runs SET ended_at=?, status='finished' WHERE id=?",
                 (T0 + duration, run_id))
    conn.commit()
    conn.close()
    return db


def full_analysis(s):
    root = tempfile.mkdtemp(prefix="netwatch-rhythm-")
    try:
        # the office case: a job at 0:25-0:45 of every 10 minutes, LAN untouched
        work = os.path.join(root, "office")
        os.makedirs(work)
        db = synthesise(work, scheduled(600, 25, 45, 0.6, hours=4, seed=21) | noise(4, 4, 22))
        A = nw.analyze(db)
        R = A["rhythm"]
        s.check("the analysis finds the 10-minute job", R["found"] and R["period_s"] == 600,
                str(R.get("period_s")))
        s.check("its own speed tests are not mistaken for it", not R.get("own_job"))
        s.check("the public-IP lookups corroborate it",
                R.get("wan_fail_inside", 0) >= R["in_window"] // 2,
                f"{R.get('wan_fail_inside')} of {R.get('wan_fail_total')}")
        s.check("the hops before the internet stayed clean meanwhile",
                all(v["lost"] == 0 for v in R["hops"].values()) and len(R["hops"]) == 3,
                str(R["hops"]))
        top = next((f for f in A["findings"] if "comes back every" in f["title"]),
                   {"title": "", "detail": "", "severity": ""})
        s.check("the verdict names it as a warning",
                top["severity"] == "warning" and "every 10 min" in top["title"],
                f"{top['severity']}: {top['title']}")
        s.check("and says where it breaks",
                "LAN and the provider's first router were fine" in top["detail"])
        s.check("and that nothing queued up before it", "dropped, not queued" in top["detail"])
        s.check("the score counts it", "regularity" in A["score_parts"],
                str(sorted(A["score_parts"])))
        report, charts = nw.build_report(A, work)
        body = open(report, encoding="utf-8").read()
        s.check("the report has a schedule section",
                "## 🔁 Loss that comes back on a schedule" in body)
        s.check("with the chart",
                any(c.endswith("rhythm.svg") for c in charts)
                and open(os.path.join(work, "charts", "rhythm.svg"),
                         encoding="utf-8").read().startswith("<svg"))
        s.check("and the burst times to compare with a router log",
                "The bursts in the slice" in body)
        summary = json.load(open(os.path.join(work, "summary.json"), encoding="utf-8"))
        s.check("summary.json carries the rhythm",
                summary["rhythm"]["found"] and summary["rhythm"]["period_s"] == 600
                and "inside" not in summary["rhythm"])

        # the same line without the job: nothing to report, and it says so
        work = os.path.join(root, "clean")
        os.makedirs(work)
        A2 = nw.analyze(synthesise(work, noise(30, 4, 23)))
        s.check("random loss produces no schedule finding",
                not A2["rhythm"]["found"]
                and not any("comes back every" in f["title"] for f in A2["findings"]))
        s.check("and no regularity penalty", "regularity" not in A2["score_parts"])
        s.check("the job costs score", A["score"] < A2["score"],
                f"{A['score']} vs {A2['score']}")
        report, _charts = nw.build_report(A2, work)
        s.check("the report says the loss was tested and has no schedule",
                "No schedule in the loss" in open(report, encoding="utf-8").read())

        # loss only while netwatch runs its own speed tests: excluded, not "found"
        work = os.path.join(root, "speed")
        os.makedirs(work)
        A3 = nw.analyze(synthesise(work, set(), speed_loss=0.6))
        s.check("loss during its own speed tests is left out", not A3["rhythm"]["found"],
                str(A3["rhythm"].get("period_s")))

        # loss right after each of netwatch's own traceroutes: found, but blamed on itself
        work = os.path.join(root, "trace")
        os.makedirs(work)
        after_trace = {T0 + x + 2 for x in range(200, 4 * 3600, 1800)}
        A4 = nw.analyze(synthesise(work, after_trace, trace_every=1800, speed_every=0))
        R4 = A4["rhythm"]
        s.check("a rhythm that matches its own traceroute is recognised as its own",
                R4["found"] and R4.get("own_job") == "traceroute", str(R4.get("own_job")))
        s.check("reported as information, not a fault",
                any(f["severity"] == "info" and "netwatch's own" in f["title"]
                    for f in A4["findings"]))
        s.check("and not charged to the connection", "regularity" not in A4["score_parts"])
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main():
    s = Suite("netwatch rhythm: loss that comes back on a schedule")
    detector(s)
    full_analysis(s)
    return s.finish()


if __name__ == "__main__":
    sys.exit(main())
