"""Tests for job_scraper/strategies/werecruit.py.

careers.werecruit.io serves every tenant from one host, so its root sitemap
listed ~12,000 foreign postings for each board. The tenant's own offers are
embedded in the board page as `window.allOffers`.
"""

from job_scraper.board import Board
from job_scraper.detector import ATSName
from job_scraper.strategies import VENDOR_SCRAPERS
from job_scraper.strategies.werecruit import scrape_werecruit
from job_scraper.urls import ats_from_host

URL = "https://careers.werecruit.io/en/unseenlabs"

PAGE = """<html><script>window.didomiOnReady = window.didomiOnReady || [];</script>
<script>
    window.allOffers = [
      {"Title":{"fr-fr":"Ing\\u00E9nieur syst\\u00E8me"},
       "TitleTranslated":"Ing\\u00E9nieur Syst\\u00E8me F/H",
       "Url":"https://careers.werecruit.io/en/unseenlabs/offers/ingenieur-systeme-ea8132",
       "Address_City":"Cesson-S\\u00E9vign\\u00E9"},
      {"Title":{"fr-fr":"Analyste SOC"},
       "Url":"/en/unseenlabs/offers/analyste-soc-802736"},
      {"Title":{"fr-fr":"No url"}},
      "junk"
    ];
    window.filtersList = [];
</script></html>"""


def board(html: str) -> Board:
    made = Board("unseenlabs", URL)
    made._html = html

    return made


def test_reads_only_the_tenants_embedded_offers():
    jobs = scrape_werecruit(board(PAGE))

    assert [(j.title, j.url, j.place) for j in jobs] == [
        ("Ingénieur Système F/H",
         "https://careers.werecruit.io/en/unseenlabs/offers/"
         "ingenieur-systeme-ea8132", "Cesson-Sévigné"),
        ("Analyste SOC",
         "https://careers.werecruit.io/en/unseenlabs/offers/"
         "analyste-soc-802736", None),
    ]
    assert {j.via for j in jobs} == {"werecruit"}


def test_page_without_offers_yields_nothing():
    assert scrape_werecruit(board("<html></html>")) == []
    assert scrape_werecruit(board("window.allOffers = [{broken")) == []


def test_host_resolves_to_werecruit_and_is_wired_in():
    assert ats_from_host(URL) == ATSName.WERECRUIT
    assert VENDOR_SCRAPERS[ATSName.WERECRUIT] is scrape_werecruit


def test_board_never_falls_through_to_the_multi_tenant_sitemap():
    made = board(PAGE)
    made.ats = ATSName.WERECRUIT

    jobs = made.scrape_board()

    assert len(jobs) == 2
    assert {j.via for j in jobs} == {"werecruit"}
