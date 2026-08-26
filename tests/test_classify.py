"""Tests for jobbot.sources.classify, moved here from test_greenhouse.py
(M6 A1) when the classifier stopped being Greenhouse-specific. Every
assertion is unchanged from before the move.
"""

from __future__ import annotations

import pytest

from jobbot.models import normalize
from jobbot.sources.classify import (
    _APPRENTICESHIP_PATTERNS,
    _BARE_STAGE_PATTERN,
    _UNAMBIGUOUS_INTERNSHIP_PATTERNS,
    classify_contract_type,
)

# --- classify_contract_type(): A1 vocabulary, per CLAUDE.md rule 4's --------
# contract-type exemption. "This list is the spec, all of it must pass."
#
# Bare "stage"/"stages" is handled separately (see the next section): A2
# requires it to only count with French context, so it's tested at the
# vocabulary-pattern layer here (confirming the word forms ARE recognized as
# internship vocabulary) rather than through classify_contract_type() (whose
# end-to-end behaviour, guard included, is what the following section tests).

_APPRENTICESHIP_TERMS = [
    "alternance", "alternances", "alternant", "alternante", "alternants", "alternantes",
    "apprenti", "apprentie", "apprentis", "apprenties", "apprentissage",
    "apprentice", "apprentices", "apprenticeship", "apprenticeships",
    "contrat de professionnalisation", "contrat pro", "contrat de pro",
]

_INTERNSHIP_TERMS_UNAMBIGUOUS = [
    "stagiaire", "stagiaires",
    "intern", "interns", "internship", "internships",
    "pfe", "projet de fin d etudes", "projets de fin d etudes",
    "cesure", "annee de cesure",
]

_BARE_STAGE_TERMS = ["stage", "stages"]

_NON_MATCHING_TERMS = [
    "internal", "international", "internationale", "internaliser",
    "staging", "stagnation", "apprehension",
]


@pytest.mark.parametrize("term", _APPRENTICESHIP_TERMS)
def test_apprenticeship_vocabulary_matches(term: str) -> None:
    assert classify_contract_type(term, "") == "apprenticeship"


@pytest.mark.parametrize("term", _INTERNSHIP_TERMS_UNAMBIGUOUS)
def test_internship_vocabulary_matches(term: str) -> None:
    assert classify_contract_type(term, "") == "internship"


@pytest.mark.parametrize("term", _BARE_STAGE_TERMS)
def test_bare_stage_is_recognized_internship_vocabulary(term: str) -> None:
    # Real internship vocabulary at the pattern layer (A1) ...
    assert _BARE_STAGE_PATTERN.search(normalize(term)) is not None
    # ... but gated by French context end-to-end (A2); see the dedicated
    # tests below for the full classify_contract_type() behaviour.
    assert classify_contract_type(f"{term} de fin d annee", "") == "internship"


@pytest.mark.parametrize("term", _NON_MATCHING_TERMS)
def test_negative_terms_classify_as_other(term: str) -> None:
    assert classify_contract_type(term, "") == "other"


def test_apprenticeship_and_internship_vocabularies_do_not_overlap() -> None:
    apprenticeship_patterns = list(_APPRENTICESHIP_PATTERNS.values())
    internship_patterns = list(_UNAMBIGUOUS_INTERNSHIP_PATTERNS.values())
    for term in _APPRENTICESHIP_TERMS:
        normalized = normalize(term)
        assert any(p.search(normalized) for p in apprenticeship_patterns)
        assert not any(p.search(normalized) for p in internship_patterns)


# --- classify_contract_type(): A2 French-context guard on bare "stage" -----


def test_stage_in_english_business_context_classifies_as_other() -> None:
    assert classify_contract_type("Series B stage company", "") == "other"


def test_growth_stage_manager_classifies_as_other() -> None:
    assert classify_contract_type("Growth Stage Manager", "") == "other"


def test_staging_environment_engineer_classifies_as_other() -> None:
    assert classify_contract_type("Staging Environment Engineer", "") == "other"


def test_stage_with_french_context_classifies_as_internship() -> None:
    assert classify_contract_type("Stage de 6 mois en developpement", "") == "internship"


def test_stagiaire_needs_no_french_context_guard() -> None:
    # Unlike bare "stage", "stagiaire" is unambiguous on its own.
    assert classify_contract_type("Senior Stagiaire Program", "") == "internship"


def test_classify_contract_type_does_not_match_substrings() -> None:
    # "intern" must not match inside "international".
    assert classify_contract_type("International Sales Manager", "") == "other"


def test_classify_contract_type_apprenticeship_wins_when_both_match() -> None:
    assert classify_contract_type("Stage puis alternance possible", "") == "apprenticeship"


# --- classify_contract_type(): A1 title-weighting / context window --------


def test_description_mentioning_stagiaires_and_alternants_does_not_flip_a_senior_role() -> None:
    title = "Senior Data Engineer"
    description = "vous encadrerez des stagiaires et alternants"
    assert classify_contract_type(title, description) == "other"


def test_description_alternant_near_a_context_word_classifies_as_apprenticeship() -> None:
    title = "Senior Engineer"
    description = "Nous recherchons un alternant pour 12 mois"
    assert classify_contract_type(title, description) == "apprenticeship"


def test_title_authoritative_even_with_a_conflicting_description() -> None:
    # A clean internship title must not be dragged down by a description
    # that just happens to have no contract-context word nearby.
    title = "Stagiaire Support Client"
    description = "L'equipe accompagne des clients dans toute la France."
    assert classify_contract_type(title, description) == "internship"


@pytest.mark.parametrize("term", _APPRENTICESHIP_TERMS)
def test_apprenticeship_vocabulary_matches_in_title_still_passes(term: str) -> None:
    # A1 regression guard: the whole parametrized vocabulary suite must
    # still pass when the term is in the title (title is now checked first).
    assert classify_contract_type(term, "some unrelated description") == "apprenticeship"


@pytest.mark.parametrize("term", _INTERNSHIP_TERMS_UNAMBIGUOUS)
def test_internship_vocabulary_matches_in_title_still_passes(term: str) -> None:
    assert classify_contract_type(term, "some unrelated description") == "internship"


# --- classify_contract_type(): A3 employment_hint --------------------------


def test_employment_hint_alternance_on_neutral_title_and_description_yields_apprenticeship() -> (
    None
):
    # Deliberately no contract-context word anywhere, and no vocabulary in
    # the title or description -- employment_hint must carry the verdict on
    # its own, with the same authority as a title hit, not the windowed
    # (proximity-required) treatment ordinary description prose gets.
    title = "Backend Engineer"
    description = "Join our platform team working on distributed systems."
    assert classify_contract_type(title, description, employment_hint="Alternance") == (
        "apprenticeship"
    )


def test_empty_employment_hint_changes_nothing() -> None:
    title = "Senior Engineer"
    description = "Nous recherchons un alternant pour 12 mois"
    without_hint = classify_contract_type(title, description)
    with_empty_hint = classify_contract_type(title, description, employment_hint="")
    assert without_hint == with_empty_hint == "apprenticeship"
