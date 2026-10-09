"""MkDocs hooks: make `mkdocs build --strict` fail on Markdown that GitHub renders but Python-Markdown does not.

GitHub starts a list or a fenced code block even when it directly follows a line of paragraph text. Python-Markdown
needs a blank line first and otherwise glues the list or code into the paragraph, so the page reads differently on
the site than on GitHub. The logger name starts with `mkdocs.` so that --strict counts the warning.
"""

import logging
import re
from typing import Any

from mkdocs.structure.pages import Page

log = logging.getLogger("mkdocs.hooks.docs_hooks")

_PARAGRAPH = re.compile(r"<p>(.*?)</p>", re.DOTALL)
_GLUED_LIST = re.compile(r"\n\s*(?:[-*+]|\d+\.) ")
_TAG = re.compile(r"<[^>]+>")


def on_page_content(html: str, page: Page, **kwargs: Any) -> str:
    for match in _PARAGRAPH.finditer(html):
        text = match.group(1)
        if '<div class="highlight">' in text or _GLUED_LIST.search(text):
            first_line = _TAG.sub("", text).strip().splitlines()[0]
            log.warning(
                "%s: a list or code block is not separated from the paragraph before it (add a blank line): %r",
                page.file.src_path,
                first_line,
            )
    return html
