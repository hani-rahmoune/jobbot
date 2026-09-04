"""Probes a hostname for known ATS/talent-platform vendor fingerprints,
cheaply and without ever fetching from an aggregator or directory (CLAUDE.md
rule 3: every host probed here must already be a resolved careers domain,
found by a human or by jobbot/discover.py's own resolution chain).

M12 Part B4 built this (as probe_rmk.py, renamed here in M14 Part A once it
covered a second vendor) for SAP SuccessFactors Recruiting Marketing (RMK,
the product formerly sold as Jobs2Web), using three independent signals
derived by directly inspecting five confirmed/rejected employers (Eramet,
Nexans, Worldline, Capgemini confirmed; Danone rejected -- see the M12
report):

1. rmk_sitemap_stylesheet: the tenant's sitemap carries an
   ``<?xml-stylesheet ... href=".../view/xsl/sitemapssl.xsl"?>`` processing
   instruction, right after the XML declaration.
2. rmk_robots_boilerplate: robots.txt carries RMK's own generic Disallow
   list -- ten fixed paths, byte-identical across every tenant regardless
   of company, language, or hosting region.
3. rmk_csp_domain: the homepage's Content-Security-Policy header names one
   of RMK's own infrastructure domains (jobs2web.com -- the literal legacy
   product domain, still live -- rmkcdn.successfactors.com, or *.sapsf.com).

M14 Part A added Eightfold AI, found via Kering's real board
(careers.kering.com), confirmed with two signals from the same homepage
fetch the RMK check already makes (no extra request needed):

4. eightfold_csp_domain: the CSP header names eightfold.ai or
   eightfold-gov.ai.
5. eightfold_ef_headers: any response header named X-EF-Trace-ID, X-EF-IID,
   or X-EF-NS is present (case-insensitive; httpx normalizes header names
   to lowercase internally so the comparison is done that way).

A third, documentary-only signal for Eightfold -- individual job URLs
shaped "/careers/{numeric ID}/" -- isn't checked here: unlike the other
four, it needs a real job URL to test against, which an arbitrary
not-yet-confirmed host doesn't hand us. It's how a human confirms a hit by
eye once eightfold.py's own adapter is pointed at the tenant, not something
this probe can check blind.

Any ONE signal for a vendor is enough to call a host confirmed for that
vendor. This is discovery-phase tooling, not a jobbot/ adapter: it never
writes to companies/*.yaml itself and makes no claim about whether a
confirmed tenant actually carries real, fetchable, relevant postings --
that's what the real adapter's own fetch() (and a human reading its output)
is for.

Usage:
    uv run python discovery/probe_vendor.py careers.example.com jobs.example.fr
    uv run python discovery/probe_vendor.py --input discovery/seeds/hosts.txt
    uv run python discovery/probe_vendor.py --settings settings.yaml jobs.eramet.com
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from jobbot.settings import SettingsError, load_settings

TIMEOUT_SECONDS = 15.0

# Byte-identical across every RMK tenant checked (Eramet, Nexans, Worldline,
# Capgemini) -- the platform's own generic crawler-etiquette boilerplate, not
# anything a tenant customizes per company.
_ROBOTS_BOILERPLATE_PATHS = (
    "/applybutton/",
    "/talentcommunity/",
    "/mobile/talentcommunity/",
    "/emailsubscribe/",
    "/email/image/",
    "/services/",
    "/preapply/",
    "/error",
    "/unsubscribe/",
    "/reset/",
)

_SITEMAP_DECLARATION_RE = re.compile(r"(?im)^Sitemap:\s*(\S+)")
_SITEMAP_STYLESHEET_RE = re.compile(r'<\?xml-stylesheet[^>]*href="[^"]*view/xsl/sitemapssl\.xsl"')

_RMK_CSP_DOMAINS = ("jobs2web.com", "rmkcdn.successfactors.com", "sapsf.com")
_EIGHTFOLD_CSP_DOMAINS = ("eightfold.ai", "eightfold-gov.ai")
_EIGHTFOLD_HEADER_NAMES = ("x-ef-trace-id", "x-ef-iid", "x-ef-ns")

# The stylesheet processing instruction is always the very first thing in the
# document, right after the XML declaration -- reading a small prefix instead
# of the full body matters here specifically because a real tenant's sitemap
# can be thousands of <url> entries (Worldline's is; Eramet's isn't), and this
# probe has no reason to pay for the rest of it just to answer a yes/no.
_SITEMAP_PREFIX_BYTES = 4096


@dataclass
class VendorSignals:
    """One host's probe result across every vendor this script knows about.
    `notes` records why a signal came back False when that's worth knowing
    (a fetch failure, a non-200) rather than just silently reading as "no"
    the same way a genuine absence would."""

    host: str
    rmk_sitemap_stylesheet: bool = False
    rmk_robots_boilerplate: bool = False
    rmk_csp_domain: bool = False
    eightfold_csp_domain: bool = False
    eightfold_ef_headers: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def rmk_confirmed(self) -> bool:
        return self.rmk_sitemap_stylesheet or self.rmk_robots_boilerplate or self.rmk_csp_domain

    @property
    def eightfold_confirmed(self) -> bool:
        return self.eightfold_csp_domain or self.eightfold_ef_headers

    @property
    def vendor(self) -> str:
        """A human-readable verdict. Both firing at once hasn't been seen in
        practice, but reported honestly rather than picking one arbitrarily
        if it ever happens."""
        if self.rmk_confirmed and self.eightfold_confirmed:
            return "successfactors_rmk+eightfold (both fired -- unexpected, check by hand)"
        if self.rmk_confirmed:
            return "successfactors_rmk"
        if self.eightfold_confirmed:
            return "eightfold"
        return "no match"


def _get(client: httpx.Client, url: str, user_agent: str) -> httpx.Response | None:
    """Every request in this probe follows the same never-raise contract:
    a failed fetch just means that signal comes back False, not a crash of
    the whole sweep -- one unreachable host must not stop the others."""
    try:
        return client.get(
            url, headers={"User-Agent": user_agent}, timeout=TIMEOUT_SECONDS, follow_redirects=True
        )
    except httpx.HTTPError:
        return None


def _check_robots(
    host: str, client: httpx.Client, user_agent: str, signals: VendorSignals
) -> str | None:
    """Returns the declared Sitemap: URL if robots.txt names one, so the
    stylesheet check below doesn't have to guess a path when it doesn't need
    to. A robots.txt fetch failure or non-200 is not itself a signal either
    way (same convention as RobotsCache: absence isn't informative)."""
    response = _get(client, f"https://{host}/robots.txt", user_agent)
    if response is None:
        signals.notes.append("robots.txt: request failed")
        return None
    if response.status_code != 200:
        signals.notes.append(f"robots.txt: HTTP {response.status_code}")
        return None

    text = response.text
    signals.rmk_robots_boilerplate = all(path in text for path in _ROBOTS_BOILERPLATE_PATHS)
    declared = _SITEMAP_DECLARATION_RE.search(text)
    return declared.group(1) if declared else None


def _check_sitemap_stylesheet(
    sitemap_url: str, client: httpx.Client, user_agent: str, signals: VendorSignals
) -> None:
    try:
        with client.stream(
            "GET",
            sitemap_url,
            headers={"User-Agent": user_agent},
            timeout=TIMEOUT_SECONDS,
            follow_redirects=True,
        ) as response:
            if response.status_code != 200:
                signals.notes.append(f"sitemap: HTTP {response.status_code} for {sitemap_url}")
                return
            prefix = b""
            for chunk in response.iter_bytes():
                prefix += chunk
                if len(prefix) >= _SITEMAP_PREFIX_BYTES:
                    break
    except httpx.HTTPError:
        signals.notes.append(f"sitemap: request failed for {sitemap_url}")
        return

    text = prefix.decode("utf-8", errors="replace")
    signals.rmk_sitemap_stylesheet = bool(_SITEMAP_STYLESHEET_RE.search(text))


def _check_homepage(host: str, client: httpx.Client, user_agent: str, signals: VendorSignals) -> None:
    """One fetch serves both vendors' header-based checks -- RMK's CSP
    domain and Eightfold's CSP domain / X-EF-* headers all come from the
    same response."""
    response = _get(client, f"https://{host}/", user_agent)
    if response is None:
        signals.notes.append("homepage: request failed")
        return

    csp = response.headers.get("content-security-policy", "")
    signals.rmk_csp_domain = any(domain in csp for domain in _RMK_CSP_DOMAINS)
    signals.eightfold_csp_domain = any(domain in csp for domain in _EIGHTFOLD_CSP_DOMAINS)

    # httpx normalizes header names to lowercase on access regardless of how
    # the server actually cased them, so a plain lowercase membership check
    # is enough here.
    response_header_names = {name.lower() for name in response.headers}
    signals.eightfold_ef_headers = any(name in response_header_names for name in _EIGHTFOLD_HEADER_NAMES)


def probe_host(host: str, client: httpx.Client, user_agent: str) -> VendorSignals:
    """The one function other code should import. Always returns a result --
    never raises -- so a batch sweep over many hosts can't be taken down by
    one bad one. `host` is a bare hostname (no scheme), e.g.
    "jobs.eramet.com", not a full URL."""
    signals = VendorSignals(host=host)
    declared_sitemap = _check_robots(host, client, user_agent, signals)
    sitemap_url = declared_sitemap or f"https://{host}/sitemap.xml"
    _check_sitemap_stylesheet(sitemap_url, client, user_agent, signals)
    _check_homepage(host, client, user_agent, signals)
    return signals


def _read_hosts(args: argparse.Namespace) -> list[str]:
    hosts = list(args.hosts)
    if args.input is not None:
        lines = args.input.read_text(encoding="utf-8").splitlines()
        hosts.extend(line.strip() for line in lines if line.strip() and not line.startswith("#"))
    return hosts


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="discovery/probe_vendor.py",
        description="Check whether one or more hostnames are SAP SuccessFactors "
        "Recruiting Marketing (Jobs2Web) or Eightfold AI tenants.",
    )
    parser.add_argument("hosts", nargs="*", help="Bare hostnames, e.g. jobs.eramet.com")
    parser.add_argument("--input", type=Path, default=None, help="Text file, one hostname per line.")
    parser.add_argument(
        "--settings", type=Path, default=Path("settings.yaml"),
        help="Path to settings.yaml, for the User-Agent contact (default: settings.yaml).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    hosts = _read_hosts(args)
    if not hosts:
        print("No hostnames given (positionally or via --input).", file=sys.stderr)
        return 2

    try:
        settings = load_settings(args.settings)
    except SettingsError as exc:
        print(f"settings error: {exc}", file=sys.stderr)
        return 2

    user_agent = f"jobbot/0.1 (+{settings.user_agent_contact})"

    with httpx.Client() as client:
        for host in hosts:
            signals = probe_host(host, client, user_agent)
            rmk_flags = "".join(
                [
                    "S" if signals.rmk_sitemap_stylesheet else "-",
                    "R" if signals.rmk_robots_boilerplate else "-",
                    "C" if signals.rmk_csp_domain else "-",
                ]
            )
            ef_flags = "".join(
                [
                    "C" if signals.eightfold_csp_domain else "-",
                    "H" if signals.eightfold_ef_headers else "-",
                ]
            )
            print(f"{signals.host:40s} rmk[{rmk_flags}] eightfold[{ef_flags}] {signals.vendor}")
            for note in signals.notes:
                print(f"    {note}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
