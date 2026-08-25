"""Greenhouse Job Board API adapter.

Greenhouse is an ATS product employers embed on their own careers page; the
public `boards-api.greenhouse.io` endpoint used here serves exactly the
postings that employer has published, first-party per CLAUDE.md rule 2.

GET https://boards-api.greenhouse.io/v1/boards/{identifier}/jobs?content=true
returns every open posting for one company's board in a single response —
`identifier` is that company's board token (see companies/hot.yaml for how to
find one from a careers page URL).
"""

from __future__ import annotations

import html
import logging
import re
from typing import Literal

import httpx

from jobbot.models import Job, normalize
from jobbot.sources.base import JobSource, SourceEmptyError, SourceError, SourceNotFoundError

logger = logging.getLogger(__name__)

BOARD_URL_TEMPLATE = "https://boards-api.greenhouse.io/v1/boards/{identifier}/jobs?content=true"
TIMEOUT_SECONDS = 15.0
MAX_ATTEMPTS = 2  # one request, one retry on 5xx/timeout


def _phrase(text: str) -> str:
    """Build a `\\s+`-joined pattern fragment from a space-separated phrase,
    word by word, so accidental whitespace differences don't break the match.
    """
    return r"\s+".join(re.escape(word) for word in text.split(" "))


# Contract-type vocabulary: language detection on the posting text, not a
# user search preference, so it's exempt from the no-hardcoded-search-terms
# guard (see CLAUDE.md rule 4 and test_no_hardcoded_search_terms).
#
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

# Title vs description signal strength (A1). A title match is authoritative
# on its own -- short and to the point, no corroboration needed. A
# description match is not: a senior role's description can casually mention
# "vous encadrerez des stagiaires et alternants" without the posting itself
# being one, so a description-only hit only counts within this many
# characters of a word that actually signals "this posting is an offer for
# a position", not merely a mention of one.
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
_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_BREAK_RE = re.compile(r"(?i)</?(p|div|li|ul|ol|br|h[1-6])\b[^>]*>")
_INLINE_SPACE_RE = re.compile(r"[ \t]+")


def _strip_html(raw: str) -> str:
    """Turn Greenhouse's HTML job content into readable plain text.

    Tags are stripped before entities are unescaped, deliberately: content
    can legitimately contain an entity-encoded "&lt;...&gt;" meant to render
    as literal angle brackets, and unescaping first would turn that into
    something that looks like a real tag and gets eaten by the tag-stripper.
    """
    if not raw:
        return ""
    text = _BLOCK_BREAK_RE.sub("\n", raw)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    lines = (_INLINE_SPACE_RE.sub(" ", line).strip() for line in text.splitlines())
    return "\n".join(line for line in lines if line)


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
    title: str, description: str
) -> Literal["internship", "apprenticeship", "other"]:
    """Classify a posting. Title is authoritative: a vocabulary hit anywhere
    in the title wins outright. Only when the title is silent does the
    description count, and only for a vocabulary hit within
    CONTRACT_CONTEXT_WINDOW_CHARS of a contract-context word -- a senior
    role's description casually mentioning "stagiaires et alternants" it
    manages must not flip the verdict.

    The bare "stage"/"stages" French-context gate (see _BARE_STAGE_PATTERN)
    is evaluated once, over the title and description combined: a short
    title ("Stage Marketing Digital") often won't carry a French stopword on
    its own even when the posting plainly is French, so the gate looks at
    the whole posting while the title/description structure above still
    governs *where* the "stage" word itself must be found.
    """
    title_text = _normalize_for_classification(title)
    description_text = _normalize_for_classification(description)
    combined_text = _normalize_for_classification(f"{title} {description}")

    allow_bare_stage = bool(_FRENCH_CONTEXT_MARKERS.search(combined_text))

    title_verdict = _vocabulary_hit(title_text, allow_bare_stage=allow_bare_stage)
    if title_verdict != "other":
        return title_verdict

    return _windowed_vocabulary_hit(description_text, allow_bare_stage=allow_bare_stage)


class GreenhouseSource(JobSource):
    name = "greenhouse"
    tier = 1
    first_party = True

    def fetch_raw(
        self, etag: str | None = None, last_modified: str | None = None
    ) -> tuple[list[dict], str | None]:
        url = BOARD_URL_TEMPLATE.format(identifier=self.identifier)
        headers = {"User-Agent": self.user_agent}

        response: httpx.Response | None = None
        error: Exception | None = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = self.client.get(url, headers=headers, timeout=TIMEOUT_SECONDS)
            except httpx.TimeoutException as exc:
                error = exc
                logger.warning(
                    "greenhouse: timeout fetching %s for %s (attempt %d/%d)",
                    url, self.company_name, attempt, MAX_ATTEMPTS,
                )
                continue

            if response.status_code >= 500:
                error = SourceError(
                    f"greenhouse: {self.company_name} ({url}) returned "
                    f"HTTP {response.status_code}"
                )
                logger.warning(
                    "greenhouse: HTTP %d fetching %s for %s (attempt %d/%d)",
                    response.status_code, url, self.company_name, attempt, MAX_ATTEMPTS,
                )
                continue

            error = None
            break

        if error is not None:
            raise SourceError(
                f"greenhouse: failed to fetch {url} for {self.company_name} "
                f"after {MAX_ATTEMPTS} attempts"
            ) from error

        # Never let an httpx exception (e.g. HTTPStatusError from
        # raise_for_status()) escape this method -- every failure mode here
        # is one of our own SourceError subclasses.
        if response.status_code == 404:
            raise SourceNotFoundError(
                f"greenhouse: board not found for {self.company_name} "
                f"({self.identifier}): {url} returned 404"
            )
        if response.status_code >= 400:
            raise SourceError(
                f"greenhouse: {self.company_name} ({url}) returned "
                f"HTTP {response.status_code}"
            )

        payload = response.json()
        jobs = payload.get("jobs", [])

        if not jobs:
            raise SourceEmptyError(
                f"greenhouse: {self.company_name} ({self.identifier}) returned zero jobs"
            )

        return jobs, None

    def parse(self, raw: list[dict]) -> list[Job]:
        jobs: list[Job] = []
        for entry in raw:
            try:
                jobs.append(self._parse_entry(entry))
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning(
                    "greenhouse: skipping malformed entry for %s: %s", self.company_name, exc
                )
        return jobs

    def _parse_entry(self, entry: dict) -> Job:
        external_id = entry["id"]
        title = entry["title"]
        url = entry["absolute_url"]
        location = ((entry.get("location") or {}).get("name")) or ""
        description = _strip_html(entry.get("content") or "")
        contract_type = classify_contract_type(title, description)

        return Job(
            company=self.company_name,
            title=title,
            location=location,
            contract_type=contract_type,
            url=url,
            posted_at=entry.get("updated_at"),
            description=description,
            source=self.name,
            external_id=str(external_id),
        )
