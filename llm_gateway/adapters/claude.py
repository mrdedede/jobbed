"""Claude backend: the anthropic SDK, not the claude CLI -- no subprocess,
no CLI binary in the image, no dependency on an interactively-authenticated
session. See the plan's context section for why this replaced
ai/call_model.py's subprocess approach.
"""

from anthropic import AsyncAnthropic

from llm_gateway.config import settings

_ALIASES = {
    "haiku": "claude-haiku-4-5",
    "sonnet": "claude-sonnet-5",
    "opus": "claude-opus-4-1",
}

_client = AsyncAnthropic(api_key=settings.anthropic_api_key)


class ClaudeAdapter:
    async def complete(
        self, model: str, prompt: str, response_schema: dict | None
    ) -> str:
        model_id = _ALIASES.get(model, model)
        kwargs = {}
        if response_schema is not None:
            # Verified against the installed anthropic SDK (0.121.0):
            kwargs["output_config"] = {
                "format": {"type": "json_schema", "schema": response_schema},
            }

        response = await _client.messages.create(
            model=model_id,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
            **kwargs,
        )
        return response.content[0].text
