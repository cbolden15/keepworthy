import json
import logging
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlsplit

from src.transcript import clean_vtt

log = logging.getLogger(__name__)
_CAPTION_TIMEOUT_SECONDS = 120

def _watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"

def youtube_video_id(url: str) -> str | None:
    """Extract a video id only from recognized YouTube watch/short URLs."""
    try:
        parsed = urlsplit(url)
    except (TypeError, ValueError):
        return None
    host = (parsed.hostname or "").lower().rstrip(".")
    if host == "youtu.be" or host.endswith(".youtu.be"):
        return parsed.path.strip("/").split("/", 1)[0] or None
    if host == "youtube.com" or host.endswith(".youtube.com"):
        if parsed.path == "/watch":
            return (parse_qs(parsed.query).get("v") or [None])[0]
        parts = parsed.path.strip("/").split("/")
        if len(parts) >= 2 and parts[0] in {"shorts", "embed", "live"}:
            return parts[1] or None
    return None

def list_playlist_ids(playlist_url: str, runner=subprocess.run) -> list[str]:
    r = runner(
        ["yt-dlp", "--flat-playlist", "--print", "id", playlist_url],
        capture_output=True, text=True, check=True,
    )
    ids = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
    ids.reverse()  # yt-dlp lists newest-first; process oldest-first
    return ids

def list_channel_ids(channel_url: str, limit: int = 15,
                     runner=subprocess.run) -> list[tuple[str, bool]]:
    # The /videos tab already excludes Shorts and lives. In flat mode a video
    # that has not aired yet (upcoming premiere, live in progress) has no
    # duration, so it is reported as unpublished.
    r = runner(
        ["yt-dlp", "--flat-playlist", "--playlist-end", str(limit),
         "--print", "%(id)s|%(duration)s", f"{channel_url}/videos"],
        capture_output=True, text=True, check=True,
    )
    entries = []
    for ln in r.stdout.splitlines():
        vid, _, duration = ln.strip().partition("|")
        if vid:
            entries.append((vid, duration.strip() not in ("", "NA")))
    entries.reverse()  # newest-first from yt-dlp; process oldest-first
    return entries

def _fmt_duration(seconds) -> str:
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return ""
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}"

def _fmt_date(yyyymmdd: str) -> str:
    if yyyymmdd and len(yyyymmdd) == 8:
        return f"{yyyymmdd[0:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]}"
    return yyyymmdd or ""

def get_metadata(video_id: str, runner=subprocess.run) -> dict:
    r = runner(
        ["yt-dlp", "--skip-download", "--dump-json", _watch_url(video_id)],
        capture_output=True, text=True, check=True,
    )
    j = json.loads(r.stdout)
    return {
        "title": j.get("title", ""),
        "channel": j.get("channel") or j.get("uploader", ""),
        "url": j.get("webpage_url", _watch_url(video_id)),
        "video_id": video_id,
        "published": _fmt_date(j.get("upload_date", "")),
        "duration": _fmt_duration(j.get("duration")),
    }

def fetch_captions_vtt(video_id: str, sub_lang: str, tmpdir, runner=subprocess.run) -> str | None:
    """Return usable captions, trying English (Original) after the English track."""
    tracks = ("en", "en-orig") if sub_lang == "en" else (sub_lang,)
    last_error = None
    for track in tracks:
        # Each download gets a fresh directory so failures cannot leave usable-looking files.
        with TemporaryDirectory(dir=tmpdir) as attempt_dir:
            directory = Path(attempt_dir)
            try:
                runner(
                    ["yt-dlp", "--skip-download", "--write-auto-sub", "--write-sub",
                     "--sub-lang", track, "--sub-format", "vtt",
                     "-o", str(directory / "%(id)s.%(ext)s"), _watch_url(video_id)],
                    capture_output=True, text=True, check=True, timeout=_CAPTION_TIMEOUT_SECONDS,
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                last_error = exc
                continue

            for path in sorted(directory.glob(f"{video_id}*.vtt")):
                try:
                    captions = path.read_text(encoding="utf-8-sig")
                except UnicodeDecodeError:
                    continue
                if captions.startswith("WEBVTT") and clean_vtt(captions).strip():
                    if track != sub_lang:
                        log.info("used %s caption fallback for %s", track, video_id)
                    return captions

    if last_error is not None:
        raise last_error
    return None
