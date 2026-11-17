"""Any server that speaks the OpenAI chat-completions protocol.

Covers OpenAI itself (base URL https://api.openai.com/v1) and local model
servers: Ollama (http://localhost:11434/v1), LM Studio
(http://localhost:1234/v1), llama.cpp, and others.
"""

from __future__ import annotations

from urllib.parse import urlparse

import httpx

from . import LLMError

# Local models on a CPU can take minutes for a long prompt.
_TIMEOUT = httpx.Timeout(900.0, connect=10.0)


class OpenAICompatibleProvider:
    name = "openai_compatible"

    def __init__(self, base_url: str, model: str, api_key: str = ""):
        if not model:
            raise LLMError("Choose a model name in Settings (for example the name shown by `ollama list`).")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.off_device = urlparse(self.base_url).hostname not in ("localhost", "127.0.0.1", "::1")

    def complete(self, system: str, prompt: str, schema: dict | None = None) -> str:
        body: dict = {"model": self.model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]}
        # Servers differ in how much structured output they support; try the strictest form first.
        formats = [None]
        if schema:
            formats = [{"type": "json_schema", "json_schema": {"name": "result", "schema": schema, "strict": True}}, {"type": "json_object"}, None]
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        for i, response_format in enumerate(formats):
            payload = body | ({"response_format": response_format} if response_format else {})
            try:
                r = httpx.post(f"{self.base_url}/chat/completions", json=payload, headers=headers, timeout=_TIMEOUT)
            except httpx.ConnectError as e:
                raise LLMError(f"Could not connect to {self.base_url}. Is the model server running?") from e
            except httpx.TimeoutException as e:
                raise LLMError(f"{self.base_url} took too long to answer.") from e
            if r.status_code == 400 and i + 1 < len(formats):
                continue  # this server rejects that response_format; try a looser one
            if r.status_code == 401:
                raise LLMError("The model server rejected the API key. Check it in Settings.")
            if r.status_code >= 400:
                raise LLMError(f"Model server error {r.status_code}: {r.text[:300]}")
            try:
                choice = r.json()["choices"][0]
                content = choice["message"]["content"]
            except (ValueError, KeyError, IndexError, TypeError) as e:
                raise LLMError(f"Unexpected reply from {self.base_url}: {r.text[:300]}") from e
            if choice.get("finish_reason") == "length":
                raise LLMError("The model's reply was cut off. Use a model with a larger context window, or lower 'max characters per request' in Settings.")
            return content or ""
        raise LLMError("The model server rejected the request.")
