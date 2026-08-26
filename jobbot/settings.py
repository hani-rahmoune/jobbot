"""Runtime settings: settings.yaml, overridable by JOBBOT_-prefixed env vars.

Env vars win over yaml. An env var's raw string is parsed with `yaml.safe_load`
so `JOBBOT_SEED_MODE=true` becomes a bool and `JOBBOT_POLL_INTERVAL_MINUTES=30`
becomes an int, matching what the equivalent yaml value would have been; if
that parse fails (or produces something odd), the raw string is used as-is and
left for pydantic to validate.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, ValidationError

ENV_PREFIX = "JOBBOT_"
DEFAULT_SETTINGS_PATH = Path("settings.yaml")


class SettingsError(Exception):
    """Raised when settings.yaml is missing, malformed, or fails validation."""


class Settings(BaseModel):
    poll_interval_minutes: int = 20
    seed_mode: bool = False
    user_agent_contact: str
    fail_on_empty_source: bool = True
    allowed_tier2_sources: list[str] = Field(default_factory=list)
    # M3 store windows -- see jobbot/store.py's JobVerdict for what each one
    # governs. JobStore takes these as constructor args; wiring them from
    # here into a real JobStore is the future orchestrator's (M5) job.
    repost_window_days: int = 180
    resurrection_window_days: int = 7
    ghost_stale_after_days: int = 90
    # M5: where the orchestrator's JobStore lives. See CLAUDE.md's M9
    # deployment note about the .gitignore exception this file needs.
    state_db_path: str = "jobbot_state.db"


def _env_overrides() -> dict[str, object]:
    overrides: dict[str, object] = {}
    for field_name in Settings.model_fields:
        env_key = f"{ENV_PREFIX}{field_name.upper()}"
        if env_key not in os.environ:
            continue
        raw_value = os.environ[env_key]
        try:
            overrides[field_name] = yaml.safe_load(raw_value)
        except yaml.YAMLError:
            overrides[field_name] = raw_value
    return overrides


def load_settings(path: Path = DEFAULT_SETTINGS_PATH) -> Settings:
    path = Path(path)
    if not path.exists():
        raise SettingsError(f"Settings file not found: {path}")

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise SettingsError(f"Malformed YAML in {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise SettingsError(
            f"{path} must contain a YAML mapping, got {type(raw).__name__}"
        )

    raw.update(_env_overrides())

    try:
        return Settings(**raw)
    except ValidationError as exc:
        raise SettingsError(f"Invalid settings: {exc}") from exc


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings singleton for whatever constructs the shared httpx
    Client and JobSource instances (the future M5 orchestrator) rather than
    caring about where settings.yaml lives. Reads from DEFAULT_SETTINGS_PATH.

    Adapters must NOT call this themselves: JobSource takes `user_agent` as
    an injected constructor argument instead, so sources/*.py stays free of
    any dependency on jobbot.settings.

    test_settings.py exercises `load_settings()` directly with explicit,
    isolated paths instead of this cache.
    """
    return load_settings(DEFAULT_SETTINGS_PATH)
