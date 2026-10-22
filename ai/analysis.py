"""Grade one posting against one CV, through the LLM gateway."""

import json
from typing import Optional, Tuple

import httpx

from ai import gateway
from job_scraper import paths

with open(paths.GRADE_JOB_MD, "r") as f:
    grade_job = f.read()

# Enforced by the model API, not by the prompt: asking for "no code fences" in
# prose got ignored often enough to break parsing. The 0-100 range is checked
# in `analyze` -- structured-output schemas do not reliably honour min/max.
ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "adequation_grade": {"type": "integer"},
        "depth_analysis": {"type": "string"},
    },
    "required": ["adequation_grade", "depth_analysis"],
    "additionalProperties": False,
}


def build_prompt(cv_md: str, job: Tuple) -> str:
    """The grading prompt. `job` is (id, company, title, description)."""
    _, company, title, description = job

    return (f"{grade_job}\n--------\n# User CV\n{cv_md}\n--------\n"
            f"# JOB POST\n\n## {title} -- {company}\n\n{description}")


async def analyze(job: Tuple, cv_md: str, model: str = gateway.HAIKU_MODEL,
                  client: Optional[httpx.AsyncClient] = None) -> dict:
    """Grade one posting.

    Args:
        job: (id, company, title, description), as
            db.analysis.select_jobs_to_analyse returns it.
        cv_md: The CV to grade against -- the user's active CV.
        model: Any model the gateway serves; Haiku unless told otherwise.
        client: Shared AsyncClient for a batch, optional.

    Returns:
        {"adequation_grade": int 0-100, "depth_analysis": str}.

    Raises:
        gateway.GatewayError: The model call failed.
        ValueError: The answer was not JSON of the expected shape.
    """
    raw = await gateway.chat(
        model, build_prompt(cv_md, job), ANALYSIS_SCHEMA, client)

    try:
        verdict = json.loads(raw)
        grade = verdict["adequation_grade"]
        analysis = verdict["depth_analysis"]
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError(f"unusable model answer: {raw[:200]!r}") from exc

    if not isinstance(grade, int) or isinstance(grade, bool) \
            or not 0 <= grade <= 100:
        raise ValueError(f"grade out of range: {grade!r}")

    if not isinstance(analysis, str):
        raise ValueError("depth_analysis is not text")

    return {"adequation_grade": grade, "depth_analysis": analysis}
