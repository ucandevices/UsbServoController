#!/usr/bin/env python3
"""
servogui.py -- desktop GUI for the USB-C 12-Channel Servo Controller.

    python servogui.py                 # auto-detect the board
    python servogui.py --port COM52
    python servogui.py --mock          # offline emulator, no hardware

Channels (left): tick "on" to drive a channel, drag its slider to move it. The
gear button sets a channel's name, travel limits (min/max us), speed and mode
(servo, or analog input on channels 0-7).

Tabs (right):
  Rail              live current/voltage, 10 s graph, peak hold, protection
                    limits, CSV recording
  Poses & sequence  save all channel positions as named poses, play a list of
                    poses back with move and hold times, repeated N times
                    or forever; upload it to the board as a script
  Firmware          reflash the board over USB (runs flash.py)

STOP, the space bar or Esc is the panic stop, 'X'.

Speed ramping happens here, not in the firmware: every 50 ms tick moves each
channel at most speed x 50 ms towards its target. Settings, poses and the
sequence live in servogui.json next to this file.

Needs only tkinter (bundled with Python) and pyserial, via usc.py.
"""

from __future__ import annotations

import argparse
import csv
import math
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from collections import deque
from pathlib import Path
from tkinter import filedialog, ttk

import serial

import servoscript
import usc
from servoconfig import (CONFIG_PATH, EXAMPLES_DIR, FROZEN, SLIDER_STEP_US,
                         SPEED_MAX, ChannelCfg, Config)
from usc import ServoController, UscError

POLL_MS = 50               # tick: ramping, status poll, graph
HISTORY_S = 10.0           # width of the current graph
ADC_FULL_SCALE = 4095
ADC_VREF = 3.3

# Overcurrent limit range offered in the GUI. The board's continuous budget is
# ~5 A (the firmware default); its narrowest servo-rail trace carries 7.8 A, so
# anything above 7.5 A would trip after the copper, not before it (README 2.4).
OC_MAX_MA = 7500
UV_MAX_MV = 13000          # voltage sense full scale is 13.2 V

FLASH_SCRIPT = Path(__file__).with_name("flash.py")
FLASH_ARG = "--run-flash"      # servogui --run-flash ... == flash.py ...

IO_ERRORS = (UscError, OSError, serial.SerialException)

SCRIPT_TEMPLATE = """\
# New script -- runs on the board, with or without the PC.
# F5 checks it; Upload stores it; 'autorun' starts it at every power-up.
# Help (button, bottom right) lists every statement.

move 0 1500 500        # channel 0 to centre over 0.5 s
sync                   # wait until it arrives

swing:
move 0 1200 800
sync
move 0 1800 800
sync
loop swing 3           # the block runs 3 times in total; no number = forever

move 0 1500 500
sync
off all
"""

# Dark palette
BG       = "#1e1f22"     # window
PANEL    = "#2b2d31"     # fields, graph
FG       = "#e6e6e6"     # text
MUTED    = "#8b8f97"     # axis labels, secondary text
GRID     = "#3a3d43"
ACCENT   = "#4f9dff"     # graph line, sliders
PEAK     = "#ffb74d"
ERROR    = "#ff6b6b"
OK_GREEN = "#6fcf73"
STOP_BG, STOP_ACTIVE = "#c62828", "#8e0000"


# --- look and feel -----------------------------------------------------------

def windows_dpi_aware() -> None:
    """Crisp text on scaled displays; must run before the Tk window exists."""
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass


def windows_dark_title_bar(window: tk.Misc) -> None:
    """Ask DWM for a dark title bar (Windows 10 20H1+ / 11); no-op elsewhere."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        window.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        on = ctypes.c_int(1)
        for attr in (20, 19):   # DWMWA_USE_IMMERSIVE_DARK_MODE, pre-20H1 value
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(on), ctypes.sizeof(on)) == 0:
                break
    except (AttributeError, OSError):
        pass


def apply_dark_theme(root: tk.Tk) -> None:
    root.configure(bg=BG)
    windows_dark_title_bar(root)
    st = ttk.Style(root)
    st.theme_use("clam")        # the only built-in theme that honours colours
    st.configure(".", background=BG, foreground=FG, fieldbackground=PANEL,
                 bordercolor=GRID, lightcolor=PANEL, darkcolor=PANEL,
                 troughcolor=PANEL, focuscolor=ACCENT, selectbackground=ACCENT,
                 selectforeground=BG, insertcolor=FG, font=("Segoe UI", 10))
    st.configure("TFrame", background=BG)
    st.configure("TLabel", background=BG, foreground=FG)
    st.configure("TLabelframe", background=BG, bordercolor=GRID)
    st.configure("TLabelframe.Label", background=BG, foreground=MUTED)
    st.configure("TButton", background=PANEL, foreground=FG, bordercolor=GRID,
                 padding=(10, 4))
    st.map("TButton", background=[("active", GRID), ("pressed", GRID)],
           foreground=[("disabled", MUTED)])
    st.configure("Gear.TButton", padding=(4, 0))
    st.configure("TCheckbutton", background=BG, foreground=FG,
                 indicatorbackground=PANEL, indicatorforeground=ACCENT)
    st.map("TCheckbutton", background=[("active", BG)],
           foreground=[("disabled", MUTED)],
           indicatorbackground=[("disabled", BG), ("selected", ACCENT), ("active", GRID)],
           indicatorforeground=[("selected", BG)])
    st.configure("Horizontal.TScale", background=ACCENT, troughcolor=PANEL,
                 bordercolor=GRID, lightcolor=ACCENT, darkcolor=ACCENT)
    st.map("Horizontal.TScale", background=[("active", "#7ab6ff")])
    st.configure("Horizontal.TProgressbar", background=PEAK, troughcolor=PANEL,
                 bordercolor=GRID, lightcolor=PEAK, darkcolor=PEAK)
    st.configure("TSpinbox", fieldbackground=PANEL, foreground=FG, background=PANEL,
                 arrowcolor=FG, bordercolor=GRID)
    st.configure("TEntry", fieldbackground=PANEL, foreground=FG, bordercolor=GRID)
    st.configure("TCombobox", fieldbackground=PANEL, foreground=FG, background=PANEL,
                 arrowcolor=FG, bordercolor=GRID)
    st.map("TCombobox", fieldbackground=[("readonly", PANEL)],
           foreground=[("readonly", FG)], selectbackground=[("readonly", PANEL)],
           selectforeground=[("readonly", FG)])
    root.option_add("*TCombobox*Listbox.background", PANEL)
    root.option_add("*TCombobox*Listbox.foreground", FG)
    root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
    root.option_add("*TCombobox*Listbox.selectForeground", BG)
    st.configure("TNotebook", background=BG, bordercolor=GRID, tabmargins=(0, 4, 0, 0))
    st.configure("TNotebook.Tab", background=PANEL, foreground=MUTED,
                 padding=(14, 4), bordercolor=GRID)
    st.map("TNotebook.Tab", background=[("selected", BG)], foreground=[("selected", FG)])
    st.configure("Big.TLabel", font=("Segoe UI", 20, "bold"))
    st.configure("Peak.TLabel", foreground=PEAK)
    st.configure("Muted.TLabel", foreground=MUTED)


def dark_listbox(parent: tk.Misc, **kw) -> tk.Listbox:
    return tk.Listbox(parent, bg=PANEL, fg=FG, selectbackground=ACCENT,
                      selectforeground=BG, highlightthickness=1,
                      highlightbackground=GRID, highlightcolor=ACCENT, relief="flat",
                      activestyle="none", font=("Segoe UI", 10), exportselection=False,
                      **kw)


def dark_text(parent: tk.Misc, **kw) -> tk.Text:
    return tk.Text(parent, bg=PANEL, fg=FG, insertbackground=FG, relief="flat",
                   highlightthickness=1, highlightbackground=GRID,
                   font=("Consolas", 9), **kw)


# Tk matches Ctrl+C/V/... on the *letter* a key produces. With a Polish or a
# Chinese IME layout active that letter is sometimes not "c", and copy/paste
# silently does nothing. Windows virtual-key codes name the physical key, so
# fall back to them whenever the letter is missing.
EDIT_KEYCODES = {67: ("c", "<<Copy>>"), 86: ("v", "<<Paste>>"), 88: ("x", "<<Cut>>"),
                 65: ("a", "<<SelectAll>>"), 90: ("z", "<<Undo>>"), 89: ("y", "<<Redo>>")}


def layout_proof_edit_keys(event) -> str | None:
    entry = EDIT_KEYCODES.get(event.keycode)
    if not entry or event.keysym.lower() == entry[0]:
        return None                      # Tk's own binding has it
    event.widget.event_generate(entry[1])
    return "break"


def attach_edit_menu(widget: tk.Text, readonly: bool = False) -> None:
    """Right-click menu, and click-to-focus so Ctrl+C works on read-only panes."""
    menu = tk.Menu(widget, tearoff=False, bg=PANEL, fg=FG, activebackground=ACCENT,
                   activeforeground=BG, relief="flat", bd=1)
    items = [("Copy", "<<Copy>>"), ("Select all", "<<SelectAll>>")]
    if not readonly:
        items = [("Cut", "<<Cut>>"), ("Copy", "<<Copy>>"), ("Paste", "<<Paste>>"),
                 None, ("Select all", "<<SelectAll>>"), None,
                 ("Undo", "<<Undo>>"), ("Redo", "<<Redo>>")]
    for item in items:
        if item is None:
            menu.add_separator()
        else:
            menu.add_command(label=item[0],
                             command=lambda ev=item[1]: widget.event_generate(ev))

    def popup(event):
        widget.focus_set()
        menu.tk_popup(event.x_root, event.y_root)
        return "break"
    widget.bind("<Button-3>", popup)
    if readonly:
        # A disabled Text never takes focus by itself, so Ctrl+C went nowhere.
        widget.bind("<Button-1>", lambda e: widget.focus_set(), add="+")
        # <<SelectAll>> is a no-op on a disabled Text; do it by hand.
        widget.bind("<<SelectAll>>", lambda e: (widget.tag_add("sel", "1.0", "end-1c"),
                                                "break")[1])


def snap(us: float) -> int:
    return int(round(us / SLIDER_STEP_US)) * SLIDER_STEP_US


# --- the GUI -----------------------------------------------------------------

class ServoGui:
    def __init__(self, root: tk.Tk, port: str | None, mock: bool,
                 config_path: Path = CONFIG_PATH):
        self.root = root
        self.port = port
        self.mock = mock
        self.config_path = config_path
        self.cfg = Config.load(config_path)
        self.dev: ServoController | None = None
        self.after_id = None
        self.last_tick = time.monotonic()

        # per-channel runtime state
        n = usc.CHANNELS
        self.target = [usc.US_NEUTRAL] * n      # where the slider says to go
        self.cmd: list[float | None] = [None] * n    # ramped position, None = off
        self.sent: list[int | None] = [None] * n     # last value sent to the board
        self.analog: list[int | None] = [None] * n
        self.override_speed: dict[int, float] = {}   # set by the sequencer
        self.input_rr = 0                           # round-robin input polling

        # rail
        self.history: deque = deque()
        self.peak_ma = 0

        # sequencer
        self.seq_playing = False
        self.seq_idx = -1
        self.seq_pass = 1
        self.seq_step_end = 0.0

        # CSV recording
        self.log_file = None
        self.log_writer = None
        self.log_t0 = 0.0
        self.log_flushed = 0.0

        # script stored on the board
        self.board_running = False
        self.tick = 0

        # firmware flashing
        self.flash_queue: queue.Queue = queue.Queue()
        self.flashing = False

        root.title("Servo Controller")
        apply_dark_theme(root)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.bind("<space>", self.on_stop_key)
        root.bind("<Escape>", self.on_stop_key)
        # every text field: copy/paste regardless of keyboard layout
        root.bind_all("<Control-KeyPress>", layout_proof_edit_keys)

        self.build_channels()
        self.build_tabs()
        self.state_lbl = ttk.Label(root, text="connecting...", padding=(10, 0, 10, 6))
        self.state_lbl.grid(row=1, column=0, columnspan=2, sticky="w")
        root.columnconfigure(1, weight=1)
        root.rowconfigure(0, weight=1)

        for ch in range(n):
            self.apply_channel_cfg(ch)
        self.connect()

    # -- layout: channels ---------------------------------------------------

    def build_channels(self) -> None:
        frame = ttk.LabelFrame(self.root, text="Channels", padding=8)
        frame.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)

        self.enabled, self.name_lbls, self.scales = [], [], []
        self.bars, self.value_lbls, self.checks = [], [], []
        for ch in range(usc.CHANNELS):
            on = tk.BooleanVar(value=False)
            name = ttk.Label(frame, width=10, anchor="w")
            name.grid(row=ch, column=0, sticky="w")
            check = ttk.Checkbutton(frame, text="on", variable=on,
                                    command=lambda c=ch: self.on_toggle(c))
            check.grid(row=ch, column=1)
            scale = ttk.Scale(frame, from_=usc.US_MIN, to=usc.US_MAX, length=300,
                              orient="horizontal",
                              command=lambda v, c=ch: self.on_slide(c, v))
            scale.grid(row=ch, column=2, padx=6, pady=1)
            bar = ttk.Progressbar(frame, maximum=ADC_FULL_SCALE, length=300)
            value = ttk.Label(frame, width=13, anchor="w")
            value.grid(row=ch, column=3)
            ttk.Button(frame, text="⚙", width=2, style="Gear.TButton",
                       command=lambda c=ch: self.edit_channel(c)).grid(row=ch, column=4)
            self.enabled.append(on)
            self.name_lbls.append(name)
            self.checks.append(check)
            self.scales.append(scale)
            self.bars.append(bar)
            self.value_lbls.append(value)
            scale.set(usc.US_NEUTRAL)    # fires on_slide: lists must exist first

        buttons = ttk.Frame(frame)
        buttons.grid(row=usc.CHANNELS, column=0, columnspan=5, pady=(8, 0), sticky="ew")
        ttk.Button(buttons, text="Enable all", command=self.enable_all).pack(side="left")
        ttk.Button(buttons, text="Move to center",
                   command=self.centre_enabled).pack(side="left", padx=8)
        stop = tk.Button(buttons, text="STOP  (space)", bg=STOP_BG, fg="white",
                         activebackground=STOP_ACTIVE, activeforeground="white",
                         relief="flat", font=("Segoe UI", 11, "bold"),
                         command=self.stop_all)
        stop.pack(side="right", ipadx=12)

    # -- layout: tabs -------------------------------------------------------

    def build_tabs(self) -> None:
        nb = ttk.Notebook(self.root)
        nb.grid(row=0, column=1, sticky="nsew", padx=(0, 8), pady=8)
        rail, poses, script, fw = (ttk.Frame(nb, padding=8) for _ in range(4))
        nb.add(rail, text="Rail")
        nb.add(poses, text="Poses & sequence")
        nb.add(script, text="Script")
        nb.add(fw, text="Firmware")
        self.build_rail(rail)
        self.build_poses(poses)            # creates autorun_var, shared with Script
        self.build_script(script)
        self.build_firmware(fw)

    def build_rail(self, frame: ttk.Frame) -> None:
        self.current_lbl = ttk.Label(frame, text="-- mA", style="Big.TLabel", width=10)
        self.current_lbl.grid(row=0, column=0, sticky="w")
        self.voltage_lbl = ttk.Label(frame, text="-- V", style="Big.TLabel", width=9)
        self.voltage_lbl.grid(row=0, column=1, sticky="w")

        peak_row = ttk.Frame(frame)
        peak_row.grid(row=1, column=0, columnspan=2, sticky="w")
        self.peak_lbl = ttk.Label(peak_row, text="peak -- mA", style="Peak.TLabel")
        self.peak_lbl.pack(side="left")
        ttk.Button(peak_row, text="reset peak", command=self.reset_peak).pack(side="left", padx=8)

        self.canvas = tk.Canvas(frame, width=460, height=220, bg=PANEL,
                                highlightthickness=1, highlightbackground=GRID)
        self.canvas.grid(row=2, column=0, columnspan=2, pady=8, sticky="nsew")
        frame.rowconfigure(2, weight=1)
        frame.columnconfigure(1, weight=1)

        row = ttk.Frame(frame)
        row.grid(row=3, column=0, columnspan=2, sticky="w")
        ttk.Button(row, text="Clear faults", command=self.clear_faults).pack(side="left")
        ttk.Button(row, text="Tare current (stops all)", command=self.tare).pack(side="left", padx=8)
        ttk.Button(row, text="Reconnect", command=self.connect).pack(side="left")

        prot = ttk.LabelFrame(frame, text="Protection (resets to 5000 mA at power-up)",
                              padding=6)
        prot.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Label(prot, text="overcurrent").pack(side="left")
        self.oc_var = tk.StringVar(value=str(usc.DEFAULT_LIMIT_MA))
        ttk.Spinbox(prot, from_=0, to=OC_MAX_MA, increment=250, width=6,
                    textvariable=self.oc_var).pack(side="left", padx=(4, 2))
        ttk.Label(prot, text="mA").pack(side="left", padx=(0, 12))
        ttk.Label(prot, text="undervoltage").pack(side="left")
        self.uv_var = tk.StringVar(value="0")
        ttk.Spinbox(prot, from_=0, to=UV_MAX_MV, increment=100, width=6,
                    textvariable=self.uv_var).pack(side="left", padx=(4, 2))
        ttk.Label(prot, text="mV").pack(side="left", padx=(0, 12))
        ttk.Button(prot, text="Apply", command=self.apply_limits).pack(side="left")
        self.limits_lbl = ttk.Label(prot, text="", style="Muted.TLabel")
        self.limits_lbl.pack(side="left", padx=8)

        rec = ttk.LabelFrame(frame, text="Recording", padding=6)
        rec.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.rec_btn = ttk.Button(rec, text="Record CSV...", command=self.toggle_record)
        self.rec_btn.pack(side="left")
        self.rec_lbl = ttk.Label(rec, text="current, voltage, faults, positions and "
                                 "inputs every 50 ms", style="Muted.TLabel")
        self.rec_lbl.pack(side="left", padx=8)

    def build_poses(self, frame: ttk.Frame) -> None:
        frame.columnconfigure(0, weight=1)
        frame.columnconfigure(1, weight=2)
        frame.rowconfigure(1, weight=1)

        # poses
        ttk.Label(frame, text="Poses", style="Muted.TLabel").grid(row=0, column=0, sticky="w")
        self.pose_list = dark_listbox(frame, height=12)
        self.pose_list.grid(row=1, column=0, sticky="nsew", padx=(0, 8))
        self.pose_list.bind("<Double-Button-1>", lambda e: self.goto_pose())
        prow = ttk.Frame(frame)
        prow.grid(row=2, column=0, sticky="ew", pady=(6, 0), padx=(0, 8))
        self.pose_name = tk.StringVar()
        ttk.Entry(prow, textvariable=self.pose_name, width=14).pack(side="left")
        ttk.Button(prow, text="Save current", command=self.save_pose).pack(side="left", padx=4)
        prow2 = ttk.Frame(frame)
        prow2.grid(row=3, column=0, sticky="ew", pady=(4, 0), padx=(0, 8))
        ttk.Button(prow2, text="Go to", command=self.goto_pose).pack(side="left")
        ttk.Button(prow2, text="Delete", command=self.delete_pose).pack(side="left", padx=4)

        # sequence
        ttk.Label(frame, text="Sequence", style="Muted.TLabel").grid(row=0, column=1, sticky="w")
        self.seq_list = dark_listbox(frame, height=12)
        self.seq_list.grid(row=1, column=1, sticky="nsew")
        srow = ttk.Frame(frame)
        srow.grid(row=2, column=1, sticky="ew", pady=(6, 0))
        self.step_pose = tk.StringVar()
        self.pose_combo = ttk.Combobox(srow, textvariable=self.step_pose, width=12,
                                       state="readonly")
        self.pose_combo.pack(side="left")
        ttk.Label(srow, text="move").pack(side="left", padx=(8, 2))
        self.step_move = tk.StringVar(value="1.0")
        ttk.Spinbox(srow, from_=0, to=60, increment=0.1, width=5,
                    textvariable=self.step_move).pack(side="left")
        ttk.Label(srow, text="s  hold").pack(side="left", padx=(2, 2))
        self.step_hold = tk.StringVar(value="0.5")
        ttk.Spinbox(srow, from_=0, to=600, increment=0.1, width=5,
                    textvariable=self.step_hold).pack(side="left")
        ttk.Label(srow, text="s").pack(side="left", padx=(2, 8))
        ttk.Button(srow, text="Add step", command=self.add_step).pack(side="left")

        srow2 = ttk.Frame(frame)
        srow2.grid(row=3, column=1, sticky="ew", pady=(4, 0))
        ttk.Button(srow2, text="Remove", command=self.remove_step).pack(side="left")
        ttk.Button(srow2, text="Up", command=lambda: self.move_step(-1)).pack(side="left", padx=4)
        ttk.Button(srow2, text="Down", command=lambda: self.move_step(1)).pack(side="left")

        play = ttk.Frame(frame)
        play.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        ttk.Button(play, text="▶  Play", command=self.play_sequence).pack(side="left")
        ttk.Button(play, text="■  Stop", command=self.stop_sequence).pack(side="left", padx=4)
        ttk.Label(play, text="repeat").pack(side="left", padx=(12, 2))
        self.repeat_var = tk.StringVar(value=str(self.cfg.repeat))
        ttk.Spinbox(play, from_=0, to=9999, increment=1, width=5,
                    textvariable=self.repeat_var,
                    command=self.on_repeat_change).pack(side="left")
        self.repeat_var.trace_add("write", lambda *a: self.on_repeat_change())
        ttk.Label(play, text="times (0 = forever)",
                  style="Muted.TLabel").pack(side="left", padx=(2, 8))
        self.seq_lbl = ttk.Label(play, text="", style="Muted.TLabel")
        self.seq_lbl.pack(side="left", padx=8)
        ttk.Label(frame, style="Muted.TLabel", wraplength=560, justify="left",
                  text="A pose stores every channel that is on. Going to a pose turns "
                       "those channels on and moves them; others are left alone. "
                       "'move' is how long each step takes to arrive (0 = at each "
                       "channel's own speed), 'hold' how long it then stays.") \
            .grid(row=5, column=0, columnspan=2, sticky="w", pady=(8, 0))

        board = ttk.LabelFrame(frame, text="On the board — runs without the PC",
                               padding=6)
        board.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        ttk.Button(board, text="Upload sequence",
                   command=self.upload_sequence).pack(side="left")
        ttk.Button(board, text="Upload script file...",
                   command=self.upload_script_file).pack(side="left", padx=4)
        self.autorun_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(board, text="autorun at power-up",
                        variable=self.autorun_var).pack(side="left", padx=8)
        ttk.Button(board, text="▶  Run", command=self.board_run).pack(side="left")
        ttk.Button(board, text="■  Stop", command=self.board_stop).pack(side="left", padx=4)
        self.board_lbl = ttk.Label(frame, text="", style="Muted.TLabel")
        self.board_lbl.grid(row=7, column=0, columnspan=2, sticky="w", pady=(4, 0))

        self.refresh_poses()
        self.refresh_sequence()

    def build_script(self, frame: ttk.Frame) -> None:
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=3)
        frame.rowconfigure(3, weight=1)

        files = ttk.Frame(frame)
        files.grid(row=0, column=0, sticky="ew")
        for text, cmd in (("New", self.script_new), ("Open...", self.script_open),
                          ("Save", self.script_save), ("Save as...", self.script_save_as)):
            ttk.Button(files, text=text, command=cmd).pack(side="left", padx=(0, 4))
        ttk.Button(files, text="From sequence",
                   command=self.script_from_sequence).pack(side="left", padx=(8, 4))
        ttk.Button(files, text="Read from board",
                   command=self.script_read_board).pack(side="left")
        self.script_file_lbl = ttk.Label(files, style="Muted.TLabel")
        self.script_file_lbl.pack(side="left", padx=12)

        ed = ttk.Frame(frame)
        ed.grid(row=1, column=0, sticky="nsew", pady=(6, 0))
        ed.columnconfigure(0, weight=1)
        ed.rowconfigure(0, weight=1)
        self.editor = tk.Text(ed, bg=PANEL, fg=FG, insertbackground=FG, relief="flat",
                              highlightthickness=1, highlightbackground=GRID,
                              highlightcolor=ACCENT, selectbackground=ACCENT,
                              selectforeground=BG, font=("Consolas", 11), undo=True,
                              wrap="none", width=60, height=14)
        sb = ttk.Scrollbar(ed, orient="vertical", command=self.editor.yview)
        self.editor.configure(yscrollcommand=sb.set)
        self.editor.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        self.editor.tag_configure("comment", foreground=MUTED)
        self.editor.tag_configure("keyword", foreground=ACCENT)
        self.editor.tag_configure("label", foreground=PEAK)
        self.editor.tag_configure("error", background="#5a1d1d")
        self.editor.bind("<<Modified>>", self.on_script_modified)
        self.editor.bind("<Control-s>", lambda e: (self.script_save(), "break")[1])
        self.editor.bind("<F5>", lambda e: (self.script_check(), "break")[1])
        attach_edit_menu(self.editor)

        bar = ttk.Frame(frame)
        bar.grid(row=2, column=0, sticky="ew", pady=6)
        ttk.Button(bar, text="Check  (F5)", command=self.script_check).pack(side="left")
        ttk.Button(bar, text="Upload", command=self.script_upload).pack(side="left", padx=4)
        ttk.Button(bar, text="Upload + Run",
                   command=lambda: self.script_upload(run=True)).pack(side="left")
        ttk.Checkbutton(bar, text="autorun at power-up",
                        variable=self.autorun_var).pack(side="left", padx=8)
        ttk.Button(bar, text="▶  Run", command=self.board_run).pack(side="left")
        ttk.Button(bar, text="■  Stop", command=self.board_stop).pack(side="left", padx=4)
        ttk.Button(bar, text="Help", command=self.script_reference).pack(side="right")

        self.script_out = dark_text(frame, height=8, state="disabled", wrap="none")
        self.script_out.grid(row=3, column=0, sticky="nsew")
        self.script_out.tag_configure("err", foreground=ERROR)
        self.script_out.tag_configure("warn", foreground=PEAK)
        self.script_out.tag_configure("ok", foreground=OK_GREEN)
        attach_edit_menu(self.script_out, readonly=True)
        self.script_board_lbl = ttk.Label(frame, text="", style="Muted.TLabel")
        self.script_board_lbl.grid(row=4, column=0, sticky="w", pady=(4, 0))

        # Reopen where we left off: the draft, else the file, else a template.
        self.script_path = Path(self.cfg.script_path) if self.cfg.script_path else None
        text = self.cfg.script_draft
        if not text and self.script_path and self.script_path.is_file():
            text = self.script_path.read_text(encoding="utf-8")
        self.script_highlight_id = None
        self.set_script_text(text or SCRIPT_TEMPLATE)
        on_disk = (self.script_path.read_text(encoding="utf-8")
                   if self.script_path and self.script_path.is_file() else None)
        self.script_dirty = on_disk is None or on_disk != self.editor.get("1.0", "end-1c")
        self.update_script_title()
        self.script_output([("Write a script, F5 to check, then Upload. "
                             "Help lists every statement.", "")])

    def build_firmware(self, frame: ttk.Frame) -> None:
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(2, weight=1)
        ttk.Label(frame, text="Application image").grid(row=0, column=0, sticky="w")
        self.image_var = tk.StringVar(value=self.cfg.flash_image)
        ttk.Entry(frame, textvariable=self.image_var).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(frame, text="Browse...", command=self.browse_image).grid(row=0, column=2)
        row = ttk.Frame(frame)
        row.grid(row=1, column=0, columnspan=3, sticky="w", pady=8)
        self.flash_btn = ttk.Button(row, text="Flash firmware", command=self.flash_firmware)
        self.flash_btn.pack(side="left")
        ttk.Label(row, style="Muted.TLabel",
                  text="no jumper needed: reboots into the USB bootloader, writes, "
                       "verifies, restarts (~3 s). Build first with make.") \
            .pack(side="left", padx=8)
        self.flash_log = dark_text(frame, height=14, state="disabled")
        self.flash_log.grid(row=2, column=0, columnspan=3, sticky="nsew")
        attach_edit_menu(self.flash_log, readonly=True)

    # -- connection ---------------------------------------------------------

    def connect(self) -> None:
        self.close_device()
        try:
            self.dev = ServoController.mock() if self.mock else ServoController.open(self.port)
            version = self.dev.version()
            self.sync_from_device()
        except IO_ERRORS as exc:
            self.dev = None
            self.set_state(f"not connected: {exc}", ERROR)
            return
        where = "mock device" if self.mock else getattr(self.dev._io, "port", "board")
        if not self.mock and not self.port:
            self.port = where               # keep it for flash.py and reconnects
        self.set_state(f"connected: {where}  {version}", OK_GREEN)
        self.load_limits()
        self.last_tick = time.monotonic()
        self.schedule()

    def close_device(self) -> None:
        if self.after_id:
            self.root.after_cancel(self.after_id)
            self.after_id = None
        self.stop_sequence()
        if self.dev:
            try:
                self.dev.stop_all()
            except IO_ERRORS:
                pass
            self.dev.close()
            self.dev = None
        for ch in range(usc.CHANNELS):
            self.mark_off(ch)

    def lost(self, exc: Exception) -> None:
        if self.after_id:
            self.root.after_cancel(self.after_id)
        self.dev = None
        self.after_id = None
        self.stop_sequence()
        self.set_state(f"connection lost: {exc} -- press Reconnect", ERROR)

    def set_state(self, text: str, colour: str = FG) -> None:
        self.state_lbl.config(text=text, foreground=colour)

    def sync_from_device(self) -> None:
        """Make the channel rows match what the board is really driving."""
        for ch, us in enumerate(self.dev.positions()):
            if us and self.cfg.channels[ch].mode == "servo":
                self.enabled[ch].set(True)
                self.cmd[ch] = self.sent[ch] = us
                self.set_target(ch, us)
            else:
                self.mark_off(ch)

    # -- channel settings ---------------------------------------------------

    def apply_channel_cfg(self, ch: int) -> None:
        c = self.cfg.channels[ch]
        self.name_lbls[ch].config(text=c.name)
        self.scales[ch].configure(from_=c.min_us, to=c.max_us)
        if c.mode == "input":
            if self.enabled[ch].get():
                self.enabled[ch].set(False)
                self.on_toggle(ch)
            self.checks[ch].state(["disabled"])
            self.scales[ch].grid_remove()
            self.bars[ch].grid(row=ch, column=2, padx=6, pady=1)
            self.value_lbls[ch].config(text="input")
        else:
            self.checks[ch].state(["!disabled"])
            self.bars[ch].grid_remove()
            self.scales[ch].grid()
            self.set_target(ch, self.target[ch])
            self.update_value_label(ch)

    def edit_channel(self, ch: int) -> None:
        c = self.cfg.channels[ch]
        win = tk.Toplevel(self.root)
        win.title(f"Channel {ch}")
        win.configure(bg=BG)
        win.transient(self.root)
        win.resizable(False, False)
        body = ttk.Frame(win, padding=12)
        body.pack(fill="both", expand=True)

        name = tk.StringVar(value=c.name)
        lo, hi = tk.StringVar(value=str(c.min_us)), tk.StringVar(value=str(c.max_us))
        speed = tk.StringVar(value=str(c.speed))
        mode = tk.StringVar(value=c.mode)

        rows = [("Name", ttk.Entry(body, textvariable=name, width=18), ""),
                ("Min", ttk.Spinbox(body, from_=usc.US_MIN, to=usc.US_MAX, increment=10,
                                    width=8, textvariable=lo), "us"),
                ("Max", ttk.Spinbox(body, from_=usc.US_MIN, to=usc.US_MAX, increment=10,
                                    width=8, textvariable=hi), "us"),
                ("Speed", ttk.Spinbox(body, from_=0, to=SPEED_MAX, increment=100,
                                      width=8, textvariable=speed), "us/s, 0 = instant"),
                ("Mode", ttk.Combobox(body, textvariable=mode, width=8, state="readonly",
                                      values=["servo", "input"] if ch < usc.ANALOG_CHANNELS
                                      else ["servo"]),
                 "input = read as analog, 0-3.3 V" if ch < usc.ANALOG_CHANNELS
                 else "no ADC on this channel")]
        for r, (label, widget, hint) in enumerate(rows):
            ttk.Label(body, text=label).grid(row=r, column=0, sticky="w", pady=3)
            widget.grid(row=r, column=1, sticky="w", padx=8)
            ttk.Label(body, text=hint, style="Muted.TLabel").grid(row=r, column=2, sticky="w")
        err = ttk.Label(body, text="", foreground=ERROR)
        err.grid(row=len(rows), column=0, columnspan=3, sticky="w")

        def save() -> None:
            try:
                new = ChannelCfg(name.get(), int(lo.get()), int(hi.get()),
                                 int(speed.get()), mode.get())
            except ValueError:
                err.config(text="min, max and speed must be whole numbers")
                return
            if not usc.US_MIN <= new.min_us < new.max_us <= usc.US_MAX:
                err.config(text=f"need {usc.US_MIN} <= min < max <= {usc.US_MAX}")
                return
            self.cfg.channels[ch] = Config.valid_channel(ch, new)
            self.save_config()
            self.apply_channel_cfg(ch)
            win.destroy()

        btns = ttk.Frame(body)
        btns.grid(row=len(rows) + 1, column=0, columnspan=3, sticky="e", pady=(8, 0))
        ttk.Button(btns, text="Cancel", command=win.destroy).pack(side="right")
        ttk.Button(btns, text="Save", command=save).pack(side="right", padx=6)
        win.bind("<Return>", lambda e: save())
        windows_dark_title_bar(win)
        win.grab_set()

    def save_config(self) -> None:
        try:
            self.cfg.save(self.config_path)
        except OSError as exc:
            self.set_state(f"could not save {self.config_path.name}: {exc}", ERROR)

    # -- channel control ----------------------------------------------------

    def set_target(self, ch: int, us: float) -> None:
        c = self.cfg.channels[ch]
        us = min(max(snap(us), c.min_us), c.max_us)
        self.target[ch] = us
        self.scales[ch].set(us)          # fires on_slide, which is idempotent here

    def on_slide(self, ch: int, value: str) -> None:
        c = self.cfg.channels[ch]
        self.target[ch] = min(max(snap(float(value)), c.min_us), c.max_us)
        self.update_value_label(ch)

    def update_value_label(self, ch: int) -> None:
        if self.cfg.channels[ch].mode != "servo":
            return
        tgt, cur = self.target[ch], self.cmd[ch]
        if cur is None or round(cur) == tgt:
            text = f"{tgt} us"
        else:
            text = f"{round(cur)} → {tgt}"
        self.value_lbls[ch].config(text=text,
                                   foreground=FG if self.enabled[ch].get() else MUTED)

    def mark_off(self, ch: int) -> None:
        self.enabled[ch].set(False)
        self.cmd[ch] = self.sent[ch] = None
        self.override_speed.pop(ch, None)
        self.update_value_label(ch)

    def on_toggle(self, ch: int) -> None:
        if not self.dev:
            self.enabled[ch].set(False)
            return
        if self.enabled[ch].get():
            self.cmd[ch] = self.sent[ch] = None     # next tick jumps to the target
            self.update_value_label(ch)
            return
        try:
            self.dev.disable(ch)
        except IO_ERRORS as exc:
            self.lost(exc)
        self.mark_off(ch)

    def enable_all(self) -> None:
        if not self.dev:
            return
        for ch in range(usc.CHANNELS):
            if self.cfg.channels[ch].mode == "servo" and not self.enabled[ch].get():
                self.enabled[ch].set(True)
                self.on_toggle(ch)

    def centre_enabled(self) -> None:
        for ch in range(usc.CHANNELS):
            c = self.cfg.channels[ch]
            if self.enabled[ch].get():
                self.set_target(ch, (c.min_us + c.max_us) / 2)

    def on_stop_key(self, event) -> None:
        # A space typed into a field or the script editor must not stop
        # everything; Esc always does.
        if event.keysym == "space" and isinstance(
                event.widget, (tk.Entry, ttk.Entry, tk.Text, tk.Spinbox,
                               ttk.Spinbox, ttk.Combobox)):
            return
        self.stop_all()

    def stop_all(self) -> None:
        self.stop_sequence()
        self.board_running = False           # X stops the board's script too
        for ch in range(usc.CHANNELS):
            self.mark_off(ch)
        if self.dev:
            try:
                self.dev.stop_all()
            except IO_ERRORS as exc:
                self.lost(exc)

    def drive(self, dt: float) -> None:
        """Advance every enabled channel towards its target, send what changed."""
        for ch in range(usc.CHANNELS):
            if not self.enabled[ch].get() or self.cfg.channels[ch].mode != "servo":
                continue
            tgt, cur = self.target[ch], self.cmd[ch]
            speed = self.override_speed.get(ch) or self.cfg.channels[ch].speed
            if cur is None or speed <= 0 or abs(tgt - cur) <= speed * dt:
                new = float(tgt)
            else:
                new = cur + math.copysign(speed * dt, tgt - cur)
            self.cmd[ch] = new
            out = int(round(new))
            if out != self.sent[ch]:
                self.dev.set_us(ch, out)
                self.sent[ch] = out
            self.update_value_label(ch)

    def read_inputs(self) -> None:
        """One input-mode channel per tick, round robin: bounded port traffic."""
        inputs = [ch for ch in range(usc.ANALOG_CHANNELS)
                  if self.cfg.channels[ch].mode == "input"]
        if not inputs:
            return
        self.input_rr = (self.input_rr + 1) % len(inputs)
        ch = inputs[self.input_rr]
        counts = self.dev.read_analog(ch)
        self.analog[ch] = counts
        self.bars[ch].config(value=counts)
        self.value_lbls[ch].config(
            text=f"{counts:4d}  {counts * ADC_VREF / ADC_FULL_SCALE:.2f} V", foreground=FG)

    # -- protection ---------------------------------------------------------

    def load_limits(self) -> None:
        try:
            lim = self.dev.limits()
        except IO_ERRORS as exc:
            self.lost(exc)
            return
        self.oc_var.set(str(lim.overcurrent_ma))
        self.uv_var.set(str(lim.undervoltage_mv))
        oc = f"{lim.overcurrent_ma} mA" if lim.overcurrent_ma else "off"
        uv = f"{lim.undervoltage_mv} mV" if lim.undervoltage_mv else "off"
        self.limits_lbl.config(text=f"in force: {oc} / {uv}"
                                    f"{' (auto)' if lim.auto else ''}", foreground=MUTED)

    def apply_limits(self) -> None:
        if not self.dev:
            return
        try:
            oc, uv = int(self.oc_var.get()), int(self.uv_var.get())
        except ValueError:
            self.limits_lbl.config(text="whole numbers only", foreground=ERROR)
            return
        if not (0 <= oc <= OC_MAX_MA and 0 <= uv <= UV_MAX_MV):
            self.limits_lbl.config(
                text=f"overcurrent 0-{OC_MAX_MA} mA, undervoltage 0-{UV_MAX_MV} mV",
                foreground=ERROR)
            return
        try:
            self.dev.set_limits(oc, uv)
        except IO_ERRORS as exc:
            self.lost(exc)
            return
        self.load_limits()

    def clear_faults(self) -> None:
        if self.dev:
            try:
                self.dev.clear_faults()
            except IO_ERRORS as exc:
                self.lost(exc)

    def tare(self) -> None:
        if self.dev:
            self.stop_all()
            try:
                self.dev.tare()
            except IO_ERRORS as exc:
                self.lost(exc)

    def reset_peak(self) -> None:
        self.peak_ma = 0

    # -- poses and sequence -------------------------------------------------

    def refresh_poses(self) -> None:
        names = sorted(self.cfg.poses)
        self.pose_list.delete(0, "end")
        for name in names:
            chans = ", ".join(self.cfg.channels[int(ch)].name
                              for ch in sorted(self.cfg.poses[name], key=int))
            self.pose_list.insert("end", f"{name}   ({chans})")
        self.pose_combo.config(values=names)
        if names and self.step_pose.get() not in names:
            self.step_pose.set(names[0])

    def selected_pose(self) -> str | None:
        sel = self.pose_list.curselection()
        return sorted(self.cfg.poses)[sel[0]] if sel else None

    def save_pose(self) -> None:
        pose = {str(ch): self.target[ch] for ch in range(usc.CHANNELS)
                if self.enabled[ch].get() and self.cfg.channels[ch].mode == "servo"}
        if not pose:
            self.seq_lbl.config(text="turn some channels on first", foreground=ERROR)
            return
        # no spaces: pose names are also words in the board's script language
        name = "_".join(self.pose_name.get().split()) or f"pose_{len(self.cfg.poses) + 1}"
        self.cfg.poses[name] = pose
        self.save_config()
        self.refresh_poses()
        self.pose_name.set("")
        self.seq_lbl.config(text=f"saved '{name}'", foreground=MUTED)

    def apply_pose(self, name: str, move_s: float = 0.0) -> None:
        self.override_speed.clear()
        for ch_s, us in self.cfg.poses.get(name, {}).items():
            ch = int(ch_s)
            if self.cfg.channels[ch].mode != "servo":
                continue
            if not self.enabled[ch].get():
                self.enabled[ch].set(True)
                self.on_toggle(ch)
            self.set_target(ch, us)
            cur = self.cmd[ch]
            if move_s > 0 and cur is not None and abs(self.target[ch] - cur) > 0:
                self.override_speed[ch] = abs(self.target[ch] - cur) / move_s

    def goto_pose(self) -> None:
        name = self.selected_pose()
        if name and self.dev:
            self.stop_sequence()
            self.apply_pose(name)

    def delete_pose(self) -> None:
        name = self.selected_pose()
        if not name:
            return
        del self.cfg.poses[name]
        self.cfg.sequence = [s for s in self.cfg.sequence if s["pose"] != name]
        self.save_config()
        self.refresh_poses()
        self.refresh_sequence()

    def refresh_sequence(self) -> None:
        self.seq_list.delete(0, "end")
        for i, s in enumerate(self.cfg.sequence, 1):
            self.seq_list.insert("end", f"{i:2d}.  {s['pose']}    move {s['move']:.1f} s"
                                        f"    hold {s['hold']:.1f} s")

    def add_step(self) -> None:
        pose = self.step_pose.get()
        if pose not in self.cfg.poses:
            self.seq_lbl.config(text="save a pose first", foreground=ERROR)
            return
        try:
            move, hold = float(self.step_move.get()), float(self.step_hold.get())
        except ValueError:
            self.seq_lbl.config(text="move and hold are seconds", foreground=ERROR)
            return
        if move < 0 or hold < 0:
            self.seq_lbl.config(text="times cannot be negative", foreground=ERROR)
            return
        sel = self.seq_list.curselection()
        at = sel[0] + 1 if sel else len(self.cfg.sequence)
        self.cfg.sequence.insert(at, {"pose": pose, "move": move, "hold": hold})
        self.save_config()
        self.refresh_sequence()
        self.seq_list.selection_set(at)

    def remove_step(self) -> None:
        sel = self.seq_list.curselection()
        if sel:
            del self.cfg.sequence[sel[0]]
            self.save_config()
            self.refresh_sequence()

    def move_step(self, delta: int) -> None:
        sel = self.seq_list.curselection()
        if not sel:
            return
        i, j = sel[0], sel[0] + delta
        if 0 <= j < len(self.cfg.sequence):
            seq = self.cfg.sequence
            seq[i], seq[j] = seq[j], seq[i]
            self.save_config()
            self.refresh_sequence()
            self.seq_list.selection_set(j)

    def on_repeat_change(self) -> None:
        try:
            n = int(self.repeat_var.get())
        except ValueError:
            return                       # mid-typing; keep the last good value
        if 0 <= n <= 9999 and n != self.cfg.repeat:
            self.cfg.repeat = n
            self.save_config()

    def play_sequence(self) -> None:
        if not self.dev or not self.cfg.sequence:
            self.seq_lbl.config(text="nothing to play" if self.dev else "not connected",
                                foreground=ERROR)
            return
        self.seq_playing = True
        self.seq_idx = -1
        self.seq_pass = 1
        self.next_step(time.monotonic())

    def stop_sequence(self) -> None:
        if self.seq_playing:
            self.seq_lbl.config(text="stopped", foreground=MUTED)
        self.seq_playing = False
        self.override_speed.clear()
        self.seq_list.selection_clear(0, "end")

    def next_step(self, now: float) -> None:
        self.seq_idx += 1
        if self.seq_idx >= len(self.cfg.sequence):
            if self.cfg.repeat and self.seq_pass >= self.cfg.repeat:
                self.stop_sequence()
                self.seq_lbl.config(text=f"done ({self.seq_pass}x)", foreground=MUTED)
                return
            self.seq_pass += 1
            self.seq_idx = 0
        step = self.cfg.sequence[self.seq_idx]
        self.apply_pose(step["pose"], step["move"])
        self.seq_step_end = now + step["move"] + step["hold"]
        self.seq_list.selection_clear(0, "end")
        self.seq_list.selection_set(self.seq_idx)
        self.seq_list.see(self.seq_idx)
        passes = f"{self.cfg.repeat}" if self.cfg.repeat else "∞"
        self.seq_lbl.config(text=f"pass {self.seq_pass}/{passes}, step "
                                 f"{self.seq_idx + 1}/{len(self.cfg.sequence)}: "
                                 f"{step['pose']}", foreground=OK_GREEN)

    # -- script stored on the board ------------------------------------------

    def set_board(self, text: str, foreground: str = MUTED) -> None:
        """Board-script status, shown on both the Poses and the Script tab."""
        self.board_lbl.config(text=text, foreground=foreground)
        if hasattr(self, "script_board_lbl"):
            self.script_board_lbl.config(text=text, foreground=foreground)

    def upload_code(self, text: str, what: str) -> bool:
        if not self.dev:
            self.set_board(text="not connected", foreground=ERROR)
            return False
        try:
            code, warnings = servoscript.compile_script(text, self.cfg)
        except servoscript.ScriptError as exc:
            self.set_board(text=f"script error: {exc}", foreground=ERROR)
            return False
        self.stop_sequence()
        try:
            crc = self.dev.script_upload(code, autorun=self.autorun_var.get())
            ok = [tuple(x) for x in self.dev.script_read()] == [tuple(x) for x in code]
        except IO_ERRORS as exc:
            self.lost(exc)
            return False
        self.board_running = False
        text = (f"{what}: {len(code)} instructions stored (CRC {crc:08X})"
                + (", autorun on" if self.autorun_var.get() else "")
                + ("" if ok else " -- READ-BACK MISMATCH"))
        if warnings:
            text += "  |  " + "; ".join(warnings[:2])
        self.set_board(text=text, foreground=OK_GREEN if ok else ERROR)
        return ok

    def upload_sequence(self) -> None:
        try:
            text = servoscript.from_gui(self.cfg)
        except servoscript.ScriptError as exc:
            self.set_board(text=str(exc), foreground=ERROR)
            return
        self.upload_code(text, "sequence")

    def upload_script_file(self) -> None:
        path = filedialog.askopenfilename(
            parent=self.root, filetypes=[("Servo script", "*.txt"), ("All files", "*.*")],
            initialdir=str(EXAMPLES_DIR))
        if path:
            self.upload_code(Path(path).read_text(encoding="utf-8"), Path(path).name)

    def board_run(self) -> None:
        if not self.dev:
            return
        self.stop_sequence()
        try:
            self.dev.script_run()
        except usc.DeviceError:
            self.set_board(text="no script stored on the board", foreground=ERROR)
            return
        except IO_ERRORS as exc:
            self.lost(exc)
            return
        self.board_running = True
        self.set_board(text="board script running", foreground=OK_GREEN)

    def board_stop(self) -> None:
        if not self.dev:
            return
        try:
            self.dev.script_stop()
        except IO_ERRORS as exc:
            self.lost(exc)
            return
        self.board_running = False
        self.set_board(text="board script stopped (servos hold)", foreground=MUTED)

    def mirror_board(self) -> None:
        """While the board runs its own script, show its positions, and adopt
        them as ours so the GUI never sends anything that fights it."""
        info = self.dev.script_info()
        self.sync_from_device()
        if info.state != "running":
            self.board_running = False
            self.set_board(text=f"board script {info.state}", foreground=MUTED)

    # -- script editor ------------------------------------------------------

    def set_script_text(self, text: str) -> None:
        """Replace the editor contents as one undoable step."""
        self.editor.delete("1.0", "end")
        self.editor.insert("1.0", text)
        self.editor.edit_separator()
        self.highlight_script()

    def script_text(self) -> str:
        return self.editor.get("1.0", "end-1c")

    def update_script_title(self) -> None:
        name = self.script_path.name if self.script_path else "untitled"
        self.script_file_lbl.config(text=name + ("  *" if self.script_dirty else ""))

    def on_script_modified(self, _event=None) -> None:
        if not self.editor.edit_modified():
            return
        self.editor.edit_modified(False)
        self.script_dirty = True
        self.update_script_title()
        if self.script_highlight_id:
            self.root.after_cancel(self.script_highlight_id)
        self.script_highlight_id = self.root.after(150, self.highlight_script)

    def highlight_script(self) -> None:
        """Colour comments, statements and labels; clear old error marks."""
        self.script_highlight_id = None
        ed = self.editor
        for tag in ("comment", "keyword", "label", "error"):
            ed.tag_remove(tag, "1.0", "end")
        for n, line in enumerate(self.script_text().split("\n"), 1):
            code, hash_, _ = line.partition("#")
            if hash_:
                ed.tag_add("comment", f"{n}.{len(code)}", f"{n}.end")
            stripped = code.strip()
            if not stripped:
                continue
            start = len(code) - len(code.lstrip())
            if stripped.endswith(":") and " " not in stripped:
                ed.tag_add("label", f"{n}.{start}", f"{n}.{start + len(stripped)}")
                continue
            word = stripped.split()[0]
            if word.lower() in servoscript.KEYWORDS:
                ed.tag_add("keyword", f"{n}.{start}", f"{n}.{start + len(word)}")

    def script_output(self, parts: list) -> None:
        """parts: (text, tag) pairs; tag '' = plain, 'err', 'warn' or 'ok'."""
        out = self.script_out
        out.config(state="normal")
        out.delete("1.0", "end")
        for text, tag in parts:
            out.insert("end", text + "\n", tag or ())
        out.config(state="disabled")

    def script_check(self) -> list | None:
        """Compile the editor text; show the listing or mark the bad line."""
        self.highlight_script()
        try:
            code, warnings = servoscript.compile_script(self.script_text(), self.cfg)
        except servoscript.ScriptError as exc:
            m = re.match(r"line (\d+)", str(exc))
            if m:
                n = int(m.group(1))
                self.editor.tag_add("error", f"{n}.0", f"{n}.end+1c")
                self.editor.see(f"{n}.0")
                self.editor.mark_set("insert", f"{n}.0")
            self.script_output([(f"error: {exc}", "err")])
            return None
        self.script_output(
            [(f"warning: {w}", "warn") for w in warnings]
            + [(servoscript.listing(code, self.cfg), "")]
            + [(f"OK -- {len(code)}/{usc.SCRIPT_MAX_INSTR} instructions", "ok")])
        return code

    def script_upload(self, run: bool = False) -> None:
        if self.script_check() is None:
            return
        name = self.script_path.name if self.script_path else "script"
        if self.upload_code(self.script_text(), name) and run:
            self.board_run()

    def script_new(self) -> None:
        self.script_path = None
        self.set_script_text(SCRIPT_TEMPLATE)
        self.script_dirty = True
        self.update_script_title()

    def script_open(self) -> None:
        start = self.script_path.parent if self.script_path else \
            EXAMPLES_DIR
        path = filedialog.askopenfilename(
            parent=self.root, initialdir=str(start),
            filetypes=[("Servo script", "*.txt"), ("All files", "*.*")])
        if not path:
            return
        self.script_path = Path(path)
        self.set_script_text(self.script_path.read_text(encoding="utf-8"))
        self.script_dirty = False
        self.update_script_title()
        self.script_check()

    def script_save(self) -> None:
        if not self.script_path:
            self.script_save_as()
            return
        try:
            self.script_path.write_text(self.script_text(), encoding="utf-8")
        except OSError as exc:
            self.script_output([(f"cannot save: {exc}", "err")])
            return
        self.script_dirty = False
        self.update_script_title()
        self.cfg.script_path = str(self.script_path)
        self.save_config()

    def script_save_as(self) -> None:
        start = self.script_path.parent if self.script_path else \
            EXAMPLES_DIR
        path = filedialog.asksaveasfilename(
            parent=self.root, initialdir=str(start), defaultextension=".txt",
            filetypes=[("Servo script", "*.txt")],
            initialfile=self.script_path.name if self.script_path else "myscript.txt")
        if path:
            self.script_path = Path(path)
            self.script_save()

    def script_from_sequence(self) -> None:
        try:
            text = servoscript.from_gui(self.cfg)
        except servoscript.ScriptError as exc:
            self.script_output([(str(exc), "err")])
            return
        self.script_path = None
        self.set_script_text(text)
        self.script_dirty = True
        self.update_script_title()
        self.script_check()

    def script_read_board(self) -> None:
        if not self.dev:
            self.script_output([("not connected", "err")])
            return
        try:
            code = self.dev.script_read()
            info = self.dev.script_info()
        except IO_ERRORS as exc:
            self.lost(exc)
            return
        if not code:
            self.script_output([("no script stored on the board", "warn")])
            return
        self.script_path = None
        self.set_script_text(servoscript.decompile(code, self.cfg))
        self.script_dirty = True
        self.update_script_title()
        self.autorun_var.set(info.autorun)
        self.script_output([(f"read {len(code)} instructions from the board "
                             f"(autorun {'on' if info.autorun else 'off'}, {info.state}). "
                             f"Undo (Ctrl+Z) brings back what was here.", "ok")])

    def script_reference(self) -> None:
        self.script_output([(servoscript.REFERENCE, "")])

    # -- recording ----------------------------------------------------------

    def toggle_record(self) -> None:
        if self.log_file:
            self.log_file.close()
            self.log_file = self.log_writer = None
            self.rec_btn.config(text="Record CSV...")
            self.rec_lbl.config(text="recording saved", foreground=MUTED)
            return
        path = filedialog.asksaveasfilename(
            parent=self.root, defaultextension=".csv", filetypes=[("CSV", "*.csv")],
            initialfile=f"servolog_{time.strftime('%Y%m%d_%H%M%S')}.csv")
        if not path:
            return
        try:
            self.log_file = open(path, "w", newline="", encoding="utf-8")
        except OSError as exc:
            self.rec_lbl.config(text=f"cannot write: {exc}", foreground=ERROR)
            return
        self.log_writer = csv.writer(self.log_file)
        self.log_writer.writerow(
            ["t_s", "current_mA", "voltage_mV", "faults"]
            + [f"{c.name}_us" for c in self.cfg.channels]
            + [f"{self.cfg.channels[ch].name}_adc" for ch in range(usc.ANALOG_CHANNELS)])
        self.log_t0 = self.log_flushed = time.monotonic()
        self.rec_btn.config(text="Stop recording")
        self.rec_lbl.config(text=f"recording to {Path(path).name}", foreground=PEAK)

    def record(self, now: float, st: usc.Status) -> None:
        if not self.log_writer:
            return
        self.log_writer.writerow(
            [f"{now - self.log_t0:.3f}", st.current_ma, st.voltage_mv, st.faults]
            + [self.sent[ch] or 0 for ch in range(usc.CHANNELS)]
            + ["" if self.analog[ch] is None or self.cfg.channels[ch].mode != "input"
               else self.analog[ch] for ch in range(usc.ANALOG_CHANNELS)])
        if now - self.log_flushed > 1.0:
            self.log_file.flush()
            self.log_flushed = now

    # -- firmware -----------------------------------------------------------

    def browse_image(self) -> None:
        path = filedialog.askopenfilename(
            parent=self.root, filetypes=[("Application image", "*.bin")],
            initialdir=str(Path(self.image_var.get()).parent))
        if path:
            self.image_var.set(path)

    def log_flash(self, text: str) -> None:
        self.flash_log.config(state="normal")
        self.flash_log.insert("end", text + "\n")
        self.flash_log.see("end")
        self.flash_log.config(state="disabled")

    def flash_firmware(self) -> None:
        if self.flashing:
            return
        image = Path(self.image_var.get())
        self.flash_log.config(state="normal")
        self.flash_log.delete("1.0", "end")
        self.flash_log.config(state="disabled")
        if self.mock:
            self.log_flash("not available with --mock")
            return
        if not image.is_file():
            self.log_flash(f"no such file: {image}")
            return
        self.cfg.flash_image = str(image)
        self.save_config()

        self.close_device()                 # stops every channel, frees the port
        self.set_state("flashing firmware...", PEAK)
        self.flashing = True
        self.flash_btn.state(["disabled"])
        # A frozen build has no python to run flash.py with; it re-runs itself.
        cmd = [sys.executable, FLASH_ARG if FROZEN else str(FLASH_SCRIPT), str(image)]
        if self.port:
            cmd += ["--port", self.port]
        threading.Thread(target=self.flash_worker, args=(cmd,), daemon=True).start()
        self.root.after(100, self.pump_flash_log)

    def flash_worker(self, cmd: list) -> None:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    creationflags=flags)
        except OSError as exc:
            self.flash_queue.put(f"cannot run flash.py: {exc}")
            self.flash_queue.put(("done", 1))
            return
        buf = b""
        while True:
            chunk = proc.stdout.read1(256) if hasattr(proc.stdout, "read1") \
                else proc.stdout.read(1)
            if not chunk:
                break
            buf += chunk
            *lines, buf = re.split(rb"[\r\n]+", buf)
            for line in lines:
                if line.strip():
                    self.flash_queue.put(line.decode("utf-8", "replace"))
        if buf.strip():
            self.flash_queue.put(buf.decode("utf-8", "replace"))
        self.flash_queue.put(("done", proc.wait()))

    def pump_flash_log(self) -> None:
        while True:
            try:
                item = self.flash_queue.get_nowait()
            except queue.Empty:
                break
            if isinstance(item, tuple):
                rc = item[1]
                self.flashing = False
                self.flash_btn.state(["!disabled"])
                self.log_flash("-- done --" if rc == 0 else f"-- FAILED (exit {rc}) --")
                self.root.after(300, self.connect)
                return
            self.log_flash(item)
        self.root.after(100, self.pump_flash_log)

    # -- tick ---------------------------------------------------------------

    def schedule(self) -> None:
        self.after_id = self.root.after(POLL_MS, self.poll)

    def poll(self) -> None:
        if not self.dev:
            return
        now = time.monotonic()
        dt, self.last_tick = now - self.last_tick, now
        try:
            if self.seq_playing and now >= self.seq_step_end:
                self.next_step(now)
            self.tick += 1
            if self.board_running:
                if self.tick % 4 == 0:
                    self.mirror_board()
            else:
                self.drive(dt)
            st = self.dev.status()
            if not st.ok:
                # The firmware drops every channel on a fault; show that.
                self.stop_sequence()
                self.sync_from_device()
            self.read_inputs()
        except IO_ERRORS as exc:
            self.lost(exc)
            return

        self.history.append((now, st.current_ma))
        while self.history and now - self.history[0][0] > HISTORY_S:
            self.history.popleft()
        self.peak_ma = max(self.peak_ma, st.current_ma)
        self.record(now, st)

        self.current_lbl.config(text=f"{st.current_ma} mA")
        self.voltage_lbl.config(text=f"{st.voltage_mv / 1000:.2f} V" if st.rail_present
                                else "no rail")
        self.peak_lbl.config(text=f"peak {self.peak_ma} mA")
        if not st.ok:
            self.set_state("FAULT: " + ", ".join(st.fault_names) +
                           " -- all channels dropped; Clear faults to continue", ERROR)
        elif str(self.state_lbl.cget("foreground")) == ERROR:
            self.set_state("OK", OK_GREEN)
        self.draw_graph(now)
        self.schedule()

    def draw_graph(self, now: float) -> None:
        c = self.canvas
        c.delete("all")
        w, h = max(c.winfo_width(), 100), max(c.winfo_height(), 60)
        left, bottom, top = 44, h - 18, 8
        top_ma = max([500, self.peak_ma] + [ma for _, ma in self.history]) * 1.1
        step = 250 if top_ma <= 1500 else 500 if top_ma <= 4000 else 1000
        top_ma = step * (int(top_ma // step) + 1)

        def y(ma: float) -> float:
            return bottom - (bottom - top) * ma / top_ma

        for ma in range(0, int(top_ma) + 1, step):
            c.create_line(left, y(ma), w - 4, y(ma), fill=GRID)
            c.create_text(left - 4, y(ma), text=str(ma), anchor="e",
                          font=("Segoe UI", 8), fill=MUTED)
        for s in range(0, int(HISTORY_S) + 1, 2):
            x = w - 4 - (w - 4 - left) * s / HISTORY_S
            c.create_text(x, h - 8, text=f"-{s}s" if s else "now",
                          anchor="center" if s else "e",
                          font=("Segoe UI", 8), fill=MUTED)
        c.create_text(left + 4, top, text="mA", anchor="nw",
                      font=("Segoe UI", 8), fill=MUTED)

        pts = []
        for t, ma in self.history:
            x = w - 4 - (w - 4 - left) * (now - t) / HISTORY_S
            pts += [x, y(ma)]
        if len(pts) >= 4:
            if self.peak_ma:
                c.create_line(left, y(self.peak_ma), w - 4, y(self.peak_ma),
                              fill=PEAK, dash=(4, 3))
            c.create_line(*pts, fill=ACCENT, width=2)

    def on_close(self) -> None:
        if self.log_file:
            self.log_file.close()
        # keep the editor text, saved to a file or not
        self.cfg.script_draft = self.script_text()
        self.cfg.script_path = str(self.script_path) if self.script_path else ""
        self.save_config()
        self.close_device()
        self.root.destroy()


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == [FLASH_ARG]:
        import flash
        return flash.main(argv[1:])

    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", help="serial port; default is auto-detect")
    p.add_argument("--mock", action="store_true", help="run against the emulator")
    args = p.parse_args(argv)

    windows_dpi_aware()
    root = tk.Tk()
    ServoGui(root, args.port, args.mock)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
