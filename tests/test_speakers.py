import numpy as np

from uncle_virgil.speakers import SpeakerStore
from uncle_virgil.storage import Storage

from .conftest import seg

D = "2026-11-16"
A, B, C = (np.eye(3, dtype=np.float32)[i] for i in range(3))


def near(v, other, amount=0.2):
    w = v + amount * other
    return w / np.linalg.norm(w)


def test_enrolled_voice_is_recognized_and_others_get_stable_day_labels(tmp_path):
    store = SpeakerStore(Storage(tmp_path))
    tom = store.enroll("Tom", A, is_user=True)
    assert store.resolve(D, near(A, B), 5.0) == (tom["id"], "Tom")
    assert store.resolve(D, B, 5.0) == ("unk-1", "Speaker 1")
    assert store.resolve(D, C, 5.0) == ("unk-2", "Speaker 2")
    assert store.resolve(D, near(B, C), 5.0) == ("unk-1", "Speaker 1")  # same voice again, same label
    assert store.resolve("2026-11-17", B, 5.0) == ("unk-1", "Speaker 1")  # a new day starts numbering afresh
    assert SpeakerStore(Storage(tmp_path)).user_ids() == {tom["id"]}  # survives a restart


def test_very_short_unmatched_voice_does_not_create_a_new_speaker(tmp_path):
    store = SpeakerStore(Storage(tmp_path))
    assert store.resolve(D, B, 0.6) == ("unk", "Unknown")
    assert store.resolve(D, None, 3.0) == ("unk", "Unknown")


def test_naming_an_unknown_speaker_updates_transcript_and_future_recognition(tmp_path):
    storage = Storage(tmp_path)
    store = SpeakerStore(storage)
    storage.name_resolver = store.name_of
    sid, label = store.resolve(D, B, 5.0)
    storage.append_segments(D, [seg(D, "09:00:00", "Hello there.", speaker=label, speaker_id=sid)])
    jack = store.name_unknown(D, sid, "Jack")
    assert storage.load_segments(D)[0]["speaker_id"] == jack["id"]
    assert "· Jack:** Hello there." in (tmp_path / "days" / D / "transcript.md").read_text()
    assert store.resolve("2026-11-20", near(B, A), 5.0) == (jack["id"], "Jack")  # recognized on a later day
    store.rename(jack["id"], "Jack Smith")
    assert "· Jack Smith:**" in (tmp_path / "days" / D / "transcript.md").read_text()
