from __future__ import annotations

import re
from pathlib import Path

import pytest

from jobbot.filters import (
    FilterConfig,
    FilterConfigError,
    JobFilter,
    KeywordFilterConfig,
    LocationFilterConfig,
    load_filters,
)
from jobbot.models import Job

_COUNTER = {"n": 0}


def _make_job(**overrides: object) -> Job:
    _COUNTER["n"] += 1
    fields = {
        "company": "Acme Corp",
        "title": "Ingénieur Logiciel",
        "location": "Paris, France",
        "contract_type": "internship",
        "description": "",
        "url": f"https://example.com/jobs/{_COUNTER['n']}",
        "source": "greenhouse",
        "external_id": str(_COUNTER["n"]),
    }
    fields.update(overrides)
    return Job(**fields)


def _write_yaml(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


@pytest.fixture
def paris_data_config() -> FilterConfig:
    """Mirrors the shipped filters.yaml (the "Paris + data" config from B1),
    built directly as Python objects so these tests don't depend on the
    real filters.yaml file's exact current contents."""
    return FilterConfig(
        locations=LocationFilterConfig(
            include=[
                "paris", "ile-de-france", "75", "92", "93", "94", "nantes", "44",
                "loire-atlantique", "remote", "teletravail", "hybride",
            ],
            exclude=["lyon", "marseille", "bordeaux", "toulouse"],
            match_mode="substring",
            unknown_location="keep",
        ),
        contract_types=["internship", "apprenticeship"],
        keywords=KeywordFilterConfig(
            include=[
                "data", "donnees", "machine learning", "deep learning", "ia", "ai", "llm",
                "nlp", "analytics", "statistiques", "python", "sql", "data science",
                "data engineer", "mlops", "computer vision",
            ],
            exclude=["sales", "commercial", "vente", "recrutement"],
            match_mode="word",
            fields=["title", "description"],
            require="any",
        ),
    )


# --- config validation ---------------------------------------------------


def test_bad_match_mode_raises(tmp_path: Path) -> None:
    file = _write_yaml(
        tmp_path / "filters.yaml",
        """
locations:
  include: [paris]
  match_mode: fuzzy
contract_types: [internship]
keywords: {}
""",
    )
    with pytest.raises(FilterConfigError):
        load_filters(file)


def test_bad_require_raises(tmp_path: Path) -> None:
    file = _write_yaml(
        tmp_path / "filters.yaml",
        """
locations:
  include: [paris]
contract_types: [internship]
keywords:
  require: always
""",
    )
    with pytest.raises(FilterConfigError):
        load_filters(file)


def test_bad_contract_type_raises(tmp_path: Path) -> None:
    file = _write_yaml(
        tmp_path / "filters.yaml",
        """
locations:
  include: [paris]
contract_types: [intership]
keywords: {}
""",
    )
    with pytest.raises(FilterConfigError):
        load_filters(file)


def test_empty_locations_include_raises(tmp_path: Path) -> None:
    file = _write_yaml(
        tmp_path / "filters.yaml",
        """
locations:
  include: []
contract_types: [internship]
keywords: {}
""",
    )
    with pytest.raises(FilterConfigError):
        load_filters(file)


def test_empty_contract_types_raises(tmp_path: Path) -> None:
    file = _write_yaml(
        tmp_path / "filters.yaml",
        """
locations:
  include: [paris]
contract_types: []
keywords: {}
""",
    )
    with pytest.raises(FilterConfigError):
        load_filters(file)


def test_empty_keywords_fields_raises(tmp_path: Path) -> None:
    file = _write_yaml(
        tmp_path / "filters.yaml",
        """
locations:
  include: [paris]
contract_types: [internship]
keywords:
  fields: []
""",
    )
    with pytest.raises(FilterConfigError):
        load_filters(file)


def test_empty_keywords_include_loads_and_passes_every_job_on_keyword_check(
    tmp_path: Path,
) -> None:
    file = _write_yaml(
        tmp_path / "filters.yaml",
        """
locations:
  include: [paris]
contract_types: [internship]
keywords:
  include: []
""",
    )
    config = load_filters(file)
    assert config.keywords.include == []

    job = _make_job(
        title="Something Entirely Unrelated",
        location="Paris",
        contract_type="internship",
        description="No keyword here whatsoever.",
    )
    result = JobFilter(config).matches(job)
    assert result.passed is True
    assert result.matched_keywords == []


def test_empty_exclude_lists_are_legal(tmp_path: Path) -> None:
    file = _write_yaml(
        tmp_path / "filters.yaml",
        """
locations:
  include: [paris]
  exclude: []
contract_types: [internship]
keywords:
  include: [data]
  exclude: []
""",
    )
    config = load_filters(file)
    assert config.locations.exclude == []
    assert config.keywords.exclude == []


def test_missing_filters_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FilterConfigError):
        load_filters(tmp_path / "does-not-exist.yaml")


def test_malformed_filters_yaml_raises(tmp_path: Path) -> None:
    file = _write_yaml(tmp_path / "filters.yaml", "not: [valid: yaml: at: all")
    with pytest.raises(FilterConfigError):
        load_filters(file)


def test_non_mapping_filters_yaml_raises(tmp_path: Path) -> None:
    file = _write_yaml(tmp_path / "filters.yaml", "- just\n- a\n- list\n")
    with pytest.raises(FilterConfigError):
        load_filters(file)


# --- behaviour -------------------------------------------------------------


def test_paris_alternance_data_role_passes(paris_data_config: FilterConfig) -> None:
    job = _make_job(
        title="Alternance Data Engineer",
        location="Paris",
        contract_type="apprenticeship",
        description="Vous rejoignez l'equipe data pour un an en alternance.",
    )
    result = JobFilter(paris_data_config).matches(job)
    assert result.passed is True
    assert result.reason is None


def test_lyon_stage_passes_contract_type_but_fails_on_location_excluded(
    greenhouse_payload, greenhouse_source, paris_data_config: FilterConfig
) -> None:
    jobs = {j.external_id: j for j in greenhouse_source.parse(greenhouse_payload["jobs"])}
    job = jobs["1002"]  # Stagiaire Data Analyst, Lyon, France -- internship
    assert job.contract_type == "internship"
    assert job.location == "Lyon, France"

    result = JobFilter(paris_data_config).matches(job)
    assert result.passed is False
    assert result.reason == "location_excluded"


def test_berlin_senior_role_fails_on_contract_type_first(
    greenhouse_payload, greenhouse_source, paris_data_config: FilterConfig
) -> None:
    jobs = {j.external_id: j for j in greenhouse_source.parse(greenhouse_payload["jobs"])}
    job = jobs["1004"]  # Senior Backend Engineer, Berlin -- "other", not in include, not excluded
    assert job.contract_type == "other"

    result = JobFilter(paris_data_config).matches(job)
    assert result.passed is False
    # Must be "contract_type", not "location_not_included" -- proves
    # contract_type is checked (and short-circuits) before location.
    assert result.reason == "contract_type"


def test_no_location_entry_respects_unknown_location_policy(
    greenhouse_payload, greenhouse_source
) -> None:
    jobs = {j.external_id: j for j in greenhouse_source.parse(greenhouse_payload["jobs"])}
    job = jobs["1005"]  # Apprenti Développeur Mobile -- no location field
    assert job.location == ""

    keep_config = FilterConfig(
        locations=LocationFilterConfig(include=["paris"], unknown_location="keep"),
        contract_types=["apprenticeship"],
        keywords=KeywordFilterConfig(include=[]),
    )
    result = JobFilter(keep_config).matches(job)
    assert result.passed is True

    drop_config = FilterConfig(
        locations=LocationFilterConfig(include=["paris"], unknown_location="drop"),
        contract_types=["apprenticeship"],
        keywords=KeywordFilterConfig(include=[]),
    )
    result = JobFilter(drop_config).matches(job)
    assert result.passed is False
    assert result.reason == "location_not_included"


def test_location_present_but_neither_excluded_nor_included_fails(
    paris_data_config: FilterConfig,
) -> None:
    # Berlin isn't in locations.exclude (that's lyon/marseille/bordeaux/
    # toulouse) and isn't in locations.include either -- distinct from both
    # the location_excluded and the empty-location cases above.
    job = _make_job(location="Berlin, Germany", contract_type="internship")
    result = JobFilter(paris_data_config).matches(job)
    assert result.passed is False
    assert result.reason == "location_not_included"


def test_paris_internship_in_sales_fails_with_keyword_excluded_despite_data_present(
    paris_data_config: FilterConfig,
) -> None:
    job = _make_job(
        title="Stage Sales Data Analyst",
        location="Paris",
        contract_type="internship",
        description="Vous travaillerez sur des projets data avec l'equipe sales.",
    )
    result = JobFilter(paris_data_config).matches(job)
    assert result.passed is False
    assert result.reason == "keyword_excluded"


def test_require_all_fails_single_match_require_any_passes_same_job(
    paris_data_config: FilterConfig,
) -> None:
    job = _make_job(
        title="Ingenieur Python",
        location="Paris",
        contract_type="internship",
        description="Poste axe sur le langage python, rien d'autre de specifique.",
    )

    all_config = paris_data_config.model_copy(deep=True)
    all_config.keywords.require = "all"
    result_all = JobFilter(all_config).matches(job)
    assert result_all.passed is False
    assert result_all.reason == "keyword_not_matched"
    assert result_all.matched_keywords == ["python"]

    any_config = paris_data_config.model_copy(deep=True)
    any_config.keywords.require = "any"
    result_any = JobFilter(any_config).matches(job)
    assert result_any.passed is True


def test_accented_and_unaccented_location_both_match(paris_data_config: FilterConfig) -> None:
    accented = _make_job(location="Île-de-France", contract_type="internship")
    unaccented = _make_job(location="ile-de-france", contract_type="internship")

    # Isolate the location check: no keyword text present, so use an empty
    # keyword include to avoid the keyword step interfering.
    config = paris_data_config.model_copy(deep=True)
    config.keywords.include = []

    assert JobFilter(config).matches(accented).passed is True
    assert JobFilter(config).matches(unaccented).passed is True


def test_match_mode_word_vs_substring_has_teeth() -> None:
    job = _make_job(
        title="Financial Analyst",
        location="Paris",
        contract_type="internship",
        description="We are a financial services company.",
    )
    base = FilterConfig(
        locations=LocationFilterConfig(include=["paris"]),
        contract_types=["internship"],
        keywords=KeywordFilterConfig(include=["ia"], match_mode="word"),
    )
    word_result = JobFilter(base).matches(job)
    assert word_result.passed is False
    assert word_result.reason == "keyword_not_matched"

    substring_config = base.model_copy(deep=True)
    substring_config.keywords.match_mode = "substring"
    substring_result = JobFilter(substring_config).matches(job)
    assert substring_result.passed is True
    assert substring_result.matched_keywords == ["ia"]


def test_matched_keywords_is_in_config_order_not_job_order() -> None:
    job = _make_job(
        title="Role",
        location="Paris",
        contract_type="internship",
        # Mentions the keywords in the REVERSE of config order below.
        description="Ce poste touche a la data, au sql, et un peu de python.",
    )
    config = FilterConfig(
        locations=LocationFilterConfig(include=["paris"]),
        contract_types=["internship"],
        keywords=KeywordFilterConfig(include=["python", "sql", "data"], match_mode="word"),
    )
    result = JobFilter(config).matches(job)
    assert result.passed is True
    assert result.matched_keywords == ["python", "sql", "data"]


# --- THE PORTABILITY TEST, load bearing ------------------------------------


def test_changing_filters_yaml_changes_results_without_code_change(tmp_path: Path) -> None:
    """The whole point of filters.yaml: relocating or changing keywords is a
    yaml edit, never a code change. jobbot/filters.py must contain none of
    the strings that make these two configs behave differently."""
    config_a_yaml = """
locations:
  include: [paris, ile-de-france, "75"]
  exclude: [lyon, marseille, bordeaux, toulouse]
  match_mode: substring
  unknown_location: keep
contract_types: [internship, apprenticeship]
keywords:
  include: [data, donnees, machine learning]
  exclude: [sales, commercial, vente, recrutement]
  match_mode: word
  fields: [title, description]
  require: any
"""
    config_b_yaml = """
locations:
  include: [toulouse, "31"]
  exclude: [paris]
  match_mode: substring
  unknown_location: keep
contract_types: [internship, apprenticeship]
keywords:
  include: [finance, fintech, paiement, blockchain]
  exclude: []
  match_mode: word
  fields: [title, description]
  require: any
"""
    config_a = load_filters(_write_yaml(tmp_path / "config_a.yaml", config_a_yaml))
    config_b = load_filters(_write_yaml(tmp_path / "config_b.yaml", config_b_yaml))

    paris_data_job = _make_job(
        title="Alternance Data Engineer",
        location="Paris",
        contract_type="apprenticeship",
        description="Une alternance data au coeur de Paris.",
    )
    toulouse_fintech_job = _make_job(
        title="Stage Fintech Blockchain Analyst",
        location="Toulouse",
        contract_type="internship",
        description="Rejoignez notre equipe fintech, paiement et blockchain a Toulouse.",
    )
    jobs = [paris_data_job, toulouse_fintech_job]

    results_a = [JobFilter(config_a).matches(job).passed for job in jobs]
    results_b = [JobFilter(config_b).matches(job).passed for job in jobs]

    assert results_a == [True, False]
    assert results_b == [False, True]
    assert results_a != results_b
    assert any(results_a)
    assert any(results_b)

    filters_source = (
        Path(__file__).resolve().parent.parent / "jobbot" / "filters.py"
    ).read_text(encoding="utf-8")
    location_and_keyword_strings = [
        "paris", "ile-de-france", "toulouse", "lyon", "marseille", "bordeaux",
        "data", "donnees", "machine learning", "sales", "commercial", "vente",
        "recrutement", "finance", "fintech", "paiement", "blockchain",
    ]
    for term in location_and_keyword_strings:
        # Word-boundary, not raw substring: a raw substring check for "data"
        # would false-positive on the stdlib `dataclass` import/decorator
        # filters.py legitimately uses. Same technique as
        # test_source_integrity.py's forbidden-literal scan.
        pattern = re.compile(r"\b" + r"\s+".join(re.escape(w) for w in term.split(" ")) + r"\b", re.IGNORECASE)
        assert not pattern.search(filters_source), f"{term!r} must not be hardcoded in filters.py"
