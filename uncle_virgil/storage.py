"""Plain-file storage. One folder per day, readable without this app:

    days/2026-11-16/
        audio/14-03-22.flac     one file per recorded block, named by local start time
        segments.jsonl          one JSON object per utterance (the source of truth)
        transcript.md           the same content rendered for reading
        summary.md, summary.json
        unknown_speakers.json   voiceprints of unnamed speakers, this day only
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import date as Date
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterator

import numpy as np

from . import audio, files

log = logging.getLogger(__name__)

_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
NO_SPEECH = "no-speech"  # recordings in which no words were recognized; kept, not transcribed again
_BLOCK_RE = re.compile(r"^(\d{2})-(\d{2})-(\d{2})(?:-\d+)?$")


def parse_time(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


def group_conversations(segments: list[dict], gap_s: float) -> list[dict]:
    """Group utterances into conversations: a pause longer than gap_s starts a new one."""
    conversations: list[dict] = []
    last_end: datetime | None = None
    for seg in sorted(segments, key=lambda s: s["start"]):
        start, end = parse_time(seg["start"]), parse_time(seg["end"])
        if last_end is None or (start - last_end).total_seconds() > gap_s:
            conversations.append({"id": f"c{len(conversations) + 1}", "start": seg["start"], "end": seg["end"], "segments": []})
            last_end = end
        conv = conversations[-1]
        conv["segments"].append(seg)
        if end > last_end:
            last_end = end
        conv["end"] = last_end.isoformat()
    return conversations


class Storage:
    def __init__(self, home: Path):
        self.home = Path(home)
        self.days = self.home / "days"
        self.days.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # Set by the app so transcripts show a speaker's current name after a rename.
        self.name_resolver: Callable[[str], str | None] = lambda speaker_id: None

    # --- days ----------------------------------------------------------------

    def day_dir(self, date: str) -> Path:
        if not _DAY_RE.match(date):
            raise ValueError(f"bad date {date!r}")
        return self.days / date

    def list_days(self) -> list[str]:
        days = [p.name for p in self.days.iterdir() if p.is_dir() and _DAY_RE.match(p.name)]
        return sorted((d for d in days if (self.days / d / "segments.jsonl").exists() or self.audio_blocks(d)), reverse=True)

    # --- audio blocks ----------------------------------------------------------

    def save_block_audio(self, start: datetime, samples: np.ndarray) -> tuple[str, str]:
        """Write a new block as FLAC. Returns (date, block name)."""
        date = start.strftime("%Y-%m-%d")
        folder = self.day_dir(date) / "audio"
        with self._lock:
            block = start.strftime("%H-%M-%S")
            n = 1
            while any(folder.glob(f"{block}.*")):
                n += 1
                block = f"{start.strftime('%H-%M-%S')}-{n}"
            audio.write_flac(folder / f"{block}.flac", samples)
        return date, block

    def audio_file(self, date: str, block: str) -> Path | None:
        if not _BLOCK_RE.match(block):
            return None
        for path in sorted((self.day_dir(date) / "audio").glob(f"{block}.*")):
            if path.suffix in audio.AUDIO_EXTENSIONS:
                return path
        return None

    def audio_blocks(self, date: str) -> list[tuple[str, Path]]:
        folder = self.day_dir(date) / "audio"
        if not folder.is_dir():
            return []
        return sorted((p.stem, p) for p in folder.iterdir() if p.suffix in audio.AUDIO_EXTENSIONS and _BLOCK_RE.match(p.stem))

    @staticmethod
    def block_start(date: str, block: str) -> datetime:
        """Local start time of a block, from its folder and file name."""
        h, m, s = (int(x) for x in _BLOCK_RE.match(block).groups())
        return datetime.fromisoformat(date).replace(hour=h, minute=m, second=s)

    def set_aside_block(self, date: str, block: str) -> None:
        """Move a recording in which no words were recognized out of the way, keeping it.

        Recordings are never deleted automatically: the recognizer can miss very quiet
        or distant speech, and the audio may still matter to you."""
        path = self.audio_file(date, block)
        if path:
            folder = path.parent / NO_SPEECH
            folder.mkdir(exist_ok=True)
            files.replace_with_retry(path, folder / path.name)

    def no_speech_blocks(self, date: str) -> list[tuple[str, Path]]:
        folder = self.day_dir(date) / "audio" / NO_SPEECH
        if not folder.is_dir():
            return []
        return sorted((p.stem, p) for p in folder.iterdir() if p.suffix in audio.AUDIO_EXTENSIONS and _BLOCK_RE.match(p.stem))

    def delete_block_audio(self, date: str, block: str) -> None:
        path = self.audio_file(date, block)
        if path:
            path.unlink(missing_ok=True)

    # --- segments --------------------------------------------------------------

    def load_segments(self, date: str) -> list[dict]:
        path = self.day_dir(date) / "segments.jsonl"
        with self._lock:
            if not path.exists():
                return []
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        segments = []
        for number, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                segments.append(json.loads(line))
            except json.JSONDecodeError:
                # e.g. the last line was cut short by a power cut; the other lines are still good
                log.warning("skipping unreadable line %d of %s", number, path)
        return segments

    def append_segments(self, date: str, segments: list[dict]) -> None:
        if not segments:
            return
        folder = self.day_dir(date)
        folder.mkdir(parents=True, exist_ok=True)
        with self._lock:
            path = folder / "segments.jsonl"
            # If an earlier write was cut short, start on a fresh line so the new lines stay readable.
            needs_newline = path.exists() and path.stat().st_size > 0 and _last_byte(path) != b"\n"
            with path.open("a", encoding="utf-8") as f:
                if needs_newline:
                    f.write("\n")
                for seg in segments:
                    f.write(json.dumps(seg, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            self.render_transcript(date)

    def rewrite_segments(self, date: str, segments: list[dict]) -> None:
        folder = self.day_dir(date)
        folder.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._write(folder / "segments.jsonl", "".join(json.dumps(s, ensure_ascii=False) + "\n" for s in segments))
            self.render_transcript(date)

    def update_segment(self, date: str, segment_id: str, **changes) -> dict:
        with self._lock:
            segments = self.load_segments(date)
            seg = next((s for s in segments if s["id"] == segment_id), None)
            if seg is None:
                raise KeyError("That line no longer exists")
            seg.update(changes, edited=True)
            self.rewrite_segments(date, segments)
            return seg

    def delete_segments(self, date: str, segment_ids: set[str]) -> dict:
        """Delete lines of a transcript, and the audio of any recording left with no lines."""
        with self._lock:
            segments = self.load_segments(date)
            keep = [s for s in segments if s["id"] not in segment_ids]
            touched = {s["block"] for s in segments if s["id"] in segment_ids}
            orphaned = touched - {s["block"] for s in keep}
            for block in orphaned:
                self.delete_block_audio(date, block)
            self.rewrite_segments(date, keep)
            return {"lines": len(segments) - len(keep), "recordings": len(orphaned)}

    def search(self, query: str, limit: int = 200, names: list[str] = ()) -> list[dict]:
        """Lines containing every word of `query` (any case), newest day first.

        `names` are people's current names: a word that is part of a name is matched
        against who said the line, which older files may record under an earlier name."""
        words = query.lower().split()
        if not words:
            return []
        lowered_names = [n.lower() for n in names]
        text_words = [w for w in words if not any(w in n for n in lowered_names)]
        hits = []
        for date in self.list_days():
            path = self.day_dir(date) / "segments.jsonl"
            if not path.exists():
                continue
            with self._lock:
                raw = path.read_text(encoding="utf-8")
            lowered = raw.lower()
            if not all(w in lowered for w in text_words):
                continue  # cheap check on the whole file before reading it line by line
            for line in raw.splitlines():
                if not line.strip() or not all(w in line.lower() for w in text_words):
                    continue
                try:
                    seg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                haystack = f"{seg['text']} {self.speaker_name(seg)}".lower()
                if all(w in haystack for w in words):
                    hits.append({**seg, "date": date, "speaker": self.speaker_name(seg)})
                    if len(hits) >= limit:
                        return hits
        return hits

    def speaker_name(self, seg: dict) -> str:
        return self.name_resolver(seg.get("speaker_id", "")) or seg.get("speaker") or "Unknown"

    def render_transcript(self, date: str, gap_s: float = 120) -> None:
        segments = self.load_segments(date)
        day = Date.fromisoformat(date)
        out = [
            f"# Transcript: {day.strftime('%A, %B')} {day.day}, {day.year}",
            "",
            "_Written by Uncle Virgil from segments.jsonl. Edits to this file are overwritten._",
            "",
        ]
        for conv in group_conversations(segments, gap_s):
            start, end = parse_time(conv["start"]), parse_time(conv["end"])
            out += [f"## {start:%H:%M} to {end:%H:%M}", ""]
            for seg in conv["segments"]:
                out += [f"**{parse_time(seg['start']):%H:%M:%S} · {self.speaker_name(seg)}:** {seg['text']}", ""]
        with self._lock:
            self._write(self.day_dir(date) / "transcript.md", "\n".join(out))

    # --- summaries -------------------------------------------------------------

    def load_summary(self, date: str) -> dict | None:
        path = self.day_dir(date) / "summary.json"
        with self._lock:
            return files.load_json(path, None)

    def save_summary(self, date: str, data: dict, markdown: str) -> None:
        folder = self.day_dir(date)
        folder.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._write(folder / "summary.json", json.dumps(data, indent=2, ensure_ascii=False))
            self._write(folder / "summary.md", markdown)

    # --- audio housekeeping ------------------------------------------------------

    def _blocks_between(self, start: datetime, end: datetime) -> Iterator[tuple[str, str, Path]]:
        """Audio blocks whose (local) start time falls within [start, end]."""
        start, end = start.replace(tzinfo=None), end.replace(tzinfo=None)
        day = start.date()
        while day <= end.date():
            date = day.isoformat()
            for block, path in self.audio_blocks(date) + self.no_speech_blocks(date):
                if start <= self.block_start(date, block) <= end:
                    yield date, block, path
            day += timedelta(days=1)

    def audio_usage(self, start: datetime, end: datetime) -> dict:
        usage = {"files": 0, "bytes": 0, "compressed_files": 0, "seconds": 0.0}
        for _, _, path in self._blocks_between(start, end):
            usage["files"] += 1
            usage["bytes"] += path.stat().st_size
            usage["compressed_files"] += path.suffix == ".opus"
            usage["seconds"] += audio.duration_s(path)
        return usage

    def delete_audio(self, start: datetime, end: datetime) -> dict:
        """Delete audio in the range. Transcripts and summaries are kept."""
        result = {"files": 0, "bytes": 0}
        for _, _, path in list(self._blocks_between(start, end)):
            result["bytes"] += path.stat().st_size
            result["files"] += 1
            path.unlink()
        return result

    def compress_audio(self, start: datetime, end: datetime, progress: Callable[[int, int], None] | None = None) -> dict:
        """Re-encode lossless audio in the range as Opus (much smaller, still clear speech)."""
        todo = [(d, b, p) for d, b, p in self._blocks_between(start, end) if p.suffix != ".opus"]
        result = {"files": 0, "bytes_before": 0, "bytes_after": 0}
        for i, (_, _, path) in enumerate(todo):
            samples = audio.read_audio(path)
            target = path.with_suffix(".opus")
            tmp = path.with_suffix(".opus.tmp")
            audio.write_opus(tmp, samples)
            # Only drop the original once the new file is confirmed to hold the same length of audio.
            if abs(audio.duration_s(tmp) - len(samples) / audio.SAMPLE_RATE) > 0.5:
                tmp.unlink(missing_ok=True)
                raise RuntimeError(f"Compressing {path.name} produced a file of the wrong length; original kept")
            result["bytes_before"] += path.stat().st_size
            files.replace_with_retry(tmp, target)
            path.unlink()
            result["bytes_after"] += target.stat().st_size
            result["files"] += 1
            if progress:
                progress(i + 1, len(todo))
        return result

    # --- helpers ---------------------------------------------------------------

    @staticmethod
    def _write(path: Path, text: str) -> None:
        files.atomic_write(path, text)


def _last_byte(path: Path) -> bytes:
    with path.open("rb") as f:
        f.seek(-1, os.SEEK_END)
        return f.read(1)
