"""Discovery CLI: finds which ATS an employer's own careers page uses, so
adding a company to companies/*.yaml stops being manual token-guessing.

CLAUDE.md rule 3 governs this entire module: a company DIRECTORY may be used
to find an employer's website -- that step happens outside this file, by a
human pasting URLs into discovery/seeds/*.txt (see discovery/seeds/README.md).
This module only ever fetches URLs built from two things: the employer's own
website (as given in the input file) and a detected ATS's own API host
(boards-api.greenhouse.io, api.lever.co, api.ashbyhq.com -- the same hosts
the adapters in jobbot/sources/ already call). It never fetches a listing
from an aggregator or job board. See
test_discovery_never_fetches_listings_from_a_directory, which is load-bearing.

Two deliberate deviations from this milestone's literal function sketches,
both needed for the CLI flags this milestone also asks for:

- discover_company() takes `delay` and `verify_result` in addition to the
  sketch's five parameters. `delay` is the value the injected `sleep`
  callable is actually called with (so --delay reaches the same politeness
  pause the tests assert on); `verify_result` is what --no-verify flips.
  Both default to matching the CLI's own defaults.
- verify() also treats a ValueError from adapter construction (a malformed
  identifier, e.g. a non-https URL handed to JsonLdSource) as a discovery
  failure rather than letting it escape -- the same "never raises, always
  (0, reason)" contract the milestone specifies for SourceError.

Post-M7 fix: the first real run (22 companies, 5 confirmed, all previously
known) showed that guessing fixed paths like /careers doesn't work -- most
sites link their careers page from the nav bar instead of serving it at a
guessable URL. Illuin Technology was the proof: it genuinely uses Ashby, but
its homepage links to none of /careers, /jobs, /carrieres, /recrutement.
discover_company's resolution order is now: (1) the site root, always fetched
first, since it's the one page guaranteed to exist and the one most likely to
carry a nav link, (2) actual careers-looking links harvested from that root
page via careers_links(), and only then (3) the guessed-path list as a last
resort. See careers_links() below and discover_company()'s docstring for the
full order.

M9 depth-2 fix: re-running against discovery/seeds/example.txt after the fix
above still left most companies unresolved with the same pattern -- a careers
link was found and followed, but that page turned out to be a culture/team
page rather than the actual listings, with the real ATS link one hop further
(Illuin's own "pourquoi-nous-rejoindre" page is the example). A depth-1 page
that itself yields no ATS is now searched for its own careers-looking links,
and up to 2 of those (listings-suggesting ones -- offres, postes, openings --
preferred over generic ones) are followed as depth 2. No recursion past that.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
import yaml

from jobbot.models import normalize
from jobbot.settings import SettingsError, load_settings
from jobbot.sources.ashby import AshbySource
from jobbot.sources.base import JobSource, SourceError
from jobbot.sources.greenhouse import GreenhouseSource
from jobbot.sources.html_text import strip_html
from jobbot.sources.jibe import JibeSource
from jobbot.sources.jsonld import JsonLdSource
from jobbot.sources.lever import LeverSource
from jobbot.sources.robots import RobotsCache
from jobbot.sources.sitemap_jsonld import SitemapJsonLdSource
from jobbot.sources.smartrecruiters import SmartRecruitersSource
from jobbot.sources.talentsoft import TalentsoftSource
from jobbot.sources.workday import WorkdaySource

logger = logging.getLogger(__name__)

FETCH_TIMEOUT_SECONDS = 15.0
MAX_URL_ATTEMPTS = 8
MAX_CAREERS_LINKS = 6
DEFAULT_DELAY_SECONDS = 2.0
DEFAULT_OUTPUT_PATH = Path("discovered.yaml")


@dataclass
class DiscoveryResult:
    company_name: str
    website: str
    ats: str | None
    identifier: str | None
    careers_url: str | None
    postings_found: int
    confidence: str  # "confirmed" | "probable" | "none"
    notes: str


# --- Part A: ATS detection -------------------------------------------------

_TOKEN = r"([a-zA-Z0-9_-]+)"

def _single_group(match: re.Match[str]) -> str:
    return match.group(1)


def _workday_identifier(match: re.Match[str]) -> str:
    """Workday's real URL shape is
    "{tenant}.wd{N}.myworkdayjobs.com/{optional-locale/}{site}" -- the
    identifier every adapter in this codebase uses is "{tenant}.wd{N}.{site}"
    (no locale segment, since fetch_raw() talks to the CXS API directly, not
    the locale-prefixed UI path), so it's assembled from the two captured
    groups rather than being one contiguous substring of the URL."""
    return f"{match.group(1)}.{match.group(2)}"


def _talentsoft_identifier(match: re.Match[str]) -> str:
    return f"https://{match.group(1)}"


# Checked in this fixed order (not by which appears first in the raw text) so
# a page mentioning more than one ATS resolves the same way every time. Each
# pattern pairs with the function that turns its match into the identifier
# shape that ATS's own adapter expects -- group(1) verbatim for most, but
# Workday and Talentsoft need their captured pieces assembled (see above).
_ATS_SIGNATURES: list[tuple[str, list[tuple[re.Pattern[str], Callable[[re.Match[str]], str]]]]] = [
    (
        "greenhouse",
        [
            (re.compile(rf"boards-api\.greenhouse\.io/v1/boards/{_TOKEN}", re.IGNORECASE), _single_group),
            (re.compile(rf"job-boards\.greenhouse\.io/{_TOKEN}", re.IGNORECASE), _single_group),
            (re.compile(rf"boards\.greenhouse\.io/{_TOKEN}", re.IGNORECASE), _single_group),
        ],
    ),
    (
        "lever",
        [
            (re.compile(rf"api\.lever\.co/v0/postings/{_TOKEN}", re.IGNORECASE), _single_group),
            (re.compile(rf"jobs\.lever\.co/{_TOKEN}", re.IGNORECASE), _single_group),
        ],
    ),
    (
        "ashby",
        [
            (
                re.compile(rf"api\.ashbyhq\.com/posting-api/job-board/{_TOKEN}", re.IGNORECASE),
                _single_group,
            ),
            (re.compile(rf"jobs\.ashbyhq\.com/{_TOKEN}", re.IGNORECASE), _single_group),
        ],
    ),
    (
        # M9d: real URL confirmed live across every Workday tenant checked
        # for M9/M9b (e.g. ipsen.wd103.myworkdayjobs.com/en-EN/Ipsen_Careers).
        "workday",
        [
            (
                re.compile(
                    r"([a-zA-Z0-9-]+\.wd\d+)\.myworkdayjobs\.com/"
                    r"(?:[a-zA-Z]{2}-[a-zA-Z]{2}/)?([a-zA-Z0-9_-]+)",
                    re.IGNORECASE,
                ),
                _workday_identifier,
            ),
        ],
    ),
    (
        # M9d: careers.smartrecruiters.com/{id} is the public career page a
        # company's own site links to; api.smartrecruiters.com is the
        # adapter's own endpoint, occasionally linked directly instead.
        "smartrecruiters",
        [
            (re.compile(rf"careers\.smartrecruiters\.com/{_TOKEN}", re.IGNORECASE), _single_group),
            (re.compile(rf"api\.smartrecruiters\.com/v1/companies/{_TOKEN}", re.IGNORECASE), _single_group),
        ],
    ),
    (
        # M9d: identifier is the full https URL to the tenant's own
        # subdomain (jobbot/sources/talentsoft.py's own identifier shape) --
        # captured whole rather than as a bare token, unlike every vendor
        # above whose identifier is just the token whichever domain gives.
        "talentsoft",
        [
            (re.compile(r"https?://([a-zA-Z0-9-]+\.talent-soft\.com)", re.IGNORECASE), _talentsoft_identifier),
        ],
    ),
]

# M9d: Jibe (jobbot/sources/jibe.py) has no shared host of its own -- every
# employer runs it on their OWN domain -- so there's no URL token to capture
# the way every vendor above has. What IS constant across every Jibe
# deployment (confirmed live on AXA) is that its pages load assets from
# jibecdn.com; when that's present, the identifier is simply the page's own
# domain, checked separately from the token-capturing loop above.
_JIBE_SIGNATURE_RE = re.compile(r"jibecdn\.com", re.IGNORECASE)

# Lightweight presence check, not a full extraction (jsonld.py's own adapter
# does the real parsing at fetch time) -- just enough to tell "this page has
# JobPosting structured markup" from "this page has neither".
_JSONLD_SCRIPT_RE = re.compile(
    r"<script[^>]*type\s*=\s*[\"']?application/ld\+json[\"']?[^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)


def detect_ats(html: str, page_url: str) -> tuple[str | None, str | None]:
    """Pure, no network. ATS-specific URL signatures are checked before the
    generic JSON-LD fallback, since a page can carry both (e.g. an embedded
    ATS widget plus unrelated boilerplate that happens to mention
    "JobPosting" elsewhere). Returns (None, None) when nothing matches.
    """
    for ats_name, pattern_pairs in _ATS_SIGNATURES:
        for pattern, build_identifier in pattern_pairs:
            match = pattern.search(html)
            if match:
                return ats_name, build_identifier(match)

    if _JIBE_SIGNATURE_RE.search(html):
        parsed = urlsplit(page_url)
        return "jibe", f"{parsed.scheme}://{parsed.netloc}"

    for script_match in _JSONLD_SCRIPT_RE.finditer(html):
        if "JobPosting" in script_match.group(1):
            return "jsonld", page_url

    return None, None


_ADAPTER_CLASSES: dict[str, type[JobSource]] = {
    "greenhouse": GreenhouseSource,
    "lever": LeverSource,
    "ashby": AshbySource,
    "jsonld": JsonLdSource,
    "workday": WorkdaySource,
    "smartrecruiters": SmartRecruitersSource,
    "talentsoft": TalentsoftSource,
    "jibe": JibeSource,
    "sitemap_jsonld": SitemapJsonLdSource,
}


def verify(ats: str, identifier: str, client: httpx.Client, user_agent: str) -> tuple[int, str]:
    """Calls the real adapter's fetch() to confirm a detected identifier
    actually resolves and returns postings -- this is what turns "probable"
    into "confirmed". Never raises: any failure comes back as (0, reason).
    """
    adapter_cls = _ADAPTER_CLASSES.get(ats)
    if adapter_cls is None:
        return 0, f"no adapter registered for ats {ats!r}"

    try:
        source = adapter_cls(identifier, identifier, client, user_agent=user_agent)
        jobs = source.fetch()
    except (SourceError, ValueError) as exc:
        return 0, str(exc)

    return len(jobs), f"{len(jobs)} postings found"


# --- Part B: careers page resolution ----------------------------------------

# Given in this literal order; for a .fr domain the French-tagged paths are
# moved ahead of the English ones (relative order preserved within each
# group), per the milestone's explicit ordering rule.
_CAREERS_PATHS: list[tuple[str, str]] = [
    ("/careers", "en"),
    ("/jobs", "en"),
    ("/carrieres", "fr"),
    ("/recrutement", "fr"),
    ("/nous-rejoindre", "fr"),
    ("/rejoignez-nous", "fr"),
    ("/emplois", "fr"),
    ("/join-us", "en"),
    ("/careers/jobs", "en"),
    ("/fr/carrieres", "fr"),
]


def candidate_careers_urls(website: str) -> list[str]:
    """Pure. Ordered URLs worth trying for a company's careers page, most
    promising first, site root last."""
    website = website.rstrip("/")
    host = urlsplit(website).netloc.lower()

    if host.endswith(".fr"):
        ordered_paths = [p for p, lang in _CAREERS_PATHS if lang == "fr"] + [
            p for p, lang in _CAREERS_PATHS if lang == "en"
        ]
    else:
        ordered_paths = [p for p, _ in _CAREERS_PATHS]

    urls = [website + path for path in ordered_paths]
    urls.append(website)
    return urls


# Same keyword list for both href and link text; link text additionally
# accepts a few whole phrases that don't contain any of these words on their
# own ("We're hiring", "On recrute").
_CAREERS_KEYWORDS = (
    "career", "careers", "jobs", "emploi", "emplois", "carriere", "carrieres",
    "recrutement", "rejoign", "join-us", "nous-rejoindre",
)
_CAREERS_TEXT_PHRASES = ("nous recrutons", "on recrute", "we're hiring")

# A nav link often points straight at the ATS itself (jobs.ashbyhq.com/token,
# boards.greenhouse.io/token) rather than at an internal /careers page --
# that's a different host than the company's own, so it needs an explicit
# exception to the same-host restriction below.
_KNOWN_ATS_HOSTS = frozenset(
    {
        "boards.greenhouse.io", "job-boards.greenhouse.io", "boards-api.greenhouse.io",
        "jobs.lever.co", "api.lever.co",
        "jobs.ashbyhq.com", "api.ashbyhq.com",
    }
)

_ANCHOR_RE = re.compile(
    r'<a\b[^>]*\bhref\s*=\s*["\']([^"\']*)["\'][^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)


def _normalize_for_careers_match(text: str) -> str:
    return normalize(text).replace("’", "'")


def _looks_like_a_careers_link(
    href: str, link_text: str, extra_keywords: tuple[str, ...] = ()
) -> bool:
    keywords = _CAREERS_KEYWORDS + extra_keywords if extra_keywords else _CAREERS_KEYWORDS
    href_norm = _normalize_for_careers_match(href)
    if any(keyword in href_norm for keyword in keywords):
        return True
    text_norm = _normalize_for_careers_match(link_text)
    if any(keyword in text_norm for keyword in keywords):
        return True
    return any(phrase in text_norm for phrase in _CAREERS_TEXT_PHRASES)


def _bare_host(netloc: str) -> str:
    return netloc.removeprefix("www.")


def _iter_careers_link_candidates(
    html: str, base_url: str, extra_keywords: tuple[str, ...] = ()
) -> list[tuple[str, str]]:
    """(absolute_url, link_text) for every anchor in `html` whose href or
    visible text suggests a careers page -- same host as `base_url` (a
    leading "www." ignored on either side), or a known ATS's own host, since
    a nav link often points straight at one. Deduplicated by URL, document
    order preserved, capped at MAX_CAREERS_LINKS. careers_links() is a thin
    wrapper over this that drops the text; kept separate because
    discover_company's depth-2 listings-preference ranking (A3) needs the
    text too, not just the URL, and needs a broader qualifying vocabulary
    (`extra_keywords`) -- "voir-les-offres" or "nos-offres" alone wouldn't
    otherwise pass the depth-1 careers-page filter at all, let alone rank
    first in it.
    """
    base_host = _bare_host(urlsplit(base_url).netloc.lower())
    seen: set[str] = set()
    candidates: list[tuple[str, str]] = []

    for match in _ANCHOR_RE.finditer(html):
        href, inner_html = match.group(1), match.group(2)
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue

        link_text = strip_html(inner_html)
        if not _looks_like_a_careers_link(href, link_text, extra_keywords):
            continue

        absolute = urljoin(base_url, href)
        if not absolute.startswith(("http://", "https://")):
            continue

        host = _bare_host(urlsplit(absolute).netloc.lower())
        if host != base_host and host not in _KNOWN_ATS_HOSTS:
            continue

        if absolute in seen:
            continue
        seen.add(absolute)
        candidates.append((absolute, link_text))
        if len(candidates) >= MAX_CAREERS_LINKS:
            break

    return candidates


def careers_links(html: str, base_url: str) -> list[str]:
    """Pure, no network. Absolute URLs of every anchor in `html` whose href
    or visible text suggests a careers page -- same host as `base_url` (a
    leading "www." ignored on either side), or a known ATS's own host, since
    a nav link often points straight at one. Deduplicated, document order
    preserved, capped at MAX_CAREERS_LINKS.
    """
    return [url for url, _text in _iter_careers_link_candidates(html, base_url)]


# Depth-2 only (A3): among a depth-1 page's own careers-looking links, the
# ones that look like they go straight to a listings page beat generic ones
# ("Team culture" vs. "Voir les offres").
_LISTINGS_KEYWORDS = (
    "offres", "postes", "opportunit", "openings", "positions",
    "all-jobs", "nos-offres", "voir-les-offres",
)


def _suggests_listings(url: str, text: str) -> bool:
    return any(
        keyword in _normalize_for_careers_match(value)
        for value in (url, text)
        for keyword in _LISTINGS_KEYWORDS
    )


def _ranked_depth2_candidates(html: str, base_url: str) -> list[str]:
    """Depth-2 candidates from a depth-1 page: same extraction as
    careers_links(), broadened to also qualify on _LISTINGS_KEYWORDS (a link
    whose only signal is "voir-les-offres" wouldn't pass careers_links()'s
    own narrower filter), with listings-suggesting links sorted ahead of
    generic ones -- a stable sort, so document order is preserved within
    each group."""
    candidates = _iter_careers_link_candidates(html, base_url, extra_keywords=_LISTINGS_KEYWORDS)
    ranked = sorted(candidates, key=lambda pair: not _suggests_listings(*pair))
    return [url for url, _text in ranked]


# M9d: the sitemap_jsonld resolution path (Part D). Same structural
# URL-shape markers as jobbot/sources/sitemap_jsonld.py's own
# DEFAULT_JOB_PATH_MARKERS -- kept as a separate, local copy rather than an
# import, matching this module's existing convention of not reaching into
# another adapter module's internals (discover.py only imports adapter
# *classes*, for verify(), never their private helpers).
_SITEMAP_JOB_PATH_MARKERS = (
    "/job/", "/jobs/", "/offre/", "/offres/", "/emploi/", "/poste/", "/career/", "/vacancy/",
)
_SITEMAP_NUMERIC_PATH_SEGMENT_RE = re.compile(r"/\d{3,}(?:[/?#-]|$)")
_SITEMAP_LOC_RE = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.IGNORECASE | re.DOTALL)
_SITEMAP_DECLARATION_RE = re.compile(r"(?im)^Sitemap:\s*(\S+)")
MAX_SITEMAP_SUB_SITEMAPS = 3
MAX_SITEMAP_JOB_SAMPLES = 3


def _looks_like_a_sitemap_job_url(url: str) -> bool:
    lowered = url.lower()
    return (
        any(marker in lowered for marker in _SITEMAP_JOB_PATH_MARKERS)
        or bool(_SITEMAP_NUMERIC_PATH_SEGMENT_RE.search(url))
    )


def _resolve_sitemap_url(base_url: str, candidate: str) -> str | None:
    """The sitemap protocol requires every <loc> and every robots.txt
    Sitemap: value to be an absolute URL, but real-world robots.txt files
    don't always get this right -- a relative "Sitemap: /sitemap.xml" is
    real, observed behavior (confirmed against discovery/seeds/Batch1.txt),
    and feeding that straight to httpx/RobotFileParser as if it were
    absolute raises deep inside urllib rather than failing cleanly. Resolved
    against `base_url` the same way an href on the page would be; returns
    None (never raises) for anything that still isn't a fetchable absolute
    http(s) URL afterward.
    """
    resolved = urljoin(base_url, candidate)
    parsed = urlsplit(resolved)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    return resolved


def _discover_sitemap_jsonld(
    website: str,
    try_url: Callable[[str], tuple[str | None, str | None, str | None]],
) -> tuple[str, str] | None:
    """Part D / M9d: the last resort, tried only after every other
    resolution step has failed. Checks whether this employer's own site
    publishes a sitemap of individual job pages carrying schema.org
    JobPosting JSON-LD -- exactly the real pattern
    jobbot/sources/sitemap_jsonld.py's own module docstring documents
    finding for Orange and Thales, both of which had been declared
    impossible by every earlier resolution step (Phenom's search widget is
    session/CSRF-bound). Returns ("sitemap_jsonld", sitemap_index_url) on a
    genuine JobPosting hit in a sampled job page, else None -- never raises,
    same as every other step here (`try_url` already turns every failure
    mode into a `None` html result rather than an exception).

    Uses `try_url` for every fetch, exactly like every other step, so
    robots.txt, the politeness delay, and the "never fetch the same URL
    twice this call" cache all apply here too -- this just isn't gated by
    the same MAX_URL_ATTEMPTS budget the page-guessing steps share, since a
    real sitemap tree can legitimately need more than 8 fetches (index +
    several leaf sitemaps + several sampled job pages) to reach a verdict.
    """
    root = website.rstrip("/")

    _, _, robots_text = try_url(f"{root}/robots.txt")
    declared_sitemaps = _SITEMAP_DECLARATION_RE.findall(robots_text) if robots_text else []
    # Per spec every declared value is already absolute, but real robots.txt
    # files aren't always spec-compliant -- resolved against `root` (a no-op
    # for one that already is absolute) rather than trusted verbatim, and
    # dropped entirely if it still isn't a fetchable absolute URL afterward.
    sitemap_urls = [
        resolved
        for declared in declared_sitemaps
        if (resolved := _resolve_sitemap_url(root, declared)) is not None
    ]

    # If a guessed path is what works, its content is already in hand --
    # reused directly as top_level_text rather than fetched a second time,
    # which `try_url`'s own already-fetched cache would just turn into a
    # `None` (it never re-fetches the same URL twice in one discover_company
    # call, by design -- see A2 in this module's docstring).
    sitemap_index_url: str | None = None
    top_level_text: str | None = None

    if sitemap_urls:
        sitemap_index_url = sitemap_urls[0]
        _, _, top_level_text = try_url(sitemap_index_url)
    else:
        for guess_path in ("/sitemap_index.xml", "/sitemap.xml"):
            _, _, guess_text = try_url(root + guess_path)
            if guess_text and "<" in guess_text[:200]:
                sitemap_index_url = root + guess_path
                top_level_text = guess_text
                break

    if sitemap_index_url is None or not top_level_text:
        return None

    top_level_locs = _SITEMAP_LOC_RE.findall(top_level_text)
    top_level_urls = [
        resolved for loc in top_level_locs if (resolved := _resolve_sitemap_url(root, loc)) is not None
    ]
    sub_sitemaps = [u for u in top_level_urls if u.endswith(".xml")]

    all_urls: list[str] = list(top_level_urls) if not sub_sitemaps else []
    for sub_sitemap_url in sub_sitemaps[:MAX_SITEMAP_SUB_SITEMAPS]:
        _, _, sub_text = try_url(sub_sitemap_url)
        if sub_text:
            all_urls.extend(
                resolved
                for loc in _SITEMAP_LOC_RE.findall(sub_text)
                if (resolved := _resolve_sitemap_url(root, loc)) is not None
            )

    job_urls = [u for u in all_urls if _looks_like_a_sitemap_job_url(u)]

    for sample_url in job_urls[:MAX_SITEMAP_JOB_SAMPLES]:
        _, _, sample_html = try_url(sample_url)
        if sample_html and any(
            "JobPosting" in match.group(1) for match in _JSONLD_SCRIPT_RE.finditer(sample_html)
        ):
            return "sitemap_jsonld", sitemap_index_url

    return None


def discover_company(
    website: str,
    company_name: str,
    client: httpx.Client,
    user_agent: str,
    sleep: Any = time.sleep,
    delay: float = DEFAULT_DELAY_SECONDS,
    verify_result: bool = True,
) -> DiscoveryResult:
    """Resolution order, at most MAX_URL_ATTEMPTS fetches total:

    1. the site root, always fetched first -- the one page guaranteed to
       exist and the one most likely to carry a nav link to careers.
    2. actual careers-looking links harvested from that root page (depth 1,
       see careers_links()), fetched in order, detect_ats run on each. A
       depth-1 page that itself yields no ATS is searched in turn for its
       own careers-looking links, and up to 2 of those (listings-suggesting
       ones first, per A3 -- see _ranked_depth2_candidates()) are followed
       as depth 2. No further recursion past depth 2 (A1): a depth-2 page
       that yields no ATS is simply a dead end.
    3. the guessed-path list (candidate_careers_urls()), for sites the
       link-following steps found nothing on.
    4. (M9d) the sitemap_jsonld route: robots.txt's Sitemap: lines (or a
       guessed /sitemap_index.xml, /sitemap.xml), recursed one level,
       sampled for job-like URLs carrying real JobPosting JSON-LD -- see
       _discover_sitemap_jsonld(). Tried last since it's the most expensive
       step, but it's also the only one that can resolve an employer whose
       listing page is entirely client-rendered.

    Every fetch in steps 1-3 counts against the same MAX_URL_ATTEMPTS
    budget and the same per-call `fetched` set (A2), so a link back to an
    already-fetched page is never fetched twice; step 4 shares `fetched`
    too (never re-fetches a URL steps 1-3 already tried) but has its own,
    separate budget (see _discover_sitemap_jsonld). Verifies the result
    unless `verify_result` is False. On failure, `notes` records every URL
    attempted and what it returned, in the exact order they were walked --
    root, then depth 1, then depth 2 for whichever depth-1 page spawned it,
    then the sitemap route -- plus whether any careers-looking links were
    found at all, so a failure is debuggable straight from unresolved.txt.
    """
    robots = RobotsCache(client, user_agent)
    attempts: list[str] = []
    fetched: set[str] = set()

    def try_url(url: str) -> tuple[str | None, str | None, str | None]:
        """Fetch one URL (skipping ones already fetched this call), record
        what happened in `attempts`. Returns (ats, identifier, html); html is
        None on any failure so callers can still tell a fetch failed apart
        from a fetch that succeeded but matched nothing."""
        if url in fetched:
            return None, None, None
        fetched.add(url)

        if attempts:  # never sleep before the very first request
            sleep(delay)

        if not robots.allowed(url):
            attempts.append(f"{url} -> robots.txt disallowed")
            return None, None, None

        try:
            response = client.get(
                url, headers={"User-Agent": user_agent}, timeout=FETCH_TIMEOUT_SECONDS
            )
        except httpx.HTTPError as exc:
            attempts.append(f"{url} -> fetch failed: {exc}")
            return None, None, None

        if response.status_code != 200:
            attempts.append(f"{url} -> HTTP {response.status_code}")
            return None, None, None

        ats, identifier = detect_ats(response.text, url)
        if ats is not None:
            attempts.append(f"{url} -> HTTP 200, detected {ats}")
            return ats, identifier, response.text

        attempts.append(f"{url} -> HTTP 200, no ATS signature")
        return None, None, response.text

    detected_ats: str | None = None
    detected_identifier: str | None = None
    detected_url: str | None = None
    root_url = website.rstrip("/")

    # Step 1: the site root.
    ats, identifier, root_html = try_url(root_url)
    if ats is not None:
        detected_ats, detected_identifier, detected_url = ats, identifier, root_url

    # Step 2: careers-looking links found on that root page (depth 1), and,
    # for any depth-1 page that itself yields no ATS, up to 2 of its own
    # careers-looking links (depth 2, A1) -- no further.
    links_found: list[str] = []
    if detected_ats is None and root_html is not None:
        links_found = careers_links(root_html, root_url)
        for link_url in links_found:
            if len(fetched) >= MAX_URL_ATTEMPTS:
                break

            ats, identifier, depth1_html = try_url(link_url)
            if ats is not None:
                detected_ats, detected_identifier, detected_url = ats, identifier, link_url
                break

            if depth1_html is not None:
                for depth2_url in _ranked_depth2_candidates(depth1_html, link_url)[:2]:
                    if len(fetched) >= MAX_URL_ATTEMPTS:
                        break
                    ats, identifier, _ = try_url(depth2_url)
                    if ats is not None:
                        detected_ats, detected_identifier, detected_url = ats, identifier, depth2_url
                        break
                if detected_ats is not None:
                    break

    # Step 3: guessed paths, last resort.
    if detected_ats is None:
        for candidate_url in candidate_careers_urls(website):
            if len(fetched) >= MAX_URL_ATTEMPTS:
                break
            if candidate_url == root_url:
                continue  # already tried in step 1
            ats, identifier, _ = try_url(candidate_url)
            if ats is not None:
                detected_ats, detected_identifier, detected_url = ats, identifier, candidate_url
                break

    # Step 4 (M9d): the sitemap_jsonld route, tried only once every page-
    # guessing and link-following step above has failed -- this is what
    # resolves an employer whose listing page is entirely client-rendered
    # but whose individual job pages are sitemap-discoverable and carry
    # real JobPosting JSON-LD (see _discover_sitemap_jsonld's own
    # docstring). Not gated by MAX_URL_ATTEMPTS, which governs the cheaper
    # guessing steps above, not this more expensive, more reliable one.
    if detected_ats is None:
        sitemap_result = _discover_sitemap_jsonld(website, try_url)
        if sitemap_result is not None:
            detected_ats, detected_identifier = sitemap_result
            detected_url = detected_identifier  # the sitemap index url itself

    if detected_ats is None or detected_identifier is None:
        notes = "; ".join(attempts) if attempts else "no attempts made"
        if links_found:
            notes += f"; careers links found but none exposed a known ATS: {', '.join(links_found)}"
        elif root_html is not None:
            notes += "; no careers links found on the root page"
        return DiscoveryResult(
            company_name=company_name,
            website=website,
            ats=None,
            identifier=None,
            careers_url=None,
            postings_found=0,
            confidence="none",
            notes=notes,
        )

    if verify_result:
        count, note = verify(detected_ats, detected_identifier, client, user_agent)
        confidence = "confirmed" if count > 0 else "probable"
    else:
        count, note = 0, "not verified (--no-verify)"
        confidence = "probable"

    return DiscoveryResult(
        company_name=company_name,
        website=website,
        ats=detected_ats,
        identifier=detected_identifier,
        careers_url=detected_url,
        postings_found=count,
        confidence=confidence,
        notes=note,
    )


# --- Part C: the CLI ---------------------------------------------------


def _derive_name_from_url(url: str) -> str:
    host = urlsplit(url).netloc or url
    host = host.removeprefix("www.")
    base = host.split(".")[0]
    return base.replace("-", " ").replace("_", " ").title() or url


def _normalize_website(url: str) -> str:
    if not url.startswith(("http://", "https://")):
        return f"https://{url}"
    return url


def _parse_input_line(line: str) -> tuple[str, str] | None:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if "," in line:
        name, _, url = line.partition(",")
        name, url = name.strip(), _normalize_website(url.strip())
        return name, url
    url = _normalize_website(line)
    return _derive_name_from_url(url), url


def load_input_companies(path: Path) -> list[tuple[str, str]]:
    """One website per line, or "name,url". Blank lines and lines starting
    with # are ignored."""
    companies = []
    for raw_line in Path(path).read_text(encoding="utf-8").splitlines():
        parsed = _parse_input_line(raw_line)
        if parsed:
            companies.append(parsed)
    return companies


def load_existing_entries(path: Path) -> list[dict]:
    if not path.exists():
        return []
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    if not isinstance(raw, list):
        return []
    return raw


def _entry_key(entry: dict) -> tuple[str | None, str | None]:
    return entry.get("ats"), entry.get("identifier")


def result_to_entry(result: DiscoveryResult) -> dict:
    return {
        "name": result.company_name,
        "ats": result.ats,
        "identifier": result.identifier,
        "enabled": True,
        "tier": "cold",
        "tags": [],
    }


def write_output_yaml(entries: list[dict], path: Path, source_input: str, now: datetime) -> None:
    """Every entry here loads through config.load_companies unchanged --
    name, ats, identifier, enabled, tier, tags. tier is always "cold": the
    user promotes a discovered entry to warm/hot by hand after reviewing it.
    """
    header = (
        f"# Discovered {now.date().isoformat()} by jobbot's discovery CLI (jobbot/discover.py)\n"
        f"# Source input file: {source_input}\n"
        f"# Every entry is tier: cold -- promote manually after review.\n\n"
    )
    body = yaml.safe_dump(entries, default_flow_style=False, sort_keys=False, allow_unicode=True)
    path.write_text(header + body, encoding="utf-8")


def append_unresolved(path: Path, result: DiscoveryResult) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(f"{result.company_name}\t{result.website}\t{result.notes}\n")


def run_discovery(
    companies: list[tuple[str, str]],
    client: httpx.Client,
    user_agent: str,
    output_path: Path,
    unresolved_path: Path,
    source_input: str,
    delay: float = DEFAULT_DELAY_SECONDS,
    verify_result: bool = True,
    append: bool = False,
    sleep: Any = time.sleep,
    limit: int | None = None,
    now: datetime | None = None,
) -> list[DiscoveryResult]:
    """Runs discover_company() over every company, writing the YAML output
    and the unresolved log incrementally (after each company, not batched at
    the end) so a Ctrl-C partway through keeps everything found so far.
    """
    if limit is not None:
        companies = companies[:limit]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    unresolved_path.parent.mkdir(parents=True, exist_ok=True)

    entries = load_existing_entries(output_path) if append else []
    seen_keys = {_entry_key(entry) for entry in entries}

    if not append:
        unresolved_path.write_text("", encoding="utf-8")

    resolved_now = now or datetime.now(UTC)
    results: list[DiscoveryResult] = []

    for company_name, website in companies:
        result = discover_company(
            website, company_name, client, user_agent,
            sleep=sleep, delay=delay, verify_result=verify_result,
        )
        results.append(result)

        if result.confidence == "none":
            append_unresolved(unresolved_path, result)
            continue

        key = (result.ats, result.identifier)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        entries.append(result_to_entry(result))
        write_output_yaml(entries, output_path, source_input, resolved_now)

    return results


def print_summary(results: list[DiscoveryResult]) -> None:
    confirmed_by_ats: dict[str, int] = {}
    probable = 0
    unresolved = 0
    total_postings = 0

    for result in results:
        if result.confidence == "confirmed":
            assert result.ats is not None
            confirmed_by_ats[result.ats] = confirmed_by_ats.get(result.ats, 0) + 1
            total_postings += result.postings_found
        elif result.confidence == "probable":
            probable += 1
        else:
            unresolved += 1

    print()
    print("=== Discovery summary ===")
    print(f"Companies attempted: {len(results)}")
    print("Confirmed:")
    if confirmed_by_ats:
        for ats, count in sorted(confirmed_by_ats.items()):
            print(f"  {ats}: {count}")
    else:
        print("  (none)")
    print(f"Probable: {probable}")
    print(f"Unresolved: {unresolved}")
    print(f"Total postings found: {total_postings}")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="-m jobbot.discover",
        description=(
            "Find which ATS a company's own careers page uses, from a list "
            "of company websites (not job boards)."
        ),
    )
    parser.add_argument(
        "--input", type=Path, required=True,
        help="Text file, one company website per line ('name,url' also accepted).",
    )
    parser.add_argument(
        "--output", type=Path, default=DEFAULT_OUTPUT_PATH,
        help=f"Where to write discovered companies as YAML (default: {DEFAULT_OUTPUT_PATH}).",
    )
    parser.add_argument(
        "--delay", type=float, default=DEFAULT_DELAY_SECONDS,
        help=f"Seconds between requests to the same host (default: {DEFAULT_DELAY_SECONDS}).",
    )
    parser.add_argument("--limit", type=int, default=None, help="Stop after N companies.")
    parser.add_argument("--verify", dest="verify", action="store_true", help="Verify each detection (default).")
    parser.add_argument("--no-verify", dest="verify", action="store_false", help="Skip verification.")
    parser.set_defaults(verify=True)
    parser.add_argument(
        "--append", action="store_true",
        help="Merge into an existing --output file instead of overwriting it.",
    )
    parser.add_argument(
        "--settings", type=Path, default=Path("settings.yaml"),
        help="Path to settings.yaml, for the User-Agent contact (default: settings.yaml).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    args = build_arg_parser().parse_args(argv)

    try:
        companies = load_input_companies(args.input)
    except OSError as exc:
        print(f"error: could not read {args.input}: {exc}", file=sys.stderr)
        return 2

    try:
        settings = load_settings(args.settings)
    except SettingsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    user_agent = f"jobbot/0.1 (+{settings.user_agent_contact})"

    unresolved_path = args.output.parent / "unresolved.txt"

    # follow_redirects=True: real company websites redirect constantly (www
    # dropped or added, http upgraded to https, a domain migration) and a
    # discovery tool that gives up on the first 3xx would miss most of them.
    client = httpx.Client(follow_redirects=True)
    try:
        results = run_discovery(
            companies,
            client,
            user_agent,
            output_path=args.output,
            unresolved_path=unresolved_path,
            source_input=str(args.input),
            delay=args.delay,
            verify_result=args.verify,
            append=args.append,
            limit=args.limit,
        )
    finally:
        client.close()

    print_summary(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
