"""Remove invisible Unicode from text that enters the system.

Stdlib only: the scraper, the AI layer and the API schemas all import it.
Removes characters that can carry hidden data or instructions. It cannot touch
a statistical watermark, which lives in the choice of words.
"""

import re
from typing import Tuple, TypeVar

_LATIN = r"[A-Za-zÀ-ɏ]"

#: No legitimate use in the text we handle. U+00A0 and U+202F stay out of this
#: set: French typography needs them.
_ALWAYS = re.compile(
    "[­͏؜᠎​‎‏‪-‮⁠-⁤"
    "⁦-⁩⁪-⁯﻿\U000e0000-\U000e007f]")

_CONTEXTUAL_CHARS = "[‌‍︀-️\U000e0100-\U000e01ef]+"

#: Joiners and variation selectors are needed inside emoji and some scripts,
#: so they only go when they touch a Latin letter.
_NEXT_TO_LATIN = re.compile(
    f"(?<={_LATIN}){_CONTEXTUAL_CHARS}|{_CONTEXTUAL_CHARS}(?={_LATIN})")

T = TypeVar("T")


def strip_invisible(text: str) -> Tuple[str, int]:
    """Remove invisible characters from one string.

    Args:
        text: Any text.

    Returns:
        Tuple of (cleaned text, number of characters removed).
    """
    cleaned = _NEXT_TO_LATIN.sub("", _ALWAYS.sub("", text))

    return cleaned, len(text) - len(cleaned)


def strip_invisible_json(value: T) -> Tuple[T, int]:
    """Apply `strip_invisible` to every string value in a JSON-like object.

    Args:
        value: A string, list or dict, nested to any depth. Dict keys and
            non-string values are left alone.

    Returns:
        Tuple of (cleaned copy, number of characters removed).
    """
    if isinstance(value, str):
        return strip_invisible(value)

    if isinstance(value, list):
        pairs = [strip_invisible_json(item) for item in value]

        return [item for item, _ in pairs], sum(count for _, count in pairs)

    if isinstance(value, dict):
        pairs = {key: strip_invisible_json(item) for key, item in value.items()}

        return ({key: item for key, (item, _) in pairs.items()},
                sum(count for _, count in pairs.values()))

    return value, 0
