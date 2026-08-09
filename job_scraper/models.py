"""The one job posting record, shared by every stage of the pipeline.

There used to be two ``Job`` dataclasses with overlapping fields -- one in the
board scraper, one in the post scraper -- which meant the same posting changed
type halfway through the run for no reason other than the second stage having
a ``description`` to add. This is that union: the board stage leaves
``description`` empty, the detail stage fills it in.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Job:
    """One job posting, as far as the pipeline has learned about it.

    Attributes:
        company: Hiring company.
        title: Job title.
        url: Job posting URL. Unique key for the whole pipeline -- it is what
            the detail stage resumes on and what the database constrains.
        place: Location, if a source named one.
        via: Which source produced this row. The board stage writes "feed" for
            a FEEDS row, the vendor's own name for a scraper that needed its
            own logic (workday, comeet, njoyn), or wordpress/sitemap/links for
            the generic fallbacks; "rendered" means it took a browser, so the
            board is unscrapable without one. The detail stage overwrites it
            with the extractor that read the posting page -- workday, jsonld,
            main, body, or none.

            Keeping it on both stages is deliberate and it earns its place: an
            extractor that starts returning nav furniture, or a strategy that
            quietly stops producing, stays visible in the output without
            instrumenting anything.
        description: Plain-text posting body. Empty until the detail stage
            fetches the posting's own page.
    """

    company: str
    title: str
    url: str
    place: Optional[str] = None
    via: str = ""
    description: str = ""
