"""Local HTTP API and UI host.

The server listens on 127.0.0.1 only. Every /api request must carry the
per-launch token (set as a cookie when the desktop window opens), so other
programs and web pages on this computer cannot read transcripts.
"""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, time, timedelta
from pathlib import Path

import numpy as np
from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import __version__, audio, autostart, files, hotkey, llm
from .app import DISK_WARN_BYTES, App
from .config import SECRET_NAMES, get_secret, set_secret
from .llm import LLMError
from .models import ASR_MODELS
from .recorder import BlockSegmenter, FRAME, Recorder
from .storage import group_conversations, parse_time

UI_DIR = Path(__file__).parent / "ui"
_LOCAL_HOSTS = ("127.0.0.1", "localhost")
_MEDIA_TYPES = {".flac": "audio/flac", ".opus": "audio/ogg", ".ogg": "audio/ogg", ".wav": "audio/wav", ".mp3": "audio/mpeg"}
MIN_SAMPLE_S = 8  # shortest voice sample accepted for enrollment


def _newly_heard(reminder: dict) -> bool:
    """A reminder picked up from speech in the last hour that the user has not looked at yet."""
    if reminder["source"] == "manual" or reminder.get("acknowledged") or reminder["status"] != "pending":
        return False
    return datetime.now().astimezone() - datetime.fromisoformat(reminder["created"]) < timedelta(hours=1)


def _parse_range(start: str, end: str) -> tuple[datetime, datetime]:
    """Accepts dates or date-times; a date-only end means the end of that day."""
    try:
        a, b = datetime.fromisoformat(start), datetime.fromisoformat(end)
    except (ValueError, TypeError) as e:
        raise HTTPException(400, "Choose a start and an end date") from e
    if len(end) <= 10:
        b = datetime.combine(b.date(), time.max)
    if b < a:
        raise HTTPException(400, "The end of the range is before its start")
    return a, b


def import_audio(app: App, pieces, start: datetime) -> int:
    """Run a recording, given as consecutive pieces of audio, through the same speech-gated block
    cutting as live audio. Returns the number of blocks queued for transcription."""
    count = 0

    def on_block(block, at):
        nonlocal count
        count += 1
        app.pipeline.submit(block, at, "import")

    segmenter = BlockSegmenter(app.engine.new_vad(app.settings.recording.vad_threshold), app.settings.recording, on_block)
    pending = np.zeros(0, dtype=np.float32)
    position = 0  # samples fed so far
    for piece in pieces:
        data = np.concatenate([pending, piece])
        usable = len(data) - len(data) % FRAME
        for i in range(0, usable, FRAME):
            position += FRAME
            segmenter.feed(data[i : i + FRAME], start + timedelta(seconds=position / audio.SAMPLE_RATE))
        pending = data[usable:]
    segmenter.close()
    return count


def create_app(app: App, token: str | None = None) -> FastAPI:
    token = token or secrets.token_urlsafe(24)
    api = FastAPI(title="Uncle Virgil", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
    api.state.token = token

    @api.middleware("http")
    async def guard(request: Request, call_next):
        host = (request.headers.get("host") or "").rsplit(":", 1)[0]
        if host not in _LOCAL_HOSTS:
            return JSONResponse({"detail": "forbidden"}, status_code=403)
        if request.url.path.startswith("/api/"):
            given = request.cookies.get("uv_token") or request.headers.get("authorization", "").removeprefix("Bearer ")
            if not secrets.compare_digest(given, token):
                return JSONResponse({"detail": "not authorized"}, status_code=401)
        return await call_next(request)

    @api.exception_handler(ValueError)
    @api.exception_handler(RuntimeError)
    @api.exception_handler(KeyError)
    @api.exception_handler(LLMError)
    async def user_error(request: Request, exc: Exception):
        return JSONResponse({"detail": str(exc.args[0]) if exc.args else type(exc).__name__}, status_code=400)

    @api.get("/")
    def index(token: str = ""):
        response = FileResponse(UI_DIR / "index.html", headers={"Cache-Control": "no-store"})
        if secrets.compare_digest(token, api.state.token):
            response.set_cookie("uv_token", token, httponly=True, samesite="strict")
        return response

    # --- status and recording --------------------------------------------------------

    @api.get("/api/status")
    def status():
        rec = app.recorder
        return {
            "version": __version__,
            "mode": rec.mode,
            "continuous": app.settings.recording.continuous,
            "speaking": rec.speaking,
            "level": rec.level,
            "recorder_error": rec.error,
            "pending": app.pipeline.pending,
            "pipeline_error": app.pipeline.last_error,
            "models": app.models.status(app.settings.speech.asr_model),
            "today": datetime.now().date().isoformat(),
            "alerts": [r for r in app.reminder_store.all() if r["status"] == "notified"],
            "heard": [r for r in app.reminder_store.all() if _newly_heard(r)],
            "owner_enrolled": bool(app.speakers.user_ids()),
            "has_reminders": bool(app.reminder_store.items),
            "disk_free_gb": round(app.disk_free() / 1e9, 1),
            "disk_low": app.disk_free() < DISK_WARN_BYTES,
            "damaged_files": files.set_aside,
            "hotkey": app.settings.recording.hotkey if app.hotkey else "",
            "llm_provider": app.settings.llm.provider,
            "setup_dismissed": app.settings.setup_dismissed,
        }

    @api.post("/api/record/dictation")
    def dictation(on: bool = Body(embed=True)):
        if on:
            app.start_capture("dictation")
        elif app.recorder.mode == "dictation":
            app.stop_capture()
        return status()

    @api.post("/api/record/continuous")
    def continuous(on: bool = Body(embed=True)):
        app.set_continuous(on)
        return status()

    @api.post("/api/import")
    def import_file(file: UploadFile = File(...), started: str = Form(""), ended: str = Form("")):
        """Add an existing recording (any source: phone, wearable, file).

        Give the time it started or ended; most recorders stamp a file with the time it ended."""
        if not app.engine.ready():
            raise HTTPException(400, "Download the speech models first (Settings > Speech models).")
        with tempfile.TemporaryDirectory() as tmp:
            # Copied to disk and read back in pieces, so a long recording never has to fit in memory.
            path = Path(tmp) / ("upload" + Path(file.filename or "").suffix.lower())
            with path.open("wb") as f:
                shutil.copyfileobj(file.file, f, 1 << 20)
            seconds, pieces = audio.open_stream(path, Path(tmp))
            length = timedelta(seconds=seconds)
            try:
                if started:
                    start = datetime.fromisoformat(started).astimezone()
                else:
                    start = (datetime.fromisoformat(ended).astimezone() if ended else datetime.now().astimezone()) - length
            except ValueError as e:
                raise HTTPException(400, "Choose when the recording was made") from e
            blocks = import_audio(app, pieces, start)
        return {"blocks": blocks, "seconds": seconds, "date": start.date().isoformat()}

    # --- days ------------------------------------------------------------------------------

    @api.get("/api/days")
    def days():
        return [{"date": d, "has_summary": (app.storage.day_dir(d) / "summary.json").exists()} for d in app.storage.list_days()]

    @api.get("/api/days/{date}")
    def day(date: str):
        segments = app.storage.load_segments(date)
        blocks = dict(app.storage.audio_blocks(date))
        for seg in segments:
            seg["speaker"] = app.storage.speaker_name(seg)
            seg["has_audio"] = seg["block"] in blocks
            seg["offset"] = max(0.0, (parse_time(seg["start"]).replace(tzinfo=None) - app.storage.block_start(date, seg["block"])).total_seconds())
        return {
            "date": date,
            "conversations": group_conversations(segments, app.settings.summary.conversation_gap_s),
            "summary": app.storage.load_summary(date),
            "job": app.jobs.get(f"summary:{date}"),
            "llm": app.settings.llm.provider,
            "speakers": app.speakers.choices(date),
            "untranscribed": app.pipeline.untranscribed(date),
            "no_speech": len(app.storage.no_speech_blocks(date)),
        }

    @api.patch("/api/days/{date}/segments/{segment_id}")
    def edit_segment(date: str, segment_id: str, text: str | None = Body(None, embed=True), speaker_id: str | None = Body(None, embed=True)):
        """Correct a line: its words, or who said it."""
        changes = {}
        if text is not None:
            if not text.strip():
                raise HTTPException(400, "A line cannot be empty. Delete it instead.")
            changes["text"] = text.strip()
        if speaker_id is not None:
            choice = next((c for c in app.speakers.choices(date) if c["id"] == speaker_id), None)
            if choice is None:
                raise HTTPException(400, "Unknown speaker")
            changes.update(speaker_id=choice["id"], speaker=choice["name"])
        return app.storage.update_segment(date, segment_id, **changes)

    @api.post("/api/days/{date}/segments/delete")
    def delete_segments(date: str, ids: list[str] = Body(embed=True)):
        """Delete lines (and any recording left with none): for things that should not have been recorded."""
        return app.storage.delete_segments(date, set(ids))

    @api.get("/api/search")
    def search(q: str):
        hits = app.storage.search(q, names=[s["name"] for s in app.speakers.speakers])
        for hit in hits:
            hit["has_audio"] = app.storage.audio_file(hit["date"], hit["block"]) is not None
        return hits

    @api.post("/api/days/{date}/summarize")
    def summarize(date: str):
        if not app.storage.load_segments(date):
            raise HTTPException(400, f"Nothing was recorded on {date}")
        app.run_job(f"summary:{date}", lambda: app.summarize(date) and None)
        return app.jobs[f"summary:{date}"]

    @api.get("/api/audio/{date}/{block}")
    def block_audio(date: str, block: str):
        path = app.storage.audio_file(date, block)
        if not path:
            raise HTTPException(404, "This audio has been deleted")
        return FileResponse(path, media_type=_MEDIA_TYPES.get(path.suffix, "application/octet-stream"))

    @api.post("/api/days/{date}/speakers/{unknown_id}")
    def name_unknown(date: str, unknown_id: str, name: str = Body(embed=True), is_user: bool = Body(False, embed=True)):
        return {k: v for k, v in app.speakers.name_unknown(date, unknown_id, name, is_user).items() if k != "embedding"}

    @api.post("/api/open-folder")
    def open_folder(date: str = Body("", embed=True)):
        """Show the data folder (or one day's folder) in the file manager."""
        path = app.storage.day_dir(date) if date else app.home
        path.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(path)  # noqa: S606 - opening the user's own data folder
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])
        return {"path": str(path)}

    # --- reminders ---------------------------------------------------------------------------

    @api.get("/api/reminders")
    def reminders():
        return app.reminder_store.all()

    @api.post("/api/reminders")
    def add_reminder(text: str = Body(embed=True), due: str = Body(embed=True)):
        if not text.strip():
            raise HTTPException(400, "Describe what to be reminded of")
        try:
            when = datetime.fromisoformat(due).astimezone()
        except ValueError as e:
            raise HTTPException(400, "Choose a date and time") from e
        reminder = app.reminders.add_manual(text, when)
        if reminder is None:
            raise HTTPException(400, "That reminder already exists")
        return reminder

    @api.patch("/api/reminders/{reminder_id}")
    def update_reminder(
        reminder_id: str,
        status: str | None = Body(None, embed=True),
        text: str | None = Body(None, embed=True),
        due: str | None = Body(None, embed=True),
        snooze_minutes: int | None = Body(None, embed=True),
        acknowledged: bool | None = Body(None, embed=True),
    ):
        if status is not None:
            if status not in ("pending", "done"):
                raise HTTPException(400, "bad status")
            return app.reminder_store.update(reminder_id, status=status, acknowledged=True) or Response(status_code=404)
        if acknowledged:
            return app.reminder_store.update(reminder_id, acknowledged=True) or Response(status_code=404)
        try:
            when = datetime.fromisoformat(due).astimezone() if due else None
        except ValueError as e:
            raise HTTPException(400, "Choose a date and time") from e
        now = datetime.now().astimezone()
        reminder = app.reminder_store.reschedule(reminder_id, now, app.settings.reminders.lead_minutes, text, when, snooze_minutes)
        return reminder or Response(status_code=404)

    @api.delete("/api/reminders/{reminder_id}")
    def delete_reminder(reminder_id: str):
        app.reminder_store.delete(reminder_id)
        return {}

    # --- people ------------------------------------------------------------------------------

    @api.get("/api/speakers")
    def speakers():
        return app.speakers.public()

    @api.post("/api/speakers/sample")
    def voice_sample(on: bool = Body(embed=True), name: str = Body("", embed=True), is_user: bool = Body(False, embed=True)):
        """Record a voice sample (on=true starts, on=false stops and enrolls `name`)."""
        if on:
            if not app.engine.ready():
                raise HTTPException(400, "Download the speech models first (Settings > Speech models).")
            app.start_capture("sample")
            return {}
        if app.recorder.mode != "sample":
            raise HTTPException(400, "No voice sample is being recorded")
        samples = app.stop_capture()
        if not name.strip():
            return {}  # cancelled
        if samples is None or len(samples) < MIN_SAMPLE_S * audio.SAMPLE_RATE:
            raise HTTPException(400, f"That sample was too short. Speak for at least {MIN_SAMPLE_S} seconds.")
        embedding = app.engine.embed(samples)
        if embedding is None:
            raise HTTPException(400, "No voice could be heard in that sample. Check the microphone and try again.")
        return {k: v for k, v in app.speakers.enroll(name, embedding, is_user).items() if k != "embedding"}

    @api.patch("/api/speakers/{speaker_id}")
    def rename_speaker(speaker_id: str, name: str = Body(embed=True)):
        app.speakers.rename(speaker_id, name)
        return {}

    @api.delete("/api/speakers/{speaker_id}")
    def delete_speaker(speaker_id: str):
        app.speakers.delete(speaker_id)
        return {}

    # --- storage -----------------------------------------------------------------------------

    @api.get("/api/storage/usage")
    def usage(start: str, end: str):
        return app.storage.audio_usage(*_parse_range(start, end))

    @api.post("/api/storage/delete")
    def delete_audio(start: str = Body(embed=True), end: str = Body(embed=True)):
        return app.storage.delete_audio(*_parse_range(start, end))

    @api.post("/api/storage/compress")
    def compress_audio(start: str = Body(embed=True), end: str = Body(embed=True)):
        a, b = _parse_range(start, end)
        progress = {"done": 0, "total": 0}

        def work():
            return app.storage.compress_audio(a, b, lambda done, total: progress.update(done=done, total=total))

        if not app.run_job("compress", work):
            raise HTTPException(400, "A compression is already running")
        app.jobs["compress"]["progress"] = progress
        return app.jobs["compress"]

    @api.get("/api/jobs/{key}")
    def job(key: str):
        return app.jobs.get(key) or {"state": "none"}

    # --- settings ----------------------------------------------------------------------------

    @api.get("/api/settings")
    def settings():
        return {
            "settings": app.settings.to_dict(),
            "secrets": {name: bool(get_secret(name)) for name in SECRET_NAMES},
            "providers": llm.provider_names(),
            "channels": sorted(app.notifier.channels),
            "input_devices": Recorder.input_devices(),
            "asr_models": [{"key": m.key, "label": m.label, "size_mb": m.size_mb, "installed": app.models.installed(m)} for m in ASR_MODELS.values()],
            "home": str(app.home),
            "plugins": app.plugins,
            "autostart": {"supported": autostart.supported(), "enabled": autostart.enabled()},
            "hotkey": {"supported": hotkey.supported(), "active": bool(app.hotkey), "error": app.hotkey_error},
        }

    @api.put("/api/settings")
    def put_settings(data: dict = Body(...)):
        if data.get("speech", {}).get("asr_model", app.settings.speech.asr_model) not in ASR_MODELS:
            raise HTTPException(400, "Unknown speech model")
        app.update_settings(data)
        return settings()

    @api.post("/api/secrets")
    def put_secret(name: str = Body(embed=True), value: str = Body(embed=True)):
        try:
            set_secret(name, value.strip())
        except ValueError:
            raise
        except Exception as e:  # no usable credential store on this system
            raise HTTPException(400, f"The key could not be saved in the system credential store: {e}") from e
        return {"set": bool(value.strip())}

    @api.post("/api/autostart")
    def put_autostart(on: bool = Body(embed=True)):
        autostart.set_enabled(on, app.home)
        return {"enabled": autostart.enabled()}

    @api.post("/api/models/download")
    def download_models():
        started = app.models.download_missing(app.settings.speech.asr_model, on_done=app.models_downloaded)
        return {"started": started}

    @api.post("/api/llm/test")
    def test_llm():
        provider = app.llm()
        if provider is None:
            raise HTTPException(400, "No language model is selected")
        return {"reply": provider.complete("You are a connection test.", "Reply with the single word: ready").strip()[:200], "off_device": provider.off_device}

    @api.post("/api/notify/test")
    def test_notify(channel: str = Body(embed=True)):
        result = app.notifier.send("Uncle Virgil test", "Notifications are working.", [channel])[channel]
        if result != "ok":
            raise HTTPException(400, result)
        return {"result": result}

    api.mount("/", StaticFiles(directory=UI_DIR), name="ui")
    return api
