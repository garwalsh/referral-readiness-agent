"""Render saved prompts for inspection without changing their stored text."""
from os.path import commonprefix
import re

from django.utils.html import escape
from django.utils.safestring import SafeString, mark_safe
from markdown_it import MarkdownIt

# Raw HTML stays text and rendered prompts cannot fetch remote images.
MARKDOWN = MarkdownIt("commonmark", {"html": False, "breaks": True}).disable("image")
SENTENCE_BREAK = re.compile(r"(?<=[.!?]) (?=[A-Z])")


def formatted_prompt(text: str) -> SafeString:
    tokens = MARKDOWN.parse(text)
    if (
        len(text) > 300 and len(text.splitlines()) == 1
        and [token.type for token in tokens] == ["paragraph_open", "inline", "paragraph_close"]
        and all(token.type == "text" for token in tokens[1].children or [])
    ):
        # Give long legacy one-line instructions breathing room at sentence breaks.
        sentences = SENTENCE_BREAK.split(text)
        text = "\n\n".join(" ".join(sentences[index:index + 3]) for index in range(0, len(sentences), 3))
    return mark_safe(MARKDOWN.render(text))


def literal_prompt(text: str) -> SafeString:
    # A literal CR in HTML source is normalized by browsers. Escape it after
    # HTML-escaping the text so it survives in the raw view.
    return mark_safe(escape(text).replace("\r", "&#13;"))


def display_diff(lines: list[str]) -> list[dict]:
    """Decorate unified diff lines, emphasizing paired single-line replacements."""
    kinds = {"-": "removed", "+": "added", "@": "header"}
    rows = []
    for index, line in enumerate(lines):
        text = line.removesuffix("\n")
        kind = "header" if index < 2 else kinds.get(text[:1], "context")
        rows.append({"kind": kind, "segments": [(literal_prompt(text), False)]})
        if kind != "added" or rows[index - 1]["kind"] != "removed":
            continue
        if rows[index - 2]["kind"] == "removed" or (
            index + 1 < len(lines) and lines[index + 1].startswith("+")
        ):
            continue
        before = lines[index - 1].removesuffix("\n")[1:]
        after = text[1:]
        start = len(commonprefix([before, after]))
        suffix = len(commonprefix([before[start:][::-1], after[start:][::-1]]))
        for row, sign, content in ((rows[index - 1], "-", before), (rows[index], "+", after)):
            end = len(content) - suffix
            row["segments"] = [(literal_prompt(sign + content[:start]), False),
                               (literal_prompt(content[start:end]), True),
                               (literal_prompt(content[end:]), False)]
    return rows
