from __future__ import annotations

from pathlib import Path

import pytest

from jobbot.settings import SettingsError, load_settings


def _write_settings(tmp_path: Path, content: str) -> Path:
    file = tmp_path / "settings.yaml"
    file.write_text(content, encoding="utf-8")
    return file


def test_yaml_values_load(tmp_path: Path) -> None:
    file = _write_settings(
        tmp_path,
        """
poll_interval_minutes: 45
seed_mode: true
user_agent_contact: "ops@example.com"
fail_on_empty_source: false
allowed_tier2_sources: ["usajobs"]
""",
    )
    settings = load_settings(file)
    assert settings.poll_interval_minutes == 45
    assert settings.seed_mode is True
    assert settings.user_agent_contact == "ops@example.com"
    assert settings.fail_on_empty_source is False
    assert settings.allowed_tier2_sources == ["usajobs"]


def test_defaults_apply_when_only_the_required_field_is_given(tmp_path: Path) -> None:
    file = _write_settings(tmp_path, 'user_agent_contact: "ops@example.com"\n')
    settings = load_settings(file)
    assert settings.poll_interval_minutes == 20
    assert settings.seed_mode is False
    assert settings.fail_on_empty_source is True
    assert settings.allowed_tier2_sources == []
    assert settings.repost_window_days == 180
    assert settings.resurrection_window_days == 7
    assert settings.ghost_stale_after_days == 90
    assert settings.state_db_path == "jobbot_state.db"


def test_jobbot_env_vars_override_yaml_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    file = _write_settings(
        tmp_path,
        """
poll_interval_minutes: 45
seed_mode: false
user_agent_contact: "ops@example.com"
fail_on_empty_source: true
""",
    )
    monkeypatch.setenv("JOBBOT_POLL_INTERVAL_MINUTES", "5")
    monkeypatch.setenv("JOBBOT_SEED_MODE", "true")
    monkeypatch.setenv("JOBBOT_USER_AGENT_CONTACT", "override@example.com")
    monkeypatch.setenv("JOBBOT_ALLOWED_TIER2_SOURCES", "[usajobs, another]")

    settings = load_settings(file)
    assert settings.poll_interval_minutes == 5
    assert settings.seed_mode is True
    assert settings.user_agent_contact == "override@example.com"
    assert settings.fail_on_empty_source is True  # untouched by env, keeps yaml value
    assert settings.allowed_tier2_sources == ["usajobs", "another"]


def test_env_var_that_is_not_valid_yaml_is_used_as_a_raw_string(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file = _write_settings(tmp_path, 'user_agent_contact: "ops@example.com"\n')
    # "[unterminated" is not parseable YAML (unbalanced flow sequence), so it
    # must fall back to being used verbatim as the string value.
    monkeypatch.setenv("JOBBOT_USER_AGENT_CONTACT", "[unterminated")

    settings = load_settings(file)
    assert settings.user_agent_contact == "[unterminated"


def test_missing_settings_file_raises(tmp_path: Path) -> None:
    with pytest.raises(SettingsError):
        load_settings(tmp_path / "does-not-exist.yaml")


def test_malformed_settings_yaml_raises(tmp_path: Path) -> None:
    file = _write_settings(tmp_path, "not: [valid: yaml: at: all")
    with pytest.raises(SettingsError):
        load_settings(file)


def test_non_mapping_settings_yaml_raises(tmp_path: Path) -> None:
    file = _write_settings(tmp_path, "- just\n- a\n- list\n")
    with pytest.raises(SettingsError):
        load_settings(file)


def test_missing_required_field_raises(tmp_path: Path) -> None:
    file = _write_settings(tmp_path, "poll_interval_minutes: 10\n")
    with pytest.raises(SettingsError):
        load_settings(file)
