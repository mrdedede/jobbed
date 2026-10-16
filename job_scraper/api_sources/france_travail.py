"""France Travail (formerly Pole Emploi) job search API.

Unlike a board, this is one nationwide search rather than one company's page,
so it needs its own OAuth2 client-credentials token instead of a plain GET.
The search response already carries the full posting text, so -- like WTTJ --
scrape_postings.py carries the description straight through instead of
re-fetching a page.

Credentials: FRANCE_TRAVAIL_CLIENT_ID and FRANCE_TRAVAIL_CLIENT_SECRET in the
environment (a mounted Secret in Kubernetes), else the JSON file at
paths.FRANCE_TRAVAIL_CREDENTIALS, gitignored, holding
{"client_id": "...", "client_secret": "..."}. Neither means the source is not
configured; fetch_jobs then returns [] and says so once, rather than raising,
same as any board that gives nothing.

The source is off until SEARCH_KEYWORDS is filled in: see the note there.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import List, Optional, Tuple

from job_scraper import paths
from job_scraper.fetching import fetch_json
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

#: The fixed search terms for the nightly pull -- global, not per user. Joined
#: into one `motsCles` query.
#:
#: Empty on purpose, for now: the terms will come from an analysis of what
#: users actually search for. **An empty list turns the source off** -- it
#: returns [] and makes no request, instead of pulling an unrestricted
#: nationwide set. Filling this list is what switches the source on.
SEARCH_KEYWORDS: List[str] = []

_logger = logging.getLogger(__name__)
_warned: set = set()


def _warn_once(key: str, message: str) -> None:
    """Say why the source is idle, once per process rather than per run."""
    if key not in _warned:
        _warned.add(key)
        _logger.warning(message)

#: Cached token, module-level: every call in a run shares one process, and a
#: fresh token per call would be one extra round trip per page for no reason.
_token: Optional[Tuple[str, float]] = None
# ponytail: no lock. fetch_jobs runs once per run and its token calls are
# sequential, so nothing can refresh concurrently. Add an asyncio.Lock if a
# second concurrent caller ever appears.


def _load_credentials() -> Optional[dict]:
    client_id = os.environ.get("FRANCE_TRAVAIL_CLIENT_ID")
    client_secret = os.environ.get("FRANCE_TRAVAIL_CLIENT_SECRET")

    if client_id and client_secret:
        return {"client_id": client_id, "client_secret": client_secret}

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


async def _get_token(session, credentials: dict) -> Optional[str]:
    global _token

    if _token and _token[1] > time.time():
        return _token[0]

    data = await fetch_json(
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


async def fetch_jobs(session) -> List[Job]:
    """Search France Travail's nationwide offer index for SEARCH_KEYWORDS.

    Args:
        session: httpx.AsyncClient (reused for both the token and search
            calls, same as every other source).

    Returns:
        Matching jobs, or [] if no credentials or keywords are configured or
        the API call fails -- never raises, so a misconfigured or down source cannot stop
        the rest of the run.
    """
    credentials = _load_credentials()

    if not credentials:
        _warn_once("credentials", "france_travail: no credentials "
                   "(FRANCE_TRAVAIL_CLIENT_ID/SECRET or the credentials "
                   "file); source skipped")

        return []

    keywords = SEARCH_KEYWORDS

    if not keywords:
        _warn_once("keywords", "france_travail: SEARCH_KEYWORDS is empty; "
                   "source skipped (fill it in to turn the source on)")

        return []

    token = await _get_token(session, credentials)

    if not token:
        return []

    headers = {"Authorization": f"Bearer {token}"}
    jobs: List[Job] = []
    seen_urls = set()
    start = 0

    while start < MAX_RESULTS:
        end = start + PAGE_SIZE - 1
        data = await fetch_json(
            session, SEARCH_URL, headers={
                **headers, "Range": f"offres {start}-{end}"
            },
            params={"motsCles": ",".join(keywords)},
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
