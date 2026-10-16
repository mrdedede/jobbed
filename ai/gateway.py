"""Async client for the LLM gateway (llm_gateway/): one chat call.

Every model call in ai/* goes through here, so which backend answers (Claude,
Ollama) is the gateway's business and a caller picks one by `model` alone.
Async so a handler or a batch can keep many calls in flight on one loop.
"""

import os
from typing import Optional

import httpx

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://localhost:8000")

#: Names the gateway resolves. "ollama-<name>" selects a local model.
HAIKU_MODEL = "haiku"
SONNET_MODEL = "sonnet"
OPUS_MODEL = "opus"

#: Generous: a long posting on a local model is slow, and the gateway has its
#: own upstream timeouts.
TIMEOUT = 180.0


class GatewayError(RuntimeError):
    """The gateway (or its upstream model) did not return an answer."""


async def chat(model: str, prompt: str, schema: Optional[dict] = None,
               client: Optional[httpx.AsyncClient] = None) -> str:
    """Send one user prompt and return the model's raw text.

    Args:
        model: A name the gateway understands ("haiku", "ollama-llama3.2").
        prompt: The single user-turn prompt.
        schema: A JSON schema the answer must match, or None for free text.
        client: A shared AsyncClient to reuse connections across a batch.
            When omitted, a short-lived one is used.

    Returns:
        The assistant message content.

    Raises:
        GatewayError: On a transport failure, a non-200 answer, or a body
            that is not a chat completion.
    """
    body = {"model": model,
            "messages": [{"role": "user", "content": prompt}]}

    if schema is not None:
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "result", "schema": schema},
        }

    owned = client is None
    client = client or httpx.AsyncClient(timeout=TIMEOUT)

    try:
        response = await client.post(
            f"{GATEWAY_URL}/v1/chat/completions", json=body)
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]
    except httpx.HTTPStatusError as exc:
        raise GatewayError(
            f"gateway answered {exc.response.status_code}: "
            f"{exc.response.text[:300]}") from exc
    except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
        raise GatewayError(f"{type(exc).__name__}: {exc}") from exc
    finally:
        if owned:
            await client.aclose()
