from encoder import Encoder
from machine import Pin
import sys
import json
import uselect
import time
import array
import micropython

micropython.alloc_emergency_exception_buf(128)

enc = Encoder()
probe = Pin(16, Pin.IN, Pin.PULL_DOWN)

# board GP13→ESP22, GP12→ESP5, GP11→ESP18, GP10→ESP17
_LED_WIRE = (
    (13, 22),
    (12, 5),
    (11, 18),
    (10, 17),
)
led_pins = [(gp, Pin(esp, Pin.IN, Pin.PULL_DOWN)) for gp, esp in _LED_WIRE]

# IRQ ring: 1 µs timestamps. meta = (led_index << 1) | level
_MASK = 2047
_us = array.array("I", [0] * (_MASK + 1))
_meta = array.array("B", [0] * (_MASK + 1))
_w = 0
_r = 0
_drop = 0


def _isr(pin, idx):
    global _w, _drop
    w = _w
    n = (w + 1) & _MASK
    if n == _r:
        _drop += 1
        return
    _us[w] = time.ticks_us()
    _meta[w] = (idx << 1) | (pin.value() & 1)
    _w = n


def _bind(idx):
    def cb(pin, i=idx):
        _isr(pin, i)
    return cb


for _i, (_gp, _pin) in enumerate(led_pins):
    _pin.irq(trigger=Pin.IRQ_RISING | Pin.IRQ_FALLING, handler=_bind(_i))

_poll = uselect.poll()
_poll.register(sys.stdin, uselect.POLLIN)
_line = ""


def read_leds():
    by_gp = {}
    bits = []
    for gp, pin in led_pins:
        v = pin.value()
        by_gp["gp%d" % gp] = v
        bits.append(v)
    return by_gp, bits


def emit_snap():
    snap = enc.snapshot()
    snap["probe"] = probe.value()
    leds, bits = read_leds()
    snap["leds"] = leds
    snap["led_bits"] = bits
    sys.stdout.write(json.dumps(snap) + "\n")


def drain_edges():
    global _r
    batch = []
    n = 0
    while _r != _w and n < 40:
        us = _us[_r]
        meta = _meta[_r]
        _r = (_r + 1) & _MASK
        batch.append([us, meta >> 1, meta & 1])
        n += 1
    if batch:
        sys.stdout.write(json.dumps({"k": "ledb", "e": batch, "d": _drop}) + "\n")
    return n


def emit_mark(kind):
    sys.stdout.write(json.dumps({"k": kind, "us": time.ticks_us()}) + "\n")


def handle_cmd(raw):
    raw = raw.strip()
    if not raw:
        return
    try:
        payload = json.loads(raw)
    except ValueError:
        return
    if "delta" in payload:
        enc.emulate_steps(payload["delta"])
    if "press" in payload:
        pressed = bool(payload["press"])
        enc.set_button_drive(pressed)
        # stamp after the pin is driven
        emit_mark("prs" if pressed else "rel")


print("HALK_USB_READY")
last_hb = time.ticks_ms()

while True:
    enc.update()
    drained = drain_edges()
    now = time.ticks_ms()
    if drained or time.ticks_diff(now, last_hb) > 50:
        emit_snap()
        last_hb = now

    for _ in _poll.poll(0):
        ch = sys.stdin.read(1)
        if not ch:
            continue
        if ch in ("\n", "\r"):
            handle_cmd(_line)
            _line = ""
            drain_edges()
        else:
            _line += ch
            if len(_line) > 240:
                _line = ""

    time.sleep_ms(1)
