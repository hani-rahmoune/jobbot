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
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import yaml

from jobbot.settings import SettingsError, load_settings
from jobbot.sources.ashby import AshbySource
from jobbot.sources.base import JobSource, SourceError
from jobbot.sources.greenhouse import GreenhouseSource
from jobbot.sources.jsonld import JsonLdSource
from jobbot.sources.lever import LeverSource
from jobbot.sources.robots import RobotsCache

logger = logging.getLogger(__name__)

FETCH_TIMEOUT_SECONDS = 15.0
MAX_URL_ATTEMPTS = 4
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

# Checked in this fixed order (not by which appears first in the raw text) so
# a page mentioning more than one ATS resolves the same way every time.
_ATS_SIGNATURES: list[tuple[str, list[re.Pattern[str]]]] = [
    (
        "greenhouse",
        [
            re.compile(rf"boards-api\.greenhouse\.io/v1/boards/{_TOKEN}", re.IGNORECASE),
            re.compile(rf"job-boards\.greenhouse\.io/{_TOKEN}", re.IGNORECASE),
            re.compile(rf"boards\.greenhouse\.io/{_TOKEN}", re.IGNORECASE),
        ],
    ),
    (
        "lever",
        [
            re.compile(rf"api\.lever\.co/v0/postings/{_TOKEN}", re.IGNORECASE),
            re.compile(rf"jobs\.lever\.co/{_TOKEN}", re.IGNORECASE),
        ],
    ),
    (
        "ashby",
        [
            re.compile(
                rf"api\.ashbyhq\.com/posting-api/job-board/{_TOKEN}", re.IGNORECASE
            ),
            re.compile(rf"jobs\.ashbyhq\.com/{_TOKEN}", re.IGNORECASE),
        ],
    ),
]

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
    for ats_name, patterns in _ATS_SIGNATURES:
        for pattern in patterns:
            match = pattern.search(html)
            if match:
                return ats_name, match.group(1)

    for script_match in _JSONLD_SCRIPT_RE.finditer(html):
        if "JobPosting" in script_match.group(1):
            return "jsonld", page_url

    return None, None


_ADAPTER_CLASSES: dict[str, type[JobSource]] = {
    "greenhouse": GreenhouseSource,
    "lever": LeverSource,
    "ashby": AshbySource,
    "jsonld": JsonLdSource,
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


def discover_company(
    website: str,
    company_name: str,
    client: httpx.Client,
    user_agent: str,
    sleep: Any = time.sleep,
    delay: float = DEFAULT_DELAY_SECONDS,
    verify_result: bool = True,
) -> DiscoveryResult:
    """Fetches candidate careers URLs in order until detect_ats finds
    something, verifying the result unless `verify_result` is False."""
    robots = RobotsCache(client, user_agent)
    candidates = candidate_careers_urls(website)[:MAX_URL_ATTEMPTS]

    detected_ats: str | None = None
    detected_identifier: str | None = None
    detected_url: str | None = None

    for index, url in enumerate(candidates):
        if index > 0:
            sleep(delay)

        if not robots.allowed(url):
            logger.info("discover: robots.txt disallows %s, skipping", url)
            continue

        try:
            response = client.get(
                url, headers={"User-Agent": user_agent}, timeout=FETCH_TIMEOUT_SECONDS
            )
        except httpx.HTTPError as exc:
            logger.info("discover: fetch failed for %s: %s", url, exc)
            continue

        if response.status_code != 200:
            logger.info("discover: %s returned HTTP %d", url, response.status_code)
            continue

        ats, identifier = detect_ats(response.text, url)
        if ats is not None:
            detected_ats, detected_identifier, detected_url = ats, identifier, url
            break

    if detected_ats is None or detected_identifier is None:
        return DiscoveryResult(
            company_name=company_name,
            website=website,
            ats=None,
            identifier=None,
            careers_url=None,
            postings_found=0,
            confidence="none",
            notes=f"no ATS signature found in {len(candidates)} attempts",
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
