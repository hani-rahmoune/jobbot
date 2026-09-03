"""SAP SuccessFactors Recruiting Marketing (RMK, the product formerly sold
as Jobs2Web) adapter -- sitemap-discovered, per-page lenient itemprop
extraction.

M12 Part B identified the vendor from three independent signals, confirmed
live against Eramet, Nexans, Worldline, and Capgemini (Danone, despite an
earlier session's guess, turned out to be iCIMS behind Adobe Experience
Manager -- see companies/corporate.yaml's note -- not this vendor at all):

1. The tenant's sitemap carries an
   ``<?xml-stylesheet ... href=".../view/xsl/sitemapssl.xsl"?>`` processing
   instruction.
2. robots.txt carries RMK's own generic Disallow boilerplate (ten fixed
   paths -- /applybutton/, /talentcommunity/, /preapply/, etc. -- identical
   across every tenant regardless of company).
3. The homepage's Content-Security-Policy header names an RMK infrastructure
   domain (jobs2web.com -- the literal legacy product domain, still live --
   rmkcdn.successfactors.com, or any *.sapsf.com host).

See discovery/probe_rmk.py, which checks all three, and the M12 report for
the full per-tenant table this session's sweep produced.

Part B2 ruled out a JSON API: `/search-jobs/results` (RMK's own documented
search endpoint shape) returns HTML on every tenant checked, not JSON, and
its query parameter doesn't actually filter (a real term and a nonsense one
returned the identical result set) -- the real search flow is session-bound,
the same shape M9b already rejected Phenom People's widget for. So this
adapter discovers job pages via the tenant's sitemap instead, exactly like
sitemap_jsonld.py -- reusing jobbot/sources/sitemap_discovery.py's traversal
and M11 Part A's three-layer candidate narrowing rather than duplicating it.

LENIENT EXTRACTION (M12 Part C, scope change 1): this vendor's own tenants
render wildly different markup for the exact same underlying platform --
Eramet and Capgemini wrap their fields in a complete, valid Microdata item
(`itemscope itemtype="http://schema.org/JobPosting"`); Worldline emits the
identical `itemprop="title"`/`itemprop="description"` elements with NO
enclosing itemscope/itemtype at all, just as real content, just sloppier
markup from the same platform. Requiring a valid Microdata item would silently
drop Worldline (and any other lenient-only tenant) for a technicality that
has nothing to do with whether the posting is real. So extraction here keys
on itemprop attributes directly wherever they appear in the document,
treating an enclosing itemscope/itemtype as a bonus signal, never a
precondition. See _ItemPropExtractor.

A field the markup doesn't carry is DERIVED, never invented:
- title: the `title` itemprop, falling back to the <title> tag with the
  trailing " Job Details | {Company}" boilerplate this vendor's template
  always appends stripped off. If neither yields anything, the entry is
  skipped with a logged warning rather than emitting a Job with a made-up
  title.
- description: every `description` itemprop found, concatenated in document
  order (confirmed live on Worldline: several separate spans, one per
  section -- "Who we are", "The opportunity", "Day-to-day
  responsibilities" -- all real content, none of them the whole story
  alone).
- location: the `streetAddress` itemprop when present (confirmed live to
  already read as a combined "City, country-code" string -- this vendor
  doesn't split it into separate locality/region/country the way
  sitemap_jsonld's real JSON-LD fixtures do), else the leading hyphen-
  delimited segment of the job URL's own slug (`/job/{City}-{Title}-
  {Code}/{id}/`) -- a
  best-effort fallback, not a precise parse (a multi-word city collapses to
  its first word), used only when the markup gives nothing better.
- url: the job URL as fetched -- the one selected off the sitemap, not
  necessarily the redirect target (this vendor's own job URLs commonly 302
  to a locale-suffixed path; follow_redirects=True on the injected client,
  set project-wide in run.py per M12 Part A, means the CONTENT still comes
  from the right page either way, so there's no need to chase the final
  URL just to store it).

LOCALE (M12 Part C): a sitemap listing offers no way to request a specific
locale -- the vendor's own redirect decides it, and confirmed live on
Worldline, that decision isn't reliably French even for a role located in
France (a real alternance in Puteaux redirected to an en_US-suffixed page,
not fr_FR). Nothing here gates on locale for that reason: classify_contract_type()
and the downstream filters.yaml pipeline work from the fetched page's real
content, not its URL, so an English-locale page describing a French posting
is classified and located correctly regardless of which locale the redirect
happened to choose.

OUT OF SCOPE (M12 Part C, scope change 2): Nexans is a confirmed RMK tenant
but is fully client-rendered -- even its sitemap-listed job URLs' real
content only appears after JavaScript executes; the raw HTML shows literal
unresolved template placeholders (`${translations[locale]....}`) even on the
redirect target. It belongs to rendered.py, not this adapter. Also excluded,
for a different reason: Capgemini, Alstom, and Sephora are confirmed RMK
tenants whose "sitemap.xml" is an RSS 2.0 feed (not the `<loc>`-based urlset
this adapter's discovery expects) and, at least for Capgemini, returns a
single stale-looking item on every fetch rather than a real board -- no
working discovery mechanism was found for these three this session.

The identifier is the tenant's sitemap URL, e.g.
"https://jobs.eramet.com/sitemap.xml" -- the same convention
sitemap_jsonld.py uses, and for the same reason: it's the one thing every
confirmed tenant reliably serves at a fixed, robots.txt-permitted, real URL.
"""

from __future__ import annotations

import logging
import re
from html.parser import HTMLParser
from urllib.parse import unquote, urlsplit

import httpx

from jobbot.models import Job
from jobbot.sources.base import JobSource, SourceError
from jobbot.sources.classify import classify_contract_type
from jobbot.sources.html_text import strip_html
from jobbot.sources.robots import RobotsCache
from jobbot.sources.sitemap_discovery import DEFAULT_PAGE_CAP, DEFAULT_SAMPLE_SIZE, SitemapDiscovery

logger = logging.getLogger(__name__)

# schema.org itemprop names are conventionally camelCase, but HTML attribute
# names are case-insensitive per the HTML spec and html.parser lowercases
# them for us -- compared lowercased throughout.
_WANTED_ITEMPROPS = frozenset({"title", "description", "streetaddress"})

# This vendor's own template always appends this exact suffix to every job
# page's <title> tag -- confirmed identical (modulo the company name) across
# every tenant checked. Used only as a fallback when the title itemprop
# itself isn't present.
_TITLE_TAG_SUFFIX_RE = re.compile(r"\s*Job Details\s*\|\s*.*$", re.IGNORECASE)
_TITLE_TAG_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


class _ItemPropExtractor(HTMLParser):
    """Collects every itemprop value this adapter cares about, tolerant of
    an itemprop element nested inside another tag of the SAME name --
    confirmed live and real, not hypothetical: Eramet's own `description`
    span nests further `<span style=...>` tags for inline formatting, so a
    naive "stop at the first matching close tag" regex would truncate the
    real content after its first sentence. Depth is tracked per active
    capture so the right closing tag ends it.

    Deliberately does NOT require an enclosing `itemscope itemtype=".../
    JobPosting"` -- see this module's docstring for why (Worldline's real,
    genuine content has no such wrapper at all).
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.values: dict[str, list[str]] = {}
        self._stack: list[dict] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._open_tag(tag, attrs)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._open_tag(tag, attrs)

    def _open_tag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = dict(attrs)
        itemprop = (attrs_dict.get("itemprop") or "").strip().lower()

        # An already-open capture whose OWN tag matches this one just went a
        # level deeper -- tracked so its matching close doesn't end the
        # capture early.
        for frame in self._stack:
            if frame["tag"] == tag:
                frame["depth"] += 1

        # Every currently-open capture's buffer gets this tag's raw markup
        # too, so nested structure (a <p>, an inline <span style=...>)
        # survives for strip_html() to interpret once the capture closes.
        if self._stack:
            raw = self.get_starttag_text() or ""
            for frame in self._stack:
                frame["buffer"].append(raw)

        if itemprop not in _WANTED_ITEMPROPS:
            return

        if tag == "meta":
            # Self-closing: the value is the `content` attribute itself,
            # there's no separate close tag to wait for (confirmed live:
            # this vendor renders streetAddress this way).
            self.values.setdefault(itemprop, []).append(attrs_dict.get("content") or "")
        else:
            self._stack.append({"name": itemprop, "tag": tag, "depth": 0, "buffer": []})

    def handle_data(self, text: str) -> None:
        for frame in self._stack:
            frame["buffer"].append(text)

    def handle_endtag(self, tag: str) -> None:
        for frame in reversed(self._stack):
            if frame["tag"] != tag:
                continue
            if frame["depth"] > 0:
                frame["depth"] -= 1
                frame["buffer"].append(f"</{tag}>")
            else:
                self._stack.remove(frame)
                self.values.setdefault(frame["name"], []).append("".join(frame["buffer"]))
            return
        # Not a capture's own tag -- still part of every open buffer's
        # content (e.g. a </p> closing something inside a description).
        for frame in self._stack:
            frame["buffer"].append(f"</{tag}>")


def _extract_itemprops(html_text: str) -> dict[str, list[str]]:
    """Never lets a malformed page crash the batch -- html.parser is
    already lenient by design (built for exactly this kind of best-effort,
    real-world-messy-HTML parsing), but a genuinely pathological document is
    still just "no fields found" here, not an exception the caller has to
    handle."""
    parser = _ItemPropExtractor()
    try:
        parser.feed(html_text)
    except Exception:  # noqa: BLE001 -- html.parser's own failure modes aren't documented/typed
        logger.warning("successfactors: itemprop extraction failed on malformed HTML")
        return {}
    return parser.values


def _title_from_title_tag(html_text: str) -> str:
    match = _TITLE_TAG_RE.search(html_text)
    if not match:
        return ""
    return _TITLE_TAG_SUFFIX_RE.sub("", match.group(1)).strip()


def _city_from_slug(job_url: str) -> str:
    """Best-effort, not a precise parse: the leading hyphen-delimited
    segment of /job/{City}-{Title}-{Code}/{id}/. A multi-word city collapses
    to its first word (e.g. "Ciudad" from "Ciudad-de-Mexico-...") -- accepted
    because there is no structural delimiter in this URL shape between the
    city and the title at all, only hyphens used for both, and this is only
    ever a fallback for when the markup itself carries no location."""
    path = urlsplit(job_url).path
    segments = [s for s in path.split("/") if s]
    if not segments:
        return ""
    slug = unquote(segments[-2]) if len(segments) >= 2 else unquote(segments[-1])
    first_word = slug.split("-", 1)[0]
    return first_word.strip()


class SuccessFactorsSource(JobSource):
    name = "successfactors"
    tier = 1
    first_party = True

    def __init__(
        self,
        identifier: str,
        company_name: str,
        client: httpx.Client,
        user_agent: str,
        search_terms: list[str] | None = None,
        slug_vocabulary: list[str] | None = None,
        sample_size: int = DEFAULT_SAMPLE_SIZE,
        page_cap: int = DEFAULT_PAGE_CAP,
    ) -> None:
        parsed = urlsplit(identifier)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError(
                f"successfactors: identifier must be a full https URL to the tenant's "
                f"sitemap, got {identifier!r}"
            )
        super().__init__(identifier, company_name, client, user_agent)
        self._robots = RobotsCache(client, user_agent)
        self._discovery = SitemapDiscovery(
            client,
            user_agent,
            company_name,
            source_name=self.name,
            search_terms=search_terms,
            # DEFAULT_JOB_PATH_MARKERS already includes "/job/", which is
            # this vendor's own URL shape on every tenant confirmed live --
            # no override needed.
            slug_vocabulary=slug_vocabulary,
            sample_size=sample_size,
            page_cap=page_cap,
        )

    # --- fetch_raw() -------------------------------------------------------

    def fetch_raw(
        self, etag: str | None = None, last_modified: str | None = None
    ) -> tuple[list[dict], str | None]:
        if not self._robots.allowed(self.identifier):
            raise SourceError(
                f"successfactors: robots.txt disallows fetching {self.identifier} "
                f"for {self.company_name}"
            )

        urls_to_fetch = self._discovery.discover_job_urls(self.identifier)

        postings: list[dict] = []
        for job_url in urls_to_fetch:
            entry = self._fetch_job_page(job_url)
            if entry is not None:
                postings.append(entry)

        # M8b: zero results is a valid, non-failing outcome -- a genuinely
        # quiet board, or every candidate page yielding no usable title, is
        # not an error.
        return postings, None

    def _fetch_job_page(self, job_url: str) -> dict | None:
        """A malformed/unreachable individual job page must not crash the
        whole batch -- skipped with a logged warning, same as
        sitemap_jsonld.py's own per-page handling."""
        try:
            html_text = self._discovery.fetch_text(job_url)
        except SourceError as exc:
            logger.warning(
                "successfactors: skipping unreachable job page for %s: %s",
                self.company_name, exc,
            )
            return None
        return {"url": job_url, "html": html_text}

    # --- parse() -------------------------------------------------------

    def parse(self, raw: list[dict]) -> list[Job]:
        jobs: list[Job] = []
        for entry in raw:
            job = self._parse_entry(entry)
            if job is not None:
                jobs.append(job)
        return jobs

    def _parse_entry(self, entry: dict) -> Job | None:
        job_url = entry["url"]
        itemprops = _extract_itemprops(entry["html"])

        title = (itemprops.get("title") or [""])[0].strip()
        if not title:
            title = _title_from_title_tag(entry["html"])
        if not title:
            # Never invent a value: a job page with no discoverable title
            # anywhere is skipped rather than published as a blank/garbage
            # entry.
            logger.warning(
                "successfactors: skipping %s for %s: no title found in itemprop or <title>",
                job_url, self.company_name,
            )
            return None

        description = strip_html("\n\n".join(itemprops.get("description") or []))

        street_address = (itemprops.get("streetaddress") or [""])[0].strip()
        location = street_address or _city_from_slug(job_url)

        contract_type = classify_contract_type(title, description, "")

        return Job(
            company=self.company_name,
            title=title,
            location=location,
            contract_type=contract_type,
            url=job_url,
            posted_at=None,  # not present in the itemprop set this adapter extracts
            description=description,
            source=self.name,
            external_id=job_url,
        )
