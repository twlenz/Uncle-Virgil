from datetime import datetime, timedelta

import numpy as np

from uncle_virgil.audio import SAMPLE_RATE
from uncle_virgil.config import RecordingSettings
from uncle_virgil.recorder import Recorder

from .conftest import silence, tone

T0 = datetime(2026, 11, 16, 14, 0).astimezone()


def loud(frame):  # stands in for the neural voice detector
    return float(np.abs(frame).max()) > 0.05


def make(settings=None):
    blocks = []
    rec = Recorder(settings or RecordingSettings(block_silence_s=2.0, pre_roll_s=0.5), lambda: loud, lambda s, t, m: blocks.append((len(s) / SAMPLE_RATE, t, m)))
    return rec, blocks


def play(rec, audio, chunk=1600):
    for i in range(0, len(audio), chunk):
        rec.feed(audio[i : i + chunk], T0 + timedelta(seconds=(i + chunk) / SAMPLE_RATE))


def test_continuous_keeps_only_speech_and_splits_at_long_pauses():
    rec, blocks = make()
    rec.start("continuous", open_microphone=False)
    # 10 s quiet, 3 s talk, 1 s pause, 2 s talk, 5 s quiet, 4 s talk, then recording is switched off
    play(rec, np.concatenate([silence(10), tone(3), silence(1), tone(2), silence(5), tone(4), silence(0.2)]))
    rec.stop()
    assert [m for _, _, m in blocks] == ["continuous", "continuous"]
    (len1, start1, _), (len2, start2, _) = blocks
    assert 6.5 <= len1 <= 7.5  # 0.5 pre-roll + 3 + 1 + 2 + 0.5 kept silence
    assert abs((start1 - T0).total_seconds() - 9.5) < 0.2  # timestamp points at the pre-roll before speech began
    assert 4.5 <= len2 <= 5.0 and abs((start2 - T0).total_seconds() - 20.5) < 0.2


def test_continuous_drops_noise_blips_and_pure_silence():
    rec, blocks = make()
    rec.start("continuous", open_microphone=False)
    play(rec, np.concatenate([silence(5), tone(0.1), silence(5)]))
    rec.stop()
    assert blocks == []


def test_long_speech_is_cut_at_the_maximum_length():
    rec, blocks = make(RecordingSettings(block_silence_s=2.0, max_block_s=10.0, pre_roll_s=0.5))
    rec.start("continuous", open_microphone=False)
    play(rec, np.concatenate([tone(9.8), silence(0.7), tone(6), silence(3)]))
    rec.stop()
    assert len(blocks) == 2 and 10 <= blocks[0][0] <= 11


def test_dictation_keeps_everything_between_start_and_stop():
    rec, blocks = make()
    rec.start("dictation", open_microphone=False)
    play(rec, np.concatenate([silence(1), tone(2), silence(4), tone(1)]))
    assert blocks == []
    rec.stop()
    assert len(blocks) == 1 and abs(blocks[0][0] - 8.0) < 0.01 and blocks[0][2] == "dictation"
    assert abs((blocks[0][1] - T0).total_seconds()) < 0.01 and rec.mode == "idle"


def test_sample_mode_returns_audio_instead_of_saving():
    rec, blocks = make()
    rec.start("sample", open_microphone=False)
    play(rec, tone(3))
    samples = rec.stop()
    assert blocks == [] and len(samples) == 3 * SAMPLE_RATE


def test_audio_is_ignored_while_idle():
    rec, blocks = make()
    play(rec, tone(3))
    assert rec.stop() is None and blocks == []


def test_tiny_speaker_clusters_are_folded_into_their_neighbor():
    from uncle_virgil.speech import absorb_small_clusters, merge_turns

    # One person, mis-split by the diarizer: 0.8 s labelled speaker 1, the rest speaker 2.
    pieces = absorb_small_clusters([(0.03, 0.86, 1), (0.69, 3.69, 2)])
    assert merge_turns(pieces) == [[0.03, 3.69, 2]]
    # A second speaker with enough speech is kept, as is a short one far from anyone else.
    assert absorb_small_clusters([(0.0, 3.0, 0), (3.2, 5.2, 1)]) == [(0.0, 3.0, 0), (3.2, 5.2, 1)]
    assert absorb_small_clusters([(0.0, 3.0, 0), (9.0, 9.6, 1)]) == [(0.0, 3.0, 0), (9.0, 9.6, 1)]
    # A clip that is short overall is left alone.
    assert absorb_small_clusters([(0.0, 1.0, 0)]) == [(0.0, 1.0, 0)]


def test_long_turns_are_cut_to_fit_the_recognizer():
    from uncle_virgil.speech import MAX_TURN_S, merge_turns

    turns = merge_turns([(0.0, 70.0, 0)])
    assert len(turns) == 3 and all(e - s <= MAX_TURN_S for s, e, _ in turns) and turns[-1][1] == 70.0
