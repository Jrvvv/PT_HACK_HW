#!/usr/bin/env python3
"""Live bruteforce / lamp status in the terminal."""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG = ROOT / "lamp_colors.jsonl"
URL = "http://127.0.0.1:8080/api/bruteforce/status"
POLL_S = 1.0


def fmt(s: float | None) -> str:
    if not isinstance(s, (int, float)) or s < 0:
        return "—"
    s = int(s)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if h:
        return f"{h} ч {m:02d} мин"
    return f"{m} мин {s:02d} с"


def fetch() -> dict:
    with urllib.request.urlopen(URL, timeout=2) as r:
        return json.load(r)


def log_summary() -> tuple[int, Counter, list[str]]:
    last: dict[str, dict] = {}
    if LOG.is_file():
        for line in LOG.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            c = str(row.get("code") or "")
            if len(c) == 4 and c.isdigit():
                last[c] = row
    colors = Counter(str(r.get("color") or "?") for r in last.values())
    non = [c for c, r in sorted(last.items()) if r.get("color") != "red"]
    return len(last), colors, non


def paint(d: dict) -> None:
    L = d.get("lamp") or {}
    n, colors, non = log_summary()
    lines = [
        f"bridge   {'OK' if d.get('connected') else 'NO'}   mode={d.get('mode') or '—'}",
        f"run      {'YES' if d.get('running') else 'no'}  stopped={d.get('stopped')}  done={d.get('done')}  err={d.get('last_error')}",
        f"code     {d.get('current')}   {d.get('index')}/{d.get('total')}   range {d.get('start_code')} → {d.get('end_code')}",
        f"time     elapsed {fmt(d.get('elapsed_s'))}   eta {fmt(d.get('eta_s'))}   rate {round(float(d.get('rate') or 0), 3)}/s",
        f"leds     {d.get('led_bits')}   lamp {L.get('check_color') or L.get('color')} {L.get('check_rgb') or L.get('rgb')} peak={L.get('peak')} saved={L.get('code')}",
        f"log      {n}/10000 left {10000 - n}   colors {dict(colors)}",
        f"not_red  {non if non else '—'}",
    ]
    text = "\n".join(lines)
    sys.stdout.write("\033[H\033[J" + text + "\n")
    sys.stdout.flush()


def main() -> None:
    print("статус (Ctrl+C выход)", flush=True)
    while True:
        try:
            paint(fetch())
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            sys.stdout.write(f"\033[H\033[Jнет связи с bridge: {exc}\n")
            sys.stdout.flush()
        time.sleep(POLL_S)


if __name__ == "__main__":
    main()
