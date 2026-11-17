import os
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from uncle_virgil.app import App

DATA = Path(__file__).parent / "data"


@pytest.fixture
def app(tmp_path, monkeypatch):
    """A full App on an empty data folder, with notifications captured instead of shown."""
    monkeypatch.setenv("UNCLE_VIRGIL_HOME", str(tmp_path))
    models = os.environ.get("UNCLE_VIRGIL_TEST_MODELS")
    if models:
        (tmp_path / "models").symlink_to(models)
    a = App(tmp_path)
    a.sent = []
    a.notifier.channels["desktop"] = lambda title, body: a.sent.append((title, body))
    return a


def seg(date, hhmmss, text, speaker="Tom", speaker_id="spk-tom", seconds=4.0, block=None, mode="continuous"):
    start = datetime.fromisoformat(f"{date}T{hhmmss}").astimezone()
    end = start.fromtimestamp(start.timestamp() + seconds).astimezone()
    return {
        "id": f"{date}T{hhmmss}",
        "start": start.isoformat(timespec="milliseconds"),
        "end": end.isoformat(timespec="milliseconds"),
        "speaker_id": speaker_id,
        "speaker": speaker,
        "text": text,
        "block": block or hhmmss.replace(":", "-"),
        "mode": mode,
    }


class FakeLLM:
    """Stands in for a language model: labels conversations by keyword and records every request."""

    name, model, off_device = "fake", "fake-1", False

    def __init__(self):
        self.calls = []

    def complete(self, system, prompt, schema=None):
        import json, re

        self.calls.append((system, prompt))
        if "conversations" in (schema or {}).get("properties", {}):
            out = []
            for cid, body in re.findall(r"### (c\d+) [^\n]*\n(.*?)(?=\n### |\Z)", prompt, re.S):
                task = "Budget report" if "budget" in body.lower() else "Hiring"
                sub = "Travel costs" if "travel" in body.lower() else "Forecast" if "forecast" in body.lower() else ""
                out.append({"id": cid, "task": task, "subtask": sub, "summary": f"Discussed {task.lower()}."})
            return json.dumps({"conversations": out, "notes": [{"time": "09:00", "kind": "todo", "text": "Email the new forecast to finance"}]})
        tasks = re.findall(r"^## (.+?) \(", prompt, re.M)
        return json.dumps({"overview": "A day of planning.", "tasks": [{"task": t, "summary": f"Worked on {t}.", "subtasks": []} for t in tasks]})


@pytest.fixture
def fake_llm():
    return FakeLLM()


def tone(seconds, rate=16000, level=0.3):
    """A loud stand-in for speech."""
    t = np.arange(int(seconds * rate)) / rate
    return (level * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


def silence(seconds, rate=16000):
    return np.zeros(int(seconds * rate), dtype=np.float32)
