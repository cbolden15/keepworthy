"""Netscape bookmark HTML: the "Export bookmarks to HTML" file from Firefox,
Chrome and Safari. html.parser resolves character references only and never
reads another file."""
import codecs
from html.parser import HTMLParser

from src.importers import (MAX_BOOKMARKS, MAX_FOLDER, MAX_FOLDER_DEPTH, Collector, ParseResult,
                           sanitize_text)

_CHUNK = 1024 * 1024


def sniff(head: bytes) -> bool:
    """True when the first 64 KiB contain, case-insensitively, the Netscape
    doctype, or both <DL> and <DT><A HREF."""
    lowered = head.lower()
    return (b"<!doctype netscape-bookmark-file-1>" in lowered
            or (b"<dl>" in lowered and b"<dt><a href" in lowered))


class _BookmarkParser(HTMLParser):
    def __init__(self, collector: Collector):
        super().__init__(convert_charrefs=True)
        self.collector = collector
        self.stack: list[str] = []      # folder names of the open <DL>s, at most MAX_FOLDER_DEPTH
        self.deeper = 0                 # open <DL>s past MAX_FOLDER_DEPTH; their names are ignored
        self.pending = ""               # the last completed <H3> name
        self.h3: list[str] | None = None
        self.anchor: dict | None = None
        self.title: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "h3":
            self.h3 = []
        elif tag == "dl":
            if len(self.stack) < MAX_FOLDER_DEPTH:
                self.stack.append(self.pending)     # "" for the root <DL> after <H1>
            else:
                self.deeper += 1
            self.pending = ""
        elif tag == "a":
            if self.anchor is not None:         # the previous <A> never closed
                self.collector.bad()
            self.anchor = dict(attrs)           # names arrive lower-cased
            self.title = []

    def handle_endtag(self, tag):
        if tag == "h3" and self.h3 is not None:
            self.pending = sanitize_text("".join(self.h3), MAX_FOLDER)
            self.h3 = None
        elif tag == "dl":
            if self.deeper:
                self.deeper -= 1
            elif self.stack:                    # a stray </DL> in a malformed tail
                self.stack.pop()
        elif tag == "a" and self.anchor is not None:
            attrs, self.anchor = self.anchor, None
            if not attrs.get("href"):
                self.collector.bad()
                return
            self.collector.add(
                attrs["href"], title="".join(self.title),
                folder="/".join(name for name in self.stack if name),
                tags=(attrs.get("tags") or "").split(","),
                saved_at=attrs.get("add_date") or "")

    def handle_data(self, data):
        if self.h3 is not None:
            self.h3.append(data)
        elif self.anchor is not None:
            self.title.append(data)


def parse(path, *, max_items: int = MAX_BOOKMARKS) -> ParseResult:
    collector = Collector("netscape", max_items)
    parser = _BookmarkParser(collector)
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    with open(path, "rb") as handle:
        while chunk := handle.read(_CHUNK):
            parser.feed(decoder.decode(chunk))
    parser.feed(decoder.decode(b"", final=True))
    parser.close()                              # an unclosed trailing <A> never completes
    return collector.result()
