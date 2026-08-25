"""Validates the actual config files shipped in this repo, not just tmp_path
fixtures -- a typo in filters.yaml, settings.yaml, or companies/*.yaml should
fail CI, not surface as a runtime surprise (CLAUDE.md: "Config files shipped
in the repo are validated by tests, not only tmp_path fixtures").

REPO_ROOT is resolved from __file__, never the current working directory:
these tests must find the same files regardless of where pytest is invoked
from.
"""

from __future__ import annotations

from pathlib import Path

from jobbot.config import KNOWN_ATS, load_companies
from jobbot.filters import load_filters
from jobbot.settings import load_settings

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_real_filters_yaml_loads() -> None:
    config = load_filters(REPO_ROOT / "filters.yaml")
    assert config.locations.include


def test_real_settings_yaml_loads() -> None:
    settings = load_settings(REPO_ROOT / "settings.yaml")
    assert settings.user_agent_contact


def test_real_companies_directory_loads() -> None:
    sources = load_companies(REPO_ROOT / "companies")
    assert len(sources) >= 1
    for source in sources:
        assert source.ats in KNOWN_ATS
