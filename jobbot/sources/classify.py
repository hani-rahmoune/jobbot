"""Contract-type classification, shared by every ATS adapter.

Extracted from greenhouse.py (M6 A1): the vocabulary and the title/
description/employment_hint weighting logic don't depend on anything
Greenhouse-specific, and every new adapter needs the same classifier.

Contract-type vocabulary: language detection on the posting text, not a
user search preference, so this whole file is exempt from the
no-hardcoded-search-terms guard (see CLAUDE.md rule 4 and
test_no_hardcoded_search_terms's EXEMPT_FILES).
"""

from __future__ import annotations

import re
from typing import Literal

from jobbot.models import normalize


def _phrase(text: str) -> str:
    """Build a `\\s+`-joined pattern fragment from a space-separated phrase,
    word by word, so accidental whitespace differences don't break the match.
    """
    return r"\s+".join(re.escape(word) for word in text.split(" "))


# One regex per concept, with an explicit, closed set of suffixes -- never a
# blanket `\w*`, which would make "intern" match "internal"/"international"
# (both start with the literal 6 letters "intern"). Every alternative here is
# spelled out and closed off with its own trailing \b.
_APPRENTICESHIP_PATTERNS = {
    "alternance": re.compile(r"\balternances?\b"),
    "alternant": re.compile(r"\balternante?s?\b"),
    "apprenti": re.compile(r"\bapprentie?s?\b"),
    "apprentissage": re.compile(r"\bapprentissages?\b"),
    "apprentice_en": re.compile(r"\bapprentice(?:ships?|s)?\b"),
    "contrat_pro": re.compile(
        r"\b(?:"
        rf"{_phrase('contrat de professionnalisation')}"
        rf"|{_phrase('contrat de pro')}"
        rf"|{_phrase('contrat pro')}"
        r")\b"
    ),
}

# Unambiguous on their own -- no additional context required.
_UNAMBIGUOUS_INTERNSHIP_PATTERNS = {
    "stagiaire": re.compile(r"\bstagiaires?\b"),
    "intern_en": re.compile(r"\bintern(?:ships?|s)?\b"),
    "pfe": re.compile(r"\bpfe\b"),
    "projet_fin_etudes": re.compile(rf"\bprojets?\s+{_phrase('de fin d etudes')}\b"),
    "cesure": re.compile(rf"\b(?:{_phrase('annee de cesure')}|cesure)\b"),
}

# Bare "stage"/"stages" is real French internship vocabulary, but it's also
# ordinary English business writing ("Series B stage company", "Growth Stage
# Manager"). It only counts as internship vocabulary when the posting as a
# whole shows French context -- see classify_contract_type().
_BARE_STAGE_PATTERN = re.compile(r"\bstages?\b")
_FRENCH_CONTEXT_MARKERS = re.compile(
    r"\b(?:de|du|des|le|la|les|en|pour|mois|" + _phrase("au sein") + r")\b"
)

# Title/employment_hint vs description signal strength. A title (or a
# structured employment_hint field, e.g. an ATS's own commitment/
# employmentType value) match is authoritative on its own -- short,
# deliberate, and not free-text prose. A description match is not: a senior
# role's description can casually mention "vous encadrerez des stagiaires et
# alternants" without the posting itself being one, so a description-only
# hit only counts within this many characters of a word that actually
# signals "this posting is an offer for a position", not merely a mention
# of one.
CONTRACT_CONTEXT_WINDOW_CHARS = 60
_CONTRACT_CONTEXT_WORDS = (
    "contrat", "duree", "poste", "offre", "recherchons", "recrutons",
    "cherchons", "mission", "debut", "rentree", "looking for", "seeking",
    "position", "hiring", "we are",
)
_CONTRACT_CONTEXT_PATTERN = re.compile(
    r"\b(?:" + "|".join(_phrase(word) for word in _CONTRACT_CONTEXT_WORDS) + r")\b"
)

_APOSTROPHE_RE = re.compile(r"[’']")


def _normalize_for_classification(text: str) -> str:
    return _APOSTROPHE_RE.sub(" ", normalize(text))


def _vocabulary_hit(
    text: str, *, allow_bare_stage: bool
) -> Literal["internship", "apprenticeship", "other"]:
    """Presence-anywhere vocabulary check, no proximity requirement. Apprenticeship
    wins if both match. `allow_bare_stage` gates the ambiguous bare "stage"/"stages"
    forms (see the French-context note on _BARE_STAGE_PATTERN above)."""
    if any(pattern.search(text) for pattern in _APPRENTICESHIP_PATTERNS.values()):
        return "apprenticeship"
    if any(pattern.search(text) for pattern in _UNAMBIGUOUS_INTERNSHIP_PATTERNS.values()):
        return "internship"
    if allow_bare_stage and _BARE_STAGE_PATTERN.search(text):
        return "internship"
    return "other"


def _near_contract_context(text: str, match: re.Match[str]) -> bool:
    start = max(0, match.start() - CONTRACT_CONTEXT_WINDOW_CHARS)
    end = match.end() + CONTRACT_CONTEXT_WINDOW_CHARS
    return _CONTRACT_CONTEXT_PATTERN.search(text[start:end]) is not None


def _windowed_vocabulary_hit(
    text: str, *, allow_bare_stage: bool
) -> Literal["internship", "apprenticeship", "other"]:
    """Like _vocabulary_hit, but a match only counts within
    CONTRACT_CONTEXT_WINDOW_CHARS of a contract-context word -- the
    description's weaker signal (see classify_contract_type)."""
    for pattern in _APPRENTICESHIP_PATTERNS.values():
        if any(_near_contract_context(text, m) for m in pattern.finditer(text)):
            return "apprenticeship"
    for pattern in _UNAMBIGUOUS_INTERNSHIP_PATTERNS.values():
        if any(_near_contract_context(text, m) for m in pattern.finditer(text)):
            return "internship"
    if allow_bare_stage and any(
        _near_contract_context(text, m) for m in _BARE_STAGE_PATTERN.finditer(text)
    ):
        return "internship"
    return "other"


def classify_contract_type(
    title: str, description: str, employment_hint: str = ""
) -> Literal["internship", "apprenticeship", "other"]:
    """Classify a posting. Title is authoritative: a vocabulary hit anywhere
    in the title wins outright.

    `employment_hint` (M6 A3) is a separate, structured signal some ATS
    expose (Lever's `categories.commitment`, Ashby's `employmentType`, a
    JSON-LD `employmentType`) holding a controlled value like "Intern" or
    "Alternance". It's checked with the same unconditional authority as the
    title -- it's a deliberate, structured field the employer chose from a
    fixed set of values, not free-text prose where a bare mention could be
    an aside about who the role manages. It must never be faked into the
    title string itself; this parameter is how it reaches the classifier.

    Only when both the title and the hint are silent does the description
    count, and only for a vocabulary hit within CONTRACT_CONTEXT_WINDOW_CHARS
    of a contract-context word -- a senior role's description casually
    mentioning "stagiaires et alternants" it manages must not flip the
    verdict.

    The bare "stage"/"stages" French-context gate (see _BARE_STAGE_PATTERN)
    is evaluated once, over the title, description, and hint combined: a
    short title ("Stage Marketing Digital") often won't carry a French
    stopword on its own even when the posting plainly is French, so the gate
    looks at the whole posting while the structure above still governs
    *where* the "stage" word itself must be found.
    """
    title_text = _normalize_for_classification(title)
    description_text = _normalize_for_classification(description)
    hint_text = _normalize_for_classification(employment_hint)
    combined_text = _normalize_for_classification(f"{title} {description} {employment_hint}")

    allow_bare_stage = bool(_FRENCH_CONTEXT_MARKERS.search(combined_text))

    title_verdict = _vocabulary_hit(title_text, allow_bare_stage=allow_bare_stage)
    if title_verdict != "other":
        return title_verdict

    hint_verdict = _vocabulary_hit(hint_text, allow_bare_stage=allow_bare_stage)
    if hint_verdict != "other":
        return hint_verdict

    return _windowed_vocabulary_hit(description_text, allow_bare_stage=allow_bare_stage)
