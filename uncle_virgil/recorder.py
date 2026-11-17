"""Microphone capture.

Two modes:
  dictation   press to start, press to stop; everything in between is kept.
  continuous  always listening; only stretches containing speech are kept,
              cut into blocks at pauses.

Audio arrives through `Recorder.feed`, so any source (the laptop microphone
now, a wearable later) can drive the same logic.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections import deque
from datetime import datetime, timedelta
from typing import Callable

import numpy as np

from .audio import SAMPLE_RATE, resample
from .config import RecordingSettings

log = logging.getLogger(__name__)

FRAME = 512  # samples per voice-detector frame (32 ms)
MIN_SPEECH_S = 0.3  # blocks with less speech than this are noise and dropped
KEEP_SILENCE_S = 0.5  # trailing silence kept at the end of a block
SOFT_CUT_PAUSE_S = 0.5  # once a block is past its maximum length, cut at a pause this long

OnBlock = Callable[[np.ndarray, datetime], None]


def now_local() -> datetime:
    return datetime.now().astimezone()


class BlockSegmenter:
    """Turns a continuous stream of frames into blocks that contain speech."""

    def __init__(self, is_speech: Callable[[np.ndarray], bool], settings: RecordingSettings, on_block: OnBlock):
        self.is_speech = is_speech
        self.on_block = on_block
        self.silence_frames = int(settings.block_silence_s * SAMPLE_RATE / FRAME)
        self.max_frames = int(settings.max_block_s * SAMPLE_RATE / FRAME)
        self.pre_roll: deque[np.ndarray] = deque(maxlen=max(1, int(settings.pre_roll_s * SAMPLE_RATE / FRAME)))
        self.speaking = False
        self._frames: list[np.ndarray] | None = None  # None while idle
        self._start: datetime | None = None
        self._silent = self._speech = 0

    def feed(self, frame: np.ndarray, now: datetime) -> None:
        """`now` is the wall-clock time at the end of this frame."""
        self.speaking = self.is_speech(frame)
        if self._frames is None:
            self.pre_roll.append(frame)
            if self.speaking:
                self._frames = list(self.pre_roll)
                self._start = now - timedelta(seconds=len(self._frames) * FRAME / SAMPLE_RATE)
                self._silent, self._speech = 0, 1
                self.pre_roll.clear()
            return
        self._frames.append(frame)
        if self.speaking:
            self._silent = 0
            self._speech += 1
        else:
            self._silent += 1
        n = len(self._frames)
        soft_cut = n >= self.max_frames and self._silent * FRAME >= SOFT_CUT_PAUSE_S * SAMPLE_RATE
        if self._silent >= self.silence_frames or soft_cut or n >= self.max_frames * 1.2:
            self.close()

    def close(self) -> None:
        """End the current block, if any, and hand it over."""
        frames, start, speech = self._frames, self._start, self._speech
        self._frames, self._start = None, None
        if not frames or speech * FRAME < MIN_SPEECH_S * SAMPLE_RATE:
            return
        drop = self._silent - int(KEEP_SILENCE_S * SAMPLE_RATE / FRAME)
        if drop > 0:
            frames = frames[:-drop]
        self.on_block(np.concatenate(frames), start)


class Recorder:
    def __init__(self, settings: RecordingSettings, vad_factory: Callable[[], Callable[[np.ndarray], bool]], on_block: Callable[[np.ndarray, datetime, str], None]):
        self.settings = settings
        self.vad_factory = vad_factory
        self.on_block = on_block
        self.mode = "idle"  # idle | dictation | continuous | sample
        self.level = 0.0  # recent loudness, 0..1, for the UI meter
        self.error = ""
        self._lock = threading.RLock()
        self._segmenter: BlockSegmenter | None = None
        self._buffer: list[np.ndarray] = []  # dictation / sample audio
        self._buffer_start: datetime | None = None
        self._pending = np.zeros(0, dtype=np.float32)  # samples not yet filling a frame
        self._stream = None
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None

    @property
    def speaking(self) -> bool:
        return bool(self._segmenter and self._segmenter.speaking)

    # --- control -----------------------------------------------------------------

    def start(self, mode: str, open_microphone: bool = True) -> None:
        if mode not in ("dictation", "continuous", "sample"):
            raise ValueError(f"unknown mode {mode!r}")
        if self.mode != "idle":
            self.stop()
        with self._lock:
            self.error = ""
            self._pending = np.zeros(0, dtype=np.float32)
            self._buffer, self._buffer_start = [], None
            if mode == "continuous":
                self._segmenter = BlockSegmenter(self.vad_factory(), self.settings, lambda s, t: self.on_block(s, t, "continuous"))
            self.mode = mode
        if open_microphone:
            try:
                self._open_microphone()
            except Exception as e:
                self.mode, self._segmenter = "idle", None
                self.error = f"Could not open the microphone: {e}"
                raise RuntimeError(self.error) from e

    def stop(self) -> np.ndarray | None:
        """Stop recording. In "sample" mode the recorded audio is returned instead of saved."""
        self._close_microphone()
        with self._lock:
            mode, self.mode = self.mode, "idle"
            self.level = 0.0
            if mode == "continuous" and self._segmenter:
                self._segmenter.close()
            self._segmenter = None
            samples = np.concatenate(self._buffer) if self._buffer else None
            start, self._buffer, self._buffer_start = self._buffer_start, [], None
        if mode == "sample":
            return samples
        if mode == "dictation" and samples is not None and len(samples) >= MIN_SPEECH_S * SAMPLE_RATE:
            self.on_block(samples, start, "dictation")
        return None

    # --- audio in ----------------------------------------------------------------

    def feed(self, samples: np.ndarray, now: datetime | None = None) -> None:
        """Accept 16 kHz mono float32 audio. `now` is the time at the end of `samples`."""
        now = now or now_local()
        with self._lock:
            if self.mode == "idle" or not len(samples):
                return
            self.level = min(1.0, float(np.sqrt(np.mean(samples**2))) * 8)
            if self.mode in ("dictation", "sample"):
                if self._buffer_start is None:
                    self._buffer_start = now - timedelta(seconds=len(samples) / SAMPLE_RATE)
                self._buffer.append(samples)
                buffered = sum(len(b) for b in self._buffer)
                if self.mode == "dictation" and buffered >= self.settings.max_block_s * SAMPLE_RATE:
                    self.on_block(np.concatenate(self._buffer), self._buffer_start, "dictation")
                    self._buffer, self._buffer_start = [], None
                return
            data = np.concatenate([self._pending, samples])
            usable = len(data) - len(data) % FRAME
            for i in range(0, usable, FRAME):
                behind = (len(data) - i - FRAME) / SAMPLE_RATE
                self._segmenter.feed(data[i : i + FRAME], now - timedelta(seconds=behind))
            self._pending = data[usable:]

    def _open_microphone(self) -> None:
        import sounddevice as sd

        device = self._device_index(self.settings.input_device)
        rate = SAMPLE_RATE
        try:
            sd.check_input_settings(device=device, samplerate=rate, channels=1)
        except Exception:
            # Some devices refuse 16 kHz; record at their native rate and convert.
            rate = int(sd.query_devices(device, "input")["default_samplerate"])

        def callback(indata, frames, time_info, status):
            if status:
                log.debug("audio status: %s", status)
            self._queue.put((indata[:, 0].copy(), now_local()))

        self._queue = queue.Queue()
        self._stream = sd.InputStream(device=device, samplerate=rate, channels=1, dtype="float32", blocksize=rate // 10, callback=callback)
        self._thread = threading.Thread(target=self._pump, args=(self._stream, self._queue, rate), daemon=True, name="recorder")
        self._stream.start()
        self._thread.start()

    def _pump(self, stream, q: queue.Queue, rate: int) -> None:
        """Move audio from the sound card callback into `feed`, off the realtime thread."""
        while True:
            item = q.get()
            if item is None:
                return
            samples, at = item
            try:
                self.feed(resample(samples, rate), at)
            except Exception:
                log.exception("recorder failed while handling audio")

    def _close_microphone(self) -> None:
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            stream.stop()
            stream.close()
        finally:
            self._queue.put(None)
            if self._thread:
                self._thread.join(timeout=5)
            self._thread = None

    # Windows lists each microphone once per audio system (MME, WASAPI, ...), so a
    # name alone is ambiguous. Only the default audio system's devices are offered.

    @staticmethod
    def _inputs() -> list[tuple[int, str]]:
        import sounddevice as sd

        api = sd.default.hostapi
        return [(i, d["name"]) for i, d in enumerate(sd.query_devices()) if d["max_input_channels"] > 0 and d["hostapi"] == api]

    @classmethod
    def _device_index(cls, name: str) -> int | None:
        """The chosen microphone, or None (system default) if none is chosen or it is unplugged."""
        return next((i for i, n in cls._inputs() if n == name), None) if name else None

    @classmethod
    def input_devices(cls) -> list[str]:
        try:
            return sorted({name for _, name in cls._inputs()})
        except Exception as e:
            log.debug("cannot list audio devices: %s", e)
            return []
