"""Probes a company name against four self-serve ATS vendors' own public,
multi-tenant board APIs, by guessing the tenant slug -- distinct from
probe_vendor.py, which fingerprints a vendor from signals on a company's OWN
already-resolved domain. These four need no domain at all: the board lives
entirely on the vendor's own API host, keyed by a slug the company chose at
signup, so a plausible guess is often enough on its own.

M16 Part A: Greenhouse, Lever, Ashby, and SmartRecruiters are each a single
unauthenticated GET, and the French data/AI scale-up scene lives almost
entirely on them (all four are self-serve products startups pick without
vendor sales calls, unlike SuccessFactors/Workday/Talentsoft's enterprise
procurement). This had never been swept systematically before this
milestone.

    Greenhouse:      boards-api.greenhouse.io/v1/boards/{slug}/jobs
    Lever:           api.lever.co/v0/postings/{slug}?mode=json
    Ashby:           api.ashbyhq.com/posting-api/job-board/{slug}
    SmartRecruiters: api.smartrecruiters.com/v1/companies/{slug}/postings

A hit is unambiguous for all four: a 200 with a real, parseable postings
list. A miss (wrong slug, or the company doesn't use that vendor) is a 404
or a 200 wrapping an empty/error body, treated identically -- this script
makes no claim about WHY a slug missed, only whether it hit. This is
discovery-phase tooling, not a jobbot/ adapter: it never writes to
companies/*.yaml and doesn't claim a hit's postings are real/relevant/
French -- that's what the real adapter's own fetch() (and a human reading
its output) is for, same division of labor as probe_vendor.py.

Usage:
    uv run python discovery/probe_ats.py "Mistral AI" "Hugging Face"
    uv run python discovery/probe_ats.py --slug mistralai "Mistral AI"
    uv run python discovery/probe_ats.py --input discovery/seeds/companies.txt
"""

from __future__ import annotations

import argparse
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from jobbot.settings import SettingsError, load_settings

TIMEOUT_SECONDS = 15.0

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def slug_variants(name: str, extra: list[str] | None = None) -> list[str]:
    """Obvious slug guesses from a company's display name, in the order a
    human would try them: accent-stripped and lowercased first (every
    variant below builds on this), then no-spaces, then hyphenated, then
    alphanumeric-only (strips ALL punctuation, including hyphens -- some
    tenants register e.g. "backmarket" where others would use "back-market").
    Explicit `extra` guesses (a known or suspected real slug) are tried
    first, ahead of anything derived from the name.
    """
    normalized = unicodedata.normalize("NFKD", name)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    lowered = ascii_only.lower().strip()

    no_spaces = re.sub(r"\s+", "", lowered)
    hyphenated = re.sub(r"\s+", "-", lowered)
    alnum_only = _NON_ALNUM_RE.sub("", lowered)

    ordered = list(extra or []) + [no_spaces, hyphenated, alnum_only]
    seen: set[str] = set()
    result = []
    for candidate in ordered:
        if candidate and candidate not in seen:
            seen.add(candidate)
            result.append(candidate)
    return result


@dataclass
class AtsHit:
    vendor: str
    slug: str
    posting_count: int


@dataclass
class ProbeResult:
    company: str
    hits: list[AtsHit] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _get(client: httpx.Client, url: str, user_agent: str) -> httpx.Response | None:
    """Never raises -- one bad slug or a transient network error must not
    stop the sweep over the rest of the list."""
    try:
        return client.get(
            url, headers={"User-Agent": user_agent}, timeout=TIMEOUT_SECONDS, follow_redirects=True
        )
    except httpx.HTTPError:
        return None


def _check_greenhouse(client: httpx.Client, slug: str, user_agent: str) -> int | None:
    response = _get(
        client, f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", user_agent
    )
    if response is None or response.status_code != 200:
        return None
    try:
        jobs = response.json().get("jobs")
    except ValueError:
        return None
    return len(jobs) if isinstance(jobs, list) else None


def _check_lever(client: httpx.Client, slug: str, user_agent: str) -> int | None:
    response = _get(client, f"https://api.lever.co/v0/postings/{slug}?mode=json", user_agent)
    if response is None or response.status_code != 200:
        return None
    try:
        postings = response.json()
    except ValueError:
        return None
    return len(postings) if isinstance(postings, list) else None


def _check_ashby(client: httpx.Client, slug: str, user_agent: str) -> int | None:
    response = _get(
        client, f"https://api.ashbyhq.com/posting-api/job-board/{slug}", user_agent
    )
    if response is None or response.status_code != 200:
        return None
    try:
        jobs = response.json().get("jobs")
    except ValueError:
        return None
    return len(jobs) if isinstance(jobs, list) else None


def _check_smartrecruiters(client: httpx.Client, slug: str, user_agent: str) -> int | None:
    """Unlike the other three, this API returns 200 with `totalFound: 0,
    content: []` for a slug that doesn't correspond to any real company at
    all -- confirmed live -- so a 200 alone is not evidence of a hit here.
    Only a NONZERO count counts; zero is treated identically to a miss
    (indistinguishable, from this API alone, from a company that genuinely
    has no current postings, which isn't actionable for us either way)."""
    response = _get(
        client, f"https://api.smartrecruiters.com/v1/companies/{slug}/postings", user_agent
    )
    if response is None or response.status_code != 200:
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    total = body.get("totalFound")
    count = total if isinstance(total, int) else None
    if count is None:
        content = body.get("content")
        count = len(content) if isinstance(content, list) else None
    return count if count else None


_CHECKERS = (
    ("greenhouse", _check_greenhouse),
    ("lever", _check_lever),
    ("ashby", _check_ashby),
    ("smartrecruiters", _check_smartrecruiters),
)


def probe_company(
    company: str, client: httpx.Client, user_agent: str, extra_slugs: list[str] | None = None
) -> ProbeResult:
    """The one function other code should import. Tries every slug variant
    against every vendor -- a company can plausibly hit more than one vendor
    for the SAME reason a human would sanity-check by hand (e.g. an old
    Greenhouse board kept alive alongside a new Ashby one), so this doesn't
    stop at the first hit."""
    result = ProbeResult(company=company)
    for slug in slug_variants(company, extra_slugs):
        for vendor, checker in _CHECKERS:
            count = checker(client, slug, user_agent)
            if count is not None:
                result.hits.append(AtsHit(vendor=vendor, slug=slug, posting_count=count))
    return result


def _read_companies(args: argparse.Namespace) -> list[str]:
    companies = list(args.companies)
    if args.input is not None:
        lines = args.input.read_text(encoding="utf-8").splitlines()
        companies.extend(line.strip() for line in lines if line.strip() and not line.startswith("#"))
    return companies


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="discovery/probe_ats.py",
        description="Check whether a company has a real board on Greenhouse, Lever, "
        "Ashby, or SmartRecruiters, by guessing its tenant slug.",
    )
    parser.add_argument("companies", nargs="*", help='Company display names, e.g. "Mistral AI"')
    parser.add_argument("--input", type=Path, default=None, help="Text file, one company per line.")
    parser.add_argument(
        "--slug", action="append", default=[], dest="extra_slugs",
        help="An extra slug guess to try first, ahead of derived ones (repeatable).",
    )
    parser.add_argument(
        "--settings", type=Path, default=Path("settings.yaml"),
        help="Path to settings.yaml, for the User-Agent contact (default: settings.yaml).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    companies = _read_companies(args)
    if not companies:
        print("No companies given (positionally or via --input).", file=sys.stderr)
        return 2

    try:
        settings = load_settings(args.settings)
    except SettingsError as exc:
        print(f"settings error: {exc}", file=sys.stderr)
        return 2

    user_agent = f"jobbot/0.1 (+{settings.user_agent_contact})"

    with httpx.Client() as client:
        for company in companies:
            result = probe_company(company, client, user_agent, args.extra_slugs)
            if result.hits:
                for hit in result.hits:
                    print(f"{company}: HIT {hit.vendor} slug={hit.slug!r} postings={hit.posting_count}")
            else:
                print(f"{company}: no hit")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
