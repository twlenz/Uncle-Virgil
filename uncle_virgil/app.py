"""The application object: owns every component and the background threads."""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from . import llm
from .config import get_secret, load_settings, save_settings
from .events import EventBus, load_plugins
from .models import ModelManager
from .notify import Notifier
from .pipeline import Pipeline
from .recorder import Recorder
from .reminders import ReminderService, ReminderStore
from .speakers import SpeakerStore
from .speech import SpeechEngine
from .storage import Storage
from .summary import summarize_day

log = logging.getLogger(__name__)


class App:
    def __init__(self, home: Path):
        self.home = Path(home)
        self.home.mkdir(parents=True, exist_ok=True)
        self.settings = load_settings(self.home)
        self.storage = Storage(self.home)
        self.models = ModelManager(self.home / "models")
        self.engine = SpeechEngine(self.models, self.settings.speech)
        self.speakers = SpeakerStore(self.storage, self.settings.speech.speaker_match_threshold)
        self.storage.name_resolver = self.speakers.name_of
        self.events = EventBus()
        self.notifier = Notifier(self.settings, get_secret)
        self.reminder_store = ReminderStore(self.home)
        self.reminders = ReminderService(self.reminder_store, self)
        self.pipeline = Pipeline(self)
        self.recorder = Recorder(self.settings.recording, lambda: self.engine.new_vad(self.settings.recording.vad_threshold), self.pipeline.submit)
        self.jobs: dict[str, dict] = {}  # long-running work shown in the UI: key -> {state, result, error}
        self.plugins: list[str] = []
        self._stop = threading.Event()
        self._auto_summarized: set[str] = set()

    # --- lifecycle ---------------------------------------------------------------

    def start(self) -> None:
        self.plugins = load_plugins(self)
        self.pipeline.start()
        self.pipeline.recover()
        self.reminders.start()
        threading.Thread(target=self._daily_loop, daemon=True, name="daily-summary").start()
        if self.settings.recording.continuous:
            try:
                self.set_continuous(True)
            except RuntimeError as e:
                log.warning("continuous recording not resumed: %s", e)

    def stop(self) -> None:
        self._stop.set()
        self.recorder.stop()
        self.reminders.stop()
        self.pipeline.stop()

    # --- recording -----------------------------------------------------------------

    def set_continuous(self, on: bool) -> None:
        if on and not self.engine.ready():
            raise RuntimeError("Download the speech models first (Settings > Speech models).")
        if on and self.recorder.mode == "idle":
            self.recorder.start("continuous")
        elif not on and self.recorder.mode == "continuous":
            self.recorder.stop()
        if self.settings.recording.continuous != on:
            self.settings.recording.continuous = on
            save_settings(self.home, self.settings)

    def start_capture(self, mode: str) -> None:
        """Start a dictation or a voice sample; pauses continuous recording meanwhile."""
        self.recorder.start(mode)

    def stop_capture(self):
        samples = self.recorder.stop()
        if self.settings.recording.continuous and self.engine.ready():
            try:
                self.recorder.start("continuous")
            except RuntimeError as e:
                log.warning("continuous recording not resumed: %s", e)
        return samples

    # --- language model and summaries --------------------------------------------------

    def llm(self):
        """The configured LLM provider, or None."""
        return llm.create(self.settings, get_secret)

    def owner_name(self) -> str:
        return next((s["name"] for s in self.speakers.speakers if s["is_user"]), self.settings.user_name)

    def summarize(self, date: str) -> dict:
        data = summarize_day(date, self.storage, self.llm(), self.settings, self.owner_name(), self.reminder_store.all())
        self.events.emit("day_summarized", date=date, summary=data)
        return data

    def run_job(self, key: str, work: Callable[[], object]) -> bool:
        """Run `work` in the background, tracked under `key`. False if it is already running."""
        if self.jobs.get(key, {}).get("state") == "running":
            return False
        self.jobs[key] = {"state": "running", "result": None, "error": ""}

        def run():
            try:
                self.jobs[key] = {"state": "done", "result": work(), "error": ""}
            except Exception as e:
                log.exception("job %s failed", key)
                self.jobs[key] = {"state": "error", "result": None, "error": str(e) or type(e).__name__}

        threading.Thread(target=run, daemon=True, name=key).start()
        return True

    def _daily_loop(self) -> None:
        """Summarize today once the configured time passes, and yesterday if it was missed."""
        while not self._stop.wait(60):
            try:
                self.check_daily_summary(datetime.now())
            except Exception:
                log.exception("daily summary check failed")

    def check_daily_summary(self, now: datetime) -> list[str]:
        auto = self.settings.summary.auto_time
        if not auto:
            return []
        try:
            hour, minute = (int(x) for x in auto.split(":"))
            due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        except ValueError:
            return []
        today, yesterday = now.date().isoformat(), (now.date() - timedelta(days=1)).isoformat()
        wanted = [yesterday] if now < due else [yesterday, today]
        started = []
        for date in wanted:
            if date in self._auto_summarized or not self.storage.load_segments(date):
                continue
            summary = self.storage.load_summary(date)
            # Yesterday needs a summary made after its last recording; today needs one made after the set time.
            last = max(s["end"] for s in self.storage.load_segments(date))
            fresh_after = due.astimezone().isoformat() if date == today else last
            if summary and summary["generated_at"] >= fresh_after:
                continue
            self._auto_summarized.add(date)
            if self.run_job(f"summary:{date}", lambda d=date: self.summarize(d) and None):
                started.append(date)
        return started

    # --- settings ------------------------------------------------------------------------

    def update_settings(self, data: dict) -> None:
        data.get("recording", {}).pop("continuous", None)  # changed only through set_continuous
        self.settings.update(data)
        save_settings(self.home, self.settings)
        self.speakers.threshold = self.settings.speech.speaker_match_threshold
