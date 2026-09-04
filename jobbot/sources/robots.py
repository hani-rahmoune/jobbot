"""Shared robots.txt compliance, used by any adapter or tool that fetches an
arbitrary employer-owned URL directly (as opposed to a named ATS's API,
which is not a "non-API fetch" per CLAUDE.md's adapter contract).

Extracted from jsonld.py (M7 Part B1) so jobbot/discover.py can honor the
same policy while resolving a company's careers page, without duplicating
the fetch-and-parse logic.

M14 Part A: replaced the stdlib `urllib.robotparser` dependency with a
from-scratch parser and matcher, after it produced a real, wrong answer
against Kering's actual robots.txt (found while investigating that
employer's Eightfold-powered board). That file's shape --

    Disallow: /
    Allow: /careers
    Allow: /api/pcsx
    ...

-- is a common, legitimate pattern (block crawling in general, carve out a
few public routes), but `urllib.robotparser.Entry.allowance()` returns the
FIRST matching rule in file order, not the most specific one. Since
"Disallow: /" is both first and matches every URL trivially (every path
starts with "/"), it wins outright and every later Allow line is dead code
as far as that module is concerned -- confirmed directly against CPython's
own source (see this module's own test suite for the reproduction). Every
adapter's own "robots.txt disallows this" log line reads identically
whether the site genuinely means it or the parser just misread an Allow
list this way, so this had been silently indistinguishable from a real
wall. It plausibly also mis-scored some of TotalEnergies' M13 findings:
that robots.txt has no leading "Disallow: /", but does use "*" as a
mid-path wildcard ("Allow: /*/careers"), which stdlib's parser also can't
match -- see _path_matches's own docstring for that half of the fix.

The correct behavior (RFC 9309 section 2.2.2, the now-standardized version
of the Robots Exclusion Protocol every major crawler implements) is: the
LONGEST matching rule wins, regardless of file order; a tie is broken in
favor of Allow. "Longest" is measured in raw characters of the rule's own
path value, `*`/`$` included -- not of some normalized/decoded form.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 15.0


@dataclass
class _RuleGroup:
    user_agents: list[str] = field(default_factory=list)
    rules: list[tuple[str, bool]] = field(default_factory=list)  # (raw path, is_allow)


def _parse_robots_txt(text: str) -> list[_RuleGroup]:
    """Groups consecutive User-agent: lines with the rules that follow them,
    the standard robots.txt record shape. A record starts at the first
    User-agent: line after the previous record's first rule line (so
    several User-agent: lines in a row belong to the SAME record, sharing
    its rules -- the standard "these two bots get identical treatment"
    idiom), and ends at the next User-agent: line that appears after at
    least one rule has been seen. Sitemap:/Crawl-delay:/anything else is
    ignored here -- not this function's concern (see discover.py for
    Sitemap: line handling)."""
    groups: list[_RuleGroup] = []
    current: _RuleGroup | None = None
    current_has_rules = False

    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field_name, _, value = line.partition(":")
        field_name = field_name.strip().lower()
        value = value.strip()

        if field_name == "user-agent":
            if current is None or current_has_rules:
                current = _RuleGroup()
                groups.append(current)
                current_has_rules = False
            current.user_agents.append(value)
        elif field_name in ("allow", "disallow"):
            if current is None:
                # A rule with no preceding User-agent: line at all -- not
                # spec-compliant, but treated as applying to everyone
                # rather than silently dropped.
                current = _RuleGroup(user_agents=["*"])
                groups.append(current)
            if value == "" and field_name == "disallow":
                # An empty Disallow value is explicitly "allow everything"
                # per the original spec, not a zero-length path rule.
                continue
            current.rules.append((value, field_name == "allow"))
            current_has_rules = True

    return groups


def _path_matches(rule_path: str, url_path: str) -> bool:
    """robots.txt path matching (RFC 9309 section 2.2.3): `*` matches any
    sequence of characters, including none; `$` anchors to the end of the
    URL when it is the rule's own last character. Confirmed live necessary
    beyond just the longest-match fix this module exists for: TotalEnergies'
    own robots.txt uses "Allow: /*/careers" and "Disallow: /*qtvc=" --
    stdlib's RuleLine.applies_to() treats "*" as a literal character (a
    plain prefix match against the literal string "/*/careers"), which no
    real URL ever starts with, silently making that rule permanently dead.
    """
    if rule_path == "":
        return False
    anchored = rule_path.endswith("$")
    pattern = rule_path[:-1] if anchored else rule_path
    segments = pattern.split("*")
    regex = ".*".join(re.escape(segment) for segment in segments)
    if anchored:
        regex += "$"
    return re.match(regex, url_path) is not None


def _applies_to_us(group_user_agents: list[str]) -> bool:
    """We only ever appear as a generic crawler -- no real robots.txt this
    project has fetched has ever named this bot specifically (it's not
    Googlebot, GPTBot, ClaudeBot, or any other well-known name), so the
    only group that ever applies to us is the wildcard one. A group naming
    only OTHER, specific bots does not apply to us even if our own
    User-Agent string happens to share a substring with one of them."""
    return any(ua.strip() == "*" for ua in group_user_agents)


def _can_fetch(groups: list[_RuleGroup], url: str) -> bool:
    parsed = urlsplit(url)
    url_path = parsed.path or "/"
    if parsed.query:
        url_path = f"{url_path}?{parsed.query}"

    best_length = -1
    best_allow = True  # no matching rule at all -- default allow
    for group in groups:
        if not _applies_to_us(group.user_agents):
            continue
        for rule_path, is_allow in group.rules:
            if not _path_matches(rule_path, url_path):
                continue
            length = len(rule_path)
            if length > best_length or (length == best_length and is_allow and not best_allow):
                best_length = length
                best_allow = is_allow

    return best_allow


class RobotsCache:
    """Fetches and caches robots.txt per host for the life of the instance.

    A robots.txt that returns non-200 (404 included) or fails to fetch at
    all is treated as allowing -- the standard convention: absence of a
    robots.txt is not a site asking to be left alone.
    """

    def __init__(self, client: httpx.Client, user_agent: str) -> None:
        self.client = client
        self.user_agent = user_agent
        self._cache: dict[str, list[_RuleGroup] | None] = {}

    def allowed(self, url: str) -> bool:
        parsed = urlsplit(url)
        host = parsed.netloc
        if host not in self._cache:
            self._cache[host] = self._fetch(parsed.scheme, host)
        groups = self._cache[host]
        if groups is None:
            return True
        return _can_fetch(groups, url)

    def _fetch(self, scheme: str, host: str) -> list[_RuleGroup] | None:
        robots_url = f"{scheme}://{host}/robots.txt"
        try:
            response = self.client.get(
                robots_url, headers={"User-Agent": self.user_agent}, timeout=TIMEOUT_SECONDS
            )
        except httpx.HTTPError:
            logger.info("robots: fetch failed for %s, treating as allowed", host)
            return None

        if response.status_code != 200:
            logger.info(
                "robots: %s returned HTTP %d, treating as allowed",
                robots_url, response.status_code,
            )
            return None

        return _parse_robots_txt(response.text)
