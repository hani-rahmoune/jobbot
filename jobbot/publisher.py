"""Discord webhook publisher.

Same transport discipline as sources/base.py: this module never constructs
its own httpx.Client, never reads settings.yaml or the environment, and never
touches the state store. The webhook URL and the client/User-Agent are all
injected by the caller (M5's orchestrator); that's what keeps every test here
offline and makes M5 the single place real config gets wired up.

Publishing and marking-published are deliberately two different modules with
no dependency from this one to the other: DiscordPublisher has no store
reference at all, so it is structurally impossible for a failed send to be
mistaken for a successful one here. The orchestrator calls store.mark_published()
itself, and only after publish() reports the job actually went out (a
confirmed 2xx) -- see test_publisher_never_marks_published_on_a_failed_send.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from jobbot.models import Job

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 15.0
MAX_RATE_LIMIT_ATTEMPTS = 3  # B3: max 3 attempts total on repeated 429s

MAX_EMBEDS_PER_MESSAGE = 10
EMBED_TITLE_MAX = 256
EMBED_DESCRIPTION_MAX = 4096
EMBED_FIELD_VALUE_MAX = 1024
EMBED_TOTAL_PAYLOAD_MAX = 6000
DESCRIPTION_PREVIEW_CHARS = 300
MESSAGE_CONTENT_MAX = 2000  # Discord's plain-message content limit, for publish_error

# Arbitrary but distinct; not user-facing config, so not a filters.yaml concern.
_EMBED_COLOR_BY_CONTRACT_TYPE = {
    "internship": 0x5865F2,   # blurple
    "apprenticeship": 0x57F287,  # green
    "other": 0x99AAB5,        # grey, shouldn't normally reach here
}

_MULTI_BLANK_LINES_RE = re.compile(r"\n{2,}")


class PublishError(Exception):
    """Raised when a send to Discord fails and won't be retried further."""

    def __init__(self, message: str, attempts: int = 1) -> None:
        super().__init__(message)
        self.attempts = attempts


class RateLimitedError(PublishError):
    """Raised after exhausting all rate-limit retries (repeated 429s)."""


@dataclass
class PublishResult:
    sent: int
    failed: list[str] = field(default_factory=list)
    requests_made: int = 0


def _truncate(text: str, limit: int) -> str:
    """Truncate to at most `limit` characters, ellipsis included in the
    count -- never erroring, never silently exceeding a Discord limit."""
    if len(text) <= limit:
        return text
    if limit <= 0:
        return ""
    return text[: limit - 1] + "…"


def _clean_description(description: str) -> str:
    return _MULTI_BLANK_LINES_RE.sub("\n", description.strip())


def _embed_char_count(embed: dict) -> int:
    """Discord's own total-payload rule: title + description + every
    field's name and value + footer text, summed across all embeds in a
    message. `url` and `color` don't count."""
    total = len(embed.get("title", "")) + len(embed.get("description", ""))
    total += len(embed.get("footer", {}).get("text", ""))
    for f in embed.get("fields", []):
        total += len(f.get("name", "")) + len(f.get("value", ""))
    return total


def build_embed(job: Job, matched_keywords: list[str]) -> dict:
    """Pure: no network, no clock. Every Discord length limit is enforced
    here by truncation, never by raising."""
    description_preview = _truncate(_clean_description(job.description), DESCRIPTION_PREVIEW_CHARS)
    description = _truncate(
        description_preview or "No description provided.", EMBED_DESCRIPTION_MAX
    )

    fields = [
        {"name": "Company", "value": _truncate(job.company, EMBED_FIELD_VALUE_MAX), "inline": True},
        {
            "name": "Location",
            "value": _truncate(job.location or "Not specified", EMBED_FIELD_VALUE_MAX),
            "inline": True,
        },
        {
            "name": "Contract type",
            "value": _truncate(job.contract_type, EMBED_FIELD_VALUE_MAX),
            "inline": True,
        },
    ]
    if matched_keywords:
        fields.append({
            "name": "Keywords",
            "value": _truncate(", ".join(matched_keywords), EMBED_FIELD_VALUE_MAX),
            "inline": False,
        })

    # Source date is display-only, explicitly labelled as the company's own
    # claim -- never presented as our freshness signal (CLAUDE.md rule 5).
    footer_text = f"Source: {job.source}"
    if job.posted_at is not None:
        footer_text += f" · Date reported by the company: {job.posted_at.date().isoformat()}"

    return {
        "title": _truncate(job.title, EMBED_TITLE_MAX),
        "url": str(job.url),
        "description": description,
        "color": _EMBED_COLOR_BY_CONTRACT_TYPE.get(
            job.contract_type, _EMBED_COLOR_BY_CONTRACT_TYPE["other"]
        ),
        "fields": fields,
        "footer": {"text": footer_text},
    }


def _retry_after_seconds(response: httpx.Response) -> float:
    try:
        body = response.json()
        return float(body.get("retry_after", 1.0))
    except (ValueError, TypeError):
        return 1.0


class DiscordPublisher:
    def __init__(
        self,
        client: httpx.Client,
        user_agent: str,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = client
        self._user_agent = user_agent
        self._sleep = sleep

    def publish(
        self, webhook_url: str, jobs: list[tuple[Job, list[str]]], dry_run: bool = False
    ) -> PublishResult:
        chunks = self._build_chunks(jobs)

        if dry_run:
            return PublishResult(sent=sum(len(chunk) for chunk in chunks), failed=[], requests_made=0)

        sent = 0
        failed: list[str] = []
        requests_made = 0

        for chunk in chunks:
            embeds = [embed for _job, embed in chunk]
            job_ids = [job.job_id for job, _embed in chunk]
            try:
                requests_made += self._post(webhook_url, {"embeds": embeds})
                sent += len(chunk)
            except PublishError as exc:
                requests_made += exc.attempts
                failed.extend(job_ids)
                logger.warning(
                    "publisher: failed to send %d job(s) to Discord: %s", len(chunk), exc
                )

        return PublishResult(sent=sent, failed=failed, requests_made=requests_made)

    def publish_error(self, webhook_url: str, message: str) -> None:
        """Best-effort: never raises, even on a failed send. Failing to
        report a failure must not itself crash the run."""
        payload = {"content": _truncate(message, MESSAGE_CONTENT_MAX)}
        try:
            response = self._client.post(
                webhook_url, json=payload, headers=self._headers(), timeout=TIMEOUT_SECONDS
            )
            if response.status_code >= 300:
                logger.warning(
                    "publisher: error webhook returned HTTP %d", response.status_code
                )
        except Exception:
            logger.warning("publisher: failed to post error message to Discord", exc_info=True)

    def _build_chunks(
        self, jobs: list[tuple[Job, list[str]]]
    ) -> list[list[tuple[Job, dict]]]:
        chunks: list[list[tuple[Job, dict]]] = []
        current: list[tuple[Job, dict]] = []
        current_chars = 0

        for job, matched_keywords in jobs:
            embed = build_embed(job, matched_keywords)
            embed_chars = _embed_char_count(embed)
            if current and (
                len(current) >= MAX_EMBEDS_PER_MESSAGE
                or current_chars + embed_chars > EMBED_TOTAL_PAYLOAD_MAX
            ):
                chunks.append(current)
                current = []
                current_chars = 0
            current.append((job, embed))
            current_chars += embed_chars

        if current:
            chunks.append(current)
        return chunks

    def _headers(self) -> dict[str, str]:
        return {"User-Agent": self._user_agent}

    def _post(self, webhook_url: str, payload: dict) -> int:
        """Returns the number of HTTP requests actually made. Raises
        PublishError (or RateLimitedError) with `.attempts` set on
        unrecoverable failure."""
        attempts = 0
        rate_limit_attempts = 0
        server_error_retried = False

        while True:
            attempts += 1
            response = self._client.post(
                webhook_url, json=payload, headers=self._headers(), timeout=TIMEOUT_SECONDS
            )

            if response.status_code < 300:
                return attempts

            if response.status_code == 429:
                rate_limit_attempts += 1
                if rate_limit_attempts >= MAX_RATE_LIMIT_ATTEMPTS:
                    raise RateLimitedError(
                        f"Discord rate-limited us on all {rate_limit_attempts} attempts",
                        attempts=attempts,
                    )
                self._sleep(_retry_after_seconds(response))
                continue

            if response.status_code >= 500:
                if server_error_retried:
                    raise PublishError(
                        f"Discord returned HTTP {response.status_code} after one retry",
                        attempts=attempts,
                    )
                server_error_retried = True
                continue

            raise PublishError(
                f"Discord returned HTTP {response.status_code}: {response.text}",
                attempts=attempts,
            )
