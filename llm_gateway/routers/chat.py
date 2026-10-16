"""POST /v1/chat/completions -- dispatches to Claude or Ollama by the
request's model field, so callers never need to know which backend ran.
"""

import anthropic
import httpx
from fastapi import APIRouter, HTTPException

from llm_gateway.adapters.base import BackendUnavailable
from llm_gateway.adapters.claude import ClaudeAdapter
from llm_gateway.adapters.ollama import OllamaAdapter
from llm_gateway.metrics import llm_requests_total
from llm_gateway.schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
)

router = APIRouter()

_claude = ClaudeAdapter()
_ollama = OllamaAdapter()


def _pick_adapter(model: str):
    return _ollama if model.startswith("ollama-") else _claude


@router.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@router.post("/v1/chat/completions", response_model=ChatCompletionResponse)
async def chat_completions(request: ChatCompletionRequest) -> ChatCompletionResponse:
    adapter = _pick_adapter(request.model)
    llm_requests_total.labels(
        adapter="ollama" if adapter is _ollama else "claude"
    ).inc()

    if not request.messages:
        raise HTTPException(status_code=422, detail="messages is empty")

    prompt = request.messages[-1].content
    schema = (
        request.response_format.schema_dict if request.response_format else None
    )

    try:
        text = await adapter.complete(request.model, prompt, schema)
    except BackendUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (anthropic.APIError, httpx.HTTPError, ValueError, KeyError) as exc:
        # The gateway worked; its upstream did not. 502 tells callers to
        # retry or fall back, where a bare 500 would look like a gateway bug.
        raise HTTPException(
            status_code=502, detail=f"{type(exc).__name__}: {exc}") from exc

    return ChatCompletionResponse(
        model=request.model,
        choices=[{"message": {"role": "assistant", "content": text}}],
    )
