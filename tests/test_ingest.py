import errno
import json
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from src import ingest
from src.ingest import append_item, rotate_queue, finish_queue, url_key

def test_append_and_rotate_roundtrip(tmp_path):
    q = tmp_path / "queue.jsonl"
    append_item(q, {"url": "https://a", "text": None, "received_at": "t1"})
    append_item(q, {"url": "https://b", "text": "cap", "received_at": "t2"})
    items = rotate_queue(q)
    assert [i["url"] for i in items] == ["https://a", "https://b"]
    assert not q.exists()  # rotated away
    # new appends during processing land in a fresh live queue
    append_item(q, {"url": "https://c", "text": None, "received_at": "t3"})
    finish_queue(q)
    assert [i["url"] for i in rotate_queue(q)] == ["https://c"]

def test_rotate_resumes_after_crash(tmp_path):
    q = tmp_path / "queue.jsonl"
    append_item(q, {"url": "https://a"})
    first = rotate_queue(q)          # crash before finish_queue
    again = rotate_queue(q)          # next run resumes the processing file
    assert [i["url"] for i in first] == ["https://a"]
    assert first == again

def test_rotate_empty_and_malformed(tmp_path):
    q = tmp_path / "queue.jsonl"
    assert rotate_queue(q) == []
    q.write_text('{"url": "https://a"}\nnot-json\n')
    assert [i["url"] for i in rotate_queue(q)] == ["https://a"]
    finish_queue(q)

def test_rotate_skips_non_object_json(tmp_path, caplog):
    q = tmp_path / "queue.jsonl"
    q.write_text('null\n123\n"str"\n[1, 2]\ntrue\n{"url": "https://a"}\n')
    with caplog.at_level(logging.WARNING, logger="yt-obsidian"):
        assert [i["url"] for i in rotate_queue(q)] == ["https://a"]
    assert any("non-object" in r.getMessage() for r in caplog.records)
    # the poison batch must not wedge the next drain
    finish_queue(q)
    append_item(q, {"url": "https://b"})
    assert [i["url"] for i in rotate_queue(q)] == ["https://b"]
    finish_queue(q)

def test_rotate_with_lingering_processing_and_pending(tmp_path):
    q = tmp_path / "queue.jsonl"
    q.write_text('null\n{"url": "https://a"}\n')
    assert [i["url"] for i in rotate_queue(q)] == ["https://a"]   # drain crashes here
    append_item(q, {"url": "https://b"})        # shared while processing lingers
    assert [i["url"] for i in rotate_queue(q)] == ["https://a"]   # resumes same batch
    finish_queue(q)
    assert [i["url"] for i in rotate_queue(q)] == ["https://b"]   # pending picked up
    finish_queue(q)

def test_malformed_line_is_logged(tmp_path, caplog):
    q = tmp_path / "queue.jsonl"
    q.write_text('not-json\n{"url": "https://a"}\n')
    with caplog.at_level(logging.WARNING, logger="yt-obsidian"):
        assert [i["url"] for i in rotate_queue(q)] == ["https://a"]
    assert any("malformed queue line" in r.getMessage() for r in caplog.records)
    finish_queue(q)

def test_read_items_uses_utf8_regardless_of_locale(tmp_path):
    q = tmp_path / "queue.jsonl"
    append_item(q, {"url": "https://a", "text": "café ☕"})
    assert [i["text"] for i in rotate_queue(q)] == ["café ☕"]
    finish_queue(q)
    # The assertion above passes even without the explicit encoding, because this
    # machine's locale is already UTF-8. Drain the same bytes again from a process
    # whose locale encoding really is ASCII: the reader must not follow it.
    append_item(q, {"url": "https://a", "text": "café ☕"})
    repo = Path(__file__).resolve().parents[1]
    env = dict(os.environ, LC_ALL="C", LANG="C", PYTHONUTF8="0", PYTHONPATH=str(repo))
    env.pop("PYTHONIOENCODING", None)
    code = ("import json, sys; from src.ingest import rotate_queue;"
            "sys.stdout.write(json.dumps([i['text'] for i in rotate_queue(sys.argv[1])]))")
    out = subprocess.run([sys.executable, "-X", "utf8=0", "-c", code, str(q)],
                         cwd=repo, env=env, capture_output=True, text=True)
    # not check=True: that reports only "non-zero exit status 1" and swallows the
    # child's traceback, which is the only clue to why the read failed
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == ["café ☕"]
    finish_queue(q)

@pytest.mark.parametrize("sep", [chr(0x2028), chr(0x2029), chr(0x0085)])
def test_unicode_line_separators_do_not_split_an_item(tmp_path, sep):
    # json.dumps(ensure_ascii=False) leaves these unescaped — they are not C0
    # controls — but str.splitlines() breaks on all three. They arrive routinely in
    # text copied out of publishing tools, so one share would become two half-lines.
    q = tmp_path / "queue.jsonl"
    append_item(q, {"url": "https://a", "text": f"before{sep}after"})
    append_item(q, {"url": "https://b"})
    items = rotate_queue(q)
    assert [i["url"] for i in items] == ["https://a", "https://b"]
    assert items[0]["text"] == f"before{sep}after"
    finish_queue(q)

def test_rotate_survives_invalid_utf8(tmp_path, caplog):
    q = tmp_path / "queue.jsonl"
    q.write_bytes(b'{"url": "https://a"}\n'
                  b'{"url": "https://b", "text": "caf\xc3"}\n'   # truncated multi-byte
                  b'{"url": \xff"https://c"}\n'                  # bad byte breaks the JSON
                  b'{"url": "https://d"}\n')
    with caplog.at_level(logging.WARNING, logger="yt-obsidian"):
        items = rotate_queue(q)                # must not raise: a raise wedges the drain
    assert [i["url"] for i in items] == ["https://a", "https://b", "https://d"]
    assert any("malformed queue line" in r.getMessage() for r in caplog.records)
    # and the bad bytes must not poison every later window
    finish_queue(q)
    append_item(q, {"url": "https://e"})
    assert [i["url"] for i in rotate_queue(q)] == ["https://e"]
    finish_queue(q)

class _FailingWriteOS:
    """Stand-in for the os module inside src.ingest: the disk fills partway through
    the first write and stays full, so every later write fails too — the recovery
    newline included. A flag that let the second write through would only exercise
    the branch that already worked."""
    def __init__(self, prefix=12):
        self.prefix = prefix
        self.full = False
    def __getattr__(self, name):
        return getattr(os, name)
    def write(self, fd, buf):
        if not self.full:
            self.full = True
            os.write(fd, buf[:self.prefix])    # short write tears the line
        raise OSError(errno.ENOSPC, "No space left on device")

def test_failed_write_does_not_swallow_the_next_item(tmp_path, monkeypatch, caplog):
    q = tmp_path / "queue.jsonl"
    monkeypatch.setattr(ingest, "os", _FailingWriteOS())
    with pytest.raises(OSError):
        append_item(q, {"url": "https://a"})   # the sharer gets a 500 for a
    monkeypatch.undo()
    append_item(q, {"url": "https://b"})       # ...and a 200 for b, which must survive
    with caplog.at_level(logging.WARNING, logger="yt-obsidian"):
        assert [i["url"] for i in rotate_queue(q)] == ["https://b"]
    assert any("malformed queue line" in r.getMessage() for r in caplog.records)
    finish_queue(q)

class _PartialWriteOS:
    """Stand-in for the os module inside src.ingest: every write is short."""
    def __init__(self):
        self.fsynced = []
    def __getattr__(self, name):
        return getattr(os, name)
    def write(self, fd, buf):
        return os.write(fd, buf[:1])
    def fsync(self, fd):
        self.fsynced.append(fd)
        return os.fsync(fd)

def test_append_item_writes_full_line_and_fsyncs(tmp_path, monkeypatch):
    q = tmp_path / "queue.jsonl"
    fake = _PartialWriteOS()
    monkeypatch.setattr(ingest, "os", fake)
    append_item(q, {"url": "https://a"})
    append_item(q, {"url": "https://b"})
    monkeypatch.undo()
    assert [i["url"] for i in rotate_queue(q)] == ["https://a", "https://b"]
    assert fake.fsynced          # durable before append_item returned
    finish_queue(q)

def test_append_item_fsyncs_the_file_and_its_directory(tmp_path, monkeypatch):
    q = tmp_path / "queue.jsonl"
    fake = _PartialWriteOS()
    monkeypatch.setattr(ingest, "os", fake)
    append_item(q, {"url": "https://a"})     # first share of a window: new inode
    monkeypatch.undo()
    # the bytes alone are not enough — the fresh directory entry needs fsyncing too
    assert len(fake.fsynced) == 2
    finish_queue(q)

class _NoProgressWriteOS:
    """Stand-in for the os module inside src.ingest: os.write always reports zero
    bytes written."""
    def __getattr__(self, name):
        return getattr(os, name)
    def write(self, fd, buf):
        return 0

def test_write_that_never_progresses_raises_instead_of_spinning(tmp_path, monkeypatch):
    q = tmp_path / "queue.jsonl"
    monkeypatch.setattr(ingest, "os", _NoProgressWriteOS())
    with pytest.raises(OSError, match="no progress"):
        append_item(q, {"url": "https://a"})   # the sharer must get a 500, not a hang

def test_append_racing_rotate_is_not_lost(tmp_path, monkeypatch):
    q = tmp_path / "queue.jsonl"
    append_item(q, {"url": "https://a"})   # something for the drain to claim

    opened, rotated = threading.Event(), threading.Event()
    real_write_all = ingest._write_all

    def paused_write_all(fd, buf):
        # the appender now holds an fd on the live queue inode, exactly as the
        # ingest server's request thread does when the drain fires
        opened.set()
        rotated.wait(0.5)
        return real_write_all(fd, buf)

    monkeypatch.setattr(ingest, "_write_all", paused_write_all)
    appender = threading.Thread(target=append_item, args=(q, {"url": "https://b"}))
    appender.start()
    assert opened.wait(5)
    drained = [i["url"] for i in rotate_queue(q)]
    rotated.set()
    appender.join(5)
    monkeypatch.undo()
    finish_queue(q)                        # drain done: processing file removed
    remaining = [i["url"] for i in rotate_queue(q)]
    # the acknowledged append must survive, in this batch or the next
    assert sorted(drained + remaining) == ["https://a", "https://b"]
    finish_queue(q)

def test_url_key_canonicalizes():
    a = url_key("https://x.com/u/status/1?utm_source=share&s=20#frag")
    b = url_key("https://x.com/u/status/1?s=20")
    assert a == b and a.startswith("url:")
    assert url_key("https://x.com/u/status/2") != a


@pytest.mark.parametrize("tracker", [
    "si", "igsh", "igshid", "fbclid", "gclid", "mibextid",
    "ref", "ref_src", "ref_url", "share_id", "s", "t",
])
def test_url_key_strips_ios_share_sheet_trackers(tracker):
    # the iOS share sheet stamps one of these onto every share of the exact
    # same post -- if they survive, the same post never dedupes twice
    bare = url_key("https://x.com/a/1")
    tracked = url_key(f"https://x.com/a/1?{tracker}=xyz123")
    assert tracked == bare


def test_url_key_strips_trackers_case_insensitively():
    assert url_key("https://x.com/a/1?SI=abc") == url_key("https://x.com/a/1")
    assert url_key("https://x.com/a/1?UTM_Source=share") == url_key("https://x.com/a/1")


def test_url_key_dedupes_repeated_ios_shares_of_the_same_post():
    # measured in the review: these were 4 distinct keys for one Instagram reel
    shares = [
        "https://www.instagram.com/p/x/?igsh=aaa111",
        "https://www.instagram.com/p/x/?igshid=bbb222",
        "https://www.instagram.com/p/x/?fbclid=ccc333",
    ]
    keys = {url_key(u) for u in shares}
    assert len(keys) == 1

    # same post shared twice on X with different per-share tokens
    a = url_key("https://x.com/a/1?si=ABC")
    b = url_key("https://x.com/a/1?s=20&t=xyz")
    assert a == b


def test_url_key_keeps_content_identifying_params():
    # these look shaped like the tracker denylist but identify the content
    # itself -- stripping them would collide two different posts into one key
    assert (url_key("https://www.youtube.com/watch?v=aaa111")
            != url_key("https://www.youtube.com/watch?v=bbb222"))
    assert (url_key("https://instagram.com/p/x?p=1")
            != url_key("https://instagram.com/p/x?p=2"))
    assert (url_key("https://example.com/x?id=1")
            != url_key("https://example.com/x?id=2"))
    assert (url_key("https://facebook.com/story?story_fbid=1")
            != url_key("https://facebook.com/story?story_fbid=2"))


def test_url_key_keeps_s_t_ref_distinct_on_generic_urls():
    # s/t/ref are per-share tokens only on a recognized social platform.
    # classify_social_url returns "generic" for anything outside that map, and
    # on a generic host these three params are ordinary content-identifying
    # query params (forum thread ids, referral-coded article links) -- they
    # must never be stripped there, or two different pages collide into one
    # dedupe key.
    assert (url_key("http://forum.example.com/viewtopic.php?t=101")
            != url_key("http://forum.example.com/viewtopic.php?t=202"))
    assert (url_key("https://news.site/article?ref=12&s=3")
            != url_key("https://news.site/article?ref=99&s=3"))
    assert (url_key("https://news.site/article?ref=12&s=3")
            != url_key("https://news.site/article"))


def test_url_key_normalizes_param_order():
    assert (url_key("https://x.com/a?b=2&a=1")
            == url_key("https://x.com/a?a=1&b=2"))


def test_url_key_normalizes_trailing_slash():
    assert url_key("https://x.com/a/1") == url_key("https://x.com/a/1/")


def test_url_key_normalizes_leading_www():
    assert (url_key("https://www.instagram.com/p/x/")
            == url_key("https://instagram.com/p/x/"))


def test_url_key_normalization_matches_real_ios_share_sheet_repeat():
    # the review's real scenario: pasted once bare, shared again via the
    # share sheet with a tracker and both www and trailing-slash drift
    a = url_key("https://instagram.com/p/x/")
    b = url_key("https://www.instagram.com/p/x/?igsh=zzz999")
    assert a == b
