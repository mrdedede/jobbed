"""Request/response shapes for the REST API.

Input validation lives here, at the trust boundary: terms and emails are
stripped and lowercased (db.users expects callers to normalize), sizes are
capped, and the model a client may ask for is a closed set.
"""

import os
from datetime import datetime
from typing import Annotated, List, Literal, Optional
from uuid import UUID

from pydantic import (AfterValidator, BaseModel, Field, StringConstraints,
                      field_validator)

Email = Annotated[str, StringConstraints(
    strip_whitespace=True, to_lower=True, max_length=254,
    pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]


# bcrypt's limit is 72 *bytes*; max_length counts characters, so the
# validator below rejects multi-byte passwords that would exceed it.
def _fits_bcrypt(value: str) -> str:
    if len(value.encode()) > 72:
        raise ValueError("password is longer than 72 bytes")

    return value


Password = Annotated[str, Field(min_length=8, max_length=72),
                     AfterValidator(_fits_bcrypt)]

Term = Annotated[str, StringConstraints(
    strip_whitespace=True, to_lower=True, min_length=1, max_length=100)]

CvMarkdown = Annotated[str, StringConstraints(
    strip_whitespace=True, min_length=1, max_length=100_000)]


# INFRA

class HealthOut(BaseModel):
    status: str = "ok"


# AUTH

class Credentials(BaseModel):
    email: Email
    password: Password


class TokenOut(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"


class UserOut(BaseModel):
    id: UUID
    email: str


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=72)
    new_password: Password


# KEYWORDS / BLACKLIST (same shape)

class TermIn(BaseModel):
    term: Term


class TermOut(BaseModel):
    id: UUID
    term: str


# CVS

class CvCreate(BaseModel):
    content_md: CvMarkdown
    make_active: bool = True


class CvUpdate(BaseModel):
    content_md: CvMarkdown


class CvOut(BaseModel):
    id: UUID
    content_md: str
    is_active: bool


# ANALYSIS

def allowed_models() -> List[str]:
    """Models a client may ask for: the server's list, never the client's.

    Defaults to the two Claude tiers. Set ANALYSIS_MODELS to e.g.
    "haiku,sonnet,ollama-qwen2.5:0.5b" to let a dev stack grade locally.
    """
    raw = os.environ.get("ANALYSIS_MODELS", "haiku,sonnet")

    return [m.strip() for m in raw.split(",") if m.strip()]


class AnalyzeRequest(BaseModel):
    limit: int = Field(20, ge=1, le=50)
    window_days: int = Field(1, ge=1, le=30)
    model: str = "haiku"

    @field_validator("model")
    @classmethod
    def _model_is_allowed(cls, value: str) -> str:
        if value not in allowed_models():
            raise ValueError(f"model must be one of {allowed_models()}")

        return value


class AnalysisOut(BaseModel):
    """One graded posting. No `description`: it is large and the url has it."""
    job_id: UUID
    adequation_grade: Optional[int] = Field(None, ge=0, le=100)
    company: str
    title: str
    place: Optional[str] = None
    url: str
    ai_model: Optional[str] = None
    depth_analysis: Optional[str] = None
    analysed_at: datetime


class AnalyzeOut(BaseModel):
    analysed: int
    failed: int
    due: int
    filtered_out: int
    remaining: int
    results: List[AnalysisOut]


class DueOut(BaseModel):
    """Postings an analysis run could grade right now, filter applied."""
    due: int


# CV GENERATION

class GenCvRequest(BaseModel):
    job_id: UUID


class GenCvOut(BaseModel):
    cv_id: UUID
    title: str
    company: str
    adequation_grade: int
    locale: str
    cv: dict
    job_id: UUID


class GenCvStarted(BaseModel):
    generation_id: UUID


class GenCvStatus(BaseModel):
    status: Literal["running", "done", "failed"]
    cv: Optional[GenCvOut] = None
    error: Optional[dict] = None
