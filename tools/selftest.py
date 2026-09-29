#!/usr/bin/env python3
"""
selftest.py -- first-power-on acceptance test for the servo controller.

The board in this repository has never been run on hardware (README section 9),
so this is the script that decides whether a freshly assembled unit works. It
walks the protocol in an order that keeps a wiring fault from becoming a damaged
servo: everything that only reads happens before anything that moves.

    python selftest.py                 # safe checks only, nothing moves
    python selftest.py --move          # also move servos, one channel at a time
    python selftest.py --mock          # exercise the script itself, no hardware

Exit code is 0 when every check passed, 1 otherwise. That makes it usable as a
production go/no-go test.
"""

from __future__ import annotations

import argparse
import sys
import time

import usc
from usc import DeviceError, ServoController, UscError

TIMER_OF = ["TIM2_CH1", "TIM2_CH2", "TIM2_CH3", "TIM2_CH4",
            "TIM3_CH1", "TIM3_CH2", "TIM3_CH3", "TIM3_CH4",
            "TIM1_CH1", "TIM1_CH2", "TIM1_CH3", "TIM4_CH3"]


class Report:
    """Collects pass/fail lines and prints them as they happen."""

    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.skipped = 0

    def ok(self, what: str, detail: str = "") -> None:
        self.passed += 1
        print(f"  PASS  {what}" + (f"  -- {detail}" if detail else ""))

    def fail(self, what: str, detail: str = "") -> None:
        self.failed += 1
        print(f"  FAIL  {what}" + (f"  -- {detail}" if detail else ""))

    def skip(self, what: str, detail: str = "") -> None:
        self.skipped += 1
        print(f"  skip  {what}" + (f"  -- {detail}" if detail else ""))

    def check(self, what: str, condition: bool, detail: str = "",
              fail_detail: str = "") -> bool:
        """detail is printed either way; fail_detail only when the check fails."""
        if condition:
            self.ok(what, detail)
        else:
            self.fail(what, fail_detail or detail)
        return condition


def section(title: str) -> None:
    print(f"\n{title}\n{'-' * len(title)}")


# --- the checks -----------------------------------------------------------

def check_identity(dev: ServoController, r: Report) -> None:
    section("1. Identity")
    try:
        version = dev.version()
    except UscError as exc:
        r.fail("V returns a version string", str(exc))
        return
    r.check("V returns a version string", version.startswith(usc.VERSION_PREFIX), version)
    r.check("version reports 12 channels", "12ch" in version, version)


def check_boot_state(dev: ServoController, r: Report) -> None:
    """Every channel must come up disabled -- an unconfigured board must not move."""
    section("2. Boot state (nothing should be energised)")
    try:
        positions = dev.positions()
    except UscError as exc:
        r.fail("G works on all 12 channels", str(exc))
        return

    live = [ch for ch, us in enumerate(positions) if us]
    r.check("all 12 channels report disabled at power-on", not live,
            fail_detail=f"channels {live} already driving")


def check_errors(dev: ServoController, r: Report) -> None:
    """The error paths are as much of the contract as the happy paths."""
    section("3. Error handling")
    cases = [
        ("S 12 1500", "channel", "channel above the top one is rejected"),
        ("S 0 400", "range", "pulse below 500 us is rejected"),
        ("S 0 3000", "range", "pulse above 2500 us is rejected"),
        ("A 8", "noanalog", "analog read on a non-ADC channel is rejected"),
        ("N 2", "index", "raw sense index above 1 is rejected"),
        ("Q", "unknown", "an unknown verb is rejected"),
        ("S", "syntax", "a command missing its arguments is rejected"),
    ]
    for line, expected, what in cases:
        try:
            reply = dev.command(line)
            r.fail(what, f"expected ERR {expected}, got {reply!r}")
        except DeviceError as exc:
            r.check(what, exc.reason == expected,
                    fail_detail=f"got ERR {exc.reason}")
        except UscError as exc:
            r.fail(what, str(exc))


def check_sensing(dev: ServoController, r: Report, expect_rail: bool) -> None:
    section("4. Rail sensing")
    try:
        st = dev.status()
    except UscError as exc:
        r.fail("F returns a status line", str(exc))
        return

    r.ok("F returns a status line", str(st))
    r.check("no faults latched at power-on", st.ok, ",".join(st.fault_names))

    try:
        r.ok("L reports the limits in force", str(dev.limits()))
    except UscError as exc:
        r.fail("L reports the limits in force", str(exc))

    if expect_rail:
        r.check("servo rail detected", st.rail_present,
                fail_detail=f"V_SENSE reads {st.voltage_mv} mV, below the "
                            f"{usc.RAIL_PRESENT_MV} mV present threshold")
        # A 2S LiPo runs 6.0-8.4 V; 4xAA or a 6 V BEC sits lower. Anything
        # above 9 V on a servo rail is worth stopping for.
        r.check("rail voltage is plausible for a servo supply",
                5000 <= st.voltage_mv <= 9000, f"{st.voltage_mv / 1000:.2f} V")
    else:
        r.skip("servo rail checks", "no --rail given; assuming USB-only power")

    try:
        idle = dev.current_ma()
    except UscError as exc:
        r.fail("I returns a current", str(exc))
        return
    # Nothing is enabled yet, so this is the board's own draw plus sense offset.
    r.check("idle current is small with every channel off", idle < 200, f"{idle} mA")

    try:
        raw_i = dev.read_raw(usc.RAW_CURRENT)
        raw_v = dev.read_raw(usc.RAW_VOLTAGE)
        r.ok("N returns raw counts", f"I_SENSE {raw_i}, V_SENSE {raw_v}")
        # A rail stuck at 0 or pinned at full scale means the divider or the
        # INA180 is not connected, not that the reading is merely wrong.
        r.check("I_SENSE is not pinned", 0 < raw_i < 4095, f"{raw_i} counts")
        if expect_rail:
            r.check("V_SENSE is not pinned", 0 < raw_v < 4095, f"{raw_v} counts")
    except UscError as exc:
        r.fail("N returns raw counts", str(exc))


def check_analog(dev: ServoController, r: Report) -> None:
    section("5. Analog inputs (channels 0-7)")
    for ch in range(usc.ANALOG_CHANNELS):
        try:
            counts = dev.read_analog(ch)
            r.check(f"ch{ch} analog read", 0 <= counts <= 4095, f"{counts} counts")
        except UscError as exc:
            r.fail(f"ch{ch} analog read", str(exc))

    for ch in range(usc.ANALOG_CHANNELS, usc.CHANNELS):
        try:
            dev.command(f"A {ch}")
            r.fail(f"ch{ch} correctly refuses analog", "it answered instead")
        except DeviceError as exc:
            r.check(f"ch{ch} correctly refuses analog", exc.reason == "noanalog",
                    fail_detail=f"got ERR {exc.reason}")
        except UscError as exc:
            r.fail(f"ch{ch} correctly refuses analog", str(exc))


def check_movement(dev: ServoController, r: Report, dwell: float) -> None:
    """
    One channel at a time, each returned to neutral and disabled before the
    next. A short in a servo lead then trips the current sense on one channel
    rather than on twelve at once.
    """
    section("6. Movement, one channel at a time")
    print("  watch each servo; anything that does not move is a wiring fault\n")

    for ch in range(usc.CHANNELS):
        label = f"ch{ch} ({TIMER_OF[ch]})"
        try:
            for us in (1500, 1200, 1800, 1500):
                dev.set_us(ch, us)
                time.sleep(dwell)

            readback = dev.get_us(ch)
            r.check(f"{label} reads back its pulse width", readback == 1500,
                    fail_detail=f"got {readback} us")

            dev.disable(ch)
            off = dev.get_us(ch)
            r.check(f"{label} reports 0 once disabled", off == 0,
                    fail_detail=f"got {off} us")

            st = dev.status()
            if not st.ok:
                r.fail(f"{label} tripped a fault", str(st))
                dev.clear_faults()
        except UscError as exc:
            r.fail(label, str(exc))
            try:
                dev.stop_all()
            except UscError:
                pass


LOAD_MIN_RISE_MA = 100     # a moving hobby servo draws hundreds of mA; idle is <20


def peak_current(dev: ServoController, seconds: float) -> int:
    """Highest rail current seen while polling for the given time."""
    peak = 0
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        peak = max(peak, dev.current_ma())
    return peak


def check_current_under_load(dev: ServoController, r: Report, dwell: float) -> None:
    """
    The current sense is only proven by a current it can see appear, and a
    servo only draws real current while it is moving: one already sitting at
    the commanded position reads within noise of idle. So drive ch0 across its
    range and keep the *peak* of continuous sampling -- a single reading taken
    at a fixed moment usually lands between moves and sees nothing.

    Needs a servo on ch0 and the servo supply connected.
    """
    section("7. Current sense responds to load (servo on ch0)")
    try:
        idle = peak_current(dev, 0.3)

        dev.set_us(0, 1500)
        time.sleep(max(dwell, 0.5))
        moving = 0
        for us in (1200, 1800, 1500):
            dev.set_us(0, us)
            moving = max(moving, peak_current(dev, max(dwell, 0.6)))
        dev.disable(0)

        r.ok("current idle / peak while ch0 moves", f"{idle} mA -> {moving} mA")
        r.check(f"moving ch0 raises the current by {LOAD_MIN_RISE_MA} mA or more",
                moving - idle >= LOAD_MIN_RISE_MA,
                fail_detail=f"only {moving - idle} mA -- no servo on ch0, servo supply "
                            f"off, or R18/U4/PA5 not in the path")
    except UscError as exc:
        r.fail("current sense responds to load", str(exc))


def check_safe_shutdown(dev: ServoController, r: Report) -> None:
    section("8. Panic stop")
    try:
        dev.stop_all()
        live = [ch for ch, us in enumerate(dev.positions()) if us]
        r.check("X disables every channel", not live,
                fail_detail=f"still live: {live}")
    except UscError as exc:
        r.fail("X disables every channel", str(exc))


# --- driver ---------------------------------------------------------------

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", help="serial port; default is auto-detect")
    p.add_argument("--mock", action="store_true", help="run against the emulator")
    p.add_argument("--move", action="store_true",
                   help="also run the tests that move servos")
    p.add_argument("--rail", action="store_true",
                   help="a servo supply is connected; check its voltage")
    p.add_argument("--dwell", type=float, default=0.4,
                   help="seconds to hold each position (default %(default)s)")
    args = p.parse_args(argv)

    r = Report()
    print("USB-C 12-Channel Servo Controller -- self test")
    print("mock device" if args.mock else f"port: {args.port or 'auto-detect'}")

    try:
        dev = ServoController.mock() if args.mock else \
            ServoController.open(port=args.port)
    except UscError as exc:
        print(f"\ncannot connect: {exc}")
        return 1

    try:
        check_identity(dev, r)
        check_boot_state(dev, r)
        check_errors(dev, r)
        check_sensing(dev, r, expect_rail=args.rail)
        check_analog(dev, r)

        if args.move:
            check_movement(dev, r, args.dwell)
            check_current_under_load(dev, r, args.dwell)
        else:
            section("6-7. Movement")
            r.skip("movement tests", "pass --move once servos are safe to run")

        check_safe_shutdown(dev, r)
    finally:
        # Whatever went wrong above, leave the board with nothing energised.
        try:
            dev.stop_all()
        except UscError:
            pass
        dev.close()

    print(f"\n{'=' * 46}")
    print(f"{r.passed} passed, {r.failed} failed, {r.skipped} skipped")
    print("RESULT: PASS" if r.failed == 0 else "RESULT: FAIL")
    return 0 if r.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
