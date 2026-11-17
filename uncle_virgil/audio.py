"""Audio file helpers. Everything in the app is 16 kHz mono float32."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import numpy as np
import soundfile as sf
import soxr

SAMPLE_RATE = 16000
AUDIO_EXTENSIONS = (".flac", ".opus", ".wav", ".mp3", ".ogg")


def resample(samples: np.ndarray, rate: int) -> np.ndarray:
    if rate == SAMPLE_RATE:
        return samples.astype(np.float32, copy=False)
    return soxr.resample(samples, rate, SAMPLE_RATE).astype(np.float32)


def read_audio(path: Path) -> np.ndarray:
    """Read any supported file as 16 kHz mono float32."""
    try:
        data, rate = sf.read(str(path), dtype="float32", always_2d=True)
    except sf.LibsndfileError:
        return _read_with_ffmpeg(path)
    return resample(data.mean(axis=1), rate)


def _read_with_ffmpeg(path: Path) -> np.ndarray:
    """Fallback for formats libsndfile cannot read (m4a, aac, ...)."""
    if not shutil.which("ffmpeg"):
        raise ValueError(f"Cannot read {path.name}: unsupported format (installing ffmpeg adds more formats)")
    cmd = ["ffmpeg", "-v", "error", "-i", str(path), "-f", "f32le", "-ac", "1", "-ar", str(SAMPLE_RATE), "-"]
    out = subprocess.run(cmd, capture_output=True, check=False)
    if out.returncode != 0:
        raise ValueError(f"Cannot read {path.name}: {out.stderr.decode(errors='replace').strip()}")
    return np.frombuffer(out.stdout, dtype=np.float32).copy()


def write_flac(path: Path, samples: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), samples, SAMPLE_RATE, format="FLAC", subtype="PCM_16")


def write_opus(path: Path, samples: np.ndarray) -> None:
    """Lossy speech compression (Ogg/Opus). Written in pieces to keep memory flat."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with sf.SoundFile(str(path), "w", SAMPLE_RATE, 1, format="OGG", subtype="OPUS") as f:
        step = SAMPLE_RATE * 30
        for i in range(0, len(samples), step):
            f.write(samples[i : i + step])


def duration_s(path: Path) -> float:
    try:
        info = sf.info(str(path))
        return info.frames / info.samplerate
    except sf.LibsndfileError:
        return 0.0
