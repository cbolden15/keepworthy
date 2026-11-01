import fcntl
import hashlib
import json
import logging
import os
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

log = logging.getLogger("yt-obsidian")

def _processing_path(queue_path) -> Path:
    p = Path(queue_path)
    return p.with_name(p.stem + ".processing.jsonl")

def _lock_path(queue_path) -> Path:
    p = Path(queue_path)
    return p.with_name(p.name + ".lock")

@contextmanager
def _queue_lock(queue_path):
    """Serialize appends against rotation — the ingest server and the drain are
    separate processes, so the lock lives in a sidecar file, not in memory.
    Hold it only across the append or the rename, never across a drain.

    Not re-entrant: flock is per open file description, so a nested acquisition
    in one process blocks on itself forever with no timeout. Never call
    append_item or rotate_queue from inside another _queue_lock block."""
    lp = _lock_path(queue_path)
    lp.parent.mkdir(parents=True, exist_ok=True)
    # read-only: LOCK_EX needs no write access, and a lock file created by the
    # ingest server must stay openable by the drain if they run as different users
    fd = os.open(lp, os.O_RDONLY | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)                       # closing releases the lock

def _write_all(fd, buf: bytes) -> None:
    while buf:                             # os.write may be short: signals, rlimits
        n = os.write(fd, buf)
        if n <= 0:                         # no progress — fail rather than spin hot
            raise OSError("os.write made no progress on the queue file")
        buf = buf[n:]

def _fsync_dir(path: Path) -> None:
    try:                                   # each drain re-creates the queue file, so
        fd = os.open(path, os.O_RDONLY)    # the first share of a window needs its
        try:                               # directory entry on disk too
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:                        # unsupported on some filesystems: skip
        pass

def append_item(queue_path, item: dict) -> None:
    p = Path(queue_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(item, ensure_ascii=False) + "\n"
    with _queue_lock(p):                   # no rotation mid-append
        fd = os.open(p, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            # A crash or a full disk can leave the last line unterminated, and the
            # recovery write below cannot help there: SIGKILL runs no code, and a
            # full disk fails the 1-byte write too. This append is the first moment
            # guaranteed to have both a live process and space, so heal it here or
            # the new item concatenates onto the torn one and both are lost.
            size = os.lseek(fd, 0, os.SEEK_END)
            if size and os.pread(fd, 1, size - 1) != b"\n":
                os.write(fd, b"\n")
            try:
                _write_all(fd, line.encode())
            except OSError:
                try:                       # an unterminated line would swallow the
                    os.write(fd, b"\n")    # next item; keep the damage to this one
                except OSError:
                    pass
                raise
            os.fsync(fd)                   # returning must mean durable
        finally:
            os.close(fd)
    _fsync_dir(p.parent)

def _read_items(path: Path) -> list[dict]:
    items = []
    # errors="replace": bad bytes must fail JSON parsing and take the skip path
    # below — a decode error here would raise out of rotate_queue and wedge the
    # drain permanently, since finish_queue would never run
    # split("\n"), not splitlines(): json.dumps(ensure_ascii=False) leaves U+2028,
    # U+2029 and U+0085 unescaped, and splitlines() would break one item into two
    # unparseable halves — a silent loss of an item the sharer was told was saved
    for ln in path.read_text(encoding="utf-8", errors="replace").split("\n"):
        ln = ln.strip()
        if not ln:
            continue
        try:
            value = json.loads(ln)
        except json.JSONDecodeError:
            log.warning("skipping malformed queue line: %.80s", ln)
            continue
        if not isinstance(value, dict):    # a non-object line would crash the drain
            log.warning("skipping non-object queue line: %.80s", ln)
            continue
        items.append(value)
    return items

def rotate_queue(queue_path) -> list[dict]:
    q = Path(queue_path)
    proc = _processing_path(q)
    with _queue_lock(q):                   # check + rename must be one step
        if not proc.exists():              # else: prior run crashed mid-drain
            if not q.exists():
                return []
            q.rename(proc)
    return _read_items(proc)

def finish_queue(queue_path) -> None:
    _processing_path(Path(queue_path)).unlink(missing_ok=True)

# iOS share-sheet tokens: the share sheet stamps a fresh per-share value onto
# one of these on every share of the *same* post (YouTube ?si=, Instagram
# ?igsh=/?igshid=, Facebook ?fbclid=, ...), so they must not affect the dedupe
# key. These never identify content on ANY host, so they are stripped
# unconditionally. Do not extend this set with anything that can identify
# distinct content -- v, p, id, story_fbid and similar must survive untouched.
_TRACKING_PARAMS = {
    "si", "igsh", "igshid", "fbclid", "gclid", "mibextid",
    "ref_src", "ref_url", "share_id",
}

# X's ?s=/?t= and a generic ?ref= are share-sheet tokens on a recognized
# social platform, but on a generic (non-social) URL the same three params
# routinely identify the content itself (forum thread ids, referral-coded
# article links). Stripping them there collides distinct pages into one
# dedupe key, so they are stripped only when classify_social_url() says the
# host is a known platform -- never for "generic".
_SOCIAL_ONLY_TRACKING_PARAMS = {"s", "t", "ref"}

def url_key(url: str) -> str:
    """The dedupe key for a shared URL. Must stay total: any input the ingest
    server accepts (scheme://rest) may reach here, including malformed hosts
    that make urlsplit raise ValueError -- that propagates unchanged, exactly
    as before, so the existing caller-side guard (src/social.py's
    `_item_key`) keeps working. Nothing added below introduces a new
    exception type."""
    parts = urlsplit(url.strip())
    strip_params = set(_TRACKING_PARAMS)
    # function-local import: src/social.py imports this module at load time,
    # so a module-level import here would be circular.
    from src.social import classify_social_url
    if classify_social_url(url) != "generic":
        strip_params |= _SOCIAL_ONLY_TRACKING_PARAMS
    query = urlencode(sorted(
        (k, v) for k, v in parse_qsl(parts.query)
        if not k.lower().startswith("utm_") and k.lower() not in strip_params
    ))
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[len("www."):]
    path = parts.path
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    canon = urlunsplit((parts.scheme, host, path, query, ""))
    return "url:" + hashlib.sha1(canon.encode()).hexdigest()
