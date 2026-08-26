"""HTML-to-plain-text cleanup, shared by every ATS adapter that hands back
job descriptions as HTML (Greenhouse's `content`, and others to follow).

Extracted from greenhouse.py (M6 A2): nothing about this is Greenhouse-
specific, and every new adapter needs it.
"""

from __future__ import annotations

import html
import re

_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_BREAK_RE = re.compile(r"(?i)</?(p|div|li|ul|ol|br|h[1-6])\b[^>]*>")
_INLINE_SPACE_RE = re.compile(r"[ \t]+")


def strip_html(raw: str) -> str:
    """Turn HTML job content into readable plain text.

    Tags are stripped before entities are unescaped, deliberately: content
    can legitimately contain an entity-encoded "&lt;...&gt;" meant to render
    as literal angle brackets, and unescaping first would turn that into
    something that looks like a real tag and gets eaten by the tag-stripper.
    """
    if not raw:
        return ""
    text = _BLOCK_BREAK_RE.sub("\n", raw)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    lines = (_INLINE_SPACE_RE.sub(" ", line).strip() for line in text.splitlines())
    return "\n".join(line for line in lines if line)
