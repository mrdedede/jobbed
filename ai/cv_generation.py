"""Rewrite the candidate's CV for one already-graded posting.

One gateway call per posting. The posting's own `depth_analysis` goes into
the prompt: it already says what the application should lead with, so the
rewrite does not have to rediscover it.
"""

import asyncio
import json
from typing import Optional
from uuid import UUID

import httpx

from ai import gateway
from db import generated_cv as db_generated_cv
from db import users
from job_scraper import paths
from ai.exceptions import NoActiveCv, NoAnalysisForJob, UnusableAnswer

with open(paths.GENERATE_CV_MD, "r") as f:
    generate_cv_prompt = f.read()

# Enforced by the model API, not by the prompt: the shape asked for in prose gets
# ignored often enough to break parsing (same lesson as ai.analysis).
# `languages` is deliberately absent -- it is copied verbatim from the CV at
# render time, so there is nothing for the model to decide.
_STRING_ARRAY = {"type": "array", "items": {"type": "string"}}

CV_SCHEMA = {
    "type": "object",
    "properties": {
        "locale": {"type": "string", "enum": ["en", "es", "fr", "pt"]},
        "cv": {
            "type": "object",
            "properties": {
                "cv_introduction": {"type": "string"},
                "profile_text": {"type": "string"},
                "skills": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "competence": {"type": "string"},
                            "skills": _STRING_ARRAY,
                        },
                        "required": ["competence", "skills"],
                        "additionalProperties": False,
                    },
                },
                "experiences": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "role": {"type": "string"},
                            "company": {"type": "string"},
                            "location": {"type": "string"},
                            "start_date": {"type": "string"},
                            "end_date": {"type": "string"},
                            "bullets": _STRING_ARRAY,
                        },
                        "required": ["role", "company", "location",
                                     "start_date", "end_date", "bullets"],
                        "additionalProperties": False,
                    },
                },
                "education": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "diploma": {"type": "string"},
                            "institution": {"type": "string"},
                            "location": {"type": "string"},
                            "period": {"type": "string"},
                            "degree": {"type": "string"},
                            "details": {"type": "string"},
                        },
                        "required": ["diploma", "institution", "location",
                                     "period", "degree", "details"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["cv_introduction", "profile_text", "skills",
                         "experiences", "education"],
            "additionalProperties": False,
        },
    },
    "required": ["locale", "cv"],
}


# Repeated after the posting: small local models weight the end of the prompt
# most, and Ollama truncates from the start when num_ctx overflows.
_LANGUAGE_REMINDER = (
    "FINAL REMINDER — MANDATORY: write the entire CV in the language of the "
    "JOB POST above, not the language of the candidate's CV, the analysis or "
    "these instructions. Set `locale` to that language and translate every "
    "text field into it. Only proper nouns stay unchanged.")


def _build_prompt(cv_md: str, depth_analysis: Optional[str], description: str) -> str:
    return (f"{generate_cv_prompt}\n--------\n# User CV\n{cv_md}\n"
            f"--------\n# ANALYSIS OF THIS POSTING\n\n{depth_analysis or ''}\n"
            f"--------\n# JOB POST\n\n{description}\n"
            f"--------\n{_LANGUAGE_REMINDER}")


async def generate_cv(user_id: UUID, job_id: UUID,
                      model: str = gateway.SONNET_MODEL,
                      cv_md: Optional[str] = None,
                      client: Optional[httpx.AsyncClient] = None) -> UUID:
    """Generate and store the CV for one posting this user already graded.

    Args:
        user_id: The user the CV is for.
        job_id: A posting with a row in this user's ai_analysis. Ungraded
            postings have no analysis to tailor against, so they are refused
            rather than generated blind.
        model: Which gateway model writes it -- Sonnet by default, not the
            Haiku grading runs on: this writes prose a recruiter reads.
        cv_md: The CV to rewrite. Defaults to the user's active CV.
        client: Shared AsyncClient, optional.

    Returns:
        The id of the stored generated_cv row.

    Raises:
        NoAnalysisForJob: Ungraded posting.
        NoActiveCv: No `cv_md` given and the user has no active CV.
        gateway.GatewayError: The model call failed.
        UnusableAnswer: The answer was not JSON of the expected shape.
    """
    row = await asyncio.to_thread(
        db_generated_cv.select_job_for_generation, user_id, job_id)

    if row is None:
        raise NoAnalysisForJob(f"no analysis stored for job {job_id} and user {user_id}")

    if cv_md is None:
        active = await asyncio.to_thread(users.select_active_user_cv, user_id)

        if active is None:
            raise NoActiveCv(f"user {user_id} has no active CV")

        cv_md = active[1]

    description, analysis_id, depth_analysis = row
    raw = await gateway.chat(
        model, _build_prompt(cv_md, depth_analysis, description),
        CV_SCHEMA, client)

    try:
        answer = json.loads(raw)
        locale, cv = answer["locale"], answer["cv"]
    except (ValueError, KeyError, TypeError) as exc:
        raise UnusableAnswer(f"unusable model answer: {raw[:200]!r}") from exc

    return await asyncio.to_thread(
        db_generated_cv.insert_generated_cv,
        user_id, locale, cv, job_id, analysis_id)
