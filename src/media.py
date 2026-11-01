import json
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

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
    tmpdir = Path(tmpdir)
    out_tmpl = str(tmpdir / "%(id)s.%(ext)s")
    runner(
        ["yt-dlp", "--skip-download", "--write-auto-sub", "--write-sub",
         "--sub-lang", sub_lang, "--sub-format", "vtt",
         "-o", out_tmpl, _watch_url(video_id)],
        capture_output=True, text=True, check=True,
    )
    vtts = sorted(tmpdir.glob(f"{video_id}*.vtt"))
    if not vtts:
        return None
    return vtts[0].read_text()
