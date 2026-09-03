"""M12 Part B4: probes a hostname for SAP SuccessFactors Recruiting Marketing
(RMK, the product formerly sold as Jobs2Web) using three independent signals,
derived by directly inspecting five confirmed/rejected employers (Eramet,
Nexans, Worldline, Capgemini confirmed; Danone rejected -- see the M12 report
for the full investigation):

1. sitemap_stylesheet: the tenant's sitemap carries an
   ``<?xml-stylesheet ... href=".../view/xsl/sitemapssl.xsl"?>`` processing
   instruction, right after the XML declaration. Confirmed identical on all
   four RMK tenants; absent on Danone's (which uses a plain urlset with
   xhtml:link hreflang alternates instead -- a different vendor entirely).
2. robots_boilerplate: robots.txt carries RMK's own generic Disallow list --
   ten fixed paths, byte-identical across all four tenants regardless of
   company, language, or hosting region.
3. csp_vendor_domain: the homepage's Content-Security-Policy response header
   names one of RMK's own infrastructure domains (jobs2web.com -- the literal
   legacy product domain, still live -- rmkcdn.successfactors.com, or any
   *.sapsf.com host).

Any ONE signal firing is enough to call a host confirmed; in practice every
tenant checked so far fires either all three or two of three (robots.txt is
occasionally fronted by a CDN that serves its own default file instead, which
loses signal 2 without affecting 1 or 3).

This is discovery-phase tooling, not a jobbot/ adapter: it never writes to
companies/*.yaml itself and makes no claim about whether a confirmed tenant
actually carries real, fetchable, relevant postings -- that is what
jobbot/sources/successfactors.py's own fetch() (and a human reading its
output) is for. Its only job is answering "is this host on this vendor"
cheaply, in at most three requests, none of them to an aggregator or
directory (CLAUDE.md rule 3: every host probed here must already be a
resolved careers domain, found by a human or by jobbot/discover.py's own
resolution chain).

Usage:
    uv run python discovery/probe_rmk.py careers.example.com jobs.example.fr
    uv run python discovery/probe_rmk.py --input discovery/seeds/hosts.txt
    uv run python discovery/probe_rmk.py --settings settings.yaml jobs.eramet.com
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

_CSP_VENDOR_DOMAINS = ("jobs2web.com", "rmkcdn.successfactors.com", "sapsf.com")

# The stylesheet processing instruction is always the very first thing in the
# document, right after the XML declaration -- reading a small prefix instead
# of the full body matters here specifically because a real tenant's sitemap
# can be thousands of <url> entries (Worldline's is; Eramet's isn't), and this
# probe has no reason to pay for the rest of it just to answer a yes/no.
_SITEMAP_PREFIX_BYTES = 4096


@dataclass
class RmkSignals:
    """One host's probe result. `notes` records why a signal came back False
    when that's worth knowing (a fetch failure, a non-200) rather than just
    silently reading as "no" the same way a genuine absence would."""

    host: str
    sitemap_stylesheet: bool = False
    robots_boilerplate: bool = False
    csp_vendor_domain: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def confirmed(self) -> bool:
        return self.sitemap_stylesheet or self.robots_boilerplate or self.csp_vendor_domain

    @property
    def signal_count(self) -> int:
        return sum([self.sitemap_stylesheet, self.robots_boilerplate, self.csp_vendor_domain])


def _get(client: httpx.Client, url: str, user_agent: str) -> httpx.Response | None:
    """Every request in this probe follows the same never-raise contract:
    a failed fetch just means that signal comes back False, not a crash of
    the whole sweep -- one unreachable host must not stop the other 39."""
    try:
        return client.get(
            url, headers={"User-Agent": user_agent}, timeout=TIMEOUT_SECONDS, follow_redirects=True
        )
    except httpx.HTTPError:
        return None


def _check_robots(
    host: str, client: httpx.Client, user_agent: str, signals: RmkSignals
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
    signals.robots_boilerplate = all(path in text for path in _ROBOTS_BOILERPLATE_PATHS)
    declared = _SITEMAP_DECLARATION_RE.search(text)
    return declared.group(1) if declared else None


def _check_sitemap_stylesheet(
    sitemap_url: str, client: httpx.Client, user_agent: str, signals: RmkSignals
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
    signals.sitemap_stylesheet = bool(_SITEMAP_STYLESHEET_RE.search(text))


def _check_csp(host: str, client: httpx.Client, user_agent: str, signals: RmkSignals) -> None:
    response = _get(client, f"https://{host}/", user_agent)
    if response is None:
        signals.notes.append("homepage: request failed")
        return
    csp = response.headers.get("content-security-policy", "")
    signals.csp_vendor_domain = any(domain in csp for domain in _CSP_VENDOR_DOMAINS)


def probe_host(host: str, client: httpx.Client, user_agent: str) -> RmkSignals:
    """The one function other code should import. Always returns a result --
    never raises -- so a batch sweep over many hosts can't be taken down by
    one bad one. `host` is a bare hostname (no scheme), e.g.
    "jobs.eramet.com", not a full URL."""
    signals = RmkSignals(host=host)
    declared_sitemap = _check_robots(host, client, user_agent, signals)
    sitemap_url = declared_sitemap or f"https://{host}/sitemap.xml"
    _check_sitemap_stylesheet(sitemap_url, client, user_agent, signals)
    _check_csp(host, client, user_agent, signals)
    return signals


def _read_hosts(args: argparse.Namespace) -> list[str]:
    hosts = list(args.hosts)
    if args.input is not None:
        lines = args.input.read_text(encoding="utf-8").splitlines()
        hosts.extend(line.strip() for line in lines if line.strip() and not line.startswith("#"))
    return hosts


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="discovery/probe_rmk.py",
        description="Check whether one or more hostnames are SAP SuccessFactors "
        "Recruiting Marketing (Jobs2Web) tenants.",
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
            flags = "".join(
                [
                    "S" if signals.sitemap_stylesheet else "-",
                    "R" if signals.robots_boilerplate else "-",
                    "C" if signals.csp_vendor_domain else "-",
                ]
            )
            verdict = "RMK" if signals.confirmed else "no match"
            print(f"{signals.host:40s} [{flags}] {verdict}")
            for note in signals.notes:
                print(f"    {note}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
