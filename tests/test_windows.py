"""Checks of the Windows-only parts, against the real system. Run on Windows (in CI, on a GitHub Windows runner)."""

import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")


def test_start_with_windows_round_trip_in_the_real_registry(tmp_path):
    from uncle_virgil import autostart

    was_on = autostart.enabled()
    try:
        autostart.set_enabled(True, tmp_path)
        assert autostart.enabled()
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
            command = winreg.QueryValueEx(key, "Uncle Virgil")[0]
        assert "pythonw.exe" in command.lower() and "-m uncle_virgil --minimized" in command
        autostart.set_enabled(False, tmp_path)
        assert not autostart.enabled()
    finally:
        autostart.set_enabled(was_on, tmp_path)


def test_keys_are_kept_in_windows_credential_manager():
    import keyring

    from uncle_virgil.config import get_secret, set_secret

    assert "Windows" in type(keyring.get_keyring()).__module__ + type(keyring.get_keyring()).__name__
    set_secret("ntfy_token", "test-value-123")
    try:
        assert get_secret("ntfy_token") == "test-value-123"
    finally:
        set_secret("ntfy_token", "")
    assert get_secret("ntfy_token") == ""


def test_desktop_notifications_use_the_windows_backend():
    from desktop_notifier import DesktopNotifier

    notifier = DesktopNotifier(app_name="Uncle Virgil")
    assert "winrt" in type(notifier._backend).__module__.lower()


def test_window_library_and_its_windows_backend_load():
    import webview

    assert int(webview.__version__.split(".")[0]) >= 5 if hasattr(webview, "__version__") else True
    from webview.platforms import winforms  # loads .NET and the WebView2 component

    assert winforms is not None


def test_microphones_can_be_listed():
    from uncle_virgil.recorder import Recorder

    assert isinstance(Recorder.input_devices(), list)  # a CI machine may have none; this must not crash


def test_dictation_shortcut_fires_when_the_keys_are_pressed():
    import ctypes
    import threading

    from uncle_virgil.hotkey import Hotkey

    pressed = threading.Event()
    combo, keys = "Ctrl+Alt+Shift+F9", [0x11, 0x12, 0x10, 0x78]  # unlikely to be taken on any machine
    shortcut = Hotkey(combo, pressed.set)
    assert shortcut.start(), shortcut.error
    try:
        user32 = ctypes.windll.user32
        for k in keys:
            user32.keybd_event(k, 0, 0, 0)
        for k in reversed(keys):
            user32.keybd_event(k, 0, 2, 0)  # key up
        assert pressed.wait(5), "the shortcut did not fire"
        other = Hotkey(combo, lambda: None)  # a second program asking for the same keys
        assert not other.start() and "already used" in other.error
    finally:
        shortcut.stop()
    again = Hotkey(combo, lambda: None)
    assert again.start(), "the shortcut was not released when stopped"
    again.stop()


MARK = "[ZoneTransfer]\r\nZoneId=3\r\n"  # what Windows writes on files unzipped from a download

LOAD_SCRIPT = r"""
import sys
from clr_loader import get_netfx
runtime = get_netfx()
try:
    runtime.get_assembly(sys.argv[1]).get_function("Python.Runtime.Loader.Initialize")
    print("LOADED")
except RuntimeError as e:
    print("FAILED:", e)
"""


def _marked_copy_of_pythonnet(tmp_path):
    import shutil

    import pythonnet

    source = Path(pythonnet.__file__).parent / "runtime"
    target = tmp_path / "runtime"
    shutil.copytree(source, target)
    for dll in target.glob("*.dll"):
        with open(str(dll) + ":Zone.Identifier", "w") as f:
            f.write(MARK)
    return target


def _load_in_fresh_process(dll):
    import subprocess

    out = subprocess.run([sys.executable, "-c", LOAD_SCRIPT, str(dll)], capture_output=True, text=True, timeout=120)
    return out.stdout + out.stderr


def test_a_downloaded_copy_of_the_dotnet_component_fails_to_load_exactly_as_reported(tmp_path):
    """Reproduces the reported error: .NET refuses a DLL that carries the downloaded-from-the-internet mark."""
    runtime_dir = _marked_copy_of_pythonnet(tmp_path)
    output = _load_in_fresh_process(runtime_dir / "Python.Runtime.dll")
    assert "Failed to resolve Python.Runtime.Loader.Initialize" in output, output


def test_unblocking_the_apps_own_files_lets_it_load(tmp_path, monkeypatch):
    import importlib.util

    runtime_dir = _marked_copy_of_pythonnet(tmp_path)
    spec = importlib.util.spec_from_file_location("launcher", Path(__file__).parent.parent / "packaging" / "launcher.py")
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    launcher.unblock_own_files()
    assert not Path(str(runtime_dir / "Python.Runtime.dll") + ":Zone.Identifier").exists()
    output = _load_in_fresh_process(runtime_dir / "Python.Runtime.dll")
    assert "LOADED" in output, output
