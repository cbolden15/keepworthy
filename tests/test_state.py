# tests/test_state.py
from src import state

def test_load_missing_returns_empty(tmp_path):
    st = state.load_state(tmp_path / "nope.json")
    assert st == {"seen": [], "attempts": {}}

def test_mark_seen_persists_and_dedupes(tmp_path):
    p = tmp_path / "state.json"
    state.mark_seen(p, "abc")
    state.mark_seen(p, "abc")
    st = state.load_state(p)
    assert st["seen"] == ["abc"]
    assert state.is_seen(st, "abc") is True
    assert state.is_seen(st, "xyz") is False

def test_record_attempt_increments(tmp_path):
    p = tmp_path / "state.json"
    assert state.record_attempt(p, "abc") == 1
    assert state.record_attempt(p, "abc") == 2

def test_mark_seen_clears_attempts(tmp_path):
    p = tmp_path / "state.json"
    state.record_attempt(p, "abc")
    state.mark_seen(p, "abc")
    st = state.load_state(p)
    assert "abc" not in st["attempts"]

def test_queue_video_preserves_context_until_note_is_seen(tmp_path):
    path = tmp_path / "state.json"
    assert state.queue_video(path, "abc", "Compare this with my agent stack.",
                             "2026-08-13T10:00:00Z") is True
    data = state.load_state(path)
    assert state.requested_videos(data) == ["abc"]
    assert state.capture_context(data, "abc") == {
        "why": "Compare this with my agent stack.",
        "captured_at": "2026-08-13T10:00:00Z",
        "source": "share",
    }

    state.mark_seen(path, "abc")
    data = state.load_state(path)
    assert state.requested_videos(data) == []
    assert state.capture_context(data, "abc") == {}

def test_queue_video_does_not_requeue_seen_video(tmp_path):
    path = tmp_path / "state.json"
    state.mark_seen(path, "abc")
    assert state.queue_video(path, "abc", "Why", "2026-08-13T10:00:00Z") is False


URL = "https://www.youtube.com/@c"


def test_register_channel_baseline_marks_seen_and_queues_nothing(tmp_path):
    p = tmp_path / "state.json"
    assert state.register_channel_videos(p, URL, ["a", "b"], "t0") == []
    st = state.load_state(p)
    assert state.is_seen(st, "a") and state.is_seen(st, "b")
    assert state.requested_videos(st) == []
    assert state.is_channel_baselined(st, URL)
    assert st["channels"][URL] == {"baselined_at": "t0"}


def test_register_channel_baseline_with_no_videos_still_baselines(tmp_path):
    p = tmp_path / "state.json"
    assert state.register_channel_videos(p, URL, [], "t0") == []
    assert state.is_channel_baselined(state.load_state(p), URL)


def test_register_channel_queues_only_unseen_after_baseline(tmp_path):
    p = tmp_path / "state.json"
    state.register_channel_videos(p, URL, ["a"], "t0")
    assert state.register_channel_videos(p, URL, ["a", "new"], "t1") == ["new"]
    st = state.load_state(p)
    assert state.requested_videos(st) == ["new"]
    assert state.capture_context(st, "new") == {
        "why": "", "captured_at": "t1", "source": "channel",
    }
    assert st["channels"][URL] == {"baselined_at": "t0"}


def test_register_channel_baseline_skips_in_flight_ids(tmp_path):
    p = tmp_path / "state.json"
    state.queue_video(p, "shared", "Review this.", "t0")
    state.record_attempt(p, "retrying")
    assert state.register_channel_videos(
        p, URL, ["shared", "retrying", "old"], "t1") == []
    st = state.load_state(p)
    assert state.is_seen(st, "old")
    assert not state.is_seen(st, "shared")
    assert not state.is_seen(st, "retrying")
    assert state.requested_videos(st) == ["shared"]
    assert state.capture_context(st, "shared")["source"] == "share"
    assert state.is_channel_baselined(st, URL)


def test_register_channel_does_not_duplicate_requested(tmp_path):
    p = tmp_path / "state.json"
    state.register_channel_videos(p, URL, [], "t0")
    state.register_channel_videos(p, URL, ["new"], "t1")
    assert state.register_channel_videos(p, URL, ["new"], "t2") == []
    st = state.load_state(p)
    assert state.requested_videos(st) == ["new"]
    assert state.capture_context(st, "new")["captured_at"] == "t1"


def test_register_channel_keeps_share_context_for_already_requested(tmp_path):
    p = tmp_path / "state.json"
    state.register_channel_videos(p, URL, [], "t0")
    state.queue_video(p, "shared", "Review this.", "t1")
    assert state.register_channel_videos(p, URL, ["shared"], "t2") == []
    assert state.capture_context(state.load_state(p), "shared")["source"] == "share"


def test_mark_seen_clears_channel_queue_entry(tmp_path):
    p = tmp_path / "state.json"
    state.register_channel_videos(p, URL, [], "t0")
    state.register_channel_videos(p, URL, ["new"], "t1")
    state.mark_seen(p, "new")
    st = state.load_state(p)
    assert state.requested_videos(st) == []
    assert state.capture_context(st, "new") == {}
    assert state.is_channel_baselined(st, URL)
