import json

import httpx
import pytest

from uncle_virgil import llm
from uncle_virgil.config import Settings
from uncle_virgil.llm import LLMError, complete_json
from uncle_virgil.llm.openai_compatible import OpenAICompatibleProvider

SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"], "additionalProperties": False}


class Scripted:
    name, off_device = "scripted", False

    def __init__(self, *replies):
        self.replies = list(replies)

    def complete(self, system, prompt, schema=None):
        return self.replies.pop(0)


def test_complete_json_accepts_fenced_json_and_retries_once():
    assert complete_json(Scripted('```json\n{"answer": "yes"}\n```'), "s", "p", SCHEMA) == {"answer": "yes"}
    assert complete_json(Scripted("I cannot", '{"answer": "ok"}'), "s", "p", SCHEMA) == {"answer": "ok"}
    with pytest.raises(LLMError, match="not valid JSON"):
        complete_json(Scripted("nope", "still nope"), "s", "p", SCHEMA)
    with pytest.raises(LLMError, match="missing required"):
        complete_json(Scripted('{"other": 1}', '{"other": 2}'), "s", "p", SCHEMA)


def test_provider_selection():
    s = Settings()
    assert llm.create(s, lambda name: "") is None
    s.llm.provider, s.llm.model = "openai_compatible", "llama3.2"
    p = llm.create(s, lambda name: "")
    assert p.off_device is False and p.base_url == "http://localhost:11434/v1"
    s.llm.base_url = "https://api.openai.com/v1"
    assert llm.create(s, lambda name: "k").off_device is True
    s.llm.provider = "mystery"
    with pytest.raises(LLMError):
        llm.create(s, lambda name: "")
    llm.register("mystery", lambda settings, get_secret: Scripted("hi"))  # a plugin registering its own provider
    assert llm.create(s, lambda name: "").complete("s", "p") == "hi"


def test_openai_compatible_falls_back_when_server_rejects_structured_output(monkeypatch):
    seen = []

    def fake_post(url, json=None, headers=None, timeout=None):
        seen.append((url, json.get("response_format", {}).get("type"), headers))
        if json.get("response_format", {}).get("type") == "json_schema":
            return httpx.Response(400, text="response_format json_schema not supported")
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"answer": "42"}'}, "finish_reason": "stop"}]})

    monkeypatch.setattr(httpx, "post", fake_post)
    p = OpenAICompatibleProvider("http://localhost:11434/v1/", "llama3.2", "key")
    assert json.loads(p.complete("sys", "question", SCHEMA)) == {"answer": "42"}
    assert [(u, f) for u, f, _ in seen] == [("http://localhost:11434/v1/chat/completions", "json_schema"), ("http://localhost:11434/v1/chat/completions", "json_object")]
    assert seen[0][2] == {"Authorization": "Bearer key"}


def test_openai_compatible_reports_problems_in_plain_words(monkeypatch):
    p = OpenAICompatibleProvider("http://localhost:11434/v1", "llama3.2")

    def refuse(*a, **k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "post", refuse)
    with pytest.raises(LLMError, match="Is the model server running"):
        p.complete("s", "p")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(200, json={"choices": [{"message": {"content": "abc"}, "finish_reason": "length"}]}))
    with pytest.raises(LLMError, match="cut off"):
        p.complete("s", "p")
    with pytest.raises(LLMError, match="Choose a model name"):
        OpenAICompatibleProvider("http://localhost:11434/v1", "")


def test_anthropic_provider_builds_the_expected_request(monkeypatch):
    import anthropic

    from uncle_virgil.llm.anthropic_provider import AnthropicProvider

    captured = {}

    class Stream:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get_final_message(self):
            block = type("B", (), {"type": "text", "text": '{"answer": "hi"}'})()
            thinking = type("B", (), {"type": "thinking", "thinking": ""})()
            return type("M", (), {"stop_reason": captured.get("stop", "end_turn"), "content": [thinking, block]})()

    p = AnthropicProvider("", "sk-test")
    monkeypatch.setattr(p.client.beta.messages, "stream", lambda **kw: captured.update(kw) or Stream())
    assert p.complete("sys", "question", SCHEMA) == '{"answer": "hi"}'
    assert captured["model"] == "claude-opus-5-5" and captured["system"] == "sys"
    assert captured["output_config"] == {"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}}
    assert captured["fallbacks"] == "default" and "temperature" not in captured and "thinking" not in captured
    captured["stop"] = "refusal"
    with pytest.raises(LLMError, match="declined"):
        p.complete("sys", "question")
    assert isinstance(p.client, anthropic.Anthropic)
