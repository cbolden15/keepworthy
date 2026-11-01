import fcntl
import json
import sqlite3
import stat
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src import config as config_mod
from src import importer, importers, ingest, state
from src.importers import Bookmark

FIXTURES = Path(__file__).parent / "fixtures" / "import"
NETSCAPE_COUNTS = {"found": 7, "new": 3, "already seen": 1, "filtered out": 0,
                   "unsupported scheme": 2, "bad rows": 1}


def _cfg(tmp_path, monkeypatch, extra=""):
    """A real Config from a tmp config.env; importer.main loads it instead of
    <project_root>/config.env."""
    env = tmp_path / "config.env"
    env.write_text(f"PLAYLIST_URL=https://x\nVAULT_DIR={tmp_path}/vault\n"
                   f"STATE_PATH={tmp_path}/state/state.json\n"
                   f"QUEUE_PATH={tmp_path}/state/queue.jsonl\n"
                   f"IMPORT_DB_PATH={tmp_path}/state/import.db\n" + extra)
    real = config_mod.load_config
    monkeypatch.setattr(importer.config_mod, "load_config",
                        lambda path, environment=None: real(env, environment=environment))
    return real(env)


def _run(capsys, *argv):
    code = importer.main([str(a) for a in argv])
    out, err = capsys.readouterr()
    return code, out, err


def _counts(out):
    pairs = (line.split(": ", 1) for line in out.splitlines() if ": " in line)
    return {name: int(value) for name, value in pairs if name in importer.COUNT_NAMES}


def _count(cfg, table, where="1=1"):
    with closing(sqlite3.connect(cfg.import_db_path)) as db:
        return db.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0]


def _payloads(cfg):
    with closing(sqlite3.connect(cfg.import_db_path)) as db:
        return {json.loads(p)["url"]: json.loads(p)
                for (p,) in db.execute("SELECT payload FROM bookmarks")}


def _write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text)
    return path


def _bookmark(**over):
    fields = dict(url="https://example.com/a", title="A", folder="", tags=(), note="",
                  saved_at="2024-03-09T12:34:56+00:00", source="pocket")
    fields.update(over)
    return Bookmark(**fields)


def test_import_inserts_and_reports_counts(tmp_path, monkeypatch, capsys):
    cfg = _cfg(tmp_path, monkeypatch)
    code, out, err = _run(capsys, "import", FIXTURES / "bookmarks.html")
    assert (code, err) == (0, "")
    assert out == ("found: 7\nnew: 3\nalready seen: 1\nfiltered out: 0\n"
                   "unsupported scheme: 2\nbad rows: 1\nimport id: 1\n")
    assert _count(cfg, "bookmarks", "enqueued_at IS NULL") == 3
    with closing(sqlite3.connect(cfg.import_db_path)) as db:
        row = db.execute("SELECT file_name, format, counts, status FROM imports").fetchone()
    assert row[0] == "bookmarks.html" and row[1] == "netscape" and row[3] == "active"
    assert json.loads(row[2]) == NETSCAPE_COUNTS
    item = _payloads(cfg)["https://example.org/k8s"]
    assert set(item) == {"url", "why", "text", "received_at", "capture_source", "import_title",
                         "import_folder", "import_tags", "import_saved_at", "import_source"}
    assert item["capture_source"] == "import" and item["text"] is None
    assert item["import_title"] == "Cluster notes & tips"
    assert item["import_folder"] == "Reading/Infra"
    assert item["import_tags"] == ["python", "async"]
    assert item["import_saved_at"] == "2024-03-09T16:00:00+00:00"
    assert item["import_source"] == "netscape"
    assert item["why"] == "Imported from browser bookmarks. Folder: Reading/Infra. Tags: python, async."
    assert datetime.fromisoformat(item["received_at"]).tzinfo == timezone.utc


def test_dry_run_writes_nothing_and_matches_real_counts(tmp_path, monkeypatch, capsys):
    cfg = _cfg(tmp_path, monkeypatch)
    code, out, _err = _run(capsys, "import", FIXTURES / "bookmarks.html", "--dry-run")
    assert code == 0 and _counts(out) == NETSCAPE_COUNTS
    assert "import id" not in out
    assert not cfg.import_db_path.exists()
    code, out, _err = _run(capsys, "import", FIXTURES / "bookmarks.html")
    assert code == 0 and _counts(out) == NETSCAPE_COUNTS


def test_repeat_file_inserts_nothing_and_names_date_and_id(tmp_path, monkeypatch, capsys):
    cfg = _cfg(tmp_path, monkeypatch)
    assert _run(capsys, "import", FIXTURES / "pocket.csv")[0] == 0
    today = datetime.now(timezone.utc).date().isoformat()
    expected = f"this file was imported on {today} (import 1)\n"
    for extra in ([], ["--dry-run"]):
        code, out, _err = _run(capsys, "import", FIXTURES / "pocket.csv", *extra)
        assert (code, out) == (0, expected)
    assert _count(cfg, "imports") == 1 and _count(cfg, "bookmarks") == 4


def test_repeat_of_active_import_still_inserts_nothing(tmp_path, monkeypatch, capsys):
    cfg = _cfg(tmp_path, monkeypatch)
    assert _run(capsys, "import", FIXTURES / "bookmarks.html", "--folder", "Reading/Infra")[0] == 0
    before = (_count(cfg, "imports"), _count(cfg, "bookmarks"))
    code, out, _err = _run(capsys, "import", FIXTURES / "bookmarks.html", "--folder", "Reading")
    assert code == 0 and out.startswith("this file was imported on ")
    assert (_count(cfg, "imports"), _count(cfg, "bookmarks")) == before == (1, 1)


def test_dedupe_against_ledger_and_state_seen(tmp_path, monkeypatch, capsys):
    cfg = _cfg(tmp_path, monkeypatch)
    state.mark_seen(cfg.state_path, ingest.url_key("https://example.com/async"))
    code, out, _err = _run(capsys, "import", FIXTURES / "pocket.csv")
    assert code == 0
    assert (_counts(out)["new"], _counts(out)["already seen"]) == (3, 1)
    second = _write(tmp_path, "more.html",
                    '<DL><p><DT><A HREF="https://example.org/bread">Bread</A>'
                    '<DT><A HREF="https://example.com/fresh">Fresh</A></DL>')
    code, out, _err = _run(capsys, "import", second)
    assert code == 0
    assert (_counts(out)["new"], _counts(out)["already seen"]) == (1, 1)
    assert _count(cfg, "bookmarks") == 4


def test_dedupe_within_file_first_wins(tmp_path, monkeypatch, capsys):
    cfg = _cfg(tmp_path, monkeypatch)
    assert _run(capsys, "import", FIXTURES / "bookmarks.html")[0] == 0
    essay = _payloads(cfg)["https://example.com/essay"]
    assert (essay["import_title"], essay["import_folder"]) == ("An essay", "Reading")


def test_filter_folder_prefix_case_insensitive(tmp_path, monkeypatch, capsys):
    cfg = _cfg(tmp_path, monkeypatch)
    code, out, _err = _run(capsys, "import", FIXTURES / "bookmarks.html", "--dry-run",
                           "--folder", "reading")
    assert code == 0
    assert _counts(out) == {**NETSCAPE_COUNTS, "new": 2, "already seen": 0, "filtered out": 2}
    code, out, _err = _run(capsys, "import", FIXTURES / "bookmarks.html", "--folder", "/READING/infra/")
    assert code == 0 and _counts(out)["new"] == 1
    assert list(_payloads(cfg)) == ["https://example.org/k8s"]


def test_filter_tag_any_of(tmp_path, monkeypatch, capsys):
    cfg = _cfg(tmp_path, monkeypatch)
    code, out, _err = _run(capsys, "import", FIXTURES / "bookmarks.html",
                           "--tag", "PYTHON", "--tag", "misc")
    assert code == 0 and _counts(out)["filtered out"] == 2
    assert sorted(_payloads(cfg)) == ["https://example.com/root", "https://example.org/k8s"]


def test_filter_since_drops_undated(tmp_path, monkeypatch, capsys):
    cfg = _cfg(tmp_path, monkeypatch)
    code, out, _err = _run(capsys, "import", FIXTURES / "pocket.csv", "--since", "2020-01-01")
    assert code == 0
    assert (_counts(out)["new"], _counts(out)["filtered out"]) == (3, 1)
    assert "https://example.org/undated" not in _payloads(cfg)
    kept, dropped = importer.select([_bookmark(saved_at="2024-03-08T23:59:59+00:00"),
                                     _bookmark(saved_at="2024-03-09T00:00:00+00:00")],
                                    since="2024-03-09")
    assert (len(kept), dropped) == (1, 1)


@pytest.mark.parametrize("value", ["2024-3-9", "yesterday", "2024-02-30", "20240309"])
def test_since_bad_value_exit_2(tmp_path, monkeypatch, capsys, value):
    cfg = _cfg(tmp_path, monkeypatch)
    code, _out, err = _run(capsys, "import", FIXTURES / "pocket.csv", "--since", value)
    assert code == 2 and "--since" in err
    assert not cfg.import_db_path.exists()


def test_why_default_pocket():
    bookmark = _bookmark(tags=("python", "async"))
    assert importer.build_why(bookmark) == "Imported from Pocket. Tags: python, async."


def test_why_default_netscape_with_folder_and_tags():
    bookmark = _bookmark(source="netscape", folder="Reading/Infra")
    assert importer.build_why(bookmark) == "Imported from browser bookmarks. Folder: Reading/Infra."
    bookmark = _bookmark(source="netscape", folder="Reading", tags=("a", "b"))
    assert importer.build_why(bookmark) == "Imported from browser bookmarks. Folder: Reading. Tags: a, b."


def test_why_raindrop_note_wins():
    bookmark = _bookmark(source="raindrop", folder="Papers", tags=("ml",), note="Read before the Q3 review")
    assert importer.build_why(bookmark) == "Read before the Q3 review"
    assert importer.build_why(bookmark, "Q3:") == "Q3: Read before the Q3 review"


def test_why_prefix_then_cut_to_280(tmp_path, monkeypatch, capsys):
    bookmark = _bookmark(source="raindrop", folder="Papers")
    assert importer.build_why(bookmark, "Q3 research:") == \
        "Q3 research: Imported from Raindrop. Folder: Papers."
    long_why = importer.build_why(bookmark, "x" * 275)
    assert len(long_why) == 280 and long_why.startswith("x" * 275 + " Impo")
    cfg = _cfg(tmp_path, monkeypatch)
    assert _run(capsys, "import", FIXTURES / "raindrop.csv", "--why", "Q3 research:")[0] == 0
    assert _payloads(cfg)["https://example.org/highlighted"]["why"] == \
        "Q3 research: Imported from Raindrop. Folder: Papers."
    assert _payloads(cfg)["https://example.com/paper"]["why"] == \
        "Q3 research: First line of a note second line"


@pytest.mark.parametrize("value", ["line one\nline two", "y" * 281])
def test_why_prefix_multiline_exit_2(tmp_path, monkeypatch, capsys, value):
    cfg = _cfg(tmp_path, monkeypatch)
    code, _out, err = _run(capsys, "import", FIXTURES / "pocket.csv", "--why", value)
    assert code == 2 and err.startswith("--why: ")
    assert not cfg.import_db_path.exists()


def _never(*args, **kwargs):
    raise AssertionError("must not be called")


def test_file_cap_exit_2_before_read(tmp_path, monkeypatch, capsys):
    cfg = _cfg(tmp_path, monkeypatch)
    big = tmp_path / "huge.html"
    with big.open("wb") as handle:
        handle.truncate(54_840_000)          # sparse: 52.3 MB on paper, nothing on disk
    monkeypatch.setattr(importer, "file_sha256", _never)
    monkeypatch.setattr(importers, "detect", _never)
    code, out, err = _run(capsys, "import", big)
    assert (code, out, err) == (2, "", "file is 52.3 MB; the limit is 50 MB\n")
    assert not cfg.import_db_path.exists()


def test_bookmark_cap_exit_2_nothing_written(tmp_path, monkeypatch, capsys):
    cfg = _cfg(tmp_path, monkeypatch)
    monkeypatch.setattr(importers, "MAX_BOOKMARKS", 3)
    code, out, err = _run(capsys, "import", FIXTURES / "bookmarks.html")
    assert (code, out, err) == (2, "", "file has more than 3 bookmarks; the limit is 3\n")
    assert _count(cfg, "imports") == 0 and _count(cfg, "bookmarks") == 0


def test_no_format_exit_2_names_formats(tmp_path, monkeypatch, capsys):
    _cfg(tmp_path, monkeypatch)
    code, _out, err = _run(capsys, "import", FIXTURES / "not-a-bookmark-file.txt")
    assert code == 2
    assert err == ("could not recognize this file as netscape, pocket or raindrop; "
                   "pass --format\n")


def test_format_override(tmp_path, monkeypatch, capsys):
    cfg = _cfg(tmp_path, monkeypatch)
    bare = _write(tmp_path, "anchors.txt", '<a href="https://example.com/bare">Bare</a>\n')
    assert _run(capsys, "import", bare, "--dry-run")[0] == 2
    code, out, _err = _run(capsys, "import", bare, "--format", "netscape")
    assert code == 0 and _counts(out)["new"] == 1
    with closing(sqlite3.connect(cfg.import_db_path)) as db:
        assert db.execute("SELECT format FROM imports").fetchone() == ("netscape",)
    code, _out, err = _run(capsys, "import", FIXTURES / "pocket.csv", "--format", "raindrop")
    assert code == 2 and err == "Raindrop export is missing column created\n"


def test_lock_held_exit_1(tmp_path, monkeypatch, capsys):
    cfg = _cfg(tmp_path, monkeypatch)
    lock = Path(f"{cfg.import_db_path}.lock")
    lock.parent.mkdir(parents=True)
    with lock.open("a") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX)
        code, out, err = _run(capsys, "import", FIXTURES / "pocket.csv")
        assert (code, out) == (1, "")
        assert err == f"another import is running (lock {lock})\n"
        assert _run(capsys, "import", FIXTURES / "pocket.csv", "--dry-run")[0] == 0
    assert not cfg.import_db_path.exists()


def test_ledger_mode_0600_and_private_file_refuses_0644(tmp_path, monkeypatch, capsys):
    cfg = _cfg(tmp_path, monkeypatch)
    assert _run(capsys, "import", FIXTURES / "pocket.csv")[0] == 0
    assert stat.S_IMODE(cfg.import_db_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(cfg.import_db_path.parent.stat().st_mode) == 0o700
    cfg.import_db_path.chmod(0o644)
    with pytest.raises(ValueError):
        importer.open_ledger(cfg.import_db_path)
    code, _out, err = _run(capsys, "import", FIXTURES / "raindrop.csv")
    assert code == 1 and err.startswith("ValueError: ")


def test_exit_codes(tmp_path, monkeypatch, capsys):
    _cfg(tmp_path, monkeypatch)
    assert _run(capsys, "import", FIXTURES / "pocket.csv")[0] == 0
    code, _out, err = _run(capsys, "import", tmp_path / "missing.html")
    assert code == 2 and err == "the bookmark file does not exist\n"

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(importer, "record_import", locked)
    code, _out, err = _run(capsys, "import", FIXTURES / "raindrop.csv")
    assert code == 1 and err == "OperationalError: database is locked\n"
    assert _run(capsys, "import")[0] == 2                    # argparse usage error
    assert _run(capsys, "frobnicate")[0] == 2
