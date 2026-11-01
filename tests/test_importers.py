from pathlib import Path

import pytest

from src import importers
from src.importers import (LimitExceeded, ParseError, netscape, parse_timestamp, pocket,
                           raindrop, sanitize_tags, sanitize_text)

FIXTURES = Path(__file__).parent / "fixtures" / "import"


def _head(name):
    return (FIXTURES / name).read_bytes()[:importers.SNIFF_BYTES]


def test_netscape_sniff_doctype():
    assert netscape.sniff(b"<!DOCTYPE NETSCAPE-Bookmark-file-1>\n<DL><p>\n")
    assert netscape.sniff(_head("bookmarks.html"))


def test_netscape_sniff_dl_anchor():
    assert netscape.sniff(b'<html><dl><p><dt><a href="https://example.com/">x</a></dl>')
    assert not netscape.sniff(b'<html><body><a href="https://example.com/">x</a></body>')


def test_pocket_sniff_header():
    assert pocket.sniff(_head("pocket.csv"))
    assert not pocket.sniff(_head("raindrop.csv"))


def test_raindrop_sniff_header():
    assert raindrop.sniff(_head("raindrop.csv"))
    assert not raindrop.sniff(b"url,created\nhttps://example.com/,2024-01-01\n")
    assert not raindrop.sniff(_head("pocket.csv"))


def test_detect_routes_each_fixture_to_its_own_parser():
    assert importers.detect(FIXTURES / "bookmarks.html") is netscape
    assert importers.detect(FIXTURES / "pocket.csv") is pocket
    assert importers.detect(FIXTURES / "raindrop.csv") is raindrop
    with pytest.raises(ParseError) as err:
        importers.detect(FIXTURES / "not-a-bookmark-file.txt")
    message = str(err.value)
    assert "netscape, pocket or raindrop" in message and "--format" in message


def test_sniff_order_is_fixed(tmp_path):
    both = tmp_path / "both.csv"
    both.write_text("url,time_added,created,folder\nhttps://example.com/,1700000000,,\n")
    assert pocket.sniff(both.read_bytes()) and raindrop.sniff(both.read_bytes())
    assert importers.detect(both) is pocket


def test_netscape_nested_folders_tags_dates():
    result = netscape.parse(FIXTURES / "bookmarks.html")
    by_title = {b.title: b for b in result.bookmarks}
    assert [b.url for b in result.bookmarks] == [
        "https://example.com/root", "https://example.com/essay",
        "https://example.org/k8s", "https://example.com/essay"]
    assert by_title["Root page"].folder == ""
    assert by_title["An essay"].folder == "Reading"
    assert by_title["An essay"].tags == ("essays", "long reads")
    assert by_title["An essay"].saved_at == "2024-03-09T12:34:56+00:00"
    assert by_title["Cluster notes & tips"].folder == "Reading/Infra"
    assert by_title["Cluster notes & tips"].tags == ("python", "async")
    assert by_title["An essay again"].folder == ""
    assert {b.source for b in result.bookmarks} == {"netscape"}
    assert {b.note for b in result.bookmarks} == {""}


def test_netscape_malformed_tail_keeps_completed_anchors():
    result = netscape.parse(FIXTURES / "bookmarks.html")
    assert "https://example.org/x" not in [b.url for b in result.bookmarks]
    assert result.found == 7         # 4 kept + 2 unsupported + 1 without href
    assert result.found == len(result.bookmarks) + result.unsupported_scheme + result.bad_rows


def test_netscape_drops_unsupported_schemes_and_counts():
    result = netscape.parse(FIXTURES / "bookmarks.html")
    assert result.unsupported_scheme == 2
    assert result.bad_rows == 1
    assert not any(b.url.startswith(("javascript:", "place:")) for b in result.bookmarks)


def test_pocket_pipe_tags_and_archive_status():
    result = pocket.parse(FIXTURES / "pocket.csv")
    by_url = {b.url: b for b in result.bookmarks}
    assert result.found == 5 and result.bad_rows == 1 and result.unsupported_scheme == 0
    first = by_url["https://example.com/async"]
    assert first.title == "Async patterns"             # BOM stripped: the title column is read
    assert first.tags == ("python", "async")
    assert first.saved_at == "2023-11-14T22:13:20+00:00"
    assert "https://example.org/bread" in by_url        # the archive row is kept
    assert by_url["https://example.org/bread"].tags == ()
    assert by_url["https://example.org/undated"].saved_at == ""
    assert {b.folder for b in result.bookmarks} == {""}


def test_pocket_missing_column_names_it(tmp_path):
    path = tmp_path / "list.csv"
    path.write_text("title,url,tags\nA,https://example.com/a,\n")
    with pytest.raises(ParseError) as err:
        pocket.parse(path)
    assert "time_added" in str(err.value)
    assert "example.com" not in str(err.value)


def test_raindrop_note_and_highlights():
    result = raindrop.parse(FIXTURES / "raindrop.csv")
    by_url = {b.url: b for b in result.bookmarks}
    assert result.found == 4 and result.bad_rows == 0
    paper = by_url["https://example.com/paper"]
    assert paper.note == "First line of a note second line"
    assert paper.folder == "Parent/Child"
    assert paper.tags == ("ml", "papers")
    assert paper.saved_at == "2024-03-09T12:34:56+00:00"
    highlighted = by_url["https://example.org/highlighted"]
    assert (highlighted.title, highlighted.note) == ("Highlighted page", "")   # highlights ignored
    assert by_url["https://example.org/undated"].saved_at == ""


def test_raindrop_missing_created_names_it():
    with pytest.raises(ParseError) as err:
        raindrop.parse(FIXTURES / "raindrop-missing-created.csv")
    assert "created" in str(err.value)


def test_header_matching_is_case_and_space_insensitive(tmp_path):
    path = tmp_path / "export.csv"
    path.write_text(" URL ,Created, Folder\nhttps://example.com/a,2024-01-02T03:04:05Z,Inbox\n")
    result = raindrop.parse(path)
    assert [(b.url, b.folder, b.saved_at) for b in result.bookmarks] == [
        ("https://example.com/a", "Inbox", "2024-01-02T03:04:05+00:00")]


def test_sanitize_limits():
    assert sanitize_text("a" * 400, importers.MAX_TITLE) == "a" * 300
    assert sanitize_text("f" * 300, importers.MAX_FOLDER) == "f" * 200
    assert sanitize_text("a\x00b\x1bc\x85d\x9fe", 50) == "abc de"
    assert sanitize_text("tab\there\nnew  line\x00!", 50) == "tab here new line!"
    assert sanitize_text("\x07bell\x7f\x9b", 50) == "bell"
    tags = sanitize_tags([f"t{i}" for i in range(25)])
    assert len(tags) == 20 and tags[0] == "t0" and tags[-1] == "t19"
    assert sanitize_tags(["x" * 60]) == ("x" * 50,)
    assert sanitize_tags(["Python", " python ", "", "  ", "async"]) == ("Python", "async")


def test_parse_timestamp():
    assert parse_timestamp("1709987696") == "2024-03-09T12:34:56+00:00"
    assert parse_timestamp("2024-03-09T12:34:56.000Z") == "2024-03-09T12:34:56+00:00"
    assert parse_timestamp("2024-03-09T12:34:56") == "2024-03-09T12:34:56+00:00"
    assert parse_timestamp("2024-03-09T14:34:56+02:00") == "2024-03-09T12:34:56+00:00"
    assert parse_timestamp("next tuesday") == ""
    assert parse_timestamp("") == ""
    assert parse_timestamp("0") == ""                         # 1970
    assert parse_timestamp("2200-01-01T00:00:00Z") == ""
    assert parse_timestamp("1709987696000000") == ""          # microseconds: out of range


def test_bookmark_cap_raises_before_memory(tmp_path):
    path = tmp_path / "many.html"
    anchors = "".join(f'<DT><A HREF="https://example.com/{i}">n{i}</A>\n' for i in range(11))
    path.write_text(f"<!DOCTYPE NETSCAPE-Bookmark-file-1>\n<DL><p>\n{anchors}</DL>\n")
    with pytest.raises(LimitExceeded) as err:
        netscape.parse(path, max_items=10)
    assert str(err.value) == "file has more than 10 bookmarks; the limit is 10"
    assert len(netscape.parse(path, max_items=11).bookmarks) == 11


def test_csv_field_size_limit_applied(tmp_path):
    path = tmp_path / "list.csv"
    big = "x" * (300 * 1024)
    path.write_text(f"title,url,time_added\nok,https://example.com/a,1700000000\n"
                    f"\"{big}\",https://example.com/b,1700000000\n")
    with pytest.raises(ParseError) as err:
        pocket.parse(path)
    assert "line 3" in str(err.value)
