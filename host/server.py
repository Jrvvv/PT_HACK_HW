#!/usr/bin/env python3
"""HALK Encoder bridge: USB serial <-> localhost web UI."""

from __future__ import annotations

import json
import random
import subprocess
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path

import serial
from PIL import Image
from serial.tools import list_ports

from lamp_color import SPOT_R, SPOT_XY, annotate, classify_image, lock_blue_spot, red_peak

ROOT = Path(__file__).resolve().parent
WWW = ROOT / "www"
PORT = 8080
BAUD = 115200

state_lock = threading.Lock()
state = {
    "pos": 0,
    "digit": 0,
    "pressed": False,
    "a": 1,
    "b": 1,
    "connected": False,
    "busy": False,
    "status": "idle",
    "probe": 0,
    "probe_pin": None,
    "leds": {"gp13": 0, "gp12": 0, "gp11": 0, "gp10": 0},
    "led_bits": [0, 0, 0, 0],
    "lamp": {
        "color": "off",
        "rgb": [0, 0, 0],
        "css": "#2a2a2a",
        "xy": [SPOT_XY[0], SPOT_XY[1]],
        "radius": SPOT_R,
        "alert": None,
        "check_color": None,
        "check_rgb": None,
        "check_path": None,
    },
}
ui_pressed = False
ser_lock = threading.Lock()
seq_lock = threading.Lock()
ser: serial.Serial | None = None
probe_log: deque[dict] = deque(maxlen=400)
timing_events: deque[dict] = deque(maxlen=250000)
_last_probe = None
_last_led_bits: list[int] | None = None
_LED_NAMES = ("gp13", "gp12", "gp11", "gp10")


def _log_probe(kind: str, **extra) -> None:
    entry = {
        "t": time.time(),
        "kind": kind,
        "pin": state.get("probe_pin"),
        "probe": state.get("probe"),
        "leds": dict(state.get("leds") or {}),
        "led_bits": list(state.get("led_bits") or []),
    }
    entry.update(extra)
    probe_log.appendleft(entry)


def apply_state(data: dict) -> None:
    global _last_probe, _last_led_bits
    with state_lock:
        if "pos" in data:
            state["pos"] = int(data["pos"])
        if "digit" in data:
            state["digit"] = int(data["digit"])
        else:
            state["digit"] = ((int(state["pos"]) % 10) + 10) % 10
        if "a" in data:
            state["a"] = int(data["a"])
        if "b" in data:
            state["b"] = int(data["b"])
        esp_pressed = bool(data["pressed"]) if "pressed" in data else False
        state["pressed"] = esp_pressed or ui_pressed
        if "probe" in data:
            val = int(data["probe"])
            state["probe"] = val
            if _last_probe is None:
                _last_probe = val
                _log_probe("sample", note="first")
            elif val != _last_probe:
                _log_probe(
                    "edge",
                    from_v=_last_probe,
                    to_v=val,
                    level="HIGH" if val else "LOW",
                )
                _last_probe = val
        if "leds" in data and isinstance(data["leds"], dict):
            state["leds"] = {
                str(k): int(v) for k, v in data["leds"].items()
            }
        if "led_bits" in data and isinstance(data["led_bits"], list):
            bits = [int(x) for x in data["led_bits"][:4]]
            while len(bits) < 4:
                bits.append(0)
            state["led_bits"] = bits
            if _last_led_bits is None:
                _last_led_bits = list(bits)
                _log_probe("leds", note="first", bits=bits)
            elif bits != _last_led_bits:
                names = ["GP13", "GP12", "GP11", "GP10"]
                changed = [
                    f"{names[i]}={'ON' if bits[i] else 'OFF'}"
                    for i in range(4)
                    if bits[i] != _last_led_bits[i]
                ]
                _log_probe(
                    "led_edge",
                    from_bits=_last_led_bits,
                    to_bits=bits,
                    note=", ".join(changed) or "leds",
                )
                _last_led_bits = list(bits)
            _note_all_four(bits)
        state["connected"] = True


def ingest_line(data: dict) -> None:
    """Snapshots update UI state. IRQ batches and press marks go to the timing log."""
    k = data.get("k")
    if k == "ledb":
        host_ns = time.perf_counter_ns()
        bits = None
        with state_lock:
            bits = list(state.get("led_bits") or [0, 0, 0, 0])
            while len(bits) < 4:
                bits.append(0)
            for item in data.get("e") or []:
                if not isinstance(item, (list, tuple)) or len(item) < 3:
                    continue
                us, idx, val = int(item[0]) & 0xFFFFFFFF, int(item[1]), int(item[2])
                if 0 <= idx <= 3:
                    bits[idx] = 1 if val else 0
                timing_events.append(
                    {
                        "k": "led",
                        "us": us,
                        "i": idx,
                        "v": 1 if val else 0,
                        "host_ns": host_ns,
                        "drop": int(data.get("d") or 0),
                    }
                )
            state["led_bits"] = bits
            state["leds"] = {name: bits[i] for i, name in enumerate(_LED_NAMES)}
            state["connected"] = True
        _note_all_four(bits)
        return
    if k in ("prs", "rel"):
        with state_lock:
            timing_events.append(
                {
                    "k": k,
                    "us": int(data.get("us") or 0) & 0xFFFFFFFF,
                    "host_ns": time.perf_counter_ns(),
                }
            )
            state["connected"] = True
        return
    apply_state(data)


def set_probe_pin(pin: str | None) -> dict:
    with state_lock:
        state["probe_pin"] = pin
        _log_probe("focus", note=("touching " + pin) if pin else "probe idle")
        return {
            "probe_pin": state["probe_pin"],
            "probe": state["probe"],
            "log": list(probe_log)[:40],
        }


def find_port() -> str:
    for p in list_ports.comports():
        name = (p.device or "").lower()
        desc = f"{p.description} {p.manufacturer}".lower()
        if "ttyusb" in name or "ttyacm" in name or "ch340" in desc or "cp210" in desc:
            return p.device
    return "/dev/ttyUSB0"


def serial_reader(port: str) -> None:
    global ser
    buf = ""
    while True:
        try:
            with ser_lock:
                if ser is None or not ser.is_open:
                    ser = serial.Serial(port, BAUD, timeout=0.05)
                    time.sleep(0.2)
                    ser.write(b"\x03")
                    time.sleep(0.05)
                    ser.write(b"\x04")
                    time.sleep(1.2)
                    ser.reset_input_buffer()
                    buf = ""
            with state_lock:
                state["connected"] = True

            while True:
                with ser_lock:
                    raw = ser.read(256)
                if raw:
                    buf += raw.decode(errors="ignore")
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        line = line.strip()
                        if not line or line.startswith("HALK_USB"):
                            continue
                        if not line.startswith("{"):
                            continue
                        try:
                            ingest_line(json.loads(line))
                        except json.JSONDecodeError:
                            pass
                else:
                    time.sleep(0.005)
        except Exception as exc:
            print("serial:", exc)
            with state_lock:
                state["connected"] = False
            with ser_lock:
                try:
                    if ser:
                        ser.close()
                except Exception:
                    pass
                ser = None
            time.sleep(1.0)


def send_cmd(payload: dict) -> dict:
    global ui_pressed
    line = json.dumps(payload) + "\n"
    with ser_lock:
        if ser is None or not ser.is_open:
            raise RuntimeError("ESP32 not connected")
        ser.write(line.encode())
        ser.flush()
    with state_lock:
        if "delta" in payload:
            state["pos"] = int(state["pos"]) + int(payload["delta"])
            state["digit"] = ((int(state["pos"]) % 10) + 10) % 10
        if "press" in payload:
            ui_pressed = bool(payload["press"])
            state["pressed"] = ui_pressed
            if ui_pressed:
                state["pos"] = 0
                state["digit"] = 0
        return dict(state)


def _set_status(msg: str, busy: bool | None = None) -> None:
    with state_lock:
        state["status"] = msg
        if busy is not None:
            state["busy"] = busy


def _pulse_press(hold_s: float = 0.08) -> None:
    send_cmd({"press": True})
    time.sleep(hold_s)
    send_cmd({"press": False})


def _step_delta(delta: int, step_s: float = 0.012) -> None:
    """Turn the encoder one detent at a time (more reliable on the Pico)."""
    delta = int(delta)
    if delta == 0:
        return
    step = 1 if delta > 0 else -1
    for _ in range(abs(delta)):
        if bf_stop.is_set():
            raise InterruptedError("stopped")
        send_cmd({"delta": step})
        time.sleep(max(step_s, 0.004))


def ensure_zero(delay: float) -> None:
    with state_lock:
        dig = int(state["digit"])
    if dig != 0:
        _set_status("reset → 0")
        _pulse_press()
        time.sleep(max(delay, 0.05))


def send_digit(digit: int, delay: float) -> dict:
    """From 0: turn to digit one step at a time, wait, push (confirm + reset to 0)."""
    digit = int(digit) % 10
    delay = max(float(delay), 0.05)

    if not seq_lock.acquire(blocking=False):
        raise RuntimeError("busy: sequence already running")
    try:
        _set_status(f"digit {digit}", True)
        ensure_zero(delay)
        if digit > 0:
            _set_status(f"turn → {digit}")
            _step_delta(digit, step_s=max(float(delay), 0.01))
            time.sleep(delay)
        _set_status(f"push confirm {digit}")
        _pulse_press()
        time.sleep(delay)
        _set_status("idle", False)
        with state_lock:
            return dict(state)
    except Exception:
        _set_status("error", False)
        raise
    finally:
        seq_lock.release()


def send_code(code: str, delay: float, *, reverse: bool = False) -> dict:
    digits = [int(ch) for ch in str(code) if ch.isdigit()]
    if not digits:
        raise ValueError("code must contain digits")
    if len(digits) > 8:
        raise ValueError("code too long (max 8)")
    delay = max(float(delay), 0.001)

    if not seq_lock.acquire(blocking=False):
        raise RuntimeError("busy: sequence already running")
    try:
        code = "".join(map(str, digits))
        way = "rev" if reverse else "fwd"
        _set_status(f"code {code} {way} finish open", True)
        ensure_zero(max(delay, 0.02))
        # Open entry is not this code. Finish it, then arm so the lamp RGB is stored under `code`.
        if not _prepare_new_password():
            raise RuntimeError("stopped before code")
        hold = max(0.015, min(0.08, delay * 2 if delay > 0.01 else 0.015))
        _send_password(digits, delay, hold, code, reverse=reverse)
        _set_status("done", False)
        with state_lock:
            out = dict(state)
            out["sent"] = "".join(map(str, digits))
            out["reverse"] = bool(reverse)
            return out
    except Exception:
        _set_status("error", False)
        raise
    finally:
        seq_lock.release()


# --- bruteforce 0000..9999 ---
bf_stop = threading.Event()
bf_thread: threading.Thread | None = None
bf = {
    "running": False,
    "stopped": False,
    "done": False,
    "current": None,
    "index": 0,
    "total": 10000,
    "start_code": 0,
    "end_code": 9999,
    "delay": 0.001,
    "started_at": None,
    "eta_s": None,
    "elapsed_s": 0.0,
    "rate": 0.0,
    "last_error": None,
}


def _bf_snapshot() -> dict:
    with state_lock:
        out = dict(bf)
        out["connected"] = state["connected"]
        out["probe"] = state["probe"]
        out["leds"] = dict(state.get("leds") or {})
        out["led_bits"] = list(state.get("led_bits") or [0, 0, 0, 0])
        out["lamp"] = dict(state.get("lamp") or {})
        return out


def estimate_bruteforce_seconds(n_codes: int, delay: float) -> float:
    """Rough wall time: avg digit 4.5, pulse ~8ms/step, short press."""
    delay = max(float(delay), 0.0)
    press = 0.015
    avg_turn = 4.5 * 0.008
    per_digit = avg_turn + delay + press + delay
    return n_codes * 4 * per_digit


def _led_bits() -> list[int]:
    with state_lock:
        bits = [int(b) for b in (state.get("led_bits") or [])]
    while len(bits) < 4:
        bits.append(0)
    return bits[:4]


# LED notes (progress GP13, GP12, GP11, GP10 = digits 1..4):
# - len1: only the first blinks, the other three stay off. Ready for a new password.
# - partial: not all four are blinking. The password is NOT entered.
#   Before a different password, finish this one (extra presses) until all four blink
#   or stay lit, then wait until it falls back to len1.
# - entered: all four blink, or all four stay lit. That password is in.
def _abort_entry() -> bool:
    with state_lock:
        running = bool(bf.get("running"))
    return running and bf_stop.is_set()


def _watch_leds(window_s: float = 0.7) -> dict:
    saw_hi = [False, False, False, False]
    saw_lo = [False, False, False, False]
    deadline = time.perf_counter() + window_s
    while time.perf_counter() < deadline:
        if _abort_entry():
            break
        bits = _led_bits()
        for i, bit in enumerate(bits):
            if bit:
                saw_hi[i] = True
            else:
                saw_lo[i] = True
        time.sleep(0.012)
    return {
        "saw_hi": saw_hi,
        "saw_lo": saw_lo,
        "blinking": [saw_hi[i] and saw_lo[i] for i in range(4)],
    }


def _entry_phase(sample: dict) -> str:
    blinking = sample["blinking"]
    saw_hi = sample["saw_hi"]
    saw_lo = sample["saw_lo"]
    if not any(saw_hi[1:]) and blinking[0]:
        return "len1"
    if all(blinking) or (all(saw_hi) and not any(saw_lo)):
        return "entered"
    return "partial"


def _finish_open_password() -> str:
    """Not-all-blinking means the password is still open. Press until it is entered or len1."""
    for _ in range(4):
        if _abort_entry():
            return "stopped"
        phase = _entry_phase(_watch_leds())
        if phase in ("len1", "entered"):
            return phase
        with state_lock:
            state["status"] = "finish open password"
        _pulse_press(0.02)
        time.sleep(0.05)
    if _abort_entry():
        return "stopped"
    return _entry_phase(_watch_leds())


def _wait_len1_blink(timeout_s: float = 2.5) -> bool:
    """Ready for a new password: GP13 blinks, GP12/GP11/GP10 stay off. Gives up if jammed."""
    saw_hi = False
    saw_lo = False
    deadline = time.perf_counter() + timeout_s
    while not _abort_entry():
        if time.perf_counter() >= deadline:
            return False
        bits = _led_bits()
        if any(bits[1:]):
            saw_hi = False
            saw_lo = False
        else:
            saw_hi = saw_hi or bits[0] == 1
            saw_lo = saw_lo or bits[0] == 0
            if saw_hi and saw_lo:
                return True
        time.sleep(0.002)
    return False


def _send_garbage() -> None:
    """Jammed entry: a throwaway 4-press, then the real password is sent separately."""
    _arm_code("junk")
    with state_lock:
        state["status"] = "garbage unstick"
    _step_delta(3, step_s=0.05)
    time.sleep(0.04)
    for _ in range(4):
        if _abort_entry():
            return
        _pulse_press(0.02)
        time.sleep(0.08)


def _prepare_new_password() -> bool:
    """Finish an open password. If that sticks, clear it with garbage, then wait for len1."""
    phase = _finish_open_password()
    if phase == "stopped":
        return False
    if phase == "len1":
        return True
    with state_lock:
        state["status"] = "wait len1"
    if _wait_len1_blink(2.5):
        return True
    if _abort_entry():
        return False
    _send_garbage()
    return _wait_len1_blink(3.0) or not _abort_entry()


def _confirm_entered(timeout_s: float = 1.8) -> bool:
    """True once all four progress LEDs are lit or all of them blinked."""
    deadline = time.perf_counter() + timeout_s
    while time.perf_counter() < deadline:
        if _abort_entry():
            return False
        if all(_led_bits()):
            return True
        time.sleep(0.02)
    return _entry_phase(_watch_leds(0.45)) == "entered"


def _send_password(
    digits: list[int], delay: float, press_hold: float, code: str, *, reverse: bool = False
) -> None:
    """Send the code. If the board jams and does not take it, garbage first, then send it again."""
    _arm_code(code)
    _send_one_code_digits(digits, delay, press_hold, reverse=reverse)
    if _confirm_entered():
        return
    if _abort_entry():
        raise InterruptedError("stopped")
    _send_garbage()
    _wait_len1_blink(3.0)
    if _abort_entry():
        raise InterruptedError("stopped")
    _arm_code(code)
    _send_one_code_digits(digits, delay, press_hold, reverse=reverse)


def _send_one_code_digits(
    digits: list[int], delay: float, press_hold: float, *, reverse: bool = False
) -> None:
    # Always one detent per command; pause between detents = delay (e.g. 1s).
    step_s = max(float(delay), 0.008)
    for d in digits:
        if bf_stop.is_set():
            raise InterruptedError("stopped")
        if d > 0:
            # reverse: from 0 turn the long way (delta = d-10) to the same digit
            delta = d - 10 if reverse else d
            _step_delta(delta, step_s=step_s)
            time.sleep(delay)
        else:
            time.sleep(max(delay, 0.0005))
        if bf_stop.is_set():
            raise InterruptedError("stopped")
        _pulse_press(press_hold)
        time.sleep(delay)


def _bruteforce_worker(codes: list[int], delay: float, label: str = "") -> None:
    delay = max(float(delay), 0.0)
    press_hold = 0.015
    total = len(codes)
    if total == 0:
        with state_lock:
            bf.update(
                {
                    "running": False,
                    "stopped": False,
                    "done": True,
                    "index": 0,
                    "total": 0,
                    "last_error": None,
                    "current": None,
                }
            )
            state["busy"] = False
            state["status"] = "done"
        return
    start = codes[0]
    end = codes[-1]
    t0 = time.time()
    with state_lock:
        bf.update(
            {
                "running": True,
                "stopped": False,
                "done": False,
                "index": 0,
                "total": total,
                "start_code": start,
                "end_code": end,
                "delay": delay,
                "started_at": t0,
                "eta_s": estimate_bruteforce_seconds(total, delay),
                "elapsed_s": 0.0,
                "rate": 0.0,
                "last_error": None,
                "current": f"{start:04d}",
                "mode": label or "range",
            }
        )
        state["busy"] = True
        state["status"] = "bruteforce"

    if not seq_lock.acquire(blocking=False):
        with state_lock:
            bf["running"] = False
            bf["last_error"] = "busy"
            state["busy"] = False
        return

    try:
        for i, n in enumerate(codes):
            if bf_stop.is_set():
                break
            code = f"{n:04d}"
            digits = [int(c) for c in code]
            with state_lock:
                bf["current"] = code
                bf["index"] = i
                elapsed = time.time() - t0
                bf["elapsed_s"] = elapsed
                if i > 0:
                    rate = i / elapsed
                    bf["rate"] = rate
                    left = total - i
                    bf["eta_s"] = left / rate if rate > 0 else None
                state["status"] = f"bf {code} wait len1"
            try:
                if not _prepare_new_password():
                    break
                if bf_stop.is_set():
                    break
                with state_lock:
                    state["status"] = f"bf {code} ({i+1}/{total})"
                _send_password(digits, delay, press_hold, code)
            except InterruptedError:
                break
            except Exception as exc:
                with state_lock:
                    bf["last_error"] = str(exc)
                break
        stopped = bf_stop.is_set()
        with state_lock:
            bf["running"] = False
            bf["stopped"] = stopped
            bf["done"] = not stopped and bf.get("last_error") is None
            bf["elapsed_s"] = time.time() - t0
            bf["index"] = min(bf["index"] + (0 if stopped else 1), total)
            state["busy"] = False
            state["status"] = "stopped" if stopped else ("done" if bf["done"] else "error")
    finally:
        seq_lock.release()
        bf_stop.clear()


def _remaining_codes() -> list[int]:
    with _code_lock:
        known = {
            int(c)
            for c in lamp_by_code
            if str(c).isdigit() and len(str(c)) <= 4 and 0 <= int(c) <= 9999
        }
        # also accept zero-padded keys
        for c in list(lamp_by_code):
            if isinstance(c, str) and c.isdigit() and len(c) == 4:
                known.add(int(c))
    return [n for n in range(10000) if n not in known]


def start_bruteforce(
    delay: float = 0.001,
    start: int = 0,
    end: int = 9999,
    random_remaining: bool = False,
) -> dict:
    global bf_thread
    if bf_thread and bf_thread.is_alive():
        raise RuntimeError("bruteforce already running")
    if random_remaining:
        codes = _remaining_codes()
        random.shuffle(codes)
        label = "random_remaining"
    else:
        start = max(0, min(9999, int(start)))
        end = max(0, min(9999, int(end)))
        step = 1 if end >= start else -1
        codes = list(range(start, end + step, step))
        label = "range"
    bf_stop.clear()
    bf_thread = threading.Thread(
        target=_bruteforce_worker,
        args=(codes, float(delay), label),
        daemon=True,
    )
    bf_thread.start()
    time.sleep(0.05)
    return _bf_snapshot()


def stop_bruteforce() -> dict:
    bf_stop.set()
    with state_lock:
        bf["stopped"] = True
        state["status"] = "stopping"
    return _bf_snapshot()


def _read_json(handler: BaseHTTPRequestHandler) -> dict:
    n = int(handler.headers.get("Content-Length", "0") or 0)
    raw = handler.rfile.read(n) if n else b"{}"
    try:
        return json.loads(raw.decode() or "{}")
    except json.JSONDecodeError:
        return {}


def _json_reply(handler: BaseHTTPRequestHandler, obj: dict, code: int = 200) -> None:
    body = json.dumps(obj).encode()
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    handler._cors()
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        print("[%s] %s" % (self.log_date_time_string(), fmt % args))

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Cache-Control", "no-store")

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/api/state":
            with state_lock:
                payload = dict(state)
                payload["log"] = list(probe_log)[:60]
            _json_reply(self, payload)
            return
        if path == "/api/probe/log":
            with state_lock:
                _json_reply(
                    self,
                    {
                        "probe": state["probe"],
                        "probe_pin": state["probe_pin"],
                        "leds": dict(state.get("leds") or {}),
                        "led_bits": list(state.get("led_bits") or [0, 0, 0, 0]),
                        "connected": state["connected"],
                        "log": list(probe_log)[:120],
                    },
                )
            return
        if path == "/api/bruteforce/status":
            _json_reply(self, _bf_snapshot())
            return
        if path == "/api/lamp/colors":
            with _code_lock:
                colors = dict(lamp_by_code)
            _json_reply(self, {"n": len(colors), "colors": colors})
            return
        if path == "/api/timing/events":
            with state_lock:
                ev = list(timing_events)
            _json_reply(self, {"n": len(ev), "events": ev})
            return

        rel = path
        if rel in ("/", ""):
            rel = "/index.html"
        fpath = (WWW / rel.lstrip("/")).resolve()
        if not str(fpath).startswith(str(WWW.resolve())) or not fpath.is_file():
            self.send_error(404)
            return
        data = fpath.read_bytes()
        ctype = "text/html; charset=utf-8"
        if fpath.suffix == ".css":
            ctype = "text/css"
        elif fpath.suffix == ".js":
            ctype = "application/javascript"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self._cors()
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        try:
            if path == "/api/input":
                _json_reply(self, send_cmd(_read_json(self)))
                return
            if path == "/api/digit":
                payload = _read_json(self)
                dig = int(payload.get("digit", 0))
                delay = float(payload.get("delay", 0.5))
                _json_reply(self, send_digit(dig, delay))
                return
            if path == "/api/code":
                payload = _read_json(self)
                code = str(payload.get("code", ""))
                delay = float(payload.get("delay", 0.5))
                reverse = bool(payload.get("reverse", False))
                _json_reply(self, send_code(code, delay, reverse=reverse))
                return
            if path == "/api/probe/focus":
                payload = _read_json(self)
                pin = payload.get("pin")
                if pin is not None:
                    pin = str(pin)
                _json_reply(self, set_probe_pin(pin))
                return
            if path == "/api/probe/clear":
                with state_lock:
                    probe_log.clear()
                _json_reply(self, {"ok": True, "log": []})
                return
            if path == "/api/bruteforce/start":
                payload = _read_json(self)
                delay = float(payload.get("delay", 0.001))
                start = int(payload.get("start", 0))
                end = int(payload.get("end", 9999))
                random_remaining = bool(
                    payload.get("random_remaining")
                    or payload.get("random")
                    or payload.get("mode") == "random_remaining"
                )
                _json_reply(
                    self,
                    start_bruteforce(delay, start, end, random_remaining=random_remaining),
                )
                return
            if path == "/api/bruteforce/stop":
                _json_reply(self, stop_bruteforce())
                return
            if path == "/api/timing/clear":
                with state_lock:
                    timing_events.clear()
                _json_reply(self, {"ok": True})
                return
            self.send_error(404)
        except Exception as exc:
            _json_reply(self, {"error": str(exc)}, 503)


_cam_lock = threading.Lock()
_cam_jpg: bytes | None = None
_cam_spot = SPOT_XY
_all_four_on = False
_check_lock = threading.Lock()
_last_check_t = 0.0
_code_lock = threading.Lock()
_pending_code: str | None = None
LAMP_LOG = ROOT / "lamp_colors.jsonl"
lamp_by_code: dict[str, dict] = {}


def _load_lamp_log() -> None:
    if not LAMP_LOG.is_file():
        return
    with LAMP_LOG.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            code = str(row.get("code") or "")
            if code:
                lamp_by_code[code] = row


def _arm_code(code: str) -> None:
    global _pending_code
    with _code_lock:
        _pending_code = str(code)


def _consume_code() -> str | None:
    global _pending_code
    with _code_lock:
        code = _pending_code
        _pending_code = None
        return code


def _save_password_rgb(code: str | None, rgb: list[int], color: str, peak: bool = False) -> None:
    if not code:
        return
    row = {
        "code": code,
        "rgb": [int(v) for v in rgb],
        "color": color,
        "peak": bool(peak),
        "t": time.time(),
    }
    with _code_lock:
        lamp_by_code[code] = row
        LAMP_LOG.parent.mkdir(parents=True, exist_ok=True)
        with LAMP_LOG.open("a") as fh:
            fh.write(json.dumps(row) + "\n")
        n = len(lamp_by_code)
    with state_lock:
        lamp = state["lamp"]
        lamp["code"] = code
        lamp["saved_n"] = n
    print(f"LAMP SAVE {code} rgb={row['rgb']} {color}", flush=True)


def _latest_jpeg() -> bytes | None:
    with _cam_lock:
        return _cam_jpg


def _note_all_four(bits: list[int]) -> None:
    global _all_four_on
    all_on = len(bits) >= 4 and all(int(b) == 1 for b in bits[:4])
    rising = all_on and not _all_four_on
    _all_four_on = all_on
    if rising:
        threading.Thread(target=_on_password_lamps, daemon=True).start()


def _on_password_lamps() -> None:
    """While all four progress LEDs are on, catch a red flash. It peaks and does not stay lit."""
    global _last_check_t
    now = time.time()
    with _check_lock:
        if now - _last_check_t < 1.2:
            return
        _last_check_t = now
    code = _consume_code()
    deadline = time.time() + 1.3
    samples: list[tuple[bytes, dict]] = []
    reds: list[tuple[bytes, dict]] = []
    seen: set[bytes] = set()
    while time.time() < deadline:
        jpg = _latest_jpeg()
        if jpg and jpg not in seen:
            seen.add(jpg)
            try:
                im = Image.open(BytesIO(jpg))
                peak = red_peak(im, _cam_spot, SPOT_R)
                info = classify_image(im, _cam_spot, SPOT_R)
            except Exception:
                peak = None
                info = None
            if peak is not None:
                reds.append((jpg, peak))
            if info is not None:
                samples.append((jpg, info))
        time.sleep(0.012)
    # One red frame is enough: the lamp peaks and then goes out.
    best_jpg: bytes | None = None
    best_info: dict | None = None
    if reds:
        best_jpg, best_info = max(reds, key=lambda item: int(item[1].get("pixels") or 0))
    else:
        changed = [(jpg, info) for jpg, info in samples if info.get("color") not in ("cyan", "blue", "off")]
        pool = changed or samples
        if pool:
            best_jpg, best_info = max(pool, key=lambda item: float(item[1].get("weight") or 0))
    if best_info is None or best_jpg is None:
        with state_lock:
            state["lamp"]["check_color"] = "off"
            state["lamp"]["alert"] = "off"
        print("LAMP CHECK: no frame", flush=True)
        return
    path = Path("/tmp/cam/check.jpg")
    path.parent.mkdir(parents=True, exist_ok=True)
    im = Image.open(BytesIO(best_jpg))
    annotate(im, (int(best_info["xy"][0]), int(best_info["xy"][1])), int(best_info["radius"]), best_info["color"]).save(
        path, quality=90
    )
    color = str(best_info["color"])
    peaked = bool(best_info.get("peak"))
    alert = None if color == "red" else color
    with state_lock:
        lamp = state["lamp"]
        lamp["check_color"] = color
        lamp["check_rgb"] = list(best_info["rgb"])
        lamp["check_path"] = str(path)
        lamp["alert"] = alert
        lamp["peak"] = peaked
        lamp["color"] = color
        lamp["rgb"] = list(best_info["rgb"])
        lamp["css"] = best_info["css"]
        lamp["code"] = code
    _save_password_rgb(code, list(best_info["rgb"]), color, peak=peaked)
    if alert:
        print(f"LAMP NOT RED: {color} rgb={best_info['rgb']} shot={path}", flush=True)
    else:
        print(
            f"LAMP RED peak={peaked} pixels={best_info.get('pixels')} rgb={best_info['rgb']} shot={path}",
            flush=True,
        )


def _camera_loop() -> None:
    global _cam_jpg, _cam_spot
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-fflags",
        "nobuffer",
        "-flags",
        "low_delay",
        "-f",
        "v4l2",
        "-input_format",
        "mjpeg",
        "-video_size",
        "1280x720",
        "-i",
        "/dev/video0",
        "-f",
        "mjpeg",
        "-q:v",
        "5",
        "pipe:1",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    buf = b""
    locked = False
    last_pub = 0.0
    try:
        assert proc.stdout is not None
        while True:
            chunk = proc.stdout.read(65536)
            if not chunk:
                break
            buf += chunk
            if len(buf) > 8_000_000:
                buf = buf[-2_000_000:]
            while True:
                i = buf.find(b"\xff\xd8")
                if i < 0:
                    buf = buf[-1:]
                    break
                j = buf.find(b"\xff\xd9", i + 2)
                if j < 0:
                    buf = buf[i:]
                    break
                frame = buf[i : j + 2]
                buf = buf[j + 2 :]
                with _cam_lock:
                    _cam_jpg = frame
                now = time.time()
                if now - last_pub < 0.2:
                    continue
                last_pub = now
                try:
                    im = Image.open(BytesIO(frame))
                    if not locked:
                        _cam_spot = lock_blue_spot(im, _cam_spot, SPOT_R)
                        locked = True
                        with state_lock:
                            state["lamp"]["xy"] = [int(_cam_spot[0]), int(_cam_spot[1])]
                    info = classify_image(im, _cam_spot, SPOT_R)
                    with state_lock:
                        lamp = state["lamp"]
                        lamp["color"] = info["color"]
                        lamp["rgb"] = list(info["rgb"])
                        lamp["css"] = info["css"]
                        lamp["xy"] = list(info["xy"])
                        lamp["radius"] = info["radius"]
                except Exception:
                    continue
    finally:
        proc.kill()


def main() -> None:
    port = find_port()
    print(f"USB port: {port}")
    _load_lamp_log()
    threading.Thread(target=_camera_loop, daemon=True).start()
    threading.Thread(target=serial_reader, args=(port,), daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Web UI: http://127.0.0.1:{PORT}/")
    server.serve_forever()


if __name__ == "__main__":
    main()
