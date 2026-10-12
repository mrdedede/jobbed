"""The one method both backends implement, so routers/chat.py can call
either without knowing which one it got.
"""

from typing import Protocol


class ModelAdapter(Protocol):
    async def complete(
        self, model: str, prompt: str, response_schema: dict | None
    ) -> str:
        """Return the model's raw text response.

        Args:
            model: The model name/alias as the caller sent it (e.g. "haiku",
                "ollama-llama3.2") -- each adapter resolves this to whatever
                its own backend expects.
            prompt: The single user-turn prompt text.
            response_schema: A JSON schema the response must match, or None
                for free-form text.
        """
        ...
