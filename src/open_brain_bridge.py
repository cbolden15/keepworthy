"""Atomic handoff from the homelab transcriber to Open Brain's canonical writer."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
_MAX_TRANSCRIPT_BYTES = 100_000
_DEFAULT_WHY = "Saved to the YouTube transcript playlist."

_MAX_RECORD_BYTES = 131_072
_MAX_WHY = 280
_TRUNCATED = "\n[truncated]"
_SUMMARY_ORIGIN = "producer:x1-claude"
_FETCH_STATUSES = frozenset({"not_attempted", "complete", "partial", "failed"})
_SOURCE_TYPES = frozenset({"youtube", "social", "web", "text"})
_CONTENT_KINDS = frozenset({"event", "article", "product", "place", "post", "video", "other"})
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")


def _atomic_write(spool_root: Path, name: str, payload: bytes, *, once: bool = False) -> Path:
    spool_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if spool_root.is_symlink():
        raise ValueError("invalid Open Brain spool root")
    path = spool_root / name
    temporary = spool_root / f".{name}.{secrets.token_hex(8)}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        try:
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short bridge write")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if once:
            try:
                os.link(temporary, path)
            except FileExistsError:
                return path
        else:
            os.replace(temporary, path)
        directory = os.open(spool_root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()
    return path


def write_youtube_capture(
    spool_root: Path,
    *,
    video_id: str,
    transcript: str,
    why: str = "",
) -> Path:
    """Write one bounded, idempotent bridge record without publishing Markdown."""
    if not isinstance(spool_root, Path) or not spool_root.is_absolute():
        raise ValueError("invalid Open Brain spool root")
    if not isinstance(video_id, str) or _VIDEO_ID.fullmatch(video_id) is None:
        raise ValueError("invalid YouTube video id")
    normalized = transcript.strip()
    if not normalized or len(normalized.encode("utf-8")) > _MAX_TRANSCRIPT_BYTES:
        raise ValueError("invalid YouTube transcript")
    normalized_why = why.strip() or _DEFAULT_WHY

    payload = json.dumps(
        {
            "privacy": "work",
            "schema_version": 1,
            "text": normalized,
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "why": normalized_why,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _atomic_write(spool_root, f"{video_id}.json", payload)


def saved_item_path(spool_root: Path, key: str) -> Path:
    """One file per dedupe key, so a redelivered share finds its own record."""
    return spool_root / (hashlib.sha256(key.encode("utf-8")).hexdigest()[:32] + ".json")


def _utc_z(value: str) -> str:
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise ValueError("invalid received_at") from error
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _bounded(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value[:limit] or None


def _encode(record: dict) -> bytes:
    return json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _fit(record: dict) -> bytes:
    """Halve media_text, then body, then shared_text until the file fits; the
    record keeps a visible marker so a reader knows content was cut."""
    payload = _encode(record)
    for field in ("media_text", "body", "shared_text"):
        while len(payload) > _MAX_RECORD_BYTES and record[field]:
            text = record[field].removesuffix(_TRUNCATED)
            keep = len(text) // 2
            record[field] = text[:keep] + _TRUNCATED if keep else ""
            payload = _encode(record)
    if len(payload) > _MAX_RECORD_BYTES:
        raise ValueError("saved item does not fit")
    return payload


def write_saved_item(
    spool_root: Path,
    *,
    key: str,
    url: str,
    why: str,
    received_at: str,
    fetch_status: str,
    source_type: str | None = None,
    content_kind: str | None = None,
    platform: str | None = None,
    title: str | None = None,
    author: str | None = None,
    body: str = "",
    media_text: str = "",
    shared_text: str = "",
    summary: str | None = None,
) -> Path:
    """Write one saved item record v1, once. Open Brain keeps the first save of
    a URL, so an existing record is never rewritten."""
    if not isinstance(spool_root, Path) or not spool_root.is_absolute():
        raise ValueError("invalid Open Brain spool root")
    required_strings = (key, url, why, received_at, fetch_status, body, media_text, shared_text)
    optional_strings = (source_type, content_kind, platform, title, author, summary)
    if any(not isinstance(value, str) for value in required_strings) or any(
        value is not None and not isinstance(value, str) for value in optional_strings
    ):
        raise ValueError("invalid saved item field type")
    if not isinstance(key, str) or not key:
        raise ValueError("invalid saved item key")
    if not isinstance(url, str) or not url.startswith("https://"):
        raise ValueError("saved item url must be https")
    why = why.strip() if isinstance(why, str) else ""
    if not why or len(why) > _MAX_WHY or _CONTROL.search(why):
        raise ValueError("invalid saved item why")
    if fetch_status not in _FETCH_STATUSES:
        raise ValueError("invalid fetch_status")
    if source_type is not None and source_type not in _SOURCE_TYPES:
        raise ValueError("invalid source_type")
    if content_kind is not None and content_kind not in _CONTENT_KINDS:
        raise ValueError("invalid content_kind")
    record = {
        "schema_version": 1,
        "url": url,
        "why": why,
        "received_at": _utc_z(received_at),
        "fetch_status": fetch_status,
        "source_type": source_type,
        "content_kind": content_kind,
        "platform": _bounded(platform.lower() if platform else None, 40),
        "title": _bounded(title, 300),
        "author": _bounded(author, 200),
        "body": body or "",
        "media_text": media_text or "",
        "shared_text": shared_text or "",
        "summary": summary or None,
        "summary_origin": _SUMMARY_ORIGIN if summary else None,
    }
    path = saved_item_path(spool_root, key)
    if path.exists():
        return path
    payload = _fit(record)
    return _atomic_write(spool_root, path.name, payload, once=True)
