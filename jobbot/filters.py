"""filters.yaml engine: decides whether a Job matches the user's current
location / contract-type / keyword preferences.

Nothing in this file names a city, region, department code, or keyword --
those live entirely in filters.yaml (CLAUDE.md rule 4). The user relocates or
changes what they're searching for by editing that file, never this one.

Department-code matching ("75", "44") is a plain substring check against the
normalized location string, same as any other include/exclude term under
match_mode: substring -- deliberately imprecise. "75" also matches inside a
postal code like "75001" (word-boundary matching would NOT, since digits on
both sides of "75" in "75001" are word characters with no boundary between
them) and, in principle, inside any other digit run that happens to contain
those two characters. That's the tradeoff of substring mode; switch to
match_mode: word for stricter (but postal-code-blind) matching.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, get_args

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator

from jobbot.models import Job, normalize

MatchMode = Literal["substring", "word"]
UnknownLocationPolicy = Literal["keep", "drop"]
RequireMode = Literal["any", "all"]

# Derived from Job.contract_type itself so this file never hardcodes the
# list and can't drift out of sync with models.py.
_KNOWN_CONTRACT_TYPES: frozenset[str] = frozenset(
    get_args(Job.model_fields["contract_type"].annotation)
)


class FilterConfigError(Exception):
    """Raised when filters.yaml is missing, malformed, or fails validation."""


class LocationOverride(BaseModel):
    """A per-contract-type override of specific `locations` fields (M19 Part
    A). Any field left unset (None) inherits the base `LocationFilterConfig`
    value for that field -- this is what lets, say, apprenticeship widen
    `include` to a nationwide city list while inheriting `match_mode` and
    `unknown_location` unchanged, and lets it clear an inherited `exclude`
    explicitly with `exclude: []` rather than being stuck with exclusions
    that made sense for the narrower default scope but not this one."""

    include: list[str] | None = Field(default=None, min_length=1)
    exclude: list[str] | None = None
    match_mode: MatchMode | None = None
    unknown_location: UnknownLocationPolicy | None = None


class LocationFilterConfig(BaseModel):
    include: list[str] = Field(min_length=1)
    exclude: list[str] = Field(default_factory=list)
    match_mode: MatchMode = "substring"
    unknown_location: UnknownLocationPolicy = "keep"
    # Per-contract-type overrides (M19 Part A). Empty by default, which is
    # exactly what makes an old-style filters.yaml with no `by_contract_type`
    # key behave identically to before: every contract type falls through to
    # the fields above, unscoped, same as it always has.
    by_contract_type: dict[str, LocationOverride] = Field(default_factory=dict)

    @field_validator("by_contract_type")
    @classmethod
    def _by_contract_type_known(cls, value: dict[str, LocationOverride]) -> dict[str, LocationOverride]:
        unknown = [c for c in value if c not in _KNOWN_CONTRACT_TYPES]
        if unknown:
            raise ValueError(
                f"unknown contract_type(s) {unknown} in locations.by_contract_type; "
                f"known: {sorted(_KNOWN_CONTRACT_TYPES)}"
            )
        return value


class KeywordFilterConfig(BaseModel):
    include: list[str] = Field(default_factory=list)  # empty = no keyword filtering
    exclude: list[str] = Field(default_factory=list)
    match_mode: MatchMode = "word"
    # min_length=1: unlike include, an empty fields list isn't "don't filter",
    # it's "search nowhere" -- every job would silently fail every keyword
    # check it's subject to. That's a config mistake, not a preference.
    fields: list[Literal["title", "description"]] = Field(
        default_factory=lambda: ["title", "description"], min_length=1
    )
    require: RequireMode = "any"


class FilterConfig(BaseModel):
    locations: LocationFilterConfig
    # min_length=1: an empty contract_types isn't "don't filter", it's
    # "nothing ever matches" (`job.contract_type in []` is always False) --
    # a config mistake, not a preference. Same reasoning as fields above.
    contract_types: list[str] = Field(min_length=1)
    keywords: KeywordFilterConfig

    @field_validator("contract_types")
    @classmethod
    def _contract_types_known(cls, value: list[str]) -> list[str]:
        unknown = [c for c in value if c not in _KNOWN_CONTRACT_TYPES]
        if unknown:
            raise ValueError(
                f"unknown contract_type(s) {unknown}; known: {sorted(_KNOWN_CONTRACT_TYPES)}"
            )
        return value


def load_filters(path: Path) -> FilterConfig:
    path = Path(path)
    if not path.exists():
        raise FilterConfigError(f"Filters file not found: {path}")

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise FilterConfigError(f"Malformed YAML in {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise FilterConfigError(f"{path} must contain a YAML mapping, got {type(raw).__name__}")

    try:
        return FilterConfig(**raw)
    except ValidationError as exc:
        raise FilterConfigError(f"Invalid filters config: {exc}") from exc


@dataclass
class FilterResult:
    passed: bool
    reason: str | None
    matched_keywords: list[str]


def _term_matches(haystack: str, term: str, mode: MatchMode) -> bool:
    """Is `term` present in the already-normalized `haystack`, per `mode`?"""
    needle = normalize(term)
    if mode == "substring":
        return needle in haystack
    pattern = r"\b" + r"\s+".join(re.escape(word) for word in needle.split(" ")) + r"\b"
    return re.search(pattern, haystack) is not None


def _any_term_matches(haystack: str, terms: list[str], mode: MatchMode) -> bool:
    return any(_term_matches(haystack, term, mode) for term in terms)


@dataclass(frozen=True)
class _EffectiveLocation:
    """The location rule that actually applies to one job, after resolving
    any `by_contract_type` override against the base `locations` config."""

    include: list[str]
    exclude: list[str]
    match_mode: MatchMode
    unknown_location: UnknownLocationPolicy


def _effective_location(locations: LocationFilterConfig, contract_type: str) -> _EffectiveLocation:
    override = locations.by_contract_type.get(contract_type)
    if override is None:
        return _EffectiveLocation(
            include=locations.include,
            exclude=locations.exclude,
            match_mode=locations.match_mode,
            unknown_location=locations.unknown_location,
        )
    return _EffectiveLocation(
        include=override.include if override.include is not None else locations.include,
        exclude=override.exclude if override.exclude is not None else locations.exclude,
        match_mode=override.match_mode if override.match_mode is not None else locations.match_mode,
        unknown_location=(
            override.unknown_location
            if override.unknown_location is not None
            else locations.unknown_location
        ),
    )


class JobFilter:
    def __init__(self, config: FilterConfig) -> None:
        self.config = config

    def matches(self, job: Job) -> FilterResult:
        # 1. contract_type
        if job.contract_type not in self.config.contract_types:
            return FilterResult(passed=False, reason="contract_type", matched_keywords=[])

        # 2. location: exclude, then include, using whichever rule applies to
        # THIS job's contract_type (the base `locations` config, or its
        # `by_contract_type` override when one is configured -- see
        # _effective_location). Skipped entirely (neither excluded nor
        # required-to-be-included) when location is empty and
        # unknown_location is "keep"; "drop" fails it outright instead.
        loc_cfg = _effective_location(self.config.locations, job.contract_type)
        location = normalize(job.location)
        if not location:
            if loc_cfg.unknown_location == "drop":
                return FilterResult(
                    passed=False, reason="location_not_included", matched_keywords=[]
                )
        else:
            if _any_term_matches(location, loc_cfg.exclude, loc_cfg.match_mode):
                return FilterResult(passed=False, reason="location_excluded", matched_keywords=[])
            if not _any_term_matches(location, loc_cfg.include, loc_cfg.match_mode):
                return FilterResult(
                    passed=False, reason="location_not_included", matched_keywords=[]
                )

        # 3. keywords: exclude, then include.
        kw_cfg = self.config.keywords
        haystack = self._keyword_haystack(job)

        if _any_term_matches(haystack, kw_cfg.exclude, kw_cfg.match_mode):
            return FilterResult(passed=False, reason="keyword_excluded", matched_keywords=[])

        # In config order, not job order.
        matched = [kw for kw in kw_cfg.include if _term_matches(haystack, kw, kw_cfg.match_mode)]

        if kw_cfg.include:
            satisfied = bool(matched) if kw_cfg.require == "any" else len(matched) == len(
                kw_cfg.include
            )
            if not satisfied:
                return FilterResult(
                    passed=False, reason="keyword_not_matched", matched_keywords=matched
                )

        return FilterResult(passed=True, reason=None, matched_keywords=matched)

    def _keyword_haystack(self, job: Job) -> str:
        parts = []
        if "title" in self.config.keywords.fields:
            parts.append(job.title)
        if "description" in self.config.keywords.fields:
            parts.append(job.description)
        return normalize(" ".join(parts))
