from machine import Pin
import time


_TRANSITIONS = {
    (0, 1): 1,
    (1, 3): 1,
    (3, 2): 1,
    (2, 0): 1,
    (0, 2): -1,
    (2, 3): -1,
    (3, 1): -1,
    (1, 0): -1,
}

# Full detent Gray sequences (A<<1|B), idle = 0b11
_CW = (0b11, 0b01, 0b00, 0b10, 0b11)
_CCW = (0b11, 0b10, 0b00, 0b01, 0b11)

EDGES_PER_DETENT = 4
DIGITS = 10


class Encoder:
    def __init__(self, pin_a=21, pin_b=23, pin_btn=19, pulse_ms=2):
        self._pulse_ms = pulse_ms
        self._a = Pin(pin_a, Pin.IN, Pin.PULL_UP)
        self._b = Pin(pin_b, Pin.IN, Pin.PULL_UP)
        self._btn = Pin(pin_btn, Pin.IN, Pin.PULL_UP)
        self.position = 0  # detent steps; digit = position % 10
        self.pressed = False
        self._last_ab = (self._a.value() << 1) | self._b.value()
        self._btn_stable = 1
        self._btn_raw = 1
        self._btn_changed_ms = time.ticks_ms()
        self._drive_btn = False
        self._busy = False
        self._edge_accum = 0

    def digit(self):
        return ((self.position % DIGITS) + DIGITS) % DIGITS

    def reset_to_zero(self):
        self.position = 0
        self._edge_accum = 0

    def _release_ab(self):
        self._a.init(Pin.IN, Pin.PULL_UP)
        self._b.init(Pin.IN, Pin.PULL_UP)

    def _drive_ab(self, ab):
        a = 1 if (ab & 0b10) else 0
        b = 1 if (ab & 0b01) else 0
        if a:
            self._a.init(Pin.IN, Pin.PULL_UP)
        else:
            self._a.init(Pin.OUT, value=0)
        if b:
            self._b.init(Pin.IN, Pin.PULL_UP)
        else:
            self._b.init(Pin.OUT, value=0)

    def _release_btn(self):
        self._btn.init(Pin.IN, Pin.PULL_UP)

    def _apply_edges(self, delta):
        if not delta:
            return
        self._edge_accum += delta
        while self._edge_accum >= EDGES_PER_DETENT:
            self._edge_accum -= EDGES_PER_DETENT
            self.position += 1
        while self._edge_accum <= -EDGES_PER_DETENT:
            self._edge_accum += EDGES_PER_DETENT
            self.position -= 1

    def update(self):
        if self._busy:
            return

        if not self._drive_btn:
            ab = (self._a.value() << 1) | self._b.value()
            if ab != self._last_ab:
                self._apply_edges(_TRANSITIONS.get((self._last_ab, ab), 0))
                self._last_ab = ab

            raw = self._btn.value()
            now = time.ticks_ms()
            if raw != self._btn_raw:
                self._btn_raw = raw
                self._btn_changed_ms = now
            elif time.ticks_diff(now, self._btn_changed_ms) > 20:
                if raw != self._btn_stable:
                    was = self._btn_stable
                    self._btn_stable = raw
                    # physical press edge -> reset like the target board
                    if was == 1 and raw == 0:
                        self.reset_to_zero()

        hardware_pressed = self._btn_stable == 0
        self.pressed = hardware_pressed or self._drive_btn

    def emulate_steps(self, delta):
        """One detent pulse per UI step (= one digit step / 36 deg)."""
        delta = int(delta)
        if delta == 0:
            return
        self._busy = True
        seq = _CW if delta > 0 else _CCW
        try:
            for _ in range(abs(delta)):
                for ab in seq:
                    self._drive_ab(ab)
                    time.sleep_ms(self._pulse_ms)
                self.position += 1 if delta > 0 else -1
            self._release_ab()
            time.sleep_ms(1)
            self._last_ab = (self._a.value() << 1) | self._b.value()
            self._edge_accum = 0
        finally:
            self._busy = False
            self.update()

    def set_button_drive(self, pressed):
        pressed = bool(pressed)
        self._drive_btn = pressed
        if pressed:
            self.reset_to_zero()
            self._btn.init(Pin.OUT, value=0)
            self.pressed = True
        else:
            self._release_btn()
            time.sleep_ms(1)
            self._btn_raw = self._btn.value()
            self._btn_stable = self._btn_raw
            self.pressed = self._btn_stable == 0

    def snapshot(self):
        return {
            "pos": self.position,
            "digit": self.digit(),
            "pressed": self.pressed,
            "a": self._a.value(),
            "b": self._b.value(),
        }
