"""UI string catalog for the Streamlit dashboard.

Mirrors `cv_generator/docx_gen.load_l10n`: flat `values_<locale>.json` files,
one per locale, fail loud on an unknown key rather than printing the raw
`{key}` into the page. `t()` additionally does `str.format(**kwargs)`
substitution, since UI strings carry dynamic values (`docx_gen`'s do not).
"""

from __future__ import annotations

import json
from typing import Dict

import streamlit as st

from job_scraper import paths

DEFAULT_LOCALE = "en"


@st.cache_data(show_spinner=False)
def load_strings(locale: str) -> Dict[str, str]:
    """One locale's UI strings, keyed by `page.element.name`.

    Args:
        locale: Locale code matching a `values_<locale>.json` filename.

    Returns:
        The locale's key -> template string mapping.

    Raises:
        FileNotFoundError: If no file matches `locale`. Unlike `docx_gen`'s
            CV content, a missing UI locale has no sensible silent fallback --
            the selector only ever offers locales a file exists for.
    """
    path = paths.UI_L10N / f"values_{locale}.json"

    return json.loads(path.read_text(encoding="utf-8"))


def available_locales() -> list[str]:
    """Locale codes with a `values_<code>.json` file, sorted."""
    return sorted(p.stem.removeprefix("values_")
                  for p in paths.UI_L10N.glob("values_*.json"))


def t(key: str, **kwargs) -> str:
    """Look up `key` in the selected locale and substitute `kwargs`.

    Args:
        key: Catalog key, e.g. `"home.title"`.
        **kwargs: Values for any `{placeholder}` the template names.

    Returns:
        The formatted string.

    Raises:
        KeyError: If `key` is not in the selected locale's file -- printing
            a raw `{key}` into the page is a worse failure than an error.
    """
    locale = st.session_state.get("locale", DEFAULT_LOCALE)
    strings = load_strings(locale)

    if key not in strings:
        raise KeyError(f"no UI string for {key!r} in values_{locale}.json")

    return strings[key].format(**kwargs)
