"""i18n.t() resolves a key against the selected locale and substitutes it."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "visualization"))

st = pytest.importorskip("streamlit", reason="streamlit not installed")
i18n = pytest.importorskip("i18n", reason="streamlit not installed")


def test_resolves_a_known_key_and_substitutes_kwargs():
    st.session_state["locale"] = "en"

    assert i18n.t("scrape_jobs.log.queued", count=3) == "3 postings queued"


def test_raises_on_an_unknown_key():
    st.session_state["locale"] = "en"

    with pytest.raises(KeyError):
        i18n.t("does.not.exist")


def test_available_locales_includes_english():
    assert "en" in i18n.available_locales()
