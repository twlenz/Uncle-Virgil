"""Start Uncle Virgil: the local service plus its desktop window."""

from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import socket
import sys
import threading
import time
from pathlib import Path

import uvicorn

from .app import App
from .config import default_home
from .server import create_app


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> None:
    # Started without a console (pythonw.exe), Python has no output streams; give it harmless ones.
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))

    parser = argparse.ArgumentParser(prog="uncle-virgil", description=__doc__)
    parser.add_argument("--home", type=Path, default=default_home(), help="data folder (default: %(default)s)")
    parser.add_argument("--port", type=int, default=0, help="port for the local service (default: any free port)")
    parser.add_argument("--no-window", action="store_true", help="run the service without the desktop window")
    parser.add_argument("--minimized", action="store_true", help="open the window minimized (used when starting with Windows)")
    parser.add_argument("--download-models", action="store_true", help="download the speech models, then exit")
    parser.add_argument("--self-test", action="store_true", help="open the window, check the interface loads and reaches the service, then exit")
    args = parser.parse_args()

    if args.download_models:
        from .config import load_settings
        from .models import ModelManager, models_dir

        ModelManager(models_dir(args.home)).download_now(load_settings(args.home).speech.asr_model)
        return

    args.home.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(args.home / "uncle-virgil.log", encoding="utf-8"), logging.StreamHandler()],
    )

    logging.getLogger("httpx").setLevel(logging.WARNING)  # it logs every request URL at INFO

    app = App(args.home)
    token = secrets.token_urlsafe(24)
    port = args.port or _free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(app, token), host="127.0.0.1", port=port, log_level="warning", log_config=None))
    app.start()
    url = f"http://127.0.0.1:{port}/?token={token}"

    try:
        if args.no_window:
            print(f"Uncle Virgil is running at {url}", flush=True)
            server.run()
            return
        import webview  # imported late so --no-window works without a GUI toolkit

        threading.Thread(target=server.run, daemon=True, name="server").start()
        while not server.started:
            time.sleep(0.05)
        window = webview.create_window("Uncle Virgil", url, width=1180, height=800, min_size=(820, 560), minimized=args.minimized)

        def on_closing(window):
            # Closing the window quits the app, which stops recording; ask first if something is recording.
            window.confirm_close = app.recorder.mode != "idle"

        window.events.closing += on_closing
        threading.Thread(target=_show_recording_in_title, args=(window, app), daemon=True, name="title").start()
        result = [0]
        webview.start(
            _self_test if args.self_test else None,
            (window, app, result) if args.self_test else None,
            localization={"global.quitConfirmation": "Uncle Virgil is recording. Closing the window stops recording. Close anyway?"},
        )
    finally:
        app.stop()
        server.should_exit = True
    if args.self_test:
        sys.exit(result[0])


TITLES = {"idle": "Uncle Virgil", "continuous": "● Listening · Uncle Virgil", "dictation": "● Dictating · Uncle Virgil", "sample": "● Recording voice sample · Uncle Virgil"}


def _show_recording_in_title(window, app) -> None:
    """Keep the window title, which the taskbar shows, saying whether the microphone is in use."""
    shown = None
    while True:
        title = TITLES.get(app.recorder.mode, TITLES["idle"])
        if title != shown:
            try:
                window.set_title(title)
                shown = title
            except Exception:  # the window is not ready yet, or is closing
                pass
        time.sleep(0.5)


def _self_test(window, app, result: list) -> None:
    """Check that the real window loads the interface and reaches the service, and that the speech
    engine runs if its models are installed. Used in CI on Windows, and for diagnosing an installation.
    The outcome is printed and written to the log (a windowed .exe has nowhere to print)."""
    import numpy as np

    result[0] = 1
    data = None

    def report(text: str) -> None:
        print(text, flush=True)
        logging.getLogger("uncle_virgil.self_test").info(text)

    try:
        if app.engine.ready():
            app.engine.new_vad(0.5)(np.zeros(512, dtype=np.float32))
            app.engine.transcribe(np.zeros(2 * 16000, dtype=np.float32))
            report("Self-test: the speech engine loaded and ran.")
        else:
            report("Self-test: speech models are not installed, so the speech engine was not checked.")
        if not window.events.loaded.wait(60):
            raise RuntimeError("the page did not load within 60 seconds")
        deadline = time.time() + 30
        while time.time() < deadline:
            raw = window.evaluate_js(
                "JSON.stringify({h1: document.querySelector('h1') && document.querySelector('h1').textContent,"
                " nav: document.querySelectorAll('#nav button').length,"
                " mode: typeof state !== 'undefined' && state.status ? state.status.mode : null})"
            )
            data = json.loads(raw) if isinstance(raw, str) else raw
            if data and data.get("h1") == "Today" and data.get("nav") == 7 and data.get("mode"):
                report(f"Self-test passed: the window shows the interface and it reached the service (recorder {data['mode']}).")
                result[0] = 0
                return
            time.sleep(0.5)
        report(f"Self-test failed: the interface did not finish loading (last state: {data}).")
    except Exception as e:
        report(f"Self-test failed: {type(e).__name__}: {e}")
    finally:
        window.destroy()


if __name__ == "__main__":
    main()
