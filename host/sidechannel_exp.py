#!/usr/bin/env python3
"""Ten LED side-channel measurements on the hackathon board.

Correct vs wrong is not known up front. Each channel is measured across digit
values; a channel "separates" only if one value sits well outside the pack.
"""

from __future__ import annotations

import json
import statistics
import time
import urllib.request
from collections import defaultdict

BASE = "http://127.0.0.1:8080"
NAMES = ("GP13", "GP12", "GP11", "GP10")
NOMINAL_HALF_US = 299_937


def api(method: str, path: str, payload: dict | None = None, timeout: float = 40):
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


def run_code(code: str, delay: float, tail: float) -> tuple[str, int, int]:
    t0 = time.perf_counter_ns()
    api("POST", "/api/code", {"code": code, "delay": delay}, timeout=30)
    time.sleep(tail)
    t1 = time.perf_counter_ns()
    return code, t0, t1


def assign(trials: list[dict], events: list[dict]) -> None:
    trials.sort(key=lambda t: t["t0"])
    for ev in events:
        ns = int(ev.get("host_ns") or 0)
        idx = None
        for i, t in enumerate(trials):
            if ns >= t["t0"]:
                idx = i
            else:
                break
        if idx is None:
            continue
        ev["trial"] = idx


def trial_edges(trial_events: list[dict], presses: list[dict]):
    if not trial_events:
        return []
    base = int(trial_events[0]["us"])
    out = []
    for e in trial_events:
        if e.get("k") != "led":
            continue
        out.append((udiff(int(e["us"]), base), int(e["i"]), int(e["v"]), int(e["us"])))
    return out


def press_offsets(trial_events: list[dict], n_digits: int) -> list[int]:
    if not trial_events:
        return []
    base = int(trial_events[0]["us"])
    presses = [udiff(int(e["us"]), base) for e in trial_events if e.get("k") == "prs"]
    if len(presses) > n_digits:
        presses = presses[-n_digits:]
    return presses


def edges_between(edges, t0, t1):
    return [e for e in edges if t0 <= e[0] < t1]


def blink_set(edges) -> list[str]:
    counts = defaultdict(int)
    polar = defaultdict(set)
    for e in edges:
        idx, val = e[1], e[2]
        counts[idx] += 1
        polar[idx].add(val)
    names = []
    for idx in range(4):
        if counts[idx] >= 2 and len(polar[idx]) == 2:
            names.append(NAMES[idx])
    return names


def first_edge(edges, idx: int):
    dts = [e[0] for e in edges if e[1] == idx]
    return min(dts) if dts else None


def periods(edges, idx: int) -> list[float]:
    dts = sorted(e[0] for e in edges if e[1] == idx)
    return [float(b - a) for a, b in zip(dts, dts[1:]) if 200_000 < (b - a) < 400_000]


def skew_us(edges) -> list[float]:
    """GP12 edge minus nearest GP13 edge, when they land in the same burst."""
    a = [e[0] for e in edges if e[1] == 0 and e[2] == 1]
    b = [e[0] for e in edges if e[1] == 1 and e[2] == 1]
    out = []
    for tb in b:
        if not a:
            break
        ta = min(a, key=lambda x: abs(x - tb))
        if abs(ta - tb) < 2000:
            out.append(float(tb - ta))
    return out


def duty_on_us(edges, idx: int) -> list[float]:
    seq = [(e[0], e[2]) for e in edges if e[1] == idx]
    out = []
    rise = None
    for dt, val in seq:
        if val == 1:
            rise = dt
        elif val == 0 and rise is not None:
            w = dt - rise
            if 1000 < w < 500_000:
                out.append(float(w))
            rise = None
    return out


def silence_after(edges, t_from: int, t_end: int) -> tuple[float | None, bool]:
    """Quiet time from the last edge after t_from (or from t_from) until the next edge or window end."""
    later = [e[0] for e in edges if e[0] >= t_from]
    if not later:
        return float(t_end - t_from), True
    # hold after the burst that follows the press: skip edges in the first 40ms, then measure gap
    burst = [dt for dt in later if dt - t_from < 40_000]
    start = max(burst) if burst else t_from
    rest = [dt for dt in later if dt > start + 1000]
    if not rest:
        return float(t_end - start), True
    return float(rest[0] - start), False


def summarize(xs: list[float]) -> dict:
    if not xs:
        return {"n": 0}
    return {
        "n": len(xs),
        "mean": round(statistics.fmean(xs), 2),
        "stdev": round(statistics.pstdev(xs), 2) if len(xs) > 1 else 0.0,
        "min": round(min(xs), 2),
        "p50": round(statistics.median(xs), 2),
        "max": round(max(xs), 2),
    }


def by_digit_spread(pairs: list[tuple[int, float]]) -> dict:
    """pairs: (digit, value). Report per-digit mean and how far the oddest digit sits."""
    groups: dict[int, list[float]] = defaultdict(list)
    for d, v in pairs:
        groups[d].append(v)
    means = {d: statistics.fmean(vs) for d, vs in groups.items() if vs}
    if len(means) < 2:
        return {"per_digit_mean": means, "separates": False}
    allv = [v for _d, v in pairs]
    center = statistics.median(allv)
    odd_d, odd_mean = max(means.items(), key=lambda kv: abs(kv[1] - center))
    within = []
    for vs in groups.values():
        if len(vs) > 1:
            within.append(statistics.pstdev(vs))
    within_s = statistics.fmean(within) if within else (statistics.pstdev(allv) if len(allv) > 1 else 0.0)
    delta = abs(odd_mean - center)
    separates = delta > max(3 * within_s, 8.0) and delta > 0.15 * max(abs(center), 1)
    return {
        "per_digit_mean": {str(k): round(v, 2) for k, v in sorted(means.items())},
        "center": round(center, 2),
        "oddest_digit": odd_d,
        "oddest_mean": round(odd_mean, 2),
        "delta": round(delta, 2),
        "within_stdev": round(within_s, 2),
        "separates": separates,
    }


def analyze(trials: list[dict], events: list[dict]) -> dict:
    assign(trials, events)
    buckets: list[list[dict]] = [[] for _ in trials]
    for ev in events:
        i = ev.get("trial")
        if i is not None:
            buckets[i].append(ev)

    rows = []
    for trial, group in zip(trials, buckets):
        group.sort(key=lambda e: (e.get("host_ns", 0), e.get("us", 0)))
        n = len(trial["code"])
        if not group:
            rows.append({"code": trial["code"], "kind": trial["kind"], "empty": True})
            continue
        base = int(group[0]["us"])
        presses = press_offsets(group, n)
        edges = []
        for e in group:
            if e.get("k") != "led":
                continue
            edges.append((udiff(int(e["us"]), base), int(e["i"]), int(e["v"])))
        t_end = udiff(int(group[-1]["us"]), base) + 1000
        row = {
            "code": trial["code"],
            "kind": trial["kind"],
            "presses_us": presses,
            "n_edges": len(edges),
        }
        if presses:
            # windows between presses, last window until end
            for pi, p in enumerate(presses):
                nxt = presses[pi + 1] if pi + 1 < len(presses) else t_end
                window = edges_between(edges, p, nxt)
                blink = blink_set(window)
                join = {}
                phase = {}
                for idx, name in enumerate(NAMES):
                    fe = first_edge(window, idx)
                    if fe is not None:
                        join[name] = round((fe - p) / 1000, 2)
                        phase[name] = round(((fe - p) % NOMINAL_HALF_US) / 1000, 2)
                per = []
                duty = []
                sk = skew_us(window)
                for idx in range(4):
                    per += periods(window, idx)
                    duty += duty_on_us(window, idx)
                span = {}
                for idx, name in enumerate(NAMES):
                    dts = [dt for dt, i, _v in window if i == idx]
                    if len(dts) >= 2:
                        span[name] = round((max(dts) - min(dts)) / 1000, 2)
                row[f"d{pi+1}"] = {
                    "blink": blink,
                    "depth": len(blink),
                    "join_ms": join,
                    "phase_mod_ms": phase,
                    "blink_span_ms": span,
                    "period_err_us": round(statistics.fmean(per) - NOMINAL_HALF_US, 2) if per else None,
                    "skew_us": round(statistics.median(sk), 1) if sk else None,
                    "duty_on_ms": round(statistics.median(duty) / 1000, 2) if duty else None,
                }
            last = presses[-1]
            quiet, cens = silence_after(edges, last, t_end)
            row["after_last"] = {
                "quiet_ms": None if quiet is None else round(quiet / 1000, 2),
                "censored": cens,
                "blink": blink_set(edges_between(edges, last, t_end)),
            }
        rows.append(row)
    return rows


def verdicts(rows: list[dict]) -> dict:
    def collect(kind: str, digit_index: int, picker):
        pairs = []
        for row in rows:
            if row.get("kind") != kind or row.get("empty"):
                continue
            cell = row.get(f"d{digit_index}")
            if not cell:
                continue
            code = row["code"]
            digit = int(code[digit_index - 1])
            val = picker(cell, row)
            if val is not None:
                pairs.append((digit, float(val)))
        return pairs

    channels = []

    def add(name: str, question: str, pairs, unit: str):
        spread = by_digit_spread(pairs)
        channels.append(
            {
                "name": name,
                "question": question,
                "unit": unit,
                "overall": summarize([v for _d, v in pairs]),
                **spread,
            }
        )

    # 1. how long all-four stay quiet after a full code
    q = []
    cens_n = 0
    for row in rows:
        if row.get("kind") != "full" or row.get("empty"):
            continue
        aft = row.get("after_last") or {}
        if aft.get("quiet_ms") is None:
            continue
        q.append((int(row["code"][0]), float(aft["quiet_ms"])))
        if aft.get("censored"):
            cens_n += 1
    add(
        "1. hold after full code",
        "After 4 digits, how long until the next LED edge (all-four stay lit)",
        q,
        "ms",
    )
    channels[-1]["censored_windows"] = cens_n

    add(
        "2. LED2 join after digit 1",
        "After the 1st digit, delay until GP12 (position 2) first edges",
        collect("single", 1, lambda c, _r: (c.get("join_ms") or {}).get("GP12")),
        "ms",
    )
    add(
        "3. LED2 blink span after digit 1",
        "How long GP12 keeps toggling after the 1st digit",
        collect("single", 1, lambda c, _r: (c.get("blink_span_ms") or {}).get("GP12")),
        "ms",
    )
    add(
        "4. LED2 blink span after digit 2",
        "How long GP12 keeps toggling after the 2nd digit, by that digit",
        collect("pair", 2, lambda c, _r: (c.get("blink_span_ms") or {}).get("GP12")),
        "ms",
    )
    add(
        "5. blink depth after digit 1",
        "How many LEDs are blinking after the 1st digit",
        collect("single", 1, lambda c, _r: c.get("depth")),
        "count",
    )
    add(
        "6. blink phase mod 300ms",
        "Phase of GP13 blink relative to the press, after digit 1",
        collect("single", 1, lambda c, _r: (c.get("phase_mod_ms") or {}).get("GP13")),
        "ms",
    )
    add(
        "7. period error",
        "Blink half-period minus 299.937 ms, after digit 1",
        collect("single", 1, lambda c, _r: c.get("period_err_us")),
        "us",
    )
    add(
        "8. GP12-GP13 skew",
        "Intra-burst delay of position-2 LED minus position-1 LED",
        collect("single", 1, lambda c, _r: c.get("skew_us")),
        "us",
    )
    add(
        "9. on-time duty",
        "High time of a blink pulse after digit 1",
        collect("single", 1, lambda c, _r: c.get("duty_on_ms")),
        "ms",
    )
    add(
        "10. quiet after 2-digit prefix",
        "After two digits, quiet time until the next edge",
        [
            (int(row["code"][0]), float(row["after_last"]["quiet_ms"]))
            for row in rows
            if row.get("kind") == "pair" and row.get("after_last") and row["after_last"].get("quiet_ms") is not None
        ],
        "ms",
    )
    # channel 10's "digit" is d1*10+d2, spread still flags an odd pair
    return {"channels": channels, "rows": rows}


def main() -> None:
    api("POST", "/api/timing/clear", {})
    trials: list[dict] = []
    plan: list[tuple[str, str, float, float]] = []
    for _rep in range(2):
        for d in range(10):
            plan.append((f"{d}", "single", 0.08, 1.1))
    for a in range(10):
        for b in range(10):
            plan.append((f"{a}{b}", "pair", 0.32, 0.85))
    for code in ["0000", "1111", "2222", "3333", "4444", "5555", "6666", "7777", "8888", "9999", "1234", "4321", "0001", "1000", "0100", "0010", "2580", "9998"]:
        plan.append((code, "full", 0.06, 2.4))

    print(f"trials {len(plan)}", flush=True)
    t_run = time.perf_counter()
    for i, (code, kind, delay, tail) in enumerate(plan):
        _c, t0, t1 = run_code(code, delay, tail)
        trials.append({"code": code, "kind": kind, "t0": t0, "t1": t1})
        if i % 10 == 0 or kind == "full":
            print(f"{i+1}/{len(plan)} {kind} {code} {(t1-t0)/1e6:.0f}ms", flush=True)
    events = api("GET", "/api/timing/events", timeout=120).get("events") or []
    rows = analyze(trials, events)
    report = verdicts(rows)
    report["wall_s"] = round(time.perf_counter() - t_run, 1)
    report["n_events"] = len(events)
    path = "/home/leon/halkaton/host/sidechannel_last.json"
    # rows can be large; keep them
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report, fh)
    print("\nWALL", report["wall_s"], "events", report["n_events"])
    for ch in report["channels"]:
        flag = "SEPARATES" if ch.get("separates") else "flat"
        print(
            f"{flag:9} {ch['name']}: n={ch['overall'].get('n')} "
            f"p50={ch['overall'].get('p50')} {ch['unit']} "
            f"odd={ch.get('oddest_digit')} delta={ch.get('delta')} within={ch.get('within_stdev')}"
        )


if __name__ == "__main__":
    main()
