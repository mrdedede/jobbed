"""Invisible-character removal: pure, no database."""

import pytest

from job_scraper.text import strip_invisible, strip_invisible_json


@pytest.mark.parametrize("carrier", [
    "­", "͏", "؜", "᠎", "​", "‎", "‏", "‪", "‮",
    "⁠", "⁤", "⁦", "⁩", "⁪", "⁯", "﻿", "\U000e0000",
    "\U000e0041", "\U000e007f",
])
def test_always_stripped_characters_go_and_are_counted(carrier):
    assert strip_invisible(f"1{carrier}2") == ("12", 1)


@pytest.mark.parametrize("carrier", ["‌", "‍", "️", "\U000e0100"])
def test_contextual_characters_go_next_to_a_latin_letter(carrier):
    assert strip_invisible(f"déve{carrier}loppeur") == ("développeur", 1)
    assert strip_invisible(f"{carrier}{carrier}go") == ("go", 2)


@pytest.mark.parametrize("kept", [
    "\U0001f469‍\U0001f4bb dev",   # woman technologist: joiner inside an emoji
    "❤️ dev",                 # red heart with its variation selector
    "میخواهم‌برنامه",              # Persian: the joiner is part of the spelling
    "Paris : 5 000 €",        # French spacing
])
def test_legitimate_text_survives(kept):
    assert strip_invisible(kept) == (kept, 0)


def test_json_strings_are_cleaned_and_keys_and_other_values_are_not():
    value = {"a​": ["x​y", {"b": "\U000e0041z"}], "n": 3, "none": None}

    assert strip_invisible_json(value) == (
        {"a​": ["xy", {"b": "z"}], "n": 3, "none": None}, 2)
