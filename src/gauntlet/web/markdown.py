"""A tiny, dependency-free markdown→HTML renderer (P5, FR-4.3).

FR-4.3 requires gate ``show:`` markdown artifacts (``prd.md``/``plan.md``) to be
rendered **as markdown**, not dumped as raw text. The project's dependency budget
(M5) deliberately admits no markdown library — the console is a thin Jinja shell
with no build step — so this is a small, *clearly scoped* renderer covering the
constructs that actually appear in the PRD/plan artifacts: ATX headings, fenced
code blocks, unordered/ordered lists, paragraphs, and inline ``code``/**bold**/
*italic*.

It is **safe by construction**: every byte of the source is HTML-escaped first,
and only a fixed set of known tags is re-introduced over the escaped text. Raw
links/images are deliberately *not* rendered, so there is no ``href``/``src``
injection surface — an artifact a reviewer is inspecting can never smuggle active
content into the console page.
"""

from __future__ import annotations

import html
import re

from markupsafe import Markup

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_ULIST = re.compile(r"^\s*[-*+]\s+(.*)$")
_OLIST = re.compile(r"^\s*\d+\.\s+(.*)$")
_FENCE = re.compile(r"^\s*```")


def _inline(text: str) -> str:
    """Escape, then re-introduce a tiny inline tag set over the escaped text."""
    s = html.escape(text)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"\*([^*]+)\*", r"<em>\1</em>", s)
    return s


def render_markdown(text: str) -> Markup:
    """Render ``text`` as a safe HTML fragment (see module docstring)."""
    out: list[str] = []
    para: list[str] = []
    code: list[str] = []
    in_code = False
    list_tag: str | None = None

    def flush_para() -> None:
        if para:
            out.append("<p>" + _inline("\n".join(para)) + "</p>")
            para.clear()

    def close_list() -> None:
        nonlocal list_tag
        if list_tag:
            out.append(f"</{list_tag}>")
            list_tag = None

    def flush_code() -> None:
        out.append(
            '<pre class="artifact"><code>'
            + html.escape("\n".join(code))
            + "</code></pre>"
        )
        code.clear()

    for raw in text.replace("\r\n", "\n").split("\n"):
        if _FENCE.match(raw):
            if in_code:
                flush_code()
                in_code = False
            else:
                flush_para()
                close_list()
                in_code = True
            continue
        if in_code:
            code.append(raw)
            continue
        if (m := _HEADING.match(raw)) is not None:
            flush_para()
            close_list()
            level = len(m.group(1))
            out.append(f"<h{level}>" + _inline(m.group(2).strip()) + f"</h{level}>")
            continue
        if (m := _ULIST.match(raw)) is not None:
            flush_para()
            if list_tag != "ul":
                close_list()
                out.append("<ul>")
                list_tag = "ul"
            out.append("<li>" + _inline(m.group(1).strip()) + "</li>")
            continue
        if (m := _OLIST.match(raw)) is not None:
            flush_para()
            if list_tag != "ol":
                close_list()
                out.append("<ol>")
                list_tag = "ol"
            out.append("<li>" + _inline(m.group(1).strip()) + "</li>")
            continue
        if not raw.strip():
            flush_para()
            close_list()
            continue
        para.append(raw)

    if in_code:
        flush_code()
    flush_para()
    close_list()
    return Markup("\n".join(out))


__all__ = ["render_markdown"]
