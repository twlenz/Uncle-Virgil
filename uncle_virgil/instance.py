"""Make sure only one copy of the app works on a data folder at a time.

Two copies would both record from the microphone, write the same files,
and send every reminder twice. The lock is held by the operating system on
an open file, so it is released automatically if the app crashes.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_held = []  # keep the open lock file alive for the life of the process


def acquire(home: Path) -> bool:
    """Take the lock for `home`. Returns False if another copy already holds it."""
    path = Path(home) / "uncle-virgil.lock"
    f = open(path, "a+")
    try:
        if sys.platform == "win32":
            import msvcrt

            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return False
    f.seek(0)
    f.truncate()
    f.write(str(os.getpid()))
    f.flush()
    _held.append(f)
    return True


def tell_already_running(show_box: bool) -> None:
    message = "Uncle Virgil is already running. Look for its window in the taskbar."
    if show_box and sys.platform == "win32":  # started from the shortcut or at sign-in: no console to print to
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, "Uncle Virgil", 0x40)  # information icon
    print(message, file=sys.stderr, flush=True)


def lower_priority() -> None:
    """Run below normal priority, so transcription never slows down what you are doing.

    Audio capture is unaffected: the sound system runs its own high-priority thread."""
    try:
        if sys.platform == "win32":
            import ctypes

            kernel32 = ctypes.windll.kernel32
            kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), 0x4000)  # BELOW_NORMAL_PRIORITY_CLASS
        else:
            os.nice(5)
    except OSError:
        pass
