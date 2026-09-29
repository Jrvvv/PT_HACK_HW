#!/usr/bin/env python3
"""Per-digit LED position + ignition time.

Board behaviour: the LED for the current password position blinks,
the others stay lit. After a full entry all are lit, then position 1 starts again.

Timestamps are ESP IRQ ticks_us. Each digit is followed by a settle window
so at least one blink can be seen.
"""

from __future__ import annotations

import json
import statistics
import time
import urllib.request
from collections import defaultdict

BASE = "http://127.0.0.1:8080"
NAMES = ("GP13", "GP12", "GP11", "GP10")  # position 1..4
# Settle after each digit so the current-position LED can blink.
OBSERVE_S = 0.45


def api(method: str, path: str, payload: dict | None = None, timeout: float = 60):
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


def percentile(xs: list[float], p: float) -> float:
    if not xs:
        return float("nan")
    ys = sorted(xs)
    k = (len(ys) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(ys) - 1)
    frac = k - lo
    return ys[lo] * (1 - frac) + ys[hi] * frac


def summarize(xs: list[float]) -> dict:
    if not xs:
        return {"n": 0}
    return {
        "n": len(xs),
        "mean_us": round(statistics.fmean(xs), 1),
        "stdev_us": round(statistics.pstdev(xs), 1) if len(xs) > 1 else 0.0,
        "min_us": round(min(xs), 1),
        "p50_us": round(percentile(xs, 0.50), 1),
        "max_us": round(max(xs), 1),
    }


def classify_blink(edges: list[tuple[int, int, int]]) -> int | None:
    """LED that actually blinks (both edges). Single rises are 'turned on', not blinks."""
    by: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for dt, idx, val in edges:
        if dt < 400:
            continue
        by[idx].append((dt, val))
    best_i = None
    best_score = 0
    for idx, evs in by.items():
        polarities = {v for _, v in evs}
        score = len(evs) + (3 if len(polarities) == 2 else 0)
        if len(polarities) == 2 and score > best_score:
            best_score = score
            best_i = idx
    return best_i


def first_edge(edges: list[tuple[int, int, int]], idx: int) -> float | None:
    dts = [dt for dt, i, _v in edges if i == idx and dt >= 400]
    return float(min(dts)) if dts else None


def blink_period(edges: list[tuple[int, int, int]], idx: int) -> float | None:
    dts = sorted(dt for dt, i, _v in edges if i == idx and dt >= 400)
    if len(dts) < 2:
        return None
    gaps = [b - a for a, b in zip(dts, dts[1:]) if 1000 < (b - a) < 2_000_000]
    if not gaps:
        return None
    return float(min(gaps))


def analyze(spans: list[tuple[str, int, int]], events: list[dict]) -> dict:
    rows = []
    for si, (code, t0, _t1) in enumerate(spans):
        t_end = spans[si + 1][1] if si + 1 < len(spans) else _t1 + 300_000_000
        group = [e for e in events if t0 <= int(e.get("host_ns") or 0) < t_end]
        presses = [e for e in group if e.get("k") == "prs"]
        # ensure_zero can add a leading press when the encoder was not at 0
        if len(presses) > 4:
            presses = presses[-4:]
        for pos, prs in enumerate(presses):
            t_prs = int(prs["us"])
            if pos + 1 < len(presses):
                limit = udiff(int(presses[pos + 1]["us"]), t_prs)
            else:
                limit = int(OBSERVE_S * 1_000_000) + 80_000
            window = []
            for e in group:
                if e.get("k") != "led":
                    continue
                dt = udiff(int(e["us"]), t_prs)
                if 0 <= dt <= max(limit, 0):
                    window.append((dt, int(e["i"]), int(e["v"])))
            window.sort()
            blink = classify_blink(window)
            ignite = {}
            period = {}
            for i, name in enumerate(NAMES):
                fe = first_edge(window, i)
                if fe is not None:
                    ignite[name] = round(fe, 1)
                bp = blink_period(window, i)
                if bp is not None:
                    period[name] = round(bp, 1)
            rows.append(
                {
                    "code": code,
                    "digit_index": pos + 1,
                    "digit": int(code[pos]) if pos < len(code) else None,
                    "position_blink": (blink + 1) if blink is not None else None,
                    "blink_led": NAMES[blink] if blink is not None else None,
                    "ignite_us": ignite,
                    "blink_half_us": period,
                    "edges": len(window),
                }
            )

    by_pos_led: dict[str, dict[str, list[float]]] = {
        f"digit{n}": {name: [] for name in NAMES} for n in range(1, 5)
    }
    by_blink_led: dict[str, list[float]] = {name: [] for name in NAMES}
    position_seq: dict[str, list] = {}
    for row in rows:
        position_seq.setdefault(row["code"], [])
        position_seq[row["code"]].append(row["position_blink"])
        key = f"digit{row['digit_index']}"
        for name, us in row["ignite_us"].items():
            by_pos_led[key][name].append(us)
        if row["blink_led"] and row["blink_led"] in row["ignite_us"]:
            by_blink_led[row["blink_led"]].append(row["ignite_us"][row["blink_led"]])

    return {
        "observe_s": OBSERVE_S,
        "codes": len(spans),
        "events": len(events),
        "rows": rows,
        "position_after_each_digit": position_seq,
        "ignite_by_entered_digit": {
            k: {name: summarize(xs) for name, xs in leds.items()} for k, leds in by_pos_led.items()
        },
        "ignite_of_blinking_led": {name: summarize(xs) for name, xs in by_blink_led.items()},
    }


def main() -> None:
    api("POST", "/api/timing/clear", {})
    # First digit sweep, then second digit sweep. Rest zeros.
    codes = [f"{d}000" for d in range(10)] + [f"0{d}00" for d in range(10)]
    spans: list[tuple[str, int, int]] = []
    print(f"sending {len(codes)} codes, observe {OBSERVE_S}s after each digit", flush=True)
    t_run = time.perf_counter()
    for code in codes:
        t0 = time.perf_counter_ns()
        api("POST", "/api/code", {"code": code, "delay": OBSERVE_S}, timeout=30)
        t1 = time.perf_counter_ns()
        spans.append((code, t0, t1))
        print(code, f"{(t1 - t0) / 1e6:.0f} ms", flush=True)
    time.sleep(0.2)
    events = (api("GET", "/api/timing/events", timeout=60).get("events") or [])
    report = analyze(spans, events)
    report["wall_s"] = round(time.perf_counter() - t_run, 2)
    path = "/home/leon/halkaton/host/timing_last.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)

    print("\nposition after each digit (1=GP13 … 4=GP10, null=no blink seen)")
    for code, seq in report["position_after_each_digit"].items():
        print(f"  {code}  {seq}")
    print("\nignition of the blinking LED, µs from press")
    print(json.dumps(report["ignite_of_blinking_led"], ensure_ascii=False, indent=2))
    print("wall_s", report["wall_s"], "events", report["events"])


if __name__ == "__main__":
    main()
