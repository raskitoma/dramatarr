"""Convert source synopsis fragments into readable plain text."""

import re
from html.parser import HTMLParser

_MARKUP = re.compile(r"</?[a-z][a-z0-9-]*(?:\s[^>]*|\s*/?)>", re.IGNORECASE)
_BLOCKS = {"div", "p", "li", "section", "article", "blockquote", "h1", "h2", "h3"}
_HIDDEN = {"script", "style", "template"}
_APPENDICES = {"personajes", "characters", "personagens"}


class _SynopsisParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden: str | None = None
        self.lark = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.hidden:
            return
        if tag in _HIDDEN:
            self.hidden = tag
        elif tag in _BLOCKS or tag == "br":
            self.parts.append("\n")
        if ("data-lark-html-role", "root") in attrs:
            self.lark = True

    def handle_endtag(self, tag: str) -> None:
        if tag == self.hidden:
            self.hidden = None
        elif not self.hidden and tag in _BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(re.sub(r"\s+", " ", data))


def clean_synopsis(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    if not _MARKUP.search(value):
        return value.strip() or None
    parser = _SynopsisParser()
    parser.feed(value)
    parser.close()
    paragraphs: list[str] = []
    for line in "".join(parser.parts).splitlines():
        text = line.strip()
        if not text:
            continue
        if parser.lark and paragraphs and text.rstrip(":").casefold() in _APPENDICES:
            break
        paragraphs.append(text)
    return "\n\n".join(paragraphs) or None
