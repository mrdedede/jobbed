"""Suggest keywords and blacklist terms from a CV, through the LLM gateway.

Nothing is stored here: the user reviews the suggestions and adds the ones
they keep (POST /v1/me/terms).
"""

import json
from typing import Dict, List, Optional

import httpx

from ai import gateway
from ai.exceptions import UnusableAnswer
from job_scraper import paths

with open(paths.SUGGEST_TERMS_MD, "r") as f:
    suggest_terms_prompt = f.read()

_STRING_ARRAY = {"type": "array", "items": {"type": "string"}}

TERMS_SCHEMA = {
    "type": "object",
    "properties": {"keywords": _STRING_ARRAY, "blacklist": _STRING_ARRAY},
    "required": ["keywords", "blacklist"],
    "additionalProperties": False,
}

#: Per list. The prompt asks for fewer; this only bounds a runaway answer.
MAX_SUGGESTIONS = 30


def _clean(terms) -> List[str]:
    """Normalise like api.schemas.Term (strip, lowercase, 1-100 chars), dedupe."""
    if not isinstance(terms, list):
        raise TypeError("not a list")

    seen = []
    for term in terms:
        if not isinstance(term, str):
            continue
        term = term.strip().lower()
        if 0 < len(term) <= 100 and term not in seen:
            seen.append(term)

    return seen[:MAX_SUGGESTIONS]


async def suggest_terms(cv_md: str, model: str,
                        client: Optional[httpx.AsyncClient] = None
                        ) -> Dict[str, List[str]]:
    """Ask the model for search terms fitting this CV.

    Returns:
        {"keywords": [...], "blacklist": [...]}, normalised and deduplicated.
        A term suggested for both lists is kept as a keyword only.

    Raises:
        gateway.GatewayError: The model call failed.
        UnusableAnswer: The answer was not JSON of the expected shape.
    """
    raw = await gateway.chat(
        model, f"{suggest_terms_prompt}\n--------\n# User CV\n{cv_md}",
        TERMS_SCHEMA, client)

    try:
        answer = json.loads(raw)
        keywords = _clean(answer["keywords"])
        blacklist = _clean(answer["blacklist"])
    except (ValueError, KeyError, TypeError) as exc:
        raise UnusableAnswer(f"unusable model answer: {raw[:200]!r}") from exc

    return {"keywords": keywords,
            "blacklist": [t for t in blacklist if t not in keywords]}
