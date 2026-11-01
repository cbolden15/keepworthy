"""Bookmark file parsers for the bulk importer.

Each format module (netscape, pocket, raindrop) exposes sniff(head) and
parse(path, *, max_items). This package holds the shared record type, the
limits, the sanitizers and format detection. Parsers read the file only:
they never fetch, and never touch state or the ledger. Error messages never
contain a url, title, folder or tag."""
import csv
import importlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit

MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_BOOKMARKS = 50_000
MAX_TITLE = 300
MAX_FOLDER = 200
MAX_FOLDER_DEPTH = 64               # nested folders tracked; deeper names are ignored
MAX_URL = 4096
MAX_TAGS = 20
MAX_TAG = 50
MAX_NOTE = 280                      # a Raindrop note becomes the item's why
MAX_CSV_FIELD = 256 * 1024          # csv.field_size_limit for both CSV parsers
SNIFF_BYTES = 64 * 1024
SOURCES = ("netscape", "pocket", "raindrop")   # sniff order and --format choices
SOURCE_LABELS = {"netscape": "browser bookmarks", "pocket": "Pocket", "raindrop": "Raindrop"}

# C0, DEL, C1, then zero-width marks and bidi overrides (reorder how a title prints)
_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")
_SPACE = re.compile(r"\s+")
_DIGITS = re.compile(r"[0-9]+")
_EARLIEST = datetime(1990, 1, 1, tzinfo=timezone.utc)
_LATEST = datetime(2100, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True)
class Bookmark:
    url: str                # as found, stripped; scheme http or https (checked by the parser)
    title: str              # "" when the export has none; at most MAX_TITLE characters
    folder: str             # "/"-joined path, "" at the root; at most MAX_FOLDER characters
    tags: tuple[str, ...]   # at most MAX_TAGS, each at most MAX_TAG characters
    note: str               # Raindrop note, "" elsewhere; one line
    saved_at: str           # UTC ISO 8601 "YYYY-MM-DDTHH:MM:SS+00:00", or "" when unknown
    source: str             # one of SOURCES


@dataclass(frozen=True)
class ParseResult:
    bookmarks: list[Bookmark]
    found: int              # rows or anchors inspected, including the two counts below
    bad_rows: int           # no url, or a row the format could not read
    unsupported_scheme: int # a url whose scheme is not http or https


class ParseError(Exception):
    """The file is not readable as this format (exit 2). The message names the
    line or the missing column; it never contains a url, title, folder or tag."""


class LimitExceeded(ParseError):
    """MAX_BOOKMARKS crossed. The message names the cap."""


def sanitize_text(value: str, limit: int) -> str:
    """Strip C0 and C1 control characters, collapse whitespace runs to one
    space, strip, cut to `limit` characters. Whitespace controls (newline,
    tab) become a space first, so a multi-line note reads as one line."""
    text = _SPACE.sub(" ", str(value or ""))
    text = _SPACE.sub(" ", _CONTROL.sub("", text)).strip()
    return text[:limit].rstrip()


def sanitize_tags(values) -> tuple[str, ...]:
    """sanitize_text each to MAX_TAG, drop empties, dedupe case-insensitively
    keeping the first spelling, keep the first MAX_TAGS."""
    out, seen = [], set()
    for value in values:
        tag = sanitize_text(value, MAX_TAG)
        if tag and tag.casefold() not in seen:
            seen.add(tag.casefold())
            out.append(tag)
            if len(out) == MAX_TAGS:
                break
    return tuple(out)


def parse_timestamp(value: str) -> str:
    """Digits only -> unix seconds -> UTC ISO. Otherwise datetime.fromisoformat
    (a trailing Z accepted; a naive value is taken as UTC). Anything else, and
    anything before 1990-01-01 or after 2100-01-01, -> ""."""
    value = str(value or "").strip()
    if not value:
        return ""
    try:
        if _DIGITS.fullmatch(value):
            when = datetime.fromtimestamp(int(value), timezone.utc)
        else:
            when = datetime.fromisoformat(value)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            when = when.astimezone(timezone.utc)
    except (ValueError, OverflowError, OSError):
        return ""
    if not _EARLIEST <= when <= _LATEST:
        return ""
    return when.replace(microsecond=0).isoformat()


class Collector:
    """Counts and builds Bookmarks for one parse. Every row goes through
    add() or bad(), so `found` is always the sum of the three outcomes."""

    def __init__(self, source: str, max_items: int):
        self.source = source
        self.max_items = max_items
        self.bookmarks: list[Bookmark] = []
        self.found = self.bad_rows = self.unsupported_scheme = 0

    def bad(self) -> None:
        self.found += 1
        self.bad_rows += 1

    def add(self, url, *, title="", folder="", tags=(), note="", saved_at="") -> None:
        url = _CONTROL.sub("", str(url or "")).strip()
        if not url:
            self.bad()
            return
        if len(url) > MAX_URL:
            self.bad()
            return
        try:
            parts = urlsplit(url)
        except ValueError:
            self.bad()
            return
        scheme = parts.scheme.lower()
        if scheme in ("http", "https") and not parts.netloc:
            self.bad()
            return
        self.found += 1
        if scheme not in ("http", "https"):
            self.unsupported_scheme += 1
            return
        if len(self.bookmarks) >= self.max_items:
            raise LimitExceeded(f"file has more than {self.max_items:,} bookmarks; "
                                f"the limit is {self.max_items:,}")
        self.bookmarks.append(Bookmark(
            url=url, title=sanitize_text(title, MAX_TITLE),
            folder=sanitize_text(folder, MAX_FOLDER), tags=sanitize_tags(tags),
            note=sanitize_text(note, MAX_NOTE), saved_at=parse_timestamp(saved_at),
            source=self.source))

    def result(self) -> ParseResult:
        return ParseResult(self.bookmarks, self.found, self.bad_rows, self.unsupported_scheme)


def header_names(head: bytes) -> set[str]:
    """The lower-cased, stripped column names on the first line of a CSV head."""
    first = head.decode("utf-8-sig", errors="replace").splitlines()[:1]
    try:
        row = next(csv.reader(first), [])
    except csv.Error:
        return set()
    return {name.strip().lower() for name in row}


def csv_rows(path, *, required: tuple[str, ...], label: str):
    """Yield each data row as a dict keyed by lower-cased, stripped header
    names. A missing required column or unreadable CSV raises ParseError."""
    csv.field_size_limit(MAX_CSV_FIELD)
    with open(path, encoding="utf-8-sig", errors="replace", newline="") as handle:
        reader = csv.DictReader(handle)
        try:
            reader.fieldnames = [name.strip().lower() for name in reader.fieldnames or []]
            for name in required:
                if name not in reader.fieldnames:
                    raise ParseError(f"{label} export is missing column {name}")
            yield from reader
        except csv.Error as e:
            line = reader.reader.line_num     # DictReader's own line_num lags a failed row
            raise ParseError(f"{label} export could not be read at line {line}") from e


def module_for(name: str):
    """The parser module for a --format value."""
    if name not in SOURCES:
        raise ParseError(f"unknown format {name}")
    return importlib.import_module(f"{__name__}.{name}")


def detect(path):
    """Try netscape, pocket, raindrop in that order on the first 64 KiB of
    the file; return the first module whose sniff is True."""
    with open(path, "rb") as handle:
        head = handle.read(SNIFF_BYTES)
    for name in SOURCES:
        module = module_for(name)
        if module.sniff(head):
            return module
    raise ParseError("could not recognize this file as netscape, pocket or raindrop; "
                     "pass --format")
