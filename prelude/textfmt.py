"""Minimal, safe rendering of Claude's text: headings, fenced code blocks and inline code."""

from __future__ import annotations

import re

from markupsafe import Markup, escape

_FENCE = re.compile(r"```[^\n`]*\n(.*?)(?:```|$)", re.DOTALL)
_INLINE = re.compile(r"`([^`\n]+)`")
_HEADING = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)


def _inline(text: str) -> str:
    out = []
    pos = 0
    for m in _HEADING.finditer(text):
        out.append(_code_spans(text[pos : m.start()]))
        out.append(f'<strong class="h">{_code_spans(m.group(1))}</strong>')
        pos = m.end()
    out.append(_code_spans(text[pos:]))
    return "".join(out)


def _code_spans(text: str) -> str:
    out, pos = [], 0
    for m in _INLINE.finditer(text):
        out.append(str(escape(text[pos : m.start()])))
        out.append(f"<code>{escape(m.group(1))}</code>")
        pos = m.end()
    out.append(str(escape(text[pos:])))
    return "".join(out)


def render_text(text: str | None) -> Markup:
    if not text:
        return Markup("")
    out, pos = [], 0
    for m in _FENCE.finditer(text):
        out.append(_inline(text[pos : m.start()].rstrip("\n")))
        out.append(f'<pre class="snip">{escape(m.group(1).rstrip())}</pre>')
        pos = m.end()
        if text[pos : pos + 1] == "\n":
            pos += 1
    out.append(_inline(text[pos:]))
    return Markup("".join(out))
