"""Ollama backend: a thin HTTP client against the local container's own
API. Ollama's native /api/chat shape is already close to OpenAI's, so this
adapter is a field-mapping layer, not new protocol work.

NOT verified against a live Ollama container in this session (none was
reachable) -- the `format` field and `message.content` response shape match
Ollama's documented /api/chat contract, but re-check against the actual
running container (`curl http://localhost:11434/api/chat ...`) before
trusting this, the same way the Claude adapter's output_config shape was
verified directly against the installed SDK.
"""

import httpx

from llm_gateway.config import settings


class OllamaAdapter:
    async def complete(
        self, model: str, prompt: str, response_schema: dict | None
    ) -> str:
        model_name = model.removeprefix("ollama-")
        payload = {
            "model": model_name,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        }
        if response_schema is not None:
            payload["format"] = response_schema

        async with httpx.AsyncClient(
            base_url=settings.ollama_base_url, timeout=settings.ollama_timeout
        ) as client:
            resp = await client.post("/api/chat", json=payload)
            resp.raise_for_status()
            return resp.json()["message"]["content"]
