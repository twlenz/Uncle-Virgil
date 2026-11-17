import pytest
from fastapi.testclient import TestClient

from uncle_virgil import hotkey
from uncle_virgil.server import create_app


@pytest.mark.parametrize(
    "combo, expected",
    [
        ("Ctrl+Alt+D", (0x0002 | 0x0001, ord("D"))),
        ("ctrl + shift + f9", (0x0002 | 0x0004, 0x78)),
        ("Win+Space", (0x0008, 0x20)),
        ("Alt+1", (0x0001, ord("1"))),
    ],
)
def test_shortcuts_are_understood(combo, expected):
    assert hotkey.parse(combo) == expected


@pytest.mark.parametrize("combo", ["D", "Ctrl+", "Hyper+D", "Ctrl+Alt+Banana", "Ctrl+F25"])
def test_unusable_shortcuts_are_refused_with_a_reason(combo):
    with pytest.raises(ValueError):
        hotkey.parse(combo)


def test_an_unusable_shortcut_is_not_saved(app):
    c = TestClient(create_app(app, "t"), base_url="http://127.0.0.1", headers={"Authorization": "Bearer t"})
    r = c.put("/api/settings", json={"recording": {"hotkey": "Ctrl+Banana"}})
    assert r.status_code == 400 and "Unknown key" in r.json()["detail"]
    assert app.settings.recording.hotkey == "Ctrl+Alt+D"
    assert c.put("/api/settings", json={"recording": {"hotkey": "ctrl+shift+f9"}}).json()["settings"]["recording"]["hotkey"] == "Ctrl+Shift+F9"
    assert c.put("/api/settings", json={"recording": {"hotkey": ""}}).json()["settings"]["recording"]["hotkey"] == ""  # turned off


def test_the_shortcut_starts_and_stops_dictation(app, monkeypatch):
    calls = []
    monkeypatch.setattr(app, "start_capture", lambda mode: (calls.append(mode), setattr(app.recorder, "mode", mode)))
    monkeypatch.setattr(app, "stop_capture", lambda: (calls.append("stop"), setattr(app.recorder, "mode", "idle")))
    app.toggle_dictation()
    app.toggle_dictation()
    assert calls == ["dictation", "stop"]


def test_if_dictation_cannot_start_the_shortcut_says_why(app, monkeypatch):
    def fail(mode):
        raise RuntimeError("Could not open the microphone: none found")

    monkeypatch.setattr(app, "start_capture", fail)
    app.toggle_dictation()
    assert app.sent == [("Could not start dictation", "Could not open the microphone: none found")]
