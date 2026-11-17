"""Who is speaking: enrolled voices, and unnamed voices tracked per day.

Voiceprints of people you have named are kept in speakers.json and recognized
on later days. Voices you have not named get "Speaker 1, 2, ..." labels that
are only consistent within one day; their voiceprints stay in that day's
folder and are never matched across days.
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime
from pathlib import Path

import numpy as np

from .storage import Storage

UNKNOWN_MATCH = 0.5  # similarity needed to reuse a same-day "Speaker N" label
MIN_VOICE_S = 1.5  # shorter than this is too little audio to start a new "Speaker N"


def _unit(v) -> np.ndarray:
    v = np.asarray(v, dtype=np.float32)
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


class SpeakerStore:
    def __init__(self, storage: Storage, threshold: float = 0.55):
        self.storage = storage
        self.path = storage.home / "speakers.json"
        self.threshold = threshold
        self._lock = threading.RLock()
        self.speakers: list[dict] = []
        if self.path.exists():
            self.speakers = json.loads(self.path.read_text(encoding="utf-8"))["speakers"]

    # --- enrolled speakers -------------------------------------------------------

    def _save(self) -> None:
        self.storage._write(self.path, json.dumps({"speakers": self.speakers}, ensure_ascii=False))

    def public(self) -> list[dict]:
        return [{k: s[k] for k in ("id", "name", "is_user", "samples", "created")} for s in self.speakers]

    def name_of(self, speaker_id: str) -> str | None:
        return next((s["name"] for s in self.speakers if s["id"] == speaker_id), None)

    def user_ids(self) -> set[str]:
        return {s["id"] for s in self.speakers if s["is_user"]}

    def enroll(self, name: str, embedding: np.ndarray, is_user: bool = False) -> dict:
        """Add a voice sample for `name`, creating the speaker if needed."""
        name = name.strip()
        if not name:
            raise ValueError("A name is required")
        with self._lock:
            speaker = next((s for s in self.speakers if s["name"].lower() == name.lower()), None)
            if speaker:
                n = speaker["samples"]
                mean = (np.asarray(speaker["embedding"]) * n + _unit(embedding)) / (n + 1)
                speaker.update(embedding=_unit(mean).tolist(), samples=n + 1, is_user=speaker["is_user"] or is_user)
            else:
                speaker = {
                    "id": "spk-" + uuid.uuid4().hex[:8],
                    "name": name,
                    "is_user": is_user,
                    "samples": 1,
                    "created": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "embedding": _unit(embedding).tolist(),
                }
                self.speakers.append(speaker)
            self._save()
            return speaker

    def rename(self, speaker_id: str, name: str) -> None:
        with self._lock:
            for s in self.speakers:
                if s["id"] == speaker_id:
                    s["name"] = name.strip() or s["name"]
            self._save()
        self._rerender_days_with(speaker_id)

    def delete(self, speaker_id: str) -> None:
        """Forget a voiceprint. Past transcripts keep the name as plain text."""
        with self._lock:
            self.speakers = [s for s in self.speakers if s["id"] != speaker_id]
            self._save()

    def _rerender_days_with(self, speaker_id: str) -> None:
        for date in self.storage.list_days():
            if any(s.get("speaker_id") == speaker_id for s in self.storage.load_segments(date)):
                self.storage.render_transcript(date)

    # --- identification ----------------------------------------------------------

    def _unknowns_path(self, date: str) -> Path:
        return self.storage.day_dir(date) / "unknown_speakers.json"

    def _load_unknowns(self, date: str) -> list[dict]:
        path = self._unknowns_path(date)
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []

    def resolve(self, date: str, embedding: np.ndarray | None, seconds: float) -> tuple[str, str]:
        """Map a voiceprint heard on `date` to (speaker_id, display name)."""
        if embedding is None:
            return "unk", "Unknown"
        with self._lock:
            best = max(self.speakers, key=lambda s: float(np.dot(s["embedding"], embedding)), default=None)
            if best and float(np.dot(best["embedding"], embedding)) >= self.threshold:
                return best["id"], best["name"]

            unknowns = self._load_unknowns(date)
            match = max(unknowns, key=lambda u: float(np.dot(u["embedding"], embedding)), default=None)
            if match and float(np.dot(match["embedding"], embedding)) >= UNKNOWN_MATCH:
                total = match["seconds"] + seconds
                mean = (np.asarray(match["embedding"]) * match["seconds"] + embedding * seconds) / total
                match.update(embedding=_unit(mean).tolist(), seconds=total)
            elif seconds < MIN_VOICE_S:
                return "unk", "Unknown"
            else:
                n = max((u["n"] for u in unknowns), default=0) + 1
                match = {"id": f"unk-{n}", "n": n, "label": f"Speaker {n}", "seconds": seconds, "embedding": _unit(embedding).tolist()}
                unknowns.append(match)
            self._unknowns_path(date).parent.mkdir(parents=True, exist_ok=True)
            self.storage._write(self._unknowns_path(date), json.dumps(unknowns))
            return match["id"], match["label"]

    def name_unknown(self, date: str, unknown_id: str, name: str, is_user: bool = False) -> dict:
        """Give a name to one day's "Speaker N" so they are recognized from now on."""
        with self._lock:
            unknowns = self._load_unknowns(date)
            unknown = next((u for u in unknowns if u["id"] == unknown_id), None)
            if not unknown:
                raise KeyError(f"No unnamed speaker {unknown_id} on {date}")
            speaker = self.enroll(name, np.asarray(unknown["embedding"], dtype=np.float32), is_user)
            self.storage._write(self._unknowns_path(date), json.dumps([u for u in unknowns if u["id"] != unknown_id]))
            segments = self.storage.load_segments(date)
            for seg in segments:
                if seg.get("speaker_id") == unknown_id:
                    seg.update(speaker_id=speaker["id"], speaker=speaker["name"])
            self.storage.rewrite_segments(date, segments)
            return speaker
