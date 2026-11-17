"""End-of-day summary: work performed, time per task, and notes to self.

How it is computed:
  1. The day's utterances are grouped into conversations (split at long pauses).
  2. The LLM labels each conversation with a task (and subtask) and a short
     summary, and picks out notes to self. Long days are sent in several
     requests, never truncated.
  3. Time per task is computed here, from timestamps, not by the LLM.
  4. The LLM writes the per-task and whole-day prose from step 2's summaries.

Without an LLM, the summary still lists totals, rule-detected notes and reminders.
"""

from __future__ import annotations

import difflib
from collections import defaultdict
from datetime import date as Date
from datetime import datetime

from .config import Settings
from .llm import LLMProvider, complete_json
from .reminders import find_notes
from .storage import Storage, group_conversations, parse_time

_LABEL_SCHEMA = {
    "type": "object",
    "properties": {
        "conversations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "task": {"type": "string"}, "subtask": {"type": "string"}, "summary": {"type": "string"}},
                "required": ["id", "task", "subtask", "summary"],
                "additionalProperties": False,
            },
        },
        "notes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"time": {"type": "string"}, "kind": {"type": "string", "enum": ["note", "idea", "todo"]}, "text": {"type": "string"}},
                "required": ["time", "kind", "text"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["conversations", "notes"],
    "additionalProperties": False,
}

_SYNTHESIS_SCHEMA = {
    "type": "object",
    "properties": {
        "overview": {"type": "string"},
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "task": {"type": "string"},
                    "summary": {"type": "string"},
                    "subtasks": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"subtask": {"type": "string"}, "summary": {"type": "string"}},
                            "required": ["subtask", "summary"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["task", "summary", "subtasks"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["overview", "tasks"],
    "additionalProperties": False,
}

_LABEL_SYSTEM = """You analyze one person's day from transcripts recorded by their personal voice notebook. \
The notebook belongs to {owner}; other speakers are people they talked with. The transcripts come from speech \
recognition, so expect misheard words and infer what was meant.

For each conversation, decide which task or project {owner} was working on:
- Give the task a short, specific name of two to five words, such as "Quarterly budget report".
- Reuse a name from the existing task list whenever the conversation belongs to that task.
- Fill in a subtask only when the task clearly has distinct parts; otherwise use an empty string.
- Use "Personal" for chat unrelated to work and "Unclear" when there is too little to tell.
- Summarize in one or two sentences what was done or decided.

Also collect notes: things {owner} said as a note to self, an idea, or a to-do. These are often introduced by \
"note to self" or "remind me", and conversations marked as dictation are usually notes in their entirety. Give \
the time each was said (HH:MM), its kind, and its content in {owner}'s words with recognition errors cleaned up. \
Include only what {owner} said.

Reply with JSON: {{"conversations": [{{"id", "task", "subtask", "summary"}}], "notes": [{{"time", "kind", "text"}}]}}, \
with exactly one entry per conversation id you were given. "kind" is one of note, idea, todo."""

_SYNTHESIS_SYSTEM = """You write the end-of-day summary for {owner}'s personal voice notebook. You are given the \
tasks {owner} worked on today, the time spent on each, and short summaries of the conversations behind them.

Write an overview of the day in two to four sentences. Then, for each task, summarize the work performed: what was \
done, what was decided, and what is still open. For each listed subtask, write one sentence. Use plain past-tense \
prose, address nobody, and state only what the conversation summaries support. Keep task and subtask names exactly \
as given.

Reply with JSON: {{"overview", "tasks": [{{"task", "summary", "subtasks": [{{"subtask", "summary"}}]}}]}}."""


def format_duration(seconds: float) -> str:
    minutes = round(seconds / 60)
    if minutes < 1:
        return "under 1m"
    return f"{minutes // 60}h {minutes % 60:02d}m" if minutes >= 60 else f"{minutes}m"


def _render_conversation(conv: dict, storage: Storage) -> str:
    start, end = parse_time(conv["start"]), parse_time(conv["end"])
    dictated = all(s.get("mode") == "dictation" for s in conv["segments"])
    head = f"### {conv['id']} ({start:%H:%M} to {end:%H:%M}{', dictation' if dictated else ''})"
    return "\n".join([head, *(f"[{parse_time(s['start']):%H:%M}] {storage.speaker_name(s)}: {s['text']}" for s in conv["segments"])])


def _split_oversize(conversations: list[dict], max_chars: int) -> list[dict]:
    """Cut conversations too long for one request into consecutive parts, so nothing is dropped."""
    out = []
    for conv in conversations:
        part: list[dict] = []
        size = 0
        for seg in conv["segments"]:
            if part and size + len(seg["text"]) > max_chars:
                out.append({"segments": part})
                part, size = [], 0
            part.append(seg)
            size += len(seg["text"]) + 30
        out.append({"segments": part})
    for i, conv in enumerate(out):
        conv.update(id=f"c{i + 1}", start=conv["segments"][0]["start"], end=max(s["end"] for s in conv["segments"]))
    return out


def _batches(texts: list[str], max_chars: int) -> list[list[int]]:
    batches: list[list[int]] = [[]]
    size = 0
    for i, text in enumerate(texts):
        if batches[-1] and size + len(text) > max_chars:
            batches.append([])
            size = 0
        batches[-1].append(i)
        size += len(text)
    return batches


def _similar(a: str, b: str) -> bool:
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio() > 0.75


def _tracked_seconds(conversations: list[dict], gap_minutes: int) -> list[float]:
    """Seconds credited to each conversation: its own length plus the quiet time
    that follows it, up to gap_minutes (work usually continues after the talking stops)."""
    seconds = []
    for i, conv in enumerate(conversations):
        start, end = parse_time(conv["start"]), parse_time(conv["end"])
        gap = (parse_time(conversations[i + 1]["start"]) - end).total_seconds() if i + 1 < len(conversations) else 0.0
        seconds.append((end - start).total_seconds() + min(max(gap, 0.0), gap_minutes * 60))
    return seconds


def summarize_day(
    date: str,
    storage: Storage,
    provider: LLMProvider | None,
    settings: Settings,
    owner: str,
    reminders: list[dict] = (),
    owner_ids: set[str] = frozenset(),
) -> dict:
    segments = storage.load_segments(date)
    if not segments:
        raise ValueError(f"Nothing was recorded on {date}")
    max_chars = settings.llm.max_chars_per_request
    conversations = _split_oversize(group_conversations(segments, settings.summary.conversation_gap_s), max_chars)
    seconds = _tracked_seconds(conversations, settings.summary.gap_attribution_min)

    notes = []
    for seg in segments:
        if owner_ids and seg.get("speaker_id") not in owner_ids and seg.get("mode") != "dictation":
            continue  # someone else's "note to self" is not yours
        notes += [{"time": seg["start"][11:16], **n} for n in find_notes(seg["text"])]

    data = {
        "date": date,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "llm": None,
        "overview": "",
        "tasks": [],
        "notes": notes,
        "reminders": [{"text": r["text"], "due": r["due"]} for r in reminders if r.get("origin", {}).get("date") == date],
        "totals": {
            "first": conversations[0]["start"],
            "last": max(c["end"] for c in conversations),
            "conversations": len(conversations),
            "speech_seconds": sum((parse_time(s["end"]) - parse_time(s["start"])).total_seconds() for s in segments),
            "tracked_seconds": sum(seconds),
        },
        "gap_attribution_min": settings.summary.gap_attribution_min,
    }

    if provider is not None:
        labels = _label_conversations(conversations, storage, provider, owner, max_chars, data["notes"])
        data["tasks"] = _build_tasks(conversations, labels, seconds)
        _synthesize(data, provider, owner)
        data["llm"] = {"provider": provider.name, "model": getattr(provider, "model", ""), "off_device": provider.off_device}

    data["notes"].sort(key=lambda n: n["time"])
    storage.save_summary(date, data, render_markdown(data))
    return data


def _label_conversations(conversations, storage, provider, owner, max_chars, notes: list[dict]) -> dict[str, dict]:
    texts = [_render_conversation(c, storage) for c in conversations]
    labels: dict[str, dict] = {}
    known_tasks: list[str] = []
    for batch in _batches(texts, max_chars):
        existing = "\n".join(f"- {t}" for t in known_tasks) or "(none yet)"
        prompt = f"Existing tasks:\n{existing}\n\nConversations:\n\n" + "\n\n".join(texts[i] for i in batch)
        reply = complete_json(provider, _LABEL_SYSTEM.format(owner=owner), prompt, _LABEL_SCHEMA)
        wanted = {conversations[i]["id"] for i in batch}
        for item in reply.get("conversations", []):
            if isinstance(item, dict) and item.get("id") in wanted:
                task = str(item.get("task") or "Unclear").strip()
                task = next((k for k in known_tasks if k.lower() == task.lower()), task)
                if task not in known_tasks:
                    known_tasks.append(task)
                labels[item["id"]] = {"task": task, "subtask": str(item.get("subtask") or "").strip(), "summary": str(item.get("summary") or "").strip()}
        for item in reply.get("notes", []):
            if isinstance(item, dict) and item.get("text") and not any(_similar(item["text"], n["text"]) for n in notes):
                notes.append({"time": str(item.get("time", ""))[:5], "kind": item.get("kind", "note"), "text": item["text"]})
    return labels


def _build_tasks(conversations, labels, seconds) -> list[dict]:
    total = sum(seconds) or 1.0
    tasks: dict[str, dict] = {}
    for conv, secs in zip(conversations, seconds):
        label = labels.get(conv["id"], {"task": "Unclear", "subtask": "", "summary": ""})
        task = tasks.setdefault(label["task"], {"task": label["task"], "seconds": 0.0, "summary": "", "subtasks": defaultdict(float), "conversations": []})
        task["seconds"] += secs
        if label["subtask"]:
            task["subtasks"][label["subtask"]] += secs
        task["conversations"].append({"start": conv["start"], "end": conv["end"], "subtask": label["subtask"], "summary": label["summary"]})
    out = []
    for task in sorted(tasks.values(), key=lambda t: -t["seconds"]):
        # A breakdown is only meaningful when a task has more than one distinct part.
        subtasks = task["subtasks"] if len(task["subtasks"]) > 1 else {}
        task["subtasks"] = [{"subtask": name, "seconds": s, "share": s / total, "summary": ""} for name, s in sorted(subtasks.items(), key=lambda kv: -kv[1])]
        task["share"] = task["seconds"] / total
        out.append(task)
    return out


def _synthesize(data: dict, provider: LLMProvider, owner: str) -> None:
    lines = []
    for task in data["tasks"]:
        lines.append(f"## {task['task']} ({format_duration(task['seconds'])})")
        if task["subtasks"]:
            lines.append("Subtasks: " + ", ".join(s["subtask"] for s in task["subtasks"]))
        for conv in task["conversations"]:
            sub = f" [{conv['subtask']}]" if conv["subtask"] else ""
            lines.append(f"- {conv['start'][11:16]}{sub}: {conv['summary']}")
        lines.append("")
    reply = complete_json(provider, _SYNTHESIS_SYSTEM.format(owner=owner), "\n".join(lines), _SYNTHESIS_SCHEMA)
    data["overview"] = str(reply.get("overview", "")).strip()
    written = {str(t.get("task", "")).lower(): t for t in reply.get("tasks", []) if isinstance(t, dict)}
    for task in data["tasks"]:
        entry = written.get(task["task"].lower(), {})
        # If the model skipped a task, fall back to its conversation summaries so nothing is blank.
        task["summary"] = str(entry.get("summary") or " ".join(c["summary"] for c in task["conversations"])).strip()
        sub_written = {str(s.get("subtask", "")).lower(): s.get("summary", "") for s in entry.get("subtasks", []) if isinstance(s, dict)}
        for sub in task["subtasks"]:
            sub["summary"] = str(sub_written.get(sub["subtask"].lower(), "")).strip()


def render_markdown(data: dict) -> str:
    day = Date.fromisoformat(data["date"])
    totals = data["totals"]
    out = [f"# Daily summary: {day.strftime('%A, %B')} {day.day}, {day.year}", ""]
    out += [
        f"Recorded {totals['conversations']} conversations between {parse_time(totals['first']):%H:%M} and {parse_time(totals['last']):%H:%M}; "
        f"{format_duration(totals['speech_seconds'])} of speech.",
        "",
    ]
    if data["overview"]:
        out += [data["overview"], ""]
    if data["tasks"]:
        out += ["## Work performed", ""]
        for task in data["tasks"]:
            out += [f"### {task['task']}", "", task["summary"], ""]
            out += [f"- **{s['subtask']}:** {s['summary']}" for s in task["subtasks"] if s["summary"]]
            if any(s["summary"] for s in task["subtasks"]):
                out.append("")
        out += ["## Time by task", "", "| Task | Time | Share of day |", "| --- | ---: | ---: |"]
        for task in data["tasks"]:
            out.append(f"| {task['task']} | {format_duration(task['seconds'])} | {task['share']:.0%} |")
            out += [f"| &nbsp;&nbsp;↳ {s['subtask']} | {format_duration(s['seconds'])} | {s['share']:.0%} |" for s in task["subtasks"]]
        out += [
            "",
            f"_Time is each conversation's length plus up to {data['gap_attribution_min']} minutes of the quiet time after it. "
            "Shares are of that tracked time, not of the whole day._",
            "",
        ]
    else:
        out += ["_No language model is connected, so tasks were not identified. Choose one in Settings, then regenerate this summary._", ""]
    out += ["## Notes to self and ideas", ""]
    out += [f"- {n['time']} ({n['kind']}): {n['text']}" for n in data["notes"]] or ["None recorded."]
    out += ["", "## Reminders set", ""]
    out += [f"- {r['text']} (due {parse_time(r['due']):%a %H:%M})" for r in data["reminders"]] or ["None."]
    return "\n".join(out) + "\n"
