"""Rewrite the candidate's CV for one already-graded posting.

One `claude` CLI call per posting. The posting's own `depth_analysis` goes into
the prompt: it already says what the application should lead with, so the
rewrite does not have to rediscover it.
"""

import json

from ai import call_model
from db import db_connection
from job_scraper import paths

with open(paths.GENERATE_CV_MD, "r") as f:
    generate_cv = f.read()

with open(paths.CV_MD, "r") as f:
    my_cv = f.read()

# Enforced by the CLI, not by the prompt: the shape asked for in prose gets
# ignored often enough to break parsing (same lesson as ai.analysis).
# `languages` is deliberately absent -- it is copied verbatim from my_cv.md at
# render time, so there is nothing for the model to decide.
_STRING_ARRAY = {"type": "array", "items": {"type": "string"}}

CV_SCHEMA = json.dumps({
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
})

GENERATION_PROMPT = f"""{generate_cv}
--------
# User CV
{my_cv}
--------
# ANALYSIS OF THIS POSTING

"""


def _generate(job_id: int):
    """Rewrite the CV for one graded posting.

    Args:
        job_id: A posting that already has a row in ai_analysis. Ungraded
            postings have no analysis to tailor against, so they are refused
            rather than generated blind.

    Returns:
        Tuple of (ai_analysis_id, verdict dict), or None when the posting is
        ungraded, the CLI failed, or its output would not parse.
    """
    row = db_connection.select_job_for_generation(job_id)
    if row is None:
        print(f"No analysis stored for job {job_id}")
        return None

    description, analysis_id, depth_analysis = row
    final_prompt = (f"{GENERATION_PROMPT}{depth_analysis}\n"
                    f"--------\n# JOB POST\n\n{description}")

    # Sonnet, not the Haiku the grading runs on: this one writes prose the
    # candidate sends to a recruiter.
    result = call_model.call_claude(call_model.SONNET_MODEL, final_prompt, CV_SCHEMA)
    if result is None:
        return None

    try:
        return analysis_id, json.loads(result.stdout)
    except json.JSONDecodeError:
        print(f"Failed to parse JSON: {result.stdout}")
        return None

def generate_cv(job_id: int) -> str:
    """Generate and store the CV for one graded posting.

    Args:
        job_id: A posting that already has a row in ai_analysis.

    Returns:
        The locale the model wrote the CV in.

    Raises:
        RuntimeError: If the posting is ungraded or the model call failed.
            `_generate` reports both by returning None, and unpacking that
            raised a bare TypeError that said nothing about either.
    """
    result = _generate(job_id)
    if result is None:
        raise RuntimeError(f"no CV generated for job {job_id} -- ungraded "
                           f"posting, or the model call failed")

    analysis_id, model_answer = result
    db_connection.insert_generated_cv(
        model_answer["locale"],
        model_answer["cv"],
        job_id,
        analysis_id
    )

    return model_answer["locale"]