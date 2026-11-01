import types
"""Tests for the one-time migration of the accumulated backlog into the
saved-content tree.

This is the only script in the project allowed to move files inside the user's
real vault, and it runs against months of accumulated notes synced by Syncthing
across three devices. Every safety property it claims is pinned here: dry-run by
default, backup before the first move, never overwrite, never delete a note,
never restamp `processed:`, never sweep the hand-placed material in a brain's
raw/articles, idempotent, safe on partial failure, and classification failure
files to Other/ rather than losing the note.

Every test operates on a pytest tmp_path vault; nothing here touches a real one.
"""
import json
import subprocess
import tarfile
from pathlib import Path

import pytest

import migrate_notes
from migrate_notes import (
    _majority_classification,
    _source_dirs,
    apply_migration,
    ensure_platform_frontmatter,
    ensure_topic_frontmatter,
    main,
    normalize_classification,
    plan_migration,
)
from src.config import allowed_topics, load_config
from src.note import GENERAL, build_note, is_safe_path_component

PROMPTS_DIR = Path(migrate_notes.__file__).resolve().parent / "prompts"

CONTENT_ROOT = "saved-content"
PROCESSED = "2026-07-24"


def _cfg(tmp_path):
    env = tmp_path / "config.env"
    env.write_text(f"PLAYLIST_URL=https://x\nVAULT_DIR={tmp_path}/vault\n")
    return load_config(env)


def _project(tmp_path):
    """A throwaway project root main() can load: config.env + the real prompt."""
    _cfg(tmp_path)
    prompts = tmp_path / "prompts"
    prompts.mkdir(exist_ok=True)
    (prompts / "classify.md").write_text((PROMPTS_DIR / "classify.md").read_text())
    return tmp_path


def _mknote(vault, name, topic_line=None, folder="YouTube Summaries", title=None,
            processed=PROCESSED):
    d = vault / folder
    d.mkdir(parents=True, exist_ok=True)
    fm = ["---", f'title: "{title if title is not None else Path(name).stem}"',
          "category: ai-dev", f"processed: {processed}"]
    if topic_line:
        fm.append(topic_line)
    fm += ["status: summarized", "---", "", "body"]
    (d / name).write_text("\n".join(fm))
    return d / name


def _dest(vault, folder, title, processed=PROCESSED):
    """Where a migrated note lands: date-prefixed, readable, source-suffixed."""
    return vault / CONTENT_ROOT / folder / f"{processed} {title} (YouTube).md"


def _reply(topic, subtopic="General"):
    """A reply shaped the way the two-line prompt contract asks for."""
    return f"topic: {topic}\nsubtopic: {subtopic}\n"


class _FakeRunner:
    """Stands in for subprocess.run so no test ever spawns the claude CLI."""

    def __init__(self, stdout="AI & Dev\n", returncode=0):
        self.stdout = stdout
        self.returncode = returncode
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, self.returncode,
                                         json.dumps({"ok": True, "output": self.stdout}) if cmd[0] == "/usr/bin/sudo" else self.stdout, "")


class _SequenceRunner:
    """Like _FakeRunner, but a different reply on each call -- for driving
    the majority-of-three vote through the real classify() closure built by
    _dry_run, rather than through plan_migration's classify hook directly."""

    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        reply = next(self.replies)
        return subprocess.CompletedProcess(cmd, 0,
                                         json.dumps({"ok": True, "output": reply}) if cmd[0] == "/usr/bin/sudo" else reply, "")


# --- planning -------------------------------------------------------------

def test_plan_migration_classifies_even_a_note_that_already_has_a_topic(tmp_path):
    """No existing-topic shortcut. Every note in the backlog was classified
    before `Career` existed and before subtopics existed at all, so reusing a
    note's own `topic:` would file the whole vault under stale topics with no
    subtopic while reporting success."""
    cfg = _cfg(tmp_path)
    _mknote(Path(cfg.vault_dir), "a.md")
    _mknote(Path(cfg.vault_dir), "b.md", topic_line='topic: "Finance"')

    plan = plan_migration(cfg, tmp_path,
                          classify=lambda text: _reply("AI & Dev", "Claude Code"))

    assert [(p.name, t, s) for p, t, s in plan] == [
        ("a.md", "AI & Dev", "Claude Code"),
        ("b.md", "AI & Dev", "Claude Code"),  # b's stale Finance is not trusted
    ]


def test_plan_migration_invalid_topic_falls_back(tmp_path):
    cfg = _cfg(tmp_path)
    _mknote(Path(cfg.vault_dir), "a.md")
    plan = plan_migration(cfg, tmp_path, classify=lambda text: "Bogus Topic")
    assert plan[0][1:] == ("Other", GENERAL)


def test_plan_migration_unlisted_subtopic_falls_back_to_general(tmp_path):
    """A note in the right topic under General is recoverable by hand; one in
    a folder its own topic does not have is not."""
    cfg = _cfg(tmp_path)
    _mknote(Path(cfg.vault_dir), "a.md")
    plan = plan_migration(cfg, tmp_path,
                          classify=lambda text: _reply("AI & Dev", "Woodworking"))
    assert plan[0][1:] == ("AI & Dev", GENERAL)


def test_plan_migration_survives_classifier_error(tmp_path):
    """A classifier blowing up must not abort the run or drop the note."""
    cfg = _cfg(tmp_path)
    _mknote(Path(cfg.vault_dir), "a.md")
    _mknote(Path(cfg.vault_dir), "b.md")

    def boom(text):
        raise RuntimeError("claude failed (exit 1)")

    plan = plan_migration(cfg, tmp_path, classify=boom)
    assert [(p.name, t, s) for p, t, s in plan] == [
        ("a.md", "Other", GENERAL), ("b.md", "Other", GENERAL)]


def test_plan_migration_is_empty_when_folder_is_gone(tmp_path):
    """Second run over an already-migrated vault: nothing to do."""
    cfg = _cfg(tmp_path)
    (Path(cfg.vault_dir) / "AI & Dev").mkdir(parents=True)
    assert plan_migration(cfg, tmp_path, classify=lambda text: "AI & Dev") == []


# --- routing: legacy backlog, root topic folders, brain transcripts --------

def test_main_plans_a_legacy_note_into_the_content_tree(tmp_path, capsys):
    """A note from the original YouTube Summaries/ backlog, classified
    AI & Dev, is planned into saved-content/AI & Dev/<subtopic>/ -- not a
    flat topic folder and not a brain."""
    root = _project(tmp_path)
    cfg = load_config(root / "config.env")
    _mknote(Path(cfg.vault_dir), "a.md")
    backups = tmp_path / "backups"
    backups.mkdir()
    runner = _FakeRunner(_reply("AI & Dev", "Claude Code"))

    rc = main([], project_root=root, backup_dir=backups, runner=runner)

    out = capsys.readouterr().out
    assert rc == 0
    assert "a.md -> saved-content/AI & Dev/Claude Code/" in out


def test_main_reclassifies_a_root_topic_folder_note_instead_of_trusting_it(tmp_path, capsys):
    """A note already sitting in a root AI & Dev/ folder carries a recognised
    `topic:` written before subtopics existed. It is classified anyway -- the
    old shortcut would have filed it under its stale topic with no subtopic
    while reporting success."""
    root = _project(tmp_path)
    cfg = load_config(root / "config.env")
    _mknote(Path(cfg.vault_dir), "a.md", topic_line='topic: "AI & Dev"', folder="AI & Dev")
    backups = tmp_path / "backups"
    backups.mkdir()
    runner = _FakeRunner(_reply("Finance"))

    rc = main([], project_root=root, backup_dir=backups, runner=runner)

    out = capsys.readouterr().out
    assert rc == 0
    assert "a.md -> saved-content/Finance/" in out
    assert len(runner.calls) == migrate_notes.CLASSIFY_VOTES  # it was asked


def test_plan_migration_drains_a_brain_transcripts_folder(tmp_path):
    """The smoke test wrote notes into <brain>/raw/transcripts/; they belong
    in the content tree like everything else."""
    cfg = _cfg(tmp_path)
    _mknote(Path(cfg.vault_dir), "a.md", folder="dev-brain/raw/transcripts")

    plan = plan_migration(cfg, tmp_path, classify=lambda text: _reply("Career"))

    assert [(p.name, t, s) for p, t, s in plan] == [("a.md", "Career", GENERAL)]


def test_plan_migration_never_plans_a_hand_placed_brain_article(tmp_path):
    """raw/articles holds audit reports and strategy documents that predate
    ingest entirely. Sweeping them would move the user's own documents."""
    cfg = _cfg(tmp_path)
    handwritten = _mknote(Path(cfg.vault_dir), "Audit.md",
                          folder="dev-brain/raw/articles")

    plan = plan_migration(cfg, tmp_path, classify=lambda text: _reply("AI & Dev"))

    assert plan == []
    assert handwritten.exists()


def test_plan_migration_drains_the_root_other_folder(tmp_path):
    """Other/ used to be both a source and a destination. It is a source only
    now: everything ends up under the content root."""
    cfg = _cfg(tmp_path)
    _mknote(Path(cfg.vault_dir), "a.md", topic_line='topic: "Other"', folder="Other")

    plan = plan_migration(cfg, tmp_path, classify=lambda text: _reply("Other"))

    assert [(p.name, t, s) for p, t, s in plan] == [("a.md", "Other", GENERAL)]


def test_main_second_run_after_applying_a_root_topic_folder_note_is_a_no_op(tmp_path, capsys):
    """Re-running the migration after a successful apply -- including one that
    moved a root-topic-folder note -- plans nothing."""
    root = _project(tmp_path)
    cfg = load_config(root / "config.env")
    _mknote(Path(cfg.vault_dir), "a.md", topic_line='topic: "AI & Dev"', folder="AI & Dev")
    backups = tmp_path / "backups"
    backups.mkdir()
    kwargs = dict(project_root=root, backup_dir=backups,
                  runner=_FakeRunner(_reply("AI & Dev", "AI Agents")))
    assert main([], **kwargs) == 0
    assert main(["--apply"], **kwargs) == 0
    dest = _dest(Path(cfg.vault_dir), "AI & Dev/AI Agents", "a")
    assert dest.exists()
    capsys.readouterr()

    rc = main([], **kwargs)

    out = capsys.readouterr().out
    assert rc == 0
    assert "nothing to do" in out.lower()


# --- frontmatter ----------------------------------------------------------

def _fm_keys(fm, key):
    return [ln for ln in fm.splitlines() if ln.startswith(key + ":")]


def test_ensure_topic_frontmatter_replaces_existing():
    text = "---\ntitle: \"T\"\ntopic: Old\nsubtopic: Stale\n---\n\nbody"
    out = ensure_topic_frontmatter(text, "Finance", "General")
    assert 'topic: "Finance"' in out and "topic: Old" not in out
    assert 'subtopic: "General"' in out and "subtopic: Stale" not in out


def test_ensure_topic_frontmatter_keeps_other_keys_and_body(tmp_path):
    text = "---\ntitle: \"T\"\ntopic: Old\ntags: [youtube]\n---\n\nbody\n\n---\n\ntail\n"
    out = ensure_topic_frontmatter(text, "Finance")
    fm = out.split("---")[1]
    assert 'title: "T"' in fm and "tags: [youtube]" in fm
    # by line, not by substring: `subtopic:` contains `topic:`
    assert _fm_keys(fm, "topic") == ['topic: "Finance"']
    assert out.endswith("\n\nbody\n\n---\n\ntail\n")


def test_ensure_topic_frontmatter_adds_missing_keys():
    text = "---\ntitle: \"T\"\n---\n\nbody"
    out = ensure_topic_frontmatter(text, "Personal", "General")
    assert out.split("---")[1].strip().endswith(
        'topic: "Personal"\nsubtopic: "General"')


def test_ensure_topic_frontmatter_handles_note_without_frontmatter():
    out = ensure_topic_frontmatter("just a body\n", "Personal")
    assert out.startswith('---\ntopic: "Personal"\nsubtopic: "General"\n---')
    assert "just a body" in out


def test_ensure_platform_frontmatter_keeps_the_note_readable():
    """The closing fence is not duplicated and the body is byte-preserved."""
    text = '---\ntitle: "T"\nprocessed: 2026-07-24\n---\n\nbody\n'
    out = ensure_platform_frontmatter(text, "youtube")
    assert out == ('---\ntitle: "T"\nprocessed: 2026-07-24\n'
                   'platform: youtube\n---\n\nbody\n')


def test_ensure_platform_frontmatter_keeps_a_crlf_note_clean():
    text = '---\r\ntitle: "T"\r\n---\r\n\r\nbody\r\n'
    out = ensure_platform_frontmatter(text, "youtube")
    assert out == '---\r\ntitle: "T"\r\nplatform: youtube\r\n---\r\n\r\nbody\r\n'


# --- applying -------------------------------------------------------------

def test_apply_migration_moves_renames_and_updates_frontmatter(tmp_path):
    cfg = _cfg(tmp_path)
    vault = Path(cfg.vault_dir)
    src = _mknote(vault, "a.md")

    apply_migration(cfg, [(src, "AI & Dev", "Claude Code")])

    dest = _dest(vault, "AI & Dev/Claude Code", "a")
    assert dest.exists() and not src.exists()
    fm = dest.read_text().split("---")[1]
    assert 'topic: "AI & Dev"' in fm
    assert 'subtopic: "Claude Code"' in fm
    assert "platform: youtube" in fm
    assert not (vault / "YouTube Summaries").exists()  # cleaned when empty


def test_apply_migration_never_rewrites_processed(tmp_path):
    """`processed:` is the save date the filename is built from. Restamping it
    would destroy exactly the signal this layout exists to create."""
    cfg = _cfg(tmp_path)
    vault = Path(cfg.vault_dir)
    src = _mknote(vault, "a.md", processed="2026-01-09")

    apply_migration(cfg, [(src, "Finance", GENERAL)])

    dest = _dest(vault, "Finance", "a", processed="2026-01-09")
    assert dest.exists()
    assert "processed: 2026-01-09" in dest.read_text()


def test_apply_migration_resolves_a_name_taken_by_a_different_note(tmp_path):
    """Two notes sharing a title and a save date must not become one note: the
    filename no longer carries the video id, so a taken name yields ' (2)'
    rather than an overwrite."""
    cfg = _cfg(tmp_path)
    vault = Path(cfg.vault_dir)
    src = _mknote(vault, "a.md", title="T")
    taken = _dest(vault, "AI & Dev/General", "T")
    taken.parent.mkdir(parents=True)
    taken.write_text("a different note that was already here\n")

    result = apply_migration(cfg, [(src, "AI & Dev", GENERAL)])

    assert taken.read_text() == "a different note that was already here\n"
    moved = taken.with_name(f"{PROCESSED} T (YouTube) (2).md")
    assert moved.exists() and not src.exists()
    assert [d for _, d in result["moved"]] == [moved]


def test_apply_migration_forces_a_suffix_when_two_sources_collide_within_one_run(
        tmp_path, capsys):
    """resolve_collision's same-video_id-means-reprocessing rule is right for
    the pipeline, but a migration can hand it two genuinely distinct source
    files for the same video -- one from the legacy backlog, one swept from a
    brain's raw/transcripts -- sharing a title and processed date. Without a
    same-run guard the second note's resolve_collision call would read back
    the first note's freshly written video_id, treat it as a match, and
    silently overwrite it. Both notes must survive under distinct names."""
    cfg = _cfg(tmp_path)
    vault = Path(cfg.vault_dir)
    old = vault / "YouTube Summaries" / "old.md"
    old.parent.mkdir(parents=True)
    old.write_text(f'---\ntitle: "T"\ncategory: ai-dev\nprocessed: {PROCESSED}\n'
                    'video_id: vid\nstatus: summarized\n---\n\nbody from old\n')
    new = vault / "dev-brain" / "raw" / "transcripts" / "new.md"
    new.parent.mkdir(parents=True)
    new.write_text(f'---\ntitle: "T"\ncategory: ai-dev\nprocessed: {PROCESSED}\n'
                    'video_id: vid\nstatus: summarized\n---\n\nbody from new\n')

    result = apply_migration(cfg, [(old, "AI & Dev", GENERAL),
                                    (new, "AI & Dev", GENERAL)])

    dest1 = _dest(vault, "AI & Dev/General", "T")
    dest2 = dest1.with_name(f"{PROCESSED} T (YouTube) (2).md")
    assert dest1.exists() and dest2.exists()
    assert "body from old" in dest1.read_text()
    assert "body from new" in dest2.read_text()
    assert [d for _, d in result["moved"]] == [dest1, dest2]
    assert "already written earlier in this run" in capsys.readouterr().err


def test_apply_migration_refuses_a_note_from_another_platform(tmp_path):
    """Every swept note came from the YouTube poller. One declaring another
    platform means that assumption is wrong, so it is reported and left in
    place rather than mislabelled '(YouTube)' in its own filename."""
    cfg = _cfg(tmp_path)
    vault = Path(cfg.vault_dir)
    src = vault / "AI & Dev" / "a.md"
    src.parent.mkdir(parents=True)
    text = ('---\ntitle: "T"\nplatform: "threads"\ntype: "thread"\n'
            f"processed: {PROCESSED}\n---\n\n1/ first post\n")
    src.write_text(text)

    result = apply_migration(cfg, [(src, "AI & Dev", GENERAL)])

    assert result["moved"] == []
    assert [p for p, _ in result["failed"]] == [src]
    assert src.read_text() == text
    assert list((vault / CONTENT_ROOT).rglob("*.md")) == []  # nothing written


def test_apply_migration_is_idempotent(tmp_path):
    cfg = _cfg(tmp_path)
    vault = Path(cfg.vault_dir)
    src = _mknote(vault, "a.md")
    apply_migration(cfg, [(src, "AI & Dev", GENERAL)])
    dest = _dest(vault, "AI & Dev/General", "a")
    first = dest.read_text()

    plan = plan_migration(cfg, tmp_path, classify=lambda text: _reply("AI & Dev"))
    result = apply_migration(cfg, plan)

    assert plan == []
    assert result["moved"] == [] and result["failed"] == []
    assert dest.read_text() == first
    assert list(dest.parent.iterdir()) == [dest]


def test_apply_migration_keeps_source_folder_when_not_empty(tmp_path):
    """Removing the folder is only ever allowed when it is genuinely empty."""
    cfg = _cfg(tmp_path)
    src = _mknote(Path(cfg.vault_dir), "a.md")
    leftover = src.parent / "notes.txt"
    leftover.write_text("hand-written, not a note\n")

    apply_migration(cfg, [(src, "AI & Dev", GENERAL)])

    assert src.parent.exists()
    assert leftover.read_text() == "hand-written, not a note\n"


def test_apply_migration_continues_after_a_failed_move(tmp_path):
    """One bad entry must not strand the rest: what moved stays valid, and the
    run reports exactly what did not move."""
    cfg = _cfg(tmp_path)
    vault = Path(cfg.vault_dir)
    missing = vault / "YouTube Summaries" / "gone.md"
    good = _mknote(vault, "b.md")

    result = apply_migration(cfg, [(missing, "Finance", GENERAL),
                                   (good, "AI & Dev", GENERAL)])

    moved_dest = _dest(vault, "AI & Dev/General", "b")
    assert moved_dest.exists() and not good.exists()
    assert 'topic: "AI & Dev"' in moved_dest.read_text()
    assert [p for p, _ in result["failed"]] == [missing]
    assert not (vault / CONTENT_ROOT / "Finance").exists()


# --- backup ---------------------------------------------------------------

def test_backup_archives_the_vault_and_skips_stversions(tmp_path):
    cfg = _cfg(tmp_path)
    _mknote(Path(cfg.vault_dir), "a.md")
    stale = Path(cfg.vault_dir) / ".stversions" / "old.md"
    stale.parent.mkdir(parents=True)
    stale.write_text("an old syncthing version\n")
    out_dir = tmp_path / "backups"
    out_dir.mkdir()

    archive = migrate_notes._backup(Path(cfg.vault_dir), out_dir)

    assert archive.parent == out_dir and archive.name.startswith("brain-backup-")
    with tarfile.open(archive) as tf:
        names = tf.getnames()
    assert "Brain/YouTube Summaries/a.md" in names
    assert not [n for n in names if ".stversions" in n]


# --- CLI ------------------------------------------------------------------

def test_main_dry_run_is_the_default_and_changes_nothing(tmp_path, capsys):
    root = _project(tmp_path)
    cfg = load_config(root / "config.env")
    src = _mknote(Path(cfg.vault_dir), "a.md")
    before = src.read_text()
    backups = tmp_path / "backups"
    backups.mkdir()
    # Real Estate is a Business & Marketing subtopic now, not a topic
    runner = _FakeRunner(_reply("Business & Marketing", "Real Estate"))

    rc = main([], project_root=root, backup_dir=backups, runner=runner)

    out = capsys.readouterr().out
    assert rc == 0
    assert "a.md -> saved-content/Business & Marketing/Real Estate/" in out
    assert "Dry run" in out
    assert src.read_text() == before  # untouched, frontmatter and all
    assert list(backups.iterdir()) == []  # no backup, because nothing was moved
    assert not (Path(cfg.vault_dir) / CONTENT_ROOT).exists()


def test_main_apply_backs_up_before_moving_anything(tmp_path):
    root = _project(tmp_path)
    cfg = load_config(root / "config.env")
    _mknote(Path(cfg.vault_dir), "a.md")
    backups = tmp_path / "backups"
    backups.mkdir()
    runner = _FakeRunner(_reply("Business & Marketing", "Real Estate"))
    assert main([], project_root=root, backup_dir=backups, runner=runner) == 0

    rc = main(["--apply"], project_root=root, backup_dir=backups, runner=runner)

    assert rc == 0
    archives = list(backups.iterdir())
    assert len(archives) == 1
    # the note is archived at its PRE-move path, which is only possible if the
    # backup was taken before the first move
    with tarfile.open(archives[0]) as tf:
        assert "Brain/YouTube Summaries/a.md" in tf.getnames()
    assert _dest(Path(cfg.vault_dir), "Business & Marketing/Real Estate", "a").exists()


def test_main_aborts_without_moving_when_the_backup_fails(tmp_path, capsys):
    root = _project(tmp_path)
    cfg = load_config(root / "config.env")
    src = _mknote(Path(cfg.vault_dir), "a.md")
    before = src.read_text()
    not_a_dir = tmp_path / "blocked"
    not_a_dir.write_text("this is a file, so the tarball cannot be written here\n")
    runner = _FakeRunner(_reply("Finance"))
    assert main([], project_root=root, backup_dir=not_a_dir, runner=runner) == 0
    capsys.readouterr()

    rc = main(["--apply"], project_root=root, backup_dir=not_a_dir, runner=runner)

    assert rc == 1
    assert "backup failed" in capsys.readouterr().err.lower()
    assert src.read_text() == before
    assert not (Path(cfg.vault_dir) / CONTENT_ROOT).exists()


def test_main_apply_leaves_hand_placed_brain_articles_alone(tmp_path):
    """End to end: raw/articles predates ingest and must survive a full run
    byte for byte, while the note beside it in raw/transcripts migrates."""
    root = _project(tmp_path)
    cfg = load_config(root / "config.env")
    vault = Path(cfg.vault_dir)
    article = _mknote(vault, "May Audit.md", folder="business-brain/raw/articles")
    before = article.read_text()
    _mknote(vault, "a.md", folder="business-brain/raw/transcripts")
    backups = tmp_path / "backups"
    backups.mkdir()
    kwargs = dict(project_root=root, backup_dir=backups,
                  runner=_FakeRunner(_reply("Finance")))

    assert main([], **kwargs) == 0
    assert main(["--apply"], **kwargs) == 0

    assert article.read_text() == before
    assert _dest(vault, "Finance", "a").exists()


def test_main_second_run_over_a_migrated_vault_reports_nothing_to_do(tmp_path, capsys):
    root = _project(tmp_path)
    cfg = load_config(root / "config.env")
    _mknote(Path(cfg.vault_dir), "a.md")
    backups = tmp_path / "backups"
    backups.mkdir()
    kwargs = dict(project_root=root, backup_dir=backups,
                  runner=_FakeRunner(_reply("Finance")))
    assert main([], **kwargs) == 0
    assert main(["--apply"], **kwargs) == 0
    dest = _dest(Path(cfg.vault_dir), "Finance", "a")
    first = dest.read_text()
    capsys.readouterr()

    rc = main([], **kwargs)

    assert rc == 0
    assert "nothing to do" in capsys.readouterr().out.lower()
    assert dest.read_text() == first
    assert len(list(backups.iterdir())) == 1  # no second backup for an empty plan


# --- dry run: the low-confidence report ------------------------------------

def test_dry_run_reports_unanimous_votes_in_one_line(tmp_path, capsys):
    """A clean run must say so, not stay silent -- silence would read the
    same as a build that predates voting."""
    root = _project(tmp_path)
    cfg = load_config(root / "config.env")
    _mknote(Path(cfg.vault_dir), "a.md")
    plan_path = tmp_path / "plan.json"
    runner = _SequenceRunner([_reply("AI & Dev")] * migrate_notes.CLASSIFY_VOTES)

    rc = migrate_notes._dry_run(cfg, root, plan_path, runner)

    out = capsys.readouterr().out
    assert rc == 0
    assert "notes needing review" in out
    assert "none: every note's votes were unanimous" in out


def test_dry_run_lists_only_the_notes_whose_votes_disagreed(tmp_path, capsys):
    """The short review list: a note with a unanimous vote never appears in
    it, even though it's still in the plan above it."""
    root = _project(tmp_path)
    cfg = load_config(root / "config.env")
    _mknote(Path(cfg.vault_dir), "a.md")
    _mknote(Path(cfg.vault_dir), "b.md")
    plan_path = tmp_path / "plan.json"
    # a.md: unanimous. b.md: 2-of-3 -- classified alphabetically, a.md first.
    runner = _SequenceRunner(
        [_reply("AI & Dev")] * 3
        + [_reply("AI & Dev"), _reply("Finance"), _reply("AI & Dev")]
    )

    rc = migrate_notes._dry_run(cfg, root, plan_path, runner)

    out = capsys.readouterr().out
    assert rc == 0
    report = out.split("notes needing review", 1)[1]
    assert "b.md" in report
    assert "AI & Dev/General, Finance/General, AI & Dev/General" in report
    assert "a.md" not in report


# --- prompt contract ------------------------------------------------------

def test_classify_prompt_matches_what_the_classifier_parses(tmp_path):
    """The summarize prompts hand back a fenced ```meta block parsed by
    src.meta.parse_model_output. This one does not: classify() takes the
    model's reply as two bare lines (`run_claude(...).strip()`), so the prompt
    must ask for exactly that, and must list every allowed topic verbatim."""
    cfg = _cfg(tmp_path)
    text = (PROMPTS_DIR / "classify.md").read_text()

    assert "```meta" not in text
    for topic in allowed_topics(cfg):
        assert topic in text, f"classify.md does not offer {topic!r}"
    assert "topic: <one of:" in text and "subtopic: <one from" in text

    # a reply shaped the way the prompt demands must survive the real path
    _mknote(Path(cfg.vault_dir), "a.md")
    plan = plan_migration(
        cfg, tmp_path,
        classify=lambda t: _reply("Business & Marketing", "SEO").strip())
    assert plan[0][1:] == ("Business & Marketing", "SEO")


def test_classify_prompt_offers_every_configured_subtopic(tmp_path):
    """A subtopic the prompt never names can never be chosen, which would
    quietly collapse a whole branch of the tree into General."""
    cfg = _cfg(tmp_path)
    text = (PROMPTS_DIR / "classify.md").read_text()
    for topic, subs in cfg.subtopics.items():
        for sub in subs:
            assert sub in text, f"classify.md does not offer {topic}/{sub}"


# --- frontmatter fences are lines, never substrings ------------------------

def _dashed_note():
    """What this project's own build_note writes for a title containing `---`:
    _yaml_str is json.dumps, so the dashes survive verbatim inside the value."""
    return build_note(
        {"title": "Rust --- the good parts", "channel": "C", "url": "https://x",
         "video_id": "vid", "published": "2026-01-01", "duration": "10:00"},
        {"category": "ai-dev", "topic": "AI & Dev", "tags": ["rust"]},
        "body text", "transcript line", "2026-01-02",
    )


def _fm_lines_and_body(text):
    """Split a note the way YAML does: on lines that are exactly `---`."""
    lines = text.split("\n")
    assert lines[0] == "---", f"note does not open with a fence: {lines[0]!r}"
    end = lines.index("---", 1)
    return lines[1:end], "\n".join(lines[end + 1:])


def test_existing_topic_is_read_past_dashes_inside_a_value():
    """A substring split lands mid-title and never sees `topic:`."""
    assert migrate_notes._existing_topic(_dashed_note()) == "AI & Dev"


def test_ensure_topic_frontmatter_survives_dashes_inside_a_value():
    """The reported corruption: `topic:` inserted mid-title, the original one
    stranded below, and PyYAML rejecting the whole block."""
    text = _dashed_note()
    _, body_before = _fm_lines_and_body(text)

    out = ensure_topic_frontmatter(text, "Finance", "General")

    fm, body = _fm_lines_and_body(out)
    assert 'title: "Rust --- the good parts"' in fm
    # by line, not by substring: the `subtopic:` line contains `topic:`
    assert [ln for ln in fm if ln.startswith("topic:")] == ['topic: "Finance"']
    assert [ln for ln in fm if ln.startswith("subtopic:")] == ['subtopic: "General"']
    assert body == body_before  # body byte-preserved


def test_ensure_topic_frontmatter_keeps_a_crlf_note_clean():
    """`.strip("\\n")` leaves the `\\r`, which smuggles a blank first line into
    the block."""
    text = "---\r\ntitle: \"T\"\r\ntopic: Old\r\n---\r\n\r\nbody\r\n"

    out = ensure_topic_frontmatter(text, "Finance", "General")

    assert out.split("\r\n")[:4] == ["---", 'title: "T"', 'topic: "Finance"',
                                     'subtopic: "General"']
    assert out.endswith("---\r\n\r\nbody\r\n")


def test_apply_migration_unescapes_a_quoted_title_all_the_way_to_the_filename(tmp_path):
    """note.py's _yaml_str JSON-escapes a title containing `"` (json.dumps).
    _frontmatter_field must undo that the same way _existing_topic already
    does, or the escaped backslash survives into sanitize_title, which maps
    it to '-' -- reproduced against the real vault:
    `The "AI Agency" Playbook` migrated as `The -AI Agency- Playbook`
    instead of the pipeline's own `The AI Agency Playbook`."""
    cfg = _cfg(tmp_path)
    vault = Path(cfg.vault_dir)
    src_dir = vault / "YouTube Summaries"
    src_dir.mkdir(parents=True)
    src = src_dir / "a.md"
    src.write_text(build_note(
        {"title": 'The "AI Agency" Playbook', "channel": "C", "url": "https://x",
         "video_id": "vid", "published": "2026-01-01", "duration": "10:00"},
        {"category": "ai-dev", "topic": "AI & Dev", "tags": []},
        "body text", "transcript line", PROCESSED,
    ))

    result = apply_migration(cfg, [(src, "AI & Dev", GENERAL)])

    dest = _dest(vault, "AI & Dev/General", "The AI Agency Playbook")
    assert dest.exists() and not src.exists()
    assert [d for _, d in result["moved"]] == [dest]


@pytest.mark.parametrize("text", [
    "﻿---\ntitle: \"T\"\ntopic: Old\n---\n\nbody\n",
    "---\ntitle: \"T\"\ntopic: Old\n\nbody, and no closing fence\n",
    "\n---\ntitle: \"T\"\ntopic: Old\n---\n\nbody\n",
], ids=["bom", "unterminated", "leading-blank-line"])
def test_apply_migration_skips_frontmatter_it_cannot_edit_safely(tmp_path, text):
    """All three read as "no frontmatter" to a naive check, and wrapping a
    fresh block around them demotes the real keys into the body."""
    cfg = _cfg(tmp_path)
    vault = Path(cfg.vault_dir)
    src = _mknote(vault, "a.md")
    src.write_text(text)

    result = apply_migration(cfg, [(src, "Finance", GENERAL)])

    assert result["moved"] == []
    assert [p for p, _, _ in result["skipped"]] == [src]
    assert src.read_text() == text  # untouched, not double-wrapped
    assert not (vault / CONTENT_ROOT / "Finance").exists()


# --- backup: never clobber the only pre-migration copy ---------------------

def test_backup_never_overwrites_an_earlier_archive(tmp_path, monkeypatch):
    """The name has 1-second resolution, so a re-run in the same second would
    truncate the archive the first run just wrote."""
    cfg = _cfg(tmp_path)
    _mknote(Path(cfg.vault_dir), "a.md")
    out_dir = tmp_path / "backups"
    out_dir.mkdir()
    monkeypatch.setattr(migrate_notes.time, "strftime", lambda fmt: "20260101-000000")

    first = migrate_notes._backup(Path(cfg.vault_dir), out_dir)
    keep = first.read_bytes()
    with pytest.raises(FileExistsError):
        migrate_notes._backup(Path(cfg.vault_dir), out_dir)

    assert first.read_bytes() == keep
    assert len(list(out_dir.iterdir())) == 1


def test_main_apply_aborts_when_that_backup_name_is_taken(tmp_path, capsys, monkeypatch):
    root = _project(tmp_path)
    cfg = load_config(root / "config.env")
    src = _mknote(Path(cfg.vault_dir), "a.md")
    before = src.read_text()
    backups = tmp_path / "backups"
    backups.mkdir()
    monkeypatch.setattr(migrate_notes.time, "strftime", lambda fmt: "20260101-000000")
    runner = _FakeRunner(_reply("Finance"))
    assert main([], project_root=root, backup_dir=backups, runner=runner) == 0
    earlier = backups / "brain-backup-20260101-000000.tar.gz"
    earlier.write_bytes(b"the earlier run's only backup\n")
    capsys.readouterr()

    rc = main(["--apply"], project_root=root, backup_dir=backups, runner=runner)

    assert rc == 1
    assert "backup failed" in capsys.readouterr().err.lower()
    assert earlier.read_bytes() == b"the earlier run's only backup\n"
    assert src.read_text() == before
    assert not (Path(cfg.vault_dir) / CONTENT_ROOT).exists()


# --- unreadable notes -----------------------------------------------------

def test_apply_migration_reports_a_note_it_cannot_decode(tmp_path):
    """UnicodeDecodeError is a ValueError, so it escaped the OSError guard and
    took the whole move report with it."""
    cfg = _cfg(tmp_path)
    vault = Path(cfg.vault_dir)
    good = _mknote(vault, "b.md")
    bad = good.parent / "a.md"
    bad.write_bytes(b"---\ntitle: \"\xff\xfe\"\n---\n\nbody\n")

    result = apply_migration(cfg, [(bad, "Finance", GENERAL),
                                   (good, "AI & Dev", GENERAL)])

    assert [p for p, _ in result["failed"]] == [bad]
    dest = _dest(vault, "AI & Dev/General", "b")
    assert dest.exists() and not good.exists()
    assert bad.exists() and not (vault / CONTENT_ROOT / "Finance").exists()


def test_plan_migration_survives_entries_it_cannot_read(tmp_path, capsys):
    """A `*.md` directory and a dangling `*.md` symlink must not abort the plan."""
    cfg = _cfg(tmp_path)
    good = _mknote(Path(cfg.vault_dir), "z.md")
    (good.parent / "a.md").mkdir()
    (good.parent / "b.md").symlink_to(good.parent / "nowhere.md")

    plan = plan_migration(cfg, tmp_path, classify=lambda text: _reply("Finance"))

    assert [(p.name, t, s) for p, t, s in plan] == [("z.md", "Finance", GENERAL)]
    err = capsys.readouterr().err
    assert "a.md" in err and "b.md" in err


# --- the plan you approved is the plan that runs ---------------------------

def test_apply_executes_the_reviewed_plan_and_classifies_nothing(tmp_path):
    """The dry run's decisions are the ones that run: no second, differently
    answered round of classification behind the operator's back."""
    root = _project(tmp_path)
    cfg = load_config(root / "config.env")
    _mknote(Path(cfg.vault_dir), "a.md")
    backups = tmp_path / "backups"
    backups.mkdir()
    runner = _FakeRunner(_reply("Finance"))

    assert main([], project_root=root, backup_dir=backups, runner=runner) == 0
    reviewed = len(runner.calls)
    assert reviewed == migrate_notes.CLASSIFY_VOTES  # majority-of-3, all Finance
    runner.stdout = _reply("Career")  # the model would answer differently now

    rc = main(["--apply"], project_root=root, backup_dir=backups, runner=runner)

    assert rc == 0
    assert _dest(Path(cfg.vault_dir), "Finance", "a").exists()
    assert not (Path(cfg.vault_dir) / CONTENT_ROOT / "Career").exists()
    assert len(runner.calls) == reviewed  # --apply asked the model nothing


def test_apply_refuses_when_no_plan_was_reviewed(tmp_path, capsys):
    root = _project(tmp_path)
    cfg = load_config(root / "config.env")
    src = _mknote(Path(cfg.vault_dir), "a.md")
    before = src.read_text()
    backups = tmp_path / "backups"
    backups.mkdir()

    rc = main(["--apply"], project_root=root, backup_dir=backups,
              runner=_FakeRunner(_reply("Finance")))

    err = capsys.readouterr().err.lower()
    assert rc == 1
    assert "no plan" in err and "dry run" in err
    assert src.read_text() == before
    assert list(backups.iterdir()) == []


def test_apply_skips_sources_that_changed_or_vanished_since_the_plan(tmp_path, capsys):
    """Syncthing can rewrite a note between the review and the run; guessing
    is not allowed."""
    root = _project(tmp_path)
    cfg = load_config(root / "config.env")
    vault = Path(cfg.vault_dir)
    edited = _mknote(vault, "a.md")
    gone = _mknote(vault, "b.md")
    steady = _mknote(vault, "c.md")
    backups = tmp_path / "backups"
    backups.mkdir()
    plan_file = tmp_path / "plan.json"
    runner = _FakeRunner(_reply("Finance"))
    assert main(["--plan", str(plan_file)], project_root=root, backup_dir=backups,
                runner=runner) == 0
    edited.write_text(edited.read_text() + "\nedited on another device\n")
    gone.unlink()
    capsys.readouterr()

    rc = main(["--apply", "--plan", str(plan_file)], project_root=root,
              backup_dir=backups, runner=runner)

    err = capsys.readouterr().err
    assert rc == 1
    assert _dest(vault, "Finance", "c").exists() and not steady.exists()
    assert edited.exists() and not _dest(vault, "Finance", "a").exists()
    assert "a.md" in err and "b.md" in err


# --- classifier excerpt -----------------------------------------------------

def test_classifier_excerpt_drops_the_transcript_callout():
    """The transcript callout is where an 18 KB note becomes 183 KB; the
    classifier should never see it."""
    text = (
        "---\ntitle: \"T\"\n---\n\n"
        "This note is about building AI agents with Claude.\n\n"
        "> [!note]- Full transcript\n" + "> line of transcript\n" * 2000
    )
    excerpt = migrate_notes._classifier_excerpt(text)
    assert "building AI agents" in excerpt
    assert "line of transcript" not in excerpt
    assert migrate_notes._TRANSCRIPT_MARK not in excerpt


def test_classifier_excerpt_leaves_a_short_note_unchanged():
    text = "---\ntitle: \"T\"\n---\n\nShort body, no transcript here.\n"
    assert migrate_notes._classifier_excerpt(text) == text


def test_classifier_excerpt_honors_the_limit():
    text = "---\ntitle: \"T\"\n---\n\n" + "word " * 5000  # no callout, way over budget
    excerpt = migrate_notes._classifier_excerpt(text, limit=200)
    assert len(excerpt) <= 200 + len('---\ntitle: "T"\n---\n')  # frontmatter kept whole
    assert 'title: "T"' in excerpt


def test_classifier_excerpt_never_splits_the_title_out_of_frontmatter():
    """A raw character slice at a tiny limit would otherwise cut the title
    line in half; the frontmatter must survive intact even past the budget."""
    text = "---\ntitle: \"T\"\ncategory: ai-dev\n---\n\n" + "word " * 5000
    excerpt = migrate_notes._classifier_excerpt(text, limit=5)
    assert 'title: "T"' in excerpt


@pytest.mark.parametrize("text", [
    "",
    "just a body, no frontmatter, no callout at all\n",
    "---\ntitle: \"T\"\n---\n\nbody with no callout\n",
    "---\ntitle: \"T\"\n---\n\n",
    "---\ntitle: \"T\"\n\nbody, and no closing fence\n",
    "﻿---\ntitle: \"T\"\n---\n\nbody\n",
    "---\r\ntitle: \"T\"\r\n---\r\n\r\nbody\r\n",
], ids=["empty", "no-frontmatter", "no-callout", "no-body", "unclosed-fence", "bom", "crlf"])
def test_classifier_excerpt_never_raises_on_a_malformed_note(text):
    assert isinstance(migrate_notes._classifier_excerpt(text), str)
    assert isinstance(migrate_notes._classifier_excerpt(text, limit=1), str)


@pytest.mark.parametrize("text", [
    "﻿---\ntitle: \"T\"\n---\n\n" + "word " * 5000,
    "---\ntitle: \"T\"\n\n" + "body with no closing fence " * 5000,
], ids=["bom-oversized", "unclosed-fence-oversized"])
def test_classifier_excerpt_truncates_a_malformed_oversized_note_without_raising(text):
    """Neither shape has frontmatter this function can trust, so it falls back
    to a plain character slice rather than raising."""
    excerpt = migrate_notes._classifier_excerpt(text, limit=200)
    assert len(excerpt) == 200


# --- classifier replies ----------------------------------------------------

def test_normalize_classification_reads_the_two_line_contract(tmp_path):
    cfg = _cfg(tmp_path)
    assert normalize_classification(
        "topic: AI & Dev\nsubtopic: Claude Code\n",
        allowed_topics(cfg), cfg.subtopics) == ("AI & Dev", "Claude Code")


def test_normalize_classification_falls_back_to_general_for_an_unlisted_subtopic(tmp_path):
    cfg = _cfg(tmp_path)
    assert normalize_classification(
        "topic: AI & Dev\nsubtopic: Woodworking\n",
        allowed_topics(cfg), cfg.subtopics) == ("AI & Dev", GENERAL)


def test_normalize_classification_gives_a_topic_without_subtopics_general(tmp_path):
    """Finance has no configured list, so it is a two-level branch."""
    cfg = _cfg(tmp_path)
    assert normalize_classification(
        "topic: Finance\nsubtopic: Crypto\n",
        allowed_topics(cfg), cfg.subtopics) == ("Finance", GENERAL)


def test_normalize_classification_strips_noise_from_the_subtopic_line(tmp_path):
    """The same classifier bolding that recovers via normalize_topic for the
    topic line (`**Finance**`) reaches the subtopic line too, but subtopic has
    no such fallback -- an un-stripped value falls straight through to
    General, collapsing a whole subtopic tree for one bolding run."""
    cfg = _cfg(tmp_path)
    assert normalize_classification(
        "topic: AI & Dev\nsubtopic: **Claude Code**\n",
        allowed_topics(cfg), cfg.subtopics) == ("AI & Dev", "Claude Code")


def test_normalize_classification_files_an_unreadable_reply_under_other(tmp_path):
    cfg = _cfg(tmp_path)
    assert normalize_classification(
        "I think this is about money", allowed_topics(cfg),
        cfg.subtopics) == ("Other", GENERAL)


def test_normalize_classification_still_reads_a_bare_single_line_reply(tmp_path):
    """The old contract, and the shapes models actually send it in."""
    cfg = _cfg(tmp_path)
    allowed = allowed_topics(cfg)
    for reply in ["Finance", " Finance ", '"Finance"', "**Finance**", "finance."]:
        assert normalize_classification(reply, allowed, cfg.subtopics) == (
            "Finance", GENERAL)


@pytest.mark.parametrize("reply", [
    "Finance", " Finance ", '"Finance"', "'Finance'", "Finance.", "finance",
    "**Finance**", "Topic: Finance", "`Finance`", "FINANCE",
])
def test_normalize_topic_reads_the_replies_models_actually_send(tmp_path, reply):
    cfg = _cfg(tmp_path)
    assert migrate_notes.normalize_topic(reply, allowed_topics(cfg)) == "Finance"


def test_normalize_topic_takes_the_first_line_when_the_model_adds_prose(tmp_path):
    cfg = _cfg(tmp_path)
    reply = "AI & Dev\n\nThis note is mostly about compilers.\n"
    assert migrate_notes.normalize_topic(reply, allowed_topics(cfg)) == "AI & Dev"


def test_normalize_topic_gives_up_rather_than_guessing(tmp_path):
    cfg = _cfg(tmp_path)
    assert migrate_notes.normalize_topic("I think this is about money", allowed_topics(cfg)) is None


def test_plan_migration_warns_when_a_reply_cannot_be_read(tmp_path, capsys):
    """`a.md -> Other/` must not read the same whether the model chose Other or
    the parse failed."""
    cfg = _cfg(tmp_path)
    _mknote(Path(cfg.vault_dir), "a.md")

    plan = plan_migration(cfg, tmp_path, classify=lambda text: "I think this is about money")

    err = capsys.readouterr().err
    assert plan[0][1] == "Other"
    assert "a.md" in err and "I think this is about money" in err


def test_plan_migration_keeps_a_deliberate_other_quiet(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    _mknote(Path(cfg.vault_dir), "a.md")

    plan = plan_migration(cfg, tmp_path, classify=lambda text: _reply("Other"))

    assert plan[0][1] == "Other"
    assert capsys.readouterr().err == ""


# --- majority-of-3 voting ---------------------------------------------------
# Measured against the real vault: one classify() call is not reproducible --
# the same note came back Other, then AI & Dev, then Business & Marketing
# across three runs. These drive the vote with a fake classifier returning a
# scripted sequence of replies, since that's the only deterministic way to
# exercise the vote logic.

def test_plan_migration_classifies_three_times_per_note(tmp_path):
    cfg = _cfg(tmp_path)
    _mknote(Path(cfg.vault_dir), "a.md")
    calls = []

    def classify(text):
        calls.append(text)
        return _reply("AI & Dev")

    plan_migration(cfg, tmp_path, classify=classify)
    assert len(calls) == migrate_notes.CLASSIFY_VOTES == 3


def test_plan_migration_unanimous_vote_is_not_low_confidence(tmp_path):
    cfg = _cfg(tmp_path)
    _mknote(Path(cfg.vault_dir), "a.md")
    low_confidence = []

    plan = plan_migration(cfg, tmp_path, classify=lambda text: _reply("AI & Dev"),
                          low_confidence=low_confidence)

    assert plan[0][1:] == ("AI & Dev", GENERAL)
    assert low_confidence == []


def test_plan_migration_takes_the_majority_of_two_of_three(tmp_path):
    cfg = _cfg(tmp_path)
    _mknote(Path(cfg.vault_dir), "a.md")
    replies = iter([_reply("AI & Dev"), _reply("Finance"), _reply("AI & Dev")])
    low_confidence = []

    plan = plan_migration(cfg, tmp_path, classify=lambda text: next(replies),
                          low_confidence=low_confidence)

    assert plan[0][1:] == ("AI & Dev", GENERAL)
    # not unanimous, so it's still reported for the operator to glance at
    assert low_confidence == [("a.md", [("AI & Dev", GENERAL), ("Finance", GENERAL),
                                        ("AI & Dev", GENERAL)])]


def test_plan_migration_all_three_disagree_takes_the_first_vote(tmp_path):
    cfg = _cfg(tmp_path)
    _mknote(Path(cfg.vault_dir), "a.md")
    replies = iter([_reply("Other"), _reply("AI & Dev"), _reply("Finance")])
    low_confidence = []

    plan = plan_migration(cfg, tmp_path, classify=lambda text: next(replies),
                          low_confidence=low_confidence)

    assert plan[0][1] == "Other"  # first vote wins when nothing has a majority
    assert [t for t, _ in low_confidence[0][1]] == ["Other", "AI & Dev", "Finance"]


def test_plan_migration_counts_an_unparseable_vote_as_other(tmp_path):
    """A vote normalize_classification can't read is folded to Other, exactly
    like the single-shot path -- here it's simply outvoted by the other two."""
    cfg = _cfg(tmp_path)
    _mknote(Path(cfg.vault_dir), "a.md")
    replies = iter([_reply("AI & Dev"), "I think this is about money",
                    _reply("AI & Dev")])

    plan = plan_migration(cfg, tmp_path, classify=lambda text: next(replies))

    assert plan[0][1] == "AI & Dev"


def test_majority_classification_takes_the_subtopic_from_the_winning_topic():
    """Voting on the pair jointly would let a minority topic win a subtopic,
    which is how a note lands in a folder its own topic does not have."""
    votes = [("AI & Dev", "Claude Code"), ("Business & Marketing", "SEO"),
             ("AI & Dev", "AI Agents")]

    topic, subtopic, unanimous = _majority_classification(votes)

    assert topic == "AI & Dev"
    assert subtopic in {"Claude Code", "AI Agents"}  # never SEO
    assert unanimous is False


def test_majority_classification_reports_a_unanimous_pair():
    votes = [("Finance", GENERAL)] * 3
    assert _majority_classification(votes) == ("Finance", GENERAL, True)


# --- exit codes ------------------------------------------------------------

def test_main_returns_non_zero_when_a_note_is_left_behind(tmp_path, capsys):
    """A wrapper must not read success while a note sits stranded in the
    legacy folder."""
    root = _project(tmp_path)
    cfg = load_config(root / "config.env")
    vault = Path(cfg.vault_dir)
    _mknote(vault, "good.md")
    (vault / "YouTube Summaries" / "bom.md").write_bytes(
        "﻿---\ntitle: \"T\"\n---\n\nbody\n".encode("utf-8"))
    backups = tmp_path / "backups"
    backups.mkdir()
    runner = _FakeRunner(_reply("Finance"))
    assert main([], project_root=root, backup_dir=backups, runner=runner) == 0
    capsys.readouterr()

    rc = main(["--apply"], project_root=root, backup_dir=backups, runner=runner)

    assert rc == 1
    assert "kept" in capsys.readouterr().err.lower()
    assert (vault / "YouTube Summaries" / "bom.md").exists()
    assert _dest(vault, "Finance", "good").exists()


# --- plan file trust boundary: it is reviewed, but may be hand-edited --------

def _write_plan(plan_path, entries):
    plan_path.write_text(json.dumps({"version": 1, "entries": entries}, indent=2) + "\n")


def test_apply_maps_an_unrecognised_topic_to_other_not_a_stray_folder(tmp_path):
    """A realistic hand-edit typo (or a case mismatch from Syncthing across
    macOS/Linux) must land exactly where an unrecognised topic already lands
    on the planning path: Other/, never a new folder named after the typo."""
    root = _project(tmp_path)
    cfg = load_config(root / "config.env")
    vault = Path(cfg.vault_dir)
    src = _mknote(vault, "a.md")
    plan_file = tmp_path / "plan.json"
    _write_plan(plan_file, [
        {"source": str(src), "topic": "Fnance", "subtopic": "General",
         "sha256": migrate_notes._digest(src)},
    ])
    backups = tmp_path / "backups"
    backups.mkdir()

    rc = main(["--apply", "--plan", str(plan_file)], project_root=root,
              backup_dir=backups, runner=_FakeRunner())

    assert rc == 0
    assert _dest(vault, "Other", "a").exists()
    assert not (vault / CONTENT_ROOT / "Fnance").exists()


def test_apply_maps_an_unlisted_subtopic_to_general(tmp_path):
    """Same trust boundary one level down: a hand-edited subtopic that the
    topic does not have must not create a folder."""
    root = _project(tmp_path)
    cfg = load_config(root / "config.env")
    vault = Path(cfg.vault_dir)
    src = _mknote(vault, "a.md")
    plan_file = tmp_path / "plan.json"
    _write_plan(plan_file, [
        {"source": str(src), "topic": "AI & Dev", "subtopic": "../ESCAPED",
         "sha256": migrate_notes._digest(src)},
    ])
    backups = tmp_path / "backups"
    backups.mkdir()

    rc = main(["--apply", "--plan", str(plan_file)], project_root=root,
              backup_dir=backups, runner=_FakeRunner())

    assert rc == 0
    assert _dest(vault, "AI & Dev/General", "a").exists()
    assert not (vault.parent / "ESCAPED").exists()


def test_apply_rejects_a_traversal_topic_and_files_under_other(tmp_path):
    root = _project(tmp_path)
    cfg = load_config(root / "config.env")
    vault = Path(cfg.vault_dir)
    src = _mknote(vault, "a.md")
    plan_file = tmp_path / "plan.json"
    _write_plan(plan_file, [
        {"source": str(src), "topic": "../../ESCAPED", "subtopic": "General",
         "sha256": migrate_notes._digest(src)},
    ])
    backups = tmp_path / "backups"
    backups.mkdir()

    rc = main(["--apply", "--plan", str(plan_file)], project_root=root,
              backup_dir=backups, runner=_FakeRunner())

    assert rc == 0
    assert _dest(vault, "Other", "a").exists()
    assert not (vault.parent.parent / "ESCAPED").exists()


def test_apply_rejects_an_absolute_path_topic_and_files_under_other(tmp_path):
    root = _project(tmp_path)
    cfg = load_config(root / "config.env")
    vault = Path(cfg.vault_dir)
    src = _mknote(vault, "a.md")
    plan_file = tmp_path / "plan.json"
    _write_plan(plan_file, [
        {"source": str(src), "topic": "/abs/path", "subtopic": "General",
         "sha256": migrate_notes._digest(src)},
    ])
    backups = tmp_path / "backups"
    backups.mkdir()

    rc = main(["--apply", "--plan", str(plan_file)], project_root=root,
              backup_dir=backups, runner=_FakeRunner())

    assert rc == 0
    assert _dest(vault, "Other", "a").exists()
    assert not Path("/abs/path/a.md").exists()


def test_apply_skips_a_plan_entry_whose_source_is_outside_the_vault(tmp_path, capsys):
    """A plan entry pointing outside the vault must be reported and skipped,
    never read, rewritten into the vault, and deleted from where it lives."""
    root = _project(tmp_path)
    cfg = load_config(root / "config.env")
    vault = Path(cfg.vault_dir)
    vault.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside.md"
    outside.write_text("not from the vault\n")
    plan_file = tmp_path / "plan.json"
    _write_plan(plan_file, [
        {"source": str(outside), "topic": "Finance", "subtopic": "General",
         "sha256": migrate_notes._digest(outside)},
    ])
    backups = tmp_path / "backups"
    backups.mkdir()

    rc = main(["--apply", "--plan", str(plan_file)], project_root=root,
              backup_dir=backups, runner=_FakeRunner())

    err = capsys.readouterr().err
    assert rc == 1
    assert "outside.md" in err
    assert outside.read_text() == "not from the vault\n"
    assert not (vault / CONTENT_ROOT).exists()
    assert list(backups.iterdir()) == []  # nothing was moved, so no backup either


def test_apply_skips_a_plan_destination_that_escapes_through_a_symlinked_root(tmp_path, capsys):
    """The destination is nested (<content root>/<topic>/<subtopic>); a
    legitimate-looking topic can still resolve outside the vault if the first
    path component -- the content root -- is a symlink planted there some
    other way. The containment check must still catch it."""
    root = _project(tmp_path)
    cfg = load_config(root / "config.env")
    vault = Path(cfg.vault_dir)
    src = _mknote(vault, "a.md")
    outside = tmp_path / "outside-tree"
    outside.mkdir()
    (vault / CONTENT_ROOT).symlink_to(outside)
    plan_file = tmp_path / "plan.json"
    _write_plan(plan_file, [
        {"source": str(src), "topic": "AI & Dev", "subtopic": "General",
         "sha256": migrate_notes._digest(src)},
    ])
    backups = tmp_path / "backups"
    backups.mkdir()

    rc = main(["--apply", "--plan", str(plan_file)], project_root=root,
              backup_dir=backups, runner=_FakeRunner())

    err = capsys.readouterr().err
    assert rc == 1
    assert "a.md" in err
    assert src.exists()
    assert list(outside.iterdir()) == []  # nothing was written through the symlink


def test_apply_rejects_a_plan_entry_naming_a_subvault_pages_file(tmp_path, capsys):
    """The source containment check must accept only direct children of a
    recognized source folder, not anywhere under the vault -- a hand-edited
    plan entry pointing into a sub-vault's own pages/ folder must be refused
    even though it resolves inside VAULT_DIR and its sha256 matches."""
    root = _project(tmp_path)
    cfg = load_config(root / "config.env")
    vault = Path(cfg.vault_dir)
    pages_dir = vault / "dev-brain" / "pages"
    pages_dir.mkdir(parents=True)
    curated = pages_dir / "Curated Index.md"
    curated.write_text("---\ntitle: \"Curated\"\n---\n\nhand-written content\n")
    plan_file = tmp_path / "plan.json"
    _write_plan(plan_file, [
        {"source": str(curated), "topic": "AI & Dev", "subtopic": "General",
         "sha256": migrate_notes._digest(curated)},
    ])
    backups = tmp_path / "backups"
    backups.mkdir()

    rc = main(["--apply", "--plan", str(plan_file)], project_root=root,
              backup_dir=backups, runner=_FakeRunner())

    err = capsys.readouterr().err
    assert rc == 1
    assert "Curated Index.md" in err
    assert curated.exists()
    assert not (vault / CONTENT_ROOT).exists()


def test_apply_rejects_a_plan_entry_naming_a_brain_article(tmp_path, capsys):
    """raw/articles is not a source, so a hand-edited plan cannot smuggle a
    hand-placed audit report into the migration either."""
    root = _project(tmp_path)
    cfg = load_config(root / "config.env")
    vault = Path(cfg.vault_dir)
    article = _mknote(vault, "May Audit.md", folder="dev-brain/raw/articles")
    plan_file = tmp_path / "plan.json"
    _write_plan(plan_file, [
        {"source": str(article), "topic": "AI & Dev", "subtopic": "General",
         "sha256": migrate_notes._digest(article)},
    ])
    backups = tmp_path / "backups"
    backups.mkdir()

    rc = main(["--apply", "--plan", str(plan_file)], project_root=root,
              backup_dir=backups, runner=_FakeRunner())

    err = capsys.readouterr().err
    assert rc == 1
    assert "May Audit.md" in err
    assert article.exists()
    assert not (vault / CONTENT_ROOT).exists()


# --- stranded notes outside the legacy folder -------------------------------

def test_main_apply_exits_nonzero_when_a_note_is_stranded_in_a_root_topic_folder(tmp_path, capsys):
    """The reviewer's repro: a good note migrates cleanly out of YouTube
    Summaries/, while a malformed note in a root topic folder (AI & Dev/)
    cannot be classified/moved and is left in place. The exit code must
    reflect that -- an operator reading rc == 0 must not conclude the
    migration finished when a note is still stranded outside the legacy
    folder."""
    root = _project(tmp_path)
    cfg = load_config(root / "config.env")
    vault = Path(cfg.vault_dir)
    _mknote(vault, "good.md")
    bom_dir = vault / "AI & Dev"
    bom_dir.mkdir(parents=True)
    (bom_dir / "bom.md").write_bytes("﻿---\ntitle: \"T\"\n---\n\nbody\n".encode("utf-8"))
    backups = tmp_path / "backups"
    backups.mkdir()
    runner = _FakeRunner(_reply("Finance"))
    assert main([], project_root=root, backup_dir=backups, runner=runner) == 0
    capsys.readouterr()

    rc = main(["--apply"], project_root=root, backup_dir=backups, runner=runner)

    assert rc == 1
    assert (bom_dir / "bom.md").exists()
    # the good note still migrates; one bad note doesn't block it
    assert _dest(vault, "Finance", "good").exists()


def test_main_apply_files_an_other_note_into_the_content_tree(tmp_path, capsys):
    """Other/ used to be its own destination, so a note resting there was not
    stranded. Now everything drains into the content root, and a clean run
    leaves the root folder empty and exits 0."""
    root = _project(tmp_path)
    cfg = load_config(root / "config.env")
    vault = Path(cfg.vault_dir)
    _mknote(vault, "a.md", folder="Other")
    backups = tmp_path / "backups"
    backups.mkdir()
    runner = _FakeRunner(_reply("Other"))

    assert main([], project_root=root, backup_dir=backups, runner=runner) == 0
    capsys.readouterr()
    rc = main(["--apply"], project_root=root, backup_dir=backups, runner=runner)

    assert rc == 0
    assert _dest(vault, "Other", "a").exists()
    assert not (vault / "Other").exists()  # drained and cleaned up


# --- source-dir exclusions ---------------------------------------------------

def test_plan_migration_excludes_a_brain_name_from_top_level_sources(tmp_path):
    """If TOPICS= accidentally lists a brain folder's own name, that folder
    must still never be swept at its top level -- only its raw/transcripts is
    a source, and everything else in it is hand-placed."""
    env = tmp_path / "config.env"
    env.write_text(
        f"PLAYLIST_URL=https://x\nVAULT_DIR={tmp_path}/vault\n"
        "TOPICS=AI & Dev,dev-brain,Personal\n"
    )
    cfg = load_config(env)
    vault = Path(cfg.vault_dir)
    handwritten = vault / "dev-brain" / "Top Level Curated.md"
    handwritten.parent.mkdir(parents=True)
    handwritten.write_text("---\ntitle: \"Curated\"\n---\n\nhand-written content\n")

    plan = plan_migration(cfg, tmp_path, classify=lambda text: _reply("Personal"))

    assert plan == []
    assert handwritten.exists()


# --- a clean migration clears the plan, so a later --apply is quiet ---------

def test_main_second_apply_after_clean_migration_does_not_print_stale_wall(tmp_path, capsys):
    """After a successful apply, a dry run that finds nothing to do must
    supersede the old plan file. Otherwise a later --apply re-reads the
    stale pre-migration plan and reports every note as 'gone since the plan
    was written' -- safe (nothing moves twice), but alarming."""
    root = _project(tmp_path)
    cfg = load_config(root / "config.env")
    _mknote(Path(cfg.vault_dir), "a.md")
    backups = tmp_path / "backups"
    backups.mkdir()
    kwargs = dict(project_root=root, backup_dir=backups,
                  runner=_FakeRunner(_reply("Finance")))
    assert main([], **kwargs) == 0
    assert main(["--apply"], **kwargs) == 0
    assert main([], **kwargs) == 0  # dry run again: nothing to do
    capsys.readouterr()

    rc = main(["--apply"], **kwargs)

    err = capsys.readouterr().err
    assert rc == 0
    assert "gone since the plan was written" not in err


def test_main_second_apply_over_an_executed_plan_takes_no_second_backup(tmp_path):
    """Pins the apply-path half of idempotency: re-running --apply against the
    plan file the first run already executed must not touch anything again."""
    root = _project(tmp_path)
    cfg = load_config(root / "config.env")
    _mknote(Path(cfg.vault_dir), "a.md")
    backups = tmp_path / "backups"
    backups.mkdir()
    plan_file = tmp_path / "plan.json"
    kwargs = dict(project_root=root, backup_dir=backups,
                  runner=_FakeRunner(_reply("Finance")))
    assert main(["--plan", str(plan_file)], **kwargs) == 0
    assert main(["--apply", "--plan", str(plan_file)], **kwargs) == 0
    dest = _dest(Path(cfg.vault_dir), "Finance", "a")
    first = dest.read_text()
    assert len(list(backups.iterdir())) == 1

    main(["--apply", "--plan", str(plan_file)], **kwargs)

    assert dest.read_text() == first
    assert len(list(backups.iterdir())) == 1  # no second backup
    assert list(dest.parent.iterdir()) == [dest]  # nothing moved twice


def test_dry_run_refuses_when_the_vault_is_missing(tmp_path, monkeypatch):
    """An unreachable VAULT_DIR must not overwrite a reviewed plan with an empty
    one. Pre-fix that sequence exited 0 twice and migrated nothing, which reads
    as success."""
    import migrate_notes
    cfg = types.SimpleNamespace(vault_dir=tmp_path / "not-there", model="sonnet")
    plan_path = tmp_path / "migration-plan.json"
    plan_path.write_text('{"entries": [{"source": "/x/a.md", "topic": "Finance", "sha256": "d"}]}')
    rc = migrate_notes._dry_run(cfg, tmp_path, plan_path, runner=None)
    assert rc == 1
    # the reviewed plan survives untouched
    assert "a.md" in plan_path.read_text()


def test_normalize_topic_reads_a_topic_that_follows_reasoning():
    """Measured against the real vault: the model writes a sentence first and
    the answer on a later line. Reading only the first non-blank line filed
    correctly-classified notes under Other/."""
    from migrate_notes import normalize_topic
    allowed = ["AI & Dev", "Finance", "Health & Fitness", "Personal", "Other"]
    real = ("Filing this note into a topic folder — the content is squarely about "
            "insulin resistance, ketogenic diets, and metabolic health.\n\nHealth & Fitness")
    assert normalize_topic(real, allowed) == "Health & Fitness"
    other = ("This is note-filing output, not a task request — I'll just classify "
             "it per the instructions.\n\nPersonal")
    assert normalize_topic(other, allowed) == "Personal"

def test_normalize_topic_ignores_a_topic_named_inside_a_sentence():
    """Whole-line match only: a reasoning sentence that merely mentions a topic
    must not be mistaken for the answer."""
    from migrate_notes import normalize_topic
    allowed = ["AI & Dev", "Finance", "Personal", "Other"]
    assert normalize_topic("This is not Finance despite the ticker talk.", allowed) is None
    # ...and when a real answer follows such a sentence, the answer wins
    assert normalize_topic("Could be Finance or Personal.\n\nAI & Dev", allowed) == "AI & Dev"

def test_normalize_topic_still_reads_a_bare_reply():
    from migrate_notes import normalize_topic
    allowed = ["AI & Dev", "Finance", "Other"]
    assert normalize_topic("Finance\n", allowed) == "Finance"
    assert normalize_topic("**Finance**", allowed) == "Finance"
    assert normalize_topic("Topic: Finance", allowed) == "Finance"
    assert normalize_topic("nonsense", allowed) is None


# --- what this migration sweeps, and what it must never sweep ---------------

def test_source_dirs_sweeps_backlog_root_topics_and_brain_transcripts(tmp_path):
    dirs = _source_dirs(tmp_path, ["AI & Dev", "Other"], "saved-content")
    names = {str(d.relative_to(tmp_path)) for d in dirs}
    assert "YouTube Summaries" in names
    assert "AI & Dev" in names
    assert "dev-brain/raw/transcripts" in names
    assert "business-brain/raw/transcripts" in names
    assert "personal-brain/raw/transcripts" in names


def test_source_dirs_never_sweeps_brain_articles(tmp_path):
    # raw/articles holds hand-placed material that predates ingest
    dirs = {str(d) for d in _source_dirs(tmp_path, ["AI & Dev"], "saved-content")}
    assert not any("raw/articles" in d for d in dirs)


def test_source_dirs_never_sweeps_the_content_root(tmp_path):
    dirs = {str(d) for d in _source_dirs(tmp_path, ["AI & Dev"], "saved-content")}
    assert not any("saved-content" in d for d in dirs)


def test_source_dirs_rejects_a_traversal_topic(tmp_path):
    """A hand-edited TOPICS=.. must never become a recognized source folder:
    vault_dir / ".." resolves outside the vault entirely once read_plan
    resolves it, so it must be filtered here the same as every other path
    component reaching the filesystem."""
    dirs = _source_dirs(tmp_path, ["AI & Dev", "..", "Other"], "saved-content")
    assert (tmp_path / "..").resolve() not in {d.resolve() for d in dirs}
    assert all(is_safe_path_component(d.name) for d in dirs if d.parent == tmp_path)


def test_migration_backfills_platform_youtube():
    text = '---\ntitle: "T"\nvideo_id: v\nprocessed: 2026-07-24\n---\n\nbody\n'
    out = ensure_platform_frontmatter(text, "youtube")
    assert "platform: youtube" in out
    assert "video_id: v" in out          # existing keys survive
    assert out.endswith("body\n")        # body is untouched


def test_migration_platform_backfill_is_idempotent():
    text = '---\ntitle: "T"\nplatform: youtube\n---\n\nbody\n'
    assert ensure_platform_frontmatter(text, "youtube") == text


def test_migration_refuses_a_note_with_a_conflicting_platform():
    # every swept note came from the poller; one that disagrees means that
    # assumption is wrong, and mislabelling it is worse than stopping
    text = '---\ntitle: "T"\nplatform: threads\n---\n\nbody\n'
    with pytest.raises(ValueError):
        ensure_platform_frontmatter(text, "youtube")


def test_migration_preserves_processed():
    text = '---\ntitle: "T"\nprocessed: 2026-07-24\n---\n\nbody\n'
    assert "processed: 2026-07-24" in ensure_platform_frontmatter(text, "youtube")
