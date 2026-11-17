from datetime import datetime

import numpy as np

from uncle_virgil import audio
from uncle_virgil.storage import Storage, group_conversations

from .conftest import seg, tone


def speechlike(seconds):
    rng = np.random.default_rng(0)
    return (tone(seconds) + 0.05 * rng.standard_normal(int(seconds * audio.SAMPLE_RATE))).astype(np.float32)


def filled(tmp_path):
    st = Storage(tmp_path)
    for date, hms in [("2026-11-14", "09:00:00"), ("2026-11-15", "10:30:00"), ("2026-11-16", "14:00:00")]:
        st.save_block_audio(datetime.fromisoformat(f"{date}T{hms}"), speechlike(20))
        st.append_segments(date, [seg(date, hms, f"Words spoken on {date}.")])
    return st


def test_files_are_plain_and_readable(tmp_path):
    st = filled(tmp_path)
    day = tmp_path / "days" / "2026-11-16"
    assert (day / "audio" / "14-00-00.flac").exists()
    text = (day / "transcript.md").read_text()
    assert "# Transcript: Monday, November 16, 2026" in text and "**14:00:00 · Tom:** Words spoken on 2026-11-16." in text
    assert st.list_days() == ["2026-11-16", "2026-11-15", "2026-11-14"]


def test_two_blocks_in_the_same_second_do_not_overwrite(tmp_path):
    st = Storage(tmp_path)
    when = datetime(2026, 11, 16, 14, 0, 0)
    assert st.save_block_audio(when, tone(1))[1] == "14-00-00"
    assert st.save_block_audio(when, tone(1))[1] == "14-00-00-2"
    assert st.block_start("2026-11-16", "14-00-00-2") == when


def test_delete_audio_in_range_keeps_text(tmp_path):
    st = filled(tmp_path)
    result = st.delete_audio(datetime(2026, 11, 14), datetime(2026, 11, 15, 23, 59, 59))
    assert result["files"] == 2 and result["bytes"] > 0
    assert st.audio_blocks("2026-11-14") == [] and st.audio_blocks("2026-11-15") == []
    assert len(st.audio_blocks("2026-11-16")) == 1  # outside the range: untouched
    assert st.load_segments("2026-11-14")[0]["text"] == "Words spoken on 2026-11-14."
    assert (tmp_path / "days" / "2026-11-14" / "transcript.md").exists()
    assert "2026-11-14" in st.list_days()


def test_compress_audio_in_range_shrinks_files_and_stays_playable(tmp_path):
    st = filled(tmp_path)
    before = st.audio_usage(datetime(2026, 11, 14), datetime(2026, 11, 16, 23, 59))
    result = st.compress_audio(datetime(2026, 11, 14), datetime(2026, 11, 15, 23, 59))
    assert result["files"] == 2 and result["bytes_after"] < result["bytes_before"] / 3
    path = st.audio_file("2026-11-14", "09-00-00")
    assert path.suffix == ".opus" and abs(len(audio.read_audio(path)) / audio.SAMPLE_RATE - 20) < 0.2
    assert st.audio_file("2026-11-16", "14-00-00").suffix == ".flac"
    after = st.audio_usage(datetime(2026, 11, 14), datetime(2026, 11, 16, 23, 59))
    assert after["files"] == 3 and after["compressed_files"] == 2 and after["bytes"] < before["bytes"]
    assert st.compress_audio(datetime(2026, 11, 14), datetime(2026, 11, 15, 23, 59))["files"] == 0  # already done


def test_conversations_split_at_long_pauses():
    d = "2026-11-16"
    convs = group_conversations([seg(d, "09:00:00", "a"), seg(d, "09:00:30", "b"), seg(d, "09:10:00", "c")], gap_s=120)
    assert [(c["id"], len(c["segments"])) for c in convs] == [("c1", 2), ("c2", 1)]
