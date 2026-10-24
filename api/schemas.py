"""Request/response shapes for the REST API.

Input validation lives here, at the trust boundary: terms and emails are
stripped and lowercased (db.users expects callers to normalize), sizes are
capped, and the model a client may ask for is a closed set.
"""

import os
from datetime import datetime
from typing import Annotated, List, Literal, Optional
from uuid import UUID

from pydantic import (AfterValidator, BaseModel, ConfigDict, Field,
                      StringConstraints, field_validator)

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


class TermsIn(BaseModel):
    """Several keywords and blacklist terms at once (accepted suggestions)."""
    keywords: List[Term] = Field(default_factory=list, max_length=50)
    blacklist: List[Term] = Field(default_factory=list, max_length=50)


class TermSuggestions(BaseModel):
    """Terms the model suggests for a CV, minus those the user already has."""
    keywords: List[str]
    blacklist: List[str]


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
    """The postings the user picked to grade, from /analyses/candidates."""
    job_ids: List[UUID] = Field(min_length=1, max_length=50)
    model: str = "haiku"

    @field_validator("job_ids")
    @classmethod
    def _dedupe(cls, value: List[UUID]) -> List[UUID]:
        return list(dict.fromkeys(value))

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
    skipped: int  # unknown, already graded, or past the daily cap
    results: List[AnalysisOut]


class CandidateOut(BaseModel):
    """An ungraded posting the user may pick to grade. No `description`."""
    job_id: UUID
    company: str
    title: str
    place: Optional[str] = None
    url: str
    scraped_at: datetime


class JobOut(CandidateOut):
    description: Optional[str] = None


# CV GENERATION

class GenCvRequest(BaseModel):
    job_id: UUID


class GenCvOut(BaseModel):
    cv_id: UUID
    title: str
    company: str
    adequation_grade: Optional[int] = None  # legacy imports may lack a grade
    locale: str
    cv: dict
    job_id: UUID
    depth_analysis: Optional[str] = None  # the posting's analysis, for the job drawer


# A user's edit of a generated CV: same shape as the `cv` object of
# ai.cv_generation.CV_SCHEMA (tests/test_api_cv_generation.py pins the fields).
CvText = Annotated[str, Field(max_length=5_000)]
CvList = Annotated[List[CvText], Field(max_length=50)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GenSkillGroup(_Strict):
    competence: CvText
    skills: CvList


class GenExperience(_Strict):
    role: CvText
    company: CvText
    location: CvText
    start_date: CvText
    end_date: CvText
    bullets: CvList


class GenEducation(_Strict):
    diploma: CvText
    institution: CvText
    location: CvText
    period: CvText
    degree: CvText
    details: CvText


class GenCvBody(_Strict):
    cv_introduction: CvText
    profile_text: CvText
    skills: List[GenSkillGroup] = Field(max_length=50)
    experiences: List[GenExperience] = Field(max_length=50)
    education: List[GenEducation] = Field(max_length=50)


class GenCvUpdate(_Strict):
    cv: GenCvBody


class GenCvStarted(BaseModel):
    generation_id: UUID


class GenCvStatus(BaseModel):
    status: Literal["running", "done", "failed"]
    cv: Optional[GenCvOut] = None
    error: Optional[dict] = None


# COVER LETTERS

LetterAnswer = Annotated[str, StringConstraints(strip_whitespace=True, max_length=2_000)]


class LetterAnswers(_Strict):
    """What the user tells the model; each may be left blank."""
    motivation: LetterAnswer = ""
    career_fit: LetterAnswer = ""
    offer_fit: LetterAnswer = ""


class LetterRequest(BaseModel):
    cv_id: UUID  # a generated CV, not an uploaded one
    answers: LetterAnswers = Field(default_factory=LetterAnswers)


class LetterOut(BaseModel):
    letter_id: UUID
    cv_id: UUID
    job_id: UUID
    title: str
    company: str
    adequation_grade: Optional[int] = None
    depth_analysis: Optional[str] = None
    locale: Optional[str] = None
    answers: dict
    letter: str
    created_at: datetime


class LetterUpdate(_Strict):
    letter: Annotated[str, StringConstraints(
        strip_whitespace=True, min_length=1, max_length=20_000)]


class LetterStarted(BaseModel):
    generation_id: UUID


class LetterStatus(BaseModel):
    status: Literal["running", "done", "failed"]
    letter: Optional[LetterOut] = None
    error: Optional[dict] = None
