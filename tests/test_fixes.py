"""Tests for the bug fixes and usability features added after the first review."""

import sys
import time
import types
from datetime import datetime, timedelta

import numpy as np
import pytest
from fastapi.testclient import TestClient

from uncle_virgil import autostart
from uncle_virgil.audio import SAMPLE_RATE
from uncle_virgil.server import create_app

from .conftest import seg, silence, tone

D = "2026-11-16"


@pytest.fixture
def client(app):
    c = TestClient(create_app(app, "t"), base_url="http://127.0.0.1")
    c.headers["Authorization"] = "Bearer t"
    return c


# --- settings ------------------------------------------------------------------------


def test_out_of_range_settings_are_brought_into_range(client, app):
    r = client.put("/api/settings", json={"recording": {"block_silence_s": 0, "max_block_s": -5}, "reminders": {"lead_minutes": 99999}, "summary": {"auto_time": "25:99"}})
    s = r.json()["settings"]
    assert (s["recording"]["block_silence_s"], s["recording"]["max_block_s"]) == (1, 30)
    assert s["reminders"]["lead_minutes"] == 1440 and s["summary"]["auto_time"] == ""
    assert client.put("/api/settings", json={"summary": {"auto_time": "07:45"}}).json()["settings"]["summary"]["auto_time"] == "07:45"
    assert client.put("/api/settings", json={"recording": {"block_silence_s": True}}).json()["settings"]["recording"]["block_silence_s"] == 1  # a stray boolean is ignored


# --- transcription queue -------------------------------------------------------------


def test_recordings_are_never_queued_twice(app):
    for hms in ("09:00:00", "09:05:00"):
        app.storage.save_block_audio(datetime.fromisoformat(f"{D}T{hms}"), tone(1))
    assert app.pipeline.recover() == 2
    assert app.pipeline.recover() == 0  # e.g. models finished downloading while recordings were already queued
    assert app.pipeline.pending == 2 and app.pipeline.untranscribed(D) == 2


def test_an_already_transcribed_recording_is_not_transcribed_again(app, monkeypatch):
    app.storage.save_block_audio(datetime.fromisoformat(f"{D}T09:00:00"), tone(1))
    app.storage.append_segments(D, [seg(D, "09:00:00", "Hello.")])
    monkeypatch.setattr(app.engine, "transcribe", lambda *a, **k: pytest.fail("transcribed twice"))
    assert app.pipeline.process(D, "09-00-00", "continuous") == []
    assert len(app.storage.load_segments(D)) == 1


# --- recording state -----------------------------------------------------------------


def test_paused_continuous_recording_says_why_and_clears_when_switched_off(app, monkeypatch):
    app.settings.recording.continuous = True  # was on when the app last closed
    monkeypatch.setattr(app.engine, "ready", lambda: False)
    app.resume_continuous()
    assert app.recorder.mode == "idle" and app.recorder.error.startswith("Continuous recording is paused: Download the speech models")
    app.set_continuous(False)
    assert app.recorder.error == "" and app.settings.recording.continuous is False


def test_a_silent_microphone_is_reported_and_reopened(app, monkeypatch):
    rec = app.recorder
    opened = []
    monkeypatch.setattr(rec, "_open_microphone", lambda: opened.append(1))
    monkeypatch.setattr(rec, "_close_microphone", lambda: None)
    monkeypatch.setattr(app.engine, "new_vad", lambda threshold: lambda frame: False)
    rec.start("continuous")
    rec._stream = object()  # an open stream...
    rec._last_audio = time.monotonic() - 10  # ...that has delivered nothing for 10 s
    rec.check_microphone()
    assert rec.error.startswith("The microphone stopped sending audio") and len(opened) == 2
    rec.check_microphone()
    assert len(opened) == 2  # not reopened again straight away
    rec._last_audio = time.monotonic()  # audio is flowing again
    rec.check_microphone()
    assert rec.error == ""


def test_microphone_audio_at_other_rates_is_resampled_as_one_stream(app):
    """Converting 0.1 s chunks separately left small errors at every chunk edge; one stream resampler matches whole-signal conversion."""
    import queue

    import soxr

    got = []
    rec = app.recorder
    rec.mode = "sample"
    rec.feed = lambda samples, at: got.append(samples)
    rate = 48000
    t = np.arange(rate * 2) / rate
    sine = (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    q = queue.Queue()
    for i in range(0, len(sine), rate // 10):
        q.put((sine[i : i + rate // 10], datetime.now()))
    q.put(None)
    rec._pump(q, rate)
    out = np.concatenate(got)
    reference = soxr.resample(sine, rate, SAMPLE_RATE)[: len(out)]
    edge = SAMPLE_RATE // 10
    assert np.abs(out - reference)[edge:-edge].max() < 1e-4  # chunk-by-chunk conversion was off by ~0.008 at each edge


# --- summary -------------------------------------------------------------------------


def test_other_peoples_notes_to_self_are_not_mine(app):
    app.speakers.enroll("Tom", [1.0, 0.0], is_user=True)
    tom = app.speakers.speakers[0]["id"]
    app.storage.append_segments(D, [
        seg(D, "09:00:00", "Note to self, call the bank.", speaker_id=tom),
        seg(D, "09:00:10", "Note to self, buy a gift for Tom.", speaker="Speaker 1", speaker_id="unk-1"),
    ])
    assert [n["text"] for n in app.summarize(D)["notes"]] == ["Call the bank"]


# --- transcript corrections, deletion and search ----------------------------------------


def test_fix_a_line_reassign_its_speaker_and_delete_a_conversation(client, app):
    app.speakers.enroll("Ana", [0.0, 1.0])
    app.storage.save_block_audio(datetime.fromisoformat(f"{D}T09:00:00"), tone(2))
    app.storage.save_block_audio(datetime.fromisoformat(f"{D}T11:00:00"), tone(2))
    app.storage.append_segments(D, [
        seg(D, "09:00:00", "The budget is do friday.", speaker="Speaker 1", speaker_id="unk-1", block="09-00-00"),
        seg(D, "09:00:05", "Got it.", block="09-00-00"),
        seg(D, "11:00:00", "Private matter.", block="11-00-00"),
    ])
    day = client.get(f"/api/days/{D}").json()
    assert {"id": "unk", "name": "Unknown"} in day["speakers"] and day["untranscribed"] == 0
    ana = next(c["id"] for c in day["speakers"] if c["name"] == "Ana")
    first = day["conversations"][0]["segments"][0]["id"]
    r = client.patch(f"/api/days/{D}/segments/{first}", json={"text": "The budget is due Friday.", "speaker_id": ana})
    assert r.json()["text"] == "The budget is due Friday." and r.json()["speaker"] == "Ana" and r.json()["edited"]
    assert "· Ana:** The budget is due Friday." in (app.home / "days" / D / "transcript.md").read_text()
    assert client.patch(f"/api/days/{D}/segments/{first}", json={"text": "  "}).status_code == 400
    assert client.patch(f"/api/days/{D}/segments/{first}", json={"speaker_id": "spk-nobody"}).status_code == 400

    # Deleting one line keeps the recording while another line still uses it.
    second = day["conversations"][0]["segments"][1]["id"]
    assert client.post(f"/api/days/{D}/segments/delete", json={"ids": [second]}).json() == {"lines": 1, "recordings": 0}
    # Deleting the last line of a recording removes its audio too.
    private = day["conversations"][1]["segments"][0]["id"]
    assert client.post(f"/api/days/{D}/segments/delete", json={"ids": [private]}).json() == {"lines": 1, "recordings": 1}
    assert app.storage.audio_file(D, "11-00-00") is None and app.pipeline.untranscribed(D) == 0
    assert [s["text"] for s in app.storage.load_segments(D)] == ["The budget is due Friday."]


def test_search_finds_lines_across_days(client, app):
    app.storage.append_segments("2026-11-14", [seg("2026-11-14", "09:00:00", "Travel budget is over.")])
    app.storage.append_segments(D, [seg(D, "10:00:00", "The BUDGET for travel looks fine."), seg(D, "10:01:00", "Lunch?")])
    hits = client.get("/api/search", params={"q": "travel budget"}).json()
    assert [(h["date"], h["text"]) for h in hits] == [(D, "The BUDGET for travel looks fine."), ("2026-11-14", "Travel budget is over.")]
    assert hits[0]["has_audio"] is False and client.get("/api/search", params={"q": "  "}).json() == []


# --- reminders -----------------------------------------------------------------------


def test_reminders_can_be_edited_snoozed_and_acknowledged(client, app):
    app.speakers.enroll("Tom", [1.0, 0.0], is_user=True)
    tom = app.speakers.speakers[0]["id"]
    now = datetime.now().astimezone()
    said = seg(now.date().isoformat(), (now - timedelta(minutes=1)).strftime("%H:%M:%S"), "Note to self, call Ana in 2 hours.", speaker_id=tom)
    [r] = app.reminders.handle_segments([said])
    assert [h["id"] for h in client.get("/api/status").json()["heard"]] == [r["id"]]  # shown so a misheard time can be fixed

    new_due = (now + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
    r2 = client.patch(f"/api/reminders/{r['id']}", json={"text": "Call Ana back", "due": new_due.strftime("%Y-%m-%dT%H:%M")}).json()
    assert r2["text"] == "Call Ana back" and r2["due"].startswith(new_due.strftime("%Y-%m-%dT10:00")) and r2["status"] == "pending"
    assert r2["notify_at"].startswith((new_due - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M"))
    assert client.get("/api/status").json()["heard"] == []  # editing counts as having seen it

    app.reminder_store.update(r["id"], status="notified")
    r3 = client.patch(f"/api/reminders/{r['id']}", json={"snooze_minutes": 10}).json()
    assert r3["status"] == "pending" and 9 <= (datetime.fromisoformat(r3["notify_at"]) - now).total_seconds() / 60 <= 11
    assert client.patch(f"/api/reminders/{r['id']}", json={"due": "not a date"}).status_code == 400


# --- import --------------------------------------------------------------------------


def test_imported_file_is_placed_by_the_time_it_ended(client, app, monkeypatch):
    import io

    import soundfile as sf

    monkeypatch.setattr(app.engine, "ready", lambda: True)
    monkeypatch.setattr(app.engine, "new_vad", lambda threshold: lambda frame: float(np.abs(frame).max()) > 0.05)
    buf = io.BytesIO()
    sf.write(buf, np.concatenate([tone(4), silence(16)]), SAMPLE_RATE, format="FLAC")
    r = client.post("/api/import", files={"file": ("memo.flac", buf.getvalue(), "audio/flac")}, data={"ended": f"{D}T15:30:20"})
    assert r.status_code == 200 and r.json()["date"] == D and r.json()["blocks"] == 1
    blocks = [b for b, _ in app.storage.audio_blocks(D)]
    assert blocks == ["15-30-00"]  # 20 s file ending 15:30:20 started at 15:30:00 (speech starts right away)


# --- Windows start-up ------------------------------------------------------------------


def test_start_with_windows_writes_and_removes_the_run_key(monkeypatch, tmp_path):
    store = {}

    class Key:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    fake = types.SimpleNamespace(
        HKEY_CURRENT_USER=object(),
        REG_SZ=1,
        OpenKey=lambda root, path: Key(),
        CreateKey=lambda root, path: Key(),
        QueryValueEx=lambda key, name: (store[name], 1) if name in store else (_ for _ in ()).throw(FileNotFoundError()),
        SetValueEx=lambda key, name, reserved, kind, value: store.__setitem__(name, value),
        DeleteValue=lambda key, name: store.pop(name) if name in store else (_ for _ in ()).throw(FileNotFoundError()),
    )
    monkeypatch.setitem(sys.modules, "winreg", fake)
    monkeypatch.setattr(sys, "platform", "win32")
    assert autostart.supported() and not autostart.enabled()
    autostart.set_enabled(True, tmp_path)
    assert autostart.enabled() and store["Uncle Virgil"].endswith(f'-m uncle_virgil --minimized --home "{tmp_path}"')
    autostart.set_enabled(False, tmp_path)
    autostart.set_enabled(False, tmp_path)  # already off: no error
    assert not autostart.enabled()


def test_start_with_windows_is_refused_elsewhere(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    assert not autostart.supported() and not autostart.enabled()
    with pytest.raises(RuntimeError):
        autostart.set_enabled(True, tmp_path)


def test_reminders_from_old_recordings_are_not_set_once_their_time_has_passed(app):
    """An imported recording from yesterday mentioning 'meeting at 5pm' must not pop up 'Reminder set' today."""
    yesterday = (datetime.now().astimezone() - timedelta(days=1)).date().isoformat()
    assert app.reminders.handle_segments([seg(yesterday, "09:00:00", "Note to self, meeting at 5pm.", mode="dictation")]) == []
    assert app.sent == [] and app.reminder_store.all() == []


def test_search_by_a_renamed_persons_new_name_still_finds_their_older_lines(client, app):
    app.speakers.enroll("Ana", [0.0, 1.0])
    ana = app.speakers.speakers[0]["id"]
    app.storage.append_segments(D, [seg(D, "10:00:00", "The forecast is ready.", speaker="Ana", speaker_id=ana)])
    app.speakers.rename(ana, "Ana Lopez")  # the file still says "Ana"
    hits = client.get("/api/search", params={"q": "lopez forecast"}).json()
    assert [(h["speaker"], h["text"]) for h in hits] == [("Ana Lopez", "The forecast is ready.")]
    assert client.get("/api/search", params={"q": "lopez zebra"}).json() == []
