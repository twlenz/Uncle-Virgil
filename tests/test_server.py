import time
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from uncle_virgil.server import create_app

from .conftest import seg, tone

D = "2026-11-16"


@pytest.fixture
def client(app):
    c = TestClient(create_app(app, "secret-token"), base_url="http://127.0.0.1")
    c.headers["Authorization"] = "Bearer secret-token"
    return c


def test_api_requires_the_launch_token_and_a_local_host_header(app):
    api = create_app(app, "secret-token")
    assert TestClient(api, base_url="http://127.0.0.1").get("/api/status").status_code == 401
    assert TestClient(api, base_url="http://evil.example").get("/api/status", headers={"Authorization": "Bearer secret-token"}).status_code == 403
    browser = TestClient(api, base_url="http://127.0.0.1")
    assert browser.get("/?token=wrong").cookies.get("uv_token") is None
    browser.get("/?token=secret-token")  # the desktop window's first request sets the cookie
    assert browser.get("/api/status").status_code == 200


def test_settings_round_trip_and_survive_restart(client, app):
    r = client.put("/api/settings", json={"user_name": "Tom", "llm": {"provider": "openai_compatible", "model": "llama3.2"}, "reminders": {"lead_minutes": 15, "channels": ["desktop", "email"]}})
    assert r.status_code == 200 and r.json()["settings"]["reminders"]["lead_minutes"] == 15
    reloaded = type(app)(app.home).settings
    assert (reloaded.user_name, reloaded.llm.model, reloaded.reminders.channels) == ("Tom", "llama3.2", ["desktop", "email"])
    assert "api_key" not in (app.home / "config.toml").read_text()  # secrets never go in the config file
    assert client.put("/api/settings", json={"speech": {"asr_model": "nope"}}).status_code == 400


def test_day_view_reminders_and_storage_endpoints(client, app):
    app.storage.save_block_audio(datetime.fromisoformat(f"{D}T09:00:00"), tone(2))
    app.storage.append_segments(D, [seg(D, "09:00:00", "Hello."), seg(D, "09:00:05", "Hi.", block="09-00-00")])
    day = client.get(f"/api/days/{D}").json()
    segs = day["conversations"][0]["segments"]
    assert [s["has_audio"] for s in segs] == [True, True] and segs[1]["offset"] == 5.0 and day["summary"] is None
    assert client.get(f"/api/audio/{D}/09-00-00").headers["content-type"] == "audio/flac"
    assert client.get("/api/days").json() == [{"date": D, "has_summary": False}]

    due = (datetime.now() + timedelta(hours=2)).isoformat(timespec="minutes")
    rid = client.post("/api/reminders", json={"text": "Call Ana", "due": due}).json()["id"]
    assert client.post("/api/reminders", json={"text": "Call Ana", "due": due}).status_code == 400  # duplicate
    assert client.patch(f"/api/reminders/{rid}", json={"status": "done"}).json()["status"] == "done"
    client.delete(f"/api/reminders/{rid}")
    assert client.get("/api/reminders").json() == []

    assert client.get(f"/api/storage/usage?start={D}&end={D}").json()["files"] == 1
    assert client.post("/api/storage/delete", json={"start": D, "end": D}).json()["files"] == 1
    assert client.get(f"/api/audio/{D}/09-00-00").status_code == 404
    assert client.get(f"/api/days/{D}").json()["conversations"][0]["segments"][0]["text"] == "Hello."  # text kept
    assert client.post("/api/storage/delete", json={"start": D, "end": "2026-11-01"}).status_code == 400


def test_summarize_job_and_errors_are_reported(client, app, monkeypatch):
    monkeypatch.setattr(app.engine, "ready", lambda: False)  # as on a fresh install, before models are downloaded
    assert client.post(f"/api/days/{D}/summarize").status_code == 400  # nothing recorded
    app.storage.append_segments(D, [seg(D, "09:00:00", "Note to self, buy milk.")])
    assert client.post(f"/api/days/{D}/summarize").json()["state"] in ("running", "done")
    for _ in range(100):
        if client.get(f"/api/jobs/summary:{D}").json()["state"] == "done":
            break
        time.sleep(0.02)
    assert client.get(f"/api/days/{D}").json()["summary"]["notes"][0]["text"] == "Buy milk"
    assert client.post("/api/llm/test").status_code == 400  # no model selected
    assert client.post("/api/record/continuous", json={"on": True}).json()["detail"].startswith("Download the speech models")


def test_voice_sample_enrolls_the_owner_and_rejects_short_samples(client, app, monkeypatch):
    import numpy as np

    monkeypatch.setattr(app.engine, "ready", lambda: True)
    monkeypatch.setattr(app.engine, "embed", lambda samples: np.array([1.0, 0.0, 0.0], dtype=np.float32))
    monkeypatch.setattr(app.recorder, "_open_microphone", lambda: None)  # audio is fed by the test instead

    assert client.post("/api/speakers/sample", json={"on": True}).status_code == 200
    app.recorder.feed(tone(3))
    r = client.post("/api/speakers/sample", json={"on": False, "name": "Tom", "is_user": True})
    assert r.status_code == 400 and "too short" in r.json()["detail"] and app.recorder.mode == "idle"

    client.post("/api/speakers/sample", json={"on": True})
    app.recorder.feed(tone(12))
    r = client.post("/api/speakers/sample", json={"on": False, "name": "Tom", "is_user": True})
    assert r.status_code == 200 and "embedding" not in r.json()
    assert client.get("/api/speakers").json()[0] | {"id": "", "created": ""} == {"id": "", "name": "Tom", "is_user": True, "samples": 1, "created": ""}
    assert client.get("/api/status").json()["owner_enrolled"] is True

    client.post("/api/speakers/sample", json={"on": True})
    app.recorder.feed(tone(12))
    assert client.post("/api/speakers/sample", json={"on": False, "name": ""}).json() == {}  # cancelled: nothing saved
    assert len(client.get("/api/speakers").json()) == 1


def test_dictation_pauses_continuous_recording_and_resumes_it(app, monkeypatch):
    monkeypatch.setattr(app.engine, "ready", lambda: True)
    monkeypatch.setattr(app.engine, "new_vad", lambda threshold: lambda frame: False)
    monkeypatch.setattr(app.recorder, "_open_microphone", lambda: None)
    app.set_continuous(True)
    assert app.recorder.mode == "continuous" and type(app)(app.home).settings.recording.continuous is True
    app.start_capture("dictation")
    assert app.recorder.mode == "dictation"
    app.stop_capture()
    assert app.recorder.mode == "continuous"
    app.set_continuous(False)
    assert app.recorder.mode == "idle" and type(app)(app.home).settings.recording.continuous is False
