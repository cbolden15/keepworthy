#!/usr/bin/env python3
"""One-time migration: YouTube Summaries/*.md, legacy root topic folders and
each brain's raw/transcripts/ -> <CONTENT_ROOT>/<topic>/<subtopic>/.

Usage:
    python3 migrate_notes.py            # dry run: classify, print and save the plan
    python3 migrate_notes.py --apply    # backup vault, then execute that plan

This is the only script here that moves files inside the vault, so it is
deliberately timid: dry run unless you pass --apply, a full tarball backup
before the first move (to a name it refuses to overwrite), never overwrite an
existing destination, never unlink a note before its copy is safely in place,
and never remove the source folder unless it is genuinely empty. A note whose
classification fails is filed under Other/ rather than dropped, and one failed
move does not abort the rest.

The dry run is also the only phase that classifies. It writes its decisions to
a plan file and --apply executes exactly that file, so the listing the operator
reviewed is the listing that runs.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
import time
from collections import Counter
from pathlib import Path

from src.config import allowed_topics, load_config
from src.note import (
    GENERAL,
    content_filename,
    destination_dir,
    is_safe_path_component,
    resolve_collision,
    safe_vault_dir,
    topic_folder,
)
from src.summarize import run_claude

# The legacy flat folder this migration drains. Deliberately not cfg.subfolder:
# it names the layout that already exists on disk, not wherever new notes go.
LEGACY_SUBFOLDER = "YouTube Summaries"

# Measured against the real vault: the same note classified three times in a
# row came back Other, then AI & Dev, then Business & Marketing. One call is
# not reproducible enough to write into a plan the operator only reviews
# once, so the dry run asks CLASSIFY_VOTES times per note and takes the
# majority -- see _majority_classification.
CLASSIFY_VOTES = 3


_BRAINS = ("dev-brain", "business-brain", "personal-brain")


def _source_dirs(vault_dir: Path, allowed: list[str], content_root: str) -> list[Path]:
    """Every folder this migration drains: the legacy flat backlog, each root
    topic folder the pipeline wrote before this change, and each brain's
    raw/transcripts. Scoped to raw/transcripts deliberately -- raw/articles
    holds hand-placed audit reports and strategy docs that predate ingest and
    must not be swept. The content root is never a source; it is the
    destination.

    Shared by plan_migration (what to sweep), read_plan (what a plan entry's
    source is allowed to resolve into) and the apply-time stranded-note check
    (every one of these folders is meant to end up empty)."""
    dirs = [vault_dir / LEGACY_SUBFOLDER]
    # a brain's own name is excluded at the top level for the same reason as
    # raw/articles: a hand-edited TOPICS= repeating one (TOPICS=AI & Dev,
    # dev-brain,Personal) must not turn a sub-vault's hand-placed pages into
    # migration sources. Only its raw/transcripts is swept. `allowed` comes
    # straight from a hand-edited TOPICS=, so it is filtered through
    # is_safe_path_component the same as every other path component reaching
    # the filesystem here -- a typo'd `TOPICS=..` must not make the vault's
    # own parent directory a recognized source folder.
    dirs += [vault_dir / name for name in allowed
             if name != content_root and name not in _BRAINS
             and is_safe_path_component(name)]
    dirs += [vault_dir / b / "raw" / "transcripts" for b in _BRAINS]
    return dirs

_TOPIC_LINE = re.compile(r"^topic:\s*(.+)$", re.M)
# anchored, so it never matches the `subtopic:` line -- and `_TOPIC_LINE`'s own
# anchor is what keeps `subtopic:` out of it
_SUBTOPIC_LINE = re.compile(r"^subtopic:\s*(.+)$", re.M)
_BOM = "\ufeff"


def _is_fence(line: str) -> bool:
    return line.strip() == "---"


def _split_frontmatter(text: str):
    """Return (frontmatter, remainder) for a note whose FIRST LINE is a `---`
    fence, else None. Fences are matched line by line, never as substrings: our
    own build_note writes titles verbatim, so a video called
    `Rust --- the good parts` puts `---` inside a YAML value, and a substring
    split would cut the block mid-value and corrupt the note. `remainder`
    starts at the closing fence line, so everything from there on — body
    included — is handed back byte for byte."""
    lines = text.splitlines(keepends=True)
    if not lines or not _is_fence(lines[0]):
        return None
    for i in range(1, len(lines)):
        if _is_fence(lines[i]):
            return "".join(lines[1:i]), "".join(lines[i:])
    return None


def frontmatter_problem(text: str) -> str | None:
    """Describe why a note's frontmatter cannot be edited safely, else None.
    Each of these reads as "no frontmatter" to a naive check, so wrapping a
    fresh block around it would demote the note's real keys into the body."""
    if text.startswith(_BOM):
        return "starts with a UTF-8 BOM"
    lines = text.splitlines()
    if lines and _is_fence(lines[0]):
        if not any(_is_fence(ln) for ln in lines[1:]):
            return "opening fence is never closed"
        return None
    for ln in lines:
        if ln.strip():
            return "fence is preceded by a blank line" if _is_fence(ln) else None
    return None


def _existing_topic(text: str) -> str | None:
    split = _split_frontmatter(text)
    if split is None:
        return None
    m = _TOPIC_LINE.search(split[0])
    if not m:
        return None
    value = m.group(1).strip()
    # note.py's _yaml_str writes topic as a JSON-quoted scalar (json.dumps is
    # valid YAML); undo that here so a real note's topic still matches an
    # entry in `allowed`, which is a list of bare strings.
    if value.startswith('"'):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            pass
    return value


def ensure_topic_frontmatter(text: str, topic: str, subtopic: str = GENERAL) -> str:
    """Set `topic:` and `subtopic:` in the note's frontmatter, leaving every
    other key and the body untouched. Any existing topic/subtopic lines are
    dropped first, so a note that already carries one ends up with exactly one.
    Callers must screen the note with frontmatter_problem() first: the
    no-frontmatter branch below wraps, and wrapping a malformed block buries
    it."""
    # JSON-quoted, matching note.py's _yaml_str: new notes already write topic
    # this way, and an unquoted value here would make a migrated note look
    # unclassified on a later run (_existing_topic reads both forms so a
    # rerun over already-migrated notes stays idempotent either way).
    quoted = json.dumps(topic, ensure_ascii=False)
    quoted_sub = json.dumps(subtopic, ensure_ascii=False)
    split = _split_frontmatter(text)
    if split is None:
        # a note without frontmatter still gets filed; it just gains a block
        return (f"---\ntopic: {quoted}\nsubtopic: {quoted_sub}\n---\n\n"
                f"{text.lstrip()}")
    fm, rest = split
    # splitlines() drops \r as well as \n, so a CRLF note keeps its own ending
    # instead of gaining a stray blank line at the top of the block
    nl = "\r\n" if text.startswith("---\r\n") else "\n"
    kept = [ln for ln in fm.splitlines()
            if not _TOPIC_LINE.match(ln) and not _SUBTOPIC_LINE.match(ln)]
    kept.append(f"topic: {quoted}")
    kept.append(f"subtopic: {quoted_sub}")
    return "---" + nl + nl.join(kept) + nl + rest


def ensure_platform_frontmatter(text: str, platform: str) -> str:
    """Add `platform:` to a note that predates the field. Every swept note came
    from the YouTube poller, so the value is asserted rather than inferred: a
    note already declaring a different platform means that assumption is wrong
    and the run must stop rather than mislabel it."""
    split = _split_frontmatter(text)
    if not split:
        raise ValueError("note has no frontmatter")
    head, body = split
    existing = re.search(r"^platform:\s*(.*)$", head, re.M)
    if existing:
        if existing.group(1).strip().strip('"') != platform:
            raise ValueError(
                f"note declares platform {existing.group(1)!r}, expected {platform!r}")
        return text
    # `body` already opens with the closing fence (see _split_frontmatter), and
    # a CRLF note keeps its own line endings, exactly as in
    # ensure_topic_frontmatter
    nl = "\r\n" if text.startswith("---\r\n") else "\n"
    return "---" + nl + head.rstrip("\r\n") + nl + f"platform: {platform}" + nl + body


def _frontmatter_field(text: str, key: str) -> str | None:
    """One frontmatter scalar, unquoted, or None when the note has no
    frontmatter or no such key. Never raises: this reads hand-edited notes."""
    split = _split_frontmatter(text)
    if split is None:
        return None
    m = re.search(rf"^{re.escape(key)}:\s*(.*)$", split[0], re.M)
    if not m:
        return None
    value = m.group(1).strip()
    # note.py's _yaml_str writes a scalar JSON-quoted (json.dumps is valid
    # YAML), so a title containing a literal `"` comes back as `\"` -- undo
    # that the same way _existing_topic does, or the escaping survives into
    # sanitize_title, which maps the stray backslash to '-'.
    if value.startswith('"'):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            pass
    return value.strip('"')


_TOPIC_PREFIX = re.compile(r"^topic\s*:\s*", re.I)
_REPLY_NOISE = "\"'`*[]() \t.,;:!?"


def normalize_topic(reply: str, allowed: list[str]) -> str | None:
    """Map a model reply onto an allowed topic, or None when it cannot be read.
    The prompt asks for a bare topic line and the model mostly obliges, but
    `"Finance"`, `**Finance**`, `Topic: Finance`, `finance.` and a topic
    followed by a paragraph of reasoning all turn up, and every one of them
    used to land in Other/ indistinguishably from a real Other."""
    # Scan every line and keep the LAST exact match, not the first: measured
    # against the real vault, the model routinely writes a sentence of reasoning
    # first and puts the answer on a later line ("...squarely about insulin
    # resistance, ketogenic diets, and metabolic health.\n\nHealth & Fitness").
    # Reading only the first line filed those correctly-classified notes under
    # Other/. Matching is whole-line after cleanup, so a topic merely NAMED
    # inside a reasoning sentence never matches -- only a line that is the topic.
    found = None
    for raw in reply.splitlines():
        if not raw.strip():
            continue
        line = _TOPIC_PREFIX.sub("", raw.strip(_REPLY_NOISE)).strip(_REPLY_NOISE)
        for t in allowed:
            if t.lower() == line.lower():
                found = t
    return found


def normalize_classification(reply: str, allowed: list[str],
                             subtopic_map: dict[str, list[str]]) -> tuple[str, str]:
    """Parse the classifier's two-line reply into (topic, subtopic).

    An unreadable topic yields Other, matching the old single-line behaviour --
    which is normalize_topic itself, still used for the fallback, so a reply
    that arrives bare, quoted, bolded or trailing a paragraph of reasoning is
    read exactly as well as it was before the contract grew a second line.
    An unreadable or unlisted subtopic yields General rather than failing the
    note: General is always a valid destination, and a note in the right topic
    under General is recoverable by hand, while a note in the wrong topic is
    not."""
    topic, subtopic = None, None
    for line in (reply or "").splitlines():
        key, _, val = line.partition(":")
        key, val = key.strip().lower(), val.strip()
        if key == "topic" and val in allowed:
            topic = val
        elif key == "subtopic":
            # the same bolding/quoting noise normalize_topic strips from the
            # topic line (`**Finance**`) reaches this line too -- a
            # classifier run that bolds one bolds the other, and unlike
            # topic there is no normalize_topic-style fallback for subtopic,
            # so an un-stripped value falls straight through to General.
            subtopic = val.strip(_REPLY_NOISE)
    if topic is None:
        topic = normalize_topic(reply or "", allowed) or "Other"
    subs = subtopic_map.get(topic) or []
    if not subs or subtopic not in subs:
        subtopic = GENERAL
    return topic, subtopic


_TRANSCRIPT_MARK = "[!note]- Full transcript"


def _classifier_excerpt(text: str, limit: int = 4000) -> str:
    """The part of a note that actually identifies its subject: frontmatter
    plus body, with the transcript callout (and everything after it) cut off
    before truncating to `limit` characters. Every YouTube note ends with the
    full speech-to-text transcript inside a collapsed callout -- that's how an
    18 KB note becomes 183 KB -- so handing the classifier the whole file
    drowns the actual subject in transcript noise. Against the real vault that
    put six notes in Other/, with a different wrong answer on every re-run.
    Never raises: a note missing frontmatter, a callout, or a body, an
    unclosed fence, a BOM, or CRLF endings all just fall through to "use
    what's there"."""
    idx = text.find(_TRANSCRIPT_MARK)
    if idx != -1:
        # cut the whole line the marker sits on (it's blockquoted, "> [!note]-
        # Full transcript"), not just the marker substring, so no "> " orphan
        # is left dangling at the end of the excerpt
        text = text[:text.rfind("\n", 0, idx) + 1]
    if len(text) <= limit:
        return text
    # A raw character slice can land mid-frontmatter and cut the title line in
    # half. Keep the whole frontmatter block intact -- even past `limit` if it
    # has to -- and spend the character budget on the body instead.
    split = _split_frontmatter(text)
    if split is None:
        return text[:limit]
    fm, rest = split
    rest_lines = rest.splitlines(keepends=True)
    closing_fence = rest_lines[0] if rest_lines else ""
    head = text[:len(text) - len(rest)] + closing_fence
    body = "".join(rest_lines[1:])
    return head + body[:max(0, limit - len(head))]


def _destination_for(cfg, allowed, topic: str, subtopic: str) -> str:
    """The vault-relative folder this note belongs in, given its already
    decided topic and subtopic. Kind no longer appears in the path, so the
    note itself no longer has to be read to work it out."""
    return destination_dir(topic, subtopic, allowed, cfg.subtopics,
                           cfg.content_root)


def _classify_vote(classify, text: str, name: str, allowed: list[str],
                   subtopic_map: dict[str, list[str]]) -> tuple[str, str]:
    """One classification vote, normalized to an allowed (topic, subtopic).
    Mirrors the single-shot path's Other fallback exactly: a raised exception
    or a reply normalize_classification can't read still returns a vote --
    (Other, General) -- rather than crashing or dropping the note. Called
    CLASSIFY_VOTES times per note, so one bad vote just gets outvoted instead
    of deciding the note alone."""
    try:
        reply = classify(text)
    except Exception as e:  # the classifier shells out; never lose a note over it
        print(f"warning: could not classify {name} ({e}); filing under Other",
              file=sys.stderr)
        return "Other", GENERAL
    topic, subtopic = normalize_classification(reply, allowed, subtopic_map)
    # `a.md -> Other/` must not read the same whether the model chose Other or
    # the parse failed, so an unreadable reply says so -- a deliberate Other
    # stays quiet.
    if topic == "Other" and reply.strip() and normalize_topic(reply, allowed) is None:
        print(f"warning: could not read a topic for {name} from {reply!r}; "
              "filing under Other", file=sys.stderr)
    return topic, subtopic


def _majority_classification(votes: list[tuple[str, str]]) -> tuple[str, str, bool]:
    """Majority topic, then the majority subtopic among the votes that agreed
    on that topic. Voting on the pair jointly would let a minority topic win a
    subtopic, which is how a note lands in a folder its own topic does not
    have. Counter breaks a tie by first appearance, so a three-way
    disagreement still yields the first vote and the caller still reports the
    note as low-confidence."""
    topics = [t for t, _ in votes]
    topic = Counter(topics).most_common(1)[0][0]
    subs = [s for t, s in votes if t == topic]
    subtopic = Counter(subs).most_common(1)[0][0]
    return topic, subtopic, len(set(votes)) == 1


def plan_migration(cfg, project_root, classify, low_confidence=None) -> list:
    """Decide where every swept note belongs: the legacy backlog, the root
    topic folders the pipeline wrote before this change, and each brain's
    raw/transcripts (see _source_dirs). Reads only; moves nothing.

    Every note is classified CLASSIFY_VOTES times and filed under the majority
    answer (see _majority_classification) -- including one that already
    carries a `topic:`. Reusing that value would be cheaper, but every note in
    the backlog was classified before `Career` existed and before subtopics
    existed at all, so trusting it would file the whole vault under stale
    topics with no subtopic while reporting success. When `low_confidence` is
    a list, every note whose votes were not unanimous is appended to it as
    (name, votes) -- the short list the operator actually needs to review, not
    the whole plan."""
    vault_dir = Path(cfg.vault_dir)
    allowed = allowed_topics(cfg)
    src_dirs = _source_dirs(vault_dir, allowed, cfg.content_root)
    plan = []
    for src_dir in src_dirs:
        if not src_dir.is_dir():
            continue
        for p in sorted(src_dir.glob("*.md")):
            try:
                text = p.read_text()
            except (OSError, ValueError) as e:
                # a *.md directory, a dangling symlink, non-UTF-8 bytes: one bad
                # entry must not abort the plan for the other forty notes
                print(f"warning: could not read {p.name} ({e}); left in place", file=sys.stderr)
                continue
            problem = frontmatter_problem(text)
            if problem:
                print(f"warning: skipping {p.name}: {problem}; left in place", file=sys.stderr)
                continue
            votes = [_classify_vote(classify, text, p.name, allowed, cfg.subtopics)
                     for _ in range(CLASSIFY_VOTES)]
            topic, subtopic, unanimous = _majority_classification(votes)
            if not unanimous and low_confidence is not None:
                low_confidence.append((p.name, votes))
            dest = _destination_for(cfg, allowed, topic, subtopic)
            if dest == str(p.parent.relative_to(vault_dir)):
                # every destination is under cfg.content_root, and
                # _source_dirs never sweeps the content root, so a swept
                # note's parent can no longer equal its own destination. Kept
                # as a cheap guard in case that invariant ever changes.
                continue
            plan.append((p, topic, subtopic))
    return plan


def apply_migration(cfg, plan) -> dict:
    """Move each planned note into its <content root>/<topic>/<subtopic>
    destination, under its new date-prefixed readable name. Returns
    {"moved", "skipped", "failed"} so the caller can report exactly what
    happened.

    The note is rewritten as well as moved -- its frontmatter gains
    `platform:` and `subtopic:` -- but never restamped: `processed:` is the
    save date the new filename is built from, and rewriting it would destroy
    the one signal this layout exists to expose."""
    allowed = allowed_topics(cfg)
    result = {"moved": [], "skipped": [], "failed": []}
    touched_dirs = set()
    # destinations this call has already written. resolve_collision's
    # same-video_id-means-reprocessing rule is right for the pipeline, but a
    # migration can hand it two genuinely distinct source files for the same
    # video (e.g. one swept from the legacy backlog, one from a brain's
    # raw/transcripts) with the same title and processed date. The second
    # one's resolve_collision call would see the first one's freshly written
    # file on disk, read back its own video_id, and treat it as a match --
    # silently overwriting a different note. Force the next free suffix
    # instead whenever a destination collides with one this run already used.
    written = set()
    for p, topic, subtopic in plan:
        touched_dirs.add(p.parent)
        try:
            text = p.read_text()
        except (OSError, ValueError) as e:
            # UnicodeDecodeError is a ValueError, and it used to escape here and
            # take the whole move report with it
            result["failed"].append((p, str(e)))
            continue
        problem = frontmatter_problem(text)
        folder = _destination_for(cfg, allowed, topic, subtopic)
        if problem:
            preview = Path(cfg.vault_dir) / folder / p.name
            result["skipped"].append((p, preview, f"malformed frontmatter: {problem}"))
            continue
        out_dir = safe_vault_dir(cfg.vault_dir, folder)
        tmp = None
        try:
            updated = ensure_platform_frontmatter(text, "youtube")
            updated = ensure_topic_frontmatter(updated, topic, subtopic)
            title = _frontmatter_field(updated, "title") or p.stem
            processed = _frontmatter_field(updated, "processed") or ""
            # resolve_collision, not a bare exists() check: the filename no
            # longer carries the video id, so two notes sharing a title and a
            # save date would otherwise be one name and one of them would be
            # dropped. A name already taken by a different note yields " (2)".
            dest = resolve_collision(
                out_dir / content_filename(processed, title, "youtube"),
                _frontmatter_field(updated, "video_id") or p.stem, "video_id")
            if dest in written:
                collided_with, stem, suffix = dest, dest.stem, dest.suffix
                n = 2
                while True:
                    candidate = dest.with_name(f"{stem} ({n}){suffix}")
                    if candidate not in written and not candidate.exists():
                        dest = candidate
                        break
                    n += 1
                print(f"warning: {p} would overwrite {collided_with}, already "
                      f"written earlier in this run; writing {dest.name} instead",
                      file=sys.stderr)
            tmp = dest.with_name(dest.name + ".migrating")
            # write-then-rename, so a half-written note can never take the
            # destination's name and be mistaken for a completed move; the
            # source is unlinked only once its copy is safely in place
            tmp.write_text(updated)
            os.replace(tmp, dest)
            p.unlink()
            written.add(dest)
            result["moved"].append((p, dest))
        except (OSError, ValueError) as e:
            # a note declaring a platform other than youtube raises here: it did
            # not come from the poller, so it is reported and left in place
            # rather than mislabelled
            if tmp is not None:
                tmp.unlink(missing_ok=True)
            result["failed"].append((p, str(e)))
    for d in touched_dirs | {Path(cfg.vault_dir) / LEGACY_SUBFOLDER}:
        if d.exists() and not any(d.iterdir()):
            d.rmdir()
    return result


def _backup(vault_dir: Path, out_dir: Path | None = None) -> Path:
    out_dir = Path(out_dir) if out_dir is not None else Path.home()
    out = out_dir / f"brain-backup-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz"
    # the name has 1-second resolution, and a re-run in the same second must
    # never touch the only pre-migration copy of the vault. Failing here means
    # main() aborts before moving anything, which is the right end.
    if out.exists():
        raise FileExistsError(f"[Errno 17] File exists: '{out}'")
    # write to a partial name and rename into place only on success, so a
    # tf.add that fails part-way never leaves a truncated file at the real
    # name looking like a valid backup
    tmp = out.with_name(out.name + ".partial")
    try:
        with tarfile.open(tmp, "x:gz") as tf:
            tf.add(vault_dir, arcname="Brain",
                   filter=lambda ti: None if "/.stversions" in ti.name else ti)
        os.replace(tmp, out)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return out


def _digest(p: Path) -> str | None:
    """A note's contents at plan time, or None if it cannot be read now."""
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except OSError:
        return None


def write_plan(path: Path, plan) -> None:
    """Persist the dry run's decisions, digests included, so --apply can prove
    it is moving the same notes the operator reviewed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    entries = [{"source": str(p), "topic": t, "subtopic": s, "sha256": _digest(p)}
               for p, t, s in plan]
    path.write_text(json.dumps({"version": 1, "entries": entries}, indent=2) + "\n")


def read_plan(path: Path, cfg):
    """Return (plan, stale): the entries safe to execute, and the ones that
    vanished, were rewritten, or failed the trust boundary since the plan was
    written. The plan file is meant to be reviewed before --apply runs it, but
    the operator may hand-edit it, so every entry is revalidated here exactly
    as planning would have produced it: the topic is remapped through
    topic_folder() and the subtopic through the topic's own configured list,
    so an unrecognised, path-traversing, or absolute-path topic falls back to
    Other/ and an unlisted subtopic to General/ rather than creating a stray
    folder or escaping the vault, and the resolved source must be a direct
    child of one of the recognized source folders (see _source_dirs) -- not
    merely somewhere inside the vault: a hand-edited entry naming a file
    nested inside a sub-vault (e.g. dev-brain/pages/Curated Index.md) must be
    rejected even though it resolves under VAULT_DIR. The destination is a
    nested <content root>/<topic>/<subtopic> path and is separately
    re-verified against VAULT_DIR itself; a symlinked content root planted in
    the vault still gets caught, since .resolve() follows it. Syncthing can
    also touch this vault from two other devices between the review and the
    run, so a changed note is reported, never guessed at."""
    data = json.loads(path.read_text())
    vault_dir = Path(cfg.vault_dir).resolve()
    allowed = allowed_topics(cfg)
    source_dirs = {d.resolve() for d in _source_dirs(vault_dir, allowed, cfg.content_root)}
    plan, stale = [], []
    for e in data.get("entries", []):
        p = Path(e["source"])
        topic = topic_folder(e["topic"], allowed)
        subs = cfg.subtopics.get(topic) or []
        subtopic = e.get("subtopic") or GENERAL
        if subtopic not in subs:
            subtopic = GENERAL
        try:
            resolved_src = p.resolve()
        except (OSError, ValueError):
            # ValueError: a hand-edited source with an embedded NUL. Mark just
            # this entry stale rather than letting it abort the whole plan.
            resolved_src = None
        if resolved_src is None or resolved_src.parent not in source_dirs:
            stale.append((p, "source is not a recognized migration source folder"))
            continue
        digest = _digest(p)
        if digest is None:
            stale.append((p, "gone since the plan was written"))
            continue
        if digest != e.get("sha256"):
            stale.append((p, "changed since the plan was written"))
            continue
        try:
            p.read_text()  # still decodable? --apply is about to rewrite it
        except (OSError, ValueError):
            stale.append((p, "changed since the plan was written"))
            continue
        dest = vault_dir / destination_dir(topic, subtopic, allowed, cfg.subtopics,
                                           cfg.content_root) / p.name
        if not dest.resolve().is_relative_to(vault_dir):
            stale.append((p, "destination is outside the vault"))
            continue
        plan.append((p, topic, subtopic))
    return plan, stale


def _print_low_confidence(low_confidence: list) -> None:
    """The short list the operator actually needs to read: only the notes
    whose CLASSIFY_VOTES votes disagreed, with what each vote said, so
    reviewing the plan means reading a handful of lines instead of all of
    them. A clean run says so in one line rather than staying silent, which
    would read the same as "this ran before voting existed"."""
    print("\n--- notes needing review (classifier votes disagreed) ---")
    if not low_confidence:
        print("none: every note's votes were unanimous")
        return
    for name, votes in low_confidence:
        print(f"{name}: {', '.join(f'{t}/{s}' for t, s in votes)}")


def _dry_run(cfg, project_root, plan_path, runner) -> int:
    # An unreachable vault (typo'd VAULT_DIR, unmounted share) would otherwise
    # produce an empty plan, overwrite the reviewed one, and exit 0 -- and the
    # follow-up --apply would then also exit 0 with nothing migrated. Refuse
    # before write_plan can retire a good plan.
    if not Path(cfg.vault_dir).is_dir():
        print(f"vault directory {cfg.vault_dir} does not exist -- refusing to "
              f"plan (check VAULT_DIR in config.env)", file=sys.stderr)
        return 1
    prompt = (project_root / "prompts" / "classify.md").read_text()

    def classify(note_text: str) -> str:
        excerpt = _classifier_excerpt(note_text)
        return run_claude(prompt, excerpt, cfg.model, project_root, runner=runner,
                          sandbox=getattr(cfg, "model_sandbox", False)).strip()

    low_confidence = []
    plan = plan_migration(cfg, project_root, classify, low_confidence=low_confidence)
    allowed = allowed_topics(cfg)
    for p, topic, subtopic in plan:
        print(f"{p.name} -> {_destination_for(cfg, allowed, topic, subtopic)}/")
    # write unconditionally, even an empty plan: this supersedes whatever an
    # earlier run left on disk, so a stale plan referencing already-moved
    # notes never survives a clean dry run to alarm a later --apply
    try:
        write_plan(plan_path, plan)
    except OSError as e:
        print(f"could not write the plan to {plan_path}: {e}", file=sys.stderr)
        return 1
    if not plan:
        print("nothing to do: no notes left to migrate")
        return 0
    print(f"\nDry run: {len(plan)} notes. Plan written to {plan_path}")
    print("Review it, then re-run with --apply to migrate exactly this plan.")
    _print_low_confidence(low_confidence)
    return 0


def _apply_run(cfg, plan_path, backup_dir) -> int:
    if not plan_path.exists():
        print(f"no plan at {plan_path}: run the dry run first "
              "(python3 migrate_notes.py) and review what it prints.", file=sys.stderr)
        return 1
    try:
        plan, stale = read_plan(plan_path, cfg)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        print(f"unreadable plan at {plan_path}: {e}\nnothing was moved.", file=sys.stderr)
        return 1
    allowed = allowed_topics(cfg)
    for p, topic, subtopic in plan:
        print(f"{p.name} -> {_destination_for(cfg, allowed, topic, subtopic)}/")
    for p, why in stale:
        print(f"skipped {p.name}: {why}", file=sys.stderr)
    if not plan:
        print("nothing to do: no planned note is still where the plan left it")
        return 1 if stale else 0

    try:
        backup = _backup(Path(cfg.vault_dir), backup_dir)
    except Exception as e:
        print(f"backup failed: {e}\nnothing was moved.", file=sys.stderr)
        return 1
    print(f"backup written: {backup}")

    result = apply_migration(cfg, plan)
    print(f"moved {len(result['moved'])} notes")
    for p, dest, why in result["skipped"]:
        print(f"skipped {p.name}: {why} ({dest})", file=sys.stderr)
    for p, err in result["failed"]:
        print(f"FAILED {p.name}: {err}", file=sys.stderr)
    # every source folder, not just the legacy one: they all drain into the
    # content root now, so a note left in any of them by a skipped or failed
    # move counts as stranded exactly like a legacy leftover. Only note about
    # a folder when a *.md is actually still in it, never for non-note cruft
    # like a lone .DS_Store, so the message and the exit code agree.
    vault_dir = Path(cfg.vault_dir)
    stranded = []
    for d in _source_dirs(vault_dir, allowed, cfg.content_root):
        left = sorted(d.glob("*.md")) if d.exists() else []
        stranded.extend(left)
        if left:
            print(f"{d.relative_to(vault_dir)}/ kept: {len(left)} note(s) still "
                  "there", file=sys.stderr)
    # anything left behind is a non-zero exit, so a wrapper script cannot read
    # success while a note sits stranded in a source folder
    return 1 if (result["failed"] or result["skipped"] or stale or stranded) else 0


def main(argv=None, project_root=None, backup_dir=None, runner=subprocess.run) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true",
                    help="execute the plan the dry run wrote (default: plan only)")
    ap.add_argument("--plan", default=None,
                    help="plan file to write/execute (default: state/migration-plan.json)")
    args = ap.parse_args(argv)
    project_root = Path(project_root) if project_root else Path(__file__).resolve().parent
    cfg = load_config(project_root / "config.env")
    plan_path = Path(args.plan) if args.plan else project_root / "state" / "migration-plan.json"
    if args.apply:
        return _apply_run(cfg, plan_path, backup_dir)
    return _dry_run(cfg, project_root, plan_path, runner)


if __name__ == "__main__":
    sys.exit(main())
