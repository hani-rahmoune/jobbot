"""Loading and validating the company source list (companies/*.yaml).

`tier` and `tags` are captured and validated now but unused until M9
(discovery/prioritization). Keep them typed correctly so that config written
today doesn't need to be rewritten later.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, ValidationError

# The set of ATS adapters this codebase actually implements. Grown one entry
# at a time as adapters land (M1: greenhouse; M6: lever, ashby, jsonld --
# Taleez was investigated for M6 but requires an API key, see companies
# README/M6 report, so no "taleez" adapter exists to register here). An
# identifier not in this set is a config typo or an ATS we haven't built,
# either way it's an error, not a silent skip.
KNOWN_ATS = frozenset({"greenhouse", "lever", "ashby", "jsonld"})


class ConfigError(Exception):
    """Raised for any problem loading or validating companies/*.yaml."""


class CompanySource(BaseModel):
    name: str
    ats: str
    identifier: str
    enabled: bool = True
    tier: Literal["hot", "warm", "cold"] = "warm"
    tags: list[str] = Field(default_factory=list)


def _read_yaml_list(path: Path) -> list[dict]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"Could not read {path}: {exc}") from exc

    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"Malformed YAML in {path}: {exc}") from exc

    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ConfigError(
            f"{path} must contain a YAML list of company entries, got {type(raw).__name__}"
        )
    return raw


def load_companies(path: Path) -> list[CompanySource]:
    """Load company sources from a single yaml file or a directory of them.

    When `path` is a directory, every `*.yaml` file inside it is merged, in
    filename order. Entries with `enabled: false` are parsed and validated
    (so a disabled entry with a typo still fails loudly) but filtered out of
    the returned list.
    """
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"Company config path does not exist: {path}")

    if path.is_dir():
        files = sorted(path.glob("*.yaml"))
        if not files:
            raise ConfigError(f"No *.yaml files found in directory: {path}")
    else:
        files = [path]

    sources: list[CompanySource] = []
    seen: dict[tuple[str, str, str], Path] = {}

    for file in files:
        for raw_entry in _read_yaml_list(file):
            if not isinstance(raw_entry, dict):
                raise ConfigError(
                    f"Invalid entry in {file}: expected a mapping, got {raw_entry!r}"
                )
            try:
                entry = CompanySource(**raw_entry)
            except ValidationError as exc:
                raise ConfigError(f"Invalid company entry in {file}: {exc}") from exc

            if entry.ats not in KNOWN_ATS:
                raise ConfigError(
                    f"Unknown ats {entry.ats!r} for company {entry.name!r} in {file}. "
                    f"Known values: {sorted(KNOWN_ATS)}"
                )

            triple = (entry.name, entry.ats, entry.identifier)
            if triple in seen:
                raise ConfigError(
                    f"Duplicate company source {triple} in {file} "
                    f"(already defined in {seen[triple]})"
                )
            seen[triple] = file

            if entry.enabled:
                sources.append(entry)

    return sources
