"""Job sources reached through a search API rather than a company board.

A board is one company's URL, detected and scraped through `Board`. An API
source is a nationwide search with its own auth, so it does not fit that
model -- it is called once per run, not once per board, and it hands back a
finished list of `Job`s directly.

API_SOURCES mirrors strategies.VENDOR_SCRAPERS: a name -> callable table,
extended by adding an entry, not a class.
"""

from typing import Callable, Dict, List

import requests

from job_scraper.api_sources.france_travail import fetch_jobs as fetch_france_travail
from job_scraper.models import Job

API_SOURCES: Dict[str, Callable[[requests.Session], List[Job]]] = {
    "france_travail": fetch_france_travail,
}

__all__ = ["API_SOURCES"]
