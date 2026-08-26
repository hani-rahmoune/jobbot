"""Load-bearing per CLAUDE.md. Do not delete, skip, xfail, or weaken these.
If one fails, the code is wrong, not the test.
"""

from __future__ import annotations

import re
from pathlib import Path

from jobbot.settings import Settings

# Importing every adapter module registers its JobSource subclass(es) via
# JobSource.__init_subclass__ (see jobbot/sources/base.py). Add new adapter
# imports here as they land.
from jobbot.sources import ashby, greenhouse, jsonld, lever  # noqa: F401
from jobbot.sources.base import registered_sources

JOBBOT_ROOT = Path(__file__).resolve().parent.parent / "jobbot"

FORBIDDEN_LITERALS = (
    "paris",
    "nantes",
    "ile-de-france",
    "rennes",
    "data",
    "machine learning",
    "artificial intelligence",
    # Added for M2 (filters.yaml engine): jobbot/filters.py must be just as
    # free of hardcoded locations/keywords as the rest of jobbot/.
    "toulouse",
    "bordeaux",
    "lyon",
    "marseille",
    "remote",
    "teletravail",
    "python",
    "sql",
    "fintech",
)

# Contract-type vocabulary is language detection, not a user search
# preference (CLAUDE.md rule 4's explicit exemption). It lives in
# classify.py (M6 A1), shared by every adapter -- greenhouse.py itself no
# longer contains any of it.
EXEMPT_FILES = {"classify.py"}


def _forbidden_pattern(literal: str) -> re.Pattern[str]:
    # Word-boundary match, not raw substring: a naive substring scan for
    # "data" would also flag the stdlib `unicodedata` module (required to
    # implement models.normalize()'s NFKD accent-stripping) and any
    # unrelated word containing "data" as a fragment. Word boundaries keep
    # the check aimed at what rule 4 actually forbids -- a hardcoded filter
    # keyword or place name used verbatim -- without punishing incidental
    # English vocabulary.
    #
    # Multi-word literals are built by splitting on spaces and joining with
    # \s+, word by word (same technique jobbot/sources/greenhouse.py uses
    # for its own multi-word contract-type phrases), rather than patching up
    # re.escape()'s output after the fact.
    words = literal.split(" ")
    pattern = r"\s+".join(re.escape(word) for word in words)
    return re.compile(rf"\b{pattern}\b", re.IGNORECASE)


_FORBIDDEN_PATTERNS = {literal: _forbidden_pattern(literal) for literal in FORBIDDEN_LITERALS}


def test_source_integrity() -> None:
    """Every registered source must be tier 1 and first-party, unless its
    `name` is explicitly allow-listed via settings.allowed_tier2_sources
    (empty by default)."""
    sources = registered_sources()
    assert sources, "no JobSource subclasses registered -- check adapter imports above"

    allowed_tier2 = Settings.model_fields["allowed_tier2_sources"].default_factory()
    assert allowed_tier2 == []

    for source_cls in sources:
        if source_cls.name in allowed_tier2:
            continue
        assert source_cls.tier == 1, f"{source_cls.name} must be tier 1 (got {source_cls.tier})"
        assert source_cls.first_party is True, f"{source_cls.name} must be first_party"


def test_no_hardcoded_search_terms() -> None:
    violations: list[str] = []

    for path in sorted(JOBBOT_ROOT.rglob("*.py")):
        if path.name in EXEMPT_FILES:
            continue
        text = path.read_text(encoding="utf-8")
        for literal, pattern in _FORBIDDEN_PATTERNS.items():
            if pattern.search(text):
                violations.append(f"{path}: contains forbidden literal {literal!r}")

    assert not violations, "\n".join(violations)
