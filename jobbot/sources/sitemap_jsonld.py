"""Sitemap-discovered, per-page JSON-LD adapter.

M9 Part B3/D3: Phenom People (Orange, Thales) was rejected because its
career-site search widget (`POST {domain}/widgets/search-jobs`) is
genuinely session- and CSRF-token-bound -- confirmed by replicating the
handshake (GET the page, extract the token from the resulting cookie,
resend it as a header) and still getting redirected. That widget stays
unusable. But the search page isn't the only thing the site serves: every
Phenom career site checked publishes a real, robots.txt-listed sitemap
index (`Sitemap:` lines in robots.txt), and every individual job page it
points to is genuinely server-rendered with a complete schema.org
`JobPosting` JSON-LD block -- confirmed live against both Thales
(careers.thalesgroup.com/fr/fr) and Orange (orange.jobs/fr/fr), ~500 real
job URLs each in just the first of several per-locale sub-sitemaps.

This is deliberately a SEPARATE adapter from jsonld.py, not a mode of it:
jsonld.py's contract is one identifier = one listing page containing
however many JobPosting blocks that one page holds, fetched once.
Here there is no listing page with embedded postings at all -- the sitemap
lists hundreds of individual job page URLs, each carrying exactly one
JobPosting. Reusing jsonld.py's single-URL contract wouldn't reach more
than one posting; this adapter's job is specifically the discover-many-
URLs-then-fetch-each-one step jsonld.py was never built for.

Fetching every discovered job page unconditionally would multiply the
request count by the posting count -- Thales alone lists 500+ per locale,
which at Workday's own measured ~1s/request would blow the entire poll's
time budget on one employer. So the candidate set is narrowed before any
individual page is fetched, the same volume-control role search_terms
plays server-side for the other adapters, just applied client-side here
since the sitemap has no search parameter of its own -- in three layers,
each one a fallback for when the one before finds nothing (M11 Part A;
never a gate, per M11's report -- an empty match at any layer falls
through to the next rather than rejecting the source):

1. `search_terms` itself (the user's own configured preference, settings.
   yaml, CLAUDE.md rule 4), matched against the URL slug -- narrowest and
   most precise when it actually matches. Confirmed live on Thales/Orange/
   Accor/Rexel: cuts hundreds-to-thousands of candidates down to single or
   low double digits.
2. DEFAULT_SLUG_VOCABULARY (M11 A1): a broader, hardcoded-with-override
   vocabulary, deliberately DISTINCT from search_terms -- it's URL
   structure/vocabulary recognition, not a user search preference (the
   same CLAUDE.md rule 4 exemption DEFAULT_JOB_PATH_MARKERS already has).
   This exists because M10's bug was real: Geodis and Manitou both have
   confirmed-live JobPosting JSON-LD and confirmed-live sitemaps, but their
   sitemaps use English-locale slugs ("Intern", "Graduate", "Trainee") that
   search_terms' own narrow, mostly-French word list (and "internship" as
   a whole word, not "intern") never matched -- M10 read that as "nothing
   here" and silently returned zero postings for both, forever, on every
   poll, without ever trying a page. A broader vocabulary closes that gap
   for the next employer with a different locale/wording too, not just
   these two specifically.
3. An evenly-spread SAMPLE (M11 A2) of `sample_size` URLs across the full
   candidate set, only when BOTH of the above match nothing -- spread via
   a stride, not "the first N", so a sitemap that happens to list one
   locale/category first doesn't bias the sample toward it. This is the
   layer that actually fixes Geodis/Manitou: every fetched page's REAL
   title and description (not just its slug) still gets the normal
   classify_contract_type() treatment downstream, which knows far more
   vocabulary than any URL-slug heuristic could.

`page_cap` (M11 A4, raised from a flat 60 to 150, and instance-overridable
-- Accor alone had 86 real search_terms-matched candidates, uncomfortably
close to the old flat cap) bounds whichever layer actually produced
candidates, logged if it truncates. Which of the three layers fired, and
how many candidates each one produced, is always logged (M11 A3) so a
silent "zero postings" is never actually silent in the log.

The identifier is the employer's sitemap index URL (from robots.txt's own
`Sitemap:` line), e.g. "https://careers.thalesgroup.com/fr/fr/sitemap_index.xml".
"""

from __future__ import annotations

import html
import json
import logging
import re
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from jobbot.models import Job, normalize
from jobbot.sources.base import JobSource, SourceError, SourceNotFoundError
from jobbot.sources.classify import classify_contract_type
from jobbot.sources.html_text import strip_html
from jobbot.sources.robots import RobotsCache

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 15.0
MAX_ATTEMPTS = 2  # one request, one retry on 5xx/timeout, per page fetched
# M11 A4: raised from 60 -- Accor alone had 86 real search_terms-matched
# candidates, uncomfortably close to the old flat cap. Instance-overridable
# (see __init__'s page_cap) rather than a bare module constant, since one
# fixed number was never going to fit every employer's board size.
DEFAULT_PAGE_CAP = 150

# M9d: structural URL-shape markers, not a user search term (CLAUDE.md rule
# 4 exempts this the same way it exempts classify.py's contract-type
# vocabulary -- these are path segments a job-board template uses, not a
# location or keyword preference). "/job/" and "/jobs/" were confirmed live
# on Thales/Orange (Phenom); the French ones cover the pattern used by
# employers whose own site is in French even without a named ATS vendor
# behind it. Overridable per instance for an employer whose URL scheme
# doesn't match any default -- see __init__'s job_path_markers.
DEFAULT_JOB_PATH_MARKERS = (
    "/job/", "/jobs/", "/offre/", "/offres/", "/emploi/", "/poste/", "/career/", "/vacancy/",
)
# A path segment that's mostly digits (a requisition/posting id) is also a
# strong job-page signal even with no recognizable word in the path at all.
_NUMERIC_PATH_SEGMENT_RE = re.compile(r"/\d{3,}(?:[/?#-]|$)")

# M11 A1: a slug-relevance vocabulary, deliberately separate from
# search_terms -- see the module docstring's numbered volume-control list
# for why this exists and why it's broader than search_terms on purpose
# (English-locale words like "intern"/"trainee"/"graduate" a French-focused
# search_terms list would never itself contain). URL structure, not a user
# search preference, so CLAUDE.md rule 4 exempts it the same way it exempts
# DEFAULT_JOB_PATH_MARKERS and classify.py's own vocabulary -- overridable
# per instance via __init__'s slug_vocabulary for an employer whose real
# wording still isn't covered by this default set.
DEFAULT_SLUG_VOCABULARY = (
    "stage", "stagiaire", "alternance", "alternant", "apprenti", "apprentissage",
    "intern", "internship", "trainee", "graduate", "apprentice", "apprenticeship",
    "vie", "student", "etudiant", "werkstudent",
)
# M11 A2: how many URLs the last-resort evenly-spread sample fetches when
# neither search_terms nor the slug vocabulary above matched anything at
# all -- confirmed live to be exactly the failure mode for Geodis and
# Manitou (real sitemaps, real JobPosting JSON-LD, English-locale slugs
# neither layer matched).
DEFAULT_SAMPLE_SIZE = 40

_LOC_RE = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.IGNORECASE | re.DOTALL)
_LD_JSON_BLOCK_RE = re.compile(
    r"<script[^>]*type\s*=\s*[\"']?application/ld\+json[\"']?[^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)
_ADDRESS_FIELDS = ("addressLocality", "addressRegion", "addressCountry")


class SitemapJsonLdSource(JobSource):
    name = "sitemap_jsonld"
    tier = 1
    first_party = True

    def __init__(
        self,
        identifier: str,
        company_name: str,
        client: httpx.Client,
        user_agent: str,
        search_terms: list[str] | None = None,
        job_path_markers: list[str] | None = None,
        slug_vocabulary: list[str] | None = None,
        sample_size: int = DEFAULT_SAMPLE_SIZE,
        page_cap: int = DEFAULT_PAGE_CAP,
    ) -> None:
        parsed = urlsplit(identifier)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError(
                f"sitemap_jsonld: identifier must be a full https URL to a sitemap "
                f"(index or plain), got {identifier!r}"
            )
        super().__init__(identifier, company_name, client, user_agent)
        self._robots = RobotsCache(client, user_agent)
        # `is not None` (not truthiness) throughout: an explicitly-passed empty
        # list is a deliberate "disable this layer" override and must be
        # honoured, not silently replaced by the default because `[]` is
        # falsy. M11 A1 needs this for slug_vocabulary specifically (tests
        # pass slug_vocabulary=[] to opt out of vocabulary-based narrowing
        # and fall straight through to sampling); job_path_markers and
        # search_terms get the same treatment for consistency.
        self.search_terms = list(search_terms) if search_terms is not None else []
        self.job_path_markers = (
            list(job_path_markers)
            if job_path_markers is not None
            else list(DEFAULT_JOB_PATH_MARKERS)
        )
        self.slug_vocabulary = (
            list(slug_vocabulary) if slug_vocabulary is not None else list(DEFAULT_SLUG_VOCABULARY)
        )
        self.sample_size = sample_size
        self.page_cap = page_cap

    def _looks_like_a_job_url(self, url: str) -> bool:
        lowered = url.lower()
        return (
            any(marker in lowered for marker in self.job_path_markers)
            or bool(_NUMERIC_PATH_SEGMENT_RE.search(url))
        )

    # --- fetch_raw() -------------------------------------------------------

    def fetch_raw(
        self, etag: str | None = None, last_modified: str | None = None
    ) -> tuple[list[dict], str | None]:
        if not self._robots.allowed(self.identifier):
            raise SourceError(
                f"sitemap_jsonld: robots.txt disallows fetching {self.identifier} "
                f"for {self.company_name}"
            )

        top_level_urls = _extract_locs(self._fetch_xml(self.identifier), self.identifier)
        sub_sitemaps = [u for u in top_level_urls if u.endswith(".xml")]

        # A one-level sitemap (no index) means the top-level document's own
        # <loc> entries already are the page URLs.
        job_candidate_urls = list(top_level_urls) if not sub_sitemaps else []
        for sitemap_url in sub_sitemaps:
            job_candidate_urls.extend(_extract_locs(self._fetch_xml(sitemap_url), sitemap_url))

        job_urls = [u for u in job_candidate_urls if self._looks_like_a_job_url(u)]

        urls_to_fetch, path_used = self._select_urls_to_fetch(job_urls)
        logger.info(
            "sitemap_jsonld: %s (%s) used the %r path: %d candidate(s) selected out of "
            "%d job-like URL(s) total",
            self.company_name, self.identifier, path_used, len(urls_to_fetch), len(job_urls),
        )

        postings: list[dict] = []
        for job_url in urls_to_fetch:
            postings.extend(self._fetch_job_posting(job_url))

        # M8b: zero results is a valid, non-failing outcome (see
        # run.process_source()) -- a genuinely quiet board, or every
        # candidate URL's page having no JobPosting block, is not an error.
        return postings, None

    def _select_urls_to_fetch(self, job_urls: list[str]) -> tuple[list[str], str]:
        """M11 Part A: three fallback layers, in order, each one tried only
        because the one before it matched nothing -- see the module
        docstring's numbered list for the full reasoning behind each one and
        the real bug (Geodis, Manitou) this exists to fix. Returns
        (urls_to_fetch, which_path_name) for the caller to log (A3) --
        "search_terms", "slug_vocabulary", or "sampled".
        """
        if self.search_terms:
            normalized_terms = [normalize(t) for t in self.search_terms]
            matched = [
                u for u in job_urls if any(t in normalize(u) for t in normalized_terms)
            ]
            if matched:
                return self._apply_page_cap(matched, "search_terms"), "search_terms"

        vocabulary_matched = [
            u for u in job_urls if any(term in normalize(u) for term in self.slug_vocabulary)
        ]
        if vocabulary_matched:
            return self._apply_page_cap(vocabulary_matched, "slug_vocabulary"), "slug_vocabulary"

        # A2: the slug filter is an optimization, never a gate -- neither
        # layer above matching anything is not itself a reason to return
        # zero postings. An evenly-spread sample of the full candidate set
        # is fetched instead, so classify_contract_type() gets a real shot
        # at each one's actual title and description, not just its slug.
        sampled = _evenly_spread_sample(job_urls, self.sample_size)
        return sampled, "sampled"

    def _apply_page_cap(self, urls: list[str], path_name: str) -> list[str]:
        if len(urls) > self.page_cap:
            logger.warning(
                "sitemap_jsonld: %s (%s) hit the %d-page cap on the %r path "
                "(%d candidates matched), more postings may exist",
                self.company_name, self.identifier, self.page_cap, path_name, len(urls),
            )
            return urls[: self.page_cap]
        return urls

    def _fetch_xml(self, url: str) -> str:
        """One sitemap document (index or leaf), with the same
        retry-once-on-5xx/timeout contract every other adapter follows."""
        headers = {"User-Agent": self.user_agent}
        response: httpx.Response | None = None
        error: Exception | None = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = self.client.get(url, headers=headers, timeout=TIMEOUT_SECONDS)
            except httpx.TimeoutException as exc:
                error = exc
                logger.warning(
                    "sitemap_jsonld: timeout fetching %s for %s (attempt %d/%d)",
                    url, self.company_name, attempt, MAX_ATTEMPTS,
                )
                continue

            if response.status_code >= 500:
                error = SourceError(
                    f"sitemap_jsonld: {self.company_name} ({url}) "
                    f"returned HTTP {response.status_code}"
                )
                logger.warning(
                    "sitemap_jsonld: HTTP %d fetching %s for %s (attempt %d/%d)",
                    response.status_code, url, self.company_name, attempt, MAX_ATTEMPTS,
                )
                continue

            error = None
            break

        if error is not None:
            raise SourceError(
                f"sitemap_jsonld: failed to fetch {url} for {self.company_name} "
                f"after {MAX_ATTEMPTS} attempts"
            ) from error

        # Never let an httpx exception escape this method -- every failure
        # mode here is one of our own SourceError subclasses.
        if response.status_code == 404:
            raise SourceNotFoundError(
                f"sitemap_jsonld: not found for {self.company_name} ({url}): returned 404"
            )
        if response.status_code >= 400:
            raise SourceError(
                f"sitemap_jsonld: {self.company_name} ({url}) "
                f"returned HTTP {response.status_code}"
            )

        return response.text

    def _fetch_job_posting(self, job_url: str) -> list[dict]:
        """A malformed/unreachable individual job page must not crash the
        whole batch -- skipped with a logged warning, same spirit as
        parse()'s per-entry error handling, just one step earlier since the
        failure here is a fetch, not a field-mapping problem."""
        try:
            html_text = self._fetch_xml(job_url)
        except SourceError as exc:
            logger.warning(
                "sitemap_jsonld: skipping unreachable job page for %s: %s",
                self.company_name, exc,
            )
            return []

        postings = []
        for match in _LD_JSON_BLOCK_RE.finditer(html_text):
            try:
                parsed = json.loads(match.group(1).strip())
            except json.JSONDecodeError:
                continue
            for entry in _job_postings_from_block(parsed):
                postings.append({"url": job_url, "posting": entry})
        return postings

    # --- parse() -------------------------------------------------------

    def parse(self, raw: list[dict]) -> list[Job]:
        jobs: list[Job] = []
        for entry in raw:
            try:
                jobs.append(self._parse_entry(entry))
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning(
                    "sitemap_jsonld: skipping malformed entry for %s: %s",
                    self.company_name, exc,
                )
        return jobs

    def _parse_entry(self, entry: dict) -> Job:
        page_url = entry["url"]
        posting = entry["posting"]

        title = posting["title"]
        url = posting.get("url") or page_url
        raw_identifier = _extract_identifier_value(posting.get("identifier"))
        external_id = raw_identifier or page_url
        location = _extract_location(posting.get("jobLocation"))
        employment_hint = posting.get("employmentType") or ""
        if isinstance(employment_hint, list):
            employment_hint = " ".join(str(value) for value in employment_hint)
        # Confirmed live on both Thales and Orange: this vendor's own
        # JobPosting JSON-LD carries `description` as HTML that's ALREADY
        # entity-escaped ("&lt;p&gt;" as literal text, not a real tag) --
        # unlike jsonld.py's fixtures, which see real "<p>" tags directly.
        # strip_html() only unescapes entities AFTER stripping tags (so a
        # genuinely-escaped "&lt;script&gt;" in prose text can't be revived
        # into a live tag mid-strip), so without unescaping first here, this
        # vendor's real tags would never be recognized as tags at all and
        # would leak into the stored description as literal "<p>" text.
        description = strip_html(html.unescape(posting.get("description") or ""))
        contract_type = classify_contract_type(title, description, str(employment_hint))

        return Job(
            company=self.company_name,
            title=title,
            location=location,
            contract_type=contract_type,
            url=url,
            posted_at=posting.get("datePosted"),
            description=description,
            source=self.name,
            external_id=external_id,
        )


def _evenly_spread_sample(urls: list[str], sample_size: int) -> list[str]:
    """M11 A2: a stride-based sample across the FULL list, not "the first
    N" -- picking from throughout the candidate set is what actually fixes
    Geodis/Manitou (a real sitemap can list one locale or category before
    another; the first N could easily all be the same one)."""
    if len(urls) <= sample_size or sample_size <= 0:
        return list(urls)
    stride = len(urls) / sample_size
    return [urls[int(i * stride)] for i in range(sample_size)]


def _job_postings_from_block(parsed: Any) -> list[dict]:
    if isinstance(parsed, dict) and "@graph" in parsed:
        candidates = parsed["@graph"]
    elif isinstance(parsed, list):
        candidates = parsed
    else:
        candidates = [parsed]

    result = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        item_type = item.get("@type")
        types = item_type if isinstance(item_type, list) else [item_type]
        if "JobPosting" in types:
            result.append(item)
    return result


def _extract_locs(xml_text: str, base_url: str) -> list[str]:
    """The sitemap protocol requires every <loc> to already be an absolute
    URL, but real-world sitemaps don't always comply (confirmed live: a
    relative <loc> crashes deep inside urllib if handed to httpx as if it
    were absolute, rather than failing cleanly) -- every entry is resolved
    against `base_url` (a no-op for one that's already absolute) and
    dropped if it still isn't a fetchable absolute http(s) URL afterward.
    """
    resolved_urls = []
    for loc in _LOC_RE.findall(xml_text):
        resolved = urljoin(base_url, loc)
        parsed = urlsplit(resolved)
        if parsed.scheme in ("http", "https") and parsed.netloc:
            resolved_urls.append(resolved)
    return resolved_urls


def _extract_identifier_value(value: Any) -> str:
    """schema.org's `identifier` is either a plain string or a
    PropertyValue object ({"@type": "PropertyValue", "value": ...})."""
    if isinstance(value, dict):
        return str(value.get("value") or value.get("name") or "")
    if value is None:
        return ""
    return str(value)


def _extract_location(job_location: Any) -> str:
    """jobLocation may be a single Place object or a list of them (take the
    first). Build the display string from whichever of locality/region/
    country are actually present, most specific first."""
    if isinstance(job_location, list):
        job_location = job_location[0] if job_location else None
    if not isinstance(job_location, dict):
        return ""

    address = job_location.get("address")
    if isinstance(address, str):
        return address
    if not isinstance(address, dict):
        return ""

    parts = [address.get(field) for field in _ADDRESS_FIELDS]
    return ", ".join(part for part in parts if part)
