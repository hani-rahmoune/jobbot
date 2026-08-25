from __future__ import annotations

from pathlib import Path

import pytest

from jobbot.config import ConfigError, load_companies


def _write(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def test_disabled_entries_are_filtered_out(tmp_path: Path) -> None:
    file = _write(
        tmp_path / "companies.yaml",
        """
- name: Enabled Co
  ats: greenhouse
  identifier: enabled-co
- name: Disabled Co
  ats: greenhouse
  identifier: disabled-co
  enabled: false
""",
    )
    sources = load_companies(file)
    assert {s.name for s in sources} == {"Enabled Co"}


def test_unknown_ats_raises(tmp_path: Path) -> None:
    file = _write(
        tmp_path / "companies.yaml",
        """
- name: Some Co
  ats: lever
  identifier: some-co
""",
    )
    with pytest.raises(ConfigError):
        load_companies(file)


def test_directory_loading_merges_multiple_yaml_files(tmp_path: Path) -> None:
    _write(tmp_path / "a.yaml", "- name: Co A\n  ats: greenhouse\n  identifier: co-a\n")
    _write(tmp_path / "b.yaml", "- name: Co B\n  ats: greenhouse\n  identifier: co-b\n")

    sources = load_companies(tmp_path)
    assert {s.name for s in sources} == {"Co A", "Co B"}


def test_duplicate_triple_across_files_raises(tmp_path: Path) -> None:
    _write(tmp_path / "a.yaml", "- name: Co A\n  ats: greenhouse\n  identifier: co-a\n")
    _write(tmp_path / "b.yaml", "- name: Co A\n  ats: greenhouse\n  identifier: co-a\n")

    with pytest.raises(ConfigError):
        load_companies(tmp_path)


def test_tier_defaults_to_warm_tags_default_to_empty(tmp_path: Path) -> None:
    file = _write(
        tmp_path / "companies.yaml",
        "- name: Plain Co\n  ats: greenhouse\n  identifier: plain-co\n",
    )
    sources = load_companies(file)
    assert sources[0].tier == "warm"
    assert sources[0].tags == []


def test_explicit_tier_and_tags_are_kept(tmp_path: Path) -> None:
    file = _write(
        tmp_path / "companies.yaml",
        (
            "- name: Tagged Co\n"
            "  ats: greenhouse\n"
            "  identifier: tagged-co\n"
            "  tier: cold\n"
            "  tags: [foo, bar]\n"
        ),
    )
    sources = load_companies(file)
    assert sources[0].tier == "cold"
    assert sources[0].tags == ["foo", "bar"]


def test_missing_path_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_companies(tmp_path / "does-not-exist.yaml")


def test_malformed_yaml_raises(tmp_path: Path) -> None:
    file = _write(tmp_path / "companies.yaml", "not: [valid: yaml: at: all")
    with pytest.raises(ConfigError):
        load_companies(file)


def test_empty_directory_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_companies(tmp_path)


def test_empty_yaml_file_yields_no_entries(tmp_path: Path) -> None:
    file = _write(tmp_path / "companies.yaml", "")
    assert load_companies(file) == []


def test_non_list_top_level_yaml_raises(tmp_path: Path) -> None:
    file = _write(tmp_path / "companies.yaml", "name: not a list\nats: greenhouse\n")
    with pytest.raises(ConfigError):
        load_companies(file)


def test_entry_missing_required_field_raises(tmp_path: Path) -> None:
    file = _write(tmp_path / "companies.yaml", "- name: Incomplete Co\n  ats: greenhouse\n")
    with pytest.raises(ConfigError):
        load_companies(file)  # missing `identifier`


def test_non_mapping_entry_raises(tmp_path: Path) -> None:
    file = _write(tmp_path / "companies.yaml", "- just a string, not a mapping\n")
    with pytest.raises(ConfigError):
        load_companies(file)
