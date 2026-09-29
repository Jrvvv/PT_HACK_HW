#!/usr/bin/env python3
"""Offline model of usb_token PIN check (from dump RE at 0x10000c78)."""
from __future__ import annotations
from pathlib import Path

CODE = Path(__file__).resolve().parents[1] / "firmware" / "dumps" / "pico-flash.bin"
# XIP image: expected table at file offset 0x54ff (flash VA 0x100054ff)
EXPECTED_OFF = 0x54FF


def expected_pin(flash: bytes | None = None) -> str:
    data = flash if flash is not None else CODE.read_bytes()
    digits = list(data[EXPECTED_OFF + 1 : EXPECTED_OFF + 5])
    if any(d > 9 for d in digits):
        raise ValueError(f"unexpected non-digit bytes: {digits}")
    return "".join(str(d) for d in digits)


def check(pin: str, flash: bytes | None = None) -> bool:
    want = expected_pin(flash)
    got = f"{int(pin):04d}"
    return got == want


def main() -> None:
    pin = expected_pin()
    print(f"modeled PIN: {pin}")
    assert check(pin)
    assert not check("0000")
    print("self-check ok")


if __name__ == "__main__":
    main()
