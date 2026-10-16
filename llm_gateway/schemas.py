"""OpenAI-compatible request/response shapes, trimmed to what ai/analysis.py
(and CV generation) actually sends: one user message, an optional JSON-schema
response format, no streaming, no multi-turn history.
"""

from typing import Optional

from pydantic import BaseModel


class ChatMessage(BaseModel):
    role: str
    content: str


class ResponseFormat(BaseModel):
    type: str = "json_schema"
    #: OpenAI's shape is {"name": ..., "schema": {...}}; a bare JSON schema is
    #: accepted too. Use `schema_dict` rather than reading this directly.
    json_schema: Optional[dict] = None

    @property
    def schema_dict(self) -> Optional[dict]:
        """The JSON schema itself, unwrapped from OpenAI's envelope."""
        wrapped = self.json_schema

        if wrapped and "schema" in wrapped and "type" not in wrapped:
            return wrapped["schema"]

        return wrapped


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    response_format: Optional[ResponseFormat] = None


class ChatCompletionChoice(BaseModel):
    message: ChatMessage


class ChatCompletionResponse(BaseModel):
    model: str
    choices: list[ChatCompletionChoice]
