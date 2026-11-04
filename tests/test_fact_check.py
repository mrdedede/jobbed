"""Fact check of generated documents: pure, no database."""

from ai.fact_check import check_cv, check_letter

SOURCE = """# Ana Lima
## Experience
**Backend Developer**, BNP Paribas SA, Paris. March 2019 - June 2022
- Cut API latency by 35% across 1,200 services; budget of 2.5 M€.
## Education
Université Paris-Saclay, 2015 - 2018
## Certifications
AWS Solutions Architect, 2021
"""


def cv(**fields):
    return {"cv_introduction": "", "profile_text": "", "skills": [], "experiences": [],
            "education": [], "certifications": [], **fields}


def values(flags):
    return [flag["value"] for flag in flags]


def test_numbers_from_the_source_pass_in_any_format():
    bullet = "Latence réduite de 35 % sur 1 200 services, budget de 2,5 M€, mars 2019."

    assert check_cv(cv(profile_text=bullet), SOURCE) == []


def test_invented_numbers_are_flagged_as_written():
    flags = check_cv(cv(profile_text="Réduit de 40 % sur 3 000 services."), SOURCE)

    assert flags == [{"kind": "number", "value": "40"},
                     {"kind": "number", "value": "3 000"}]


def test_a_number_is_a_whole_token_not_a_substring():
    # "5" is inside "2015" and "35%", but no number in the source is 5.
    assert values(check_cv(cv(profile_text="5 clients"), SOURCE)) == ["5"]


def test_a_changed_decimal_is_not_the_same_number():
    assert values(check_cv(cv(profile_text="25 M€"), SOURCE)) == ["25"]


def test_two_years_side_by_side_stay_two_numbers():
    assert values(check_cv(cv(profile_text="2019 2024"), SOURCE)) == ["2024"]


def test_digits_inside_a_name_are_not_quantities():
    assert check_cv(cv(profile_text="S3, K8s et ISO27001"), SOURCE) == []


def test_repeats_are_flagged_once():
    assert values(check_cv(cv(profile_text="40 et 40"), SOURCE)) == ["40"]


def test_names_match_without_case_accents_or_punctuation():
    document = cv(
        experiences=[{"company": "BNP PARIBAS", "bullets": []}],
        education=[{"institution": "Universite Paris Saclay"}],
        certifications=[{"name": "aws solutions architect"}])

    assert check_cv(document, SOURCE) == []


def test_invented_names_are_flagged_with_their_kind():
    document = cv(
        experiences=[{"company": "Google", "bullets": []}],
        education=[{"institution": "MIT"}],
        certifications=[{"name": "CKA"}])

    assert check_cv(document, SOURCE) == [
        {"kind": "employer", "value": "Google"},
        {"kind": "institution", "value": "MIT"},
        {"kind": "certification", "value": "CKA"}]


def test_a_name_must_match_whole_words():
    document = cv(experiences=[{"company": "BNP Pari", "bullets": []}])

    assert values(check_cv(document, SOURCE)) == ["BNP Pari"]


def test_an_empty_or_partial_cv_has_nothing_to_flag():
    assert check_cv(cv(), SOURCE) == []
    assert check_cv({"profile_text": "p"}, SOURCE) == []
    assert check_cv({"experiences": [{"company": "  "}]}, SOURCE) == []


def test_letter_numbers_can_come_from_any_source():
    letter = "Après 12 services, vos 300 agences et mes 4 ans de télétravail."

    assert check_letter(letter, cv(profile_text="12 services"),
                        {"motivation": "4 ans"}, "300 agences") == []


def test_letter_flags_numbers_no_source_has():
    assert check_letter("J'ai géré 50 personnes.", cv(), {"motivation": ""}, "") == [
        {"kind": "number", "value": "50"}]
