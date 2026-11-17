"""Settings (config.toml in the data folder) and secrets (OS keyring)."""

from __future__ import annotations

import logging
import math
import os
import re
import tomllib
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path

import tomli_w

log = logging.getLogger(__name__)


def default_home() -> Path:
    env = os.environ.get("UNCLE_VIRGIL_HOME")
    return Path(env) if env else Path.home() / "UncleVirgil"


@dataclass
class RecordingSettings:
    input_device: str = ""  # empty = system default microphone
    continuous: bool = False  # resume continuous recording when the app starts
    block_silence_s: float = 6.0  # this much silence ends a recorded block
    max_block_s: float = 300.0  # long conversations are cut into blocks this long
    pre_roll_s: float = 1.0  # audio kept from before speech was detected
    vad_threshold: float = 0.5


@dataclass
class SpeechSettings:
    asr_model: str = "parakeet-tdt-0.6b-v2"
    threads: int = 0  # 0 = automatic
    speaker_match_threshold: float = 0.55


@dataclass
class LLMSettings:
    provider: str = "none"  # none | anthropic | openai_compatible | <plugin name>
    model: str = ""
    base_url: str = "http://localhost:11434/v1"  # openai_compatible only
    max_chars_per_request: int = 24000


@dataclass
class SummarySettings:
    auto_time: str = "21:00"  # local HH:MM to summarize the day; empty disables
    conversation_gap_s: int = 120  # a pause this long starts a new conversation
    gap_attribution_min: int = 20  # quiet time after a conversation counted toward its task


@dataclass
class ReminderSettings:
    lead_minutes: int = 30
    use_llm: bool = False
    confirm_new: bool = True  # desktop notification when a reminder is heard, so mistakes are caught early
    channels: list[str] = field(default_factory=lambda: ["desktop"])


@dataclass
class NotifySettings:
    email_to: str = ""
    email_from: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    ntfy_server: str = "https://ntfy.sh"
    ntfy_topic: str = ""


@dataclass
class Settings:
    user_name: str = "Me"
    setup_dismissed: bool = False
    recording: RecordingSettings = field(default_factory=RecordingSettings)
    speech: SpeechSettings = field(default_factory=SpeechSettings)
    llm: LLMSettings = field(default_factory=LLMSettings)
    summary: SummarySettings = field(default_factory=SummarySettings)
    reminders: ReminderSettings = field(default_factory=ReminderSettings)
    notify: NotifySettings = field(default_factory=NotifySettings)

    def to_dict(self) -> dict:
        return asdict(self)

    def update(self, data: dict) -> None:
        """Merge a (possibly partial) nested dict, ignoring unknown keys, then bring values into range."""
        _merge(self, data)
        for (section, key), (low, high) in LIMITS.items():
            obj = getattr(self, section)
            value = getattr(obj, key)
            setattr(obj, key, type(value)(min(max(value, low), high)))
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", self.summary.auto_time):
            self.summary.auto_time = ""


# Allowed range of each number. An out-of-range value (say 0 seconds of silence
# ending a recording) would quietly break the app, so it is moved into range.
LIMITS = {
    ("recording", "block_silence_s"): (1, 120),
    ("recording", "max_block_s"): (30, 3600),
    ("recording", "pre_roll_s"): (0, 5),
    ("recording", "vad_threshold"): (0.1, 0.95),
    ("speech", "threads"): (0, 64),
    ("speech", "speaker_match_threshold"): (0.2, 0.95),
    ("llm", "max_chars_per_request"): (2000, 2_000_000),
    ("summary", "conversation_gap_s"): (10, 3600),
    ("summary", "gap_attribution_min"): (0, 240),
    ("reminders", "lead_minutes"): (0, 1440),
    ("notify", "smtp_port"): (1, 65535),
}


def _merge(obj, data: dict) -> None:
    for f in fields(obj):
        if f.name not in data:
            continue
        current, new = getattr(obj, f.name), data[f.name]
        if is_dataclass(current):
            if isinstance(new, dict):
                _merge(current, new)
        elif isinstance(current, bool):
            setattr(obj, f.name, bool(new))
        elif isinstance(current, (int, float)) and isinstance(new, (int, float)) and not isinstance(new, bool) and math.isfinite(new):
            setattr(obj, f.name, type(current)(new))
        elif isinstance(current, list) and isinstance(new, list):
            setattr(obj, f.name, [str(x) for x in new])
        elif isinstance(current, str) and isinstance(new, str):
            setattr(obj, f.name, new)


def load_settings(home: Path) -> Settings:
    settings = Settings()
    path = home / "config.toml"
    if path.exists():
        try:
            settings.update(tomllib.loads(path.read_text(encoding="utf-8")))
        except (tomllib.TOMLDecodeError, OSError) as e:
            log.error("Could not read %s (%s); using defaults", path, e)
    return settings


def save_settings(home: Path, settings: Settings) -> None:
    home.mkdir(parents=True, exist_ok=True)
    tmp = home / "config.toml.tmp"
    tmp.write_text(tomli_w.dumps(settings.to_dict()), encoding="utf-8")
    tmp.replace(home / "config.toml")


# --- secrets -----------------------------------------------------------------
# API keys and passwords never go in config.toml. They live in the operating
# system's credential store (Windows Credential Manager) via `keyring`, with
# environment variables as an override.

SECRET_NAMES = ("anthropic_api_key", "openai_api_key", "smtp_password", "ntfy_token")
_ENV_ALIASES = {"anthropic_api_key": "ANTHROPIC_API_KEY", "openai_api_key": "OPENAI_API_KEY"}
_SERVICE = "uncle-virgil"


def get_secret(name: str) -> str:
    for env in (f"UNCLE_VIRGIL_{name.upper()}", _ENV_ALIASES.get(name, "")):
        if env and os.environ.get(env):
            return os.environ[env]
    try:
        import keyring

        return keyring.get_password(_SERVICE, name) or ""
    except Exception as e:  # no keyring backend available
        log.debug("keyring unavailable: %s", e)
        return ""


def set_secret(name: str, value: str) -> None:
    if name not in SECRET_NAMES:
        raise ValueError(f"unknown secret {name!r}")
    import keyring

    if value:
        keyring.set_password(_SERVICE, name, value)
    else:
        try:
            keyring.delete_password(_SERVICE, name)
        except keyring.errors.PasswordDeleteError:
            pass
