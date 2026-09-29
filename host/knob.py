#!/usr/bin/env python3
"""Python driver for the HALK encoder bridge (turn + press over HTTP)."""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any


class Knob:
    """Dёргает энкодер через host/server.py на 127.0.0.1:8080."""

    def __init__(self, base: str = "http://127.0.0.1:8080", timeout: float = 30.0) -> None:
        self.base = base.rstrip("/")
        self.timeout = float(timeout)

    def _req(self, method: str, path: str, payload: dict | None = None) -> dict:
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            self.base + path,
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            try:
                err = json.loads(body)
            except json.JSONDecodeError:
                err = {"error": body or str(exc)}
            raise RuntimeError(err.get("error") or body or str(exc)) from exc

    # --- low level ---

    def state(self) -> dict:
        return self._req("GET", "/api/state")

    def leds(self) -> list[int]:
        st = self._req("GET", "/api/bruteforce/status")
        bits = st.get("led_bits") or [0, 0, 0, 0]
        return [int(b) for b in bits[:4]]

    def turn(self, steps: int) -> dict:
        """Повернуть на N щелчков. + по часовой, − против."""
        return self._req("POST", "/api/input", {"delta": int(steps)})

    def press(self, down: bool = True) -> dict:
        return self._req("POST", "/api/input", {"press": bool(down)})

    def click(self, hold_s: float = 0.08) -> dict:
        """Нажать и отпустить кнопку."""
        self.press(True)
        time.sleep(max(0.005, float(hold_s)))
        return self.press(False)

    def zero(self, delay: float = 0.05) -> dict:
        """Сброс позиции энкодера нажатием (как ensure_zero на мосту)."""
        st = self.state()
        if int(st.get("digit") or 0) != 0:
            out = self.click()
            time.sleep(max(delay, 0.05))
            return out
        return st

    # --- high level ---

    def digit(self, dig: int, delay: float = 0.08) -> dict:
        """С нуля: крутить до цифры 0–9 и подтвердить кнопкой."""
        return self._req(
            "POST",
            "/api/digit",
            {"digit": int(dig) % 10, "delay": float(delay)},
        )

    def code(self, code: str | int, delay: float = 0.08) -> dict:
        """Ввести пароль целиком (через мост: сброс + цифры)."""
        return self._req(
            "POST",
            "/api/code",
            {"code": f"{int(code):04d}" if str(code).isdigit() else str(code), "delay": float(delay)},
        )

    def wait_leds(
        self,
        pred,
        timeout_s: float = 8.0,
        poll_s: float = 0.05,
    ) -> list[int]:
        """Ждать, пока pred(led_bits) станет True. Иначе TimeoutError."""
        deadline = time.perf_counter() + timeout_s
        last = self.leds()
        while time.perf_counter() < deadline:
            last = self.leds()
            if pred(last):
                return last
            time.sleep(poll_s)
        raise TimeoutError(f"leds timeout, last={last}")

    def wait_all_four(self, timeout_s: float = 8.0) -> list[int]:
        return self.wait_leds(lambda b: len(b) >= 4 and all(b[:4]), timeout_s=timeout_s)

    def wait_len1(self, timeout_s: float = 8.0) -> list[int]:
        """Первый мигает/есть, остальные выключены — грубая проверка по снимку."""
        saw_hi = False
        saw_lo = False
        deadline = time.perf_counter() + timeout_s
        while time.perf_counter() < deadline:
            b = self.leds()
            if any(b[1:4]):
                saw_hi = False
                saw_lo = False
            else:
                if b[0] == 1:
                    saw_hi = True
                else:
                    saw_lo = True
                if saw_hi and saw_lo:
                    return b
            time.sleep(0.02)
        raise TimeoutError(f"len1 timeout, last={self.leds()}")


def _print(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="HALK knob driver")
    p.add_argument("--base", default="http://127.0.0.1:8080")
    p.add_argument("--timeout", type=float, default=30.0)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("state")
    sub.add_parser("leds")

    t = sub.add_parser("turn", help="повернуть на N щелчков")
    t.add_argument("steps", type=int)

    c = sub.add_parser("click", help="нажать кнопку")
    c.add_argument("--hold", type=float, default=0.08)

    sub.add_parser("zero", help="сброс в 0 нажатием")

    d = sub.add_parser("digit", help="ввести одну цифру 0–9")
    d.add_argument("digit", type=int)
    d.add_argument("--delay", type=float, default=0.08)

    co = sub.add_parser("code", help="ввести 4-значный код")
    co.add_argument("code")
    co.add_argument("--delay", type=float, default=0.08)

    w = sub.add_parser("wait-all", help="ждать пока горят все 4 LED")
    w.add_argument("--timeout", type=float, default=8.0)

    args = p.parse_args(argv)
    k = Knob(base=args.base, timeout=args.timeout)

    if args.cmd == "state":
        _print(k.state())
    elif args.cmd == "leds":
        _print({"led_bits": k.leds()})
    elif args.cmd == "turn":
        _print(k.turn(args.steps))
    elif args.cmd == "click":
        _print(k.click(args.hold))
    elif args.cmd == "zero":
        _print(k.zero())
    elif args.cmd == "digit":
        _print(k.digit(args.digit, args.delay))
    elif args.cmd == "code":
        _print(k.code(args.code, args.delay))
    elif args.cmd == "wait-all":
        _print({"led_bits": k.wait_all_four(args.timeout)})
    else:
        p.error("unknown command")
        return 2
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, TimeoutError, urllib.error.URLError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
