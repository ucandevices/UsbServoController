#!/usr/bin/env python3
"""
servo_mcp.py -- MCP server that lets an AI agent drive the servo controller.

    python servo_mcp.py                    # stdio transport, auto-detect board
    python servo_mcp.py --port COM52
    python servo_mcp.py --mock             # offline emulator

Register it with any MCP client as a stdio server, e.g. in its JSON config:

    "servo": {"command": "python", "args": ["tools/servo_mcp.py"]}

Guardrails, so an agent cannot do what a person would not:
  * every position is clamped to the channel's min/max from servogui.json
    (set them with the GUI's gear button); input-mode channels are never driven
  * moves ramp (duration, or the channel's configured speed) and block until
    they arrive, watching rail current the whole time; a latched fault stops
    everything and is reported
  * the overcurrent limit can be raised to 7500 mA at most
  * the serial port is opened on demand and released after IDLE_RELEASE_S of
    inactivity (servos keep holding position), so the GUI and flash.py can use
    the board in between; everything stops when the server exits

Settings, poses and sequences are the GUI's (servoconfig.py). Only one program
can hold the port at a time: close the GUI before an agent uses the board.
"""

from __future__ import annotations

import argparse
import atexit
import threading
import time
from typing import Any

import serial
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

import servoscript
import usc
from servoconfig import CONFIG_PATH, Config
from usc import ServoController, UscError

IDLE_RELEASE_S = 15.0
STEP_S = 0.02               # ramp step
MAX_MOVE_S = 30.0
MAX_SEQUENCE_S = 120.0
OC_MAX_MA = 7500
UV_MAX_MV = 13000
IO_ERRORS = (UscError, OSError, serial.SerialException)


class Board:
    """The one serial connection, opened lazily and released when idle."""

    def __init__(self, port: str | None, mock: bool):
        self.port = port
        self.mock = mock
        self.dev: ServoController | None = None
        self.lock = threading.RLock()
        self.last_use = 0.0
        threading.Thread(target=self._idle_watch, daemon=True).start()

    def get(self) -> ServoController:
        with self.lock:
            self.last_use = time.monotonic()
            if self.dev is None:
                try:
                    self.dev = (ServoController.mock() if self.mock
                                else ServoController.open(self.port))
                except IO_ERRORS as exc:
                    raise ToolError(
                        f"cannot open the board: {exc}. If the GUI (servogui.py) is "
                        f"open, close it -- only one program can hold the port.") from exc
            return self.dev

    def release(self) -> None:
        with self.lock:
            if self.dev is not None:
                self.dev.close()
                self.dev = None

    def drop(self) -> None:
        """After an I/O error: forget the connection, reopen on next use."""
        with self.lock:
            try:
                if self.dev is not None:
                    self.dev.close()
            except IO_ERRORS:
                pass
            self.dev = None

    def shutdown(self) -> None:
        with self.lock:
            if self.dev is not None:
                try:
                    self.dev.stop_all()
                except IO_ERRORS:
                    pass
                self.release()

    def _idle_watch(self) -> None:
        while True:
            time.sleep(1.0)
            with self.lock:
                if self.dev is not None and time.monotonic() - self.last_use > IDLE_RELEASE_S:
                    self.release()


board: Board              # set in main()
mcp = MCPServer(
    "servo-controller",
    instructions=(
        "Controls a 12-channel USB hobby-servo controller. Call board_status first "
        "to see channel names, travel limits and the rail. Positions are pulse "
        "widths in microseconds (about 1000-2000 for most servos, 1500 = centre). "
        "Channels can be addressed by number (0-11) or by name. Prefer a "
        "duration_s of 0.5-2 s for moves: fast moves draw ~2 A per large servo. "
        "Call stop_all immediately if anything looks wrong. For motion that must "
        "keep running without the PC, write a script and upload_script it."),
)


# --- helpers ---------------------------------------------------------------

def config() -> Config:
    return Config.load(CONFIG_PATH)        # re-read: the GUI may have changed it


def resolve(cfg: Config, key: Any) -> int:
    if isinstance(key, int) or (isinstance(key, str) and key.strip().isdigit()):
        ch = int(key)
        if not 0 <= ch < usc.CHANNELS:
            raise ToolError(f"channel {ch} out of range 0-{usc.CHANNELS - 1}")
        return ch
    for ch, c in enumerate(cfg.channels):
        if c.name.lower() == str(key).strip().lower():
            return ch
    names = ", ".join(c.name for c in cfg.channels)
    raise ToolError(f"no channel named {key!r}; channels are: {names}")


def call(fn):
    """Run fn(dev); convert I/O errors into a clean tool error."""
    try:
        return fn(board.get())
    except IO_ERRORS as exc:
        board.drop()
        raise ToolError(f"board I/O failed: {exc}") from exc


def ramp(dev: ServoController, cfg: Config, targets: dict[int, int],
         duration_s: float) -> dict:
    """
    Move channels together to targets. duration_s > 0: all arrive together,
    linearly. duration_s == 0: each channel at its configured speed (0 = jump).
    Samples rail current throughout; stops everything on a latched fault.
    """
    start = dev.positions()
    plan = {}
    for ch, tgt in targets.items():
        s = start[ch] or tgt                   # an off channel just jumps
        if duration_s > 0:
            t = duration_s
        else:
            speed = cfg.channels[ch].speed
            t = abs(tgt - s) / speed if speed else 0.0
        plan[ch] = (s, tgt, min(t, MAX_MOVE_S))
    total = max((t for _, _, t in plan.values()), default=0.0)

    peak, t0 = 0, time.monotonic()
    while True:
        el = time.monotonic() - t0
        for ch, (s, tgt, t) in plan.items():
            pos = tgt if t <= 0 or el >= t else s + (tgt - s) * el / t
            dev.set_us(ch, int(round(pos)))
        st = dev.status()
        peak = max(peak, st.current_ma)
        if not st.ok:
            dev.stop_all()
            return {"arrived": False, "fault": st.fault_names, "peak_current_ma": peak,
                    "note": "fault latched: every channel was stopped. Call clear_faults "
                            "before moving again, and move slower or fewer servos."}
        if el >= total:
            break
        time.sleep(STEP_S)
    # settle: a servo lags its command; keep watching the current briefly
    settle_end = time.monotonic() + 0.3
    while time.monotonic() < settle_end:
        peak = max(peak, dev.current_ma())
    return {"arrived": True, "duration_s": round(total, 2), "peak_current_ma": peak}


def clamp_targets(cfg: Config, positions: dict[str, int]) -> tuple[dict[int, int], list]:
    targets, notes = {}, []
    for key, us in positions.items():
        ch = resolve(cfg, key)
        c = cfg.channels[ch]
        if c.mode != "servo":
            raise ToolError(f"channel {ch} ({c.name}) is an analog input, not a servo")
        clamped = min(max(int(us), c.min_us), c.max_us)
        if clamped != int(us):
            notes.append(f"{c.name}: {us} clamped to {clamped} (limits {c.min_us}-{c.max_us})")
        targets[ch] = clamped
    return targets, notes


# --- tools -----------------------------------------------------------------

@mcp.tool()
def board_status() -> dict:
    """Rail current/voltage, faults, protection limits, and every channel's name,
    mode, travel limits, speed and current position (0 = off)."""
    cfg = config()

    def go(dev):
        st, lim, pos = dev.status(), dev.limits(), dev.positions()
        return {
            "firmware": dev.version(),
            "rail": {"current_ma": st.current_ma,
                     "voltage_v": round(st.voltage_mv / 1000, 2) if st.rail_present else None,
                     "servo_supply_present": st.rail_present,
                     "faults": st.fault_names},
            "protection": {"overcurrent_ma": lim.overcurrent_ma,
                           "undervoltage_mv": lim.undervoltage_mv,
                           "undervoltage_auto": lim.auto},
            "channels": [{"channel": ch, "name": c.name, "mode": c.mode,
                          "min_us": c.min_us, "max_us": c.max_us,
                          "speed_us_per_s": c.speed, "position_us": pos[ch]}
                         for ch, c in enumerate(cfg.channels)],
        }
    return call(go)


@mcp.tool()
def move_servos(positions: dict[str, int], duration_s: float = 1.0) -> dict:
    """Move one or more servos and wait until they arrive.

    positions: {channel number or name: pulse width in us}, e.g. {"0": 1200}.
    duration_s: time for all to arrive together (0 = each channel's own speed).
    Positions are clamped to each channel's limits. Turns channels on if off."""
    if not positions:
        raise ToolError("positions is empty")
    if not 0 <= duration_s <= MAX_MOVE_S:
        raise ToolError(f"duration_s must be 0-{MAX_MOVE_S}")
    cfg = config()
    targets, notes = clamp_targets(cfg, positions)
    result = call(lambda dev: ramp(dev, cfg, targets, duration_s))
    result["positions_us"] = {cfg.channels[ch].name: us for ch, us in targets.items()}
    if notes:
        result["clamped"] = notes
    return result


@mcp.tool()
def disable_servos(channels: list[str]) -> str:
    """Stop driving the given channels (numbers or names); the servos go limp."""
    cfg = config()
    chans = [resolve(cfg, k) for k in channels]

    def go(dev):
        for ch in chans:
            dev.disable(ch)
    call(go)
    return "disabled: " + ", ".join(cfg.channels[ch].name for ch in chans)


@mcp.tool()
def stop_all() -> str:
    """Panic stop: every channel off immediately."""
    call(lambda dev: dev.stop_all())
    return "all channels stopped"


@mcp.tool()
def list_poses() -> dict:
    """Poses and the sequence saved from the GUI (servogui.json)."""
    cfg = config()
    return {"poses": {name: {cfg.channels[int(ch)].name: us for ch, us in pose.items()}
                      for name, pose in cfg.poses.items()},
            "sequence": cfg.sequence, "repeat": cfg.repeat}


@mcp.tool()
def go_to_pose(name: str, duration_s: float = 1.0) -> dict:
    """Move to a saved pose (see list_poses) and wait until it arrives."""
    cfg = config()
    if name not in cfg.poses:
        raise ToolError(f"no pose {name!r}; poses are: {', '.join(cfg.poses) or 'none'}")
    return move_servos({ch: us for ch, us in cfg.poses[name].items()}, duration_s)


@mcp.tool()
def play_sequence(loops: int = 1) -> dict:
    """Play the GUI's saved sequence of poses (move and hold times as saved),
    repeated `loops` times. Stops early on a fault; capped at 120 s."""
    cfg = config()
    if not cfg.sequence:
        raise ToolError("no sequence saved; build one in the GUI's Poses tab")
    loops = max(1, min(int(loops), 100))
    peak, steps, t0 = 0, 0, time.monotonic()
    for _ in range(loops):
        for step in cfg.sequence:
            if time.monotonic() - t0 > MAX_SEQUENCE_S:
                return {"completed": False, "steps": steps, "peak_current_ma": peak,
                        "note": f"stopped at the {MAX_SEQUENCE_S:.0f} s cap"}
            r = go_to_pose(step["pose"], step["move"])
            steps += 1
            peak = max(peak, r["peak_current_ma"])
            if not r["arrived"]:
                return {"completed": False, "steps": steps, **r}
            time.sleep(step["hold"])
    return {"completed": True, "steps": steps, "peak_current_ma": peak,
            "seconds": round(time.monotonic() - t0, 1)}


@mcp.tool()
def read_analog(channel: str) -> dict:
    """Read channel 0-7 as an analog input (0-4095 counts, 0-3.3 V)."""
    cfg = config()
    ch = resolve(cfg, channel)
    if ch >= usc.ANALOG_CHANNELS:
        raise ToolError("only channels 0-7 have an analog input")
    counts = call(lambda dev: dev.read_analog(ch))
    return {"channel": ch, "name": cfg.channels[ch].name, "counts": counts,
            "volts": round(counts * 3.3 / 4095, 3)}


@mcp.tool()
def measure_current(seconds: float = 1.0) -> dict:
    """Sample the servo-rail current continuously; returns peak, mean and min mA."""
    seconds = max(0.1, min(float(seconds), 30.0))

    def go(dev):
        vals, end = [], time.monotonic() + seconds
        while time.monotonic() < end:
            vals.append(dev.current_ma())
            time.sleep(0.001)
        return {"samples": len(vals), "peak_ma": max(vals), "min_ma": min(vals),
                "mean_ma": round(sum(vals) / len(vals))}
    return call(go)


@mcp.tool()
def set_protection(overcurrent_ma: int, undervoltage_mv: int = 0) -> dict:
    """Set the rail trip limits (0 disables either). Overcurrent is capped at
    7500 mA: the board's continuous budget is ~5 A. Resets at power-up."""
    if not 0 <= overcurrent_ma <= OC_MAX_MA:
        raise ToolError(f"overcurrent_ma must be 0-{OC_MAX_MA}")
    if not 0 <= undervoltage_mv <= UV_MAX_MV:
        raise ToolError(f"undervoltage_mv must be 0-{UV_MAX_MV}")

    def go(dev):
        dev.set_limits(overcurrent_ma, undervoltage_mv)
        lim = dev.limits()
        return {"overcurrent_ma": lim.overcurrent_ma, "undervoltage_mv": lim.undervoltage_mv}
    return call(go)


@mcp.tool()
def clear_faults() -> str:
    """Clear a latched overcurrent/undervoltage fault so channels can move again."""
    call(lambda dev: dev.clear_faults())
    return "faults cleared"


@mcp.tool()
def upload_script(script: str, autorun: bool = False, run: bool = False) -> dict:
    """Compile a servo script and store it in the board's flash, where the
    firmware runs it without a PC (autorun: also start at every power-up).

    One statement per line; channels by number or name; times in ms or '1.5s':
      label:                  jump/loop target
      move <ch> <us> [<time>] ramp there; does not wait: moves on different
                              channels run together, a repeat move of the
                              same channel waits for the previous one
      pose <name> [<time>]    move every channel of a GUI pose
      sync                    wait until all moves arrive
      wait <time>
      off <ch>|all
      loop <label> [<n>]      block runs n times in total; no n = forever
      jump <label>
      waitin <ch> >|< <0-4095>   wait for analog input ch (0-7)
      ifin <ch> >|< <0-4095>     run next statement only if true
      end
    Positions are clamped to the channel limits. Max 254 instructions."""
    try:
        code, warnings = servoscript.compile_script(script, config())
    except servoscript.ScriptError as exc:
        raise ToolError(f"script error: {exc}") from exc

    def go(dev):
        crc = dev.script_upload(code, autorun=autorun)
        if [tuple(x) for x in dev.script_read()] != [tuple(x) for x in code]:
            raise ToolError("read-back from flash did not match")
        if run:
            dev.script_run()
        return crc
    crc = call(go)
    return {"stored": True, "instructions": len(code), "crc": f"{crc:08X}",
            "autorun": autorun, "running": run, "warnings": warnings,
            "listing": servoscript.listing(code, config())}


@mcp.tool()
def script_status() -> dict:
    """What script is stored on the board, and whether it is running."""
    info = call(lambda dev: dev.script_info())
    return {"stored": info.valid, "instructions": info.count, "autorun": info.autorun,
            "state": info.state, "at_instruction": info.pc}


@mcp.tool()
def run_script() -> str:
    """Start the stored script from the top."""
    call(lambda dev: dev.script_run())
    return "script running"


@mcp.tool()
def stop_script() -> str:
    """Stop the stored script; servos keep their positions (stop_all to let go)."""
    call(lambda dev: dev.script_stop())
    return "script stopped"


@mcp.tool()
def release_port() -> str:
    """Close the serial port now (servos keep holding) so the GUI or flash.py
    can use the board. It reopens automatically on the next tool call."""
    board.release()
    return "port released"


def main(argv=None) -> int:
    global board
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", help="serial port; default is auto-detect")
    p.add_argument("--mock", action="store_true", help="run against the emulator")
    args = p.parse_args(argv)

    board = Board(args.port, args.mock)
    atexit.register(board.shutdown)
    try:
        mcp.run("stdio")
    finally:
        board.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
