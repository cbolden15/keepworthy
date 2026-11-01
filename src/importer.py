"""Bulk bookmark import: load a bookmark file into a private SQLite ledger.
poll.py later drips the ledger's links into the social queue.

    python -m src.importer import FILE [--dry-run] [--format F] [--folder PATH]
                                       [--tag NAME ...] [--since YYYY-MM-DD] [--why TEXT]
    python -m src.importer status
    python -m src.importer cancel IMPORT_ID [--yes]

Output and errors carry counts, the import id, the format and exception type
names only: never a url, title, folder, tag, note or file name."""
import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
from contextlib import closing, contextmanager
from datetime import date, datetime, timezone
from pathlib import Path

from src import config as config_mod
from src import importers, ingest, ingest_server, state
from src.importers import SOURCE_LABELS, Bookmark
from src.saved_discovery import private_file

LOCK_SUFFIX = ".lock"
BUSY_TIMEOUT_S = 5
MAX_WHY = 280                          # same bound as ingest_server.MAX_WHY_CHARS
MAX_FILE_NAME = 255
COUNT_NAMES = ("found", "new", "already seen", "filtered out", "unsupported scheme", "bad rows")

SCHEMA = """
CREATE TABLE IF NOT EXISTS imports (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  file_name   TEXT    NOT NULL,
  sha256      TEXT    NOT NULL,
  format      TEXT    NOT NULL CHECK(format IN ('netscape','pocket','raindrop')),
  counts      TEXT    NOT NULL,
  created_at  TEXT    NOT NULL,
  status      TEXT    NOT NULL DEFAULT 'active' CHECK(status IN ('active','cancelled'))
);
CREATE UNIQUE INDEX IF NOT EXISTS imports_active_sha256
  ON imports(sha256) WHERE status = 'active';
CREATE TABLE IF NOT EXISTS bookmarks (
  key         TEXT    PRIMARY KEY,
  payload     TEXT    NOT NULL,
  saved_at    TEXT    NOT NULL DEFAULT '',
  import_id   INTEGER NOT NULL REFERENCES imports(id),
  enqueued_at TEXT
);
CREATE INDEX IF NOT EXISTS bookmarks_pending ON bookmarks(enqueued_at, saved_at);
"""

_SPACE = re.compile(r"\s+")
_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


class UsageError(Exception):
    """Bad input or usage: exit 2. The message names no url, title or file."""


class LockHeld(Exception):
    """Another import or cancel holds the ledger lock: exit 1."""


def _connect(path) -> sqlite3.Connection:
    db = sqlite3.connect(path, timeout=BUSY_TIMEOUT_S)
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA synchronous=FULL")
    db.executescript(SCHEMA)
    return db


def open_ledger(path) -> sqlite3.Connection:
    """The same steps as saved_discovery.open_database: a 0700 directory, the
    file created 0600 before SQLite opens it, and the owner-only check."""
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    private_file(path)
    return _connect(path)


def file_sha256(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def build_why(bookmark: Bookmark, prefix: str = "") -> str:
    """The optional --why prefix, then the Raindrop note if present, else
    "Imported from <Source>. Folder: <path>. Tags: <a, b>." One line, at most
    MAX_WHY characters."""
    parts = [prefix] if prefix else []
    if bookmark.note:
        parts.append(bookmark.note)
    else:
        text = f"Imported from {SOURCE_LABELS[bookmark.source]}."
        if bookmark.folder:
            text += f" Folder: {bookmark.folder}."
        if bookmark.tags:
            text += f" Tags: {', '.join(bookmark.tags)}."
        parts.append(text)
    return _SPACE.sub(" ", " ".join(parts)).strip()[:MAX_WHY]


def select(bookmarks, *, folder=None, tag=None, since=None) -> tuple[list[Bookmark], int]:
    """(kept, filtered_out). folder: equal or a sub-folder, case-insensitive,
    outer "/" ignored. tag: any of the given names, case-insensitive. since:
    saved on or after that day; undated bookmarks are dropped."""
    want_folder = folder.strip("/").casefold() if folder is not None else None
    want_tags = {t.casefold() for t in tag} if tag else None
    kept = []
    for bookmark in bookmarks:
        if want_folder is not None:
            have = bookmark.folder.strip("/").casefold()
            if have != want_folder and not have.startswith(want_folder + "/"):
                continue
        if want_tags and not want_tags & {t.casefold() for t in bookmark.tags}:
            continue
        if since and (not bookmark.saved_at or bookmark.saved_at[:10] < since):
            continue
        kept.append(bookmark)
    return kept, len(bookmarks) - len(kept)


def plan_import(db, state_path, bookmarks) -> tuple[list[tuple[str, Bookmark]], int, int]:
    """([(key, bookmark)] to insert, already-seen count, bad-row count). A key
    already in the ledger, in state `seen`, or produced by an earlier row of
    the same file is already seen; the first occurrence in file order wins.
    A url that url_key cannot parse is a bad row."""
    seen = set(state.load_state(state_path).get("seen", []))
    planned, keys, already, bad = [], set(), 0, 0
    for bookmark in bookmarks:
        try:
            key = ingest.url_key(bookmark.url)
        except ValueError:
            bad += 1
            continue
        if (key in keys or key in seen
                or db.execute("SELECT 1 FROM bookmarks WHERE key=?", (key,)).fetchone()):
            already += 1
            continue
        keys.add(key)
        planned.append((key, bookmark))
    return planned, already, bad


def queue_item(bookmark: Bookmark, *, why: str, received_at: str) -> dict:
    """The queue line the drip appends: the only contract with the processor."""
    return {"url": bookmark.url, "why": why, "text": None, "received_at": received_at,
            "capture_source": "import", "import_title": bookmark.title,
            "import_folder": bookmark.folder, "import_tags": list(bookmark.tags),
            "import_saved_at": bookmark.saved_at, "import_source": bookmark.source}


def record_import(db, *, file_name, sha256, fmt, counts, rows, now) -> int:
    """One transaction: the imports row and every bookmarks row. rows are
    (key, queue item) pairs. Returns the import id."""
    with db:
        cur = db.execute(
            "INSERT INTO imports(file_name, sha256, format, counts, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (importers.sanitize_text(file_name, MAX_FILE_NAME), sha256, fmt,
             json.dumps(counts), now.isoformat()))
        import_id = cur.lastrowid
        db.executemany(
            "INSERT INTO bookmarks(key, payload, saved_at, import_id) VALUES (?, ?, ?, ?)",
            [(key, json.dumps(item, ensure_ascii=False), item["import_saved_at"], import_id)
             for key, item in rows])
    return import_id


def _utc_now(now=None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _sent_on(db, day: str) -> int:
    """Rows the drip stamped on that UTC day (YYYY-MM-DD)."""
    return db.execute("SELECT COUNT(*) FROM bookmarks WHERE substr(enqueued_at, 1, 10) = ?",
                      (day,)).fetchone()[0]


def _waiting(db) -> int:
    return db.execute("SELECT COUNT(*) FROM bookmarks WHERE enqueued_at IS NULL").fetchone()[0]


def drip(db, queue_path, limit: int, now: datetime | None = None) -> dict:
    """Append up to `limit` unsent bookmarks per UTC day to the social queue,
    newest saved first, undated last. Append first, stamp second (the
    saved_discovery.flush_outbox contract): a crash in between redelivers the
    item, and the processor's seen check makes the copy a "duplicate"."""
    now = _utc_now(now)
    sent_today = _sent_on(db, now.date().isoformat())
    waiting = _waiting(db)
    if limit <= 0:
        return {"enqueued": 0, "sent_today": sent_today, "waiting": waiting, "limit": limit}
    budget = max(0, limit - sent_today)
    rows = db.execute("SELECT key, payload FROM bookmarks WHERE enqueued_at IS NULL "
                      "ORDER BY saved_at DESC, key ASC LIMIT ?", (budget,)).fetchall()
    stamp = now.isoformat()
    for key, payload in rows:
        ingest.append_item(queue_path, json.loads(payload))
        with db:
            db.execute("UPDATE bookmarks SET enqueued_at=? WHERE key=?", (stamp, key))
    return {"enqueued": len(rows), "sent_today": sent_today + len(rows),
            "waiting": waiting - len(rows), "limit": limit}


def status(db, limit: int, now=None) -> dict:
    """Ledger counts for the status command; sent_today uses the drip's UTC day."""
    now = _utc_now(now)
    waiting = _waiting(db)
    sent = db.execute("SELECT COUNT(*) FROM bookmarks WHERE enqueued_at IS NOT NULL").fetchone()[0]
    return {"waiting": waiting, "sent": sent, "sent_today": _sent_on(db, now.date().isoformat()),
            "limit": limit, "days_remaining": math.ceil(waiting / limit) if limit > 0 else None}


def cancel(db, import_id: int, *, yes: bool) -> int:
    """The number of this import's unsent rows. With yes=True, in one
    transaction, the import is marked cancelled and those rows are deleted;
    sent rows stay, so they keep deduping a later re-import."""
    if not yes:
        return db.execute("SELECT COUNT(*) FROM bookmarks WHERE import_id=? AND enqueued_at IS NULL",
                          (import_id,)).fetchone()[0]
    with db:
        db.execute("UPDATE imports SET status='cancelled' WHERE id=?", (import_id,))
        return db.execute("DELETE FROM bookmarks WHERE import_id=? AND enqueued_at IS NULL",
                          (import_id,)).rowcount


@contextmanager
def _import_lock(db_path):
    lock_path = Path(str(db_path) + LOCK_SUFFIX)
    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with lock_path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise LockHeld(f"another import is running (lock {lock_path})") from None
        yield


def _ledger_for_dry_run(path) -> sqlite3.Connection:
    """A dry run reads the real ledger when there is one and never creates it."""
    path = Path(path)
    if path.exists():
        return open_ledger(path)
    return _connect(":memory:")


def _check_args(args) -> str:
    """Validate --since and --why; return the --why prefix."""
    if args.since is not None:
        try:
            if not _DAY.fullmatch(args.since):
                raise ValueError
            date.fromisoformat(args.since)
        except ValueError:
            raise UsageError("--since must be a date as YYYY-MM-DD") from None
    prefix = args.why.strip()
    if prefix:
        try:
            ingest_server._why(prefix)
        except ValueError as e:
            raise UsageError(f"--why: {e}") from None
    return prefix


def _print_counts(counts: dict) -> None:
    for name in COUNT_NAMES:
        print(f"{name}: {counts[name]}")


def _cmd_import(cfg, args) -> int:
    prefix = _check_args(args)
    path = Path(args.file)
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        raise UsageError("the bookmark file does not exist") from None
    if size > importers.MAX_FILE_BYTES:
        raise UsageError(f"file is {size / (1024 * 1024):.1f} MB; the limit is "
                         f"{importers.MAX_FILE_BYTES // (1024 * 1024)} MB")
    if args.dry_run:
        with closing(_ledger_for_dry_run(cfg.import_db_path)) as db:
            return _run_import(cfg, args, path, prefix, db)
    with _import_lock(cfg.import_db_path), closing(open_ledger(cfg.import_db_path)) as db:
        return _run_import(cfg, args, path, prefix, db)


def _run_import(cfg, args, path, prefix, db) -> int:
    digest = file_sha256(path)
    hit = db.execute("SELECT id, created_at FROM imports WHERE sha256=? AND status='active'",
                     (digest,)).fetchone()
    if hit:
        print(f"this file was imported on {hit[1][:10]} (import {hit[0]})")
        return 0
    module = importers.module_for(args.format) if args.format else importers.detect(path)
    fmt = module.__name__.rsplit(".", 1)[1]
    result = module.parse(path, max_items=importers.MAX_BOOKMARKS)
    kept, filtered = select(result.bookmarks, folder=args.folder, tag=args.tag, since=args.since)
    planned, already, bad_keys = plan_import(db, cfg.state_path, kept)
    counts = {"found": result.found, "new": len(planned), "already seen": already,
              "filtered out": filtered, "unsupported scheme": result.unsupported_scheme,
              "bad rows": result.bad_rows + bad_keys}
    if args.dry_run:
        _print_counts(counts)
        return 0
    now = datetime.now(timezone.utc).replace(microsecond=0)
    rows = [(key, queue_item(b, why=build_why(b, prefix), received_at=now.isoformat()))
            for key, b in planned]
    import_id = record_import(db, file_name=path.name, sha256=digest, fmt=fmt,
                              counts=counts, rows=rows, now=now)
    _print_counts(counts)
    print(f"import id: {import_id}")
    return 0


def _cmd_status(cfg) -> int:
    with closing(open_ledger(cfg.import_db_path)) as db:
        stats = status(db, cfg.import_daily_limit)
    print(f"waiting: {stats['waiting']}")
    print(f"sent: {stats['sent']}")
    print(f"sent today: {stats['sent_today']}")
    print(f"limit: {stats['limit']} per day")
    if stats["days_remaining"] is None:
        print("days remaining: off (IMPORT_DAILY_LIMIT=0)")
    else:
        print(f"days remaining: {stats['days_remaining']}")
    return 0


def _cmd_cancel(cfg, args) -> int:
    with _import_lock(cfg.import_db_path), closing(open_ledger(cfg.import_db_path)) as db:
        row = db.execute("SELECT status FROM imports WHERE id=?", (args.import_id,)).fetchone()
        if row is None:
            raise UsageError(f"there is no import {args.import_id}")
        if row[0] == "cancelled":
            print(f"import {args.import_id} is already cancelled")
            return 0
        count = cancel(db, args.import_id, yes=args.yes)
    if args.yes:
        print(f"import {args.import_id} cancelled; deleted {count} unsent links")
    else:
        print(f"import {args.import_id} has {count} unsent links; "
              "pass --yes to cancel it and delete them")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m src.importer",
                                     description="Import a bookmark file into the drip ledger.")
    commands = parser.add_subparsers(dest="command", required=True)
    imp = commands.add_parser("import", help="load a bookmark file into the ledger")
    imp.add_argument("file", help="Netscape bookmark HTML, Pocket list.csv or Raindrop CSV")
    imp.add_argument("--dry-run", action="store_true", help="print the counts, write nothing")
    imp.add_argument("--format", choices=importers.SOURCES, help="skip format detection")
    imp.add_argument("--folder", help="only this folder and its sub-folders")
    imp.add_argument("--tag", action="append", help="only bookmarks with this tag (repeatable)")
    imp.add_argument("--since", help="only bookmarks saved on or after YYYY-MM-DD")
    imp.add_argument("--why", default="", help="text put before each link's why")
    commands.add_parser("status", help="show waiting and sent counts")
    cancel_cmd = commands.add_parser("cancel", help="stop an import and delete its unsent links")
    cancel_cmd.add_argument("import_id", type=int)
    cancel_cmd.add_argument("--yes", action="store_true", help="delete; without it, only count")
    return parser


def _describe(error: Exception) -> str:
    # an OSError's str() carries the file name; its strerror does not
    detail = error.strerror if isinstance(error, OSError) and error.strerror else str(error)
    return f"{type(error).__name__}: {detail}"


def main(argv=None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as e:                  # usage errors are exit 2, --help is 0
        return 0 if e.code in (0, None) else 2
    old_umask = os.umask(0o077)
    try:
        project_root = Path(__file__).resolve().parent.parent
        cfg = config_mod.load_config(project_root / "config.env", environment=os.environ)
        if args.command == "status":
            return _cmd_status(cfg)
        if args.command == "cancel":
            return _cmd_cancel(cfg, args)
        return _cmd_import(cfg, args)
    except (UsageError, importers.ParseError) as e:
        print(str(e), file=sys.stderr)
        return 2
    except LockHeld as e:
        print(str(e), file=sys.stderr)
        return 1
    except (sqlite3.Error, OSError, ValueError) as e:
        print(_describe(e), file=sys.stderr)
        return 1
    finally:
        os.umask(old_umask)


if __name__ == "__main__":
    sys.exit(main())
