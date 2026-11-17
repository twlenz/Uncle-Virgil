"""Start Uncle Virgil when the user signs in to Windows.

Uses the per-user "Run" registry key, the standard place for this; no
administrator rights are needed and nothing outside the user's own
registry hive is touched.
"""

from __future__ import annotations

import sys
from pathlib import Path

_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_NAME = "Uncle Virgil"


def supported() -> bool:
    return sys.platform == "win32"


def command(home: Path) -> str:
    """The command Windows runs at sign-in: this app, without a console window, minimized."""
    if getattr(sys, "frozen", False):  # the standalone build: run the .exe itself
        return f'"{sys.executable}" --minimized --home "{home}"'
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    if pythonw.exists():
        exe = pythonw
    return f'"{exe}" -m uncle_virgil --minimized --home "{home}"'


def enabled() -> bool:
    if not supported():
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _KEY) as key:
            winreg.QueryValueEx(key, _NAME)
            return True
    except FileNotFoundError:
        return False


def set_enabled(on: bool, home: Path) -> None:
    if not supported():
        raise RuntimeError("Starting automatically is only available on Windows")
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _KEY) as key:
        if on:
            winreg.SetValueEx(key, _NAME, 0, winreg.REG_SZ, command(home))
        else:
            try:
                winreg.DeleteValue(key, _NAME)
            except FileNotFoundError:
                pass
