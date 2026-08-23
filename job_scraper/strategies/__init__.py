"""Scraping strategies, cheapest and highest-fidelity first.

A board is tried against each in turn -- feed, WordPress REST, sitemap,
anchors, and only with a renderer, a browser pass. Every strategy returns []
rather than raising, so a board never dies on its best path and always falls
through to the next.
"""

from typing import Callable, Dict

from job_scraper.detector import ATSName
from job_scraper.strategies.comeet import scrape_comeet
from job_scraper.strategies.feed import FEEDS, Feed, scrape_feed
from job_scraper.strategies.inline_json import scrape_inline_json
from job_scraper.strategies.links import JOB_PATH, scrape_links
from job_scraper.strategies.njoyn import scrape_njoyn
from job_scraper.strategies.notes import VENDOR_NOTES
from job_scraper.strategies.sitemap import scrape_sitemap
from job_scraper.strategies.welcometothejungle import scrape_wttj
from job_scraper.strategies.wordpress import scrape_wordpress
from job_scraper.strategies.workday import scrape_workday

#: ATS -> vendor scraper. Separate from FEEDS because these are functions, not
#: table rows: they need logic a Feed cannot express -- a POST paging loop for
#: Workday, a key lifted off the board page for Comeet, a table read as rows
#: for njoyn.
#:
#: An ATS absent from both this and FEEDS simply has no feed path and falls
#: through to the generic strategies. See notes.VENDOR_NOTES for what was
#: probed and why it is not here yet.
VENDOR_SCRAPERS: Dict[ATSName, Callable] = {
    ATSName.WORKDAY: scrape_workday,
    ATSName.COMEET: scrape_comeet,
    ATSName.NJOYN: scrape_njoyn,
    ATSName.WTTJ: scrape_wttj,
}

__all__ = [
    "FEEDS",
    "Feed",
    "JOB_PATH",
    "VENDOR_NOTES",
    "VENDOR_SCRAPERS",
    "scrape_comeet",
    "scrape_feed",
    "scrape_inline_json",
    "scrape_links",
    "scrape_njoyn",
    "scrape_sitemap",
    "scrape_wordpress",
    "scrape_workday",
    "scrape_wttj",
]
