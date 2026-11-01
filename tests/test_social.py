import urllib.error

import pytest

from src.social import (
    FetchError,
    classify_social_url,
    content_kind_for,
    fetch_page_text,
    filing_route,
    source_type_for,
)
from src import social


@pytest.mark.parametrize("url,expected", [
    ("https://www.instagram.com/reel/ABC123/", "instagram"),
    ("https://instagram.com/p/XYZ789/?igsh=1", "instagram"),
    ("https://www.facebook.com/user/posts/123", "facebook"),
    ("https://m.facebook.com/user/posts/123", "facebook"),
    ("https://fb.watch/abc/", "facebook"),
    ("https://www.threads.net/@user/post/C123", "threads"),
    ("https://www.threads.com/@user/post/C123", "threads"),
    ("https://twitter.com/user/status/123", "twitter"),
    ("https://x.com/user/status/123", "twitter"),
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "youtube"),
    ("https://m.youtube.com/watch?v=dQw4w9WgXcQ", "youtube"),
    ("https://youtu.be/dQw4w9WgXcQ", "youtube"),
    ("https://example.com/article", "generic"),
    # host-based, not substring: a lookalike apex must not match
    ("https://notinstagram.com/reel/ABC123/", "generic"),
    # a redirect/link-shortener URL carrying a platform name only in its
    # path or query must not be misclassified
    ("https://short.link/r?u=https://instagram.com/p/XYZ", "generic"),
    ("https://bit.ly/instagram.com/foo", "generic"),
    # a trailing-dot host is DNS-equivalent to the apex and must classify
    # the same way
    ("https://instagram.com./p/x", "instagram"),
])
def test_classify_social_url(url, expected):
    assert classify_social_url(url) == expected


def test_saved_content_dimensions_and_event_route_are_closed():
    assert source_type_for("instagram") == "social"
    assert source_type_for("generic") == "web"
    assert content_kind_for("https://www.stubhub.com/event/123", "generic") == "event"
    assert content_kind_for("https://example.com/x", "generic", "product") == "product"
    assert content_kind_for("https://example.com/x", "generic", "made-up") == "other"
    assert filing_route({"topic": "Other"}, "event") == (
        "Personal",
        "Events & Experiences",
    )


class FakeResponse:
    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeOpener:
    def __init__(self, body):
        self._body = body

    def open(self, req, timeout=None):
        return FakeResponse(self._body)


class CapturingOpener:
    def __init__(self, body=b"<html></html>"):
        self._body = body
        self.req = None
        self.timeout = None

    def open(self, req, timeout=None):
        self.req = req
        self.timeout = timeout
        return FakeResponse(self._body)


def test_fetch_page_text_parses_og_tags():
    html = b"""<html><head>
      <meta property="og:title" content="A Post"/>
      <meta property="og:description" content="The caption text"/>
      <meta name="author" content="@someone"/>
    </head><body>hi</body></html>"""
    out = fetch_page_text("https://x.com/u/status/1", None, opener=FakeOpener(html))
    assert out == {"title": "A Post", "text": "The caption text", "author": "@someone"}


def test_fetch_page_text_raises_fetcherror():
    class Boom:
        def open(self, req, timeout=None):
            raise OSError("nope")
    with pytest.raises(FetchError):
        fetch_page_text("https://x.com/u/status/1", None, opener=Boom())


def test_fetch_page_text_wraps_http_error():
    class Boom:
        def open(self, req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", None, None)
    with pytest.raises(FetchError):
        fetch_page_text("https://x.com/u/status/1", None, opener=Boom())


def test_fetch_page_text_unescapes_html_entities():
    html = b'<html><head><meta property="og:title" content="Cats &amp; Dogs"/></head></html>'
    out = fetch_page_text("https://x.com/u/1", None, opener=FakeOpener(html))
    assert out["title"] == "Cats & Dogs"


def test_fetch_page_text_handles_reversed_attribute_order():
    html = b"""<html><head>
      <meta content="Reversed Title" property="og:title"/>
      <meta content='Single Quoted Caption' property='og:description'/>
    </head></html>"""
    out = fetch_page_text("https://x.com/u/1", None, opener=FakeOpener(html))
    assert out["title"] == "Reversed Title"
    assert out["text"] == "Single Quoted Caption"


def test_fetch_page_text_sends_browser_user_agent_and_timeout():
    opener = CapturingOpener()
    fetch_page_text("https://x.com/u/status/1", None, opener=opener)
    ua = opener.req.get_header("User-agent")
    assert ua and "Mozilla" in ua
    assert opener.timeout is not None and opener.timeout > 0


def test_build_opener_handles_none_cookies_path():
    opener = social._build_opener(None)
    assert hasattr(opener, "open")


def test_build_opener_handles_missing_cookies_file(tmp_path):
    missing = tmp_path / "nope.txt"
    opener = social._build_opener(missing)
    assert hasattr(opener, "open")


import json as json_mod
import subprocess
from types import SimpleNamespace

from src.social import fetch_social_media


def _ok(stdout=""):
    return SimpleNamespace(returncode=0, stdout=stdout, stderr="")


def test_fetch_social_media_video(tmp_path):
    def runner(cmd, **kw):
        if "--dump-json" in cmd:
            return _ok(json_mod.dumps({"title": "Reel", "uploader": "someone",
                                       "description": "cap", "upload_date": "20260101"}))
        (tmp_path / "media.m4a").write_bytes(b"audio")   # simulate download
        return _ok()
    out = fetch_social_media("https://www.instagram.com/reel/A/", None, tmp_path, runner=runner)
    assert out["kind"] == "video"
    assert out["files"] == [tmp_path / "media.m4a"]
    assert out["meta"] == {"title": "Reel", "author": "someone",
                           "caption": "cap", "date": "2026-01-01"}


def test_fetch_social_media_images_fallback(tmp_path):
    def runner(cmd, **kw):
        if cmd[0] == "yt-dlp":
            raise subprocess.CalledProcessError(1, cmd, stderr="no video")
        # gallery-dl call: simulate two images + info json
        (tmp_path / "post_1.jpg").write_bytes(b"img1")
        (tmp_path / "post_2.jpg").write_bytes(b"img2")
        (tmp_path / "post.json").write_text(json_mod.dumps(
            {"title": "Pic", "username": "someone", "content": "cap", "date": "2026-01-02"}))
        return _ok()
    out = fetch_social_media("https://www.instagram.com/p/A/", None, tmp_path, runner=runner)
    assert out["kind"] == "images"
    assert [f.name for f in out["files"]] == ["post_1.jpg", "post_2.jpg"]
    assert out["meta"]["author"] == "someone"
    assert out["meta"]["caption"] == "cap"


def test_fetch_social_media_none_when_all_fail(tmp_path):
    def runner(cmd, **kw):
        raise subprocess.CalledProcessError(1, cmd, stderr="boom")
    out = fetch_social_media("https://www.threads.net/@u/post/1", None, tmp_path, runner=runner)
    assert out["kind"] == "none"
    assert out["files"] == []


def test_fetch_social_media_missing_tool_falls_back_to_gallery_dl(tmp_path):
    # yt-dlp not installed on this host: FileNotFoundError from the runner,
    # not a CalledProcessError -- must degrade to gallery-dl, not crash.
    def runner(cmd, **kw):
        if cmd[0] == "yt-dlp":
            raise FileNotFoundError("yt-dlp not found")
        (tmp_path / "post_1.jpg").write_bytes(b"img1")
        return _ok()
    out = fetch_social_media("https://www.instagram.com/p/A/", None, tmp_path, runner=runner)
    assert out["kind"] == "images"
    assert [f.name for f in out["files"]] == ["post_1.jpg"]


def test_fetch_social_media_hung_tool_times_out_and_falls_back(tmp_path):
    # yt-dlp hangs: the runner raises TimeoutExpired rather than blocking the
    # 5-minute poll job forever. Must be handled like any other tool failure.
    def runner(cmd, **kw):
        if cmd[0] == "yt-dlp":
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 1))
        (tmp_path / "post_1.jpg").write_bytes(b"img1")
        return _ok()
    out = fetch_social_media("https://www.instagram.com/p/A/", None, tmp_path, runner=runner)
    assert out["kind"] == "images"


def test_fetch_social_media_passes_timeout_to_every_runner_call(tmp_path):
    seen_timeouts = []

    def runner(cmd, **kw):
        seen_timeouts.append(kw.get("timeout"))
        raise subprocess.CalledProcessError(1, cmd, stderr="boom")
    fetch_social_media("https://www.threads.net/@u/post/1", None, tmp_path, runner=runner)
    assert seen_timeouts and all(t is not None and t > 0 for t in seen_timeouts)


def test_fetch_social_media_missing_metadata_fields_do_not_raise(tmp_path):
    def runner(cmd, **kw):
        if "--dump-json" in cmd:
            return _ok(json_mod.dumps({}))
        (tmp_path / "media.m4a").write_bytes(b"audio")
        return _ok()
    out = fetch_social_media("https://www.instagram.com/reel/A/", None, tmp_path, runner=runner)
    assert out["kind"] == "video"
    assert out["meta"] == {"title": "", "author": "", "caption": "", "date": ""}


def test_fetch_social_media_omits_cookies_flag_when_none(tmp_path):
    seen_cmds = []

    def runner(cmd, **kw):
        seen_cmds.append(cmd)
        if "--dump-json" in cmd:
            return _ok(json_mod.dumps({"title": "T"}))
        (tmp_path / "media.m4a").write_bytes(b"audio")
        return _ok()
    fetch_social_media("https://www.instagram.com/reel/A/", None, tmp_path, runner=runner)
    for cmd in seen_cmds:
        assert "--cookies" not in cmd


def test_fetch_social_media_ignores_partial_download_with_no_completed_file(tmp_path):
    # yt-dlp aborted partway through (e.g. CDN sent no Content-Length so the
    # --max-filesize cap couldn't reject up front) and left a non-empty
    # media.m4a.part behind. That partial must not be reported as media --
    # it should fall through to gallery-dl, which also has nothing, so the
    # overall result must be kind="none".
    def runner(cmd, **kw):
        if "--dump-json" in cmd:
            return _ok(json_mod.dumps({"title": "Reel"}))
        if cmd[0] == "yt-dlp":
            (tmp_path / "media.m4a.part").write_bytes(b"partial-audio")
            return _ok()
        raise subprocess.CalledProcessError(1, cmd, stderr="no images")
    out = fetch_social_media("https://www.instagram.com/reel/A/", None, tmp_path, runner=runner)
    assert out["kind"] == "none"
    assert out["files"] == []


def test_fetch_social_media_prefers_completed_file_over_partial(tmp_path):
    # a partial artifact from an earlier aborted attempt sits alongside the
    # real completed download; only the completed file may be returned.
    def runner(cmd, **kw):
        if "--dump-json" in cmd:
            return _ok(json_mod.dumps({"title": "Reel", "uploader": "someone",
                                       "description": "cap", "upload_date": "20260101"}))
        (tmp_path / "media.m4a.part").write_bytes(b"partial-audio")
        (tmp_path / "media.m4a").write_bytes(b"audio")
        return _ok()
    out = fetch_social_media("https://www.instagram.com/reel/A/", None, tmp_path, runner=runner)
    assert out["kind"] == "video"
    assert out["files"] == [tmp_path / "media.m4a"]


def test_fetch_social_media_passes_url_as_argv_element_not_shell_string(tmp_path):
    # untrusted URL from the public internet: must never be interpolated into
    # a shell string, so this must survive as a single argv element intact.
    weird_url = "https://x.com/p/1?a=$(rm -rf /);`touch pwned`"
    seen_cmds = []

    def runner(cmd, **kw):
        seen_cmds.append(cmd)
        if "--dump-json" in cmd:
            return _ok(json_mod.dumps({"title": "T"}))
        (tmp_path / "media.m4a").write_bytes(b"audio")
        return _ok()
    fetch_social_media(weird_url, None, tmp_path, runner=runner)
    assert seen_cmds
    for cmd in seen_cmds:
        assert isinstance(cmd, list)
        assert weird_url in cmd


from pathlib import Path

from src import gemini, ingest
from src import meta as meta_mod, note as note_mod, state as state_mod
from src.config import load_config
from src.ingest import append_item, url_key
from src.social import process_social_item, drain_social_queue

_MODEL_OUT = ("```meta\ncategory: ai-dev\ntopic: AI & Dev\ntags: [x]\n"
              "topics: [Agents]\n```\n---\n# N\nbody")


def _cfg(tmp_path, **over):
    lines = ["PLAYLIST_URL=https://x", f"VAULT_DIR={tmp_path}/vault",
             f"STATE_PATH={tmp_path}/state.json", f"QUEUE_PATH={tmp_path}/queue.jsonl",
             "GEMINI_API_KEY=g", "MAX_ATTEMPTS=2"]
    lines += [f"{k}={v}" for k, v in over.items()]
    (tmp_path / "prompts").mkdir(exist_ok=True)
    (tmp_path / "prompts" / "summarize-social.md").write_text("SOCIAL PROMPT")
    env = tmp_path / "config.env"
    env.write_text("\n".join(lines) + "\n")
    return load_config(env)


def _video_media(url, ck, td, **kw):
    (Path(td) / "media.m4a").write_bytes(b"audio")
    return {"kind": "video", "files": [Path(td) / "media.m4a"],
            "meta": {"title": "T", "author": "@a", "caption": "cap", "date": "2026-07-01"}}


def _no_media(url, ck, td, **kw):
    return {"kind": "none", "files": [],
            "meta": {"title": "", "author": "", "caption": "", "date": ""}}


def _deps(**over):
    base = dict(
        fetch_social_media=_video_media,
        fetch_page_text=lambda url, ck: {"title": "", "text": "", "author": ""},
        transcribe_audio=lambda p, k, m: "spoken words",
        run_claude_social=lambda pr, pay, im, mo, cwd: _MODEL_OUT,
        parse_model_output=meta_mod.parse_model_output,
        build_social_note=note_mod.build_social_note,
        build_social_stub_note=note_mod.build_social_stub_note,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _process(cfg, project_root, item, processed, deps):
    # the drain computes the dedupe key and reads the prompt once per batch;
    # these per-item tests hand over the same two values.
    return process_social_item(cfg, project_root, item, processed, deps,
                               url_key(item["url"]),
                               Path(project_root, cfg.social_prompt_path).read_text())


def _boom_claude(*a, **k):
    raise RuntimeError("claude down")


def test_process_social_item_video_happy(tmp_path):
    cfg = _cfg(tmp_path)
    item = {"url": "https://www.instagram.com/reel/A/", "text": None}
    out = _process(cfg, tmp_path, item, "2026-07-26", _deps())
    assert out == "summarized"
    # _MODEL_OUT names topic AI & Dev with no subtopic -> General
    notes = list((tmp_path / "vault" / "saved-content" / "AI & Dev" / "General").glob("*.md"))
    assert len(notes) == 1
    content = notes[0].read_text()
    assert 'platform: "instagram"' in content and "spoken words" in content
    assert state_mod.is_seen(state_mod.load_state(cfg.state_path), url_key(item["url"]))


def test_process_social_item_payload_carries_shared_text_and_caption(tmp_path):
    cfg = _cfg(tmp_path)
    seen = {}

    def claude(prompt, payload, images, model, cwd):
        seen["prompt"], seen["payload"] = prompt, payload
        return _MODEL_OUT
    item = {"url": "https://www.instagram.com/reel/A/",
            "why": "Adapt this hook for Example Project.",
            "text": "Focus on the opening sentence.",
            "received_at": "2026-08-13T10:00:00Z"}
    _process(cfg, tmp_path, item, "2026-07-26", _deps(run_claude_social=claude))
    assert seen["prompt"] == "SOCIAL PROMPT"
    assert "## Why saved\nAdapt this hook for Example Project." in seen["payload"]
    assert "## Shared note\nFocus on the opening sentence." in seen["payload"]
    assert "cap" in seen["payload"] and "spoken words" in seen["payload"]
    assert item["url"] in seen["payload"]
    note = next((tmp_path / "vault" / "saved-content").rglob("*.md")).read_text()
    assert 'capture_why: "Adapt this hook for Example Project."' in note
    assert 'capture_context_at: "2026-08-13T10:00:00Z"' in note


def test_process_social_item_duplicate(tmp_path):
    cfg = _cfg(tmp_path)
    item = {"url": "https://x.com/u/status/1", "text": None}
    state_mod.mark_seen(cfg.state_path, url_key(item["url"]))
    calls = []
    deps = _deps(fetch_social_media=lambda *a, **k: calls.append(1) or _no_media(*a, **k))
    assert _process(cfg, tmp_path, item, "2026-07-26", deps) == "duplicate"
    assert calls == []                       # duplicate must do no side effects
    assert not (tmp_path / "vault").exists()


def test_process_social_item_images_go_to_claude_without_transcription(tmp_path):
    cfg = _cfg(tmp_path)
    seen = {}

    def images_media(url, ck, td, **kw):
        img = Path(td) / "post_1.jpg"
        img.write_bytes(b"img")
        return {"kind": "images", "files": [img],
                "meta": {"title": "P", "author": "@a", "caption": "cap", "date": "2026-07-02"}}

    def claude(prompt, payload, image_paths, model, cwd):
        seen["images"] = [Path(p) for p in image_paths]
        seen["exists"] = all(Path(p).exists() for p in image_paths)
        return _MODEL_OUT

    def no_transcribe(*a, **k):
        raise AssertionError("images must not be sent to the transcriber")
    deps = _deps(fetch_social_media=images_media, run_claude_social=claude,
                 transcribe_audio=no_transcribe)
    item = {"url": "https://www.instagram.com/p/A/", "text": None}
    assert _process(cfg, tmp_path, item, "2026-07-26", deps) == "summarized"
    assert [p.name for p in seen["images"]] == ["post_1.jpg"]
    assert seen["exists"]                    # still on disk when claude runs


def test_process_social_item_transcribe_failure_falls_back_to_page_text(tmp_path):
    cfg = _cfg(tmp_path)

    def boom_transcribe(p, k, m):
        raise gemini.TranscribeError("gemini down")
    deps = _deps(transcribe_audio=boom_transcribe,
                 fetch_page_text=lambda url, ck: {"title": "PT", "text": "page words",
                                                  "author": "@p"})
    item = {"url": "https://www.instagram.com/reel/A/", "text": None}
    assert _process(cfg, tmp_path, item, "2026-07-26", deps) == "summarized"
    # not transcribed (video kind, no transcript) but a caption is present --
    # the note still lands on _MODEL_OUT's topic/subtopic either way
    note = list((tmp_path / "vault" / "saved-content" / "AI & Dev" / "General").glob("*.md"))[0].read_text()
    assert "spoken words" not in note


def test_process_social_item_fetch_failed_writes_stub(tmp_path):
    cfg = _cfg(tmp_path, MAX_ATTEMPTS=1)         # single-attempt cap: terminal on first try

    def boom_page(url, ck):
        raise FetchError("nope")
    deps = _deps(fetch_social_media=_no_media, fetch_page_text=boom_page)
    item = {"url": "https://www.threads.net/@u/post/1", "text": "shared cap"}
    assert _process(cfg, tmp_path, item, "2026-07-26", deps) == "fetch-failed"
    stub = list((tmp_path / "vault" / "saved-content" / "Other").glob("*.md"))[0].read_text()
    assert 'status: "fetch-failed"' in stub
    assert "shared cap" in stub and item["url"] in stub
    assert state_mod.is_seen(state_mod.load_state(cfg.state_path), url_key(item["url"]))


def test_process_social_item_fetch_retry_then_stub(tmp_path):
    # spec: MAX_ATTEMPTS=3 behavior matches the youtube flow -- a fetch
    # failure must retry, and the stub (plus mark_seen) must land only at the
    # cap. Marking it seen on attempt 1 makes a re-share after fixing the
    # cause (expired cookies, etc.) come back "duplicate" forever.
    cfg = _cfg(tmp_path)                          # MAX_ATTEMPTS=2

    def boom_page(url, ck):
        raise FetchError("cookies expired")
    deps = _deps(fetch_social_media=_no_media, fetch_page_text=boom_page)
    item = {"url": "https://www.instagram.com/p/abc/", "text": "shared cap"}
    assert _process(cfg, tmp_path, item, "2026-07-26", deps) == "retry"
    assert not state_mod.is_seen(state_mod.load_state(cfg.state_path), url_key(item["url"]))
    assert not (tmp_path / "vault").exists()      # nothing written yet -- still recoverable
    assert _process(cfg, tmp_path, item, "2026-07-26", deps) == "fetch-failed"
    stub = list((tmp_path / "vault" / "saved-content" / "Other").glob("*.md"))[0].read_text()
    assert 'status: "fetch-failed"' in stub
    assert "shared cap" in stub and item["url"] in stub
    assert state_mod.is_seen(state_mod.load_state(cfg.state_path), url_key(item["url"]))


def test_process_social_item_summary_retry_then_stub(tmp_path):
    cfg = _cfg(tmp_path)                     # MAX_ATTEMPTS=2
    deps = _deps(run_claude_social=_boom_claude)
    item = {"url": "https://x.com/u/status/9", "text": "keep me"}
    assert _process(cfg, tmp_path, item, "2026-07-26", deps) == "retry"
    assert not state_mod.is_seen(state_mod.load_state(cfg.state_path), url_key(item["url"]))
    assert _process(cfg, tmp_path, item, "2026-07-26", deps) == "summary-failed"
    stub = list((tmp_path / "vault" / "saved-content" / "Other").glob("*.md"))[0].read_text()
    assert 'status: "summary-failed"' in stub
    assert "keep me" in stub and item["url"] in stub
    assert state_mod.is_seen(state_mod.load_state(cfg.state_path), url_key(item["url"]))


def test_process_social_item_page_text_only_lands_in_the_ai_dev_general_folder(tmp_path):
    # has_content must be satisfiable by page text ALONE (no caption, no
    # transcript, no images) -- every other test that supplies page text also
    # goes through _video_media, which carries caption "cap" and would satisfy
    # has_content on its own, masking a regression in the page-text term.
    cfg = _cfg(tmp_path)
    deps = _deps(fetch_social_media=_no_media,
                 fetch_page_text=lambda url, ck: {"title": "PT", "text": "page words",
                                                  "author": "@p"})
    item = {"url": "https://some-blog.example/post/1", "text": None}
    assert _process(cfg, tmp_path, item, "2026-07-26", deps) == "summarized"
    notes = list((tmp_path / "vault" / "saved-content" / "AI & Dev" / "General").glob("*.md"))
    assert len(notes) == 1


def test_process_social_item_subtopic_threads_into_the_subtopic_folder(tmp_path):
    # the model naming a configured subtopic must file three levels deep, not
    # just two -- _MODEL_OUT never names one, so every other test exercises
    # this call site with subtopic defaulting to "General" regardless of
    # whether the wiring is actually correct.
    cfg = _cfg(tmp_path)                     # default SUBTOPICS maps AI & Dev -> Claude Code, ...
    out = ("```meta\ncategory: ai-dev\ntopic: AI & Dev\nsubtopic: Claude Code\n"
           "tags: [x]\n```\n---\nbody")
    deps = _deps(run_claude_social=lambda *a: out)
    item = {"url": "https://www.instagram.com/reel/A/", "text": None}
    assert _process(cfg, tmp_path, item, "2026-07-26", deps) == "summarized"
    notes = list((tmp_path / "vault" / "saved-content" / "AI & Dev" / "Claude Code").glob("*.md"))
    assert len(notes) == 1


def test_process_social_item_unknown_topic_falls_back_to_other(tmp_path):
    cfg = _cfg(tmp_path)
    out = "```meta\ncategory: other\ntopic: Underwater Basket Weaving\ntags: [x]\n```\n---\nbody"
    deps = _deps(run_claude_social=lambda *a: out)
    item = {"url": "https://www.instagram.com/reel/A/", "text": None}
    assert _process(cfg, tmp_path, item, "2026-07-26", deps) == "summarized"
    assert len(list((tmp_path / "vault" / "saved-content" / "Other").glob("*.md"))) == 1


def test_fetch_failed_event_files_under_personal_events_with_provenance(tmp_path):
    cfg = _cfg(tmp_path)
    item = {
        "url": "https://www.stubhub.com/event/159871048/",
        "why": "Concert I am interested in",
        "received_at": "2026-08-13T14:20:36Z",
    }
    deps = _deps(
        fetch_social_media=lambda *a, **k: {"kind": "none", "files": [], "meta": {}},
        fetch_page_text=lambda *a, **k: (_ for _ in ()).throw(FetchError("403")),
    )
    cfg.max_attempts = 1

    assert _process(cfg, tmp_path, item, "2026-08-13", deps) == "fetch-failed"

    note_path = next(
        (
            tmp_path
            / "vault"
            / "saved-content"
            / "Personal"
            / "Events & Experiences"
        ).glob("*.md")
    )
    note = note_path.read_text()
    assert 'source_type: "web"' in note
    assert 'content_kind: "event"' in note
    assert 'capture_why: "Concert I am interested in"' in note


def test_process_social_item_unknown_platform_is_a_string_never_none(tmp_path):
    cfg = _cfg(tmp_path)
    item = {"url": "https://some-blog.example/post/1", "text": None}
    assert _process(cfg, tmp_path, item, "2026-07-26", _deps()) == "summarized"
    note = list((tmp_path / "vault" / "saved-content" / "AI & Dev" / "General").glob("*.md"))[0].read_text()
    assert 'platform: "generic"' in note and "None" not in note


def test_process_social_item_removes_tempdir_even_when_the_item_fails(tmp_path):
    cfg = _cfg(tmp_path)
    dirs = []

    def media(url, ck, td, **kw):
        dirs.append(Path(td))
        return _video_media(url, ck, td, **kw)
    deps = _deps(fetch_social_media=media, run_claude_social=_boom_claude)
    item = {"url": "https://x.com/u/status/7", "text": None}
    assert _process(cfg, tmp_path, item, "2026-07-26", deps) == "retry"
    assert dirs and not dirs[0].exists()     # vault and queue share this disk


def test_process_social_item_hostile_topic_cannot_escape_the_vault(tmp_path):
    # destination_dir's bare-folder fallback returns an unmapped topic
    # verbatim, and a hand-edited TOPICS= list can put anything into
    # `allowed` -- the write site must still contain it inside the vault.
    cfg = _cfg(tmp_path, TOPICS="../../etc")
    out = "```meta\ncategory: other\ntopic: ../../etc\ntags: [x]\n```\n---\nbody"
    deps = _deps(run_claude_social=lambda *a: out)
    item = {"url": "https://www.instagram.com/reel/A/", "text": None}
    assert _process(cfg, tmp_path, item, "2026-07-26", deps) == "summarized"
    notes = list((tmp_path / "vault" / "Other").glob("*.md"))
    assert len(notes) == 1
    assert not (tmp_path.parent / "etc").exists()


def test_process_social_item_untitled_posts_do_not_overwrite_each_other(tmp_path):
    cfg = _cfg(tmp_path, MAX_ATTEMPTS=1)         # single-attempt cap: terminal on first try
    deps = _deps(fetch_social_media=_no_media,
                 fetch_page_text=lambda url, ck: (_ for _ in ()).throw(FetchError("nope")))
    for n in (1, 2):
        item = {"url": f"https://www.threads.net/@u/post/{n}", "text": f"share {n}"}
        assert _process(cfg, tmp_path, item, "2026-07-26", deps) == "fetch-failed"
    stubs = list((tmp_path / "vault" / "saved-content" / "Other").glob("*.md"))
    assert len(stubs) == 2                   # same date/title/platform, distinct notes


def test_drain_social_queue_requeues_retries(tmp_path):
    cfg = _cfg(tmp_path)
    url = "https://x.com/u/status/5"
    append_item(cfg.queue_path, {"url": url, "text": None})
    calls = []
    deps = _deps(fetch_social_media=lambda *a, **k: calls.append(1) or _video_media(*a, **k),
                 run_claude_social=_boom_claude)
    results = drain_social_queue(cfg, tmp_path, "2026-07-26", deps=deps)
    assert results == [(url, "retry")]
    assert cfg.queue_path.exists()                          # re-queued for next cycle
    assert len(calls) == 1                                  # not re-processed this run
    assert not ingest._processing_path(cfg.queue_path).exists()


def test_drain_social_queue_fetch_failure_retries_before_stub(tmp_path):
    # the review's exact repro: cookies expire, every fetch fails. Before the
    # fix this stubbed and marked-seen on cycle 1, so a re-share after fixing
    # the cookies came back "duplicate" and the post was unrecoverable.
    cfg = _cfg(tmp_path)                          # MAX_ATTEMPTS=2
    url = "https://www.instagram.com/p/abc/"
    append_item(cfg.queue_path, {"url": url, "text": None})

    def boom_page(u, ck):
        raise FetchError("cookies expired")
    deps = _deps(fetch_social_media=_no_media, fetch_page_text=boom_page)
    outcomes = []
    for _ in range(3):
        outcomes += [o for _u, o in drain_social_queue(cfg, tmp_path, "2026-07-26", deps=deps)]
    assert outcomes == ["retry", "fetch-failed"]  # terminal only at the cap
    assert not cfg.queue_path.exists()             # no longer re-queued
    assert state_mod.is_seen(state_mod.load_state(cfg.state_path), url_key(url))


def test_drain_social_queue_finishes_and_marks_seen(tmp_path):
    cfg = _cfg(tmp_path)
    url = "https://www.instagram.com/reel/A/"
    append_item(cfg.queue_path, {"url": url, "text": None})
    assert drain_social_queue(cfg, tmp_path, "2026-07-26", deps=_deps()) == [(url, "summarized")]
    assert not ingest._processing_path(cfg.queue_path).exists()
    assert not cfg.queue_path.exists()                      # nothing left to retry
    assert state_mod.is_seen(state_mod.load_state(cfg.state_path), url_key(url))


def test_drain_social_queue_one_failing_item_does_not_abort_the_batch(tmp_path):
    cfg = _cfg(tmp_path)
    bad, good = "https://x.com/u/status/1", "https://www.instagram.com/reel/A/"
    append_item(cfg.queue_path, {"url": bad, "text": None})
    append_item(cfg.queue_path, {"url": good, "text": None})

    def media(url, ck, td, **kw):
        if url == bad:
            raise RuntimeError("unexpected explosion")
        return _video_media(url, ck, td, **kw)
    results = drain_social_queue(cfg, tmp_path, "2026-07-26", deps=_deps(fetch_social_media=media))
    assert results == [(bad, "retry"), (good, "summarized")]
    assert not ingest._processing_path(cfg.queue_path).exists()
    assert len(list((tmp_path / "vault" / "saved-content" / "AI & Dev" / "General").glob("*.md"))) == 1


def test_drain_social_queue_keeps_batch_when_rotate_fails(tmp_path, monkeypatch):
    # rotate_queue raises on read errors by design; swallowing that and calling
    # finish_queue would delete a batch that was never processed.
    cfg = _cfg(tmp_path)
    append_item(cfg.queue_path, {"url": "https://x.com/u/status/1", "text": None})
    proc = ingest._processing_path(cfg.queue_path)
    cfg.queue_path.rename(proc)                             # simulate a crashed prior run

    def boom(path):
        raise OSError("EIO")
    monkeypatch.setattr(social.ingest, "rotate_queue", boom)
    with pytest.raises(OSError):
        drain_social_queue(cfg, tmp_path, "2026-07-26", deps=_deps())
    assert proc.exists()                                    # batch survives for next run


def test_drain_social_queue_drops_item_without_a_usable_url(tmp_path):
    cfg = _cfg(tmp_path)
    good = "https://www.instagram.com/reel/A/"
    append_item(cfg.queue_path, {"text": "no url here"})
    append_item(cfg.queue_path, {"url": good, "text": None})
    results = drain_social_queue(cfg, tmp_path, "2026-07-26", deps=_deps())
    assert results[0][1] == "invalid" and results[1] == (good, "summarized")
    assert not cfg.queue_path.exists()                      # never re-queued: poison item


def test_drain_social_queue_invalid_item_keeps_the_shared_text_in_a_stub(tmp_path):
    # dropping the item is right (it would poison every later drain) but the
    # user's text must still land somewhere they can read it.
    cfg = _cfg(tmp_path)
    append_item(cfg.queue_path, {"text": "the thing I actually wanted to save"})
    results = drain_social_queue(cfg, tmp_path, "2026-07-26", deps=_deps())
    assert results[0][1] == "invalid"
    assert "None" not in results[0][0]                       # no "social None → invalid"
    stub = list((tmp_path / "vault" / "saved-content" / "Other").glob("*.md"))[0].read_text()
    assert "the thing I actually wanted to save" in stub


def test_drain_social_queue_unexpected_failure_reaches_a_terminal_state(tmp_path):
    # MAX_ATTEMPTS=2. An item failing for a reason the item-level handlers do
    # not cover must still be bounded: without a recorded attempt it re-queues
    # every cycle forever, paying for the whole pipeline each time.
    cfg = _cfg(tmp_path)
    url = "https://x.com/u/status/11"
    append_item(cfg.queue_path, {"url": url, "text": "keep me"})
    calls = []

    def boom_media(u, ck, td, **kw):
        calls.append(1)
        raise RuntimeError("unexpected explosion")
    deps = _deps(fetch_social_media=boom_media)
    outcomes = []
    for _ in range(4):                                       # more cycles than attempts
        outcomes += [o for _u, o in drain_social_queue(cfg, tmp_path, "2026-07-26", deps=deps)]
    assert outcomes == ["retry", "summary-failed"]           # terminal, then nothing left
    assert len(calls) == 2                                   # no work after the stub
    assert not cfg.queue_path.exists()                       # no longer re-queued
    assert state_mod.is_seen(state_mod.load_state(cfg.state_path), url_key(url))
    stub = list((tmp_path / "vault" / "saved-content" / "Other").glob("*.md"))[0].read_text()
    assert 'status: "summary-failed"' in stub
    assert "keep me" in stub and url in stub


def test_drain_social_queue_vault_write_failure_reaches_a_terminal_state(tmp_path, monkeypatch):
    # the reviewer's worst case: the note write itself fails (synced mount,
    # ENOSPC, permissions) *after* transcription and claude have both been
    # paid for. Here only the topic folder is unwritable, so the Other stub
    # still lands.
    cfg = _cfg(tmp_path)
    url = "https://www.instagram.com/reel/B/"
    append_item(cfg.queue_path, {"url": url, "text": "shared cap"})
    real_write = social._write_social_note

    def flaky_write(c, post_meta, key, content, topic, subtopic="General", processed=""):
        if topic != "Other":
            raise OSError("Read-only file system")
        return real_write(c, post_meta, key, content, topic, subtopic, processed)
    monkeypatch.setattr(social, "_write_social_note", flaky_write)
    outcomes = []
    for _ in range(4):
        outcomes += [o for _u, o in drain_social_queue(cfg, tmp_path, "2026-07-26", deps=_deps())]
    assert outcomes == ["retry", "summary-failed"]
    assert not cfg.queue_path.exists()
    assert state_mod.is_seen(state_mod.load_state(cfg.state_path), url_key(url))
    stub = list((tmp_path / "vault" / "saved-content" / "Other").glob("*.md"))[0].read_text()
    assert "shared cap" in stub and url in stub


def test_process_social_item_hostile_date_stays_inside_the_vault(tmp_path):
    # gallery-dl's date field is remote JSON. content_filename is keyed on the
    # pipeline's own `processed` date, never on this remote value, so a
    # traversal-shaped date can only ever land inside frontmatter text -- it
    # never reaches the filename or the folder path at all.
    cfg = _cfg(tmp_path)

    def dated_media(url, ck, td, **kw):
        (Path(td) / "media.m4a").write_bytes(b"audio")
        return {"kind": "video", "files": [Path(td) / "media.m4a"],
                "meta": {"title": "T", "author": "@a", "caption": "cap",
                         "date": "../../pwn"}}
    item = {"url": "https://www.instagram.com/p/A/", "text": None}
    out = _process(cfg, tmp_path, item, "2026-07-26",
                              _deps(fetch_social_media=dated_media))
    assert out == "summarized"
    notes = list((tmp_path / "vault" / "saved-content" / "AI & Dev" / "General").glob("*.md"))
    assert len(notes) == 1
    assert not list(tmp_path.glob("*.md"))                   # nothing escaped the vault
    assert notes[0].name == "2026-07-26 T (Instagram).md"
    assert 'date: "../../pwn"' in notes[0].read_text()       # kept, but only as YAML text


@pytest.mark.parametrize("url", [
    "http://[::1",                    # unclosed IPv6 host
    "https://[2001:db8",              # unclosed IPv6 host
    "https://[",                      # bare bracket
    "http://[]/x",                    # empty IPv6 host
    "https://user@[::1/x",            # unclosed IPv6 host with userinfo
    "https://ex／ample.com/p",         # fullwidth solidus, NFKC-normalizes into the path
    "https://exa℀mple.com/p",         # homoglyph host character
    "https://x.com℀/a",               # homoglyph host character
    12345,                            # non-string
    "",                               # blank
])
def test_item_key_never_raises_on_unparseable_urls(url):
    # every one of these is accepted by the ingest server (scheme://rest) but
    # raises inside urlsplit/url_key on 3.14+; _item_key must swallow that and
    # return None rather than let it escape and wedge the drain.
    assert social._item_key(url) is None


def test_invalid_stub_survives_a_lone_surrogate_in_the_item(tmp_path):
    # a queue line containing a JSON \ud800 escape decodes to a lone surrogate.
    # blob.encode() with the default strict utf-8 codec raises UnicodeEncodeError
    # on that, which would escape drain_social_queue, skip finish_queue, and
    # wedge the queue exactly like the failure the previous commit fixed. Not
    # reachable through append_item today (it fails its own encode first), but
    # the error path itself must not be able to raise regardless.
    cfg = _cfg(tmp_path)
    item = {"text": "\ud800"}
    # build_social_stub_note is stubbed out here: this test isolates the named
    # defect (hashlib.sha1(blob.encode()) raising on the key computation), not
    # the separate and unmentioned question of whether a lone surrogate can
    # round-trip through Path.write_text elsewhere in the note pipeline.
    deps = _deps(build_social_stub_note=lambda pm, status, shared, processed: "stub\n")
    outcome = social._invalid_stub(cfg, item, None, "2026-07-26", deps)
    assert outcome == "invalid"
    assert len(list((tmp_path / "vault" / "saved-content" / "Other").glob("*.md"))) == 1


def test_invalid_stub_redelivery_of_the_same_item_overwrites_its_own_note(tmp_path):
    # Finding 1's repro: a url-less share redelivered (e.g. after a crashed
    # drain re-reads the same queue line) must rewrite its own note rather
    # than pile up a "(2)", "(3)", ... copy on every redelivery.
    cfg = _cfg(tmp_path)
    deps = _deps()
    item = {"text": "save me"}
    social._invalid_stub(cfg, item, None, "2026-07-26", deps)
    social._invalid_stub(cfg, item, None, "2026-07-26", deps)
    notes = list((tmp_path / "vault" / "saved-content" / "Other").glob("*.md"))
    assert len(notes) == 1


def test_invalid_stub_two_different_url_less_items_do_not_overwrite_each_other(tmp_path):
    # the other half of Finding 1: two distinct url-less shares landing on the
    # same date/title/platform (both are "Untitled (Web)") must never
    # collapse onto one note -- that would silently lose one of the shares.
    cfg = _cfg(tmp_path)
    deps = _deps()
    social._invalid_stub(cfg, {"text": "save me"}, None, "2026-07-26", deps)
    social._invalid_stub(cfg, {"text": "keep this too"}, None, "2026-07-26", deps)
    notes = list((tmp_path / "vault" / "saved-content" / "Other").glob("*.md"))
    assert len(notes) == 2
    bodies = {n.read_text() for n in notes}
    assert any("save me" in b for b in bodies)
    assert any("keep this too" in b for b in bodies)


def test_drain_social_queue_malformed_url_cannot_wedge_the_queue(tmp_path):
    # the ingest server accepts anything shaped like scheme://rest, so a url
    # urlsplit refuses to parse reaches the queue. Re-parsing it on the error
    # path would escape the drain, skip finish_queue and leave .processing in
    # place forever: the queue would never rotate again.
    cfg = _cfg(tmp_path)
    bad, good = "http://[::1", "https://www.instagram.com/reel/A/"
    append_item(cfg.queue_path, {"url": bad, "text": "look at this"})
    append_item(cfg.queue_path, {"url": good, "text": None})
    results = drain_social_queue(cfg, tmp_path, "2026-07-26", deps=_deps())
    assert [o for _u, o in results] == ["invalid", "summarized"]
    assert len(list((tmp_path / "vault" / "saved-content" / "AI & Dev" / "General").glob("*.md"))) == 1
    stub = list((tmp_path / "vault" / "saved-content" / "Other").glob("*.md"))[0].read_text()
    assert "look at this" in stub                # the sharer's text is kept
    assert f'url: "{bad}"' in stub                # the malformed url itself is kept
    assert f"Original: {bad}" in stub
    assert not ingest._processing_path(cfg.queue_path).exists()
    assert not cfg.queue_path.exists()           # never re-queued: poison item


def test_drain_social_queue_missing_prompt_file_keeps_the_batch(tmp_path):
    # a SOCIAL_PROMPT_PATH typo is a deploy fault, not an item fault. Absorbing
    # it per item burns a transcription per share and marks them all seen, which
    # the sharer cannot undo; the batch must stay queued and self-heal instead.
    cfg = _cfg(tmp_path, SOCIAL_PROMPT_PATH="prompts/typo-social.md")
    append_item(cfg.queue_path, {"url": "https://www.instagram.com/reel/A/", "text": None})
    with pytest.raises(OSError):
        drain_social_queue(cfg, tmp_path, "2026-07-26", deps=_deps())
    assert cfg.queue_path.exists()               # batch not consumed
    assert not ingest._processing_path(cfg.queue_path).exists()
    assert not (tmp_path / "vault").exists()     # no stubs written
    assert state_mod.load_state(cfg.state_path) == {"seen": [], "attempts": {}}


def test_drain_social_queue_records_at_most_one_attempt_per_cycle(tmp_path, monkeypatch):
    # claude fails (one attempt) and the stub write fails too, so the failure
    # reaches the drain's catch-all. Charging a second attempt there would halve
    # the retry budget for every mixed failure sequence.
    cfg = _cfg(tmp_path, MAX_ATTEMPTS=1)
    url = "https://x.com/u/status/12"
    append_item(cfg.queue_path, {"url": url, "text": "keep me"})
    real_write, writes = social._write_social_note, []

    def flaky_write(c, post_meta, key, content, topic, subtopic="General", processed=""):
        writes.append(topic)
        if len(writes) == 1:                     # only the first stub write fails
            raise OSError("Read-only file system")
        return real_write(c, post_meta, key, content, topic, subtopic, processed)

    real_record, recorded = social.state.record_attempt, []

    def counting_record(path, k):
        recorded.append(k)
        return real_record(path, k)
    monkeypatch.setattr(social, "_write_social_note", flaky_write)
    monkeypatch.setattr(social.state, "record_attempt", counting_record)
    deps = _deps(run_claude_social=_boom_claude)
    results = drain_social_queue(cfg, tmp_path, "2026-07-26", deps=deps)
    assert recorded == [url_key(url)]            # one cycle, one attempt
    assert results == [(url, "summary-failed")]


def test_drain_social_queue_reads_the_configured_social_prompt_once(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, SOCIAL_PROMPT_PATH="prompts/other-social.md")
    (tmp_path / "prompts" / "other-social.md").write_text("OTHER SOCIAL PROMPT")
    prompts = []
    # a stable prompt list is consistent with either "read once, reused for
    # both items" or "read once per item" -- only counting the actual
    # filesystem read distinguishes the two, so the prompt file's read is
    # spied on directly rather than inferred from its contents. Filtered by
    # name since rotate_queue also calls Path.read_text, on the queue file.
    reads = []
    real_read_text = Path.read_text

    def counting_read_text(self, *a, **kw):
        if self.name == "other-social.md":
            reads.append(self)
        return real_read_text(self, *a, **kw)
    monkeypatch.setattr(Path, "read_text", counting_read_text)

    def claude(prompt, payload, images, model, cwd):
        prompts.append(prompt)
        return _MODEL_OUT
    for n in (1, 2):
        append_item(cfg.queue_path, {"url": f"https://www.instagram.com/reel/{n}/", "text": None})
    drain_social_queue(cfg, tmp_path, "2026-07-26", deps=_deps(run_claude_social=claude))
    assert prompts == ["OTHER SOCIAL PROMPT"] * 2   # one read, both items
    assert len(reads) == 1                          # the file itself was opened once, not per item


def _saved(tmp_path):
    root = tmp_path / "saved"
    return [json_mod.loads(p.read_text()) for p in sorted(root.glob("*.json"))] if root.exists() and root.is_dir() else []


def test_summarized_share_emits_a_complete_saved_item(tmp_path):
    cfg = _cfg(tmp_path, OPEN_BRAIN_SAVED_SPOOL_DIR=f"{tmp_path}/saved")
    item = {"url": "https://www.instagram.com/reel/A/", "why": "Adapt this hook.",
            "text": "shared", "received_at": "2026-08-13T10:00:00+00:00"}
    assert _process(cfg, tmp_path, item, "2026-07-26", _deps()) == "summarized"
    [record] = _saved(tmp_path)
    assert (record["url"], record["why"], record["received_at"], record["fetch_status"]) == (item["url"], "Adapt this hook.", "2026-08-13T10:00:00Z", "complete")
    assert (record["platform"], record["source_type"], record["content_kind"]) == ("instagram", "social", "post")
    assert record["body"] == "cap" and record["media_text"] == "spoken words"
    assert record["shared_text"] == "shared"
    assert record["summary"] and record["summary_origin"] == "producer:x1-claude"


def test_fetch_failed_share_emits_a_failed_saved_item(tmp_path):
    cfg = _cfg(tmp_path, MAX_ATTEMPTS=1, OPEN_BRAIN_SAVED_SPOOL_DIR=f"{tmp_path}/saved")
    def boom_page(url, ck):
        raise FetchError("nope")
    item = {"url": "https://www.threads.net/@u/post/1", "why": "Read later", "text": "shared cap"}
    assert _process(cfg, tmp_path, item, "2026-07-26", _deps(fetch_social_media=_no_media, fetch_page_text=boom_page)) == "fetch-failed"
    [record] = _saved(tmp_path)
    assert (record["fetch_status"], record["shared_text"], record["summary"], record["platform"]) == ("failed", "shared cap", None, "threads")


def test_summary_failed_share_emits_an_unsummarized_saved_item(tmp_path):
    cfg = _cfg(tmp_path, MAX_ATTEMPTS=1, OPEN_BRAIN_SAVED_SPOOL_DIR=f"{tmp_path}/saved")
    item = {"url": "https://www.instagram.com/reel/A/", "why": "Hook", "text": None}
    assert _process(cfg, tmp_path, item, "2026-07-26", _deps(run_claude_social=_boom_claude)) == "summary-failed"
    [record] = _saved(tmp_path)
    assert record["fetch_status"] == "complete"
    assert record["summary"] is None and record["media_text"] == "spoken words"


def test_no_saved_item_without_the_spool_setting(tmp_path):
    cfg = _cfg(tmp_path)
    item = {"url": "https://www.instagram.com/reel/A/", "why": "x", "text": None}
    assert _process(cfg, tmp_path, item, "2026-07-26", _deps()) == "summarized"
    assert _saved(tmp_path) == []


def test_http_share_writes_a_note_but_no_saved_item(tmp_path):
    cfg = _cfg(tmp_path, OPEN_BRAIN_SAVED_SPOOL_DIR=f"{tmp_path}/saved")
    item = {"url": "http://example.com/a", "why": "x", "text": None}
    assert _process(cfg, tmp_path, item, "2026-07-26", _deps()) == "summarized"
    assert _saved(tmp_path) == []
    assert list((tmp_path / "vault" / "saved-content").rglob("*.md"))


def test_spool_failure_never_changes_the_share_outcome(tmp_path, caplog):
    blocker = tmp_path / "saved"
    blocker.write_text("not a directory")
    cfg = _cfg(tmp_path, OPEN_BRAIN_SAVED_SPOOL_DIR=str(blocker))
    item = {"url": "https://www.instagram.com/reel/A/", "why": "x", "text": None}
    assert _process(cfg, tmp_path, item, "2026-07-26", _deps()) == "summarized"
    assert list((tmp_path / "vault" / "saved-content").rglob("*.md"))
    assert "saved item not written" in caplog.text


def test_share_without_a_why_gets_the_default_reason(tmp_path):
    cfg = _cfg(tmp_path, OPEN_BRAIN_SAVED_SPOOL_DIR=f"{tmp_path}/saved")
    item = {"url": "https://www.instagram.com/reel/A/", "text": None}
    _process(cfg, tmp_path, item, "2026-07-26", _deps())
    [record] = _saved(tmp_path)
    assert record["why"] == "Saved from the share sheet."


def test_saved_item_helper_never_raises_for_metadata_or_writer_errors(tmp_path, monkeypatch, caplog):
    cfg = _cfg(tmp_path, OPEN_BRAIN_SAVED_SPOOL_DIR=f"{tmp_path}/saved")
    assert social._emit_saved_item(cfg, {"url": "https://example.com", "why": 123}, {}, "key", "complete") is False
    class BadMeta:
        def get(self, key):
            raise RuntimeError("metadata failure")
    assert social._emit_saved_item(cfg, {"url": "https://example.com"}, BadMeta(), "key", "complete") is False
    def boom(*args, **kwargs):
        raise RuntimeError("writer failure")
    monkeypatch.setattr(social.open_brain_bridge, "write_saved_item", boom)
    assert social._emit_saved_item(cfg, {"url": "https://example.com"}, {}, "key", "complete") is False
    assert "saved item not written" in caplog.text


def test_saved_item_unexpected_failure_retries_then_emits(tmp_path):
    cfg = _cfg(tmp_path, OPEN_BRAIN_SAVED_SPOOL_DIR=f"{tmp_path}/saved")
    url = "https://x.com/u/status/11"
    append_item(cfg.queue_path, {"url": url, "text": "keep me"})
    def boom(*a, **kw):
        raise RuntimeError("unexpected")
    deps = _deps(fetch_social_media=boom)
    assert drain_social_queue(cfg, tmp_path, "2026-07-26", deps=deps) == [(url, "retry")]
    assert _saved(tmp_path) == []
    assert drain_social_queue(cfg, tmp_path, "2026-07-26", deps=deps) == [(url, "summary-failed")]
    [record] = _saved(tmp_path)
    assert (record["fetch_status"], record["shared_text"], record["platform"]) == ("failed", "keep me", "twitter")


def test_saved_item_partial_fetch_and_web_mapping(tmp_path):
    cfg = _cfg(tmp_path, OPEN_BRAIN_SAVED_SPOOL_DIR=f"{tmp_path}/saved")
    def no_transcript(*a):
        raise gemini.TranscribeError("unavailable")
    deps = _deps(transcribe_audio=no_transcript, fetch_page_text=lambda *a: {"text": "page", "title": "", "author": ""})
    item = {"url": "https://example.com/article", "why": "read"}
    assert _process(cfg, tmp_path, item, "2026-07-26", deps) == "summarized"
    [record] = _saved(tmp_path)
    assert (record["fetch_status"], record["source_type"], record["content_kind"], record["platform"]) == ("partial", "web", "other", None)
    assert record["body"] == "cap\n\npage" and record["media_text"] == ""


def test_saved_item_writer_error_does_not_spend_attempt(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, OPEN_BRAIN_SAVED_SPOOL_DIR=f"{tmp_path}/saved")
    url = "https://www.instagram.com/reel/A/"
    append_item(cfg.queue_path, {"url": url})
    def boom(*a, **kw):
        raise RuntimeError("writer failure")
    monkeypatch.setattr(social.open_brain_bridge, "write_saved_item", boom)
    assert drain_social_queue(cfg, tmp_path, "2026-07-26", deps=_deps()) == [(url, "summarized")]
    assert state_mod.load_state(cfg.state_path)["attempts"] == {}
    assert not cfg.queue_path.exists()


@pytest.mark.parametrize("spool_configured", [False, True])
@pytest.mark.parametrize("model_fails", [False, True])
def test_non_string_caption_does_not_change_share_outcome(tmp_path, monkeypatch, spool_configured, model_fails):
    options = {"MAX_ATTEMPTS": 1}
    if spool_configured:
        options["OPEN_BRAIN_SAVED_SPOOL_DIR"] = str(tmp_path / "saved")
    cfg = _cfg(tmp_path, **options)
    url = "https://www.instagram.com/reel/non-string/"
    append_item(cfg.queue_path, {"url": url})

    def media(*args):
        result = _video_media(*args)
        result["meta"]["caption"] = 123
        return result

    model_calls = []
    def model(*args):
        model_calls.append(True)
        if model_fails:
            return _boom_claude(*args)
        return _MODEL_OUT
    deps = _deps(fetch_social_media=media, run_claude_social=model)
    recorded = []
    real_record = social.state.record_attempt
    def record_attempt(path, key):
        recorded.append(key)
        return real_record(path, key)
    monkeypatch.setattr(social.state, "record_attempt", record_attempt)
    expected = "summary-failed" if model_fails else "summarized"
    assert drain_social_queue(cfg, tmp_path, "2026-07-26", deps=deps) == [(url, expected)]
    assert model_calls == [True]
    assert recorded == ([url_key(url)] if model_fails else [])
    assert state_mod.load_state(cfg.state_path)["attempts"] == {}
    assert list((tmp_path / "vault" / "saved-content").rglob("*.md"))
    assert not cfg.queue_path.exists()
