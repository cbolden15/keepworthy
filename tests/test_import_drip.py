import json
import logging
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone

import pytest

from src import importer, ingest, poll, social
from src.importers import Bookmark
from tests.test_social import _LONG_TEXT, _art, _cfg as _social_cfg, _deps

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


def _bookmark(n, saved_at):
    return Bookmark(url=f"https://example.com/{n}", title=f"Item {n}", folder="Inbox",
                    tags=("t",), note="", saved_at=saved_at, source="pocket")


def _seed(db, bookmarks):
    rows = [(ingest.url_key(b.url),
             importer.queue_item(b, why=importer.build_why(b), received_at=NOW.isoformat()))
            for b in bookmarks]
    return importer.record_import(db, file_name="seed.csv", sha256=f"sha-{len(rows)}-{rows[0][0]}",
                                  fmt="pocket", counts={}, rows=rows, now=NOW)


def _ledger(tmp_path):
    return closing(importer.open_ledger(tmp_path / "state" / "import.db"))


def _queued(queue_path):
    return [item["url"] for item in ingest._read_items(queue_path)] if queue_path.exists() else []


def test_drip_newest_first_undated_last(tmp_path):
    queue = tmp_path / "queue.jsonl"
    with _ledger(tmp_path) as db:
        _seed(db, [_bookmark("old", "2020-01-01T00:00:00+00:00"),
                   _bookmark("undated", ""),
                   _bookmark("new", "2024-03-09T12:34:56+00:00"),
                   _bookmark("mid", "2022-06-01T00:00:00+00:00")])
        stats = importer.drip(db, queue, 20, now=NOW)
    assert stats == {"enqueued": 4, "sent_today": 4, "waiting": 0, "limit": 20}
    assert _queued(queue) == [f"https://example.com/{n}" for n in ("new", "mid", "old", "undated")]


def test_drip_daily_limit_across_utc_midnight(tmp_path):
    queue = tmp_path / "queue.jsonl"
    evening = datetime(2026, 11, 1, 23, 50, tzinfo=timezone.utc)
    with _ledger(tmp_path) as db:
        _seed(db, [_bookmark(n, f"2024-01-01T00:00:{n:02d}+00:00") for n in range(50)])
        assert importer.drip(db, queue, 20, now=evening)["enqueued"] == 20
        assert importer.drip(db, queue, 20, now=evening + timedelta(minutes=9))["enqueued"] == 0
        after_midnight = importer.drip(db, queue, 20, now=evening + timedelta(minutes=11))
    assert after_midnight == {"enqueued": 20, "sent_today": 20, "waiting": 10, "limit": 20}
    assert len(_queued(queue)) == 40


def test_drip_subtracts_sent_today(tmp_path):
    queue = tmp_path / "queue.jsonl"
    with _ledger(tmp_path) as db:
        _seed(db, [_bookmark(n, "2024-01-01T00:00:00+00:00") for n in range(30)])
        with db:
            db.execute("UPDATE bookmarks SET enqueued_at=? WHERE key IN "
                       "(SELECT key FROM bookmarks ORDER BY key LIMIT 12)",
                       ((NOW - timedelta(hours=3)).isoformat(),))
        stats = importer.drip(db, queue, 20, now=NOW)
    assert stats == {"enqueued": 8, "sent_today": 20, "waiting": 10, "limit": 20}
    assert len(_queued(queue)) == 8


def test_drip_limit_zero_enqueues_nothing(tmp_path):
    queue = tmp_path / "queue.jsonl"
    with _ledger(tmp_path) as db:
        _seed(db, [_bookmark(n, "") for n in range(3)])
        stats = importer.drip(db, queue, 0, now=NOW)
    assert stats == {"enqueued": 0, "sent_today": 0, "waiting": 3, "limit": 0}
    assert not queue.exists()


class _StampFails:
    """The ledger connection, except that the UPDATE after the append raises:
    a crash between the queue append and the stamp."""

    def __init__(self, db):
        self.db = db

    def execute(self, sql, *args):
        if sql.startswith("UPDATE"):
            raise sqlite3.OperationalError("disk I/O error")
        return self.db.execute(sql, *args)

    def __enter__(self):
        return self.db.__enter__()

    def __exit__(self, *exc):
        return self.db.__exit__(*exc)


def test_drip_replay_after_crash_between_append_and_stamp(tmp_path):
    cfg = _social_cfg(tmp_path)
    url = "https://example.com/replayed"
    with _ledger(tmp_path) as db:
        _seed(db, [Bookmark(url=url, title="R", folder="", tags=(), note="",
                            saved_at="2024-01-01T00:00:00+00:00", source="pocket")])
        with pytest.raises(sqlite3.OperationalError):
            importer.drip(_StampFails(db), cfg.queue_path, 20, now=NOW)
        assert importer.drip(db, cfg.queue_path, 20, now=NOW)["enqueued"] == 1
    assert _queued(cfg.queue_path) == [url, url]
    results = social.drain_social_queue(cfg, tmp_path, "2026-11-01",
                                        deps=_deps(fetch_article=lambda u, **kw: _art(_LONG_TEXT)))
    assert results == [(ingest.url_key(url), "summarized"), (ingest.url_key(url), "duplicate")]


def test_drip_queue_item_fields(tmp_path):
    queue = tmp_path / "queue.jsonl"
    bookmark = Bookmark(url="https://example.org/fields", title="Fields", folder="Reading/Infra",
                        tags=("python", "async"), note="", saved_at="2024-03-09T12:34:56+00:00",
                        source="netscape")
    with _ledger(tmp_path) as db:
        _seed(db, [bookmark])
        importer.drip(db, queue, 20, now=NOW)
    [line] = queue.read_text().splitlines()
    assert json.loads(line) == {
        "url": "https://example.org/fields",
        "why": "Imported from browser bookmarks. Folder: Reading/Infra. Tags: python, async.",
        "text": None, "received_at": NOW.isoformat(), "capture_source": "import",
        "import_title": "Fields", "import_folder": "Reading/Infra",
        "import_tags": ["python", "async"], "import_saved_at": "2024-03-09T12:34:56+00:00",
        "import_source": "netscape"}


def _main_config(monkeypatch, tmp_path, extra=""):
    env = tmp_path / "config.env"
    env.write_text(f"PLAYLIST_URL=https://x\nVAULT_DIR={tmp_path}/vault\n"
                   f"STATE_PATH={tmp_path}/state/state.json\n"
                   f"QUEUE_PATH={tmp_path}/state/queue.jsonl\n"
                   f"IMPORT_DB_PATH={tmp_path}/state/import.db\n" + extra)
    real = poll.config_mod.load_config
    monkeypatch.setattr(poll.config_mod, "load_config",
                        lambda path, environment=None: real(env, environment=environment))
    return real(env)


def test_poll_main_drips_between_youtube_and_social(monkeypatch, tmp_path, caplog):
    cfg = _main_config(monkeypatch, tmp_path)
    with _ledger(tmp_path) as db:
        _seed(db, [_bookmark(n, "") for n in range(3)])
    order = []
    monkeypatch.setattr(poll, "run", lambda c, root, processed, deps: order.append("youtube") or [])
    monkeypatch.setattr(social, "drain_social_queue",
                        lambda c, root, processed: order.append(_queued(cfg.queue_path)) or [])
    with caplog.at_level(logging.INFO, logger="yt-obsidian"):
        poll.main()
    assert order[0] == "youtube" and len(order[1]) == 3
    assert "import drip: enqueued 3 (sent today 3, waiting 0, limit 20)" in caplog.messages


def test_poll_main_closes_the_ledger_after_the_drip(monkeypatch, tmp_path):
    _main_config(monkeypatch, tmp_path)
    importer.open_ledger(tmp_path / "state" / "import.db").close()
    held = []
    real_drip = importer.drip

    def drip(db, *args, **kwargs):
        held.append(db)
        return real_drip(db, *args, **kwargs)
    monkeypatch.setattr(importer, "drip", drip)
    monkeypatch.setattr(poll, "run", lambda c, root, processed, deps: [])
    monkeypatch.setattr(social, "drain_social_queue", lambda c, root, processed: [])
    poll.main()
    [db] = held
    with pytest.raises(sqlite3.ProgrammingError):
        db.execute("SELECT 1")


def test_poll_main_drip_failure_does_not_block_social_drain(monkeypatch, tmp_path, caplog):
    _main_config(monkeypatch, tmp_path)
    importer.open_ledger(tmp_path / "state" / "import.db").close()

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")
    calls = []
    monkeypatch.setattr(importer, "drip", boom)
    monkeypatch.setattr(poll, "run", lambda c, root, processed, deps: calls.append("youtube") or [])
    monkeypatch.setattr(social, "drain_social_queue",
                        lambda c, root, processed: calls.append("social") or [])
    with caplog.at_level(logging.INFO, logger="yt-obsidian"):
        poll.main()
    assert calls == ["youtube", "social"]
    assert "import drip failed: OperationalError" in caplog.messages


@pytest.mark.parametrize("extra,make_ledger", [("", False), ("IMPORT_DAILY_LIMIT=0\n", True)])
def test_poll_main_skips_drip_without_ledger_or_with_limit_zero(monkeypatch, tmp_path, caplog,
                                                              extra, make_ledger):
    cfg = _main_config(monkeypatch, tmp_path, extra)
    if make_ledger:
        importer.open_ledger(cfg.import_db_path).close()

    def never(*args, **kwargs):
        raise AssertionError("the drip must not run")
    monkeypatch.setattr(importer, "drip", never)
    monkeypatch.setattr(poll, "run", lambda c, root, processed, deps: [])
    monkeypatch.setattr(social, "drain_social_queue", lambda c, root, processed: [])
    with caplog.at_level(logging.INFO, logger="yt-obsidian"):
        poll.main()
    assert not any("import drip" in m for m in caplog.messages)
    assert cfg.import_db_path.exists() is make_ledger
