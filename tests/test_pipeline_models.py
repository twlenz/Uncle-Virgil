"""Whole-pipeline test on real audio with the real speech models.

Skipped unless UNCLE_VIRGIL_TEST_MODELS points at a folder holding the
downloaded models (the app's own `models` folder works).
"""

import os
from datetime import datetime, timedelta

import pytest

from uncle_virgil import audio

from .conftest import DATA

pytestmark = pytest.mark.skipif(not os.environ.get("UNCLE_VIRGIL_TEST_MODELS"), reason="speech models not available")

# Tomorrow, so the "meeting at 5pm" in the recording is still ahead and becomes a reminder.
T0 = (datetime.now() + timedelta(days=1)).replace(hour=14, minute=0, second=0, microsecond=0).astimezone()
D = T0.date().isoformat()


def test_recorded_conversation_becomes_named_transcript_reminder_and_summary(app):
    assert app.engine.ready()
    app.pipeline.start()
    app.speakers.enroll("Tom", app.engine.embed(audio.read_audio(DATA / "owner_sample.flac")), is_user=True)

    # Play a two-person conversation (synthetic voices) through continuous recording.
    app.settings.recording.block_silence_s = 3.0
    samples = audio.read_audio(DATA / "conversation.flac")
    app.recorder.start("continuous", open_microphone=False)
    for i in range(0, len(samples), 1600):
        app.recorder.feed(samples[i : i + 1600], T0 + timedelta(seconds=(i + 1600) / audio.SAMPLE_RATE))
    app.recorder.stop()
    assert app.pipeline.wait_idle() and app.pipeline.last_error == ""

    segments = app.storage.load_segments(D)
    speakers = [s["speaker"] for s in segments]
    assert speakers == ["Tom", "Speaker 1", "Tom", "Speaker 1", "Tom"]  # the owner is named, the other voice is separated
    text = " ".join(s["text"] for s in segments).lower()
    for phrase in ("quarterly budget report", "travel numbers", "hiring plan", "meeting with jack at 5", "automate the expense report"):
        assert phrase in text
    assert segments[0]["start"].startswith(f"{D}T14:00:0") and segments[-1]["start"].startswith(f"{D}T14:00:2")
    assert len(app.storage.audio_blocks(D)) == 2  # the 4 s pause split the recording in two

    reminders = app.reminder_store.all()
    assert len(reminders) == 1 and reminders[0]["due"].startswith(f"{D}T17:00") and "Jack" in reminders[0]["text"]

    summary = app.summarize(D)
    assert [n["kind"] for n in summary["notes"]] == ["note", "idea"]
    assert "Speaker 1:**" in (app.home / "days" / D / "transcript.md").read_text()


def test_unnamed_voices_are_numbered_in_order_of_appearance(app):
    app.pipeline.start()
    app.pipeline.submit(audio.read_audio(DATA / "conversation.flac"), T0, "continuous")
    assert app.pipeline.wait_idle()
    assert [s["speaker"] for s in app.storage.load_segments(D)][:2] == ["Speaker 1", "Speaker 2"]


def test_short_dictation_is_attributed_to_the_owner_and_silence_is_discarded(app):
    import numpy as np

    app.pipeline.start()
    app.speakers.enroll("Tom", app.engine.embed(audio.read_audio(DATA / "owner_sample.flac")), is_user=True)
    samples = audio.read_audio(DATA / "conversation.flac")
    app.pipeline.submit(samples[int(1.0 * 16000) : int(4.8 * 16000)], T0, "dictation")
    app.pipeline.submit(np.zeros(16000 * 5, dtype=np.float32), T0 + timedelta(minutes=5), "continuous")
    assert app.pipeline.wait_idle()
    segments = app.storage.load_segments(D)
    assert len(segments) == 1 and segments[0]["speaker"] == "Tom" and segments[0]["mode"] == "dictation"
    assert [b for b, _ in app.storage.audio_blocks(D)] == ["14-00-00"]  # the silent block's audio was removed


def test_importing_a_recording_file_goes_through_the_same_pipeline(app):
    from fastapi.testclient import TestClient

    from uncle_virgil.server import create_app

    app.pipeline.start()
    client = TestClient(create_app(app, "t"), base_url="http://127.0.0.1", headers={"Authorization": "Bearer t"})
    with (DATA / "conversation.flac").open("rb") as f:
        r = client.post("/api/import", files={"file": ("walk.flac", f, "audio/flac")}, data={"started": f"{D}T08:00:00"})
    assert r.status_code == 200 and r.json()["blocks"] == 1 and r.json()["date"] == D
    assert app.pipeline.wait_idle()
    segments = app.storage.load_segments(D)
    assert len(segments) == 5 and segments[0]["start"].startswith(f"{D}T08:00:0") and segments[0]["mode"] == "import"
    assert client.post("/api/import", files={"file": ("notes.txt", b"not audio", "text/plain")}).status_code == 400
