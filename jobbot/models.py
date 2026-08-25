"""Core domain model.

`Job` is the shape every source adapter must parse into. `job_id` identifies a
single posting for dedup; `content_fingerprint` identifies the underlying
opening so a repost under a new requisition id is still recognized as the same
job. See the field/property docstrings below for exact rules.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, ValidationInfo, computed_field, field_validator

_WHITESPACE_RE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Fold text for comparison: NFKD-decompose, drop combining marks (accents),
    lowercase, collapse runs of whitespace to a single space, and strip.

    Used everywhere text needs to be compared regardless of accent, case, or
    incidental whitespace differences (job_id, content_fingerprint, and any
    future dedup/matching logic).
    """
    decomposed = unicodedata.normalize("NFKD", text)
    without_marks = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    collapsed = _WHITESPACE_RE.sub(" ", without_marks.lower())
    return collapsed.strip()


class Job(BaseModel):
    company: str
    title: str
    location: str
    contract_type: Literal["internship", "apprenticeship", "other"]
    url: HttpUrl
    posted_at: datetime | None = Field(
        default=None,
        description=(
            "Date reported by the source (e.g. Greenhouse's `updated_at`). "
            "Display only, and must be labelled in the UI as reported by the "
            "company. Never used to decide freshness: companies bump this on "
            "typo edits, reopen closed requisitions, and run evergreen "
            "postings for a year. Freshness is decided by our own "
            "first_seen_at (added in M3), never by this field."
        ),
    )
    description: str = ""
    source: str
    external_id: str | None = None

    @field_validator("title", "company")
    @classmethod
    def _reject_blank(cls, value: str, info: ValidationInfo) -> str:
        if not value.strip():
            raise ValueError(f"{info.field_name} must not be empty or whitespace-only")
        return value

    @computed_field
    @property
    def job_id(self) -> str:
        """Stable identifier for a single posting, sha256 hex.

        Prefers `source:external_id` when the source gave us one, since that's
        the most precise handle we have. Falls back to a normalized
        company/title/location/url key. Deterministic and stable across
        processes: no randomness, no wall-clock input.
        """
        if self.external_id:
            raw = f"{self.source}:{self.external_id}"
        else:
            raw = (
                f"{self.company}:{normalize(self.title)}:"
                f"{normalize(self.location)}:{self.url}"
            )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @computed_field
    @property
    def content_fingerprint(self) -> str:
        """Identifies the underlying opening, independent of requisition id.

        Sha256 hex of normalized company/title/location plus the first 600
        characters of the normalized description. The 600 character cut is
        deliberate: companies edit trailing boilerplate (legal notices,
        diversity statements) without the job itself changing, and reposting
        the same opening under a new requisition id must still fingerprint
        the same.
        """
        raw = (
            f"{normalize(self.company)}:{normalize(self.title)}:"
            f"{normalize(self.location)}:{normalize(self.description[:600])}"
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()
