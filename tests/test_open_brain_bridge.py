import json

import pytest

from src.open_brain_bridge import write_youtube_capture


def test_write_youtube_capture_is_atomic_and_idempotent(tmp_path):
    root = tmp_path / "Brain" / ".open-brain-ingress" / "youtube"

    first = write_youtube_capture(
        root,
        video_id="dQw4w9WgXcQ",
        transcript="  Local transcript  ",
    )
    second = write_youtube_capture(
        root,
        video_id="dQw4w9WgXcQ",
        transcript="Replacement transcript",
        why="Use this implementation pattern.",
    )

    assert second == first
    assert json.loads(second.read_text()) == {
        "privacy": "work",
        "schema_version": 1,
        "text": "Replacement transcript",
        "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "why": "Use this implementation pattern.",
    }
    assert list(root.glob("*.tmp")) == []


def test_write_youtube_capture_rejects_unbounded_or_invalid_input(tmp_path):
    root = tmp_path / "youtube"

    with pytest.raises(ValueError):
        write_youtube_capture(root, video_id="invalid", transcript="text")
    with pytest.raises(ValueError):
        write_youtube_capture(
            root,
            video_id="dQw4w9WgXcQ",
            transcript="x" * 100_001,
        )

import hashlib
import os
import stat

from src.open_brain_bridge import saved_item_path, write_saved_item


def _write(root, **over):
    fields = dict(
        key="https://www.instagram.com/reel/A",
        url="https://www.instagram.com/reel/A/?igsh=abc",
        why="  Adapt this hook.  ",
        received_at="2026-10-26T14:00:00.123456+00:00",
        fetch_status="complete",
        source_type="social",
        content_kind="post",
        platform="instagram",
        title="T",
        author="@a",
        body="cap",
        media_text="spoken words",
        shared_text="note",
        summary="# N\nbody",
    )
    fields.update(over)
    return write_saved_item(root, **fields)


def test_write_saved_item_writes_record_v1(tmp_path):
    root = tmp_path / "open-brain-saved"
    path = _write(root)
    expected_name = hashlib.sha256(b"https://www.instagram.com/reel/A").hexdigest()[:32]
    assert path == root / f"{expected_name}.json"
    assert path == saved_item_path(root, "https://www.instagram.com/reel/A")
    assert json.loads(path.read_text()) == {
        "schema_version": 1,
        "url": "https://www.instagram.com/reel/A/?igsh=abc",
        "why": "Adapt this hook.",
        "received_at": "2026-10-26T14:00:00Z",
        "fetch_status": "complete",
        "source_type": "social",
        "content_kind": "post",
        "platform": "instagram",
        "title": "T",
        "author": "@a",
        "body": "cap",
        "media_text": "spoken words",
        "shared_text": "note",
        "summary": "# N\nbody",
        "summary_origin": "producer:x1-claude",
    }
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(root).st_mode) == 0o700
    assert list(root.glob("*.tmp")) == [] and list(root.glob(".*")) == []


def test_write_saved_item_keeps_the_first_record(tmp_path):
    root = tmp_path / "spool"
    first = _write(root)
    before = first.read_bytes()
    second = _write(root, summary="different", why="Other reason")
    assert second == first
    assert first.read_bytes() == before


def test_write_saved_item_without_summary_has_no_origin(tmp_path):
    record = json.loads(_write(tmp_path / "s", summary=None).read_text())
    assert record["summary"] is None and record["summary_origin"] is None


def test_write_saved_item_normalizes_received_at_to_utc_z(tmp_path):
    offset = json.loads(_write(tmp_path / "a", received_at="2026-10-26T09:00:00-05:00").read_text())
    assert offset["received_at"] == "2026-10-26T14:00:00Z"
    naive = json.loads(_write(tmp_path / "b", received_at="2026-10-26T14:00:00").read_text())
    assert naive["received_at"] == "2026-10-26T14:00:00Z"


@pytest.mark.parametrize("over", [
    {"url": "http://example.com/a"},
    {"why": "   "},
    {"why": "line one\nline two"},
    {"why": "x" * 281},
    {"fetch_status": "done"},
    {"source_type": "podcast"},
    {"content_kind": "reel"},
    {"received_at": "yesterday"},
    {"key": ""},
])
def test_write_saved_item_rejects_invalid_input(tmp_path, over):
    with pytest.raises(ValueError):
        _write(tmp_path / "s", **over)
    assert not (tmp_path / "s").exists() or list((tmp_path / "s").iterdir()) == []


def test_write_saved_item_truncates_long_content_to_the_file_bound(tmp_path):
    path = _write(tmp_path / "s", media_text="m" * 200_000, body="b" * 50_000)
    assert path.stat().st_size <= 131_072
    record = json.loads(path.read_text())
    assert record["media_text"].endswith("[truncated]")
    assert record["why"] == "Adapt this hook."


def test_write_saved_item_bounds_title_author_platform(tmp_path):
    record = json.loads(_write(
        tmp_path / "s", title="t" * 400, author="a" * 300, platform="P" * 60,
    ).read_text())
    assert len(record["title"]) == 300
    assert len(record["author"]) == 200
    assert record["platform"] == "p" * 40


def test_saved_item_concurrent_first_writer_wins(tmp_path, monkeypatch):
    root = tmp_path / "s"
    real_link = os.link
    def racing_link(src, dst):
        dst.write_text('first writer')
        real_link(src, dst)
    monkeypatch.setattr(os, "link", racing_link)
    path = _write(root)
    assert path.read_text() == 'first writer'
    assert list(root.glob(".*")) == []


@pytest.mark.parametrize("operation", ["write", "fsync"])
def test_failed_atomic_write_cleans_temporary_file(tmp_path, monkeypatch, operation):
    root = tmp_path / "s"
    def fail(*args):
        raise OSError("synthetic failure")
    monkeypatch.setattr(os, operation, fail)
    with pytest.raises(OSError, match="synthetic failure"):
        _write(root)
    assert list(root.iterdir()) == []


@pytest.mark.parametrize("field,value", [
    ("received_at", []),
    ("fetch_status", []),
    ("source_type", []),
    ("content_kind", []),
    ("platform", 123),
    ("title", 123),
    ("author", 123),
    ("body", 123),
    ("media_text", 123),
    ("shared_text", 123),
    ("summary", 123),
    ("body", None),
    ("summary", 0),
])
def test_write_saved_item_rejects_non_string_fields(tmp_path, field, value):
    with pytest.raises(ValueError):
        _write(tmp_path / "s", **{field: value})
    assert not (tmp_path / "s").exists()


@pytest.mark.parametrize("separator", ["\u0085", "\u009f", "\u2028", "\u2029"])
def test_write_saved_item_rejects_unicode_controls_and_line_separators(tmp_path, separator):
    with pytest.raises(ValueError):
        _write(tmp_path / "s", why=f"before{separator}after")
    assert not (tmp_path / "s").exists()
