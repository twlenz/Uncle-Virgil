from datetime import datetime, timedelta

import pytest

from uncle_virgil.reminders import find_notes, parse_due

from .conftest import seg

NOW = datetime(2026, 11, 16, 14, 0).astimezone()  # a Monday, 2 pm


def at(day, hour, minute=0):
    return NOW.replace(day=day, hour=hour, minute=minute)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("meeting with Jack at 5pm", at(16, 17)),
        ("meeting with Jack at five P M", at(16, 17)),
        ("call mom at 5", at(16, 17)),  # bare hour: next daytime occurrence
        ("standup at 9", at(17, 9)),
        ("dentist tomorrow at 9", at(17, 9)),
        ("lunch at noon tomorrow", at(17, 12)),
        ("send the report by 17:00", at(16, 17)),
        ("pick up the kids at half past four", at(16, 16, 30)),
        ("meeting at quarter to five", at(16, 16, 45)),
        ("call Bob Friday at 3:30", at(20, 15, 30)),
        ("submit timesheet monday at 10", at(23, 10)),  # today is Monday and 10 has passed
        ("check the oven in 20 minutes", NOW + timedelta(minutes=20)),
        ("move the car in half an hour", NOW + timedelta(minutes=30)),
        ("pick up milk tonight", at(16, 20)),
        ("gym tomorrow morning", at(17, 9)),
        ("cut travel by 10 percent", None),
        ("we could automate the report", None),
    ],
)
def test_parse_due(text, expected):
    assert parse_due(text, NOW) == expected


def test_find_notes_tolerates_misheard_trigger():
    notes = find_notes("Thanks, note to South, meeting with Jack at 5pm. Out to self idea. We could automate the report.")
    assert notes == [
        {"kind": "note", "text": "Meeting with Jack at 5pm"},
        {"kind": "idea", "text": "We could automate the report"},
    ]


def test_find_notes_ignores_ordinary_speech():
    assert find_notes("We decided not to sell the building, and I set a reminder for the team.") == []


def test_spoken_note_becomes_reminder_and_fires_ahead_of_time(app):
    app.speakers.enroll("Tom", [1.0, 0.0], is_user=True)
    tom = app.speakers.speakers[0]["id"]
    today = NOW.date().isoformat()
    mine = seg(today, "14:00:00", "Note to self, meeting with Jack at 5pm.", speaker_id=tom)
    theirs = seg(today, "14:00:10", "Remind me to call you at 4pm.", speaker="Speaker 1", speaker_id="unk-1")
    created = app.reminders.handle_segments([mine, theirs], now=NOW)
    assert [r["text"] for r in created] == ["Meeting with Jack at 5pm"]  # other people's words are ignored
    assert app.reminders.handle_segments([mine], now=NOW) == []  # same note twice is not duplicated

    reminder = app.reminder_store.update(created[0]["id"], notify_at=at(16, 16, 30).isoformat())
    assert app.reminders.tick(at(16, 16, 29)) == []
    assert len(app.reminders.tick(at(16, 16, 30))) == 1
    assert app.sent[0][0] == "Reminder set: Meeting with Jack at 5pm"  # confirmation when it was heard
    assert app.sent[1:] == [("Reminder: Meeting with Jack at 5pm", "In 30 minutes, at 5:00 PM")]
    assert app.reminders.tick(at(16, 16, 31)) == [] and len(app.sent) == 2  # sent once only
    assert app.reminder_store.all()[0]["status"] == "notified"
    assert reminder["origin"] == {"date": today, "segment": mine["id"]}


def test_long_overdue_reminder_is_marked_missed_not_sent(app):
    r = app.reminder_store.add("Old thing", at(16, 9), 30, "manual", at(16, 8))
    assert app.reminders.tick(at(16, 18)) == []
    assert app.reminder_store.all()[0]["status"] == "missed" and not app.sent and r


def test_llm_pass_adds_reminders_the_rules_miss(app, monkeypatch):
    app.settings.reminders.use_llm = True

    class Provider:
        name, off_device = "fake", False

        def complete(self, system, prompt, schema=None):
            return '{"reminders": [{"text": "Dentist appointment", "due": "2026-11-17T09:00"}]}'

    monkeypatch.setattr(app, "llm", lambda: Provider())
    today = NOW.date().isoformat()
    created = app.reminders.handle_segments([seg(today, "14:00:00", "I have the dentist tomorrow at 9.")], now=NOW)
    assert [(r["text"], r["due"][:16], r["source"]) for r in created] == [("Dentist appointment", "2026-11-17T09:00", "llm")]
