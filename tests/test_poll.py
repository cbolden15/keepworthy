from dataclasses import replace
import logging
import subprocess
import pytest
from types import SimpleNamespace
from pathlib import Path
from src import state
from src import note as note_mod
from src.config import Config
from src import poll

def make_cfg(tmp_path):
    # process_video reads the prompt file from project_root/prompt_path; since
    # tests pass tmp_path as project_root, the fixture file must exist there.
    (tmp_path / "prompts").mkdir(parents=True, exist_ok=True)
    (tmp_path / "prompts" / "summarize.md").write_text("PROMPT")
    return Config(
        playlist_url="https://pl",
        vault_dir=tmp_path / "Brain",
        subfolder="YouTube Summaries",
        model="sonnet",
        prompt_path=Path("prompts/summarize.md"),
        state_path=tmp_path / "state.json",
        max_attempts=3,
        sub_lang="en",
        topics=["AI & Dev", "Business & Marketing", "Real Estate",
                "Finance", "Health & Fitness", "Personal"],
        ingest_token="",
        ingest_port=8787,
        ingest_bind="0.0.0.0",
        cookies_path=None,
        gemini_api_key="",
        gemini_model="gemini-2.5-flash",
        queue_path=tmp_path / "state" / "queue.jsonl",
        social_prompt_path=Path("prompts/summarize-social.md"),
        content_root="Social Content",
        subtopics={},
    )

def base_deps(**over):
    d = dict(
        list_playlist_ids=lambda url, **k: ["v1"],
        get_metadata=lambda vid, **k: {"title": "T", "channel": "C", "url": "u",
                                       "video_id": vid, "published": "2026-07-18", "duration": "00:01:00"},
        fetch_captions_vtt=lambda vid, lang, td, **k: "WEBVTT\n\nhello world",
        clean_vtt=lambda t: "hello world",
        run_claude=lambda prompt, tr, model, cwd, **k: "```meta\ncategory: ai-dev\ntags: [ai]\ntopics: [X]\n```\n---\n## TL;DR\nx",
        parse_model_output=lambda t: ({"category": "ai-dev", "tags": ["ai"], "topics": ["X"]}, "## TL;DR\nx"),
        build_note=lambda *a: "NOTE",
        build_stub_note=lambda *a: "STUB",
        write_open_brain_capture=lambda *a, **k: None,
    )
    d.update(over)
    return SimpleNamespace(**d)

def test_process_video_writes_note_and_marks_seen(tmp_path):
    cfg = make_cfg(tmp_path)
    out = poll.process_video(cfg, tmp_path, "v1", "2026-07-20", base_deps())
    assert out == "summarized"
    notes = list((cfg.vault_dir / cfg.content_root / "Other").glob("*.md"))
    assert len(notes) == 1 and notes[0].read_text() == "NOTE"
    assert state.is_seen(state.load_state(cfg.state_path), "v1")

def test_process_video_threads_capture_context_into_note_builder(tmp_path):
    cfg = make_cfg(tmp_path)
    state.queue_video(cfg.state_path, "v1", "Try this orchestration pattern.",
                      "2026-08-13T10:00:00Z")
    captured = {}
    def build_note(video_meta, *args):
        captured.update(video_meta)
        return "NOTE"
    deps = base_deps(build_note=build_note)
    assert poll.process_video(cfg, tmp_path, "v1", "2026-07-20", deps) == "summarized"
    assert captured["capture_why"] == "Try this orchestration pattern."
    assert captured["capture_context_at"] == "2026-08-13T10:00:00Z"
    assert captured["capture_source"] == "share"
    assert state.capture_context(state.load_state(cfg.state_path), "v1") == {}

def test_process_video_bridges_transcript_without_summary_or_note(tmp_path):
    cfg = replace(
        make_cfg(tmp_path),
        open_brain_spool_dir=(tmp_path / "Brain" / ".open-brain-ingress" / "youtube"),
    )
    captured = {}

    def write_capture(root, **fields):
        captured["root"] = root
        captured.update(fields)

    def unexpected_summary(*args, **kwargs):
        raise AssertionError("bridge mode must not summarize twice")

    deps = base_deps(
        get_metadata=lambda vid, **k: {
            "title": "T", "channel": "C", "url": "u", "video_id": vid,
            "published": "2026-07-18", "duration": "00:01:00",
        },
        run_claude=unexpected_summary,
        write_open_brain_capture=write_capture,
    )

    outcome = poll.process_video(
        cfg, tmp_path, "dQw4w9WgXcQ", "2026-08-26", deps
    )

    assert outcome == "bridged"
    assert captured == {
        "root": cfg.open_brain_spool_dir,
        "video_id": "dQw4w9WgXcQ",
        "transcript": "hello world",
        "why": "",
    }
    assert not (cfg.vault_dir / cfg.content_root).exists()
    assert state.is_seen(state.load_state(cfg.state_path), "dQw4w9WgXcQ")

def test_process_video_bridges_channel_upload_with_channel_why(tmp_path):
    cfg = replace(
        make_cfg(tmp_path),
        open_brain_spool_dir=(tmp_path / "Brain" / ".open-brain-ingress" / "youtube"),
    )
    captured = {}

    def write_capture(root, **fields):
        captured[fields["video_id"]] = fields

    deps = base_deps(
        get_metadata=lambda vid, **k: {
            "title": "T", "channel": "C", "url": "u", "video_id": vid,
            "published": "2026-07-18", "duration": "00:01:00",
        },
        write_open_brain_capture=write_capture,
    )
    channel_vid, playlist_vid = "dQw4w9WgXcQ", "aaaaaaaaaaa"
    state.register_channel_videos(cfg.state_path, "https://www.youtube.com/@c", [], "t0")
    state.register_channel_videos(
        cfg.state_path, "https://www.youtube.com/@c", [channel_vid], "t1")

    assert poll.process_video(cfg, tmp_path, channel_vid, "2026-10-05", deps) == "bridged"
    assert poll.process_video(cfg, tmp_path, playlist_vid, "2026-10-05", deps) == "bridged"

    assert captured[channel_vid]["why"] == "New upload from subscribed channel C"
    assert captured[playlist_vid]["why"] == ""

def test_run_processes_sidecar_requested_video_not_in_playlist(tmp_path):
    cfg = make_cfg(tmp_path)
    state.queue_video(cfg.state_path, "requested", "Review this.",
                      "2026-08-13T10:00:00Z")
    deps = base_deps(
        list_playlist_ids=lambda url, **k: ["playlist"],
        get_metadata=lambda vid, **k: {"title": vid, "channel": "C", "url": "u",
                                       "video_id": vid, "published": "2026-07-18",
                                       "duration": "00:01:00"},
    )
    assert [video_id for video_id, _ in poll.run(cfg, tmp_path, "2026-07-20", deps)] == [
        "playlist", "requested",
    ]

def test_process_video_files_note_into_recognized_topic_folder(tmp_path):
    cfg = make_cfg(tmp_path)
    deps = base_deps(parse_model_output=lambda t: (
        {"category": "real-estate", "tags": ["re"], "topics": ["Other"], "topic": "Real Estate"}, "## TL;DR\nx"))
    out = poll.process_video(cfg, tmp_path, "v1", "2026-07-20", deps)
    assert out == "summarized"
    notes = list((cfg.vault_dir / cfg.content_root / "Real Estate").glob("*.md"))
    assert len(notes) == 1 and notes[0].read_text() == "NOTE"
    assert state.is_seen(state.load_state(cfg.state_path), "v1")

def test_process_video_files_note_into_the_subtopic_folder(tmp_path):
    # a topic with a configured subtopic list files three levels deep, using
    # the subtopic the model named rather than the flat topic folder.
    cfg = replace(make_cfg(tmp_path), subtopics={"AI & Dev": ["Claude Code"]})
    deps = base_deps(parse_model_output=lambda t: (
        {"category": "ai-dev", "tags": ["ai"], "topics": ["X"],
         "topic": "AI & Dev", "subtopic": "Claude Code"}, "## TL;DR\nx"))
    out = poll.process_video(cfg, tmp_path, "v1", "2026-07-20", deps)
    assert out == "summarized"
    notes = list((cfg.vault_dir / cfg.content_root / "AI & Dev" / "Claude Code").glob("*.md"))
    assert len(notes) == 1 and notes[0].read_text() == "NOTE"
    assert state.is_seen(state.load_state(cfg.state_path), "v1")

def test_no_captions_writes_stub(tmp_path):
    cfg = make_cfg(tmp_path)
    deps = base_deps(fetch_captions_vtt=lambda *a, **k: None)
    out = poll.process_video(cfg, tmp_path, "v1", "2026-07-20", deps)
    assert out == "no-transcript"
    notes = list((cfg.vault_dir / cfg.content_root / "Other").glob("*.md"))
    assert notes[0].read_text() == "STUB"
    assert state.is_seen(state.load_state(cfg.state_path), "v1")

def test_claude_failure_retries_then_gives_up(tmp_path):
    cfg = make_cfg(tmp_path)
    def boom(*a, **k): raise RuntimeError("claude down")
    deps = base_deps(run_claude=boom)
    # attempts 1 and 2 → retry, not seen
    assert poll.process_video(cfg, tmp_path, "v1", "2026-07-20", deps) == "retry"
    assert poll.process_video(cfg, tmp_path, "v1", "2026-07-20", deps) == "retry"
    assert not state.is_seen(state.load_state(cfg.state_path), "v1")
    # attempt 3 → summary-failed stub, marked seen
    assert poll.process_video(cfg, tmp_path, "v1", "2026-07-20", deps) == "summary-failed"
    assert state.is_seen(state.load_state(cfg.state_path), "v1")

def test_metadata_failure_retries_then_gives_up(tmp_path):
    # A bad/deleted/private video id (e.g. a stale queue entry) must not crash
    # the whole poll cycle -- it should retry like a claude failure, then
    # permanently stub out so later videos in the run still get processed.
    cfg = make_cfg(tmp_path)
    def boom(vid, **k): raise RuntimeError("yt-dlp: video unavailable")
    deps = base_deps(get_metadata=boom)
    assert poll.process_video(cfg, tmp_path, "v1", "2026-07-20", deps) == "retry"
    assert poll.process_video(cfg, tmp_path, "v1", "2026-07-20", deps) == "retry"
    assert not state.is_seen(state.load_state(cfg.state_path), "v1")
    assert poll.process_video(cfg, tmp_path, "v1", "2026-07-20", deps) == "metadata-failed"
    assert state.is_seen(state.load_state(cfg.state_path), "v1")
    notes = list((cfg.vault_dir / cfg.content_root / "Other").glob("*.md"))
    assert notes[0].read_text() == "STUB"

def test_metadata_failure_does_not_block_later_videos_in_the_run(tmp_path):
    cfg = make_cfg(tmp_path)
    def get_metadata(vid, **k):
        if vid == "bad":
            raise RuntimeError("yt-dlp: video unavailable")
        return {"title": "T", "channel": "C", "url": "u", "video_id": vid,
                "published": "2026-07-18", "duration": "00:01:00"}
    deps = base_deps(
        list_playlist_ids=lambda url, **k: ["bad", "good"],
        get_metadata=get_metadata,
    )
    results = poll.run(cfg, tmp_path, "2026-07-20", deps)
    assert ("good", "summarized") in results

def _vault_notes(cfg):
    return sorted(cfg.vault_dir.rglob("*.md")) if cfg.vault_dir.exists() else []

def test_caption_failure_retries_then_gives_up(tmp_path):
    # yt-dlp runs with check=True, so a rate-limited subtitle download (HTTP 429)
    # surfaces as CalledProcessError. It must be treated like a metadata failure:
    # counted, retried, then stubbed -- never allowed to end the cycle.
    cfg = make_cfg(tmp_path)
    def boom(vid, lang, td, **k):
        raise subprocess.CalledProcessError(1, ["yt-dlp"])
    deps = base_deps(fetch_captions_vtt=boom,
                     build_stub_note=note_mod.build_stub_note)
    # attempts 1 and 2 → retry, no note written, not seen
    assert poll.process_video(cfg, tmp_path, "v1", "2026-07-20", deps) == "retry"
    assert poll.process_video(cfg, tmp_path, "v1", "2026-07-20", deps) == "retry"
    assert _vault_notes(cfg) == []
    assert not state.is_seen(state.load_state(cfg.state_path), "v1")
    # attempt 3 → captions-failed stub under Other/, marked seen
    assert poll.process_video(cfg, tmp_path, "v1", "2026-07-20", deps) == "captions-failed"
    assert state.is_seen(state.load_state(cfg.state_path), "v1")
    notes = list((cfg.vault_dir / cfg.content_root / "Other").glob("*.md"))
    assert len(notes) == 1
    text = notes[0].read_text()
    assert "video_id: v1" in text
    assert "status: captions-failed" in text
    assert "Caption download failed" in text

def test_caption_failure_does_not_block_later_videos_in_the_run(tmp_path):
    cfg = make_cfg(tmp_path)
    def fetch_captions_vtt(vid, lang, td, **k):
        if vid == "bad":
            raise subprocess.CalledProcessError(1, ["yt-dlp"])
        return "WEBVTT\n\nhello world"
    deps = base_deps(
        list_playlist_ids=lambda url, **k: ["bad", "good"],
        get_metadata=lambda vid, **k: {"title": vid, "channel": "C", "url": "u",
                                       "video_id": vid, "published": "2026-07-18",
                                       "duration": "00:01:00"},
        fetch_captions_vtt=fetch_captions_vtt,
    )
    results = poll.run(cfg, tmp_path, "2026-07-20", deps)
    assert results == [("bad", "retry"), ("good", "summarized")]
    names = [p.name for p in _vault_notes(cfg)]
    assert names == ["2026-07-20 good (YouTube).md"]

def test_write_note_files_into_topic_folder(tmp_path):
    from src import poll
    from src.config import load_config
    env = tmp_path / "config.env"
    env.write_text(f"PLAYLIST_URL=https://x\nVAULT_DIR={tmp_path}/vault\nTOPICS=AI & Dev\n")
    cfg = load_config(env)
    vm = {"title": "T", "video_id": "vid1", "published": "2026-01-01"}
    p = poll._write_note(cfg, vm, "content", topic="AI & Dev", subtopic="Claude Code")
    assert p.parent == cfg.vault_dir / cfg.content_root / "AI & Dev" / "Claude Code"
    q = poll._write_note(cfg, vm, "content", topic="Bogus")
    assert q.parent.name == "Other"

def test_write_note_lands_in_the_subtopic_folder(tmp_path):
    cfg = replace(make_cfg(tmp_path), subtopics={"AI & Dev": ["Claude Code"]})
    meta = {"title": "Claude Code Tips", "video_id": "abc123",
            "published": "2026-01-01"}
    path = poll._write_note(cfg, meta, "content", "AI & Dev", "Claude Code",
                            "2026-07-30")
    assert path == (cfg.vault_dir / "Social Content" / "AI & Dev" /
                    "Claude Code" / "2026-07-30 Claude Code Tips (YouTube).md")

def test_write_note_topic_without_subtopics_is_two_levels(tmp_path):
    cfg = replace(make_cfg(tmp_path), topics=["Career"], subtopics={})
    meta = {"title": "A Talk", "video_id": "abc123", "published": "2026-01-01"}
    path = poll._write_note(cfg, meta, "content", "Career", "General",
                            "2026-07-30")
    assert path == (cfg.vault_dir / "Social Content" / "Career" /
                    "2026-07-30 A Talk (YouTube).md")

def test_write_note_second_video_same_name_gets_a_suffix(tmp_path):
    cfg = replace(make_cfg(tmp_path), topics=["Career"], subtopics={})
    a = {"title": "Same", "video_id": "aaa", "published": "2026-01-01"}
    b = {"title": "Same", "video_id": "bbb", "published": "2026-01-01"}
    # real frontmatter with distinct video_ids: a bare string (as in the
    # sibling overwrite test, before its own fix) has no frontmatter at all,
    # so resolve_collision would read back no identity for either video and
    # the "(2)" suffix would appear no matter what identity was passed --
    # that would prove nothing about identity actually being distinguished.
    one = "---\nvideo_id: aaa\n---\n\none\n"
    two = "---\nvideo_id: bbb\n---\n\ntwo\n"
    poll._write_note(cfg, a, one, "Career", "General", "2026-07-30")
    second = poll._write_note(cfg, b, two, "Career", "General", "2026-07-30")
    assert second.name == "2026-07-30 Same (YouTube) (2).md"
    assert second.read_text() == two

def test_write_note_same_video_twice_overwrites(tmp_path):
    cfg = replace(make_cfg(tmp_path), topics=["Career"], subtopics={})
    meta = {"title": "Same", "video_id": "aaa", "published": "2026-01-01"}
    # resolve_collision reads the identity key from the written file's own
    # frontmatter, so the content here must carry it -- a bare string (as
    # build_note's real output never is) would read back as no identity at
    # all and always collide as a foreign note.
    one = "---\nvideo_id: aaa\n---\n\none\n"
    two = "---\nvideo_id: aaa\n---\n\ntwo\n"
    first = poll._write_note(cfg, meta, one, "Career", "General", "2026-07-30")
    again = poll._write_note(cfg, meta, two, "Career", "General", "2026-07-30")
    assert again == first
    assert again.read_text() == two
    assert len(list(again.parent.glob("*.md"))) == 1

def test_write_note_hostile_topic_cannot_escape_the_vault(tmp_path):
    # destination_dir's bare-folder fallback returns an unmapped topic
    # verbatim, and a hand-edited TOPICS= list can put anything into
    # `allowed` -- the write site must still contain it inside the vault.
    cfg = replace(make_cfg(tmp_path), topics=["../../etc"], subtopics={})
    vm = {"title": "T", "video_id": "vid1", "published": "2026-01-01"}
    p = poll._write_note(cfg, vm, "content", topic="../../etc")
    assert p.parent.name == "Other"
    assert p.is_relative_to(Path(cfg.vault_dir).resolve())

def test_write_note_null_byte_topic_falls_back_and_completes(tmp_path):
    # A hand-edited TOPICS value (or a topic the model echoes back) can
    # contain anything, including a null byte. Path.resolve() raises
    # ValueError on that; if it escaped process_video unhandled it would
    # abort run() before the social phase in poll.main ever gets a chance to
    # run. safe_vault_dir must absorb it and let the cycle complete.
    cfg = replace(make_cfg(tmp_path), topics=["AI\x00Dev"], subtopics={})
    vm = {"title": "T", "video_id": "vid1", "published": "2026-01-01"}
    p = poll._write_note(cfg, vm, "content", topic="AI\x00Dev")
    assert p.parent.name == "Other"
    assert p.is_relative_to(Path(cfg.vault_dir).resolve())
    assert p.read_text() == "content"

def test_run_completes_a_full_cycle_with_a_null_byte_topic(tmp_path):
    cfg = replace(make_cfg(tmp_path), topics=["AI\x00Dev"], subtopics={})
    deps = base_deps(parse_model_output=lambda t: (
        {"category": "ai-dev", "tags": ["ai"], "topics": ["X"], "topic": "AI\x00Dev"}, "## TL;DR\nx"))
    results = poll.run(cfg, tmp_path, "2026-07-20", deps)
    assert results == [("v1", "summarized")]
    notes = list((Path(cfg.vault_dir).resolve() / "Other").glob("*.md"))
    assert len(notes) == 1 and notes[0].read_text() == "NOTE"
    assert state.is_seen(state.load_state(cfg.state_path), "v1")

def test_run_skips_seen(tmp_path):
    cfg = make_cfg(tmp_path)
    state.mark_seen(cfg.state_path, "v1")
    results = poll.run(cfg, tmp_path, "2026-07-20", base_deps())
    assert results == []  # v1 already seen → nothing processed

def _stub_main_config(monkeypatch, tmp_path):
    env = tmp_path / "config.env"
    env.write_text(f"PLAYLIST_URL=https://x\nVAULT_DIR={tmp_path}/vault\n"
                   f"STATE_PATH={tmp_path}/s.json\n")
    real_load = poll.config_mod.load_config
    monkeypatch.setattr(
        poll.config_mod,
        "load_config",
        lambda p, environment=None: real_load(env, environment=environment),
    )

def test_main_runs_social_drain_after_playlist(monkeypatch, tmp_path):
    from src import social
    order = []
    monkeypatch.setattr(poll, "run", lambda cfg, root, processed, deps: order.append("youtube") or [])
    monkeypatch.setattr(social, "drain_social_queue",
                        lambda cfg, root, processed: order.append("social") or [])
    _stub_main_config(monkeypatch, tmp_path)
    poll.main()
    assert order == ["youtube", "social"]

def test_main_social_failure_does_not_break_the_youtube_phase(monkeypatch, tmp_path):
    from src import social
    done = []
    monkeypatch.setattr(poll, "run", lambda cfg, root, processed, deps:
                        done.append("youtube") or [("v1", "summarized")])
    def boom(*a, **k): raise RuntimeError("queue on fire")
    monkeypatch.setattr(social, "drain_social_queue", boom)
    _stub_main_config(monkeypatch, tmp_path)
    poll.main()  # must not raise
    assert done == ["youtube"]


CHANNEL = "https://www.youtube.com/@c"


def _meta(vid, **k):
    return {"title": vid, "channel": "C", "url": "u", "video_id": vid,
            "published": "2026-07-18", "duration": "00:01:00"}


def test_run_baselines_new_channel_without_processing(tmp_path):
    cfg = replace(make_cfg(tmp_path), channels=[CHANNEL])
    deps = base_deps(
        list_playlist_ids=lambda url, **k: [],
        list_channel_ids=lambda url, **k: [("old1", True), ("old2", True)],
    )
    assert poll.run(cfg, tmp_path, "2026-10-05", deps) == []
    st = state.load_state(cfg.state_path)
    assert state.is_seen(st, "old1") and state.is_seen(st, "old2")
    assert state.is_channel_baselined(st, CHANNEL)


def test_run_warns_when_new_channel_baselines_with_no_videos(tmp_path, caplog):
    cfg = replace(make_cfg(tmp_path), channels=[CHANNEL])
    deps = base_deps(
        list_playlist_ids=lambda url, **k: [],
        list_channel_ids=lambda url, **k: [],
    )
    with caplog.at_level(logging.INFO, logger="yt-obsidian"):
        assert poll.run(cfg, tmp_path, "2026-10-05", deps) == []
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("baselined with no published videos" in r.getMessage()
               for r in warnings)
    assert state.is_channel_baselined(state.load_state(cfg.state_path), CHANNEL)


def test_run_processes_new_channel_upload_after_baseline(tmp_path):
    cfg = replace(make_cfg(tmp_path), channels=[CHANNEL])
    state.register_channel_videos(cfg.state_path, CHANNEL, ["old1"], "t0")
    built = []
    deps = base_deps(
        list_playlist_ids=lambda url, **k: ["v1"],
        list_channel_ids=lambda url, **k: [
            ("old1", True), ("new1", True), ("soon", False),
        ],
        get_metadata=_meta,
        build_note=lambda video_meta, *a: built.append(video_meta) or "NOTE",
    )
    results = poll.run(cfg, tmp_path, "2026-10-05", deps)
    assert [vid for vid, _ in results] == ["v1", "new1"]
    sources = {m["video_id"]: m["capture_source"] for m in built}
    assert sources == {"v1": "playlist", "new1": "channel"}
    st = state.load_state(cfg.state_path)
    assert not state.is_seen(st, "soon")
    assert "soon" not in state.requested_videos(st)


def test_run_skips_failing_channel_and_keeps_going(tmp_path):
    bad = "https://www.youtube.com/@missing"
    cfg = replace(make_cfg(tmp_path), channels=[bad, CHANNEL])

    def list_channel_ids(url, **k):
        if url == bad:
            raise subprocess.CalledProcessError(1, ["yt-dlp"])
        return [("old1", True)]

    deps = base_deps(
        list_playlist_ids=lambda url, **k: ["v1"],
        list_channel_ids=list_channel_ids,
        get_metadata=_meta,
    )
    results = poll.run(cfg, tmp_path, "2026-10-05", deps)
    assert results == [("v1", "summarized")]
    st = state.load_state(cfg.state_path)
    assert not state.is_channel_baselined(st, bad)
    assert state.is_channel_baselined(st, CHANNEL)


def test_run_without_channels_never_lists_channels(tmp_path):
    cfg = make_cfg(tmp_path)

    def boom(url, **k):
        raise AssertionError("channel listing must not run")

    deps = base_deps(list_channel_ids=boom)
    assert [vid for vid, _ in poll.run(cfg, tmp_path, "2026-10-05", deps)] == ["v1"]

@pytest.mark.parametrize("mode,line", [
    ({"mode": "full", "trafilatura": "2.3.1", "pypdf": "6.21.1"},
     "article capture: full (trafilatura 2.3.1, pypdf 6.21.1)"),
    ({"mode": "fallback", "trafilatura": None, "pypdf": None},
     "article capture: fallback (meta tags only; install requirements-capture.txt)"),
])
def test_main_logs_the_article_capture_mode(monkeypatch, tmp_path, caplog, mode, line):
    from src import article, social
    monkeypatch.setattr(poll, "run", lambda cfg, root, processed, deps: [])
    monkeypatch.setattr(social, "drain_social_queue", lambda cfg, root, processed: [])
    monkeypatch.setattr(article, "capture_mode", lambda: mode)
    _stub_main_config(monkeypatch, tmp_path)
    with caplog.at_level(logging.INFO, logger="yt-obsidian"):
        poll.main()
    assert line in caplog.messages
