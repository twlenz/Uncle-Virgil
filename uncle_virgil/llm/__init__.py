"""Pluggable language-model providers.

A provider is any object with:

    name: str
    off_device: bool          # True if text is sent to another company's servers
    def complete(self, system: str, prompt: str, schema: dict | None = None) -> str

`schema`, when given, is a JSON Schema the reply must follow; providers that
can enforce it should, and the caller validates either way.

Built in: "anthropic" (Claude) and "openai_compatible" (ChatGPT, and local
servers such as Ollama, LM Studio and llama.cpp). Add your own with
`register("name", factory)`, where factory(settings, get_secret) returns a
provider.
"""

from __future__ import annotations

import json
import re
from typing import Callable, Protocol

from ..config import Settings


class LLMError(Exception):
    """A provider could not produce a usable answer. The message is shown to the user."""


class LLMProvider(Protocol):
    name: str
    off_device: bool

    def complete(self, system: str, prompt: str, schema: dict | None = None) -> str: ...


Factory = Callable[[Settings, Callable[[str], str]], LLMProvider]
_registry: dict[str, Factory] = {}


def register(name: str, factory: Factory) -> None:
    _registry[name] = factory


def provider_names() -> list[str]:
    return ["none", *_registry]


def create(settings: Settings, get_secret: Callable[[str], str]) -> LLMProvider | None:
    """The provider chosen in settings, or None when no LLM is configured."""
    name = settings.llm.provider
    if name in ("", "none"):
        return None
    if name not in _registry:
        raise LLMError(f"Unknown LLM provider {name!r}")
    return _registry[name](settings, get_secret)


def _parse_json(text: str) -> dict:
    """Parse a JSON object, tolerating code fences or chatter around it (common with small local models)."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        if m := re.search(r"\{.*\}", text, re.DOTALL):
            return json.loads(m.group(0))
        raise


def complete_json(provider: LLMProvider, system: str, prompt: str, schema: dict) -> dict:
    """Ask for a JSON object matching `schema`. Retries once if the reply is not valid JSON."""
    last_error = None
    for _ in range(2):
        text = provider.complete(system, prompt, schema)
        try:
            data = _parse_json(text)
            if isinstance(data, dict) and all(key in data for key in schema.get("required", [])):
                return data
            last_error = "reply was missing required fields"
        except json.JSONDecodeError as e:
            last_error = f"reply was not valid JSON ({e})"
    raise LLMError(f"{provider.name}: {last_error}")


def _anthropic(settings: Settings, get_secret):
    from .anthropic_provider import AnthropicProvider

    return AnthropicProvider(settings.llm.model, get_secret("anthropic_api_key"))


def _openai_compatible(settings: Settings, get_secret):
    from .openai_compatible import OpenAICompatibleProvider

    return OpenAICompatibleProvider(settings.llm.base_url, settings.llm.model, get_secret("openai_api_key"))


register("anthropic", _anthropic)
register("openai_compatible", _openai_compatible)
