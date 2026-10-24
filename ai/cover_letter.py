"""Write a cover letter from one of the candidate's generated CVs.

One gateway call per letter. Builds on the tailored CV rather than the source
one: it is already in the posting's language and ordered by what the
posting's `depth_analysis` said to lead with. The candidate's answers are the
only source of motivation, so the model has no reason to make one up.
"""

import asyncio
import json
from typing import Optional
from uuid import UUID

import httpx

from ai import gateway
from ai.exceptions import NoGeneratedCv, UnusableAnswer
from db import cover_letters as db_letters
from job_scraper import paths

with open(paths.COVER_LETTER_MD, "r") as f:
    cover_letter_prompt = f.read()

LETTER_SCHEMA = {
    "type": "object",
    "properties": {
        "locale": {"type": "string", "enum": ["en", "es", "fr", "pt"]},
        "letter": {"type": "string"},
    },
    "required": ["locale", "letter"],
    "additionalProperties": False,
}

#: Prompt headings for the user's answers, in the order the form asks them.
_QUESTIONS = {
    "motivation": "Why this job is interesting to me",
    "career_fit": "How this job aligns with my career goals",
    "offer_fit": "Why this offer makes sense to me",
}

# Repeated after the posting, as in ai.cv_generation: the end of the prompt
# weighs most, and the answers above are often in another language.
_LANGUAGE_REMINDER = (
    "FINAL REMINDER — MANDATORY: write the entire letter in the language of "
    "the JOB POST above, not the language of the CV, the analysis, the "
    "candidate's answers or these instructions. Set `locale` to that language.")


def _build_prompt(cv: dict, depth_analysis: Optional[str], answers: dict,
                  description: str) -> str:
    answered = "\n\n".join(
        f"## {heading}\n{answers.get(key) or '(no answer)'}"
        for key, heading in _QUESTIONS.items())

    return (f"{cover_letter_prompt}\n--------\n# TAILORED CV\n"
            f"{json.dumps(cv, ensure_ascii=False, indent=1)}\n"
            f"--------\n# ANALYSIS OF THIS POSTING\n\n{depth_analysis or ''}\n"
            f"--------\n# CANDIDATE'S ANSWERS\n\n{answered}\n"
            f"--------\n# JOB POST\n\n{description}\n"
            f"--------\n{_LANGUAGE_REMINDER}")


async def generate_cover_letter(user_id: UUID, cv_id: UUID, answers: dict,
                                model: str = gateway.SONNET_MODEL,
                                client: Optional[httpx.AsyncClient] = None) -> UUID:
    """Write and store a cover letter from one of this user's generated CVs.

    Args:
        user_id: The user the letter is for.
        cv_id: One of this user's generated_cv rows.
        answers: The user's answers, keyed like `_QUESTIONS`; blanks allowed.
        model: Which gateway model writes it -- Sonnet, as for CVs: this is
            prose a recruiter reads.
        client: Shared AsyncClient, optional.

    Returns:
        The id of the stored cover_letters row.

    Raises:
        NoGeneratedCv: No such generated CV for this user.
        gateway.GatewayError: The model call failed.
        UnusableAnswer: The answer was not JSON of the expected shape, or
            the letter is empty (the model refused to write one).
    """
    row = await asyncio.to_thread(db_letters.select_cv_for_letter, user_id, cv_id)

    if row is None:
        raise NoGeneratedCv(f"no generated cv {cv_id} for user {user_id}")

    job_id, cv, description, depth_analysis = row
    raw = await gateway.chat(
        model, _build_prompt(cv, depth_analysis, answers, description or ""),
        LETTER_SCHEMA, client)

    try:
        answer = json.loads(raw)
        locale, letter = answer["locale"], answer["letter"].strip()
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise UnusableAnswer(f"unusable model answer: {raw[:200]!r}") from exc

    if not letter:
        raise UnusableAnswer("the model wrote an empty letter")

    return await asyncio.to_thread(
        db_letters.insert_cover_letter,
        user_id, cv_id, job_id, locale, answers, letter)
