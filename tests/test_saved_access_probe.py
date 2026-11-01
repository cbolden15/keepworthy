"""Synthetic saved-feed boundaries; these tests never contact Meta."""
import importlib.util
import json
from pathlib import Path
import stat

import pytest

spec = importlib.util.spec_from_file_location(
    "saved_probe", Path(__file__).parents[1] / "bin" / "probe_saved_access.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def test_logged_out_page_is_not_an_empty_success():
    with pytest.raises(ValueError, match="Missing saved_media"):
        probe.connection(probe.documents('<html><title>Log in</title></html>'), "saved_media")


def test_instagram_uses_cursor_and_respects_limit(monkeypatch):
    monkeypatch.setattr(probe.time, "sleep", lambda _: None)
    client = object.__new__(probe.Client)
    calls = []
    responses = iter([
        {"status": "ok", "items": [{"media": {"id": "one"}}],
         "more_available": True, "next_max_id": "page two"},
        {"status": "ok", "items": [{"media": {"id": "two"}}, {"media": {"id": "three"}}],
         "more_available": False},
    ])
    def request(url, headers):
        calls.append(url)
        return json.dumps(next(responses))
    client.request = request
    assert client.instagram(2) == [{"id": "one"}, {"id": "two"}]
    assert calls[1].endswith("?max_id=page+two")


def test_instagram_repeated_cursor_fails_instead_of_looping(monkeypatch):
    monkeypatch.setattr(probe.time, "sleep", lambda _: None)
    client = object.__new__(probe.Client)
    client.request = lambda *_: json.dumps(
        {"status": "ok", "items": [], "more_available": True, "next_max_id": "same"})
    with pytest.raises(ValueError, match="repeated"):
        client.instagram(2)


def test_threads_does_not_silently_truncate_unverified_pagination():
    client = object.__new__(probe.Client)
    client.request = lambda *_: '<script type="application/json">' + json.dumps(
        {"saved_media": {"edges": [], "page_info": {"has_next_page": True}}}) + '</script>'
    with pytest.raises(ValueError, match="pagination replay is not verified"):
        client.threads(10)


def test_private_output_preserves_existing_parent_permissions(tmp_path):
    tmp_path.chmod(0o750)
    output = tmp_path / "saved.json"
    probe.private_json(output, {"private": "fixture"})
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o750
