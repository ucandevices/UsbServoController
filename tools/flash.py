#!/usr/bin/env python3
"""
flash.py -- reflash the servo controller over USB, no jumper, no replug.

    python flash.py                                   # default build output
    python flash.py path/to/USBServoController-ch32.bin
    python flash.py --port COM52 app.bin

Asks the running application to reboot into the board's USB bootloader (the
BOOT command), writes the new application, verifies it by CRC-32 and starts it.
Works the same if the board is already sitting in the bootloader -- which is
where it stays after an interrupted flash, because page 0 (the reset vector)
is written last and the bootloader will not start an application without it.

Takes the *application* image (make's build/USBServoController-ch32.bin, linked
at 0x3000). A blank chip has no bootloader yet: fit the JP2 jumper and flash
build/USBServoController-ch32-full.bin with wchisp once (see USER.md).

Bootloader protocol: embedded-ch32/bootloader/bl_main.c.
"""

from __future__ import annotations

import argparse
import sys
import time
import zlib
from pathlib import Path

import serial

import usc
from servoconfig import DEFAULT_IMAGE

PAGE = 256


class FlashError(Exception):
    pass


class Link:
    """One open CDC port, one command line out, one reply line back."""

    def __init__(self, port: str, timeout: float = 2.0):
        self.port = port
        self.ser = serial.Serial(port, 115200, timeout=timeout)
        self.ser.reset_input_buffer()

    def close(self) -> None:
        self.ser.close()

    def command(self, line: str, timeout: float | None = None) -> str:
        if timeout is not None:
            self.ser.timeout = timeout
        self.ser.write((line + "\n").encode("ascii"))
        self.ser.flush()
        raw = self.ser.readline()
        if not raw:
            raise FlashError(f"no reply to {line[:20]!r}")
        reply = raw.decode("ascii", "replace").strip()
        if reply.startswith("ERR"):
            raise FlashError(f"{line[:20]!r} failed: {reply}")
        return reply


def find_ports(explicit: str | None) -> list:
    if explicit:
        return [explicit]
    return [p.device for p in usc.candidate_ports()]


def open_matching(explicit: str | None, want, deadline: float) -> tuple:
    """
    Poll until a port answers 'V' with a reply for which want(reply) is true.
    Ports come and go while the board re-enumerates, so every failure here is
    retried until the deadline.
    """
    last = "no port with the board's USB IDs"
    while time.monotonic() < deadline:
        for port in find_ports(explicit):
            try:
                link = Link(port, timeout=1.0)
            except (OSError, serial.SerialException) as exc:
                last = f"{port}: {exc}"
                continue
            try:
                reply = link.command("V")
                if want(reply):
                    return link, reply
                last = f"{port}: answered {reply!r}"
            except (FlashError, OSError, serial.SerialException) as exc:
                last = f"{port}: {exc}"
            link.close()
        time.sleep(0.25)
    raise FlashError(f"timed out ({last})")


def is_bootloader(reply: str) -> bool:
    return reply.startswith("OK BOOTLOADER")


def is_application(reply: str) -> bool:
    return reply.startswith(usc.VERSION_PREFIX)


def enter_bootloader(explicit: str | None) -> tuple:
    link, reply = open_matching(explicit, lambda r: is_bootloader(r) or is_application(r),
                                time.monotonic() + 5.0)
    if is_bootloader(reply):
        print(f"{link.port}: already in the bootloader")
        return link, reply

    print(f"{link.port}: {reply}")
    print("rebooting into the bootloader...")
    ack = link.command("BOOT")
    if ack != "OK BOOT":
        raise FlashError(f"BOOT answered {ack!r}")
    link.close()
    time.sleep(1.0)      # let the old port disappear before polling for the new one
    link, reply = open_matching(explicit, is_bootloader, time.monotonic() + 10.0)
    print(f"{link.port}: bootloader up")
    return link, reply


def flash(image: bytes, explicit: str | None) -> None:
    link, reply = enter_bootloader(explicit)
    try:
        fields = reply.split()          # OK BOOTLOADER <ver> <base hex> <size hex>
        base, size = int(fields[3], 16), int(fields[4], 16)
        if len(image) > size:
            raise FlashError(f"image is {len(image)} bytes, application region is {size}")

        if len(image) % PAGE:
            image += b"\xff" * (PAGE - len(image) % PAGE)
        pages = len(image) // PAGE

        print(f"erasing {size // 1024} KB at 0x{base:04X}...")
        link.command("E", timeout=10.0)

        link.ser.timeout = 2.0
        order = list(range(1, pages)) + [0]     # reset vector last
        for n, page in enumerate(order, 1):
            chunk = image[page * PAGE:(page + 1) * PAGE]
            link.command(f"W {page * PAGE:X} {chunk.hex().upper()}")
            if n % 8 == 0 or n == pages:
                print(f"\rwriting {n}/{pages} pages", end="", flush=True)
        print()

        want = zlib.crc32(image) & 0xFFFFFFFF
        got = int(link.command(f"C {len(image):X}").split()[1], 16)
        if got != want:
            raise FlashError(f"CRC mismatch: device {got:08X}, image {want:08X}")
        print(f"verified, CRC-32 {got:08X}")

        link.command("G")
    finally:
        link.close()

    time.sleep(1.0)
    link, reply = open_matching(explicit, is_application, time.monotonic() + 10.0)
    link.close()
    print(f"{link.port}: running {reply}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("image", nargs="?", default=str(DEFAULT_IMAGE),
                   help="application .bin (default: %(default)s)")
    p.add_argument("--port", help="serial port; default is auto-detect by USB IDs")
    args = p.parse_args(argv)

    path = Path(args.image)
    image = path.read_bytes()
    if path.name.endswith("-full.bin"):
        print("that is the combined bootloader+application image, for wchisp only;"
              " pass the application .bin")
        return 1
    if len(image) < 4 or (image[0] & 0x7F) != 0x6F:
        print(f"{path} does not start with a jump instruction; not an application image")
        return 1

    try:
        flash(image, args.port)
    except FlashError as exc:
        print(f"\nflash failed: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
