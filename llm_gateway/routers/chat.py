"""POST /v1/chat/completions -- dispatches to Claude or Ollama by the
request's model field, so callers never need to know which backend ran.
"""

from fastapi import APIRouter

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


@router.post("/v1/chat/completions", response_model=ChatCompletionResponse)
async def chat_completions(request: ChatCompletionRequest) -> ChatCompletionResponse:
    adapter = _pick_adapter(request.model)
    llm_requests_total.labels(
        adapter="ollama" if adapter is _ollama else "claude"
    ).inc()

    prompt = request.messages[-1].content
    schema = (
        request.response_format.json_schema if request.response_format else None
    )

    text = await adapter.complete(request.model, prompt, schema)

    return ChatCompletionResponse(
        model=request.model,
        choices=[{"message": {"role": "assistant", "content": text}}],
    )
