"""A keyboard shortcut that works from any program (Windows only).

Uses the Windows RegisterHotKey function, so no keyboard hook is installed
and no other key presses are seen by this app: Windows tells it only when
the chosen combination is pressed.
"""

from __future__ import annotations

import logging
import sys
import threading
from typing import Callable

log = logging.getLogger(__name__)

MODIFIERS = {"ctrl": 0x0002, "control": 0x0002, "alt": 0x0001, "shift": 0x0004, "win": 0x0008}
MOD_NOREPEAT = 0x4000  # holding the keys down does not repeat the action
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
_NAMED_KEYS = {"space": 0x20, "pause": 0x13, "insert": 0x2D, "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22}


def supported() -> bool:
    return sys.platform == "win32"


def parse(combo: str) -> tuple[int, int]:
    """'Ctrl+Alt+D' -> (modifier flags, virtual-key code). Raises ValueError for anything else."""
    parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
    if len(parts) < 2:
        raise ValueError("Use at least one of Ctrl, Alt, Shift or Win plus a key, for example Ctrl+Alt+D")
    *mods, key = parts
    flags = 0
    for m in mods:
        if m not in MODIFIERS:
            raise ValueError(f"Unknown modifier {m!r}; use Ctrl, Alt, Shift or Win")
        flags |= MODIFIERS[m]
    if len(key) == 1 and key.isalnum():
        vk = ord(key.upper())
    elif key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        vk = 0x70 + int(key[1:]) - 1
    elif key in _NAMED_KEYS:
        vk = _NAMED_KEYS[key]
    else:
        raise ValueError(f"Unknown key {key!r}; use a letter, a digit, F1 to F24, Space, or Pause")
    return flags, vk


class Hotkey:
    """Calls `callback` (on its own thread) each time the combination is pressed, until stopped."""

    def __init__(self, combo: str, callback: Callable[[], None]):
        self.combo = combo
        self.flags, self.vk = parse(combo)
        self.callback = callback
        self.error = ""
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._ready = threading.Event()

    def start(self) -> bool:
        """Register the shortcut. Returns False (and sets `error`) if Windows refuses it."""
        self._thread = threading.Thread(target=self._loop, daemon=True, name="hotkey")
        self._thread.start()
        self._ready.wait(5)
        return not self.error

    def _loop(self) -> None:
        import ctypes
        from ctypes import wintypes

        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        self._thread_id = kernel32.GetCurrentThreadId()
        # The shortcut belongs to this thread: Windows posts WM_HOTKEY to its message queue.
        if not user32.RegisterHotKey(None, 1, self.flags | MOD_NOREPEAT, self.vk):
            self.error = f"{self.combo} is already used by another program. Choose a different shortcut in Settings."
            self._ready.set()
            return
        self._ready.set()
        msg = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY:
                    threading.Thread(target=self._fire, daemon=True).start()
        finally:
            user32.UnregisterHotKey(None, 1)

    def _fire(self) -> None:
        try:
            self.callback()
        except Exception:
            log.exception("shortcut action failed")

    def stop(self) -> None:
        if self._thread and self._thread.is_alive() and self._thread_id:
            import ctypes

            ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
            self._thread.join(timeout=5)


def beep(starting: bool) -> None:
    """A short system sound, so a shortcut press is noticed even with the window hidden."""
    if not supported():
        return
    import winsound

    winsound.MessageBeep(winsound.MB_OK if starting else winsound.MB_ICONASTERISK)
