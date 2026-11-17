"""Start Uncle Virgil: the local service plus its desktop window."""

from __future__ import annotations

import argparse
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
        webview.start(localization={"global.quitConfirmation": "Uncle Virgil is recording. Closing the window stops recording. Close anyway?"})
    finally:
        app.stop()
        server.should_exit = True


if __name__ == "__main__":
    main()
