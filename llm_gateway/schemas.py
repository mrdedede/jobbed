"""OpenAI-compatible request/response shapes, trimmed to what ai/analysis.py
(and, later, CV generation) actually sends: one user message, an optional
JSON-schema response format, no streaming, no multi-turn history.
"""

from pydantic import BaseModel


class ChatMessage(BaseModel):
    role: str
    content: str


class ResponseFormat(BaseModel):
    type: str = "json_schema"
    json_schema: dict | None = None


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    response_format: ResponseFormat | None = None


class ChatCompletionChoice(BaseModel):
    message: ChatMessage


class ChatCompletionResponse(BaseModel):
    model: str
    choices: list[ChatCompletionChoice]
