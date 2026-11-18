"""Safeguards against data loss and against disturbing the rest of the computer."""

import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime

import numpy as np
import pytest

from uncle_virgil import files
from uncle_virgil.app import App
from uncle_virgil.speech import BlockResult

from .conftest import seg, tone

D = "2026-11-17"


# --- power cut / damaged files ----------------------------------------------------------


def test_a_transcript_cut_off_mid_line_does_not_stop_the_app(tmp_path):
    day = tmp_path / "days" / D
    day.mkdir(parents=True)
    good = json.dumps(seg(D, "09:00:00", "Hello."))
    (day / "segments.jsonl").write_text(good + '\n{"id": "y", "start": "2026-11-17T09:0')  # power cut while writing
    app = App(tmp_path)
    app.start()
    try:
        assert [s["text"] for s in app.storage.load_segments(D)] == ["Hello."]
        app.storage.append_segments(D, [seg(D, "10:00:00", "After the power cut.")])
        assert [s["text"] for s in app.storage.load_segments(D)] == ["Hello.", "After the power cut."]  # new line not glued to the broken one
        assert app.storage.search("power cut")[0]["text"] == "After the power cut."
    finally:
        app.stop()


def test_damaged_settings_reminders_and_voiceprints_are_kept_aside_not_lost(tmp_path):
    (tmp_path / "config.toml").write_text("user_name = 'Tom\n[broken")
    (tmp_path / "reminders.json").write_text('[{"id": "a", "te')
    (tmp_path / "speakers.json").write_bytes(b"\xff\xfe garbage")
    files.set_aside.clear()
    app = App(tmp_path)  # starts with defaults instead of refusing to start
    assert app.reminder_store.all() == [] and app.speakers.speakers == []
    kept = sorted(p.name.split(".damaged-")[0] for p in tmp_path.glob("*.damaged-*"))
    assert kept == ["config.toml", "reminders.json", "speakers.json"]
    assert len(files.set_aside) == 3  # reported in the app
    app.update_settings({"user_name": "Tom"})  # saving new settings does not touch the kept copy
    assert any(p.read_text().startswith("user_name = 'Tom") for p in tmp_path.glob("config.toml.damaged-*"))


def test_saving_retries_when_windows_briefly_locks_the_file(tmp_path, monkeypatch):
    real_replace, calls = os.replace, []

    def flaky(src, dst):
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError("The process cannot access the file because it is being used by another process")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", flaky)
    files.atomic_write(tmp_path / "x.json", "{}")
    assert (tmp_path / "x.json").read_text() == "{}" and len(calls) == 3


# --- nothing deleted automatically ---------------------------------------------------------


def test_a_recording_with_no_recognized_words_is_kept_not_deleted(app, monkeypatch):
    monkeypatch.setattr(app.engine, "transcribe", lambda samples, assume_speech=False: BlockResult())
    app.storage.save_block_audio(datetime.fromisoformat(f"{D}T09:00:00"), tone(2))
    assert app.pipeline.process(D, "09-00-00", "dictation") == []
    assert [b for b, _ in app.storage.no_speech_blocks(D)] == ["09-00-00"]
    assert app.pipeline.untranscribed(D) == 0 and app.pipeline.recover() == 0  # not retried at every start
    usage = app.storage.audio_usage(datetime.fromisoformat(D), datetime.fromisoformat(f"{D}T23:59"))
    assert usage["files"] == 1  # still counted, and covered by "delete/compress audio in range"


# --- two copies ----------------------------------------------------------------------------


def test_a_second_copy_on_the_same_data_refuses_to_start(tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {**os.environ, "UNCLE_VIRGIL_MODELS": str(tmp_path / "models")}
    cmd = [sys.executable, "-m", "uncle_virgil", "--no-window", "--home", str(tmp_path / "home")]
    first = subprocess.Popen(cmd + ["--port", str(port)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
    try:
        assert "running at" in first.stdout.readline()
        second = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=60)
        assert second.returncode == 1 and "already running" in second.stderr
    finally:
        first.terminate()
        first.wait(timeout=10)
    third = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
    try:
        assert "running at" in third.stdout.readline()  # once the first has closed, it starts normally
    finally:
        third.terminate()
        third.wait(timeout=10)


# --- disk space ----------------------------------------------------------------------------


def test_recording_stops_before_the_drive_fills_and_resumes_when_space_is_freed(app, monkeypatch):
    free = {"bytes": 50_000_000_000}
    monkeypatch.setattr(app, "disk_free", lambda: free["bytes"])
    monkeypatch.setattr(app.engine, "ready", lambda: True)
    monkeypatch.setattr(app.engine, "new_vad", lambda threshold: lambda frame: False)
    monkeypatch.setattr(app.recorder, "_open_microphone", lambda: None)
    app.set_continuous(True)
    free["bytes"] = 600_000_000  # 0.6 GB left
    app.check_disk()
    assert app.recorder.mode == "idle" and app.recorder.error.startswith("Recording stopped: only 0.6 GB free")
    assert app.sent and app.sent[-1][0] == "Uncle Virgil stopped recording"
    with pytest.raises(RuntimeError, match="only 0.6 GB free"):
        app.start_capture("dictation")
    free["bytes"] = 20_000_000_000  # space freed
    app.check_disk()
    assert app.recorder.mode == "continuous" and app.recorder.error == ""


def test_low_disk_space_is_shown_before_it_becomes_a_problem(app, monkeypatch):
    from fastapi.testclient import TestClient

    from uncle_virgil.server import create_app

    monkeypatch.setattr(app, "disk_free", lambda: 3_200_000_000)
    status = TestClient(create_app(app, "t"), base_url="http://127.0.0.1", headers={"Authorization": "Bearer t"}).get("/api/status").json()
    assert status["disk_low"] is True and status["disk_free_gb"] == 3.2


# --- leaving room for the rest of the computer ---------------------------------------------------


def test_transcription_uses_at_most_half_the_processor(app, monkeypatch):
    for cores, expected in [(2, 1), (4, 2), (8, 4), (32, 4)]:
        monkeypatch.setattr(os, "cpu_count", lambda c=cores: c)
        assert app.engine._threads() == expected


def test_importing_a_long_recording_does_not_load_it_all_into_memory(tmp_path):
    import soundfile as sf

    from uncle_virgil import audio

    path = tmp_path / "long.wav"
    with sf.SoundFile(str(path), "w", 44100, 2, subtype="PCM_16") as f:
        for _ in range(60):  # 10 minutes of stereo at 44.1 kHz
            f.write(np.zeros((441000, 2), dtype=np.float32))
    seconds, pieces = audio.open_stream(path, tmp_path)
    assert abs(seconds - 600) < 0.01
    sizes = [len(p) for p in pieces]
    assert max(sizes) < 10.1 * 16000 and abs(sum(sizes) - 600 * 16000) < 200  # about 10-second pieces, nothing lost
