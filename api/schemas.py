"""Request/response shapes for the REST API.

Input validation lives here, at the trust boundary: terms and emails are
stripped and lowercased (db.users expects callers to normalize), sizes are
capped, and the model a client may ask for is a closed set.
"""

from datetime import datetime
from typing import Annotated, List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field, StringConstraints

Email = Annotated[str, StringConstraints(
    strip_whitespace=True, to_lower=True, max_length=254,
    pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]

# bcrypt silently truncates at 72 *bytes*; max_length counts characters, so
# the auth layer still has to reject len(password.encode()) > 72.
Password = Annotated[str, Field(min_length=8, max_length=72)]

Term = Annotated[str, StringConstraints(
    strip_whitespace=True, to_lower=True, min_length=1, max_length=100)]

CvMarkdown = Annotated[str, Field(min_length=1, max_length=100_000)]


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

class AnalyzeRequest(BaseModel):
    limit: int = Field(20, ge=1, le=50)
    window_days: int = Field(1, ge=1, le=30)
    # Closed set: a client never picks "opus" or an "ollama-*" backend.
    model: Literal["haiku", "sonnet"] = "haiku"


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
    results: List[AnalysisOut]
