"""Entry point for the standalone Windows build (see build-windows.ps1)."""

import os
import sys


def unblock_own_files() -> None:
    """Remove the "downloaded from the internet" mark from the app's own DLL files.

    Windows adds that mark to every file unzipped from a download, and the .NET
    runtime then refuses to load the app's .NET components (pythonnet and the
    WebView2 window), so the window cannot open. This does what right-click >
    Properties > Unblock does, and only for files inside the app's own folder.
    """
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(sys.executable)
    for folder, _, names in os.walk(base):
        for name in names:
            if name.lower().endswith(".dll"):
                try:
                    os.remove(os.path.join(folder, name) + ":Zone.Identifier")
                except OSError:
                    pass  # not marked, or the folder is read-only (the error shown later says what to do)


if __name__ == "__main__":
    unblock_own_files()
    from uncle_virgil.__main__ import main

    main()
