import json
import os
import fcntl
from contextlib import contextmanager
from pathlib import Path

def _load_unlocked(path: Path) -> dict:
    if not path.exists():
        return {"seen": [], "attempts": {}}
    data = json.loads(path.read_text())
    data.setdefault("seen", [])
    data.setdefault("attempts", {})
    return data

@contextmanager
def _state_lock(path: Path, *, exclusive: bool):
    lock_path = Path(path).with_suffix(Path(path).suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)

def load_state(path: Path) -> dict:
    p = Path(path)
    with _state_lock(p, exclusive=False):
        return _load_unlocked(p)

def is_seen(state: dict, vid: str) -> bool:
    return vid in state.get("seen", [])

def _save_unlocked(path: Path, data: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, p)

def mark_seen(path: Path, vid: str) -> None:
    path = Path(path)
    with _state_lock(path, exclusive=True):
        data = _load_unlocked(path)
        if vid not in data["seen"]:
            data["seen"].append(vid)
        data["attempts"].pop(vid, None)
        if vid in data.get("requested_videos", []):
            data["requested_videos"].remove(vid)
        data.get("capture_context", {}).pop(vid, None)
        _save_unlocked(path, data)

def record_attempt(path: Path, vid: str) -> int:
    path = Path(path)
    with _state_lock(path, exclusive=True):
        data = _load_unlocked(path)
        n = int(data["attempts"].get(vid, 0)) + 1
        data["attempts"][vid] = n
        _save_unlocked(path, data)
        return n

def mark_media_processed(path: Path, key: str) -> None:
    path = Path(path)
    with _state_lock(path, exclusive=True):
        data = _load_unlocked(path)
        done = data.setdefault("media_processed", [])
        if key not in done:
            done.append(key)
        _save_unlocked(path, data)

def queue_video(path: Path, video_id: str, why: str, captured_at: str) -> bool:
    """Register a YouTube request and its owner-authored capture context.

    Returns False when the video was already processed. The context is kept
    until mark_seen runs after the source note is durable.
    """
    path = Path(path)
    with _state_lock(path, exclusive=True):
        data = _load_unlocked(path)
        if video_id in data["seen"]:
            return False
        requested = data.setdefault("requested_videos", [])
        if video_id not in requested:
            requested.append(video_id)
        data.setdefault("capture_context", {})[video_id] = {
            "why": why,
            "captured_at": captured_at,
            "source": "share",
        }
        _save_unlocked(path, data)
        return True

def requested_videos(data: dict) -> list[str]:
    return list(data.get("requested_videos", []))

def capture_context(data: dict, video_id: str) -> dict:
    value = data.get("capture_context", {}).get(video_id, {})
    return dict(value) if isinstance(value, dict) else {}

def register_channel_videos(path: Path, channel_url: str, video_ids: list[str],
                            captured_at: str) -> list[str]:
    """Queue a channel's new uploads; the first call for a channel only baselines.

    The baseline marks every listed id seen so adding a channel never
    backfills its catalog. Returns the ids queued by this call.
    """
    path = Path(path)
    with _state_lock(path, exclusive=True):
        data = _load_unlocked(path)
        channels = data.setdefault("channels", {})
        if channel_url not in channels:
            channels[channel_url] = {"baselined_at": captured_at}
            in_flight = set(data.get("requested_videos", [])) | set(data["attempts"])
            for vid in video_ids:
                # In-flight ids (a /share request or a retrying playlist video)
                # are left for process_video/mark_seen.
                if vid not in data["seen"] and vid not in in_flight:
                    data["seen"].append(vid)
            _save_unlocked(path, data)
            return []
        requested = data.setdefault("requested_videos", [])
        contexts = data.setdefault("capture_context", {})
        queued = []
        for vid in video_ids:
            if vid in data["seen"] or vid in requested:
                continue
            requested.append(vid)
            contexts[vid] = {"why": "", "captured_at": captured_at, "source": "channel"}
            queued.append(vid)
        if queued:
            _save_unlocked(path, data)
        return queued

def is_channel_baselined(data: dict, channel_url: str) -> bool:
    return channel_url in data.get("channels", {})
