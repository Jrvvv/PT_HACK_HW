#!/usr/bin/env python3
"""Minimal demo: turn encoder, click, read LEDs. Bridge must be running."""

from knob import Knob


def main() -> None:
    k = Knob()
    print("state", k.state().get("connected"), "digit", k.state().get("digit"))
    print("leds before", k.leds())
    print("turn +2")
    k.turn(2)
    print("click")
    k.click()
    print("leds after", k.leds())


if __name__ == "__main__":
    main()
