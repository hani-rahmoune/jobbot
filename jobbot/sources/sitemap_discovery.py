"""Shared sitemap traversal and job-URL volume control.

Extracted from sitemap_jsonld.py during M12 Part C so successfactors.py
doesn't grow a drifting copy of the exact same logic. Both adapters discover
their job pages the same way -- a sitemap index (or a plain sitemap with no
index) points to leaf sitemaps, whose <loc> entries are individual job page
URLs -- and both need the candidate set narrowed before fetching every single
one, since a real employer's board can list thousands of URLs. What differs
between the two adapters is what happens to EACH selected URL once fetched
(schema.org JSON-LD extraction for one, lenient itemprop extraction for the
other); that part stays in each adapter's own module.

The narrowing is M11 Part A's three-layer fallback, each layer tried only
because the one before it matched nothing -- never a gate, an empty match at
any layer falls through to the next rather than the source going quiet:

1. `search_terms` itself (the user's own configured preference, settings.
   yaml, CLAUDE.md rule 4), matched against the URL slug -- narrowest and
   most precise when it actually matches.
2. DEFAULT_SLUG_VOCABULARY: a broader, hardcoded-with-override vocabulary,
   deliberately DISTINCT from search_terms -- it's URL structure/vocabulary
   recognition, not a user search preference (the same CLAUDE.md rule 4
   exemption DEFAULT_JOB_PATH_MARKERS already has). Confirmed live necessary
   for employers whose sitemap uses English-locale slugs ("Intern",
   "Graduate", "Trainee") that a French-focused search_terms list would
   never match on its own.
3. An evenly-spread SAMPLE of `sample_size` URLs across the full candidate
   set, only when both of the above match nothing -- spread via a stride,
   not "the first N", so a sitemap that happens to list one locale/category
   first doesn't bias the sample toward it.

`page_cap` bounds whichever layer actually produced candidates, logged if it
truncates. Which of the three layers fired, and how many candidates it
produced, is always logged so a silent "zero postings" is never actually
silent.

M14 Part C adds a fourth, narrower step layered ON TOP of whichever of the
above actually matched: `locations` (the user's own filters.yaml
`locations.include` list, threaded down from run.py the same way
search_terms is -- CLAUDE.md rule 4, never hardcoded here) is matched
against the same URL slug. Accor's board was the concrete motivator: 86
search_terms-matched candidates, of which only 43 were ever French, the
other 43 fetched (at real per-page cost) only to be discarded downstream by
JobFilter once their location was known. Matching the location terms
against the slug BEFORE the fetch, when the slug happens to carry a city
name, avoids ever fetching the other half. Exactly like the slug vocabulary
layer, this is a fallback refinement, not a gate: if narrowing by location
would leave zero candidates, the un-narrowed set is kept instead, since not
every employer puts a city in the slug (Thales, same problem shape, doesn't).
See `_refine_by_location()`.

Also M14 Part C: `looks_like_a_job_url()` now takes an optional deny-list
(`DEFAULT_NON_JOB_PATH_MARKERS`) alongside its existing allow-list --
Accor's own sitemap listed a `/blogs/` marketing post whose slug happened to
contain the word "apprenticeship", which every layer above then dutifully
treated as a real job candidate. A path that's obviously editorial content
is excluded up front regardless of which allow-list marker also happens to
match.

See sitemap_jsonld.py's own module docstring for the full M11 Part A
narrative (the Geodis/Manitou bug this exists to fix) -- not repeated here.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urljoin, urlsplit

import httpx

from jobbot.models import normalize
from jobbot.sources.base import SourceError, SourceNotFoundError

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 15.0
MAX_ATTEMPTS = 2  # one request, one retry on 5xx/timeout, per page fetched

# M11 A4: raised from a flat 60 -- Accor alone had 86 real search_terms-
# matched candidates, uncomfortably close to a flat cap. Instance-overridable
# on both adapters rather than a bare module constant, since one fixed
# number was never going to fit every employer's board size.
DEFAULT_PAGE_CAP = 150

# M9d: structural URL-shape markers, not a user search term (CLAUDE.md rule 4
# exempts this the same way it exempts classify.py's contract-type
# vocabulary -- these are path segments a job-board template uses, not a
# location or keyword preference).
DEFAULT_JOB_PATH_MARKERS = (
    "/job/", "/jobs/", "/offre/", "/offres/", "/emploi/", "/poste/", "/career/", "/vacancy/",
)
# A path segment that's mostly digits (a requisition/posting id) is also a
# strong job-page signal even with no recognizable word in the path at all.
_NUMERIC_PATH_SEGMENT_RE = re.compile(r"/\d{3,}(?:[/?#-]|$)")

# M14 Part C: structural URL-shape deny-list, the mirror image of
# DEFAULT_JOB_PATH_MARKERS above and exempt from CLAUDE.md rule 4 for the
# same reason -- editorial-content path segments a job-board template uses,
# not a location or keyword preference. Confirmed live necessary for Accor,
# whose sitemap includes a `/blogs/` marketing post that otherwise matches
# the slug vocabulary purely because its title contains "apprenticeship".
DEFAULT_NON_JOB_PATH_MARKERS = ("/blog/", "/blogs/", "/news/", "/press/")

# M11 A1: a slug-relevance vocabulary, deliberately separate from
# search_terms -- URL structure, not a user search preference, so CLAUDE.md
# rule 4 exempts it the same way it exempts DEFAULT_JOB_PATH_MARKERS and
# classify.py's own vocabulary.
DEFAULT_SLUG_VOCABULARY = (
    "stage", "stagiaire", "alternance", "alternant", "apprenti", "apprentissage",
    "intern", "internship", "trainee", "graduate", "apprentice", "apprenticeship",
    "vie", "student", "etudiant", "werkstudent",
)
# M11 A2: how many URLs the last-resort evenly-spread sample fetches when
# neither search_terms nor the slug vocabulary above matched anything at all.
DEFAULT_SAMPLE_SIZE = 40

_LOC_RE = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.IGNORECASE | re.DOTALL)


def looks_like_a_job_url(
    url: str,
    job_path_markers: list[str],
    non_job_path_markers: list[str] | None = None,
) -> bool:
    """`non_job_path_markers` defaults to DEFAULT_NON_JOB_PATH_MARKERS rather
    than an empty list -- the Accor /blogs/ bug this exists to fix should
    stay fixed for every caller, not just ones that opt in. The deny-list
    always wins: a URL matching both an allow marker and a deny marker
    (Accor's /blogs/ apprenticeship post matched the slug vocabulary too) is
    not a job URL.
    """
    if non_job_path_markers is None:
        non_job_path_markers = list(DEFAULT_NON_JOB_PATH_MARKERS)
    lowered = url.lower()
    if any(marker in lowered for marker in non_job_path_markers):
        return False
    return (
        any(marker in lowered for marker in job_path_markers)
        or bool(_NUMERIC_PATH_SEGMENT_RE.search(url))
    )


# M15 Part B: matches a URL's TRAILING path segment when it's purely
# numeric (3+ digits) -- the canonical "requisition ID" position for
# ".../JobDetail/{title-slug}/{id}"-shaped URLs. Anchored to end-of-string,
# unlike _NUMERIC_PATH_SEGMENT_RE above (which matches anywhere, for the
# "is this a job URL at all" question) -- an ID elsewhere in the path
# (Thales' ".../job/R0313776/Ingenieur-..." has its ID BEFORE the title,
# and prefixed with a letter, so it doesn't match here at all) is a
# different URL shape entirely and must not be deduplicated by this.
_TRAILING_NUMERIC_ID_RE = re.compile(r"/(\d{3,})/?$")


def _dedupe_by_trailing_numeric_id(urls: list[str]) -> list[str]:
    """Keeps the first occurrence (document order) of each distinct trailing
    numeric ID, dropping the rest. Confirmed live necessary for
    TotalEnergies: its sitemap lists every posting once PER LOCALE MIRROR
    (en_US, fr_FR, es_ES, de_DE, nl_NL, pt_BR -- six differently-prefixed
    URLs, the same numeric requisition ID each time), which would otherwise
    burn most of a page cap re-rendering/re-fetching the exact same posting
    under a different locale prefix. A URL with no trailing numeric ID at
    all (some employers' slugs carry none) is never deduplicated against
    anything -- always kept.
    """
    seen_ids: set[str] = set()
    deduped: list[str] = []
    for url in urls:
        match = _TRAILING_NUMERIC_ID_RE.search(url)
        if match is None:
            deduped.append(url)
            continue
        job_id = match.group(1)
        if job_id in seen_ids:
            continue
        seen_ids.add(job_id)
        deduped.append(url)
    return deduped


def evenly_spread_sample(urls: list[str], sample_size: int) -> list[str]:
    """A stride-based sample across the FULL list, not "the first N" --
    picking from throughout the candidate set is what actually fixes a
    sitemap that lists one locale or category before another, where "the
    first N" could easily all be the same one."""
    if len(urls) <= sample_size or sample_size <= 0:
        return list(urls)
    stride = len(urls) / sample_size
    return [urls[int(i * stride)] for i in range(sample_size)]


def extract_locs(xml_text: str, base_url: str) -> list[str]:
    """The sitemap protocol requires every <loc> to already be an absolute
    URL, but real-world sitemaps don't always comply (a relative <loc>
    crashes deep inside urllib if handed to httpx as if it were absolute,
    rather than failing cleanly) -- every entry is resolved against
    `base_url` (a no-op for one that's already absolute) and dropped if it
    still isn't a fetchable absolute http(s) URL afterward.
    """
    resolved_urls = []
    for loc in _LOC_RE.findall(xml_text):
        resolved = urljoin(base_url, loc)
        parsed = urlsplit(resolved)
        if parsed.scheme in ("http", "https") and parsed.netloc:
            resolved_urls.append(resolved)
    return resolved_urls


class SitemapDiscovery:
    """One instance per adapter, reused across that adapter's fetch() calls
    (it holds no per-call state). Traverses a sitemap (index or plain),
    filters to job-like URLs, and narrows the result with the three-layer
    fallback described in this module's docstring. `source_name` is used
    only for log-line prefixing, so a reader can tell which adapter a given
    line came from without every call site repeating it.

    Also owns `fetch_text()`, the retry-once-on-5xx/timeout HTTP fetch every
    adapter in this codebase uses -- shared here because both the sitemap
    traversal AND each adapter's own per-job-page fetch need the identical
    retry contract, not because it's conceptually part of "sitemap discovery"
    specifically.
    """

    def __init__(
        self,
        client: httpx.Client,
        user_agent: str,
        company_name: str,
        source_name: str,
        search_terms: list[str] | None = None,
        job_path_markers: list[str] | None = None,
        slug_vocabulary: list[str] | None = None,
        non_job_path_markers: list[str] | None = None,
        locations: list[str] | None = None,
        sample_size: int = DEFAULT_SAMPLE_SIZE,
        page_cap: int = DEFAULT_PAGE_CAP,
    ) -> None:
        self.client = client
        self.user_agent = user_agent
        self.company_name = company_name
        self.source_name = source_name
        # `is not None` (not truthiness) throughout: an explicitly-passed
        # empty list is a deliberate "disable this layer" override and must
        # be honoured, not silently replaced by the default because `[]` is
        # falsy (M11 Part A's fix -- see sitemap_jsonld.py's tests for why).
        self.search_terms = list(search_terms) if search_terms is not None else []
        self.job_path_markers = (
            list(job_path_markers)
            if job_path_markers is not None
            else list(DEFAULT_JOB_PATH_MARKERS)
        )
        self.slug_vocabulary = (
            list(slug_vocabulary) if slug_vocabulary is not None else list(DEFAULT_SLUG_VOCABULARY)
        )
        self.non_job_path_markers = (
            list(non_job_path_markers)
            if non_job_path_markers is not None
            else list(DEFAULT_NON_JOB_PATH_MARKERS)
        )
        # M14 Part C: filters.yaml's own locations.include, threaded down
        # from run.py -- never hardcoded here (CLAUDE.md rule 4). A refinement
        # applied on top of whichever layer below matches, not a layer of its
        # own; see _refine_by_location().
        self.locations = list(locations) if locations is not None else []
        self.sample_size = sample_size
        self.page_cap = page_cap

    def fetch_text(self, url: str) -> str:
        """One document -- sitemap XML or an individual job page's HTML,
        the retry contract doesn't care which. Never lets an httpx exception
        escape: every failure mode is one of this codebase's own SourceError
        subclasses."""
        headers = {"User-Agent": self.user_agent}
        response: httpx.Response | None = None
        error: Exception | None = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = self.client.get(url, headers=headers, timeout=TIMEOUT_SECONDS)
            except httpx.TimeoutException as exc:
                error = exc
                logger.warning(
                    "%s: timeout fetching %s for %s (attempt %d/%d)",
                    self.source_name, url, self.company_name, attempt, MAX_ATTEMPTS,
                )
                continue

            if response.status_code >= 500:
                error = SourceError(
                    f"{self.source_name}: {self.company_name} ({url}) "
                    f"returned HTTP {response.status_code}"
                )
                logger.warning(
                    "%s: HTTP %d fetching %s for %s (attempt %d/%d)",
                    self.source_name, response.status_code, url, self.company_name,
                    attempt, MAX_ATTEMPTS,
                )
                continue

            error = None
            break

        if error is not None:
            raise SourceError(
                f"{self.source_name}: failed to fetch {url} for {self.company_name} "
                f"after {MAX_ATTEMPTS} attempts"
            ) from error

        if response.status_code == 404:
            raise SourceNotFoundError(
                f"{self.source_name}: not found for {self.company_name} ({url}): returned 404"
            )
        if response.status_code >= 400:
            raise SourceError(
                f"{self.source_name}: {self.company_name} ({url}) "
                f"returned HTTP {response.status_code}"
            )

        return response.text

    def discover_job_urls(self, sitemap_url: str) -> list[str]:
        """Fetches `sitemap_url` itself, then traverses it. Split out as
        discover_job_urls_from_text() (M12 Part A) for a caller that needs
        to inspect the top-level document BEFORE committing to sitemap
        traversal -- successfactors.py fetches once, sniffs whether the
        response is this shape (a <loc>-based urlset/sitemapindex) or an RSS
        feed (a different SuccessFactors RMK tenant configuration entirely,
        see that module's docstring), and only calls
        discover_job_urls_from_text() for the former, without a redundant
        second fetch of the same URL."""
        return self.discover_job_urls_from_text(sitemap_url, self.fetch_text(sitemap_url))

    def discover_job_urls_from_text(self, sitemap_url: str, top_level_text: str) -> list[str]:
        """Index -> leaf sitemaps -> <loc> entries -> job-shaped URLs -> the
        three-layer narrowing. Returns the final list of URLs the calling
        adapter should fetch and parse. Logs which path fired and the
        resulting candidate count, once, regardless of which adapter
        called it (M11 A3). `top_level_text` is the already-fetched content
        of `sitemap_url` -- see discover_job_urls() for the common case
        where the caller doesn't already have it in hand."""
        top_level_urls = extract_locs(top_level_text, sitemap_url)
        sub_sitemaps = [u for u in top_level_urls if u.endswith(".xml")]

        # A one-level sitemap (no index) means the top-level document's own
        # <loc> entries already are the page URLs.
        job_candidate_urls = list(top_level_urls) if not sub_sitemaps else []
        for sub_url in sub_sitemaps:
            job_candidate_urls.extend(extract_locs(self.fetch_text(sub_url), sub_url))

        job_urls = [
            u
            for u in job_candidate_urls
            if looks_like_a_job_url(u, self.job_path_markers, self.non_job_path_markers)
        ]
        # M15 Part B: deduplicated BEFORE narrowing (not just before the page
        # cap) so every layer below -- search_terms, slug vocabulary,
        # location, and the sample fallback -- counts and logs the real
        # number of distinct postings, not one inflated by locale mirrors.
        job_urls = _dedupe_by_trailing_numeric_id(job_urls)

        urls_to_fetch, path_used = self._select_urls_to_fetch(job_urls, sitemap_url)
        logger.info(
            "%s: %s (%s) used the %r path: %d candidate(s) selected out of "
            "%d job-like URL(s) total",
            self.source_name, self.company_name, sitemap_url, path_used,
            len(urls_to_fetch), len(job_urls),
        )
        return urls_to_fetch

    def _select_urls_to_fetch(
        self, job_urls: list[str], sitemap_url: str
    ) -> tuple[list[str], str]:
        if self.search_terms:
            normalized_terms = [normalize(t) for t in self.search_terms]
            matched = [
                u for u in job_urls if any(t in normalize(u) for t in normalized_terms)
            ]
            if matched:
                return self._finalize_selection(matched, "search_terms", sitemap_url)

        vocabulary_matched = [
            u for u in job_urls if any(term in normalize(u) for term in self.slug_vocabulary)
        ]
        if vocabulary_matched:
            return self._finalize_selection(vocabulary_matched, "slug_vocabulary", sitemap_url)

        # The slug filter is an optimization, never a gate -- neither layer
        # above matching anything is not itself a reason to return zero
        # postings. No location refinement here: this is already the
        # last-resort path, and the sample is small and unfiltered by design.
        sampled = evenly_spread_sample(job_urls, self.sample_size)
        return sampled, "sampled"

    def _finalize_selection(
        self, matched: list[str], path_name: str, sitemap_url: str
    ) -> tuple[list[str], str]:
        """M14 Part C: refines `matched` (whichever of search_terms or
        slug_vocabulary produced it) by `self.locations` before applying the
        page cap. A fallback refinement, not a gate, exactly like the layers
        above it: if narrowing by location leaves nothing, the un-narrowed
        set is kept, since not every employer's sitemap puts a city in the
        slug. Narrowing before capping (rather than after) so the cap, when
        it binds at all, binds on the best-available set."""
        if self.locations:
            normalized_locations = [normalize(loc) for loc in self.locations]
            location_matched = [
                u for u in matched if any(loc in normalize(u) for loc in normalized_locations)
            ]
            if location_matched:
                refined_path_name = f"{path_name}+location"
                return (
                    self._apply_page_cap(location_matched, refined_path_name, sitemap_url),
                    refined_path_name,
                )
        return self._apply_page_cap(matched, path_name, sitemap_url), path_name

    def _apply_page_cap(self, urls: list[str], path_name: str, sitemap_url: str) -> list[str]:
        if len(urls) > self.page_cap:
            logger.warning(
                "%s: %s (%s) hit the %d-page cap on the %r path "
                "(%d candidates matched), more postings may exist",
                self.source_name, self.company_name, sitemap_url, self.page_cap,
                path_name, len(urls),
            )
            return urls[: self.page_cap]
        return urls
