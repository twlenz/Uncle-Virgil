"""Claude, through the official Anthropic SDK."""

from __future__ import annotations

import anthropic

from . import LLMError

DEFAULT_MODEL = "claude-opus-5-5"
# Models where the API can re-run a declined request on another model.
_FALLBACK_MODELS = ("claude-fable-5-2", "claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5")


class AnthropicProvider:
    name = "anthropic"
    off_device = True

    def __init__(self, model: str, api_key: str):
        self.model = model or DEFAULT_MODEL
        # With no key given, the SDK falls back to ANTHROPIC_API_KEY or an `ant auth login` profile.
        self.client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()

    def complete(self, system: str, prompt: str, schema: dict | None = None) -> str:
        output_config: dict = {"effort": "medium"}
        if schema:
            output_config["format"] = {"type": "json_schema", "schema": schema}
        extra = {}
        if self.model in _FALLBACK_MODELS:
            extra = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
        try:
            # Streamed so a long day's transcript cannot hit an HTTP timeout.
            with self.client.beta.messages.stream(
                model=self.model,
                max_tokens=64000,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                output_config=output_config,
                **extra,
            ) as stream:
                message = stream.get_final_message()
        except anthropic.AuthenticationError as e:
            raise LLMError("Claude rejected the API key. Check it in Settings.") from e
        except anthropic.RateLimitError as e:
            raise LLMError("Claude rate limit reached. Try again in a few minutes.") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"Claude API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError("Could not reach the Claude API. Check the internet connection.") from e
        except anthropic.AnthropicError as e:  # e.g. no credentials configured
            raise LLMError(str(e)) from e

        if message.stop_reason == "refusal":
            raise LLMError("Claude declined to process this text.")
        if message.stop_reason == "max_tokens":
            raise LLMError("Claude's reply was cut off before it finished.")
        return "".join(block.text for block in message.content if block.type == "text")
