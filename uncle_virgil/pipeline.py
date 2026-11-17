"""Background processing: audio block -> transcript segments -> reminders.

Audio is written to disk before anything else, so a crash never loses a
recording. A block with audio but no transcript is picked up again at the
next start. Blocks in which no speech is found are deleted.
"""

from __future__ import annotations

import logging
import queue
import threading
from datetime import datetime, timedelta

import numpy as np

from . import audio

log = logging.getLogger(__name__)


class Pipeline:
    def __init__(self, app):
        self.app = app
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self.pending = 0
        self.last_error = ""

    def submit(self, samples: np.ndarray, start: datetime, mode: str) -> tuple[str, str]:
        """Save a recorded block and queue it for transcription."""
        date, block = self.app.storage.save_block_audio(start, samples)
        self._enqueue(date, block, mode)
        return date, block

    def _enqueue(self, date: str, block: str, mode: str) -> None:
        self.pending += 1
        self._queue.put((date, block, mode))

    def recover(self) -> int:
        """Queue blocks that were recorded but never transcribed. Call only while the queue is empty."""
        found = 0
        for date in self.app.storage.list_days():
            done = {s["block"] for s in self.app.storage.load_segments(date)}
            for block, _ in self.app.storage.audio_blocks(date):
                if block not in done:
                    self._enqueue(date, block, "continuous")
                    found += 1
        return found

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name="pipeline")
        self._thread.start()

    def stop(self) -> None:
        self._queue.put(None)

    def wait_idle(self, timeout: float = 600) -> bool:
        """Block until every queued block has been handled (used by tests and import)."""
        deadline = datetime.now() + timedelta(seconds=timeout)
        while self.pending and datetime.now() < deadline:
            threading.Event().wait(0.05)
        return not self.pending

    def _run(self) -> None:
        while (item := self._queue.get()) is not None:
            date, block, mode = item
            try:
                if self.app.engine.ready():
                    self.process(date, block, mode)
                    self.last_error = ""
                # Without models the audio simply stays on disk; recover() retries after they are downloaded.
            except Exception as e:
                log.exception("could not process %s %s", date, block)
                self.last_error = f"{date} {block}: {type(e).__name__}: {e}"
            finally:
                self.pending -= 1

    def process(self, date: str, block: str, mode: str) -> list[dict]:
        app = self.app
        path = app.storage.audio_file(date, block)
        if path is None:
            return []
        result = app.engine.transcribe(audio.read_audio(path), assume_speech=mode == "dictation")
        if not result.turns:
            app.storage.delete_block_audio(date, block)  # no speech: nothing worth keeping
            return []

        # Resolve in order of first appearance, so new voices are numbered Speaker 1, 2, ... as they speak.
        order = dict.fromkeys(turn.cluster for turn in result.turns)
        who = {c: app.speakers.resolve(date, result.voices[c].embedding, result.voices[c].seconds) for c in order}
        if mode == "dictation" and len(who) == 1:
            # A short dictated clip can be too brief to identify by voice; whoever pressed the button is the owner.
            cluster, (speaker_id, _) = next(iter(who.items()))
            owner = next((s for s in app.speakers.speakers if s["is_user"]), None)
            if speaker_id == "unk" and owner:
                who[cluster] = (owner["id"], owner["name"])

        start = app.storage.block_start(date, block).astimezone()
        segments = []
        for i, turn in enumerate(result.turns):
            speaker_id, name = who.get(turn.cluster, ("unk", "Unknown"))
            segments.append(
                {
                    "id": f"{date}T{block}-{i:03d}",
                    "start": (start + timedelta(seconds=turn.start)).isoformat(timespec="milliseconds"),
                    "end": (start + timedelta(seconds=turn.end)).isoformat(timespec="milliseconds"),
                    "speaker_id": speaker_id,
                    "speaker": name,
                    "text": turn.text,
                    "block": block,
                    "mode": mode,
                }
            )
        app.storage.append_segments(date, segments)
        app.reminders.handle_segments(segments)
        app.events.emit("block_transcribed", date=date, block=block, segments=segments)
        return segments
