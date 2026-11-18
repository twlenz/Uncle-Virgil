"""The application object: owns every component and the background threads."""

from __future__ import annotations

import logging
import shutil
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from . import hotkey, llm
from .config import get_secret, load_settings, save_settings
from .events import EventBus, load_plugins
from .models import ModelManager, models_dir
from .notify import Notifier
from .pipeline import Pipeline
from .recorder import Recorder
from .reminders import ReminderService, ReminderStore
from .speakers import SpeakerStore
from .speech import SpeechEngine
from .storage import Storage
from .summary import summarize_day

log = logging.getLogger(__name__)

DISK_STOP_BYTES = 1_000_000_000  # recording stops below 1 GB free
DISK_RESUME_BYTES = 1_500_000_000
DISK_WARN_BYTES = 5_000_000_000
DISK_FULL_MESSAGE = "Recording stopped: only {:.1f} GB free on the drive. Free up space, or compress or delete old audio under Storage."


class App:
    def __init__(self, home: Path):
        self.home = Path(home)
        self.home.mkdir(parents=True, exist_ok=True)
        self.settings = load_settings(self.home)
        self.storage = Storage(self.home)
        self.models = ModelManager(models_dir(self.home))
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
        self._started = False
        self.hotkey: hotkey.Hotkey | None = None
        self.hotkey_error = ""

    # --- lifecycle ---------------------------------------------------------------

    def start(self) -> None:
        self._started = True
        self.plugins = load_plugins(self)
        self.pipeline.start()
        self.pipeline.recover()
        self.reminders.start()
        threading.Thread(target=self._daily_loop, daemon=True, name="daily-summary").start()
        threading.Thread(target=self._health_loop, daemon=True, name="microphone-check").start()
        self.apply_hotkey()
        self.resume_continuous()

    def resume_continuous(self) -> None:
        """Restart continuous recording if it is switched on but not running (at start-up, after a dictation)."""
        if not self.settings.recording.continuous or self.recorder.mode != "idle":
            return
        try:
            self.set_continuous(True)
        except RuntimeError as e:
            log.warning("continuous recording not resumed: %s", e)
            # Leave the switch on, and say why nothing is being recorded.
            self.recorder.error = f"Continuous recording is paused: {e}"

    def models_downloaded(self) -> None:
        self.pipeline.recover()
        self.resume_continuous()

    def _health_loop(self) -> None:
        ticks = 0
        while not self._stop.wait(2):
            try:
                self.recorder.check_microphone()
                if ticks % 15 == 0:  # every 30 seconds
                    self.check_disk()
            except Exception:
                log.exception("health check failed")
            ticks += 1

    # --- disk space ----------------------------------------------------------------------

    def disk_free(self) -> int:
        return shutil.disk_usage(self.home).free

    def check_disk(self) -> None:
        """Stop recording before the drive fills up (a full system drive makes Windows itself misbehave),
        and resume continuous recording once there is room again."""
        free = self.disk_free()
        if free < DISK_STOP_BYTES and self.recorder.mode in ("continuous", "dictation"):
            self.recorder.stop()  # saves what was already recorded
            self.recorder.error = DISK_FULL_MESSAGE.format(free / 1e9)
            self.notifier.send("Uncle Virgil stopped recording", DISK_FULL_MESSAGE.format(free / 1e9), ["desktop"])
            log.warning("recording stopped: %.2f GB free", free / 1e9)
        elif free >= DISK_RESUME_BYTES and self.recorder.error.startswith("Recording stopped: only"):
            self.recorder.error = ""
            self.resume_continuous()

    def _require_disk_space(self) -> None:
        free = self.disk_free()
        if free < DISK_STOP_BYTES:
            raise RuntimeError(DISK_FULL_MESSAGE.format(free / 1e9))

    def stop(self) -> None:
        self._stop.set()
        if self.hotkey:
            self.hotkey.stop()
        self.recorder.stop()
        self.reminders.stop()
        self.pipeline.stop()

    # --- recording -----------------------------------------------------------------

    def set_continuous(self, on: bool) -> None:
        if on and not self.engine.ready():
            raise RuntimeError("Download the speech models first (Settings > Speech models).")
        if on:
            self._require_disk_space()
        if on and self.recorder.mode == "idle":
            self.recorder.start("continuous")
        elif not on and self.recorder.mode == "continuous":
            self.recorder.stop()
        if not on and self.recorder.error.startswith("Continuous recording is paused"):
            self.recorder.error = ""
        if self.settings.recording.continuous != on:
            self.settings.recording.continuous = on
            save_settings(self.home, self.settings)

    def start_capture(self, mode: str) -> None:
        """Start a dictation or a voice sample; pauses continuous recording meanwhile."""
        self._require_disk_space()
        self.recorder.start(mode)

    def stop_capture(self):
        samples = self.recorder.stop()
        self.resume_continuous()
        return samples

    # --- language model and summaries --------------------------------------------------

    def llm(self):
        """The configured LLM provider, or None."""
        return llm.create(self.settings, get_secret)

    def owner_name(self) -> str:
        return next((s["name"] for s in self.speakers.speakers if s["is_user"]), self.settings.user_name)

    def summarize(self, date: str) -> dict:
        data = summarize_day(date, self.storage, self.llm(), self.settings, self.owner_name(), self.reminder_store.all(), self.speakers.user_ids())
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
            last = max(datetime.fromisoformat(s["end"]) for s in self.storage.load_segments(date))
            fresh_after = due.astimezone() if date == today else last
            if summary and datetime.fromisoformat(summary["generated_at"]) >= fresh_after:
                continue
            self._auto_summarized.add(date)
            if self.run_job(f"summary:{date}", lambda d=date: self.summarize(d) and None):
                started.append(date)
        return started

    # --- settings ------------------------------------------------------------------------

    def update_settings(self, data: dict) -> None:
        data.get("recording", {}).pop("continuous", None)  # changed only through set_continuous
        combo = data.get("recording", {}).get("hotkey")
        if isinstance(combo, str) and combo.strip():
            data["recording"]["hotkey"] = "+".join(p.strip().capitalize() for p in combo.split("+"))
            hotkey.parse(combo)  # refuse an unusable shortcut before saving anything
        old_combo = self.settings.recording.hotkey
        self.settings.update(data)
        save_settings(self.home, self.settings)
        self.speakers.threshold = self.settings.speech.speaker_match_threshold
        if self.settings.recording.hotkey != old_combo and self._started:
            self.apply_hotkey()

    # --- keyboard shortcut ---------------------------------------------------------------

    def apply_hotkey(self) -> None:
        """(Re)register the dictation shortcut from settings."""
        if self.hotkey:
            self.hotkey.stop()
            self.hotkey = None
        self.hotkey_error = ""
        combo = self.settings.recording.hotkey
        if not combo or not hotkey.supported():
            return
        try:
            candidate = hotkey.Hotkey(combo, self.toggle_dictation)
        except ValueError as e:
            self.hotkey_error = str(e)
            return
        if candidate.start():
            self.hotkey = candidate
        else:
            self.hotkey_error = candidate.error

    def toggle_dictation(self) -> None:
        """What the shortcut does: start dictating, or stop and save."""
        if self.recorder.mode == "dictation":
            self.stop_capture()
            hotkey.beep(False)
            return
        try:
            self.start_capture("dictation")
            hotkey.beep(True)
        except RuntimeError as e:
            self.notifier.send("Could not start dictation", str(e), ["desktop"])
