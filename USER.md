# USB-C 12-Channel Servo Controller — User Manual

This is the manual for **using** the board: what is on it, how to wire it, how to
drive it from a PC, and how to build and flash the firmware. The design
rationale, costs and known limitations live in [`README.md`](README.md); this
file only repeats what you need at the bench.

> ⚠️ **Prototype status.** Board revision v1 has **not yet been run on real
> hardware.** Everything here is taken from the schematic, the board file and the
> firmware source, and the host tools have only been exercised against their
> built-in emulator (`--mock`). Treat the first boards as bring-up units and run
> the self-test (§3.4) before trusting one with servos you care about.

**Contents**

1. [Hardware manual](#1-hardware-manual) — board tour, connectors, power, wiring
2. [First power-on](#2-first-power-on) — the safe sequence for a new board
3. [Tooling manual](#3-tooling-manual) — `servoctl`, `selftest`, the Python library
4. [Embedded manual](#4-embedded-manual) — protocol, behaviour, build, flash
5. [Troubleshooting](#5-troubleshooting)
6. [Quick reference card](#6-quick-reference-card)

---

## 1. Hardware manual

### 1.1 What it is

A 12-channel hobby-servo controller driven over USB-C. The PC sends text
commands to a virtual serial port; the board turns them into standard RC servo
pulses. It also measures the **current and voltage of the servo supply** and
can shut every servo off on overcurrent or a flat battery.

| | |
|---|---|
| MCU | WCH CH32V203C8T6 (32-bit RISC-V, 96 MHz) |
| Host interface | USB-C, appears as a serial (CDC) port — no driver on Windows 10+, Linux, macOS |
| Servo outputs | 12 channels, 3.3 V logic pulses, 50 Hz, 0.5 µs resolution; 500–2500 µs by default, settable per channel from 64 to 4080 µs |
| Analog inputs | channels 0–7 only, **0–3.3 V** |
| Servo supply | separate, via screw terminal J2 — see §1.4 for limits |
| Rail monitoring | servo-rail current (0–13.2 A range) and voltage (0–13.2 V range) |
| Indicator | one red LED (heartbeat) |
| Size | 66 × 38 mm, 4 × M3 mounting holes |

### 1.2 Board tour

Hold the board **component side up, USB-C connector on the right.**

```
                                  TOP EDGE
 ┌─────────────────────────────────────────────────────────────────────┐
 │ (H1)          J9  SDI debug (not fitted)      JP2                (H2) │
 │               3V3 DIO CLK RST GND             BOOT0      USB SERVO   │
 │                ○   ○   ○   ○   ○               ▯          CONTROLLER │
 │  C15                                           ▯          v1         │
 │ (470µF)                 Y1        ┌──────┐                        ┌──┤
 │                                   │  U1  │                   J1   │  │ USB-C
 │  J2 SERVO PWR  ─                  │CH32V │                        │  │
 │   [−]  GND                        └──────┘                        └──┤
 │   [+]  V+   +                                                        │
 │                        0  1  2  3  4  5  6  7  8  9  10 11           │
 │  JP1 ▯▯   VSRV    SIG  ○  ○  ○  ○  ○  ○  ○  ○  ○  ○  ○  ○   SIG      │
 │           LOGIC   V+   ○  ○  ○  ○  ○  ○  ○  ○  ○  ○  ○  ○   V+   D3  │
 │ (H3)              GND  ○  ○  ○  ○  ○  ○  ○  ○  ○  ○  ○  ○   GND  LED │
 └─────────────────────────────────────────────────────────────────(H4)┘
                                 BOTTOM EDGE
```

| Ref | What | Where |
|---|---|---|
| **J1** | USB-C — data and logic power | right edge |
| **J2** | Servo power in, 5.08 mm screw terminal | left edge. **Lower screw = `+`**, upper screw = `−` (GND). Silkscreen `+` / `−` marks each side. |
| **J3–J8** | 12 servo ports, 3 rows × 12 | along the bottom. Channel numbers printed above; row names `SIG` / `V+` / `GND` printed at the right end |
| **JP1** | "VSRV / LOGIC" jumper — power the logic from the servo supply | left edge, below J2 |
| **JP2** | BOOT0 jumper — enter the USB bootloader | top edge, centre |
| **J9** | WCH SDI debug header, **not fitted** | top edge, left of centre |
| **D3** | red status LED | bottom right |
| **H1–H4** | M3 mounting holes, 3.2 mm | corners; centres 45.5 × 29 mm apart |

### 1.3 Servo ports (J3–J8)

Each **column** is one channel. Each column has three pins, from top to bottom:

| Row | Pin | Standard servo wire colour |
|---|---|---|
| top (toward the chip) | **SIG** — the pulse | orange / yellow / white |
| middle | **V+** — servo power, straight from J2 | red |
| bottom (toward the board edge) | **GND** | brown / black |

This is the standard Futaba/JR/Hitec order, so a normal 3-wire servo plug goes on
**with the dark wire toward the bottom edge.** Channel 0 is on the left,
channel 11 on the right.

| Ch | MCU pin | Timer | Analog in? |
|---|---|---|---|
| 0 | PA0 | TIM2_CH1 | ✅ |
| 1 | PA1 | TIM2_CH2 | ✅ |
| 2 | PA2 | TIM2_CH3 | ✅ |
| 3 | PA3 | TIM2_CH4 | ✅ |
| 4 | PA6 | TIM3_CH1 | ✅ |
| 5 | PA7 | TIM3_CH2 | ✅ |
| 6 | PB0 | TIM3_CH3 | ✅ |
| 7 | PB1 | TIM3_CH4 | ✅ |
| 8 | PA8 | TIM1_CH1 | — |
| 9 | PA9 | TIM1_CH2 | — |
| 10 | PA10 | TIM1_CH3 | — |
| 11 | PB8 | TIM4_CH3 | — |

Every SIG pin has a **220 Ω series resistor** between the MCU and the header.

**What can be connected to a servo port:**

| Device | Works? | Notes |
|---|---|---|
| Standard analog / digital hobby servo | ✅ | the intended use |
| Continuous-rotation servo | ✅ | 1500 µs ≈ stop; trim per servo |
| ESC (brushed or brushless) | ✅ | 1000–2000 µs is inside the range. **See the BEC warning below.** |
| Servos that need 5 V logic pulses | ⚠️ | most accept 3.3 V; a few older ones do not |
| Potentiometer / 0–3.3 V sensor on ch 0–7 | ⚠️ | read with the `A` command — see limits in §1.7 |
| 5 V sensor output | ❌ | inputs are 0–3.3 V only; 5 V will inject current into the MCU |
| LEDs, relays, logic outputs | ❌ | firmware has no digital-output mode yet |
| UART / TTL serial device | ❌ | firmware does not support it yet |

> ⚠️ **ESC BEC warning.** Many ESCs *output* 5–6 V on their red wire (a "BEC").
> Plugged into this board, that output lands on the shared **V+ rail** together
> with whatever is on J2. Two supplies fighting on one rail can damage both.
> If you plug in an ESC with a BEC while J2 also has a supply, **pull the red
> wire out of the ESC's plug** (or use only the ESC's BEC to power the rail and
> leave J2 empty).

### 1.4 Power

The board has **two independent power domains** that share a common ground:

- **Logic** (MCU, USB, LED) — 3.3 V from an on-board regulator, fed from USB-C
  or, if JP1 is fitted, from the servo supply.
- **Servo rail** (V+ row on all 12 ports) — whatever you connect to J2, passed
  straight through. **The board does not regulate servo power.**

```
  USB-C VBUS 5V ──D1──┐
                      ├──► HT7533 3.3V LDO ──► MCU, LED
  J2 + ──[5mΩ shunt]──┼──► V+ on all 12 servo ports
                      │
                      └── JP1 ──D2──┘   (JP1 fitted = logic can run from J2)
  J2 − ─────────────────── GND (common to everything)
```

**Servo supply limits**

| | Value |
|---|---|
| Supply voltage | **choose it to match your servos** — see below |
| Absolute maximum on J2 | **16 V** (set by the 16 V bulk capacitors) |
| Maximum for correct voltage reading | 13.2 V |
| Continuous current, whole board | **≈ 5 A total** across all channels |
| Terminal wire size | 12–22 AWG; use **18 AWG or thicker** for loads over 2 A |

> The screw terminal itself is rated 24 A, but the copper behind it is not.
> Budget about **5 A total** — a dozen small servos holding position, or three
> or four working hard.

**Choosing the servo supply.** Since the voltage on J2 goes straight to every
servo, pick it for your servos, not for the board:

| Supply | Suits |
|---|---|
| 4.8 V (4× NiMH) | standard servos |
| 5–6 V BEC / bench supply | standard servos — the most common choice |
| 7.4 V (2S LiPo), 8.4 V charged | **HV (high-voltage) servos only** |
| 11.1 V (3S LiPo) | almost no servos — only if yours are rated for it |

> ⛔ **No reverse-polarity protection.** Connecting the servo supply backwards
> will destroy the current-sense chip (U4) and can make the two large capacitors
> vent. Check `+` and `−` **before** tightening the screws, every time. Wire the
> supply with the power switched off.

> **Battery protection is automatic for LiPo packs.** About 100 ms after a servo
> supply appears, the firmware recognises a **2S or 3S LiPo** by its voltage and
> sets a 3.2 V/cell cutoff. Any other supply (5 V / 6 V BEC, NiMH, bench supply)
> gets **no** undervoltage cutoff unless you set one. Details and overrides are in
> §4.4.

### 1.5 The two jumpers

Both are 2-pin headers closed with a standard 2.54 mm shunt cap. The board ships
**with both open**; the caps come loose in the bag.

**JP1 — "VSRV / LOGIC": run the logic from the servo supply**

| JP1 | Logic powered from | Use when |
|---|---|---|
| **open** (default) | USB only | normal use with a PC. Recommended. |
| **fitted** | USB **or** servo supply, whichever is higher | the USB port's power is weak (long cable, unpowered hub), or you want the board to stay alive with USB unplugged |

Diodes on both paths stop either supply back-feeding the other, so it is safe
to have USB and J2 connected with JP1 fitted.

> **Standalone operation:** with JP1 fitted and the servo supply on, the board
> runs a stored script with no PC at all — upload it with autorun (§3.1c) and it
> starts at every power-up. Without a stored autorun script, every channel stays
> off after power-up until a host sends commands.

**JP2 — BOOT0: firmware update mode**

| JP2 | At power-up the board runs |
|---|---|
| **open** (normal) | the servo-controller firmware |
| **fitted** | WCH's ROM bootloader — first flash of a blank chip, or recovery (§4.7) |

Routine firmware updates **do not use JP2**: `flash.py` reboots the board into
its own USB bootloader over the serial port (§4.7). JP2 is for a blank chip, or
for recovering a board whose application no longer answers on USB. It is only
read **at power-up** — fit it, *then* plug in USB, and remove it afterwards.

### 1.6 USB-C

- Any USB-C cable to a USB-A or USB-C port works. The board is a USB 2.0
  full-speed device and draws about 15 mA (estimated, not yet measured).
- Either plug orientation works.
- Data lines and the CC pins are ESD-protected.
- USB **never** powers the servos. With only USB connected, the board runs and
  answers commands, but nothing on the V+ row has power.

### 1.7 Electrical limits of the signal pins

| | |
|---|---|
| Output high / low | 3.3 V / 0 V through 220 Ω |
| Analog input range (ch 0–7) | **0 to 3.3 V** — never exceed |
| Analog reading | 12-bit, 0–4095 counts over 0–3.3 V |
| 5 V tolerance | **not guaranteed** — do not feed 5 V into a SIG pin |

> ⚠️ **Analog inputs are shared with outputs.** A channel that is not driving a
> servo is held **low** by the MCU, and is switched to input only for the ~20 µs
> of each reading. So a potentiometer (1–10 kΩ) wired as a divider between 3.3 V
> and GND reads correctly, but a sensor with a strong output will be fighting the
> pin between readings. Keep analog sources high-impedance, and **never** read a
> channel that has a servo plugged in and enabled — the reading briefly
> interrupts its pulse.

> ⛔ **Where to get 3.3 V for a potentiometer.** The servo header has **no
> 3.3 V pin** — its middle row is the servo supply (5–13 V). A pot wired between
> the header's V+ and GND puts up to the full servo voltage on the ADC pin.
> Take 3.3 V and GND from **J9 pin 1 (3V3) and pin 5 (GND)** instead — solder a
> 1×5 2.54 mm header there (§1.8). Wire: pot ends → J9.1 and J9.5, wiper → the
> channel's **SIG** pin (top row).

> ⚠️ **A servo plug reversed by one row** puts V+ on the SIG pin. The 220 Ω
> resistor limits the damage, but at 6–8 V it still pushes 10–20 mA into the MCU
> pin. Check plug orientation before powering J2.

### 1.8 J9 — debug header (developers only)

Not fitted. Solder a 1×5 2.54 mm header if you need live debugging.

| Pin | Signal |
|---|---|
| 1 | 3V3 |
| 2 | SDI_DIO (PA13) |
| 3 | SDI_CLK (PA14) |
| 4 | NRST |
| 5 | GND |

> ⛔ This is **WCH's 2-wire SDI protocol, not ARM SWD.** An ST-Link, J-Link or
> other ARM probe **will not work.** Use a **WCH-LinkE**. You do not need J9 at
> all to update the firmware — that goes over USB (§4.7).

### 1.9 Status LED (D3)

| LED | Meaning |
|---|---|
| one short blip per second | running, idle, servo supply present on J2 |
| **two** short blips per second | running, idle, **no servo supply** on J2 |
| **steady on** | a **stored script is running** (§3.1c) |
| fast even flashing, 10 Hz | **fault latched** (overcurrent or undervoltage): every channel was dropped. Clear it with `C`, `servoctl.py clear` or the GUI's **Clear faults** |
| even flashing, 5 Hz | in the USB **bootloader** (after `BOOT`, or an interrupted flash) — run `tools/flash.py` |
| off | no power, or the ROM bootloader (JP2 fitted) |
| stuck on with no script running, or stuck off while powered | firmware has hung — unplug and re-plug |

Only one pattern shows at a time; a fault outranks a running script, which
outranks the idle patterns.

---

## 2. First power-on

Follow this order on a new board. It is arranged so that a wiring or assembly
fault shows up **before** anything moves.

1. **Inspect.** Look for solder bridges on U1 (the square 48-pin chip) and on the
   USB-C connector. Check that JP1 and JP2 are **open**.
2. **USB only.** Plug in USB-C, no servo supply, no servos. The LED should start
   blinking within a second.
3. **Find the port.**
   ```sh
   cd tools
   pip install -r requirements.txt
   python servoctl.py ports
   ```
4. **Safe self-test** — nothing moves:
   ```sh
   python selftest.py
   ```
   It must end with `RESULT: PASS`. The rail-voltage check is skipped because
   there is no servo supply yet.
5. **Connect the servo supply** — **power off**, polarity checked twice — to J2,
   with **no servos plugged in**. Switch it on.
6. **Check the protection limits** the board picked for your supply (§4.4):
   ```sh
   python servoctl.py limits
   ```
   A LiPo should show a 6400 mV (2S) or 9600 mV (3S) undervoltage limit marked
   `(auto)`; any other supply shows `undervoltage off`. Set your own with
   `limits <mA> <mV>` if you want one.
7. **Zero the current sensor** with nothing drawing current:
   ```sh
   python servoctl.py tare
   ```
8. **Check the rail:**
   ```sh
   python servoctl.py sense
   ```
   Voltage should match your supply, current should be near 0, no faults.
9. **One servo on channel 0**, then:
   ```sh
   python servoctl.py sweep 0 --park
   ```
10. **Full acceptance test**, with one servo on each channel you plan to use:
    ```sh
    python selftest.py --move --rail
    ```

---

## 3. Tooling manual

All host tools live in `tools/`, are plain Python 3, and depend only on
`pyserial`.

```sh
cd tools
pip install -r requirements.txt
```

### 3.1 Finding the board

The board shows up as a serial port:

| OS | Port name |
|---|---|
| Windows 10/11 | `COMn` (Device Manager → Ports) — built-in driver, nothing to install |
| Linux | `/dev/ttyACM0`, `/dev/ttyACM1`, … |
| macOS | `/dev/cu.usbmodem…` |

`servoctl.py ports` lists every serial port and marks the candidates. The tools
auto-detect the board by its USB ID (`1A86:FE0C`) **and** a version handshake,
because that ID is WCH's generic one and other WCH gadgets share it. To skip
auto-detection, pass `--port`:

```sh
python servoctl.py --port COM5 info
python servoctl.py --port /dev/ttyACM0 info
```

On Linux, add yourself to the `dialout` group once (`sudo usermod -aG dialout
$USER`, then log out and in) so you do not need `sudo`.

### 3.1b `servogui.py` — desktop GUI

```sh
python tools/servogui.py              # auto-detect;  --port COM52  /  --mock
```

**Channels (left).** Tick **on** to drive a channel, drag the slider to move it.
**Enable all** turns on every servo channel at its slider position. **STOP**,
the **space bar** or **Esc** is the panic stop (`X`). The **⚙** button per row sets:

| Setting | What it does |
|---|---|
| Name | shown in the row, poses and CSV headers |
| Min / Max | travel limits in µs — the slider cannot go past them. Set them just inside the servo's mechanical end stops: a servo pushing against a stop stalls and draws its full stall current (~2.3 A for an MG995) |
| Speed | µs per second, 0 = instant. Ramping is done by the GUI in 50 ms steps, so moves are smooth and current spikes smaller |
| Mode | **servo**, or **input** (channels 0–7): the row becomes a live analog bar, 0–4095 counts / 0–3.3 V, and the channel is never driven |

**Rail tab.** Live current and voltage, a 10 s current graph with peak hold,
**Clear faults**, **Tare**, the **Protection** limits (`P`; offered up to
7500 mA — the board's continuous budget is ~5 A (§1.4), its narrowest rail trace
7.8 A; they reset to 5000 mA at power-up), and **Record CSV**, which logs
current, voltage, faults, every channel's pulse width and the analog inputs every
50 ms.

**Poses & sequence tab.** **Save current** stores every channel that is on as a
named pose; **Go to** (or double-click) moves back to it. A sequence is a list of
poses, each with a **move** time (how long to get there; 0 = each channel's own
speed) and a **hold** time. **Play** runs it **repeat** times (0 = forever; the same count goes into a board script uploaded with **Upload sequence**); STOP
or a fault ends it. This is the replacement for the Maestro's sequencer.

**Script tab.** A text editor for board scripts (§3.1c) with syntax colouring:
**Check (F5)** compiles and shows the instruction listing, or marks the bad line
in red; **Upload** / **Upload + Run** store it on the board (with **autorun at
power-up** if ticked); **Run** / **Stop** control the stored script; **Read from
board** turns the stored script back into editable text; **From sequence**
starts from the Poses tab's sequence; **Help** lists every statement.
New / Open / Save / Save as work on `.txt` files (Ctrl+S saves). The editor
text is kept between runs even if unsaved. Space typed in the editor does not
stop the servos; **Esc** always does.

**Firmware tab.** **Flash firmware** runs `flash.py` on the chosen application
image and shows its output — no jumper — then reconnects (§4.7). Build the image
first with `make`.

Settings, poses and the sequence are saved in `tools/servogui.json`. A latched
fault drops every channel; the GUI shows it and **Clear faults** re-arms. Uses
only tkinter (bundled with Python) and pyserial.

### 3.1c `servoscript.py` — scripts that run on the board, without a PC

The firmware has a small script engine. A script is compiled on the PC, stored
in the last 4 KB of flash, and run by the board itself — it keeps going with
the USB cable unplugged, and with **autorun** it starts at every power-up.
Up to 254 instructions; timing comes from the servo timer, to 1 ms.

```sh
python tools/servoscript.py check  tools/examples/wave.txt        # compile, list
python tools/servoscript.py upload tools/examples/wave.txt --run  # store + start
python tools/servoscript.py upload wave.txt --autorun             # start at power-up
python tools/servoscript.py status | run | stop | dump
python tools/servoscript.py from-gui -o seq.txt                   # GUI sequence -> script
```

| Statement | Meaning |
|---|---|
| `name:` | a label, target of `loop` / `jump` |
| `move <ch> <us> [<time>]` | ramp to `<us>` over `<time>` (default 0 = jump). Does **not** wait: moves on *different* channels run together. A second move of the *same* channel first waits for the one before (the compiler adds a `sync` and says so). A channel that is off ramps from where it was last driven (1500 µs if never), over the full time |
| `pose <name> [<time>]` | move every channel of a GUI pose |
| `sync` | wait until every move has **arrived** (`wait` just pauses; `sync` waits for the servos) |
| `wait <time>` / `sleep <time>` | pause for a fixed time. **A plain number is milliseconds** (`2` = 2 ms): write `2s`, `1.5s` or `2000` |
| `off <ch>` / `off all` | stop driving (the servo goes limp). A channel still moving finishes its move first |
| `loop <label> [<n>]` | go back to `<label>`; the block runs `n` times in total, forever without `n` |
| `jump <label>` | go to `<label>` |
| `waitin <ch> > <v>` / `< <v>` | wait for analog input `ch` (0–7) to go above / below `v` (0–4095) |
| `ifin <ch> > <v>` / `< <v>` | run the next statement only if the condition holds |
| `end` | stop; moves still running finish first, then servos hold (same at the end of the script) |

Channels are numbers or the names set in the GUI, and positions are clamped to
each channel's GUI limits, with a warning. A latched rail fault or `X` stops a
running script. The LED blinks four times faster while a script runs. The GUI's
**Poses & sequence** tab can upload its sequence (or a script file) to the board
and run or stop it; the MCP server can too. Pose names used in scripts cannot
contain spaces (the GUI now saves them with underscores).

**Standalone:** fit **JP1** so the logic runs from the servo supply, upload with
`--autorun`, unplug USB — the script starts whenever the servo supply comes on.

### 3.1d `servo_mcp.py` — AI agent control (MCP)

An MCP server that lets an AI agent drive the board through typed tools
instead of shell commands. Install the dependency and register it with your MCP
client as a stdio server:

```sh
pip install "mcp>=2"
python tools/servo_mcp.py        # the command to register; optional: --port COM52
```

Tools: `board_status`, `move_servos` (ramped, waits for arrival, reports peak
current), `disable_servos`, `stop_all`, `list_poses`, `go_to_pose`,
`play_sequence`, `read_analog`, `measure_current`, `set_protection`,
`clear_faults`, `upload_script`, `script_status`, `run_script`, `stop_script`,
`release_port`.

Guardrails: positions are clamped to the GUI's per-channel limits, input-mode
channels are never driven, the overcurrent limit is capped at 7500 mA, a fault
stops everything and is reported, and everything stops when the server exits.
The port is opened on demand and released after 15 s idle (servos keep holding),
so the GUI and `flash.py` can use the board in between — but only one program
can hold the port at a time.

### 3.2 `servoctl.py` — command-line control

General form: `python servoctl.py [--port P] [--mock] [--timeout S] <command> …`

| Command | What it does |
|---|---|
| `ports` | list serial ports, mark candidates |
| `info` | firmware version, every channel's state, rail status |
| `set <ch> <µs>` | set a channel's pulse width, within its travel limits (500–2500 by default). **Also switches the channel on.** |
| `get <ch>` | read a channel's pulse width (`0` = off) |
| `enable <ch>` / `disable <ch>` | switch a channel's output on / off, keeping its last width |
| `center` | set **every** channel to 1500 µs — switches all 12 on |
| `stop` | panic stop: every channel off |
| `sweep <ch>` | walk a channel back and forth. Options: `--low 1000 --high 2000 --step 10 --delay 0.02 --cycles 1 --park` |
| `analog <ch>` | read channel 0–7 as an analog input (0–4095) |
| `sense` | one reading: faults, current (mA), voltage (mV), rail present |
| `monitor` | repeat `sense` until Ctrl-C. Options: `--interval 0.5`, `--stop-on-fault` |
| `tare` | zero the current sensor — switches all channels off first |
| `limits` | show the protection limits in force, and whether undervoltage is automatic |
| `limits <mA> <mV>` | set overcurrent and undervoltage trip levels; `0` disables either. Switches undervoltage to manual until the next power-up |
| `clear` | clear latched faults |
| `boot` | restart into the USB bootloader and stay there (`flash.py` does this itself, §4.7) |
| `raw "<line>"` | send one literal protocol line, print the reply |
| `repl` | interactive session — type protocol commands directly |

Examples:

```sh
python servoctl.py info
python servoctl.py set 3 1200            # channel 3 to 1200 µs
python servoctl.py sweep 0 --low 900 --high 2100 --cycles 3 --park
python servoctl.py monitor --interval 0.2 --stop-on-fault
python servoctl.py raw "S 0 1500"
python servoctl.py stop
```

> **Note on `set`.** There is no "set target but stay off" — setting a pulse
> width is what energises the servo. Use `enable`/`disable` to toggle a channel
> without losing its position.

### 3.3 Any serial terminal works too

The protocol is plain text, so you can type at the board from any terminal set
to the board's port (baud rate is ignored):

```sh
python -m serial.tools.miniterm COM5 115200 --eol LF
```

PuTTY, Tera Term, `screen /dev/ttyACM0` and the Arduino serial monitor all work;
set the line ending to **LF** (or CR+LF). See §4.3 for the commands.

### 3.4 `selftest.py` — acceptance test

```sh
python selftest.py                  # safe checks only, nothing moves
python selftest.py --rail           # a servo supply is connected; check its voltage too
python selftest.py --move --rail    # also move each servo in turn
```

| Option | Meaning |
|---|---|
| `--port P` | serial port; default auto-detect |
| `--move` | run the tests that move servos, one channel at a time |
| `--rail` | a servo supply is on J2; check its voltage is plausible (5–9 V) |
| `--dwell S` | seconds to hold each test position (default 0.4) |
| `--mock` | run against the emulator, no hardware |

What it checks, in order: identity → all channels off at boot → error handling →
rail sensing → analog inputs → movement (`--move`) → current sensing sees a load
→ panic stop. It **always** finishes with a panic stop, even on failure or
Ctrl-C, and exits **0** on pass, **1** on any failure — usable as a production
go/no-go test.

> The `--rail` voltage check expects **5–9 V**. A 3S pack (11.1 V) will fail it
> on purpose; drop `--rail` if that is your supply.

> Firmware older than **0.3** fails the check *"no faults latched at power-on"*
> on any supply below 6 V, because its fixed 6.0 V undervoltage default trips
> first — and it fails *"L reports the limits in force"*, because it has no `L`
> command. Flash 0.3 or later (§4.7).

### 3.5 `usc.py` — Python library

For your own scripts. One class, one method per command:

```python
import sys
sys.path.insert(0, "path/to/tools")
from usc import ServoController, DeviceError

with ServoController.open() as dev:          # or .open("COM5"), or .mock()
    print(dev.version())
    dev.set_limits(5000, 5000)                # suit a 6 V supply
    dev.clear_faults()
    dev.tare()

    dev.set_us(0, 1500)                       # also enables channel 0
    dev.set_us(1, 1000)
    print(dev.positions())                    # [1500, 1000, 0, 0, ...]

    st = dev.status()                         # Status(faults, current_ma, voltage_mv, rail_present)
    print(st, st.ok, st.fault_names)

    try:
        dev.set_us(2, 1500)
    except DeviceError as e:
        print("refused:", e.reason)           # e.g. 'fault'

    dev.stop_all()
```

| Method | Protocol | Returns |
|---|---|---|
| `version()` | `V` | str |
| `travel(ch)` / `set_travel(ch, min, max)` | `R` | (min, max) / — |
| `save_settings()` / `reset_settings()` | `W` / `W D` | — |
| `set_us(ch, us)` | `S` | — |
| `get_us(ch)` | `G` | int, 0 = off |
| `enable(ch, on=True)` / `disable(ch)` | `E` | — |
| `stop_all()` | `X` | — |
| `read_analog(ch)` | `A` | int 0–4095 |
| `current_ma()` / `voltage_mv()` | `I` / `U` | int |
| `read_raw(0 \| 1)` | `N` | int counts |
| `tare()` | `Z` | int offset |
| `status()` | `F` | `Status` |
| `clear_faults()` | `C` | — |
| `limits()` | `L` | `Limits(overcurrent_ma, undervoltage_mv, auto)` |
| `set_limits(mA, mV)` | `P` | — |
| `reboot_to_bootloader()` | `BOOT` | str |
| `command("…")` | anything | raw reply str |
| `center_all()`, `positions()`, `sweep(...)`, `watch()` | built on the above | |

Errors: `DeviceError` (board said `ERR …`; `.reason` holds the word),
`ProtocolError` (unexpected or missing reply), `NotFoundError` (no board found).
All derive from `UscError`.

### 3.6 Working without hardware

Every tool accepts `--mock`, and the library has `ServoController.mock()`. This
runs an in-process emulator of the firmware's command parser, so you can write
and test host code before a board exists. It simulates 12 mA idle plus 120 mA per
enabled channel on a 7.94 V rail. It has no timers, no USB and no ADC noise — it
proves your code, not the board.

### 3.7 Existing Maestro software

From firmware **0.4** the board speaks the Maestro *serial* protocols (§4.3b),
so community libraries work unchanged: point them at the board's COM port.
Tested with [FRC4564/Maestro](https://github.com/FRC4564/Maestro):

```python
import maestro
m = maestro.Controller("COM52")      # /dev/ttyACM0 on Linux
m.setSpeed(0, 20)                    # 0.25 us per 10 ms units: 500 us/s
m.setTarget(0, 8000)                 # quarter-microseconds: 2000 us
```

Pololu's Maestro Control Center and `UscCmd` **cannot** talk to this board —
they require Pololu's own USB vendor ID. Use `servogui.py` instead. That
library's `getMovingState()` always returns True on Python 3 (a bytes/str
comparison bug in the library); `isMoving(ch)` works.

---

## 4. Embedded manual

### 4.1 Firmware behaviour at a glance

| | |
|---|---|
| Version string | `USBServoController 0.4 12ch CH32V203 isense maestro` |
| Clock | 8 MHz crystal × 12 = 96 MHz; USB 48 MHz |
| Servo frame | 20 ms (50 Hz), all channels |
| Pulse range | per channel, 500–2500 µs by default, settable 64–4080 µs (`R`); neutral 1500 |
| Resolution | 0.5 µs (Maestro targets use it; ASCII `S` takes whole µs) |
| At power-up | **every channel off (output held low)** — nothing moves until commanded |
| Settings storage | travel limits and Maestro start-up speed/acceleration, saved with `W` (survive power-off and firmware updates); protection limits, zero-offset and positions are not stored |
| Rail monitoring | every ~10 ms, starting 250 ms after power-up |
| Protection trip | 5 consecutive out-of-limit readings (~50 ms) → all channels off, fault latched |

### 4.2 Protocol basics

- Text lines, terminated with `\n` (`\r` is ignored). Max 63 characters.
- Command letters are case-insensitive; fields separated by spaces.
- **Exactly one reply per command line**, ending `\r\n`:
  - `OK` — done
  - `OK <value>` — done, here is the value
  - `ERR <reason>` — refused
- Send the next command after reading the reply.

### 4.3 Command reference

**Servo control**

| Command | Reply | Meaning |
|---|---|---|
| `S <ch> <us>` | `OK` | set pulse width within the channel's travel limits **and switch the channel on** |
| `G <ch>` | `OK <us>` | pulse width, `0` if the channel is off |
| `E <ch> 1` | `OK` | switch channel on at its last width (1500 if never set) |
| `E <ch> 0` | `OK` | switch channel off (output low, servo goes limp) |
| `X` | `OK` | **panic stop** — every channel off |
| `A <ch>` | `OK <counts>` | read channel 0–7 as analog, 0–4095 over 0–3.3 V |

**Stored script** (`servoscript.py` drives these; format in `User/script.h`)

| Command | Reply | Meaning |
|---|---|---|
| `QC` | `OK` | clear the RAM upload buffer |
| `QA <i> <op> <ch> <a> <b>` | `OK` | put instruction `i` in the buffer (validated) |
| `QS <count> <flags>` | `OK <crc>` | save the buffer to flash; flags bit 0 = autorun |
| `QI` | `OK <valid> <count> <flags> <state> <pc>` | stored script; state 0 idle, 1 running, 2 done, 3 faulted |
| `QG <i>` | `OK <op> <ch> <a> <b>` | read instruction `i` back from flash |
| `QR` / `QX` | `OK` | run from the top / stop (servos hold). `X` and a rail fault also stop it |

**Servo-rail monitoring**

| Command | Reply | Meaning |
|---|---|---|
| `I` | `OK <mA>` | servo-rail current |
| `U` | `OK <mV>` | servo-rail voltage |
| `F` | `OK <faults> <mA> <mV> <present>` | status: fault bits, last current, last voltage, rail present (1/0) |
| `L` | `OK <mA> <mV> <auto>` | limits in force; `auto` = 1 while the undervoltage limit is chosen automatically. `<mV>` is `0` when no cutoff applies |
| `P <mA> <mV>` | `OK` | set overcurrent / undervoltage limits; `0` disables either. Leaves auto mode until the next power-up |
| `C` | `OK` | clear latched faults |
| `Z` | `OK <counts>` | zero the current sensor (switches all channels off first) |
| `N 0` / `N 1` | `OK <counts>` | raw ADC counts: 0 = current, 1 = voltage |

**System**

| Command | Reply | Meaning |
|---|---|---|
| `V` | version string | note: **no** `OK` prefix |
| `R <ch>` | `OK <min> <max>` | the channel's travel limits, µs |
| `R <ch> <min> <max>` | `OK` | set them, 64 ≤ min < max ≤ 4080. Every `S`, script move and Maestro target is clamped to them; a running channel outside is pulled inside at once. In RAM until `W` |
| `W` | `OK` | save travel limits and Maestro speed/acceleration to flash, loaded at every power-up |
| `W D` | `OK` | factory defaults (500–2500 µs, speed/accel 0) in RAM and flash |
| `BOOT` | `OK BOOT` | reset into the board's USB bootloader; the port drops and returns as the bootloader (§4.7). |

**Error reasons**

| `ERR …` | Cause |
|---|---|
| `syntax` | missing or non-numeric argument |
| `channel` | channel number above 11 |
| `range` | pulse outside the channel's travel limits, or `R` limits outside 64–4080 / min ≥ max |
| `fault` | a rail fault is latched — `S` and `E … 1` are refused until `C` |
| `flash` | `W` could not write or verify the settings page |
| `noanalog` | `A` on channel 8–11 (no analog path) |
| `index` | `N` with an index other than 0 or 1 |
| `adc` | ADC conversion timed out |
| `unknown` | unrecognised command letter |
| `toolong` | line longer than 63 characters |

Example session:

```
> V
USBServoController 0.4 12ch CH32V203 isense maestro
> L
OK 5000 0 1            <- 6 V BEC detected: no undervoltage cutoff, auto mode
> S 0 1500
OK
> F
OK 0 142 5980 1        <- no faults, 142 mA, 5.98 V, rail present
> X
OK
```

### 4.3b Maestro serial protocols (firmware 0.4+)

Any byte of `0x80` or above starts a Pololu Maestro command; printable ASCII
goes to the text protocol above. Both can be used on the same port. Three
framings, as on a Maestro:

| Framing | Example: channel 0 to 1500 µs |
|---|---|
| Compact | `84 00 70 2E` |
| Pololu, device number 12 | `AA 0C 04 00 70 2E` |
| Mini SSC | `FF 00 7F` (0–254, 127 = 1500 µs, ±476 µs) |

Targets are in **quarter-microseconds** (1500 µs = 6000) sent as two 7-bit
bytes, low first. A target of `0` switches the channel off.

| Compact | Command | Data | Reply |
|---|---|---|---|
| `84` | Set Target | ch, target | — |
| `9F` | Set Multiple Targets | count, first ch, count × target | — |
| `87` | Set Speed | ch, speed in 0.25 µs per 10 ms (0 = unlimited) | — |
| `89` | Set Acceleration | ch, 0–255 in 0.25 µs per 10 ms per 80 ms (0 = unlimited) | — |
| `90` | Get Position | ch | 2 bytes, quarter-µs, low first; 0 = off |
| `93` | Get Moving State | — | 1 byte, 1 while any channel is ramping |
| `A1` | Get Errors | — | 2 bytes, then cleared; `0x0010` = protocol error |
| `A2` | Go Home | — | — (all channels off) |
| `A4` | Stop Script | — | — (stops the board's stored script) |
| `AE` | Get Script Status | — | 1 byte, 0 = running, 1 = stopped |

`8A` Set PWM and `A7`/`A8` Restart Script are accepted and ignored.

How it differs from a Maestro:

- Targets clamp to each channel's **travel limits** (`R`, 500–2500 µs by
  default), as a Maestro clamps to its channel min/max. Output is in 0.5 µs
  steps; Get Position returns the exact target once a move has arrived.
- Speed and acceleration set over the protocol last until power-off. To make
  them the power-up values, send `W` afterwards (a Maestro keeps these in
  Control Center settings).
- An off channel jumps straight to its first target: its position is unknown.
- A latched rail fault (§4.4) blocks Maestro targets like it blocks `S`.
- An ASCII `S`, `E` or `X` on a channel ends any Maestro ramp on it.

### 4.4 Protection: overcurrent and undervoltage

The firmware watches the servo rail and, on a fault, **switches every channel
off** (which makes the servos go limp and stops them drawing current) and
**latches** the fault until you clear it.

| Fault | Bit in `F` | Default limit | Trips when |
|---|---|---|---|
| Overcurrent | `1` | 5000 mA | current above limit for ~50 ms |
| Undervoltage | `2` | **automatic** — see below | voltage below limit for ~50 ms, *and* a supply is present (> 2 V) |

**Automatic undervoltage (the default).** About 100 ms after a servo supply
appears — once the voltage has settled — the firmware picks a cutoff from it:

| Supply voltage when connected | Treated as | Undervoltage cutoff |
|---|---|---|
| below 6.6 V | 5 V / 6 V BEC, 4–5 cell NiMH, bench supply | **none** |
| 6.6–8.8 V | 2S LiPo | **6.4 V** (3.2 V/cell) |
| 8.8–9.9 V | ambiguous (e.g. 7-cell NiMH) | **none** |
| 9.9 V and above | 3S LiPo | **9.6 V** (3.2 V/cell) |

Unplugging the servo supply resets this, so swapping a pack for a different one
is re-detected. Check what was chosen with `L` (`servoctl.py limits`).

Automatic mode cannot tell a **2S pack that is already below 6.6 V** from a
bench supply, so it gives that pack no cutoff — charge packs before use. It also
treats a regulated supply in the 6.6–8.8 V range (for example a 7.4 V HV-servo
BEC) as a 2S pack, which is harmless: a cutoff at 6.4 V only acts if that supply
collapses.

**Manual limits.** `P <mA> <mV>` overrides both limits and switches undervoltage
to manual until the next power-up. Suggestions:

| Servo supply | `P` |
|---|---|
| 4× NiMH, 4.8 V | `P 5000 4200` |
| 5 V BEC / bench supply | `P 5000 4400` |
| 6 V BEC | `P 5000 5000` |
| 2S LiPo (7.4 V) | `P 5000 6400` — same as automatic |
| 3S LiPo (11.1 V) | `P 5000 9600` — same as automatic |

Limits are not stored: after a power-cycle the board is back in automatic mode
with a 5000 mA overcurrent limit. `P 0 0` disables both — only do that on a
bench supply with its own current limit. After changing limits, send `C` to
clear any fault the old limit caused.

**Recovering from a trip:** find and fix the cause (a stalled servo, a flat
battery) → `C` → re-enable channels with `S` or `E`.

> ⚠️ **What undervoltage protection does not do.** It stops the servos, but the
> board itself keeps drawing ~15 mA from the battery if JP1 is fitted. It does
> not disconnect the battery. **Unplug the battery** when the board trips on a
> flat pack, or it will keep discharging.

> The current reading is for the **whole rail**, not per channel. A trip tells
> you *something* drew too much, not which servo.

### 4.5 Accuracy of the readings

- **Current:** about 3.2 mA per count, 0–13.2 A range. Out of the box there is a
  zero offset of a few tens of mA — run `Z` (or `servoctl.py tare`) with nothing
  drawing current after each power-up.
- **Voltage:** about 3.2 mV per count, 0–13.2 V range. Saturates above 13.2 V.
- Both are referenced to the board's 3.3 V regulator (±2–3 %). Good for
  thresholds; not a calibrated meter.

### 4.6 Building the firmware

Source is in `embedded-ch32/`.

**You need:**
- **xPack `riscv-none-elf-gcc`** (tested with 15.2.0) —
  <https://xpack-dev-tools.github.io/riscv-none-elf-gcc-xpack/>
- **GNU make**
- Python 3 (only for the pin-map check)

**Build:**

```sh
cd embedded-ch32
make GCC_PATH=/path/to/xpack-riscv-none-elf-gcc-15.2.0-1/bin
```

Outputs in `embedded-ch32/build/`: `USBServoController-ch32.elf`, `.hex`, `.bin`.
`make clean` removes them; `make size` prints flash/RAM use (~20 KB of 64 KB
flash, ~3 KB of 20 KB RAM).

> Three toolchain traps are already handled in the Makefile and sources — keep
> them if you change toolchain: `-march=rv32imac_zicsr_zifencei`, `-std=gnu11`,
> and every interrupt handler declared `interrupt("machine")`, **never**
> `"WCH-Interrupt-fast"` (upstream GCC silently ignores it and the build links
> but crashes). README §5 explains each.

**Source layout:**

| File | Role |
|---|---|
| `User/main.c` | init, main loop (1 ms tick): protocol, rail monitor, LED |
| `User/servo.c/.h` | 12-channel PWM on TIM1–TIM4, pulse limits, analog reads |
| `User/sense.c/.h` | ADC, current/voltage conversion, fault detection |
| `User/protocol.c/.h` | command parser, replies, bootloader jump |
| `User/cdc_glue.c` | USB CDC line-coding stub |
| `USBLIB/CONFIG/` | USB descriptors and endpoint handling (WCH library, modified) |
| `HAL/`, `Startup/`, `Ld/` | WCH vendor HAL, startup code, linker script |
| `tools/verify_pinmap_ch32.py` | checks schematic ↔ pin table ↔ firmware agree |
| `test/test_sense.c` | runs the real `sense.c` on the PC against a stubbed ADC |

**Changing things:**

| To change | Edit |
|---|---|
| default travel limits (500–2500 µs) | `SERVO_US_MIN` / `SERVO_US_MAX` in `servo.h`; per channel at runtime with `R` |
| frame rate / resolution | `SERVO_TIMER_HZ`, `SERVO_FRAME_TICKS` in `servo.h` (16-bit timer: ticks ≤ 65535) |
| default protection limits | `SENSE_DEFAULT_LIMIT_MA` / `_MV` in `sense.h` |
| trip speed | `SENSE_TRIP_COUNT`, `SENSE_TASK_PERIOD_MS` in `sense.c` |
| add a command | a new `case` in `execute()` in `protocol.c` — and mirror it in `tools/usc.py` (`ServoController` and `MockDevice`) |

After changing anything in `sense.c`, run its host test — it compiles the real
file for the PC and drives it through 21 supply scenarios (any host C compiler;
`HOSTCC="zig cc"` works too):

```sh
make hosttest HOSTCC=gcc
```

After any schematic change, run the cross-check:

```sh
python embedded-ch32/tools/verify_pinmap_ch32.py   # needs KiCad 10's kicad-cli
```

### 4.7 Flashing the firmware — over USB, no programmer

Flash is split in four: a small **USB bootloader** in the first 12 KB, the
**application** from `0x3000` (48 KB), the **stored script** (2 KB from
`0xF000`) and the **settings** page (`0xF800`)
(`embedded-ch32/User/flash_layout.h`). A firmware update keeps the stored
script and the settings — provided the bootloader on the board was built with
this layout (bootloaders from before the script engine erase up to `0xFFFF`;
update it once with the JP2 + `wchisp` full image below). The
bootloader runs on every reset and starts the application at once, unless the
application asked it to stay (`BOOT` command) or there is no valid application.

**Routine update — no jumper, no replug** (board running, USB plugged in):

```sh
cd embedded-ch32
make flash                       # build, then python ../tools/flash.py
```

or `python tools/flash.py [app.bin] [--port COMx]` directly. It sends `BOOT`,
waits for the bootloader (same USB IDs, same COM port — LED blinks fast, 5 Hz),
erases, writes, verifies by CRC-32 and starts the new application: about 3 s.
Page 0 is written last, so if the flash is interrupted the board simply stays in
the bootloader — run `flash.py` again. Give it the **application** image
`build/USBServoController-ch32.bin`, never the `-full.bin`.

**First flash of a blank chip, or recovery — JP2 + ROM bootloader.** The chip
also has WCH's factory USB bootloader in ROM; it installs the bootloader and
application together. No debugger is needed.

1. Install **`wchisp`**: `cargo install wchisp`, or download a release from
   <https://github.com/ch32-rs/wchisp>. (`chprog` or WCH's WCHISPTool also work.)
2. **Unplug USB.** Fit a shunt cap on **JP2**.
3. **Plug in USB.** The LED stays **off**; the board appears as USB device
   `4348:55e0` (the WCH bootloader), not as a serial port.
4. Check it is seen, then flash:
   ```sh
   wchisp info
   make full                     # in embedded-ch32/
   wchisp flash embedded-ch32/build/USBServoController-ch32-full.bin
   ```
5. **Unplug USB, remove the JP2 cap**, plug back in. The LED blinks, and
   `servoctl.py info` shows the version.

> **Windows:** if `wchisp info` cannot find the device, the bootloader needs the
> WinUSB driver. Run [Zadig](https://zadig.akeo.ie/), select the device with ID
> `4348 55E0`, and install **WinUSB**. This is needed once per PC and does not
> affect the board's normal serial port.

A blank board from the factory has no firmware at all, so steps 2–5 are how
every new board gets programmed the first time. After that, use `flash.py`.

**Debugging (optional):** fit a 1×5 header on J9 and connect a **WCH-LinkE**
(3V3, DIO, CLK, GND; RST optional). Use MounRiver Studio, WCH-LinkUtility or
OpenOCD from WCH's toolchain. An ST-Link will not work (§1.8).

---

## 5. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| LED dark, USB plugged in | JP2 fitted → ROM bootloader; or no firmware yet | remove JP2 and re-plug; or flash (§4.7) |
| LED blinking fast (5 Hz) | in the USB bootloader: after `BOOT`, an interrupted flash, or no valid application | run `python tools/flash.py` |
| No serial port appears | charge-only USB cable; board in bootloader mode | use a data cable; check JP2 |
| `servoctl` finds no board | another program has the port open; Linux permissions | close serial monitors; join `dialout`; try `--port` |
| Linux: port vanishes / garbled for a few seconds after plug-in | ModemManager probing the port | `sudo systemctl stop ModemManager`, or add a udev rule to ignore `1a86:fe0c` |
| Every `S` or `E` gives `ERR fault` | a fault latched — a flat pack, a stalled servo, or (firmware older than 0.3) the old fixed 6 V undervoltage default on a 5–6 V supply | `servoctl.py sense` to see which; fix the cause, or set `limits` (§4.4); `clear`. Update old firmware (§4.7) |
| Servos twitch or reset when several move | supply cannot deliver the current, or wires too thin | stronger supply, thicker J2 wiring, shorter leads |
| Servos do nothing, commands reply `OK` | no servo supply on J2 (USB never powers servos) | `sense` — `rail present` must be 1 |
| One servo does nothing | plug reversed, or wrong row | dark wire toward the bottom edge (§1.3) |
| Trips overcurrent when servos start | inrush from many servos at once | move them in turn, or raise the limit with `P` |
| Current reads a few tens of mA with everything off | sensor zero offset | `tare` after each power-up |
| Voltage reads ~13200 and never changes | supply above 13.2 V — reading saturated | use a lower-voltage supply (§1.4) |
| `A` on ch 8–11 gives `ERR noanalog` | those channels have no analog input | use channels 0–7 |
| `wchisp` cannot see the board | JP2 not fitted at power-up; Windows driver | fit JP2 *before* plugging in; Zadig → WinUSB |
| Board still in bootloader after flashing | JP2 still fitted | remove it, re-plug |

---

## 6. Quick reference card

```
WIRING    servo plug: signal toward the chip, dark wire toward the board edge
          J2: lower screw +, upper screw −   (NO reverse protection)
          servo supply ≤ 16 V absolute, ≤ 13.2 V to read correctly, ≈ 5 A total
          USB never powers the servos

JUMPERS   JP1 fitted  = logic may run from the servo supply
          JP2 fitted  = ROM bootloader at power-up (blank chip / recovery only)

EVERY POWER-UP
          L             check limits: LiPo 2S/3S get an automatic cutoff,
                        other supplies get none unless you set one with P
          Z             zero the current sensor, nothing running

COMMANDS  S ch us   set + enable     G ch    read (0 = off)
          E ch 0|1  off / on         X       PANIC STOP
          A ch      analog, ch 0–7   V       version
          I  U  F   current / voltage / status
          L         show limits      P mA mV set limits (manual)
          C         clear faults     Z       tare

TOOLS     python servoctl.py info | set 0 1500 | sweep 0 --park | monitor | stop
          python selftest.py [--rail] [--move]
          add --mock to run anything without hardware

FLASH     make flash            (or python tools/flash.py) — no jumper
          blank chip: JP2 on → plug → wchisp flash build/…-full.bin
                      → unplug → JP2 off → plug
```
