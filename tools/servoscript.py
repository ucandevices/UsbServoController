#!/usr/bin/env python3
"""
servoscript.py -- write servo scripts that the board runs on its own.

A script is compiled here, uploaded into the board's flash, and executed by the
firmware (embedded-ch32/User/script.c): it keeps running with the PC unplugged,
and with autorun it starts at power-up. Standalone power: fit JP1 so the logic
runs from the servo supply.

    python servoscript.py check  wave.txt              # compile, show listing
    python servoscript.py upload wave.txt --run         # store it, start it
    python servoscript.py upload wave.txt --autorun     # also start at power-up
    python servoscript.py run | stop | status | dump    # dump = read back
    python servoscript.py from-gui -o seq.txt           # GUI sequence -> script

Language (one statement per line, # starts a comment; channels are numbers
0-11 or the names set in the GUI).
Times: a plain number is MILLISECONDS -- 2 is 2 ms; write 2s, 1.5s or 2000.

    top:                        a label (jump / loop target)
    move <ch> <us> [<time>]     ramp to <us> over <time> (default 0 = jump).
                                Starts the move and goes straight on, so moves
                                on DIFFERENT channels run together. A second
                                move of the SAME channel first waits for the
                                one before (sync added). A channel that is OFF
                                ramps from where it was last driven (1500 if
                                never), over the full time.
    pose <name> [<time>]        move every channel of a GUI pose
    sync                        wait until every move has ARRIVED
    wait <time>  (or sleep)     pause for a fixed time, moving or not
    off <ch> | off all          stop driving (the servo goes limp); a channel
                                still moving finishes its move first
    loop <label> [<n>]          go back to <label>; the block runs n times in
                                total (omit n: forever)
    jump <label>
    waitin <ch> > <value>       wait until analog input ch (0-7) reads above
    waitin <ch> < <value>         or below value (0-4095 = 0-3.3 V)
    ifin <ch> > <value>         run the next statement only if the input
    ifin <ch> < <value>           condition holds
    end                         stop here; moves still running finish first,
                                servos then hold position (also at the end
                                of the script)

Positions are clamped to each channel's min/max from the GUI (servogui.json),
with a warning, exactly as the GUI and the MCP server do.

Example -- wave, three times, then park:

    move 0 1500 500
    sync
    wave:
    move 0 1100 700
    sync
    move 0 1900 700
    sync
    loop wave 3
    move 0 1500 500
    sync
    off all
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import usc
from servoconfig import CONFIG_PATH, Config

OPS = usc.SCRIPT_OPS
NAMES = {v: k for k, v in OPS.items()}
MAX_TIME_MS = 3_600_000     # one hour per wait/move

# The language section of the docstring, for the GUI's Reference button.
REFERENCE = __doc__[__doc__.index("Language"):__doc__.index("Example --")].rstrip()
KEYWORDS = frozenset(OPS) | {"pose", "sleep"}


class ScriptError(Exception):
    pass


def parse_time(tok: str, line_no: int) -> int:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(ms|s)?", tok.strip().lower())
    if not m:
        raise ScriptError(f"line {line_no}: bad time {tok!r} (e.g. 500, 500ms, 1.5s)")
    ms = float(m.group(1)) * (1000 if m.group(2) == "s" else 1)
    if ms > MAX_TIME_MS:
        raise ScriptError(f"line {line_no}: time over one hour")
    return int(round(ms))


def compile_script(text: str, cfg: Config | None = None) -> tuple[list, list]:
    """
    Compile script text. Returns (instructions, warnings); instructions are
    (op, ch, a, b) tuples ready for ServoController.script_upload().
    """
    cfg = cfg or Config.load(CONFIG_PATH)
    names = {c.name.lower(): ch for ch, c in enumerate(cfg.channels)}
    code: list = []
    fixups: list = []            # (index, label, line_no)
    labels: dict = {}
    warnings: list = []

    def channel(tok: str, n: int, analog: bool = False) -> int:
        t = tok.lower()
        if t.isdigit():
            ch = int(t)
        elif t in names:
            ch = names[t]
        else:
            raise ScriptError(f"line {n}: unknown channel {tok!r}")
        if not 0 <= ch < usc.CHANNELS:
            raise ScriptError(f"line {n}: channel {ch} out of range 0-{usc.CHANNELS - 1}")
        if analog and ch >= usc.ANALOG_CHANNELS:
            raise ScriptError(f"line {n}: channel {ch} has no analog input (0-7 only)")
        return ch

    def position(ch: int, tok: str, n: int) -> int:
        if not tok.isdigit():
            raise ScriptError(f"line {n}: bad position {tok!r}")
        us, c = int(tok), cfg.channels[ch]
        if c.mode != "servo":
            raise ScriptError(f"line {n}: {c.name} is set as an analog input in the GUI")
        clamped = min(max(us, c.min_us, usc.US_ABS_MIN), c.max_us, usc.US_ABS_MAX)
        if clamped != us:
            warnings.append(f"line {n}: {c.name} {us} clamped to {clamped}")
        return clamped

    def condition(args: list, n: int) -> tuple:
        if len(args) != 3 or args[1] not in "<>" or not args[2].isdigit():
            raise ScriptError(f"line {n}: expected '<ch> > <value>' or '<ch> < <value>'")
        value = int(args[2])
        if value > 4095:
            raise ScriptError(f"line {n}: analog value is 0-4095")
        return channel(args[0], n, analog=True), value, 0 if args[1] == ">" else 1

    # Channels with a move started since the last sync. `move` does not wait,
    # so moves on different channels run together -- but a second move of the
    # SAME channel would silently replace the first. Nobody means that, so the
    # compiler waits for the first one (an added sync) and says so.
    moving: set = set()

    def settle(where: str, why: str) -> None:
        """Wait for every running move (an added sync), and say so."""
        if moving:
            code.append((OPS["sync"], 0, 0, 0))
            names = ", ".join(sorted(cfg.channels[ch].name for ch in moving))
            warnings.append(f"{where}: {names} still moving; waiting for it to "
                            f"finish {why} (sync added)")
            moving.clear()

    def add_move(ch: int, us: int, ms: int, n: int) -> None:
        if ch in moving:
            settle(f"line {n}", "before moving it again")
        if 0 < ms < 20:
            warnings.append(f"line {n}: move time {ms} ms is shorter than one servo "
                            f"frame (20 ms), so it is a jump -- did you mean {ms}s?")
        code.append((OPS["move"], ch, us, ms))
        moving.add(ch)

    def settle_before_jump(n: int) -> None:
        # A jump lands on a label whose next move may hit a channel that is
        # still moving here; wait first, so every path into a label is settled.
        settle(f"line {n}", "before the jump")

    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.endswith(":") and " " not in line:
            label = line[:-1].lower()
            if label in labels:
                raise ScriptError(f"line {n}: label {label!r} defined twice")
            labels[label] = len(code)
            continue                     # fall-through keeps `moving`; jumps settle it
        word, *args = line.replace(">", " > ").replace("<", " < ").split()
        word = word.lower()

        if word == "move":
            if len(args) not in (2, 3):
                raise ScriptError(f"line {n}: move <ch> <us> [<time>]")
            ch = channel(args[0], n)
            add_move(ch, position(ch, args[1], n),
                     parse_time(args[2], n) if len(args) == 3 else 0, n)
        elif word == "pose":
            if len(args) not in (1, 2):
                raise ScriptError(f"line {n}: pose <name> [<time>]")
            name = args[0]
            match = {k.lower(): k for k in cfg.poses}.get(name.lower())
            if not match:
                raise ScriptError(f"line {n}: no GUI pose {name!r}")
            ms = parse_time(args[1], n) if len(args) == 2 else 0
            for ch_s, us in sorted(cfg.poses[match].items(), key=lambda kv: int(kv[0])):
                ch = int(ch_s)
                add_move(ch, position(ch, str(us), n), ms, n)
        elif word == "sync" and not args:
            code.append((OPS["sync"], 0, 0, 0))
            moving.clear()
        elif word == "end" and not args:
            # the engine stops updating ramps at END: let them arrive first
            settle(f"line {n}", "before the script ends")
            code.append((OPS["end"], 0, 0, 0))
        elif word in ("wait", "sleep") and len(args) == 1:
            code.append((OPS["wait"], 0, 0, parse_time(args[0], n)))
        elif word == "off" and len(args) == 1:
            ch = usc.SCRIPT_CH_ALL if args[0].lower() == "all" else channel(args[0], n)
            if (ch == usc.SCRIPT_CH_ALL and moving) or ch in moving:
                settle(f"line {n}", "before turning it off")
            code.append((OPS["off"], ch, 0, 0))
            if ch == usc.SCRIPT_CH_ALL:
                moving.clear()
            else:
                moving.discard(ch)
        elif word == "loop" and len(args) in (1, 2):
            count = int(args[1]) if len(args) == 2 and args[1].isdigit() else 0
            if len(args) == 2 and not args[1].isdigit():
                raise ScriptError(f"line {n}: loop count must be a number")
            settle_before_jump(n)
            fixups.append((len(code), args[0].lower(), n))
            code.append((OPS["loop"], 0, 0, count))
        elif word == "jump" and len(args) == 1:
            settle_before_jump(n)
            fixups.append((len(code), args[0].lower(), n))
            code.append((OPS["jump"], 0, 0, 0))
        elif word in ("waitin", "ifin"):
            ch, value, cond = condition(args, n)
            code.append((OPS[word], ch, value, cond))
        else:
            raise ScriptError(f"line {n}: cannot parse {raw.strip()!r}")

    settle("end of script", "before the script ends")

    for idx, label, n in fixups:
        if label not in labels:
            raise ScriptError(f"line {n}: no label {label!r}")
        op, ch, _, b = code[idx]
        code[idx] = (op, ch, labels[label], b)
    if len(code) > usc.SCRIPT_MAX_INSTR:
        raise ScriptError(f"{len(code)} instructions; the board holds {usc.SCRIPT_MAX_INSTR}")
    if not code:
        raise ScriptError("script is empty")
    return code, warnings


def listing(code: list, cfg: Config | None = None) -> str:
    cfg = cfg or Config.load(CONFIG_PATH)

    def cname(ch: int) -> str:
        return "all" if ch == usc.SCRIPT_CH_ALL else cfg.channels[ch].name

    out = []
    for i, (op, ch, a, b) in enumerate(code):
        name = NAMES.get(op, f"op{op}")
        if name == "move":
            arg = f"{cname(ch)} {a} us over {b} ms"
        elif name == "wait":
            arg = f"{b} ms"
        elif name == "off":
            arg = cname(ch)
        elif name == "loop":
            arg = f"-> {a}, " + (f"{b} times" if b else "forever")
        elif name == "jump":
            arg = f"-> {a}"
        elif name in ("waitin", "ifin"):
            arg = f"{cname(ch)} {'<' if b else '>'} {a}"
        else:
            arg = ""
        out.append(f"{i:3d}  {name:6s} {arg}")
    return "\n".join(out)


def decompile(code: list, cfg: Config | None = None) -> str:
    """
    Turn instructions (e.g. read back from the board) into script text that
    compiles to the same instructions. Labels are invented (L<index>); channels
    are written as numbers, with the GUI name as a comment.
    """
    cfg = cfg or Config.load(CONFIG_PATH)
    targets = {a for op, ch, a, b in code if NAMES.get(op) in ("loop", "jump")}
    lines = ["# read back from the board"]
    for i, (op, ch, a, b) in enumerate(code):
        if i in targets:
            lines.append(f"L{i}:")
        name = NAMES.get(op)
        if name == "move":
            lines.append(f"move {ch} {a} {b}".ljust(24) + f"# {cfg.channels[ch].name}")
        elif name == "wait":
            lines.append(f"wait {b}")
        elif name == "off":
            lines.append("off all" if ch == usc.SCRIPT_CH_ALL else f"off {ch}")
        elif name == "loop":
            lines.append(f"loop L{a}" + (f" {b}" if b else ""))
        elif name == "jump":
            lines.append(f"jump L{a}")
        elif name in ("waitin", "ifin"):
            lines.append(f"{name} {ch} {'<' if b else '>'} {a}")
        elif name in ("sync", "end"):
            lines.append(name)
        else:
            raise ScriptError(f"instruction {i}: unknown opcode {op}")
    return "\n".join(lines) + "\n"


def from_gui(cfg: Config | None = None) -> str:
    """The GUI's pose sequence as script text."""
    cfg = cfg or Config.load(CONFIG_PATH)
    if not cfg.sequence:
        raise ScriptError("the GUI has no sequence (Poses & sequence tab)")
    lines = ["# generated from the GUI sequence by servoscript.py from-gui"]
    if cfg.repeat != 1:
        lines.append("top:")
    for step in cfg.sequence:
        lines.append(f"pose {step['pose']} {int(step['move'] * 1000)}")
        lines.append("sync")
        if step["hold"] > 0:
            lines.append(f"wait {int(step['hold'] * 1000)}")
    if cfg.repeat == 0:
        lines.append("loop top                # repeat forever")
    elif cfg.repeat > 1:
        lines.append(f"loop top {cfg.repeat}              # {cfg.repeat} passes in total")
    else:
        lines.append("end")
    for name in {s["pose"] for s in cfg.sequence}:
        if " " in name:
            raise ScriptError(f"pose {name!r} has a space in its name; rename it in the GUI")
    return "\n".join(lines) + "\n"


# --- command line ------------------------------------------------------------

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", help="serial port; default is auto-detect")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="compile and show the listing")
    c.add_argument("file")
    u = sub.add_parser("upload", help="compile and store on the board")
    u.add_argument("file")
    u.add_argument("--autorun", action="store_true", help="start at every power-up")
    u.add_argument("--run", action="store_true", help="start it now")
    sub.add_parser("run", help="start the stored script")
    sub.add_parser("stop", help="stop it (servos hold)")
    sub.add_parser("status", help="what is stored, is it running")
    sub.add_parser("dump", help="read the stored script back")
    g = sub.add_parser("from-gui", help="write the GUI's sequence as a script")
    g.add_argument("-o", "--output", help="file to write (default: print)")
    args = p.parse_args(argv)

    try:
        if args.cmd == "from-gui":
            text = from_gui()
            if args.output:
                Path(args.output).write_text(text, encoding="utf-8")
                print(f"wrote {args.output}")
            else:
                print(text, end="")
            return 0
        if args.cmd in ("check", "upload"):
            code, warnings = compile_script(Path(args.file).read_text(encoding="utf-8"))
            for w in warnings:
                print("warning:", w)
            print(listing(code))
            print(f"-- {len(code)}/{usc.SCRIPT_MAX_INSTR} instructions")
            if args.cmd == "check":
                return 0
        with usc.ServoController.open(args.port) as dev:
            if args.cmd == "upload":
                crc = dev.script_upload(code, autorun=args.autorun)
                back = dev.script_read()
                if [tuple(x) for x in back] != [tuple(x) for x in code]:
                    print("read-back does not match what was sent")
                    return 1
                print(f"stored and verified (CRC {crc:08X})"
                      + (", autorun on" if args.autorun else ""))
                if args.run:
                    dev.script_run()
                    print("running")
            elif args.cmd == "run":
                dev.script_run()
                print("running")
            elif args.cmd == "stop":
                dev.script_stop()
                print("stopped")
            elif args.cmd == "status":
                print(dev.script_info())
            elif args.cmd == "dump":
                code = dev.script_read()
                print(listing(code) if code else "no script stored")
    except (ScriptError, OSError, usc.UscError) as exc:
        print(f"error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
