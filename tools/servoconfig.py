"""
servoconfig.py -- channel settings, poses and sequence shared by the GUI and
the MCP server, kept in servogui.json next to this file.

No GUI dependency: servo_mcp.py imports this without tkinter.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

import usc

SLIDER_STEP_US = 10
SPEED_MAX = 10000          # us per second; 0 = instant

# A PyInstaller build unpacks the code to a temp dir, so settings, examples and
# the firmware image live next to the executable instead.
FROZEN = getattr(sys, "frozen", False)
APP_DIR = Path(sys.executable if FROZEN else __file__).resolve().parent
CONFIG_PATH = APP_DIR / "servogui.json"
EXAMPLES_DIR = APP_DIR / "examples"
DEFAULT_IMAGE = (APP_DIR / "USBServoController-ch32.bin" if FROZEN else
                 APP_DIR.parent / "embedded-ch32" / "build" / "USBServoController-ch32.bin")


@dataclass
class ChannelCfg:
    name: str
    min_us: int = usc.US_MIN
    max_us: int = usc.US_MAX
    speed: int = 0             # us per second, 0 = instant
    mode: str = "servo"        # "servo" or "input" (channels 0-7 only)


class Config:
    """Everything the GUI remembers between runs, kept in servogui.json."""

    def __init__(self):
        self.channels = [ChannelCfg(f"ch{ch}") for ch in range(usc.CHANNELS)]
        self.poses: dict[str, dict[str, int]] = {}      # name -> {"ch": us}
        self.sequence: list[dict] = []                   # {pose, move, hold}
        self.repeat = 1                                  # passes; 0 = forever
        self.flash_image = str(DEFAULT_IMAGE)
        self.script_path = ""                            # file open in the Script tab
        self.script_draft = ""                           # its text, saved or not

    @classmethod
    def load(cls, path: Path) -> "Config":
        cfg = cls()
        if not path.exists():
            return cfg
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            for ch, c in enumerate(data.get("channels", [])[:usc.CHANNELS]):
                cfg.channels[ch] = cls.valid_channel(ch, ChannelCfg(
                    str(c.get("name", f"ch{ch}")), int(c.get("min_us", usc.US_MIN)),
                    int(c.get("max_us", usc.US_MAX)), int(c.get("speed", 0)),
                    str(c.get("mode", "servo"))))
            cfg.poses = {str(k): {str(ch): int(us) for ch, us in v.items()}
                         for k, v in data.get("poses", {}).items()}
            cfg.sequence = [{"pose": str(s["pose"]), "move": float(s["move"]),
                             "hold": float(s["hold"])} for s in data.get("sequence", [])]
            # older files had a loop flag, which meant forever
            cfg.repeat = max(0, int(data.get("repeat", 0 if data.get("loop") else 1)))
            cfg.flash_image = str(data.get("flash_image", cfg.flash_image))
            cfg.script_path = str(data.get("script_path", ""))
            cfg.script_draft = str(data.get("script_draft", ""))
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            print(f"servogui: ignoring unreadable {path.name}: {exc}", file=sys.stderr)
            return cls()
        return cfg

    @staticmethod
    def valid_channel(ch: int, c: ChannelCfg) -> ChannelCfg:
        c.min_us = min(max(c.min_us, usc.US_MIN), usc.US_MAX - SLIDER_STEP_US)
        c.max_us = min(max(c.max_us, c.min_us + SLIDER_STEP_US), usc.US_MAX)
        c.speed = min(max(c.speed, 0), SPEED_MAX)
        if c.mode != "input" or ch >= usc.ANALOG_CHANNELS:
            c.mode = "servo"
        # no spaces: channel names are also words in the board's script language
        c.name = "_".join(c.name.split())[:16] or f"ch{ch}"
        return c

    def save(self, path: Path) -> None:
        data = {"channels": [asdict(c) for c in self.channels], "poses": self.poses,
                "sequence": self.sequence, "repeat": self.repeat,
                "flash_image": self.flash_image, "script_path": self.script_path,
                "script_draft": self.script_draft}
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(path)
