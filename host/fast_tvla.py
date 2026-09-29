#!/usr/bin/env python3
"""Fast brute (delay 0.001) with a Welch t-test on the residual after the 300 ms blink.

Method: same idea as Lucky Thirteen / TVLA — a difference of microseconds to a few
milliseconds is invisible in one trace and shows up only as a shift of the mean
over many repeats. |t| > 4.5 is the usual leakage-assessment threshold.
"""

from __future__ import annotations

import json
import math
import statistics
import time
import urllib.request
from collections import defaultdict

BASE = "http://127.0.0.1:8080"
HALF = 299_937  # measured blink half-period, microseconds


def api(method: str, path: str, payload: dict | None = None, timeout: float = 30):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def udiff(a: int, b: int) -> int:
    d = (int(a) - int(b)) & 0xFFFFFFFF
    if d >= 0x80000000:
        d -= 0x100000000
    return d


def welch(a: list[float], b: list[float]) -> float | None:
    if len(a) < 4 or len(b) < 4:
        return None
    va = statistics.variance(a)
    vb = statistics.variance(b)
    se2 = va / len(a) + vb / len(b)
    if se2 <= 0:
        return 0.0
    return (statistics.fmean(a) - statistics.fmean(b)) / math.sqrt(se2)


def nearest_grid(dt: int, period: int = HALF) -> int:
    k = round(dt / period)
    return dt - k * period


def main() -> None:
    api("POST", "/api/timing/clear", {})
    trials = []
    # 30 repeats of each first digit, minimum delay, same shape as the first brute.
    for rep in range(30):
        for d in range(10):
            code = f"{d}000"
            t0 = time.perf_counter_ns()
            api("POST", "/api/code", {"code": code, "delay": 0.001}, timeout=20)
            t1 = time.perf_counter_ns()
            trials.append({"code": code, "digit": d, "rep": rep, "kind": "digit", "t0": t0, "t1": t1})
        print(f"rep {rep+1}/30", flush=True)
    # one uninterrupted fast run, like the first brute force the user watched
    for n in range(40):
        code = f"{n:04d}"
        t0 = time.perf_counter_ns()
        api("POST", "/api/code", {"code": code, "delay": 0.001}, timeout=20)
        t1 = time.perf_counter_ns()
        trials.append({"code": code, "digit": int(code[0]), "rep": -1, "kind": "burst", "t0": t0, "t1": t1})
    events = api("GET", "/api/timing/events", timeout=120).get("events") or []
    json.dump({"trials": trials, "events": events}, open("/tmp/fast_tvla_raw.json", "w"))

    trials.sort(key=lambda t: t["t0"])
    buckets: list[list[dict]] = [[] for _ in trials]
    for ev in events:
        ns = int(ev.get("host_ns") or 0)
        idx = None
        for i, t in enumerate(trials):
            if ns >= t["t0"] - 20_000_000:
                idx = i
            else:
                break
        if idx is not None and ns < (trials[idx + 1]["t0"] if idx + 1 < len(trials) else t["t1"] + 50_000_000):
            buckets[idx].append(ev)

    residuals = defaultdict(list)  # digit -> residual us of first post-press edge vs 300ms grid
    short_gaps = defaultdict(list)  # digit -> count of 2..80 ms gaps (strange blinks)
    gap_hist = defaultdict(int)
    burst_gaps = []

    for trial, group in zip(trials, buckets):
        if trial["kind"] != "digit" or not group:
            continue
        group.sort(key=lambda e: e.get("us", 0))
        base = int(group[0]["us"])
        presses = [udiff(int(e["us"]), base) for e in group if e.get("k") == "prs"]
        edges = [(udiff(int(e["us"]), base), int(e["i"]), int(e["v"])) for e in group if e.get("k") == "led"]
        if not presses:
            continue
        p0 = presses[0]
        # Predict the free-running blink from edges before the press, then
        # measure how far the next edge misses that prediction. A few hundred
        # microseconds of check-time show up here; the 300 ms phase does not.
        prev = [dt for dt, i, v in edges if i == 0 and v == 1 and p0 - 1_500_000 < dt < p0]
        gaps = [b - a for a, b in zip(prev, prev[1:]) if 200_000 < (b - a) < 400_000]
        after = [dt for dt, i, v in edges if i == 0 and v == 1 and dt >= p0]
        if gaps and after:
            period = statistics.median(gaps)
            pred = prev[-1]
            while pred < p0:
                pred += period
            actual = after[0]
            cands = (pred - period, pred, pred + period)
            best = min(cands, key=lambda x: abs(x - actual))
            residuals[trial["digit"]].append(actual - best)
        # gaps between successive edges of any LED, collapsed bursts (<2ms) into one
        times = []
        for dt, _i, _v in edges:
            if not times or dt - times[-1] > 2000:
                times.append(dt)
        shorts = 0
        for a, b in zip(times, times[1:]):
            gap = b - a
            if trial["kind"] == "digit":
                bucket = int(gap / 1000)
                if bucket < 800:
                    gap_hist[bucket] += 1
            if 2_000 < gap < 80_000:
                shorts += 1
                burst_gaps.append(gap / 1000)
        short_gaps[trial["digit"]].append(shorts)

    # burst-only gap histogram
    burst_hist = defaultdict(int)
    for trial, group in zip(trials, buckets):
        if trial["kind"] != "burst" or not group:
            continue
        group.sort(key=lambda e: e.get("us", 0))
        base = int(group[0]["us"])
        times = []
        for e in group:
            if e.get("k") != "led":
                continue
            dt = udiff(int(e["us"]), base)
            if not times or dt - times[-1] > 2000:
                times.append(dt)
        for a, b in zip(times, times[1:]):
            gap = (b - a) / 1000
            if gap < 800:
                burst_hist[int(gap)] += 1

    report = {"per_digit": {}, "tests": [], "gap_hist_ms": [], "burst_gap_hist_ms": []}
    pool = []
    for d in range(10):
        xs = residuals[d]
        report["per_digit"][str(d)] = {
            "n": len(xs),
            "residual_mean_us": round(statistics.fmean(xs), 1) if xs else None,
            "residual_stdev_us": round(statistics.pstdev(xs), 1) if len(xs) > 1 else None,
            "strange_blinks_mean": round(statistics.fmean(short_gaps[d]), 2) if short_gaps[d] else None,
        }
        pool.append(xs)

    for d in range(10):
        others = [v for i, xs in enumerate(pool) if i != d for v in xs]
        t = welch(pool[d], others)
        report["tests"].append(
            {
                "digit": d,
                "t": None if t is None else round(t, 2),
                "leak": t is not None and abs(t) > 4.5,
                "mean_us": report["per_digit"][str(d)]["residual_mean_us"],
            }
        )

    report["gap_hist_ms"] = sorted(
        ({"ms": k, "n": v} for k, v in gap_hist.items() if v >= 8),
        key=lambda x: -x["n"],
    )[:18]
    report["burst_gap_hist_ms"] = sorted(
        ({"ms": k, "n": v} for k, v in burst_hist.items() if v >= 3),
        key=lambda x: -x["n"],
    )[:18]
    report["n_events"] = len(events)
    report["n_trials"] = len(trials)
    if burst_gaps:
        report["strange_gap_ms"] = {
            "n": len(burst_gaps),
            "p50": round(statistics.median(burst_gaps), 2),
            "mean": round(statistics.fmean(burst_gaps), 2),
        }
    json.dump(report, open("/home/leon/halkaton/host/fast_tvla_last.json", "w"), indent=2)
    print(json.dumps({k: report[k] for k in ("tests", "per_digit", "gap_hist_ms", "burst_gap_hist_ms", "strange_gap_ms") if k in report}, indent=2))


if __name__ == "__main__":
    main()
