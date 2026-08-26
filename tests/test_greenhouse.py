from __future__ import annotations

import httpx
import pytest
import respx
from conftest import TEST_USER_AGENT

from jobbot.models import normalize
from jobbot.sources.base import SourceEmptyError, SourceError, SourceNotFoundError
from jobbot.sources.greenhouse import (
    _APPRENTICESHIP_PATTERNS,
    _BARE_STAGE_PATTERN,
    _UNAMBIGUOUS_INTERNSHIP_PATTERNS,
    GreenhouseSource,
    _strip_html,
    classify_contract_type,
)

BOARD_URL = "https://boards-api.greenhouse.io/v1/boards/acme/jobs"


def _make_source(
    client: httpx.Client, identifier: str = "acme", company_name: str = "Acme Corp"
) -> GreenhouseSource:
    return GreenhouseSource(identifier, company_name, client, user_agent=TEST_USER_AGENT)


# --- parse() -----------------------------------------------------------


def test_parse_classifies_every_fixture_entry_correctly(greenhouse_payload, greenhouse_source):
    jobs = {job.external_id: job for job in greenhouse_source.parse(greenhouse_payload["jobs"])}

    assert jobs["1001"].contract_type == "apprenticeship"  # French alternance
    assert jobs["1002"].contract_type == "internship"  # French stage
    assert jobs["1003"].contract_type == "internship"  # English internship
    assert jobs["1004"].contract_type == "other"  # senior full-time
    assert jobs["1005"].contract_type == "apprenticeship"  # apprenti, no location
    assert jobs["1006"].contract_type == "internship"  # stage, HTML entities
    assert jobs["1008"].contract_type == "apprenticeship"  # repost of 1001
    assert "1007" not in jobs  # malformed entry, must be skipped


def test_parse_skips_malformed_entry_and_returns_the_rest(greenhouse_payload, greenhouse_source):
    jobs = greenhouse_source.parse(greenhouse_payload["jobs"])
    assert len(jobs) == 7  # 8 fixture entries minus the one malformed entry


def test_missing_location_becomes_empty_string_not_a_crash(greenhouse_payload, greenhouse_source):
    jobs = {job.external_id: job for job in greenhouse_source.parse(greenhouse_payload["jobs"])}
    assert jobs["1005"].location == ""


def test_html_becomes_readable_plain_text(greenhouse_payload, greenhouse_source):
    jobs = {job.external_id: job for job in greenhouse_source.parse(greenhouse_payload["jobs"])}
    description = jobs["1006"].description

    assert "<p>" not in description
    assert "<div>" not in description
    assert "<li>" not in description
    assert "&amp;" not in description
    assert "&eacute;" not in description

    assert "Missions & responsabilités:" in description
    assert "Analyse de données" in description
    # An entity-encoded "<hebdomadaire>" must survive as literal text, not be
    # eaten by tag-stripping: content is decoded *after* real tags are gone.
    assert "Reporting <hebdomadaire>" in description
    assert "800€/mois" in description


def test_strip_html_on_empty_content_returns_empty_string() -> None:
    assert _strip_html("") == ""


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


# --- fetch_raw() / fetch() ----------------------------------------------


def test_fetch_raw_returns_a_two_tuple(greenhouse_payload, mock_client) -> None:
    source = _make_source(mock_client)
    with respx.mock:
        respx.get(BOARD_URL).mock(return_value=httpx.Response(200, json=greenhouse_payload))
        result = source.fetch_raw()

    assert isinstance(result, tuple)
    assert len(result) == 2
    raw_items, new_etag = result
    assert isinstance(raw_items, list)
    assert new_etag is None


def test_fetch_on_mocked_200_returns_expected_count(greenhouse_payload, mock_client) -> None:
    source = _make_source(mock_client)
    with respx.mock:
        respx.get(BOARD_URL).mock(return_value=httpx.Response(200, json=greenhouse_payload))
        jobs = source.fetch()

    assert len(jobs) == 7  # 8 fixture entries minus the malformed one


def test_fetch_raises_source_empty_error_on_empty_jobs_array(mock_client) -> None:
    source = _make_source(mock_client)
    with respx.mock:
        respx.get(BOARD_URL).mock(return_value=httpx.Response(200, json={"jobs": []}))
        with pytest.raises(SourceEmptyError):
            source.fetch()


def test_fetch_retries_once_on_500_then_succeeds(greenhouse_payload, mock_client) -> None:
    source = _make_source(mock_client)
    with respx.mock:
        route = respx.get(BOARD_URL)
        route.side_effect = [httpx.Response(500), httpx.Response(200, json=greenhouse_payload)]
        jobs = source.fetch()

    assert len(jobs) == 7
    assert route.call_count == 2


def test_fetch_retries_once_on_timeout_then_succeeds(greenhouse_payload, mock_client) -> None:
    source = _make_source(mock_client)
    with respx.mock:
        route = respx.get(BOARD_URL)
        route.side_effect = [
            httpx.TimeoutException("timed out"),
            httpx.Response(200, json=greenhouse_payload),
        ]
        jobs = source.fetch()

    assert len(jobs) == 7
    assert route.call_count == 2


def test_fetch_raises_source_error_after_exhausting_retries(mock_client) -> None:
    source = _make_source(mock_client)
    with respx.mock:
        route = respx.get(BOARD_URL).mock(return_value=httpx.Response(500))
        with pytest.raises(SourceError):
            source.fetch()

    assert route.call_count == 2  # one request, one retry, then give up


def test_fetch_does_not_retry_on_4xx(mock_client) -> None:
    url = "https://boards-api.greenhouse.io/v1/boards/missing-co/jobs"
    source = _make_source(mock_client, "missing-co", "Missing Co")
    with respx.mock:
        route = respx.get(url).mock(return_value=httpx.Response(410))  # Gone, not 404
        with pytest.raises(SourceError):
            source.fetch()

    assert route.call_count == 1


def test_fetch_raises_source_not_found_error_on_404(mock_client) -> None:
    url = "https://boards-api.greenhouse.io/v1/boards/missing-co/jobs"
    source = _make_source(mock_client, "missing-co", "Missing Co")
    with respx.mock:
        route = respx.get(url).mock(return_value=httpx.Response(404))
        with pytest.raises(SourceNotFoundError):
            source.fetch()

    assert route.call_count == 1
    # SourceNotFoundError is a SourceError, so callers that only catch the
    # base class still see it.
    with respx.mock:
        respx.get(url).mock(return_value=httpx.Response(404))
        with pytest.raises(SourceError):
            source.fetch()


def test_user_agent_header_matches_what_was_injected(greenhouse_payload, mock_client) -> None:
    custom_user_agent = "jobbot-test/9.9 (+someone-else@example.invalid)"
    source = GreenhouseSource("acme", "Acme Corp", mock_client, user_agent=custom_user_agent)
    with respx.mock:
        route = respx.get(BOARD_URL).mock(return_value=httpx.Response(200, json=greenhouse_payload))
        source.fetch()

    sent_headers = route.calls.last.request.headers
    assert sent_headers["User-Agent"] == custom_user_agent
