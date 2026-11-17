"""Notes to self and reminders.

Detection works in two layers:
  1. Rules (always on, instant, offline): spoken triggers such as "note to
     self" or "remind me", plus a parser for the time that follows.
  2. Optional LLM pass (Settings > Reminders): catches looser phrasing.

A note with a time becomes a reminder; a note without one is kept as a note
for the daily summary.
"""

from __future__ import annotations

import difflib
import json
import logging
import re
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

# --- finding notes -----------------------------------------------------------------

# Speech recognizers mishear "note to self" ("note to South", "though to self",
# "out to self"), so the trigger is matched loosely: "to self" is rare enough in
# ordinary speech that any single word before it counts.
_TRIGGER = re.compile(
    r"\b(?:(?:[a-z']+ )?(?:to|two|too|2) self(?!-)"
    r"|(?:notes?|node|no|though|memo) (?:to|two|too|2) (?:my ?self|south|shelf)"
    r"|remind (?:me|myself)(?: (?:to|that|about))?"
    r"|reminder(?: (?:to|that|about))?(?=[:,]| to | that | about ))\b[\s:,.\-]*",
    re.IGNORECASE,
)
_IDEA = re.compile(r"^(?:an? |the |my )?idea\b[\s:,.\-]*", re.IGNORECASE)


def find_notes(text: str) -> list[dict]:
    """Notes spoken in one utterance: [{"kind": "note"|"idea", "text": ...}]."""
    matches = list(_TRIGGER.finditer(text))
    notes = []
    for i, m in enumerate(matches):
        body = text[m.end() :]
        if i + 1 < len(matches):  # another note follows: stop at the end of the sentence before it
            body = text[m.end() : matches[i + 1].start()]
            if (cut := max(body.rfind(c) for c in ".!?")) > 0:
                body = body[:cut]
        body = body.strip(" \t,.;:-")
        kind = "note"
        if idea := _IDEA.match(body):
            kind, body = "idea", body[idea.end() :].strip()
        if len(body) >= 3:
            notes.append({"kind": kind, "text": body[0].upper() + body[1:]})
    return notes


# --- understanding spoken times -----------------------------------------------------

_UNITS = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split()
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50}
_WEEKDAYS = "monday tuesday wednesday thursday friday saturday sunday".split()
_PART_OF_DAY = {"morning": 9, "afternoon": 14, "evening": 18, "tonight": 20}
_NOT_A_TIME = r"(?!\s*(?:%|percent|minutes?|mins?|hours?|days?|weeks?|people|dollars|bucks|times))"
_TIME_PATTERNS = [
    re.compile(r"\b(?:at|by|around|about|@)\s+(\d{1,2})(?:[:.](\d{2})| (\d{2}))?\s*(am|pm)?\b" + _NOT_A_TIME),
    re.compile(r"\b(\d{1,2})(?:[:.](\d{2}))?()\s*(am|pm)\b"),
    re.compile(r"\b(\d{1,2}):(\d{2})()()\b"),
    re.compile(r"\b(\d{1,2})()() o'?clock()\b"),
]
_RELATIVE = re.compile(r"\bin (\d+|an?|half an|a half) ?(min(?:ute)?s?|hours?|hrs?)\b")


def _normalize(text: str) -> str:
    t = text.lower().replace("’", "'")
    t = re.sub(r"\b([ap])\.?\s?m\b\.?", r"\1m", t)  # "p.m.", "P M" -> "pm"
    t = re.sub(r"\b(" + "|".join(_TENS) + r")[ -](" + "|".join(_UNITS[1:10]) + r")\b", lambda m: str(_TENS[m[1]] + _UNITS.index(m[2])), t)
    t = re.sub(r"\b(" + "|".join(_UNITS) + r")\b", lambda m: str(_UNITS.index(m[1])), t)
    t = re.sub(r"\b(" + "|".join(_TENS) + r")\b", lambda m: str(_TENS[m[1]]), t)
    t = re.sub(r"\bhalf past (\d{1,2})\b", r"\1:30", t)
    t = re.sub(r"\b(?:a )?quarter past (\d{1,2})\b", r"\1:15", t)
    t = re.sub(r"\b(?:a )?quarter (?:to|till|of) (\d{1,2})\b", lambda m: f"{(int(m[1]) - 2) % 12 + 1}:45", t)
    t = re.sub(r"\bnoon\b", "12:00 pm", t)
    t = re.sub(r"\bmidnight\b", "12:00 am", t)
    return t


def parse_due(text: str, now: datetime) -> datetime | None:
    """The time a spoken note refers to, or None if it names no time.

    A bare hour ("at 5") means the next time the clock shows it during the
    daytime (7 am to 9 pm): said at 2 pm, "at 5" is 5 pm today and "at 9" is
    9 am tomorrow.
    """
    t = _normalize(text)

    if m := _RELATIVE.search(t):
        amount = {"a": 1.0, "an": 1.0, "half an": 0.5, "a half": 0.5}.get(m[1]) or float(m[1])
        return now + (timedelta(minutes=amount) if m[2].startswith("min") else timedelta(hours=amount))

    day_offset: int | None = None  # None = no day was said
    weekday = False
    if re.search(r"\btomorrow\b", t):
        day_offset = 1
    elif re.search(r"\b(?:today|tonight|this (?:morning|afternoon|evening))\b", t):
        day_offset = 0
    elif m := re.search(r"\b(" + "|".join(_WEEKDAYS) + r")\b", t):
        day_offset, weekday = (_WEEKDAYS.index(m[1]) - now.weekday()) % 7, True
    part = next((p for p in _PART_OF_DAY if re.search(rf"\b{p}\b", t)), None)

    clock = None
    for pattern in _TIME_PATTERNS:
        for m in pattern.finditer(t):
            hour, minute, meridiem = int(m[1]), int(m[2] or m[3] or 0), m[4] or None
            if hour <= 23 and minute <= 59 and not (meridiem and not 1 <= hour <= 12):
                clock = (hour, minute, meridiem)
                break
        if clock:
            break

    if clock is None:
        if day_offset is None and part is None:
            return None
        base = (now + timedelta(days=day_offset or 0)).replace(hour=_PART_OF_DAY.get(part, 9), minute=0, second=0, microsecond=0)
        return _roll_forward(base, now, day_offset, weekday)

    hour, minute, meridiem = clock
    day = (now + timedelta(days=day_offset or 0)).replace(second=0, microsecond=0)
    if meridiem or hour > 12 or hour == 0:
        if meridiem == "pm" and hour < 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
        return _roll_forward(day.replace(hour=hour, minute=minute), now, day_offset, weekday)

    # No am/pm given: use the part of day if one was said, else the next occurrence.
    if part == "morning":
        candidates = [hour % 12]
    elif part in ("afternoon", "evening", "tonight"):
        candidates = [hour % 12 + 12]
    else:
        candidates = [hour % 12, hour % 12 + 12]
    options = [day.replace(hour=h, minute=minute) for h in candidates]
    if day_offset == 0 and not weekday:  # "today" or "tonight" was said
        future = [o for o in options if o > now]
        return min(future) if future else max(options)  # if already past, the caller drops it
    if not day_offset:  # no day said (or today's weekday): take each option's next occurrence
        step = timedelta(days=7 if weekday else 1)
        options = sorted(o if o > now else o + step for o in options)
    usual = [o for o in options if 7 <= o.hour < 21]  # prefer daytime: "at 9" is 9 am, "at 5" is 5 pm
    return (usual or options)[0]


def _roll_forward(due: datetime, now: datetime, day_offset: int | None, weekday: bool) -> datetime:
    """A time that has already passed means the next one, unless "today" was said."""
    if due > now:
        return due
    if weekday and day_offset == 0:
        return due + timedelta(days=7)
    return due + timedelta(days=1) if day_offset is None else due


# --- storage -------------------------------------------------------------------------


class ReminderStore:
    def __init__(self, home: Path):
        self.path = Path(home) / "reminders.json"
        self._lock = threading.RLock()
        self.items: list[dict] = []
        if self.path.exists():
            self.items = json.loads(self.path.read_text(encoding="utf-8"))

    def _save(self) -> None:
        tmp = self.path.with_name("reminders.json.tmp")
        tmp.write_text(json.dumps(self.items, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def all(self) -> list[dict]:
        with self._lock:
            return sorted(self.items, key=lambda r: r["due"])

    def add(self, text: str, due: datetime, lead_minutes: int, source: str, now: datetime, origin: dict | None = None) -> dict | None:
        """Add a reminder. Returns None if an equivalent one already exists."""
        with self._lock:
            for r in self.items:
                same_time = abs((datetime.fromisoformat(r["due"]) - due).total_seconds()) <= 120
                if same_time and difflib.SequenceMatcher(None, r["text"].lower(), text.lower()).ratio() > 0.6:
                    return None
            notify_at = max(due - timedelta(minutes=lead_minutes), now)
            reminder = {
                "id": uuid.uuid4().hex[:10],
                "text": text,
                "due": due.isoformat(timespec="seconds"),
                "notify_at": notify_at.isoformat(timespec="seconds"),
                "status": "pending",  # pending -> notified -> done; or missed
                "source": source,  # rule | llm | manual
                "created": now.isoformat(timespec="seconds"),
                "origin": origin or {},
            }
            self.items.append(reminder)
            self._save()
            return reminder

    def reschedule(self, reminder_id: str, now: datetime, lead_minutes: int, text: str | None = None, due: datetime | None = None, snooze_minutes: int | None = None) -> dict | None:
        """Change a reminder's text or time, or snooze it. Any change makes it pending again."""
        with self._lock:
            r = next((x for x in self.items if x["id"] == reminder_id), None)
            if r is None:
                return None
            if text is not None and text.strip():
                r["text"] = text.strip()
            if due is not None:
                r["due"] = due.isoformat(timespec="seconds")
                r["notify_at"] = max(due - timedelta(minutes=lead_minutes), now).isoformat(timespec="seconds")
                r["status"] = "pending"
            if snooze_minutes:
                r["notify_at"] = (now + timedelta(minutes=snooze_minutes)).isoformat(timespec="seconds")
                r["status"] = "pending"
            r["acknowledged"] = True
            self._save()
            return r

    def update(self, reminder_id: str, **changes) -> dict | None:
        with self._lock:
            for r in self.items:
                if r["id"] == reminder_id:
                    r.update(changes)
                    self._save()
                    return r
        return None

    def delete(self, reminder_id: str) -> None:
        with self._lock:
            self.items = [r for r in self.items if r["id"] != reminder_id]
            self._save()


# --- service ---------------------------------------------------------------------------

_MISSED_AFTER = timedelta(hours=6)  # a reminder this far past due (computer was off) is not sent
_TIME_WORDS = re.compile(r"\d|noon|midnight|tomorrow|tonight|morning|afternoon|evening|" + "|".join(_WEEKDAYS), re.IGNORECASE)

_LLM_SCHEMA = {
    "type": "object",
    "properties": {
        "reminders": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"text": {"type": "string"}, "due": {"type": "string"}},
                "required": ["text", "due"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["reminders"],
    "additionalProperties": False,
}


def describe_due(due: datetime, now: datetime) -> str:
    minutes = round((due - now).total_seconds() / 60)
    clock = f"{due:%I:%M %p}".lstrip("0")
    day = "" if due.date() == now.date() else f" on {due:%A}"
    if minutes < -1:
        return f"Was due at {clock}{day}"
    if minutes <= 1:
        return f"Now ({clock})"
    if minutes < 90:
        return f"In {minutes} minutes, at {clock}"
    return f"At {clock}{day}"


class ReminderService:
    def __init__(self, store: ReminderStore, app):
        self.store = store
        self.app = app  # provides settings, notifier, events, llm(), speakers
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # --- creating reminders from speech --------------------------------------------

    def handle_segments(self, segments: list[dict], now: datetime | None = None) -> list[dict]:
        """Look for reminders in newly transcribed utterances. Returns the ones created."""
        settings = self.app.settings.reminders
        user_ids = self.app.speakers.user_ids()
        mine = [s for s in segments if not user_ids or s.get("speaker_id") in user_ids or s.get("mode") == "dictation"]
        created = []
        for seg in mine:
            said_at = datetime.fromisoformat(seg["start"])
            for note in find_notes(seg["text"]):
                due = parse_due(note["text"], said_at)
                if due and due > said_at:
                    created.append(self._add(note["text"], due, "rule", seg, now))
        if settings.use_llm and mine and any(_TIME_WORDS.search(s["text"]) for s in mine):
            try:
                created += self._llm_pass(mine, now)
            except Exception as e:
                log.warning("LLM reminder pass failed: %s", e)
        return [r for r in created if r]

    def _add(self, text: str, due: datetime, source: str, seg: dict | None, now: datetime | None = None) -> dict | None:
        now = now or datetime.now().astimezone()
        if source != "manual" and due < now:
            return None  # e.g. found in an old recording that was imported: the time has passed, the note stays in that day's summary
        origin = {"date": seg["start"][:10], "segment": seg["id"]} if seg else {}
        reminder = self.store.add(text, due, self.app.settings.reminders.lead_minutes, source, now, origin)
        if reminder:
            self.app.events.emit("reminder_created", reminder=reminder)
            if source != "manual" and self.app.settings.reminders.confirm_new and "desktop" in self.app.settings.reminders.channels:
                # Say what was understood, so a misheard time can be fixed right away.
                self.app.notifier.send("Reminder set: " + reminder["text"], describe_due(due, now), ["desktop"])
        return reminder

    def add_manual(self, text: str, due: datetime) -> dict | None:
        return self._add(text.strip(), due, "manual", None)

    def _llm_pass(self, segments: list[dict], now: datetime | None = None) -> list[dict | None]:
        from .llm import complete_json

        provider = self.app.llm()
        if provider is None:
            return []
        said_at = datetime.fromisoformat(segments[0]["start"])
        lines = "\n".join(f"[{s['start'][11:16]}] {s['text']}" for s in segments)
        system = (
            "You extract reminders from a person's own spoken words, recorded by their personal voice notebook. "
            "Include only things the speaker clearly intends to do or attend at a definite time: appointments, "
            "meetings, deadlines, or explicit requests to be reminded. Skip anything vague or already past. "
            'Reply with JSON: {"reminders": [{"text": short description, "due": "YYYY-MM-DDTHH:MM" local time}]}. '
            "Use an empty list when there are none."
        )
        prompt = f"The words below were spoken on {said_at:%A, %Y-%m-%d}; the time each was said is in brackets.\n\n{lines}"
        out = []
        for item in complete_json(provider, system, prompt, _LLM_SCHEMA).get("reminders", []):
            try:
                due = datetime.fromisoformat(item["due"]).replace(tzinfo=said_at.tzinfo)
            except (ValueError, KeyError, TypeError):
                continue
            if due > said_at and item.get("text"):
                out.append(self._add(item["text"], due, "llm", segments[0], now))
        return out

    # --- sending ------------------------------------------------------------------------

    def tick(self, now: datetime | None = None) -> list[dict]:
        """Send every reminder whose notification time has come. Returns those sent."""
        now = now or datetime.now().astimezone()
        sent = []
        for r in self.store.all():
            if r["status"] != "pending" or datetime.fromisoformat(r["notify_at"]) > now:
                continue
            due = datetime.fromisoformat(r["due"])
            if now - due > _MISSED_AFTER:
                self.store.update(r["id"], status="missed")
                continue
            results = self.app.notifier.send("Reminder: " + r["text"], describe_due(due, now), self.app.settings.reminders.channels)
            r = self.store.update(r["id"], status="notified", delivery=results, notified_at=now.isoformat(timespec="seconds"))
            self.app.events.emit("reminder_fired", reminder=r)
            sent.append(r)
        return sent

    def start(self) -> None:
        def loop():
            while not self._stop.wait(15):
                try:
                    self.tick()
                except Exception:
                    log.exception("reminder check failed")

        self._thread = threading.Thread(target=loop, daemon=True, name="reminders")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
