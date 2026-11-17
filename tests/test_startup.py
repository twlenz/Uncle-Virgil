"""Start the real program the way a user does, and check it answers."""

import os
import socket
import subprocess
import sys
import time

import httpx


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_service_starts_and_answers(tmp_path):
    port = free_port()
    env = {**os.environ, "UNCLE_VIRGIL_MODELS": str(tmp_path / "models")}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uncle_virgil", "--no-window", "--home", str(tmp_path / "home"), "--port", str(port)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env,
    )
    try:
        line = proc.stdout.readline()
        assert "running at" in line, line
        token = line.strip().rsplit("token=", 1)[1]
        for _ in range(50):
            try:
                r = httpx.get(f"http://127.0.0.1:{port}/api/status", headers={"Authorization": f"Bearer {token}"}, timeout=2)
                break
            except httpx.ConnectError:
                time.sleep(0.1)
        assert r.status_code == 200 and r.json()["mode"] == "idle"
        assert httpx.get(f"http://127.0.0.1:{port}/api/status", timeout=2).status_code == 401  # nothing answers without the token
        page = httpx.get(f"http://127.0.0.1:{port}/?token={token}", timeout=2)
        assert page.status_code == 200 and "<title>Uncle Virgil</title>" in page.text
        assert (tmp_path / "home" / "uncle-virgil.log").exists()
    finally:
        proc.terminate()
        proc.wait(timeout=10)
