"""Tests for jobbot.sources.html_text, moved here from test_greenhouse.py
(M6 A2) when HTML cleanup stopped being Greenhouse-specific.
"""

from __future__ import annotations

from jobbot.sources.html_text import strip_html


def test_strip_html_on_empty_content_returns_empty_string() -> None:
    assert strip_html("") == ""


def test_strip_html_decodes_entities_after_stripping_tags() -> None:
    raw = (
        "<div><p>Missions &amp; responsabilit&eacute;s:</p>"
        "<ul><li>Analyse de donn&eacute;es</li>"
        "<li>Reporting &lt;hebdomadaire&gt;</li></ul>"
        "<p>Salaire: 800&euro;/mois</p></div>"
    )
    text = strip_html(raw)

    assert "<p>" not in text
    assert "<div>" not in text
    assert "<li>" not in text
    assert "&amp;" not in text
    assert "&eacute;" not in text

    assert "Missions & responsabilités:" in text
    assert "Analyse de données" in text
    # An entity-encoded "<hebdomadaire>" must survive as literal text, not be
    # eaten by tag-stripping: content is decoded *after* real tags are gone.
    assert "Reporting <hebdomadaire>" in text
    assert "800€/mois" in text
