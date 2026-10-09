"""France Travail (formerly Pole Emploi) job search API.

Unlike a board, this is one nationwide search rather than one company's page,
so it needs its own OAuth2 client-credentials token instead of a plain GET.
The search response already carries the full posting text, so -- like WTTJ --
post_scraper.py carries the description straight through instead of
re-fetching a page.

Credentials: user_info/france_travail_credentials.json, gitignored, holding
{"client_id": "...", "client_secret": "..."}. Missing or unreadable file means
the source is not configured; fetch_jobs then returns [] rather than raising,
same as any board that gives nothing.
"""

from __future__ import annotations

import json
import time
from typing import List, Optional, Tuple

from job_scraper import paths
from job_scraper.fetching import fetch_json
from job_scraper.filters import get_keywords
from job_scraper.models import Job

TOKEN_URL = (
    "https://entreprise.pole-emploi.fr/connexion/oauth2/access_token"
    "?realm=/partenaire"
)
SEARCH_URL = "https://api.pole-emploi.io/partenaire/offresdemploi/v2/offres/search"
SCOPE = "api_offresdemploiv2 o2dsoffre"

#: One page per call; the API refuses a range wider than this.
PAGE_SIZE = 150

#: Hard cap on results per run -- this is a keyword-matched search across all
#: of France, not one company's board, so an unbounded pull is a different
#: kind of run than everything else in the pipeline.
MAX_RESULTS = 1500

#: Cached token, module-level: every call in a run shares one process, and a
#: fresh token per call would be one extra round trip per page for no reason.
_token: Optional[Tuple[str, float]] = None


def _load_credentials() -> Optional[dict]:
    if not paths.FRANCE_TRAVAIL_CREDENTIALS.exists():
        return None

    try:
        data = json.loads(
            paths.FRANCE_TRAVAIL_CREDENTIALS.read_text(encoding="utf-8")
        )
    except (ValueError, OSError):
        return None

    if not data.get("client_id") or not data.get("client_secret"):
        return None

    return data


def _get_token(session, credentials: dict) -> Optional[str]:
    global _token

    if _token and _token[1] > time.time():
        return _token[0]

    data = fetch_json(
        session, TOKEN_URL, method="post",
        data={
            "grant_type": "client_credentials",
            "client_id": credentials["client_id"],
            "client_secret": credentials["client_secret"],
            "scope": SCOPE,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    if not isinstance(data, dict) or "access_token" not in data:
        return None

    # 60s margin so a token does not expire mid-page.
    expires_at = time.time() + float(data.get("expires_in", 0)) - 60
    _token = (data["access_token"], expires_at)

    return _token[0]


def _job_from_offer(offer: dict) -> Optional[Job]:
    url = offer.get("origineOffre", {}).get("urlOrigine") or offer.get("id")
    title = offer.get("intitule")

    if not url or not title:
        return None

    return Job(
        company=(offer.get("entreprise") or {}).get("nom") or "",
        title=title,
        url=url,
        place=(offer.get("lieuTravail") or {}).get("libelle"),
        via="france_travail",
        description=offer.get("description") or "",
    )


def fetch_jobs(session) -> List[Job]:
    """Search France Travail's nationwide offer index for the user's keywords.

    Args:
        session: Requests session (reused for both the token and search
            calls, same as every other source).

    Returns:
        Matching jobs, or [] if no credentials are configured or the API call
        fails -- never raises, so a misconfigured or down source cannot stop
        the rest of the run.
    """
    credentials = _load_credentials()

    if not credentials:
        return []

    token = _get_token(session, credentials)

    if not token:
        return []

    keywords = get_keywords()
    headers = {"Authorization": f"Bearer {token}"}
    jobs: List[Job] = []
    seen_urls = set()
    start = 0

    while start < MAX_RESULTS:
        end = start + PAGE_SIZE - 1
        data = fetch_json(
            session, SEARCH_URL, headers={
                **headers, "Range": f"offres {start}-{end}"
            },
            params={"motsCles": ",".join(keywords)} if keywords else {},
        )

        if not isinstance(data, dict):
            break

        offers = data.get("resultats") or []

        for offer in offers:
            job = _job_from_offer(offer)

            if job and job.url not in seen_urls:
                seen_urls.add(job.url)
                jobs.append(job)

        if len(offers) < PAGE_SIZE:
            break

        start += PAGE_SIZE

    return jobs
