"""Local speech engine: who spoke when, what they said, and voiceprints.

Everything runs on the CPU through sherpa-onnx (ONNX Runtime). No audio or
text leaves the machine here.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field

import numpy as np

from .audio import SAMPLE_RATE
from .config import SpeechSettings
from .models import ASR_MODELS, EMBEDDING, SEGMENTATION, VAD, ModelManager

log = logging.getLogger(__name__)

MAX_TURN_S = 28.0  # Whisper models accept at most 30 s at a time
MERGE_GAP_S = 1.0  # same-speaker pieces closer than this are transcribed together
MIN_TURN_S = 0.25
MIN_CLUSTER_S = 1.5  # a "speaker" heard for less than this in a block is probably a mis-split
PAD_S = 0.1
MAX_VOICE_S = 60.0  # audio used for one voiceprint


@dataclass
class Turn:
    start: float  # seconds from the start of the block
    end: float
    cluster: int  # speaker index within this block
    text: str


@dataclass
class Voice:
    embedding: np.ndarray | None  # unit-length voiceprint, None if too little audio
    seconds: float


@dataclass
class BlockResult:
    turns: list[Turn] = field(default_factory=list)
    voices: dict[int, Voice] = field(default_factory=dict)


def absorb_small_clusters(pieces: list[tuple[float, float, int]]) -> list[tuple[float, float, int]]:
    """Fold "speakers" with very little speech into the speaker talking next to them.

    On a short recording the diarizer often splits one person in two, because a
    second or less of audio is too little to compare voices reliably. A speaker
    heard for under MIN_CLUSTER_S in total is therefore treated as part of the
    nearest established speaker when the two are within MERGE_GAP_S of each other.
    """
    totals: dict[int, float] = {}
    for start, end, cluster in pieces:
        totals[cluster] = totals.get(cluster, 0.0) + end - start
    big = [p for p in pieces if totals[p[2]] >= MIN_CLUSTER_S]
    out = []
    for start, end, cluster in pieces:
        if totals[cluster] < MIN_CLUSTER_S and big:
            gap, nearest = min((max(0.0, b[0] - end, start - b[1]), b[2]) for b in big)
            if gap <= MERGE_GAP_S:
                cluster = nearest
        out.append((start, end, cluster))
    return out


def merge_turns(pieces: list[tuple[float, float, int]]) -> list[list]:
    """Join consecutive same-speaker pieces so words are not cut at short pauses."""
    merged: list[list] = []
    for start, end, cluster in sorted(pieces):
        last = merged[-1] if merged else None
        if last and last[2] == cluster and start - last[1] <= MERGE_GAP_S and end - last[0] <= MAX_TURN_S:
            last[1] = max(last[1], end)
        else:
            merged.append([start, end, cluster])
    # Anything still too long for the recognizer is cut into equal parts.
    out: list[list] = []
    for start, end, cluster in merged:
        parts = max(1, int(np.ceil((end - start) / MAX_TURN_S)))
        step = (end - start) / parts
        out += [[start + i * step, start + (i + 1) * step, cluster] for i in range(parts)]
    return out


class SileroVad:
    """Frame-by-frame speech detector for the recorder. Feed 512-sample frames."""

    frame = 512

    def __init__(self, model_path: str, threshold: float):
        import sherpa_onnx as so

        cfg = so.VadModelConfig()
        cfg.silero_vad.model = model_path
        cfg.silero_vad.threshold = threshold
        cfg.silero_vad.min_silence_duration = 0.25
        cfg.silero_vad.min_speech_duration = 0.1
        cfg.silero_vad.window_size = self.frame
        cfg.sample_rate = SAMPLE_RATE
        self._vad = so.VoiceActivityDetector(cfg, buffer_size_in_seconds=30)

    def __call__(self, frame: np.ndarray) -> bool:
        self._vad.accept_waveform(frame)
        while not self._vad.empty():  # we only want the live flag; drop finished segments
            self._vad.pop()
        return self._vad.is_speech_detected()


class SpeechEngine:
    def __init__(self, manager: ModelManager, settings: SpeechSettings):
        self.manager = manager
        self.settings = settings
        self._lock = threading.Lock()
        self._loaded_asr: str | None = None
        self._diarizer = self._extractor = self._recognizer = None

    def ready(self) -> bool:
        return self.settings.asr_model in ASR_MODELS and not self.manager.missing(self.settings.asr_model)

    def _threads(self) -> int:
        return self.settings.threads or min(4, os.cpu_count() or 2)

    def new_vad(self, threshold: float) -> SileroVad:
        return SileroVad(str(self.manager.path(VAD.check)), threshold)

    def _load(self) -> None:
        import sherpa_onnx as so

        if self._loaded_asr == self.settings.asr_model:
            return
        p, n = self.manager.path, self._threads()
        embedding = so.SpeakerEmbeddingExtractorConfig(model=str(p(EMBEDDING.check)), num_threads=n)
        self._extractor = so.SpeakerEmbeddingExtractor(embedding)
        self._diarizer = so.OfflineSpeakerDiarization(
            so.OfflineSpeakerDiarizationConfig(
                segmentation=so.OfflineSpeakerSegmentationModelConfig(
                    pyannote=so.OfflineSpeakerSegmentationPyannoteModelConfig(model=str(p(SEGMENTATION.check))),
                    num_threads=n,
                ),
                embedding=embedding,
                clustering=so.FastClusteringConfig(num_clusters=-1, threshold=0.5),
                min_duration_on=0.3,
                min_duration_off=0.5,
            )
        )
        model = ASR_MODELS[self.settings.asr_model]
        files = {role: str(p(rel)) for role, rel in model.files.items()}
        if model.kind == "whisper":
            self._recognizer = so.OfflineRecognizer.from_whisper(**files, num_threads=n)
        else:
            self._recognizer = so.OfflineRecognizer.from_transducer(**files, model_type="nemo_transducer", num_threads=n)
        self._loaded_asr = self.settings.asr_model
        log.info("speech models loaded (%s, %d threads)", model.key, n)

    def embed(self, samples: np.ndarray) -> np.ndarray | None:
        """Voiceprint of a stretch of one person's speech."""
        with self._lock:
            self._load()
            return self._embed(samples)

    def _embed(self, samples: np.ndarray) -> np.ndarray | None:
        stream = self._extractor.create_stream()
        stream.accept_waveform(SAMPLE_RATE, samples[: int(MAX_VOICE_S * SAMPLE_RATE)])
        stream.input_finished()
        if not self._extractor.is_ready(stream):
            return None
        v = np.asarray(self._extractor.compute(stream), dtype=np.float32)
        norm = float(np.linalg.norm(v))
        return v / norm if norm > 0 else None

    def _recognize(self, samples: np.ndarray) -> str:
        stream = self._recognizer.create_stream()
        stream.accept_waveform(SAMPLE_RATE, samples)
        self._recognizer.decode_stream(stream)
        return stream.result.text.strip()

    def transcribe(self, samples: np.ndarray, assume_speech: bool = False) -> BlockResult:
        """Diarize and transcribe one block of audio.

        assume_speech: for push-to-talk clips, transcribe the whole clip as one
        speaker if the diarizer finds no speech (it can miss very short clips).
        """
        with self._lock:
            self._load()
            total = len(samples) / SAMPLE_RATE
            pieces = [(s.start, s.end, s.speaker) for s in self._diarizer.process(samples).sort_by_start_time()]
            if not pieces and assume_speech and total >= MIN_TURN_S:
                pieces = [(0.0, total, 0)]
            pieces = absorb_small_clusters(pieces)

            result = BlockResult()
            for start, end, cluster in merge_turns(pieces):
                if end - start < MIN_TURN_S:
                    continue
                a, b = max(0.0, start - PAD_S), min(total, end + PAD_S)
                text = self._recognize(samples[int(a * SAMPLE_RATE) : int(b * SAMPLE_RATE)])
                if text:
                    result.turns.append(Turn(start, end, cluster, text))

            for cluster in {t.cluster for t in result.turns}:
                audio = np.concatenate([samples[int(s * SAMPLE_RATE) : int(e * SAMPLE_RATE)] for s, e, k in pieces if k == cluster])
                result.voices[cluster] = Voice(self._embed(audio), len(audio) / SAMPLE_RATE)
            return result
