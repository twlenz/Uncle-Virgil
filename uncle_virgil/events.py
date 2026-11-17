"""A small event bus, the attachment point for plugins and (later) agents.

Events emitted by the app:
    block_transcribed   date, block, segments
    reminder_created    reminder
    reminder_fired      reminder
    day_summarized      date, summary

A plugin is any installed Python package that declares an entry point in the
"uncle_virgil.plugins" group pointing at a function `setup(app)`. Inside it,
subscribe to events, or register an LLM provider or notification channel:

    def setup(app):
        app.events.subscribe("day_summarized", my_agent.on_summary)
        uncle_virgil.llm.register("my-llm", make_provider)
        app.notifier.channels["sms"] = send_sms
"""

from __future__ import annotations

import logging
from collections import defaultdict
from importlib.metadata import entry_points
from typing import Callable

log = logging.getLogger(__name__)


class EventBus:
    def __init__(self):
        self._subscribers: dict[str, list[Callable]] = defaultdict(list)

    def subscribe(self, event: str, handler: Callable) -> None:
        self._subscribers[event].append(handler)

    def emit(self, event: str, **payload) -> None:
        for handler in list(self._subscribers[event]):
            try:
                handler(**payload)
            except Exception:  # a broken plugin must not stop recording
                log.exception("handler for %s failed", event)


def load_plugins(app) -> list[str]:
    loaded = []
    for ep in entry_points(group="uncle_virgil.plugins"):
        try:
            ep.load()(app)
            loaded.append(ep.name)
        except Exception:
            log.exception("plugin %s failed to load", ep.name)
    return loaded
