# `tools/` — host-side tooling

Everything here runs on the PC and talks to the board over its USB CDC port. It
is separate from `embedded-ch32/tools/`, which holds `verify_pinmap_ch32.py` —
that one checks the schematic against the firmware and never opens a serial port.

| File | What it is |
|---|---|
| `usc.py` | the driver library — one class, one method per protocol command |
| `servoctl.py` | the CLI, built on `usc.py` |
| `servogui.py` | desktop GUI: sliders, limits, speed, analog inputs, poses & sequences, rail graph, CSV log, flashing |
| `flash.py` | reflash over USB through the board's bootloader, no jumper |
| `servoscript.py` | compile / upload / run scripts the board executes without a PC |
| `servo_mcp.py` | MCP server: AI-agent control with guardrails (needs `mcp>=2`) |
| `build_release.py` | standalone GUI for Windows / Linux (PyInstaller), see section 5 |
| `servoconfig.py` | channel names, limits, poses, sequence — shared by GUI and MCP |
| `examples/` | example board scripts |
| `selftest.py` | first-power-on acceptance test, exit code 0/1 |
| `requirements.txt` | `pyserial`, and nothing else |

```sh
pip install -r requirements.txt

python servoctl.py ports            # find the board
python servoctl.py info             # version, all 12 channels, rail status
python servoctl.py set 0 1500       # move channel 0 to neutral
python servoctl.py sweep 0          # walk it, to check the wiring
python servoctl.py monitor          # watch current and voltage
python servoctl.py stop             # panic stop
python selftest.py --move --rail    # full acceptance run on a new board
```

Every one of those takes `--mock`, which swaps in an in-process emulator of the
firmware's parser and needs no hardware. **The board has never been powered
(README §9), so `--mock` is the only way this tooling has been exercised so
far** — it proves the host code, not the board.

---

## 1. Investigation: what tooling is actually used to drive servos

The question worth answering before writing anything was *what shape* this
tooling should take. The answer comes from the Maestro ecosystem, which README
§6.2 already maps — this section adds what that implies for the host side.

### The ecosystem is Python + pyserial, almost without exception

Every community client for the Mini Maestro is a thin wrapper around pyserial.
[FRC4564/Maestro](https://github.com/FRC4564/Maestro), the most-copied one,
is a single class requiring the pyserial module; it opens a port by name and
writes protocol bytes. [kalhamaar/Maestro](https://github.com/kalhamaar/Maestro)
and [mpiannucci/MiniMaestro](https://github.com/mpiannucci/MiniMaestro) are the
same shape, and [DaveForan/PololuMaestro](https://github.com/DaveForan/PololuMaestro)
is a Python-3/PEP-8 refresh of the same code. Pololu's own documentation ships a
`maestro.py` class that controls a Maestro over its serial interface.

There is no meaningful C++, Rust or Go layer in daily use, and no USB-HID or
libusb layer for *runtime control* at all — the native-USB path exists only for
configuration and script upload, and is what binds to Pololu's vendor ID.

**So: Python, pyserial, one class, no framework.** That is what `usc.py` is.

### The reference GUI, and what it does

Pololu's Maestro Control Center is the thing people actually use day to day, and
it is worth copying the *shape* of. Its tabs, from Pololu's user's guide:

| Tab | What it gives you |
|---|---|
| **Status** | one row per channel: an enable checkbox, a target slider, a position indicator — real-time control and monitoring |
| **Channel Settings** | per-channel mode (servo / input / output), min, max, neutral, speed, acceleration |
| **Sequencer** | a list of *frames*, each a full set of channel positions plus a duration in ms, played back or exported to a script |
| **Script** | write, single-step and run scripts stored on the controller |

The Status tab is the one that matters first, and it is the one a CLI can only
approximate — `servoctl.py monitor` is the closest thing here. It is free, and
runs on Windows and Linux.

**It can never work against this board.** Control Center, `UscCmd` and the
Pololu USB SDK all bind to USB vendor ID `0x1FFB`, which is Pololu's USB-IF
assignment. That is not clonable, so an equivalent host tool is part of this
project rather than optional polish — which is the reason `tools/` exists as a
first-class directory and not a scratch folder.

### Port discovery is a solved convention

pyserial's `serial.tools.list_ports.comports()` returns objects carrying `vid`,
`pid`, `serial_number` and a description, and matching on VID/PID is the
standard way host tools find their board. The wrinkle here is that the firmware
ships **WCH's stock CDC identifiers, `0x1A86:0xFE0C`** (`USBLIB/CONFIG/usb_desc.c`),
not its own — so VID/PID alone would also match any other WCH CDC device on the
machine. `usc.py` therefore filters on VID/PID *and then* probes each candidate
with `V`, keeping the one that answers with the version string. Projects that hit
the same problem solve it the same way: CircuitPython's tooling matches on the
CDC interface *name* rather than on IDs for exactly this reason.

> Worth considering for a later firmware revision: WCH does not police its VID,
> but shipping a distinct PID and a product string of your own would make
> discovery exact instead of probabilistic.

### What this means for the ordering of work

1. **A Python library + CLI first.** It matches what the ecosystem already does,
   it is what a person needs on the bench the hour the first board arrives, and
   everything else is built on top of it. ← *this directory*
2. **A Pololu/Compact protocol layer in the firmware second** (README §6.2),
   because it makes all fourteen libraries in that table work unmodified. At
   that point `usc.py` becomes a *second* way in, not the only way.
3. **A Control Center equivalent last**, once the firmware has settings worth
   persisting. A sequencer — frames of positions plus durations — is the single
   most-copied feature and is pure host-side code; it needs nothing from the
   firmware that `S` does not already provide.

---

## 2. `usc.py` — the library

```python
from usc import ServoController

with ServoController.open() as dev:        # or .open("COM5"), or .mock()
    print(dev.version())
    dev.set_us(0, 1500)                    # S 0 1500 -- also enables ch0
    print(dev.get_us(0))                   # G 0
    print(dev.status())                    # F -> Status(faults, mA, mV, rail)
    dev.stop_all()                         # X
```

One method per command in README §5: `version`, `set_us`, `get_us`, `enable`,
`disable`, `stop_all`, `read_analog`, `current_ma`, `voltage_mv`, `read_raw`,
`tare`, `status`, `clear_faults`, `set_limits`, `reboot_to_bootloader`. Anything
not covered goes through `dev.command("...")`, which returns the raw reply line
and raises `DeviceError` on `ERR`.

Two firmware behaviours the library documents rather than hides, because both
surprise people:

- **`S` enables the channel.** `Servo_SetPulseUs()` sets `s_enabled`
  unconditionally, so setting a pulse width is what starts a servo moving. There
  is no "set the target but stay off".
- **`G` returns `0` for a disabled channel**, not its last pulse width. So
  `set_us(0, 1500); disable(0); get_us(0)` gives `0`, and the stored width is
  not readable back. `positions()` returns the same 0-means-off convention.

### The emulator

`MockDevice` reimplements `protocol.c`'s dispatch and a simplified `sense.c` in
about 150 lines. It exists because the board has never been run, and it is
deliberately *not* a simulator: no timers, no ADC noise, no USB. It catches
host-side mistakes — a malformed command, a misparsed reply, a wrong error
string — and nothing else. Simulated current is `12 mA + 120 mA per enabled
channel`, enough for the fault logic to be exercised.

---

## 3. `selftest.py` — the bring-up test

Ordered so that a wiring fault cannot become a damaged servo: everything
read-only happens before anything moves, and `--move` is opt-in.

```
1. Identity          V answers, and says 12ch
2. Boot state        all 12 channels report disabled  <- an unconfigured board must not move
3. Error handling    seven rejections, each with the right ERR reason
4. Rail sensing      F/I/U/N; I_SENSE and V_SENSE not pinned at 0 or 4095
5. Analog inputs     0-7 return counts, 8-11 return ERR noanalog
6. Movement          --move only, one channel at a time, each parked before the next
7. Current sense     moves ch0, checks the peak current rises (servo on ch0)
8. Panic stop        X really does disable everything
```

It always ends with `X`, including on failure or Ctrl-C, so a failed run leaves
nothing energised. Exit code 0/1 makes it usable as a production go/no-go test.

Checks 4 and 7 are the ones that matter most on a first article, because they
are what distinguishes "R18/U4/PA5 are correctly assembled" from "the firmware
compiles". A current reading that does not change when a channel is enabled means
the sense path is not connected, whatever the numbers look like.

---

## 4. Two findings from reading the firmware alongside this

**A latched fault does not block `S`.** README §5 states that while a fault is
latched, `E <ch> 1` is refused with `ERR fault` — and it is, in `protocol.c`'s
`case 'E'`. But `case 'S'` has no such check, and `Servo_SetPulseUs()` sets
`s_enabled = true`. So `S 0 1500` re-energises a channel straight through the
interlock, and `sense.c` will only trip again after `SENSE_TRIP_COUNT` further
task periods. The fix is one line — the same `Sense_Faults() != 0` guard in
`case 'S'` — but it is a firmware change, so it is noted here rather than made.
Until then, host code must treat the interlock as advisory: `selftest.py` checks
`status()` after each movement instead of relying on it.

**`BOOT` reboots into the board's own USB bootloader** (verified on hardware
2026-09-28). It is what `flash.py` uses to reflash without a jumper: `python
flash.py [app.bin] [--port COMx]` — BOOT, erase, write (page 0 last), CRC-32
verify, start; about 3 s. `servoctl.py boot --yes` does only the first step and
leaves the board waiting in the bootloader (LED at 5 Hz). The bootloader
protocol is documented in `embedded-ch32/bootloader/bl_main.c`.

---

## 5. Standalone GUI release (`build_release.py`)

`servogui.py` packaged with PyInstaller into one executable, zipped with
`examples/` and the application firmware image (so the Firmware tab works):

    python build_release.py                       # Windows -> dist/ServoGUI-windows-x64.zip
    wsl -d Ubuntu -- ~/servogui-venv/bin/python build_release.py   # Linux -> dist/ServoGUI-linux-x64.tar.gz

It builds for the OS it runs on, so the Linux build runs in WSL (venv at
`~/servogui-venv` with `pyinstaller pyserial`; needs `python3-tk`). A frozen
build keeps `servogui.json` next to the executable and flashes by re-running
itself as `ServoGUI --run-flash <image>`. On Linux the user must be in the
`dialout` group to open the serial port.

## Sources

- [FRC4564/Maestro](https://github.com/FRC4564/Maestro) — the most-copied Python client
- [kalhamaar/Maestro](https://github.com/kalhamaar/Maestro), [mpiannucci/MiniMaestro](https://github.com/mpiannucci/MiniMaestro), [DaveForan/PololuMaestro](https://github.com/DaveForan/PololuMaestro)
- [Pololu Maestro Servo Controller User's Guide](https://www.pololu.com/docs/0j40/all) — and its [Status](https://www.pololu.com/docs/0J40/4.a), [Channel Settings](https://www.pololu.com/docs/0J40/4.b) and [Sequencer](https://www.pololu.com/docs/0J40/4.c) tabs
- [Pololu related resources](https://www.pololu.com/docs/0J40/10) — the official `maestro.py`
- [pySerial `serial.tools.list_ports`](https://pyserial.readthedocs.io/en/stable/tools.html)
