#!/usr/bin/env python3
"""
build_release.py -- package servogui.py as a standalone app with PyInstaller.

    python build_release.py            # on Windows -> dist/ServoGUI-windows-x64.zip
    python3 build_release.py           # on Linux   -> dist/ServoGUI-linux-x64.tar.gz

Builds for the OS it runs on; PyInstaller cannot cross-compile. The Linux build
runs on this PC under WSL (see README). Needs: pip install pyinstaller pyserial,
and on Linux the python3-tk package.

The archive holds the single-file executable, examples/ and the application
firmware image, so the Firmware tab works out of the box. servogui.json is
created next to the executable on first run.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
DIST = TOOLS / "dist"
FIRMWARE = TOOLS.parent / "embedded-ch32" / "build" / "USBServoController-ch32.bin"
NAME = "ServoGUI"


def main() -> int:
    windows = sys.platform == "win32"
    osname = "windows" if windows else "linux"
    arch = {"amd64": "x64", "x86_64": "x64", "aarch64": "arm64"}.get(
        platform.machine().lower(), platform.machine().lower())
    if not FIRMWARE.is_file():
        print(f"missing {FIRMWARE}; run make in embedded-ch32 first")
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        subprocess.run([
            sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
            "--onefile", "--windowed", "--name", NAME,
            "--distpath", str(tmp / "exe"), "--workpath", str(tmp / "work"),
            "--specpath", str(tmp), "--paths", str(TOOLS),
            "--hidden-import", "flash",          # imported lazily by --run-flash
            str(TOOLS / "servogui.py"),
        ], check=True)

        stage = tmp / f"{NAME}-{osname}-{arch}"
        stage.mkdir()
        exe = NAME + (".exe" if windows else "")
        shutil.copy2(tmp / "exe" / exe, stage / exe)
        shutil.copytree(TOOLS / "examples", stage / "examples")
        shutil.copy2(FIRMWARE, stage / FIRMWARE.name)

        DIST.mkdir(exist_ok=True)
        if windows:
            out = Path(shutil.make_archive(str(DIST / stage.name), "zip", tmp, stage.name))
        else:
            out = DIST / f"{stage.name}.tar.gz"
            with tarfile.open(out, "w:gz") as tar:    # keeps the exec bit
                tar.add(stage, arcname=stage.name)
    print(f"built {out} ({out.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
