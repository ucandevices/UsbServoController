"""
usc.py -- host-side driver for the USB-C 12-Channel Servo Controller.

Speaks the ASCII line protocol documented in README section 5 and implemented in
embedded-ch32/User/protocol.c. One dependency, pyserial, and nothing else; the
library is importable on its own and is what servoctl.py and selftest.py drive.

    from usc import ServoController

    with ServoController.open() as dev:
        print(dev.version())
        dev.set_us(0, 1500)
        print(dev.current_ma(), "mA")

Port discovery matches the board's USB IDs (WCH 0x1A86:0xFE0C, from
USBLIB/CONFIG/usb_desc.c). Those are WCH's generic CDC defaults, so any other
WCH CDC device on the same machine will also match -- open() probes each
candidate with 'V' and keeps the one that answers with our version string.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Iterator, Optional

import serial
import serial.tools.list_ports

# --- device constants, mirrored from the firmware -------------------------

USB_VID = 0x1A86           # WCH; usb_desc.c idVendor
USB_PID = 0xFE0C           # usb_desc.c idProduct
VERSION_PREFIX = "USBServoController"

CHANNELS = 12              # servo.h SERVO_CHANNELS
ANALOG_CHANNELS = 8        # channels 0..7 have an ADC path; 8..11 do not
US_MIN = 500               # servo.h SERVO_US_MIN
US_MAX = 2500              # servo.h SERVO_US_MAX
US_NEUTRAL = 1500          # servo.h SERVO_US_NEUTRAL

RAW_CURRENT = 0            # sense.h SENSE_RAW_CURRENT
RAW_VOLTAGE = 1            # sense.h SENSE_RAW_VOLTAGE

FAULT_OVERCURRENT = 0x01   # sense.h SENSE_FAULT_OVERCURRENT
FAULT_UNDERVOLTAGE = 0x02  # sense.h SENSE_FAULT_UNDERVOLTAGE

RAIL_PRESENT_MV = 2000     # sense.h SENSE_RAIL_PRESENT_MV

# Stored scripts (script.h). tools/servoscript.py compiles the text form.
SCRIPT_OPS = {"end": 0, "move": 1, "wait": 2, "sync": 3, "off": 4,
              "loop": 5, "jump": 6, "waitin": 7, "ifin": 8}
SCRIPT_MAX_INSTR = 254     # script.h SCRIPT_MAX_INSTR
SCRIPT_FLAG_AUTORUN = 0x01
SCRIPT_CH_ALL = 255
SCRIPT_STATES = ("idle", "running", "done", "faulted")
DEFAULT_LIMIT_MA = 5000    # sense.h SENSE_DEFAULT_LIMIT_MA

# Undervoltage starts in AUTO mode: the limit is picked from the supply voltage
# once it has settled (sense.h SENSE_UV_*). Mirrored here for the emulator.
UV_2S_MIN_MV = 6600        # sense.h SENSE_UV_2S_MIN_MV
UV_2S_MAX_MV = 8800        # sense.h SENSE_UV_2S_MAX_MV
UV_2S_LIMIT_MV = 6400      # sense.h SENSE_UV_2S_LIMIT_MV
UV_3S_MIN_MV = 9900        # sense.h SENSE_UV_3S_MIN_MV
UV_3S_LIMIT_MV = 9600      # sense.h SENSE_UV_3S_LIMIT_MV


def auto_undervoltage_limit(rail_mv: int) -> int:
    """The limit the firmware's AUTO mode picks for a settled supply; 0 = none."""
    if rail_mv >= UV_3S_MIN_MV:
        return UV_3S_LIMIT_MV
    if UV_2S_MIN_MV <= rail_mv <= UV_2S_MAX_MV:
        return UV_2S_LIMIT_MV
    return 0

DEFAULT_BAUD = 115200      # ignored by CDC, but pyserial wants a number
DEFAULT_TIMEOUT = 1.0


class UscError(Exception):
    """Base for every failure this module raises."""


class DeviceError(UscError):
    """The device replied 'ERR <reason>'."""

    def __init__(self, reason: str, command: str = ""):
        self.reason = reason
        self.command = command
        super().__init__(f"{reason!r} in reply to {command!r}" if command else reason)


class ProtocolError(UscError):
    """The device replied with something the protocol does not allow."""


class NotFoundError(UscError):
    """No controller could be found on any serial port."""


def fault_names(faults: int) -> list:
    """Decode the bitmask returned by F into readable names."""
    names = []
    if faults & FAULT_OVERCURRENT:
        names.append("overcurrent")
    if faults & FAULT_UNDERVOLTAGE:
        names.append("undervoltage")
    if faults & ~(FAULT_OVERCURRENT | FAULT_UNDERVOLTAGE):
        names.append("unknown(0x%02X)" % faults)
    return names


@dataclass(frozen=True)
class Status:
    """One decoded 'F' reply."""

    faults: int
    current_ma: int
    voltage_mv: int
    rail_present: bool

    @property
    def ok(self) -> bool:
        return self.faults == 0

    @property
    def fault_names(self) -> list:
        return fault_names(self.faults)

    def __str__(self) -> str:
        rail = f"{self.voltage_mv / 1000:.2f} V" if self.rail_present else "no rail"
        state = "OK" if self.ok else "FAULT " + ",".join(self.fault_names)
        return f"{state}  {self.current_ma} mA  {rail}"


@dataclass(frozen=True)
class Limits:
    """One decoded 'L' reply: the protection limits in force."""

    overcurrent_ma: int
    undervoltage_mv: int
    auto: bool             # undervoltage limit chosen from the detected supply

    def __str__(self) -> str:
        oc = f"{self.overcurrent_ma} mA" if self.overcurrent_ma else "off"
        uv = f"{self.undervoltage_mv} mV" if self.undervoltage_mv else "off"
        how = " (auto)" if self.auto else " (manual)"
        return f"overcurrent {oc}, undervoltage {uv}{how}"


@dataclass(frozen=True)
class ScriptInfo:
    """One decoded 'QI' reply: the script stored in flash and the engine state."""

    valid: bool
    count: int
    autorun: bool
    state: str             # idle / running / done / faulted
    pc: int

    def __str__(self) -> str:
        if not self.valid:
            return f"no script stored ({self.state})"
        return (f"{self.count} instructions, autorun {'on' if self.autorun else 'off'}, "
                f"{self.state}" + (f" at {self.pc}" if self.state == "running" else ""))


# --- port discovery -------------------------------------------------------

def candidate_ports() -> list:
    """Serial ports whose USB IDs match the board. May include other WCH CDC gear."""
    return [p for p in serial.tools.list_ports.comports()
            if p.vid == USB_VID and p.pid == USB_PID]


def list_ports() -> list:
    """Every serial port on the machine, for when auto-detection needs a human."""
    return list(serial.tools.list_ports.comports())


# --- the controller -------------------------------------------------------

class ServoController:
    """A connected board. Every method maps to one protocol command."""

    def __init__(self, transport):
        # transport is anything with write()/readline()/close(); in practice a
        # serial.Serial, or MockDevice from this module.
        self._io = transport

    # -- construction ------------------------------------------------------

    @classmethod
    def open(cls, port: Optional[str] = None, timeout: float = DEFAULT_TIMEOUT,
             baud: int = DEFAULT_BAUD) -> "ServoController":
        """
        Open a named port, or find the board if port is None.

        Auto-detection is VID/PID first, then a 'V' handshake, because the
        firmware ships WCH's stock CDC identifiers rather than its own.
        """
        if port is not None:
            dev = cls(serial.Serial(port, baud, timeout=timeout))
            dev._drain()
            return dev

        tried = []
        for info in candidate_ports():
            try:
                probe = cls(serial.Serial(info.device, baud, timeout=timeout))
            except (OSError, serial.SerialException) as exc:
                tried.append(f"{info.device}: {exc}")
                continue
            try:
                probe._drain()
                if probe.version().startswith(VERSION_PREFIX):
                    return probe
                tried.append(f"{info.device}: answered V with something else")
            except UscError as exc:
                tried.append(f"{info.device}: {exc}")
            probe.close()

        detail = "; ".join(tried) if tried else \
            f"no serial port with USB {USB_VID:04X}:{USB_PID:04X}"
        raise NotFoundError(f"no servo controller found ({detail})")

    @classmethod
    def mock(cls) -> "ServoController":
        """A controller backed by the in-process protocol emulator."""
        return cls(MockDevice())

    def __enter__(self) -> "ServoController":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self._io.close()

    # -- transport ---------------------------------------------------------

    def _drain(self) -> None:
        """Throw away anything left in the buffer by a previous run."""
        reset = getattr(self._io, "reset_input_buffer", None)
        if reset:
            reset()

    def command(self, line: str) -> str:
        """
        Send one command line, return the reply with its line ending stripped.

        Raises DeviceError on 'ERR <reason>'. This is the escape hatch for
        commands the typed methods below do not cover.
        """
        self._io.write((line.strip() + "\n").encode("ascii"))
        flush = getattr(self._io, "flush", None)
        if flush:
            flush()

        raw = self._io.readline()
        if not raw:
            raise ProtocolError(f"timed out waiting for a reply to {line.strip()!r}")
        reply = raw.decode("ascii", "replace").strip()

        if reply.startswith("ERR "):
            raise DeviceError(reply[4:].strip(), line.strip())
        return reply

    def _ok(self, line: str) -> None:
        reply = self.command(line)
        if reply != "OK":
            raise ProtocolError(f"expected 'OK' for {line!r}, got {reply!r}")

    def _value(self, line: str) -> int:
        reply = self.command(line)
        parts = reply.split()
        if len(parts) != 2 or parts[0] != "OK":
            raise ProtocolError(f"expected 'OK <value>' for {line!r}, got {reply!r}")
        try:
            return int(parts[1])
        except ValueError:
            raise ProtocolError(f"non-numeric value in {reply!r}") from None

    # -- servo control -----------------------------------------------------

    def version(self) -> str:
        """'V'. Note the firmware answers with a bare string, not 'OK ...'."""
        return self.command("V")

    def set_us(self, channel: int, microseconds: int) -> None:
        """
        'S <ch> <us>'. Also *enables* the channel -- Servo_SetPulseUs sets
        s_enabled unconditionally, so this is how a channel starts moving.
        """
        self._check_channel(channel)
        if not US_MIN <= microseconds <= US_MAX:
            raise ValueError(f"{microseconds} us outside {US_MIN}..{US_MAX}")
        self._ok(f"S {channel} {microseconds}")

    def get_us(self, channel: int) -> int:
        """'G <ch>'. Returns 0 when the channel is disabled, not its last width."""
        self._check_channel(channel)
        return self._value(f"G {channel}")

    def enable(self, channel: int, on: bool = True) -> None:
        """'E <ch> <0|1>'. Refused with 'fault' while a rail fault is latched."""
        self._check_channel(channel)
        self._ok(f"E {channel} {1 if on else 0}")

    def disable(self, channel: int) -> None:
        self.enable(channel, False)

    def stop_all(self) -> None:
        """'X'. Panic stop: drops every pulse, which de-energises every servo."""
        self._ok("X")

    def read_analog(self, channel: int) -> int:
        """'A <ch>'. 12-bit count, channels 0..7 only."""
        self._check_channel(channel)
        if channel >= ANALOG_CHANNELS:
            raise ValueError(
                f"channel {channel} has no ADC path (0..{ANALOG_CHANNELS - 1} only)")
        return self._value(f"A {channel}")

    # -- rail sensing ------------------------------------------------------

    def current_ma(self) -> int:
        """'I'."""
        return self._value("I")

    def voltage_mv(self) -> int:
        """'U'."""
        return self._value("U")

    def read_raw(self, index: int) -> int:
        """'N <0|1>'. Raw counts: 0 = I_SENSE (PA5), 1 = V_SENSE (PA4)."""
        if index not in (RAW_CURRENT, RAW_VOLTAGE):
            raise ValueError("index must be 0 (current) or 1 (voltage)")
        return self._value(f"N {index}")

    def tare(self) -> int:
        """'Z'. Disables every channel first, then stores and returns the offset."""
        return self._value("Z")

    def status(self) -> Status:
        """'F'."""
        reply = self.command("F")
        parts = reply.split()
        if len(parts) != 5 or parts[0] != "OK":
            raise ProtocolError(f"malformed F reply: {reply!r}")
        try:
            return Status(int(parts[1]), int(parts[2]), int(parts[3]), parts[4] == "1")
        except ValueError:
            raise ProtocolError(f"non-numeric field in F reply: {reply!r}") from None

    def clear_faults(self) -> None:
        """'C'."""
        self._ok("C")

    def limits(self) -> Limits:
        """'L'. Limits in force, and whether undervoltage is still automatic."""
        reply = self.command("L")
        parts = reply.split()
        if len(parts) != 4 or parts[0] != "OK":
            raise ProtocolError(f"malformed L reply: {reply!r}")
        try:
            return Limits(int(parts[1]), int(parts[2]), parts[3] == "1")
        except ValueError:
            raise ProtocolError(f"non-numeric field in L reply: {reply!r}") from None

    def set_limits(self, overcurrent_ma: int, undervoltage_mv: int) -> None:
        """
        'P <mA> <mV>'. Either 0 disables that limit. Switches undervoltage out
        of AUTO mode until the board next powers up.
        """
        if overcurrent_ma < 0 or undervoltage_mv < 0:
            raise ValueError("limits must be >= 0")
        self._ok(f"P {overcurrent_ma} {undervoltage_mv}")

    # -- maintenance -------------------------------------------------------

    def reboot_to_bootloader(self) -> str:
        """
        'BOOT'. The board leaves the bus after replying, so the port dies here,
        and comes back on the same USB IDs as its bootloader (flash.py).
        """
        return self.command("BOOT")

    # -- stored scripts ----------------------------------------------------

    def script_upload(self, instructions: list, autorun: bool = False) -> int:
        """
        Replace the stored script. instructions: (op, ch, a, b) tuples, as
        servoscript.compile_script() returns. Stops a running script. Returns
        the CRC-32 the board computed.
        """
        if len(instructions) > SCRIPT_MAX_INSTR:
            raise ValueError(f"at most {SCRIPT_MAX_INSTR} instructions")
        self._ok("QC")
        for i, (op, ch, a, b) in enumerate(instructions):
            self._ok(f"QA {i} {op} {ch} {a} {b}")
        return self._value(f"QS {len(instructions)} {SCRIPT_FLAG_AUTORUN if autorun else 0}")

    def script_info(self) -> ScriptInfo:
        """'QI'."""
        parts = self.command("QI").split()
        if len(parts) != 6 or parts[0] != "OK":
            raise ProtocolError(f"malformed QI reply: {parts!r}")
        valid, count, flags, state, pc = (int(x) for x in parts[1:])
        return ScriptInfo(bool(valid), count, bool(flags & SCRIPT_FLAG_AUTORUN),
                          SCRIPT_STATES[state] if state < len(SCRIPT_STATES) else str(state),
                          pc)

    def script_read(self) -> list:
        """The stored instructions as (op, ch, a, b) tuples, read back from flash."""
        info = self.script_info()
        out = []
        for i in range(info.count if info.valid else 0):
            parts = self.command(f"QG {i}").split()
            out.append(tuple(int(x) for x in parts[1:5]))
        return out

    def script_run(self) -> None:
        """'QR': start the stored script from the top."""
        self._ok("QR")

    def script_stop(self) -> None:
        """'QX': stop the script; servos keep their positions."""
        self._ok("QX")

    # -- convenience built on the above ------------------------------------

    def center_all(self) -> None:
        """Park every channel at neutral. This enables them all."""
        for ch in range(CHANNELS):
            self.set_us(ch, US_NEUTRAL)

    def positions(self) -> list:
        """Every channel's pulse width; 0 where disabled."""
        return [self.get_us(ch) for ch in range(CHANNELS)]

    def sweep(self, channel: int, low: int = 1000, high: int = 2000,
              step: int = 10, delay: float = 0.02, cycles: int = 1) -> None:
        """Walk a channel back and forth. The first thing to run on a new servo."""
        self._check_channel(channel)
        if not US_MIN <= low <= high <= US_MAX:
            raise ValueError(f"need {US_MIN} <= low <= high <= {US_MAX}")
        for _ in range(cycles):
            path = list(range(low, high + 1, step)) + list(range(high, low - 1, -step))
            for us in path:
                self.set_us(channel, us)
                time.sleep(delay)

    def watch(self, interval: float = 0.5) -> Iterator:
        """Yield an 'F' reading forever. Ctrl-C to stop."""
        while True:
            yield self.status()
            time.sleep(interval)

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _check_channel(channel: int) -> None:
        if not 0 <= channel < CHANNELS:
            raise ValueError(f"channel {channel} outside 0..{CHANNELS - 1}")


# --- offline emulator -----------------------------------------------------

class MockDevice:
    """
    An in-process stand-in for the firmware's parser.

    The board has never been run on hardware (README section 9), so this exists
    so the tooling above can be exercised and tested now. It reimplements
    protocol.c's dispatch and a simplified sense.c closely enough to catch a
    host-side mistake, and deliberately not closely enough to be mistaken for
    the real thing -- it has no timers and no ADC noise.
    """

    IDLE_MA = 12               # the board itself
    PER_CHANNEL_MA = 120       # a small servo holding position
    RAIL_MV = 7940             # a part-discharged 2S pack, as in the README

    def __init__(self):
        self._out = bytearray()
        self._pulse = [US_NEUTRAL] * CHANNELS
        self._enabled = [False] * CHANNELS
        self._faults = 0
        self._limit_ma = DEFAULT_LIMIT_MA
        self._manual_mv = None  # None = AUTO mode, as at firmware power-up
        self._offset = 0
        self._line = ""
        self.closed = False
        self._script: list = []          # stored (op, ch, a, b)
        self._script_flags = 0
        self._script_state = 0           # the emulator "runs" a script instantly
        self._stage: dict = {}

    # transport surface, matching serial.Serial closely enough for command()
    def write(self, data: bytes) -> int:
        for ch in data.decode("ascii", "replace"):
            if ch == "\r":
                continue
            if ch == "\n":
                self._execute(self._line)
                self._line = ""
            else:
                self._line += ch
        return len(data)

    def readline(self) -> bytes:
        idx = self._out.find(b"\n")
        if idx < 0:
            return b""
        line, self._out = bytes(self._out[:idx + 1]), self._out[idx + 1:]
        return line

    def flush(self) -> None:
        pass

    def reset_input_buffer(self) -> None:
        self._out.clear()

    def close(self) -> None:
        self.closed = True

    # -- simulated sensing -------------------------------------------------

    def _current_ma(self) -> int:
        return self.IDLE_MA + self.PER_CHANNEL_MA * sum(self._enabled)

    def _limit_mv(self) -> int:
        # The emulated rail is always present and settled, so AUTO mode has
        # already classified it.
        if self._manual_mv is not None:
            return self._manual_mv
        return auto_undervoltage_limit(self.RAIL_MV)

    def _check_trip(self) -> None:
        if self._limit_ma and self._current_ma() > self._limit_ma:
            self._faults |= FAULT_OVERCURRENT
            self._enabled = [False] * CHANNELS
        if self._limit_mv() and self.RAIL_MV < self._limit_mv():
            self._faults |= FAULT_UNDERVOLTAGE
            self._enabled = [False] * CHANNELS

    # -- the parser --------------------------------------------------------

    def _reply(self, text: str) -> None:
        self._out.extend((text + "\r\n").encode("ascii"))

    def _execute(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        if line.upper().startswith("BOOT"):
            self._reply("OK BOOT")
            return

        verb, args = line[0].upper(), line[1:].split()
        nums = []

        def need(n: int) -> bool:
            # Arguments are parsed per-command, not up front, because the
            # firmware dispatches on the verb first: "bogus" is ERR unknown,
            # not ERR syntax.
            del nums[:]
            for token in args[:n]:
                if not token.isdigit():
                    break
                nums.append(int(token))
            if len(nums) < n:
                self._reply("ERR syntax")
                return False
            return True

        if verb == "S":
            if not need(2):
                return
            ch, us = nums[0], nums[1]
            if ch >= CHANNELS:
                self._reply("ERR channel")
            elif not US_MIN <= us <= US_MAX:
                self._reply("ERR range")
            else:
                # Matches the firmware: S enables the channel, and does NOT
                # consult the fault latch. See tools/README.md.
                self._pulse[ch], self._enabled[ch] = us, True
                self._check_trip()
                self._reply("OK")
        elif verb == "G":
            if not need(1):
                return
            ch = nums[0]
            if ch >= CHANNELS:
                self._reply("ERR channel")
            else:
                self._reply(f"OK {self._pulse[ch] if self._enabled[ch] else 0}")
        elif verb == "E":
            if not need(2):
                return
            ch, on = nums[0], nums[1]
            if ch >= CHANNELS:
                self._reply("ERR channel")
            elif on and self._faults:
                self._reply("ERR fault")
            else:
                self._enabled[ch] = bool(on)
                self._check_trip()
                self._reply("OK")
        elif verb == "A":
            if not need(1):
                return
            if nums[0] >= ANALOG_CHANNELS:
                self._reply("ERR noanalog")
            else:
                self._reply(f"OK {2047 + nums[0]}")
        elif verb == "X":
            self._enabled = [False] * CHANNELS
            self._reply("OK")
        elif verb == "V":
            self._reply("USBServoController 0.3 12ch CH32V203 isense (mock)")
        elif verb == "I":
            self._reply(f"OK {self._current_ma()}")
        elif verb == "U":
            self._reply(f"OK {self.RAIL_MV}")
        elif verb == "N":
            if not need(1):
                return
            if nums[0] > RAW_VOLTAGE:
                self._reply("ERR index")
            elif nums[0] == RAW_CURRENT:
                self._reply(f"OK {self._current_ma() // 4}")
            else:
                self._reply(f"OK {self.RAIL_MV // 4}")
        elif verb == "Z":
            self._enabled = [False] * CHANNELS
            self._offset = 3
            self._reply(f"OK {self._offset}")
        elif verb == "F":
            self._reply(f"OK {self._faults} {self._current_ma()} {self.RAIL_MV} 1")
        elif verb == "C":
            self._faults = 0
            self._reply("OK")
        elif verb == "P":
            if not need(2):
                return
            self._limit_ma, self._manual_mv = nums[0], nums[1]
            self._reply("OK")
        elif verb == "L":
            auto = 1 if self._manual_mv is None else 0
            self._reply(f"OK {self._limit_ma} {self._limit_mv()} {auto}")
        elif verb == "Q":
            self._script_cmd(line[1:].strip())
        else:
            self._reply("ERR unknown")

    def _script_cmd(self, rest: str) -> None:
        sub, args = (rest[:1].upper(), rest[1:].split()) if rest else ("", [])
        nums = [int(a) for a in args if a.isdigit()]
        if sub == "C":
            self._stage = {}
            self._reply("OK")
        elif sub == "A" and len(nums) == 5:
            self._stage[nums[0]] = tuple(nums[1:])
            self._reply("OK")
        elif sub == "S" and len(nums) == 2:
            self._script = [self._stage.get(i, (0, 0, 0, 0)) for i in range(nums[0])]
            self._script_flags, self._script_state = nums[1], 0
            self._reply("OK 1")
        elif sub == "I":
            valid = 1 if self._script else 0
            self._reply(f"OK {valid} {len(self._script)} {self._script_flags} "
                        f"{self._script_state} 0")
        elif sub == "G" and nums:
            self._reply("OK " + " ".join(map(str, self._script[nums[0]]))
                        if nums[0] < len(self._script) else "OK 0 0 0 0")
        elif sub == "R":
            if self._script:
                self._script_state = 2
                self._reply("OK")
            else:
                self._reply("ERR noscript")
        elif sub == "X":
            self._reply("OK")
        else:
            self._reply("ERR unknown")
