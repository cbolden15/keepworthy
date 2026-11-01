import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import media

FIX = Path(__file__).parent / "fixtures"

def fake_runner(stdout):
    def _run(cmd, **kw):
        return SimpleNamespace(stdout=stdout, stderr="", returncode=0)
    return _run

def test_list_playlist_ids_oldest_first():
    # yt-dlp prints newest first; we reverse to oldest→newest
    run = fake_runner("id3\nid2\nid1\n")
    assert media.list_playlist_ids("https://pl", runner=run) == ["id1", "id2", "id3"]

def test_get_metadata_maps_fields():
    run = fake_runner((FIX / "metadata.json").read_text())
    md = media.get_metadata("abc123", runner=run)
    assert md["title"] == "Build a RAG agent"
    assert md["channel"] == "DevChannel"
    assert md["video_id"] == "abc123"
    assert md["published"] == "2026-07-18"
    assert md["duration"] == "00:21:14"
    assert md["url"] == "https://www.youtube.com/watch?v=abc123"

def test_youtube_video_id_recognizes_watch_short_and_shorts_urls():
    assert media.youtube_video_id("https://www.youtube.com/watch?v=abc123&list=x") == "abc123"
    assert media.youtube_video_id("https://youtu.be/abc123?t=10") == "abc123"
    assert media.youtube_video_id("https://youtube.com/shorts/abc123") == "abc123"
    assert media.youtube_video_id("https://example.com/watch?v=abc123") is None

def test_fetch_captions_none_when_no_file(tmp_path):
    run = fake_runner("")  # yt-dlp wrote nothing
    assert media.fetch_captions_vtt("abc123", "en", tmp_path, runner=run) is None

def test_fetch_captions_returns_vtt(tmp_path):
    # simulate yt-dlp having written a vtt file
    (tmp_path / "abc123.en.vtt").write_text("WEBVTT\n\nhello")
    run = fake_runner("")
    out = media.fetch_captions_vtt("abc123", "en", tmp_path, runner=run)
    assert out is not None and "hello" in out

def test_list_channel_ids_command_shape():
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    media.list_channel_ids("https://www.youtube.com/@c", runner=run)
    assert calls == [[
        "yt-dlp", "--flat-playlist", "--playlist-end", "15",
        "--print", "%(id)s|%(duration)s",
        "https://www.youtube.com/@c/videos",
    ]]


def test_list_channel_ids_oldest_first_and_marks_unpublished():
    # yt-dlp prints newest first; an upcoming premiere has no duration yet.
    run = fake_runner("soon|NA\nnew|647.0\nold|86.0\nblank|\n")
    assert media.list_channel_ids("https://www.youtube.com/@c", runner=run) == [
        ("blank", False), ("old", True), ("new", True), ("soon", False),
    ]


def test_list_channel_ids_respects_limit():
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    media.list_channel_ids("https://www.youtube.com/@c", limit=3, runner=run)
    assert calls[0][calls[0].index("--playlist-end") + 1] == "3"


def test_list_channel_ids_propagates_failure():
    def run(cmd, **kw):
        raise subprocess.CalledProcessError(1, cmd)

    with pytest.raises(subprocess.CalledProcessError):
        media.list_channel_ids("https://www.youtube.com/@missing", runner=run)
