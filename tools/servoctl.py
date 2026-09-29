#!/usr/bin/env python3
"""
servoctl.py -- command-line control for the USB-C 12-Channel Servo Controller.

    python servoctl.py ports                 list candidate serial ports
    python servoctl.py info                  version, positions, rail status
    python servoctl.py set 0 1500            set channel 0 to 1500 us (enables it)
    python servoctl.py get 0                 read channel 0 back
    python servoctl.py enable 0 / disable 0
    python servoctl.py center                park every channel at 1500 us
    python servoctl.py stop                  panic stop, all channels off
    python servoctl.py sweep 0               walk a channel to check wiring
    python servoctl.py analog 3              read channel 3 as an input
    python servoctl.py sense                 one rail reading
    python servoctl.py monitor               repeat it until Ctrl-C
    python servoctl.py tare                  zero the current sense
    python servoctl.py limits                show the protection limits in force
    python servoctl.py limits 5000 4400      set overcurrent / undervoltage limits
    python servoctl.py clear                 clear latched faults
    python servoctl.py repl                  interactive, one command per line
    python servoctl.py raw "S 0 1500"        send a literal protocol line

Add --mock to run any of these against the offline emulator in usc.py, with no
hardware attached. Add --port COM5 to skip auto-detection.
"""

from __future__ import annotations

import argparse
import sys
import time

import usc
from usc import ServoController, UscError


# --- connection -----------------------------------------------------------

def connect(args) -> ServoController:
    if args.mock:
        return ServoController.mock()
    return ServoController.open(port=args.port, timeout=args.timeout)


# --- subcommands ----------------------------------------------------------

def cmd_ports(args) -> int:
    matches = {p.device for p in usc.candidate_ports()}
    ports = usc.list_ports()
    if not ports:
        print("no serial ports at all")
        return 1

    print(f"{'port':<10} {'VID:PID':<10} description")
    for p in sorted(ports, key=lambda x: x.device):
        ids = f"{p.vid:04X}:{p.pid:04X}" if p.vid is not None else "-"
        mark = "  <- candidate" if p.device in matches else ""
        print(f"{p.device:<10} {ids:<10} {p.description}{mark}")

    if not matches:
        print(f"\nnothing with USB {usc.USB_VID:04X}:{usc.USB_PID:04X}; "
              "pass --port explicitly, or --mock to work offline")
        return 1
    return 0


def cmd_info(args) -> int:
    with connect(args) as dev:
        print(dev.version())
        print()

        positions = dev.positions()
        print(f"{'ch':<4} {'pulse':<10} {'timer':<12} analog")
        for ch, us in enumerate(positions):
            state = f"{us} us" if us else "off"
            analog = "yes" if ch < usc.ANALOG_CHANNELS else "-"
            print(f"{ch:<4} {state:<10} {TIMER_OF[ch]:<12} {analog}")

        print()
        print(dev.status())
        print(dev.limits())
    return 0


# From servo.h: the channel -> timer map, worth showing because a whole timer
# failing looks like four adjacent dead channels rather than random ones.
TIMER_OF = ["TIM2_CH1", "TIM2_CH2", "TIM2_CH3", "TIM2_CH4",
            "TIM3_CH1", "TIM3_CH2", "TIM3_CH3", "TIM3_CH4",
            "TIM1_CH1", "TIM1_CH2", "TIM1_CH3", "TIM4_CH3"]


def cmd_set(args) -> int:
    with connect(args) as dev:
        dev.set_us(args.channel, args.microseconds)
        print(f"ch{args.channel} = {args.microseconds} us")
    return 0


def cmd_get(args) -> int:
    with connect(args) as dev:
        us = dev.get_us(args.channel)
        print(f"ch{args.channel} = {us} us" if us else f"ch{args.channel} is off")
    return 0


def cmd_enable(args) -> int:
    with connect(args) as dev:
        dev.enable(args.channel, True)
        print(f"ch{args.channel} enabled at {dev.get_us(args.channel)} us")
    return 0


def cmd_disable(args) -> int:
    with connect(args) as dev:
        dev.disable(args.channel)
        print(f"ch{args.channel} disabled")
    return 0


def cmd_center(args) -> int:
    with connect(args) as dev:
        dev.center_all()
        print(f"all {usc.CHANNELS} channels at {usc.US_NEUTRAL} us")
    return 0


def cmd_stop(args) -> int:
    with connect(args) as dev:
        dev.stop_all()
        print("all channels disabled")
    return 0


def cmd_sweep(args) -> int:
    with connect(args) as dev:
        print(f"ch{args.channel}: {args.low} -> {args.high} us, "
              f"{args.cycles} cycle(s); Ctrl-C to stop")
        try:
            dev.sweep(args.channel, args.low, args.high,
                      step=args.step, delay=args.delay, cycles=args.cycles)
        except KeyboardInterrupt:
            print("\ninterrupted")
        finally:
            if args.park:
                dev.disable(args.channel)
                print(f"ch{args.channel} disabled")
    return 0


def cmd_analog(args) -> int:
    with connect(args) as dev:
        counts = dev.read_analog(args.channel)
        # VDDA is the reference on LQFP-48 -- there is no VREF+ pin.
        print(f"ch{args.channel} = {counts} counts  (~{counts * 3300 / 4095:.0f} mV)")
    return 0


def cmd_sense(args) -> int:
    with connect(args) as dev:
        st = dev.status()
        print(st)
        if args.raw:
            print(f"raw: I_SENSE {dev.read_raw(usc.RAW_CURRENT)} counts, "
                  f"V_SENSE {dev.read_raw(usc.RAW_VOLTAGE)} counts")
        return 0 if st.ok else 2


def cmd_monitor(args) -> int:
    with connect(args) as dev:
        print("time      current   rail      state")
        started = time.monotonic()
        try:
            for st in dev.watch(args.interval):
                state = "ok" if st.ok else ",".join(st.fault_names)
                rail = f"{st.voltage_mv / 1000:6.2f} V" if st.rail_present else "   none"
                print(f"{time.monotonic() - started:7.1f}s  "
                      f"{st.current_ma:5d} mA  {rail}  {state}")
                if args.stop_on_fault and not st.ok:
                    print("fault latched -- stopping")
                    return 2
        except KeyboardInterrupt:
            print("\nstopped")
    return 0


def cmd_tare(args) -> int:
    with connect(args) as dev:
        print("this disables every channel first")
        offset = dev.tare()
        print(f"current-sense offset stored: {offset} counts")
    return 0


def cmd_limits(args) -> int:
    if (args.overcurrent_ma is None) != (args.undervoltage_mv is None):
        print("give both limits, or neither to show the current ones")
        return 1
    with connect(args) as dev:
        if args.overcurrent_ma is not None:
            dev.set_limits(args.overcurrent_ma, args.undervoltage_mv)
        print(dev.limits())
    return 0


def cmd_clear(args) -> int:
    with connect(args) as dev:
        dev.clear_faults()
        print(dev.status())
    return 0


def cmd_boot(args) -> int:
    if not args.yes:
        print("this stops the application and leaves the board in its USB bootloader")
        print("until it is flashed or power-cycled. to reflash, just run flash.py --")
        print("it does this step itself. re-run with --yes if you want it anyway.")
        return 1
    with connect(args) as dev:
        print(dev.reboot_to_bootloader())
        print("board is now in the bootloader; flash with: python flash.py")
    return 0


def cmd_raw(args) -> int:
    with connect(args) as dev:
        print(dev.command(args.line))
    return 0


def cmd_repl(args) -> int:
    with connect(args) as dev:
        print(dev.version())
        print("one protocol command per line; 'quit' to leave, 'stop' for X")
        while True:
            try:
                line = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not line:
                continue
            if line.lower() in ("quit", "exit", "q"):
                break
            if line.lower() == "stop":
                line = "X"
            try:
                print(dev.command(line))
            except UscError as exc:
                print(f"! {exc}")
    return 0


# --- argument parsing -----------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="servoctl",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", help="serial port; default is auto-detect")
    p.add_argument("--mock", action="store_true",
                   help="use the offline emulator instead of hardware")
    p.add_argument("--timeout", type=float, default=usc.DEFAULT_TIMEOUT,
                   help="reply timeout in seconds (default %(default)s)")

    sub = p.add_subparsers(dest="command", required=True)

    def add(name, fn, help_text):
        s = sub.add_parser(name, help=help_text)
        s.set_defaults(func=fn)
        return s

    add("ports", cmd_ports, "list serial ports and mark candidates")
    add("info", cmd_info, "version, per-channel state, rail status")

    s = add("set", cmd_set, "set a channel's pulse width (also enables it)")
    s.add_argument("channel", type=int)
    s.add_argument("microseconds", type=int)

    for name, fn, text in (("get", cmd_get, "read a channel's pulse width"),
                           ("enable", cmd_enable, "enable a channel's output"),
                           ("disable", cmd_disable, "disable a channel's output"),
                           ("analog", cmd_analog, "read a channel as an analog input")):
        s = add(name, fn, text)
        s.add_argument("channel", type=int)

    add("center", cmd_center, "park every channel at 1500 us")
    add("stop", cmd_stop, "disable every channel")

    s = add("sweep", cmd_sweep, "walk one channel back and forth")
    s.add_argument("channel", type=int)
    s.add_argument("--low", type=int, default=1000)
    s.add_argument("--high", type=int, default=2000)
    s.add_argument("--step", type=int, default=10)
    s.add_argument("--delay", type=float, default=0.02)
    s.add_argument("--cycles", type=int, default=1)
    s.add_argument("--park", action="store_true",
                   help="disable the channel when finished")

    s = add("sense", cmd_sense, "one rail current/voltage/fault reading")
    s.add_argument("--raw", action="store_true", help="also print raw ADC counts")

    s = add("monitor", cmd_monitor, "repeat the rail reading until Ctrl-C")
    s.add_argument("--interval", type=float, default=0.5)
    s.add_argument("--stop-on-fault", action="store_true")

    add("tare", cmd_tare, "zero the current sense (disables all channels)")

    s = add("limits", cmd_limits,
            "show protection limits, or set them (0 disables either; "
            "setting leaves undervoltage auto mode)")
    s.add_argument("overcurrent_ma", type=int, nargs="?")
    s.add_argument("undervoltage_mv", type=int, nargs="?")

    add("clear", cmd_clear, "clear latched faults")

    s = add("boot", cmd_boot, "reboot into the board's USB bootloader")
    s.add_argument("--yes", action="store_true")

    s = add("raw", cmd_raw, "send one literal protocol line")
    s.add_argument("line")

    add("repl", cmd_repl, "interactive protocol session")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except UscError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print()
        return 130


if __name__ == "__main__":
    sys.exit(main())
