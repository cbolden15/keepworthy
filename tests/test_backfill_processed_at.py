"""Tests for the one-time processed_at backfill.

This script edits notes in the user's real vault, so every safety property it
claims is pinned here: dry-run by default, idempotent, never rewrites an
existing processed_at, never touches processed, never disturbs the body or the
rest of the frontmatter, and leaves a note it cannot handle exactly alone.

Every test operates on a pytest tmp_path; nothing here touches a real vault.
"""
from pathlib import Path

import pytest

from backfill_processed_at import backfilled, main, run

NOTE = """---
title: "A video"
published: 2026-02-22
processed: 2026-07-21
duration: "00:24:16"
tags: [youtube, health]
status: summarized
---

## TL;DR
Body text stays put.
"""


def _write(dirpath: Path, name: str, text: str) -> Path:
    p = dirpath / name
    p.write_text(text)
    return p


def test_inserts_processed_at_directly_after_processed():
    out = backfilled(NOTE)
    lines = out.splitlines()
    i = lines.index("processed: 2026-07-21")
    assert lines[i + 1] == "processed_at: 2026-07-21T00:00:00"


def test_leaves_everything_else_byte_identical():
    out = backfilled(NOTE)
    assert out.replace("processed_at: 2026-07-21T00:00:00\n", "") == NOTE


def test_is_idempotent():
    once = backfilled(NOTE)
    assert backfilled(once) is None


def test_never_rewrites_an_existing_processed_at():
    already = NOTE.replace("processed: 2026-07-21\n",
                           "processed: 2026-07-21\nprocessed_at: 2026-07-21T09:15:30\n")
    assert backfilled(already) is None


@pytest.mark.parametrize("text", [
    "no frontmatter at all\n",
    "---\ntitle: unterminated frontmatter\nprocessed: 2026-07-21\n",
    "---\ntitle: no processed key\n---\n\nbody\n",
    "---\nprocessed: undated\n---\n\nbody\n",
    "---\nprocessed: 2026-07-21T09:00:00\n---\n\nbody\n",
])
def test_leaves_notes_it_cannot_handle_alone(text):
    # a malformed or already-datetime `processed:` is a hand-edited note; the
    # backfill declines rather than guessing at it
    assert backfilled(text) is None


def test_a_processed_line_in_the_body_is_not_mistaken_for_frontmatter():
    note = "---\ntitle: x\n---\n\nprocessed: 2026-07-21\n"
    assert backfilled(note) is None


def test_preserves_crlf_line_endings():
    out = backfilled(NOTE.replace("\n", "\r\n"))
    assert "processed_at: 2026-07-21T00:00:00\r\n" in out
    assert "\n" not in out.replace("\r\n", "")


def test_dry_run_writes_nothing(tmp_path, capsys):
    p = _write(tmp_path, "note.md", NOTE)
    changed, skipped, failed = run(tmp_path, apply=False)
    assert (changed, skipped, failed) == (1, 0, 0)
    assert p.read_text() == NOTE


def test_apply_writes_and_leaves_no_tmp_files(tmp_path):
    p = _write(tmp_path, "note.md", NOTE)
    run(tmp_path, apply=True)
    assert "processed_at: 2026-07-21T00:00:00" in p.read_text()
    assert list(tmp_path.glob("*.tmp")) == []


def test_walks_nested_topic_folders(tmp_path):
    nested = tmp_path / "Health & Fitness" / "General"
    nested.mkdir(parents=True)
    p = _write(nested, "note.md", NOTE)
    run(tmp_path, apply=True)
    assert "processed_at:" in p.read_text()


def test_a_second_apply_changes_nothing(tmp_path):
    p = _write(tmp_path, "note.md", NOTE)
    run(tmp_path, apply=True)
    after_first = p.read_text()
    changed, _, _ = run(tmp_path, apply=True)
    assert changed == 0
    assert p.read_text() == after_first


def test_cli_defaults_to_dry_run(tmp_path):
    p = _write(tmp_path, "note.md", NOTE)
    assert main(["--vault", str(tmp_path)]) == 0
    assert p.read_text() == NOTE


def test_cli_apply_writes(tmp_path):
    p = _write(tmp_path, "note.md", NOTE)
    assert main(["--vault", str(tmp_path), "--apply"]) == 0
    assert "processed_at:" in p.read_text()


def test_cli_rejects_a_missing_vault(tmp_path):
    assert main(["--vault", str(tmp_path / "nope")]) == 2
