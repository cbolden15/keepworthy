import json
import stat
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from src import ingest, saved_discovery as discovery, social


def raw(code="abc", video=False):
    return {"code": code, "media_type": 2 if video else 1,
            "caption": {"text": "Source caption"}, "user": {"username": "author"}}


def client(**overrides):
    return SimpleNamespace(has_more=True, instagram=lambda n: [raw()],
                           facebook=lambda n: [], threads=lambda n: [], **overrides)


def test_normalizes_each_feed_and_retains_only_source_projection():
    key, item = discovery.normalize("instagram", {**raw(), "token": "secret"}, "date")
    assert key == ingest.url_key(item["url"])
    assert item["saved_caption"] == "Source caption"
    assert item["why"] == "" and item["capture_source"] == "saved_feed"
    assert "secret" not in json.dumps(item)
    assert discovery.normalize("threads", {"thread_items": [{"post": raw()}]}, "date")[1]["url"].endswith("/@author/post/abc")
    fb = {"savable": {"savable_permalink": "https://www.facebook.com/watch/?v=123",
                      "savable_default_category": "VIDEO", "savable_title": {"text": "caption"}}}
    assert discovery.normalize("facebook", fb, "date")[1]["saved_content_kind"] == "video"


@pytest.mark.parametrize("url", ["https://evil.example/a", "https://www.facebook.com@evil.example/a",
                                      "http://www.facebook.com/a", "https://www.facebook.com:123/a"])
def test_rejects_foreign_permalinks(url):
    with pytest.raises(ValueError):
        discovery.normalize("facebook", {"savable": {"savable_permalink": url}}, "date")


def test_backfill_limits_then_daily_stops_at_previous_boundary(tmp_path):
    db = discovery.open_database(tmp_path / "db")
    q = tmp_path / "q.jsonl"
    c = client()
    calls = []
    c.instagram = lambda n: calls.append(n) or [raw("old")]
    first = discovery.discover(db, q, c)
    assert first["instagram"]["new"] == 1
    c.instagram = lambda n: calls.append(n) or [raw("new"), raw("old"), {"code": "unsupported/older"}]
    second = discovery.discover(db, q, c)
    assert calls == [25, 100]
    assert second["instagram"]["new"] == 1
    assert discovery.status(db)["discovered"] == 2  # no unrequested older backfill
    assert discovery.discover(db, q, c)["instagram"]["new"] == 0
    assert len(q.read_text().splitlines()) == 2
    assert stat.S_IMODE((tmp_path / "db").stat().st_mode) == 0o600


def test_gap_is_sticky_and_not_reported_as_recovered_next_day(tmp_path):
    db = discovery.open_database(tmp_path / "db")
    q = tmp_path / "q"
    c = client()
    discovery.discover(db, q, c, "day1")
    c.instagram = lambda n: [raw("new")]
    assert discovery.discover(db, q, c, "day2")["instagram"]["status"] == "gap"
    assert discovery.discover(db, q, c, "day3")["instagram"]["status"] == "gap"
    assert db.execute("SELECT gap_since FROM feeds WHERE platform='instagram'").fetchone() == ("day2",)


def test_end_of_feed_without_overlap_does_not_indicate_gap(tmp_path):
    db = discovery.open_database(tmp_path / "db")
    c = client()
    discovery.discover(db, tmp_path / "q", c)
    c.has_more = False
    c.instagram = lambda n: [raw("changed")]
    assert discovery.discover(db, tmp_path / "q", c)["instagram"]["status"] == "ok"


def test_one_platform_failure_keeps_other_feeds_running_and_snapshot(tmp_path):
    db = discovery.open_database(tmp_path / "db")
    c = client()
    discovery.discover(db, tmp_path / "q", c, "day1")
    def reject(n):
        raise HTTPError("https://example.invalid/?token=secret", 403, "secret", {}, None)
    c.instagram = reject
    c.threads = lambda n: [{"thread_items": [{"post": raw("thread")}]}]
    result = discovery.discover(db, tmp_path / "q", c, "day2")
    assert result["instagram"]["status"] == "login_required"
    assert "secret" not in json.dumps(result)
    assert result["threads"]["new"] == 1
    assert db.execute("SELECT last_success FROM feeds WHERE platform='instagram'").fetchone() == ("day1",)


def test_invalid_record_does_not_commit_partial_page_or_initialization(tmp_path):
    db = discovery.open_database(tmp_path / "db")
    c = client()
    c.instagram = lambda n: [raw(), {"code": "bad/code"}]
    assert discovery.discover(db, tmp_path / "q", c)["instagram"]["status"] == "feed_error"
    assert discovery.status(db)["discovered"] == 0
    assert db.execute("SELECT last_success FROM feeds WHERE platform='instagram'").fetchone() == (None,)


def test_crash_after_queue_append_replays_without_losing_outbox(tmp_path, monkeypatch):
    db = discovery.open_database(tmp_path / "db")
    q = tmp_path / "q"
    key, item = discovery.normalize("instagram", raw(), "day1")
    with db:
        db.execute("INSERT INTO items(key,platform,payload) VALUES(?,?,?)", (key, "instagram", json.dumps(item)))
    append = ingest.append_item
    def crash(queue, item):
        append(queue, item)
        raise OSError("simulated crash")
    monkeypatch.setattr(ingest, "append_item", crash)
    with pytest.raises(OSError):
        discovery.flush_outbox(db, q)
    assert discovery.status(db)["outbox_pending"] == 1
    monkeypatch.setattr(ingest, "append_item", append)
    assert discovery.flush_outbox(db, q) == 1
    items = [json.loads(line) for line in q.read_text().splitlines()]
    assert ingest.url_key(items[0]["url"]) == ingest.url_key(items[1]["url"])


def test_free_only_video_waits_without_fetch_or_paid_transcription(tmp_path):
    key, item = discovery.normalize("instagram", raw(video=True), "date")
    cfg = SimpleNamespace(state_path=tmp_path / "state")
    assert social.process_social_item(cfg, tmp_path, item, "date", SimpleNamespace(), key, "prompt") == "waiting-free-transcription"
    assert not cfg.state_path.exists()


def test_free_only_detected_video_waits_before_paid_transcription(tmp_path):
    key, item = discovery.normalize("instagram", raw(), "date")
    deps = SimpleNamespace(fetch_social_media=lambda *a: {"kind": "video", "meta": {}, "files": [tmp_path / "video"]})
    cfg = SimpleNamespace(state_path=tmp_path / "state", cookies_path=None)
    assert social.process_social_item(cfg, tmp_path, item, "date", deps, key, "prompt") == "waiting-free-transcription"
    assert item["saved_content_kind"] == "video"
    assert not cfg.state_path.exists()


def test_waiting_video_stays_durable_in_queue(tmp_path):
    _, item = discovery.normalize("instagram", raw(video=True), "date")
    q = tmp_path / "q"
    ingest.append_item(q, item)
    (tmp_path / "prompt").write_text("prompt")
    cfg = SimpleNamespace(state_path=tmp_path / "state", queue_path=q, social_prompt_path="prompt")
    assert social.drain_social_queue(cfg, tmp_path, "date", SimpleNamespace())[0][1] == "waiting-free-transcription"
    assert json.loads(q.read_text())["url"] == item["url"]
    assert not ingest._processing_path(q).exists()


def test_config_requires_owner_only_credentials(tmp_path):
    cfg = tmp_path / "config"
    cookies = tmp_path / "cookies"
    cookies.write_text("secret")
    cfg.write_text(json.dumps({"cookies": str(cookies), "database": str(tmp_path / "db"), "queue": str(tmp_path / "q")}))
    cfg.chmod(0o600)
    cookies.chmod(0o644)
    with pytest.raises(ValueError, match="owner-only"):
        discovery.load_config(cfg)
    cookies.chmod(0o600)
    assert discovery.load_config(cfg)["cookies"] == cookies
