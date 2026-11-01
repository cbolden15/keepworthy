"""Daily Meta saved-feed discovery with a durable, replay-safe queue outbox."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit

from src import ingest
from src.saved_feeds import Client

BACKFILL = {"instagram": 25, "facebook": 15, "threads": 10}
DAILY = {"instagram": 100, "facebook": 100, "threads": 10}


def private_file(path):
    info = Path(path).lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Configuration and state must be owner-only regular files")


def load_config(path):
    private_file(path)
    value = json.loads(Path(path).read_text())
    if set(value) != {"cookies", "database", "queue"}:
        raise ValueError("Discovery configuration requires cookies, database, queue")
    for name, val in value.items():
        if not isinstance(val, str) or not Path(val).is_absolute():
            raise ValueError(f"{name} must be an absolute path")
    private_file(value["cookies"])
    return {k: Path(v) for k, v in value.items()}


def _text(value):
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > 65_536:
        raise ValueError("Saved text has invalid shape or exceeds size limit")
    return value


def normalize(platform, raw, received_at):
    """Project only source identity/text; do not persist feed tokens or CDN URLs."""
    if platform == "threads":
        posts = [x["post"] for x in raw["thread_items"]]
        media = next((p for p in posts if p.get("has_viewer_saved")), posts[0])
    else:
        media = raw.get("savable", {}) if platform == "facebook" else raw
    if platform in ("instagram", "threads"):
        code = media["code"]
        if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", code):
            raise ValueError("Invalid post code")
        user = media.get("user") or {}
        author = _text(user.get("username"))
        if platform == "instagram":
            url = f"https://www.instagram.com/p/{code}/"
        else:
            if not re.fullmatch(r"[A-Za-z0-9_.]{1,64}", author):
                raise ValueError("Invalid Threads author")
            url = f"https://www.threads.com/@{author}/post/{code}"
        caption = _text((media.get("caption") or {}).get("text"))
        kind = "video" if media.get("media_type") == 2 else "post"
        if any(p.get("media_type") == 2 for p in media.get("carousel_media") or []):
            kind = "video"
    else:
        url = media.get("savable_permalink") or media.get("url")
        caption = _text((media.get("savable_title") or {}).get("text"))
        attrs = media.get("savable_attributes") or []
        author = _text(attrs[-1].get("text")) if attrs else ""
        kind = "video" if media.get("savable_default_category") == "VIDEO" else "post"
    # Only authenticated platform posts enter this integration. External saved
    # links need a separate web-ingest policy rather than arbitrary URL fetching.
    parts = urlsplit(url)
    hosts = {"instagram": {"instagram.com", "www.instagram.com"},
             "facebook": {"facebook.com", "www.facebook.com", "m.facebook.com"},
             "threads": {"threads.com", "www.threads.com", "threads.net", "www.threads.net"}}
    if (parts.scheme != "https" or parts.hostname not in hosts[platform]
            or parts.username or parts.password or parts.port or len(url) > 4096):
        raise ValueError("Saved permalink is outside its platform")
    url = urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))
    key = ingest.url_key(url)
    item = {"url": url, "why": "", "received_at": received_at,
            "capture_source": "saved_feed", "saved_platform": platform,
            "saved_caption": caption, "saved_author": author,
            "saved_content_kind": kind, "transcription_policy": "free_only"}
    return key, item


def open_database(path):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.exists():
        private_file(path)
    # Do not put an initially world-readable database on disk before chmod.
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    db = sqlite3.connect(path, timeout=10)
    db.execute("PRAGMA synchronous=FULL")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS items (
          key TEXT PRIMARY KEY, platform TEXT NOT NULL,
          payload TEXT NOT NULL, enqueued INTEGER NOT NULL DEFAULT 0 CHECK(enqueued IN (0,1))
        );
        CREATE TABLE IF NOT EXISTS feeds (
          platform TEXT PRIMARY KEY, last_success TEXT, status TEXT NOT NULL,
          snapshot TEXT NOT NULL DEFAULT '[]', gap_since TEXT
        );
    """)
    return db


def flush_outbox(db, queue):
    count = 0
    for key, payload in db.execute("SELECT key,payload FROM items WHERE enqueued=0 ORDER BY rowid").fetchall():
        # Durable queue append precedes acknowledgment. A crash between these
        # steps can redeliver; the existing processor's URL key dedupes it.
        ingest.append_item(queue, json.loads(payload))
        with db:
            db.execute("UPDATE items SET enqueued=1 WHERE key=?", (key,))
        count += 1
    return count


def discover(db, queue, client, now=None):
    now = now or datetime.now(timezone.utc).isoformat()
    results = {}
    flush_outbox(db, queue)
    for platform in BACKFILL:
        previous = db.execute("SELECT last_success,snapshot,gap_since FROM feeds WHERE platform=?", (platform,)).fetchone()
        initialized = previous is not None and previous[0] is not None
        limit = DAILY[platform] if initialized else BACKFILL[platform]
        try:
            records = getattr(client, platform)(limit)
            old_snapshot = json.loads(previous[1]) if initialized else []
            old_keys = set(old_snapshot)
            projected, candidates = {}, {}
            # Validate every new record before committing. Once the previous
            # boundary is reached, older history is outside this import's scope.
            for raw in records:
                key, item = normalize(platform, raw, now)
                projected[key] = item
                if initialized and key in old_keys:
                    break
                candidates[key] = item
            gap = bool(initialized and client.has_more and not old_keys.intersection(projected))
            gap_since = (previous[2] if previous else None) or (now if gap else None)
            inserted = 0
            snapshot = list(dict.fromkeys([*projected, *old_snapshot]))[:DAILY[platform]]
            with db:
                for key, item in candidates.items():
                    inserted += db.execute("INSERT OR IGNORE INTO items(key,platform,payload) VALUES(?,?,?)",
                                           (key, platform, json.dumps(item))).rowcount
                db.execute("""INSERT INTO feeds(platform,last_success,status,snapshot,gap_since) VALUES(?,?,?,?,?)
                    ON CONFLICT(platform) DO UPDATE SET last_success=excluded.last_success,
                    status=excluded.status,snapshot=excluded.snapshot,gap_since=excluded.gap_since""",
                           (platform, now, "gap" if gap_since else "ok", json.dumps(snapshot), gap_since))
            enqueued = flush_outbox(db, queue)
            results[platform] = {"status": "gap" if gap_since else "ok", "fetched": len(records),
                                 "new": inserted, "enqueued": enqueued}
        except (HTTPError, URLError, OSError, ValueError, KeyError, TypeError, IndexError, StopIteration) as error:
            if isinstance(error, HTTPError) and error.code in (401, 403):
                status = "login_required"
            elif isinstance(error, ValueError) and "Login or account challenge" in str(error):
                status = "login_required"
            else:
                status = "feed_error"
            with db:
                db.execute("INSERT INTO feeds(platform,status) VALUES(?,?) ON CONFLICT(platform) DO UPDATE SET status=excluded.status",
                           (platform, status))
            # Never log raw upstream exception text: it can contain tokens/URLs.
            results[platform] = {"status": status, "error_type": type(error).__name__}
    return results


def status(db):
    return {"feeds": [dict(zip(("platform", "last_success", "status", "gap_since"), row))
                      for row in db.execute("SELECT platform,last_success,status,gap_since FROM feeds ORDER BY platform")],
            "discovered": db.execute("SELECT COUNT(*) FROM items").fetchone()[0],
            "outbox_pending": db.execute("SELECT COUNT(*) FROM items WHERE enqueued=0").fetchone()[0]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--status", action="store_true", help="Show counts and feed health without network requests")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        cfg = load_config(args.config)
        lock = cfg["database"].with_suffix(".lock")
        lock.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with lock.open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with open_database(cfg["database"]) as db:
                if args.status:
                    print(json.dumps(status(db)))
                    return 0
                results = discover(db, cfg["queue"], Client(cfg["cookies"]))
                print(json.dumps(results))
                return int(any(r["status"] != "ok" for r in results.values()))
    except (OSError, ValueError, sqlite3.Error) as error:
        print(json.dumps({"status": "discovery_error", "error_type": type(error).__name__}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
