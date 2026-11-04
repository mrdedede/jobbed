"""Grade one posting against one CV: the model reports, `ai.scoring` grades."""

import json
import logging
from datetime import date
from typing import Optional, Tuple

import httpx

from ai import gateway, scoring
from job_scraper import paths

logger = logging.getLogger(__name__)

with open(paths.GRADE_JOB_MD, "r") as f:
    grade_job = f.read()

#: ATS name, as stored in job_postings.ats -> what its grade-job-<ats>.md adds
#: to the prompt. Any other ATS, or none, gets grade-job.md alone.
_ATS_EMPHASIS = {path.stem.removeprefix("grade-job-"): path.read_text()
                 for path in paths.AI.glob("grade-job-*.md")}

# Enforced by the model API, not by the prompt: asking for "no code fences" in
# prose got ignored often enough to break parsing. There is no grade here on
# purpose: `ai.scoring` computes it. The write-up comes last so it follows from
# the assessment instead of the other way round.
_NULLABLE_STRING = {"anyOf": [{"type": "string"}, {"type": "null"}]}

ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "requirements": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "kind": {"type": "string", "enum": [
                        "skill", "experience", "education", "language",
                        "certification", "other"]},
                    "importance": {"type": "string",
                                   "enum": ["required", "preferred"]},
                    "status": {"type": "string",
                               "enum": ["met", "transferable", "missing"]},
                    "evidence": {"type": "string"},
                    "quote": {"type": "string"},
                },
                "required": ["text", "kind", "importance", "status",
                             "evidence", "quote"],
                "additionalProperties": False,
            },
        },
        "required_years": {"anyOf": [{"type": "number"}, {"type": "null"}]},
        "experiences": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "company": {"type": "string"},
                    "role": {"type": "string"},
                    "start": {"type": "string"},
                    "end": _NULLABLE_STRING,
                    "relevant": {"type": "boolean"},
                },
                "required": ["company", "role", "start", "end", "relevant"],
                "additionalProperties": False,
            },
        },
        "onsite_country": _NULLABLE_STRING,
        "depth_analysis": {"type": "string"},
        # Optional: the job's location, for postings the scraper found none on.
        "place": {"type": "string"},
    },
    "required": ["requirements", "required_years", "experiences",
                 "onsite_country", "depth_analysis"],
    "additionalProperties": False,
}

#: The answer when a posting has no text: nothing to pay the model for.
_NOTHING_TO_ASSESS = ("The description carried nothing to judge, so this "
                      "posting could not be assessed.")


def build_prompt(cv_md: str, job: Tuple) -> str:
    """The grading prompt. `job` is (id, company, title, description, ats)."""
    _, company, title, description, ats = job
    emphasis = _ATS_EMPHASIS.get(ats)
    instructions = f"{grade_job}\n{emphasis}" if emphasis else grade_job

    return (f"{instructions}\n--------\n# Today's date\n"
            f"{date.today().isoformat()}\n--------\n"
            f"# User CV\n{cv_md}\n--------\n"
            f"# JOB POST\n\n## {title} -- {company}\n\n{description}")


def _squash(text: str) -> str:
    return " ".join(text.split()).casefold()


def _assessment(raw: str, description: str) -> Tuple[dict, str, str]:
    """Parse the model's answer. Raises on the wrong shape.

    Returns:
        (the facts `ai.scoring` needs, the write-up, the place).
    """
    verdict = json.loads(raw)
    analysis = verdict["depth_analysis"]

    if not isinstance(analysis, str):
        raise ValueError("depth_analysis is not text")

    required_years = verdict["required_years"]

    if required_years is not None and (
            isinstance(required_years, bool)
            or not isinstance(required_years, (int, float))
            or required_years < 0):
        raise ValueError(f"bad required_years: {required_years!r}")

    posting = _squash(description)
    # A requirement with no line in the posting behind it is the model's own.
    requirements = [r for r in verdict["requirements"]
                    if _squash(r["quote"]) and _squash(r["quote"]) in posting]
    dropped = len(verdict["requirements"]) - len(requirements)

    if dropped:
        logger.warning("dropped %s of %s requirements with no quote in the "
                       "posting", dropped, len(verdict["requirements"]))

    place = verdict.get("place")

    return ({"requirements": requirements, "required_years": required_years,
             "experiences": verdict["experiences"],
             "onsite_country": verdict["onsite_country"]},
            analysis, place.strip() if isinstance(place, str) else "")


def _with_breakdown(analysis: str, scored: dict) -> str:
    """The write-up plus the score, from the same numbers as the grade."""
    if scored["grade"] is None:
        return analysis

    lines = [f"**Grade: {scored['grade']}/100** (fit: {scored['fit']}/100)",
             *(f"- {note}" for note in scored["notes"])]

    return f"{analysis}\n\n" + "\n".join(lines)


async def analyze(job: Tuple, cv_md: str, model: str = gateway.HAIKU_MODEL,
                  client: Optional[httpx.AsyncClient] = None) -> dict:
    """Grade one posting.

    Args:
        job: (id, company, title, description, ats), as
            db.analysis.select_ungraded_jobs returns it.
        cv_md: The CV to grade against -- the user's active CV.
        model: Any model the gateway serves; Haiku unless told otherwise.
        client: Shared AsyncClient for a batch, optional.

    Returns:
        {"adequation_grade": int 0-100 or None, "depth_analysis": str,
        "place": str or None, "assessment": dict}. The grade is None when the
        posting has nothing to assess: a stored verdict, not a failure, so it
        does not come back in every batch.

    Raises:
        gateway.GatewayError: The model call failed.
        ValueError: The answer was not JSON of the expected shape.
    """
    description, ats = job[3] or "", job[4]
    today = date.today()

    if not description.strip():
        return {"adequation_grade": None, "depth_analysis": _NOTHING_TO_ASSESS,
                "place": None, "assessment": None}

    raw = await gateway.chat(
        model, build_prompt(cv_md, job), ANALYSIS_SCHEMA, client)

    try:
        assessment, analysis, place = _assessment(raw, description)
        scored = scoring.score(assessment, ats, today)
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ValueError(f"unusable model answer: {raw[:200]!r}") from exc

    return {"adequation_grade": scored["grade"],
            "depth_analysis": _with_breakdown(analysis, scored),
            "place": place or None,
            "assessment": {**assessment, "fit": scored["fit"],
                           "graded_on": today.isoformat()}}
