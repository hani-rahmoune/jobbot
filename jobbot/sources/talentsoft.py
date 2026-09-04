"""Cegid Talentsoft adapter -- server-rendered HTML listing pages.

M9 coverage expansion. Talentsoft's real "Recruiting Front Office" REST API
(developers.cegid.com) requires a Token issued to the employer's own
integration partners -- not a public, unauthenticated surface, so it isn't
used here. What every Talentsoft-powered career site DOES expose publicly,
with no auth and no JavaScript required, is its own candidate-facing job
listing page: confirmed live against Credit Agricole CIB
(casa-cacib-recrute.talent-soft.com, 313 open postings) and its sibling LCL
(casa-lcl-recrute.talent-soft.com, 193), whose `/job/list-of-all-jobs.aspx`
page returns real job listings directly in the server-rendered HTML --
title, a direct link, and a contract-type/location badge list -- paginated.
robots.txt on both tenants carries no rules at all (everything allowed);
checked live via RobotsCache, same as jsonld.py, before every fetch.

This is the "HTML-only listing page" path CLAUDE.md's Part A allows
specifically because the content is genuinely server-rendered, not injected
by JavaScript -- confirmed by fetching the page with plain httpx (no JS
engine) and finding the job titles and links already present in the
response body.

Talentsoft's own front-end framework renders every tenant's listing page
from one of two generic, interchangeable templates -- confirmed live: CACIB
defaults to the "card" template (`ts-offer-card` CSS classes, 100 postings
per page), LCL to the "list" template (`ts-offer-list-item`, only 10 per
page) -- admin-selected per tenant, with no request parameter found to
force one or the other. Both are Talentsoft's own product markup (not
something each employer custom-builds) and share the same structural shape
(a title link followed by one `<ul class="ts-offer-...">` of badges), so
_OFFER_CARD_RE matches either, and page size is measured from page 1's own
result count rather than assumed as a platform-wide constant (see
_fetch_all_pages) -- necessary specifically because that count is NOT
uniform across tenants.

Pagination is NOT the usual "stop at a short page" heuristic used by every
other adapter in this codebase -- confirmed live that requesting a page
number past the real last page does not return an empty/short page, it
silently WRAPS AROUND and re-serves page 1. Instead, the real total posting
count is parsed from the page's own "Nombre de resultats : N offre(s)" text
(present on every page, including page 1), and combined with page 1's own
measured size to compute exactly how many more pages to request -- no
guessing, and immune to the wraparound. If that count can't be found for
some reason, only page 1 is used and a warning is logged, rather than risk
silently re-scraping page 1's own postings under the belief they're new.

Server-side search (M9's search_terms): the listing page's own free-text
search box posts through full ASP.NET view-state machinery, but a much
simpler `?Keywords=` GET parameter was confirmed live to produce the exact
same server-side narrowing (CACIB: 313 -> 45 for "alternance"; LCL: 193 ->
2) without any of that -- same optional, config-driven, one-query-per-term,
dedup design as every other adapter's search_terms.

M18 Part B: a THIRD template variant exists, confirmed live on Enedis
(enedis-recrute.talent-soft.com) -- its listing page reports a real,
nonzero "N offres" count but contains no `ts-offer-card`/`ts-offer-list-
item` markup anywhere (a newer, more JS-driven rendering this project's
scraper was never built to read, and Playwright confirmed no postings
appear even after full JS execution or via any XHR/fetch call the page
itself makes). Investigated for an alternative server-rendered route
before reaching for a browser (CLAUDE.md's own preference, and this
project's explicit rule against adding more rendered sources): Talentsoft
ships its own platform-wide RSS export, `/handlers/offerRss.ashx`,
confirmed live and unrelated to the broken listing template -- real,
well-formed RSS with a genuine `<item>` per posting (title, link,
category badges, a full HTML description, and a `pubDate`), the SAME
`?Keywords=` search parameter as the classic page, and robots.txt-allowed
on every tenant checked.

Its one real limitation: the un-narrowed feed always returns exactly the
20 most recent postings, with no page/offset parameter found that changes
that (confirmed: `page=`, `Take=`, `PageIndex=` all silently ignored) --
unlike the classic HTML template's own genuine pagination. A `?Keywords=`-
narrowed query, however, is NOT capped at 20 the same way (confirmed live:
"alternance" -> 11, "stage" -> 4 on Enedis, both under the ceiling with no
sign of truncation) -- which matters in practice because search_terms is
what every real poll actually uses (settings.yaml always configures the
four real terms), so the plain full-board crawl's 20-item ceiling is a
real but secondary limitation, logged whenever the RSS fallback's own
result count comes in under the classic page's own reported total.

Auto-detected, not configured: `_fetch_all_pages()` falls back to this
route only when the classic template's own offer-card regex matches
NOTHING despite the page reporting a nonzero total -- every tenant whose
classic template already works (confirmed: CACIB, LCL, CEA, BRGM, and
every other tenant in companies/*.yaml as of M18) is completely
unaffected, since that condition never fires for them.
"""

from __future__ import annotations

import html
import logging
import math
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import httpx

from jobbot.models import Job
from jobbot.sources.base import JobSource, SourceError, SourceNotFoundError
from jobbot.sources.classify import classify_contract_type
from jobbot.sources.html_text import strip_html
from jobbot.sources.robots import RobotsCache

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 15.0
MAX_ATTEMPTS = 2  # one request, one retry on 5xx/timeout, per page
# Real per-page size varies by tenant template (100 or 10 -- see module
# docstring), so unlike every other adapter these caps bound page COUNT,
# not a fixed posting count: 40 pages is 4000 postings on the "card"
# template but only 400 on the "list" one -- comfortably above both real
# tenants measured for this milestone (313 and 193).
MAX_PAGES = 40  # plain full-board crawl, no search_terms
MAX_PAGES_PER_SEARCH_TERM = 10  # one narrowed query, far fewer results regardless of template

# Every Talentsoft career site is served in French for this project (the
# whole project's own scope is the French market -- see CLAUDE.md's opening
# line), via Microsoft's standard Locale ID query parameter -- an
# internationalization protocol constant, not a location/keyword search
# term, so it's fixed here the same way Workday's `limit` field name is
# fixed rather than user-configurable.
_LCID_FRENCH = 1036

_LIST_PATH = "/job/list-of-all-jobs.aspx"

_TOTAL_OFFERS_RE = re.compile(r"(\d+)\s*offre")

# M18 Part B: the RSS-export fallback route -- see the module docstring's
# own section for why this exists and its one real limitation (the
# un-narrowed feed is capped at the 20 most recent postings).
_RSS_PATH = "/handlers/offerRss.ashx"
# Each <item>'s <link> carries the real offer id as a query parameter
# (e.g. "...?idOffre=180448&..."), the RSS export's own equivalent of the
# classic template's trailing "_{id}.aspx" -- a structural URL convention,
# not a user search preference.
_RSS_OFFER_ID_RE = re.compile(r"[?&]idOffre=(\d+)")
# Every RSS title seen is prefixed with the offer's own internal reference
# number ("2026-180448 - Electrotechnicien F/H") -- structural noise from
# the export, not part of the real title.
_RSS_REFERENCE_PREFIX_RE = re.compile(r"^\d{4}-\d+\s*-\s*")
# The RSS description's own labeled "Ville : {city}" line is a more
# reliable location signal than assuming a fixed category position (which
# Talentsoft's admin-configurable "profile fields" don't guarantee stays
# in the same order on every tenant) -- confirmed live on every Enedis
# item checked.
_RSS_VILLE_LABEL_RE = re.compile(r"(?im)^Ville\s*:\s*(.+)$")

# Talentsoft ships two interchangeable listing templates, admin-selected per
# tenant, not by URL parameter (confirmed live: Credit Agricole CIB defaults
# to the "card" template -- CSS classes prefixed `ts-offer-card` -- while
# Credit Agricole's own LCL subsidiary, same platform, defaults to the
# "list" template -- `ts-offer-list-item` -- and no query parameter was
# found live to force one or the other). Both wrap a posting's title link
# and its contract-type/location badge list in the same structural shape,
# just under different class-name prefixes, so one regex matches either.
#
# M16 Part B: the title group matches ANY content up to the next `</a>`
# (not just `[^<]+?`, plain text) -- confirmed live necessary for BRGM's own
# tenant, whose "top offer" postings wrap a decorative, empty icon `<div>`
# INSIDE the title link before the actual title text
# (`<a class="ts-offer-list-item__title-link">
#     <div class="...top-offer-picto..."><div class="square"></div></div>
#     Technicien superieur mesures physiques F/H</a>`) -- the old
# text-only pattern couldn't match past that nested tag at all, silently
# dropping every "top offer" posting on any tenant that uses this template
# feature. The captured group is cleaned with strip_html() below (which
# drops any nested markup and empty lines, e.g. from that decorative div)
# rather than the plain html.unescape() a text-only capture only needed.
_OFFER_CARD_RE = re.compile(
    r'ts-offer-(?:card|list-item)__title-link[^>]*\shref="(?P<href>[^"]*)"[^>]*>'
    r"\s*(?P<title>.+?)\s*</a>.*?"
    r'<ul\s+class="ts-offer-[^"]*"[^>]*>\s*(?P<badges>.*?)</ul>',
    re.DOTALL,
)
_BADGE_LI_RE = re.compile(r"<li[^>]*>([^<]*)</li>")
_TRAILING_ID_RE = re.compile(r"_(\d+)\.aspx$")


class TalentsoftSource(JobSource):
    name = "talentsoft"
    tier = 1
    first_party = True

    def __init__(
        self,
        identifier: str,
        company_name: str,
        client: httpx.Client,
        user_agent: str,
        search_terms: list[str] | None = None,
    ) -> None:
        parsed = urlsplit(identifier)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError(
                f"talentsoft: identifier must be an https URL "
                f"(e.g. 'https://casa-cacib-recrute.talent-soft.com'), got {identifier!r}"
            )
        super().__init__(identifier, company_name, client, user_agent)
        self._base_url = identifier.rstrip("/")
        self._robots = RobotsCache(client, user_agent)
        self.search_terms = list(search_terms) if search_terms else []

    def fetch_raw(
        self, etag: str | None = None, last_modified: str | None = None
    ) -> tuple[list[dict], str | None]:
        list_url = f"{self._base_url}{_LIST_PATH}"
        if not self._robots.allowed(list_url):
            raise SourceError(
                f"talentsoft: robots.txt disallows fetching {list_url} for {self.company_name}"
            )

        # M8b: zero results is a valid, non-failing outcome (see
        # run.process_source()) -- both branches below may legitimately
        # return an empty list.
        if self.search_terms:
            return self._fetch_by_search_terms(list_url), None

        return self._fetch_all_pages(list_url, keywords="", max_pages=MAX_PAGES), None

    def _fetch_by_search_terms(self, list_url: str) -> list[dict]:
        postings_by_id: dict[str, dict] = {}
        for term in self.search_terms:
            for posting in self._fetch_all_pages(
                list_url, keywords=term, max_pages=MAX_PAGES_PER_SEARCH_TERM
            ):
                key = posting["id"]
                if key in postings_by_id:
                    continue
                postings_by_id[key] = posting
        return list(postings_by_id.values())

    def _fetch_all_pages(self, list_url: str, keywords: str, max_pages: int) -> list[dict]:
        # max_pages has no default deliberately: MAX_PAGES is read at each
        # call site instead (see fetch_raw()) rather than bound once here at
        # function-definition time, so a test's monkeypatch of the module
        # constant actually takes effect (a `= MAX_PAGES` default parameter
        # value is frozen at import time and would silently ignore it).
        first_page_html = self._fetch_page_html(list_url, page=1, keywords=keywords)
        postings = _extract_offer_cards(first_page_html, self._base_url)

        total = _extract_total_offers(first_page_html)

        # M18 Part B: a real, nonzero total with zero matched cards means
        # this tenant's classic template isn't one _OFFER_CARD_RE
        # recognizes at all (confirmed live: Enedis) -- not a genuinely
        # empty board, which would report total == 0. Fall back to the
        # platform's own RSS export rather than reporting nothing.
        if not postings and total:
            return self._fetch_rss_postings(keywords, total)

        if total is None:
            if postings:
                logger.warning(
                    "talentsoft: %s (%s) could not find a total-results count, "
                    "only page 1 was fetched -- more postings may exist",
                    self.company_name, self.identifier,
                )
            return postings

        # Page size is NOT a fixed platform constant -- confirmed live that
        # it differs by which of Talentsoft's two listing templates a tenant
        # is configured with (100 for the "card" template, 10 for the
        # "list" template -- see _OFFER_CARD_RE's docstring), with no
        # request parameter found to force one. Rather than hardcode either
        # number and risk under-paginating a "list"-template tenant, the
        # actual size of page 1 IS the page size for every subsequent page
        # of this same query.
        if total <= 0 or not postings:
            return postings
        page_size = len(postings)

        total_pages = min(max_pages, math.ceil(total / page_size))
        if math.ceil(total / page_size) > max_pages:
            logger.warning(
                "talentsoft: %s (%s) keywords %r hit the %d-page cap, "
                "more postings may exist (total reported: %d)",
                self.company_name, self.identifier, keywords, max_pages, total,
            )

        for page in range(2, total_pages + 1):
            page_html = self._fetch_page_html(list_url, page=page, keywords=keywords)
            postings.extend(_extract_offer_cards(page_html, self._base_url))

        return postings

    def _fetch_page_html(self, list_url: str, page: int, keywords: str) -> str:
        params: dict[str, str | int] = {"LCID": _LCID_FRENCH, "page": page}
        if keywords:
            params["Keywords"] = keywords
        return self._get_text(list_url, params, context=f"page {page}")

    def _fetch_rss_postings(self, keywords: str, total: int) -> list[dict]:
        """M18 Part B fallback -- see the module docstring's own section.
        Only reached when the classic template's own offer-card regex
        matched nothing despite a nonzero total (see _fetch_all_pages)."""
        rss_url = f"{self._base_url}{_RSS_PATH}"
        if not self._robots.allowed(rss_url):
            raise SourceError(
                f"talentsoft: robots.txt disallows fetching {rss_url} for {self.company_name}"
            )

        params: dict[str, str] = {}
        if keywords:
            params["Keywords"] = keywords
        rss_text = self._get_text(rss_url, params, context="RSS fallback")
        postings = _parse_rss_feed_text(rss_text)

        # The un-narrowed feed is capped at the 20 most recent postings with
        # no discovered pagination (see module docstring) -- a genuinely
        # narrowed query isn't usually affected in practice, but report
        # honestly whenever fewer postings came back than the classic
        # template's own count says exist, regardless of why.
        if len(postings) < total:
            logger.warning(
                "talentsoft: %s (%s) RSS fallback keywords %r returned %d of %d "
                "reported offers -- this mode has no discovered pagination, "
                "more postings may exist",
                self.company_name, self.identifier, keywords, len(postings), total,
            )

        return postings

    def _get_text(self, url: str, params: dict[str, str | int], context: str) -> str:
        """One GET, with the same retry-once-on-5xx/timeout contract every
        other adapter follows. `context` is only for log lines (e.g. "page 3"
        or "RSS fallback"), to tell which caller a given warning came from."""
        headers = {"User-Agent": self.user_agent}

        response: httpx.Response | None = None
        error: Exception | None = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = self.client.get(
                    url, params=params, headers=headers, timeout=TIMEOUT_SECONDS
                )
            except httpx.TimeoutException as exc:
                error = exc
                logger.warning(
                    "talentsoft: timeout fetching %s (%s) for %s (attempt %d/%d)",
                    url, context, self.company_name, attempt, MAX_ATTEMPTS,
                )
                continue

            if response.status_code >= 500:
                error = SourceError(
                    f"talentsoft: {self.company_name} ({url}) "
                    f"returned HTTP {response.status_code}"
                )
                logger.warning(
                    "talentsoft: HTTP %d fetching %s (%s) for %s (attempt %d/%d)",
                    response.status_code, url, context, self.company_name, attempt, MAX_ATTEMPTS,
                )
                continue

            error = None
            break

        if error is not None:
            raise SourceError(
                f"talentsoft: failed to fetch {url} ({context}) for {self.company_name} "
                f"after {MAX_ATTEMPTS} attempts"
            ) from error

        # Never let an httpx exception escape this method -- every failure
        # mode here is one of our own SourceError subclasses.
        if response.status_code == 404:
            raise SourceNotFoundError(
                f"talentsoft: board not found for {self.company_name} "
                f"({self.identifier}): {url} returned 404"
            )
        if response.status_code >= 400:
            raise SourceError(
                f"talentsoft: {self.company_name} ({url}) "
                f"returned HTTP {response.status_code}"
            )

        return response.text

    def parse(self, raw: list[dict]) -> list[Job]:
        jobs: list[Job] = []
        for entry in raw:
            try:
                jobs.append(self._parse_entry(entry))
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning(
                    "talentsoft: skipping malformed entry for %s: %s", self.company_name, exc
                )
        return jobs

    def _parse_entry(self, entry: dict) -> Job:
        if entry.get("kind") == "rss":
            return self._parse_rss_entry(entry)
        return self._parse_card_entry(entry)

    def _parse_card_entry(self, entry: dict) -> Job:
        title = entry["title"]
        external_id = entry["id"]
        if not external_id:
            raise ValueError("id is empty")
        badges = entry.get("badges") or []
        employment_hint = badges[0] if badges else ""
        location = ", ".join(badges[1:]) if len(badges) > 1 else ""
        contract_type = classify_contract_type(title, "", employment_hint)

        return Job(
            company=self.company_name,
            title=title,
            location=location,
            contract_type=contract_type,
            url=entry["url"],
            posted_at=None,  # not present on the listing page; no per-job fetch, see docstring
            description="",
            source=self.name,
            external_id=external_id,
        )

    def _parse_rss_entry(self, entry: dict) -> Job:
        """M18 Part B. Unlike the card path above, the RSS export carries a
        real description and a real posted date -- richer than what the
        classic template's listing page ever exposed, since that path never
        needed a per-job fetch either."""
        title = entry["title"]
        external_id = entry["id"]
        if not external_id:
            raise ValueError("id is empty")
        categories = entry.get("categories") or []
        # Position 0 is the profession/department family, not a contract-
        # type signal -- confirmed live (see module docstring) that position
        # 1 is consistently the contract-type category ("CDI", "Alternance").
        employment_hint = categories[1] if len(categories) > 1 else ""
        description = entry.get("description") or ""
        location = _location_from_rss_description(description) or (
            categories[-1] if categories else ""
        )
        contract_type = classify_contract_type(title, description, employment_hint)

        return Job(
            company=self.company_name,
            title=title,
            location=location,
            contract_type=contract_type,
            url=entry["url"],
            posted_at=entry.get("posted_at"),
            description=description,
            source=self.name,
            external_id=external_id,
        )


def _extract_total_offers(page_html: str) -> int | None:
    match = _TOTAL_OFFERS_RE.search(page_html)
    return int(match.group(1)) if match else None


def _extract_offer_cards(page_html: str, base_url: str) -> list[dict]:
    postings = []
    for match in _OFFER_CARD_RE.finditer(page_html):
        href = match.group("href")
        title = strip_html(match.group("title"))
        badges = [html.unescape(b).strip() for b in _BADGE_LI_RE.findall(match.group("badges"))]
        id_match = _TRAILING_ID_RE.search(href)
        postings.append(
            {
                "id": id_match.group(1) if id_match else href,
                "title": title,
                "url": base_url + href if href.startswith("/") else href,
                "badges": badges,
            }
        )
    return postings


def _location_from_rss_description(description: str) -> str:
    match = _RSS_VILLE_LABEL_RE.search(description)
    return match.group(1).strip() if match else ""


def _parse_rss_pub_date(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None


def _parse_rss_feed_text(rss_text: str) -> list[dict]:
    """A real RSS document is well-formed XML by spec (unlike arbitrary
    HTML), so a real parser is used here rather than regex -- same
    precedent as successfactors.py's own RSS mode. ET.ParseError on
    genuinely malformed content becomes a SourceError; a well-formed but
    empty feed (no <item>s) is a legitimate M8b zero-result, not an error.
    """
    try:
        root = ET.fromstring(rss_text)
    except ET.ParseError as exc:
        raise SourceError(f"talentsoft: malformed RSS feed: {exc}") from exc

    postings = []
    for item in root.iter("item"):
        link = (item.findtext("link") or "").strip()
        raw_title = (item.findtext("title") or "").strip()
        title = _RSS_REFERENCE_PREFIX_RE.sub("", raw_title).strip()
        categories = [c.text.strip() for c in item.findall("category") if c.text]
        description = strip_html(item.findtext("description") or "")
        id_match = _RSS_OFFER_ID_RE.search(link)
        postings.append(
            {
                "kind": "rss",
                "id": id_match.group(1) if id_match else link,
                "title": title,
                "url": link,
                "categories": categories,
                "description": description,
                "posted_at": _parse_rss_pub_date(item.findtext("pubDate")),
            }
        )
    return postings
