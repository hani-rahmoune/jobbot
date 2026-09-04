"""Dedicated tests for jobbot/sources/robots.py's matching algorithm.

No such tests existed before M14 -- every adapter's own test suite only ever
exercised the trivial "Disallow: /" (blocks everything) and 404 (allows
everything) cases. That gap is exactly how the longest-match bug (see
robots.py's own module docstring) went unnoticed: it only bites a robots.txt
that mixes a broad Disallow with more specific Allow rules, a shape no
existing adapter fixture happened to use.
"""

from __future__ import annotations

import httpx
import respx
from conftest import TEST_USER_AGENT

from jobbot.sources.robots import RobotsCache, _can_fetch, _parse_robots_txt


def _groups(text: str):
    return _parse_robots_txt(text)


# --- the real bug: Disallow: / + selective Allow: -----------------------


def test_a_disallow_all_with_a_more_specific_allow_permits_the_allowed_path() -> None:
    """The exact shape confirmed live on Kering's Eightfold-hosted board.
    Python's stdlib urllib.robotparser gets this wrong (first-match-wins:
    "Disallow: /" matches every path and is listed first, so every later
    Allow line is dead)."""
    text = "User-agent: *\nDisallow: /\nAllow: /careers\nAllow: /api/pcsx\n"
    groups = _groups(text)
    assert _can_fetch(groups, "https://example.com/api/pcsx/search") is True
    assert _can_fetch(groups, "https://example.com/careers/job/1") is True


def test_a_disallow_all_still_blocks_a_path_with_no_more_specific_allow() -> None:
    text = "User-agent: *\nDisallow: /\nAllow: /careers\n"
    groups = _groups(text)
    assert _can_fetch(groups, "https://example.com/some/other/path") is False


def test_longest_match_wins_regardless_of_file_order() -> None:
    """A more specific Disallow appearing BEFORE a less specific Allow must
    still lose to the more specific rule -- order in the file must not
    matter, only rule length."""
    text = "User-agent: *\nDisallow: /careers/internal\nAllow: /careers\n"
    groups = _groups(text)
    assert _can_fetch(groups, "https://example.com/careers/internal/x") is False
    assert _can_fetch(groups, "https://example.com/careers/public") is True


def test_a_tie_in_length_is_broken_in_favor_of_allow() -> None:
    text = "User-agent: *\nDisallow: /jobs\nAllow: /jobs\n"
    groups = _groups(text)
    assert _can_fetch(groups, "https://example.com/jobs") is True


# --- wildcard and end-anchor support -------------------------------------


def test_a_mid_path_wildcard_matches_real_urls() -> None:
    """Confirmed necessary on TotalEnergies' real robots.txt ("Allow:
    /*/careers"). Stdlib's parser treats '*' as a literal character, so a
    rule like this never matches any real URL under stdlib -- silently dead
    even though the site author clearly meant it to work."""
    text = "User-agent: *\nDisallow: /\nAllow: /*/careers\n"
    groups = _groups(text)
    assert _can_fetch(groups, "https://example.com/en_US/careers") is True
    assert _can_fetch(groups, "https://example.com/fr_FR/careers/SearchJobs") is True
    assert _can_fetch(groups, "https://example.com/unrelated") is False


def test_a_query_string_wildcard_matches() -> None:
    text = "User-agent: *\nDisallow: /careers/*qtvc=\n"
    groups = _groups(text)
    assert _can_fetch(groups, "https://example.com/careers/page?qtvc=1") is False
    assert _can_fetch(groups, "https://example.com/careers/page?other=1") is True


def test_a_dollar_anchor_matches_only_the_exact_path() -> None:
    text = "User-agent: *\nDisallow: /\nAllow: /$\n"
    groups = _groups(text)
    assert _can_fetch(groups, "https://example.com/") is True
    assert _can_fetch(groups, "https://example.com/anything") is False


# --- user-agent group selection -------------------------------------------


def test_a_rule_group_for_a_different_named_bot_does_not_apply_to_us() -> None:
    text = "User-agent: GPTBot\nDisallow: /\nUser-agent: *\nAllow: /\n"
    groups = _groups(text)
    assert _can_fetch(groups, "https://example.com/careers") is True


def test_no_matching_rule_at_all_defaults_to_allowed() -> None:
    text = "User-agent: *\nDisallow: /admin\n"
    groups = _groups(text)
    assert _can_fetch(groups, "https://example.com/careers") is True


def test_an_empty_disallow_value_means_allow_everything() -> None:
    text = "User-agent: *\nDisallow:\n"
    groups = _groups(text)
    assert _can_fetch(groups, "https://example.com/anything") is True


# --- parsing edge cases ---------------------------------------------------


def test_comments_are_stripped() -> None:
    text = "User-agent: *  # everyone\nDisallow: /admin  # keep out\n"
    groups = _groups(text)
    assert _can_fetch(groups, "https://example.com/admin") is False
    assert _can_fetch(groups, "https://example.com/public") is True


def test_field_names_are_case_insensitive() -> None:
    text = "USER-AGENT: *\nDISALLOW: /admin\n"
    groups = _groups(text)
    assert _can_fetch(groups, "https://example.com/admin") is False


# --- RobotsCache (the fetch/cache layer, unchanged by M14) ----------------


def test_robots_cache_treats_a_404_as_allowed(mock_client: httpx.Client) -> None:
    cache = RobotsCache(mock_client, TEST_USER_AGENT)
    with respx.mock:
        respx.get("https://example.invalid/robots.txt").mock(return_value=httpx.Response(404))
        assert cache.allowed("https://example.invalid/anything") is True


def test_robots_cache_treats_a_fetch_failure_as_allowed(mock_client: httpx.Client) -> None:
    cache = RobotsCache(mock_client, TEST_USER_AGENT)
    with respx.mock:
        respx.get("https://example.invalid/robots.txt").mock(side_effect=httpx.ConnectError("boom"))
        assert cache.allowed("https://example.invalid/anything") is True


def test_robots_cache_uses_the_real_rules_on_a_200(mock_client: httpx.Client) -> None:
    cache = RobotsCache(mock_client, TEST_USER_AGENT)
    with respx.mock:
        respx.get("https://example.invalid/robots.txt").mock(
            return_value=httpx.Response(200, text="User-agent: *\nDisallow: /\nAllow: /careers\n")
        )
        assert cache.allowed("https://example.invalid/careers/job/1") is True
        assert cache.allowed("https://example.invalid/other") is False


def test_robots_cache_only_fetches_robots_txt_once_per_host(mock_client: httpx.Client) -> None:
    cache = RobotsCache(mock_client, TEST_USER_AGENT)
    with respx.mock:
        route = respx.get("https://example.invalid/robots.txt").mock(
            return_value=httpx.Response(200, text="User-agent: *\nAllow: /\n")
        )
        cache.allowed("https://example.invalid/a")
        cache.allowed("https://example.invalid/b")
    assert route.call_count == 1
