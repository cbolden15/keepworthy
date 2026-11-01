import logging
import os
import tempfile
from functools import partial
from datetime import date
from pathlib import Path

from src import config as config_mod
from src.config import allowed_topics
from src import media, transcript as transcript_mod, meta as meta_mod, note as note_mod, summarize, state
from src import social
from src import open_brain_bridge
from src import finance_content

log = logging.getLogger("yt-obsidian")

def _default_deps(cfg=None):
    from types import SimpleNamespace
    return SimpleNamespace(
        list_playlist_ids=media.list_playlist_ids,
        list_channel_ids=media.list_channel_ids,
        get_metadata=media.get_metadata,
        fetch_captions_vtt=media.fetch_captions_vtt,
        clean_vtt=transcript_mod.clean_vtt,
        run_claude=partial(summarize.run_claude, sandbox=getattr(cfg,"model_sandbox",False)),
        parse_model_output=meta_mod.parse_model_output,
        build_note=note_mod.build_note,
        build_stub_note=note_mod.build_stub_note,
        write_open_brain_capture=open_brain_bridge.write_youtube_capture,
    )

def _write_note(cfg, video_meta, content, topic="Other", subtopic="General",
                processed="") -> Path:
    folder = note_mod.destination_dir(topic, subtopic, allowed_topics(cfg),
                                      cfg.subtopics, cfg.content_root)
    out_dir = note_mod.safe_vault_dir(cfg.vault_dir, folder)
    out_dir.mkdir(parents=True, exist_ok=True)
    fn = note_mod.content_filename(processed, video_meta.get("title", ""),
                                   "youtube")
    path = note_mod.resolve_collision(out_dir / fn,
                                      video_meta["video_id"], "video_id")
    if video_meta.get("_sure_context_added"):
        note_mod.write_private_note(path, content)
        return path
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(content)
    tmp.replace(path)
    return path

def _default_why(video_meta) -> str:
    # The spool record carries only `why`; give channel uploads their own provenance.
    if video_meta.get("capture_source") == "channel":
        return f"New upload from subscribed channel {video_meta.get('channel', '')}".strip()
    return ""

def process_video(cfg, project_root, vid, processed, deps) -> str:
    context = state.capture_context(state.load_state(cfg.state_path), vid)
    capture_fields = {
        "capture_why": context.get("why", ""),
        "capture_context_at": context.get("captured_at", ""),
        "capture_source": context.get("source", "playlist"),
    }

    try:
        metadata = deps.get_metadata(vid)
    except Exception as e:  # bad/deleted/private id → retry, then permanent stub;
        attempts = state.record_attempt(cfg.state_path, vid)  # must never crash the whole run
        log.warning("metadata fetch failed for %s (attempt %d): %s", vid, attempts, e)
        if attempts >= cfg.max_attempts:
            stub_meta = {"video_id": vid, **capture_fields}
            if cfg.open_brain_spool_dir is None:
                stub = deps.build_stub_note(stub_meta, "metadata-failed", processed)
                _write_note(cfg, stub_meta, stub, topic="Other", processed=processed)
            state.mark_seen(cfg.state_path, vid)
            return "metadata-failed"
        return "retry"

    video_meta = {**metadata, **capture_fields}

    with tempfile.TemporaryDirectory() as td:
        try:
            raw_vtt = deps.fetch_captions_vtt(vid, cfg.sub_lang, td)
        except Exception as e:
            # yt-dlp rate limit / unavailable subtitles → retry, then permanent
            # stub, exactly like a metadata failure; must never end the run.
            attempts = state.record_attempt(cfg.state_path, vid)
            log.warning("caption fetch failed for %s (attempt %d): %s", vid, attempts, e)
            if attempts >= cfg.max_attempts:
                if cfg.open_brain_spool_dir is None:
                    stub = deps.build_stub_note(video_meta, "captions-failed", processed)
                    _write_note(cfg, video_meta, stub, topic="Other", processed=processed)
                state.mark_seen(cfg.state_path, vid)
                return "captions-failed"
            return "retry"
    if raw_vtt is None:
        if cfg.open_brain_spool_dir is None:
            stub = deps.build_stub_note(video_meta, "no-transcript", processed)
            _write_note(cfg, video_meta, stub, topic="Other", processed=processed)
        state.mark_seen(cfg.state_path, vid)
        return "no-transcript"

    clean = deps.clean_vtt(raw_vtt)
    if cfg.open_brain_spool_dir is not None:
        deps.write_open_brain_capture(
            cfg.open_brain_spool_dir,
            video_id=vid,
            transcript=clean,
            why=capture_fields["capture_why"] or _default_why(video_meta),
        )
        state.mark_seen(cfg.state_path, vid)
        return "bridged"
    prompt_text = Path(project_root, cfg.prompt_path).read_text()
    try:
        raw_out = deps.run_claude(prompt_text, clean, cfg.model, project_root)
    except Exception as e:  # claude failure → retry policy
        attempts = state.record_attempt(cfg.state_path, vid)
        log.warning("summarize failed for %s (attempt %d): %s", vid, attempts, e)
        if attempts >= cfg.max_attempts:
            stub = deps.build_stub_note(video_meta, "summary-failed", processed)
            _write_note(cfg, video_meta, stub, topic="Other", processed=processed)
            state.mark_seen(cfg.state_path, vid)
            return "summary-failed"
        return "retry"

    model_meta, body = deps.parse_model_output(raw_out)
    finance_source = video_meta.get("title", "") + "\n" + clean
    private_body = finance_content.annotate(cfg, model_meta, body, finance_source, vid)
    if private_body != body:
        video_meta["_sure_context_added"] = True
    content = deps.build_note(video_meta, model_meta, private_body, clean, processed)
    _write_note(cfg, video_meta, content, topic=model_meta.get("topic", "Other"),
               subtopic=model_meta.get("subtopic", "General"), processed=processed)
    state.mark_seen(cfg.state_path, vid)
    return "summarized"

def _discover_channel_videos(cfg, deps) -> None:
    # A failing channel (bad handle, bot check) is skipped for this tick and is
    # never baselined; the playlist and other channels still run.
    for url in cfg.channels:
        try:
            entries = deps.list_channel_ids(url)
        except Exception as e:
            log.warning("channel fetch failed for %s: %s", url, e)
            continue
        published = [vid for vid, ok in entries if ok]
        baselined = state.is_channel_baselined(state.load_state(cfg.state_path), url)
        queued = state.register_channel_videos(cfg.state_path, url, published,
                                               note_mod.now_stamp())
        if not baselined and not published:
            log.warning("channel %s baselined with no published videos; check the URL", url)
        elif not baselined:
            log.info("channel %s baselined (%d existing videos skipped)", url, len(published))
        elif queued:
            log.info("channel %s queued %s", url, ", ".join(queued))

def run(cfg, project_root, processed, deps) -> list:
    _discover_channel_videos(cfg, deps)
    st = state.load_state(cfg.state_path)
    ids = list(dict.fromkeys([
        *deps.list_playlist_ids(cfg.playlist_url),
        *state.requested_videos(st),
    ]))
    results = []
    for vid in ids:
        if state.is_seen(st, vid):
            continue
        outcome = process_video(cfg, project_root, vid, processed, deps)
        results.append((vid, outcome))
    return results

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    project_root = Path(__file__).resolve().parent.parent
    cfg = config_mod.load_config(project_root / "config.env", environment=os.environ)
    processed = date.today().isoformat()
    results = run(cfg, project_root, processed, _default_deps(cfg))
    for vid, outcome in results:
        log.info("processed %s → %s", vid, outcome)
    if not results:
        log.info("no new videos")

    # phase 2: shared social posts. Isolated so a queue failure can never take
    # the playlist pipeline down with it.
    try:
        for url, outcome in social.drain_social_queue(cfg, project_root, processed):
            log.info("social %s → %s", url, outcome)
    except Exception as e:
        log.error("social queue drain failed: %s", e)

if __name__ == "__main__":
    main()
