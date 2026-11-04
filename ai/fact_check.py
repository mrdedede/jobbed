"""Flag facts in a generated document that its sources do not contain.

Deterministic and pure: no model call, no I/O. The prompts say "never invent";
this checks what can be checked in code, and only warns the user.

ponytail: translated text cannot be compared with its source, so skills, roles
and bullet wording go unchecked and an invented tool in `skills` is not caught.
Add a language-aware comparison if that gap shows up in real CVs.

ponytail: numbers worked out from dates ("6 years" in `profile_text`) get
flagged. Add a narrow exemption if they turn out to dominate the flags.
"""

import re
import unicodedata
from typing import Dict, Iterable, Iterator, List, Set, Tuple

Flag = Dict[str, str]

_SEPARATOR = "[.,   ]"

#: One number, with its thousands groups and decimals. Digits glued after a
#: letter are names ("S3", "ISO27001"), not quantities. Groups of exactly three
#: digits only, so "2019 2021" stays two years.
_NUMBER = re.compile(
    rf"(?<!\w)(\d{{1,3}}(?:{_SEPARATOR}\d{{3}}(?!\d))+|\d+)(?:[.,](\d+))?(?!\d)")

_NOT_ALNUM = re.compile(r"[\W_]+")

#: CV list, field in each entry, and the kind of flag it raises.
_NAMED_FIELDS = (("experiences", "company", "employer"),
                 ("education", "institution", "institution"),
                 ("certifications", "name", "certification"))


def _strings(value: object) -> Iterator[str]:
    """Every string value in a JSON-like object."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)


def _numbers(text: str) -> Iterator[Tuple[str, str]]:
    """(number as written, comparable form) for each number in `text`."""
    for match in _NUMBER.finditer(text):
        whole = re.sub(_SEPARATOR, "", match[1]).lstrip("0") or "0"
        yield match[0], f"{whole}.{match[2]}" if match[2] else whole


def _number_flags(document: object, source_numbers: Set[str]) -> List[Flag]:
    return [{"kind": "number", "value": written}
            for text in _strings(document)
            for written, number in _numbers(text)
            if number not in source_numbers]


def _name_key(text: str) -> str:
    """Case, accents and punctuation removed, padded for whole-word matching."""
    decomposed = unicodedata.normalize("NFKD", text)
    plain = "".join(c for c in decomposed if not unicodedata.combining(c))

    return f" {_NOT_ALNUM.sub(' ', plain.casefold()).strip()} "


def _unique(flags: Iterable[Flag]) -> List[Flag]:
    return list({(flag["kind"], flag["value"]): flag for flag in flags}.values())


def check_cv(cv: dict, source_md: str) -> List[Flag]:
    """Numbers and names in a generated CV that its source CV does not have.

    Args:
        cv: The generated CV.
        source_md: The CV it was written from.

    Returns:
        Flags, each {"kind": "number" | "employer" | "institution" |
        "certification", "value": <as written in the CV>}, without repeats.
    """
    source_numbers = {number for _, number in _numbers(source_md)}
    source_names = _name_key(source_md)
    flags = _number_flags(cv, source_numbers)

    for section, field, kind in _NAMED_FIELDS:
        entries = cv.get(section) if isinstance(cv, dict) else None

        for entry in entries if isinstance(entries, list) else []:
            name = entry.get(field) if isinstance(entry, dict) else None

            if (isinstance(name, str) and name.strip()
                    and _name_key(name) not in source_names):
                flags.append({"kind": kind, "value": name})

    return _unique(flags)


def check_letter(letter: str, cv: dict, answers: dict,
                 description: str) -> List[Flag]:
    """Numbers in a letter that none of its sources have.

    Args:
        letter: The generated letter.
        cv: The tailored CV it was written from.
        answers: The candidate's answers.
        description: The posting, the allowed source of facts about the company.

    Returns:
        Flags, each {"kind": "number", "value": <as written in the letter>},
        without repeats.
    """
    source_numbers = {number
                      for source in (cv, answers, description)
                      for text in _strings(source)
                      for _, number in _numbers(text)}

    return _unique(_number_flags(letter, source_numbers))
