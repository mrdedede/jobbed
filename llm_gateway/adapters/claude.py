"""Claude backend: the anthropic SDK -- no subprocess,
no CLI binary in the image, no dependency on an interactively-authenticated
session.
"""

import os

from anthropic import AsyncAnthropic

from llm_gateway.adapters.base import BackendUnavailable
from llm_gateway.config import settings

#: Short names callers send. Each can be overridden without a redeploy of
#: callers: CLAUDE_MODEL_HAIKU / _SONNET / _OPUS.
_ALIASES = {
    "haiku": os.environ.get("CLAUDE_MODEL_HAIKU", "claude-haiku-5-5"),
    "sonnet": os.environ.get("CLAUDE_MODEL_SONNET", "claude-sonnet-5-5"),
    "opus": os.environ.get("CLAUDE_MODEL_OPUS", "claude-opus-5-5"),
}

_client = AsyncAnthropic(api_key=settings.anthropic_api_key)


class ClaudeAdapter:
    async def complete(
        self, model: str, prompt: str, response_schema: dict | None
    ) -> str:
        if not settings.anthropic_api_key:
            raise BackendUnavailable("ANTHROPIC_API_KEY is not set")

        model_id = _ALIASES.get(model, model)
        kwargs = {}
        if response_schema is not None:
            # Verified against the installed anthropic SDK (0.121.0).
            kwargs["output_config"] = {
                "format": {"type": "json_schema", "schema": response_schema},
            }

        response = await _client.messages.create(
            model=model_id,
            # Generous: on current models thinking shares this budget.
            max_tokens=16000,
            messages=[{"role": "user", "content": prompt}],
            **kwargs,
        )
        # A cut-off or declined answer is not an answer, whatever text it has.
        if response.stop_reason in ("max_tokens", "refusal"):
            raise ValueError(f"model stopped early: {response.stop_reason}")

        text = "".join(
            block.text for block in response.content if block.type == "text")

        if not text:
            raise ValueError(
                f"model returned no text (stop_reason={response.stop_reason})")

        return text
