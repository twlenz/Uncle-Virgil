"""Careful file handling, shared by everything that saves data.

- Writes go to a temporary file that is flushed to disk before it replaces
  the old one, so a crash or power cut leaves either the old or the new
  version, never a half-written file.
- On Windows, antivirus scanners and the search indexer briefly hold newly
  written files open, which makes a replace fail; it is retried.
- A file that cannot be read (damaged outside the app's control) is moved
  aside with a ".damaged-<time>" name, never deleted, and the app carries on.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

# Files found damaged and set aside since the app started, shown to the user.
set_aside: list[str] = []


def atomic_write(path: Path, text: str) -> None:
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    replace_with_retry(tmp, path)


def replace_with_retry(src: Path, dst: Path, attempts: int = 10) -> None:
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.05 * (attempt + 1))  # another program has the file open for a moment


def set_aside_damaged(path: Path, reason: Exception) -> Path:
    """Keep a damaged file under a new name so nothing is lost, and record that it happened."""
    path = Path(path)
    backup = path.with_name(f"{path.name}.damaged-{datetime.now():%Y%m%d-%H%M%S}")
    replace_with_retry(path, backup)
    log.error("%s could not be read (%s); kept as %s", path, reason, backup.name)
    set_aside.append(f"{path.name} (kept as {backup.name})")
    return backup


def load_json(path: Path, default):
    """Read a JSON file. A missing file gives `default`; a damaged one is set aside and gives `default`."""
    path = Path(path)
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        set_aside_damaged(path, e)
        return default
