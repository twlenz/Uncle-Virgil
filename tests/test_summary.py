import pytest

from uncle_virgil.summary import summarize_day

from .conftest import seg

D = "2026-11-16"


def day(app):
    """Three conversations: budget 09:00-09:10, hiring 09:30-09:35, budget again 11:00-11:05."""
    app.storage.append_segments(
        D,
        [
            seg(D, "09:00:00", "Let's review the budget and the travel costs.", seconds=300),
            seg(D, "09:05:00", "Travel is too high.", speaker="Speaker 1", speaker_id="unk-1", seconds=300),
            seg(D, "09:30:00", "Now the hiring plan for next quarter.", seconds=300),
            seg(D, "11:00:00", "Back to the budget forecast. Note to self, send the forecast to Ana at 3pm.", seconds=300),
        ],
    )


def test_time_shares_come_from_timestamps_not_the_model(app, fake_llm):
    day(app)
    app.settings.summary.gap_attribution_min = 20
    s = summarize_day(D, app.storage, fake_llm, app.settings, "Tom")
    tasks = {t["task"]: t for t in s["tasks"]}
    # budget: 10 min talk + 20 min of the gap that followed, then 5 min (last conversation gets no gap)
    assert tasks["Budget report"]["seconds"] == 10 * 60 + 20 * 60 + 5 * 60
    # hiring: 5 min talk + 20 of the 85 quiet minutes after it
    assert tasks["Hiring"]["seconds"] == 5 * 60 + 20 * 60
    assert sum(t["share"] for t in s["tasks"]) == pytest.approx(1.0)
    assert [t["task"] for t in s["tasks"]] == ["Budget report", "Hiring"]  # largest first
    assert [(x["subtask"], x["seconds"]) for x in tasks["Budget report"]["subtasks"]] == [("Travel costs", 1800), ("Forecast", 300)]
    assert tasks["Hiring"]["subtasks"] == []
    assert s["overview"] == "A day of planning." and tasks["Hiring"]["summary"] == "Worked on Hiring."
    assert s["totals"]["speech_seconds"] == 1200 and s["totals"]["conversations"] == 3


def test_gap_attribution_can_be_switched_off(app, fake_llm):
    day(app)
    app.settings.summary.gap_attribution_min = 0
    s = summarize_day(D, app.storage, fake_llm, app.settings, "Tom")
    assert {t["task"]: t["seconds"] for t in s["tasks"]} == {"Budget report": 900, "Hiring": 300}


def test_notes_merge_rule_and_model_findings_without_duplicates(app, fake_llm):
    day(app)
    s = summarize_day(D, app.storage, fake_llm, app.settings, "Tom")
    assert s["notes"] == [
        {"time": "09:00", "kind": "todo", "text": "Email the new forecast to finance"},
        {"time": "11:00", "kind": "note", "text": "Send the forecast to Ana at 3pm"},
    ]


def test_long_days_are_sent_in_several_requests_and_nothing_is_dropped(app, fake_llm):
    day(app)
    app.settings.llm.max_chars_per_request = 120  # force one conversation per request, and split the first
    s = summarize_day(D, app.storage, fake_llm, app.settings, "Tom")
    label_prompts = [p for sys, p in fake_llm.calls if "For each conversation" in sys]
    assert len(label_prompts) >= 3
    sent = "\n".join(label_prompts)
    for text in ("travel costs", "Travel is too high", "hiring plan", "budget forecast"):
        assert text in sent
    assert "- Budget report" in label_prompts[-1]  # earlier task names are offered for reuse
    assert sum(len(t["conversations"]) for t in s["tasks"]) == s["totals"]["conversations"]


def test_without_a_model_the_summary_still_lists_notes_and_reminders(app):
    day(app)
    app.reminders.handle_segments(app.storage.load_segments(D))
    s = app.summarize(D)
    assert s["tasks"] == [] and s["llm"] is None
    assert [n["text"] for n in s["notes"]] == ["Send the forecast to Ana at 3pm"]
    assert [r["text"] for r in s["reminders"]] == ["Send the forecast to Ana at 3pm"]
    md = (app.home / "days" / D / "summary.md").read_text()
    assert "No language model is connected" in md and "- 11:00 (note): Send the forecast to Ana at 3pm" in md


def test_markdown_has_work_time_table_and_notes(app, fake_llm, monkeypatch):
    day(app)
    monkeypatch.setattr(app, "llm", lambda: fake_llm)
    app.summarize(D)
    md = (app.home / "days" / D / "summary.md").read_text()
    assert "## Work performed" in md and "| Budget report | 35m | 58% |" in md and "↳ Travel costs | 30m | 50% |" in md


def test_empty_day_is_an_error(app):
    with pytest.raises(ValueError, match="Nothing was recorded"):
        app.summarize("2026-01-01")


def test_daily_summary_runs_once_after_the_set_time_and_catches_up_yesterday(app):
    from datetime import datetime

    day(app)
    app.settings.summary.auto_time = "21:00"
    assert app.check_daily_summary(datetime(2026, 11, 16, 20, 59)) == []
    assert app.check_daily_summary(datetime(2026, 11, 16, 21, 0)) == [D]
    assert app.check_daily_summary(datetime(2026, 11, 16, 21, 1)) == []
    fresh = type(app)(app.home)  # app restarted the next morning with no summary on disk yet
    (app.home / "days" / D / "summary.json").unlink(missing_ok=True)
    assert fresh.check_daily_summary(datetime(2026, 11, 17, 8, 0)) == [D]
