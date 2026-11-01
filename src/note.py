import json
import logging
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path

log = logging.getLogger("yt-obsidian")

def write_private_note(path, content):
    """Replace a contextualized note atomically with owner-only permissions."""
    path = Path(path)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".sure-note-")
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)

_STRIP_CHARS = str.maketrans("", "", '#^[]*?"<>')
# Only a trailing run of "#tag" tokens (e.g. "#shorts #finance" at the end of
# a title) is a hashtag tail to drop whole. Anchoring to the end keeps a
# mid-title "#" (C#, #1 Mistake, #4021) from being eaten as a token -- those
# fall through to _STRIP_CHARS below, which removes just the '#' character.
_HASHTAG = re.compile(r"(?:\s#\w+)+$")
# A title cut mid-phrase reads worse than a shorter one, so a trailing
# function word is dropped after truncation rather than left dangling.
_DANGLING = {"a", "an", "the", "of", "by", "for", "to", "and", "or", "with",
             "from", "in", "on", "at", "is", "are", "was", "that", "this",
             "how", "why", "what", "its", "it's", "your", "you"}

_SOURCE_LABELS = {
    "youtube": "YouTube",
    "threads": "Threads",
    "instagram": "Instagram",
    "facebook": "Facebook",
    "twitter": "X",
    "generic": "Web",
}

def source_label(platform: str) -> str:
    # These strings land in filenames, so an unknown platform must map to an
    # existing label rather than inventing a new one -- a typo'd or newly
    # added platform would otherwise silently create a second vocabulary.
    return _SOURCE_LABELS.get((platform or "").strip().lower(), "Web")

def sanitize_title(title: str, limit: int = 78) -> str:
    # ':' carries meaning in a title ("ZTA: Zero Token Architecture"), so it
    # becomes a separator rather than being deleted. '/' and '|' cannot: '/'
    # is a path separator and '|' is Obsidian's wikilink alias separator.
    s = _HASHTAG.sub("", title or "")
    s = s.replace(":", " - ")
    s = re.sub(r"[/\\|]", "-", s)
    s = s.translate(_STRIP_CHARS)
    s = re.sub(r"-{2,}", "-", s)
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"(?: - ){2,}", " - ", s)
    s = s.strip(" -.,")
    if len(s) > limit:
        s = s[:limit].rsplit(" ", 1)[0] or s[:limit]
        if s.count("(") > s.count(")"):
            s = s[:s.rindex("(")]
        words = s.split()
        while words and words[-1].lower().strip(".,-—") in _DANGLING:
            words.pop()
        s = " ".join(words).strip(" -.,—")
    return s or "Untitled"

_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")

def now_stamp() -> str:
    """The moment this note is being written, to the second.

    Separate from `processed:` because that one is stamped once per run and
    doubles as the filename prefix, so every note from a single run carries an
    identical date and nothing inside a run can be ordered. This is read at
    note-build time instead, which is per item.

    Local wall clock with no offset suffix: that is Obsidian's native
    "Date & time" property format, and it is what a Base sorts on as a
    datetime rather than as a string."""
    return datetime.now().replace(microsecond=0).isoformat()

def content_filename(processed: str, title: str, platform: str) -> str:
    # processed, not published: a capture tree is browsed as "what did I add
    # recently", and filing an old video under its publish date scatters it
    # away from everything saved the same week. published: stays in
    # frontmatter for the Base to sort on.
    date = processed if processed and _DATE.fullmatch(processed) else "undated"
    return f"{date} {sanitize_title(title)} ({source_label(platform)}).md"

def topic_folder(topic: str, allowed: list[str]) -> str:
    return topic if topic in allowed else "Other"

def is_safe_path_component(value: str) -> bool:
    # A brain name from config.env reaches the filesystem as a path
    # component, so a hand-edited or typo'd config must never be able to
    # steer it outside the vault. Reject rather than sanitize-and-hope.
    if not value or not value.strip():
        return False
    if value != value.strip():
        return False
    if len(value) > 255:
        return False
    if any(ord(c) < 32 for c in value):
        return False
    if "/" in value or "\\" in value:
        return False
    if ".." in value:
        return False
    if value.startswith("."):
        return False
    if ":" in value:  # e.g. a Windows drive letter like "C:"
        return False
    return True

GENERAL = "General"

def destination_dir(topic: str, subtopic: str, allowed: list[str],
                    subtopic_map: dict[str, list[str]], root: str) -> str:
    """Vault-relative folder for a note: "<root>/<topic>[/<subtopic>]".

    A topic with no configured subtopic list is two levels deep; a topic with
    one is three, using General when the model names something unlisted. Kind
    (transcripts/threads/articles) no longer appears: it described where a note
    came from, and this tree is organized by what it is about. `type:` in
    frontmatter still records it."""
    folder = topic_folder(topic, allowed)
    if not is_safe_path_component(root) or not is_safe_path_component(folder):
        return "Other"
    subs = subtopic_map.get(folder)
    if not subs:
        return f"{root}/{folder}"
    chosen = subtopic if subtopic in subs else GENERAL
    if not is_safe_path_component(chosen):
        chosen = GENERAL
    return f"{root}/{folder}/{chosen}"

def safe_vault_dir(vault_dir, folder: str) -> Path:
    # destination_dir's bare-folder fallback (an unmapped topic) comes
    # straight from the hand-edited TOPICS= list, which is_safe_path_component
    # never validates -- a topic string like "../../etc" would otherwise reach
    # this join untouched. Containment is checked lexically (os.path.normpath)
    # on the *unresolved* join, not via Path.resolve(): resolve() follows
    # symlinks, so a legitimate in-vault symlink (e.g. a sub-vault pointing at
    # an external drive) would otherwise fail containment and get silently
    # redirected into Other/ -- exactly the failure mode this function exists
    # to prevent. "../" traversal and absolute paths are still caught because
    # normpath collapses them before the prefix check. No expanduser: a "~"
    # component must stay a literal in-vault folder name, not expand to the
    # home directory.
    vault_dir = Path(vault_dir).resolve()
    dest = Path(os.path.normpath(str(vault_dir / folder)))
    try:
        contained = dest == vault_dir or vault_dir in dest.parents
        if contained:
            # Create it here rather than probing with resolve(): this is the one
            # choke point that can degrade to Other/ instead of raising. resolve()
            # only surfaces the null-byte case -- realpath() swallows OSError on a
            # path that does not exist -- so a >255-char topic (ENAMETOOLONG) or a
            # DANGLING in-vault symlink (mkdir's exist_ok re-raises FileExistsError
            # when the target is not a directory) would sail past it and abort the
            # whole poll cycle, since poll.run has no per-item guard and runs
            # outside main's social try. Callers still mkdir(exist_ok=True); that
            # becomes a harmless no-op.
            dest.mkdir(parents=True, exist_ok=True)
            return dest
    except (ValueError, OSError) as e:
        log.warning("vault destination for folder %r is unusable (%s); "
                    "falling back to Other/", folder, e)
        return vault_dir / "Other"
    log.warning("topic folder %r resolved outside the vault %s; "
                "falling back to Other/", folder, vault_dir)
    return vault_dir / "Other"

def _yaml_str(value: str) -> str:
    # JSON string encoding is valid YAML and safely escapes quotes/backslashes.
    return json.dumps(value if value is not None else "", ensure_ascii=False)

def _tags_line(model_tags: list[str]) -> str:
    tags = ["youtube"] + [t for t in model_tags if t != "youtube"]
    return "tags: [" + ", ".join(_yaml_str(t) for t in tags) + "]"

def _frontmatter(video_meta: dict, model_meta: dict, status: str, processed: str,
                 processed_at: str | None = None) -> str:
    processed_at = processed_at or now_stamp()
    lines = [
        "---",
        f'title: {_yaml_str(video_meta.get("title", ""))}',
        f'channel: {_yaml_str(video_meta.get("channel", ""))}',
        f'url: {video_meta.get("url", "")}',
        'platform: youtube',
        'source_type: youtube',
        'content_kind: video',
        f'video_id: {video_meta.get("video_id", "")}',
        f'published: {video_meta.get("published", "")}',
        f'processed: {processed}',
        f'processed_at: {processed_at}',
        f'capture_why: {_yaml_str(video_meta.get("capture_why", ""))}',
        f'capture_context_at: {_yaml_str(video_meta.get("capture_context_at", ""))}',
        f'capture_source: {_yaml_str(video_meta.get("capture_source", "playlist"))}',
        f'duration: "{video_meta.get("duration", "")}"',
        f'category: {_yaml_str(model_meta.get("category", "other"))}',
        f'topic: {_yaml_str(model_meta.get("topic", "Other"))}',
        f'subtopic: {_yaml_str(model_meta.get("subtopic", "General"))}',
        _tags_line(model_meta.get("tags", [])),
        f'status: {status}',
        "---",
    ]
    return "\n".join(lines)

def _transcript_callout(transcript: str) -> str:
    quoted = "\n".join("> " + ln for ln in transcript.splitlines())
    return "> [!note]- Full transcript\n" + quoted

_CAP_SUFFIX = {"size cap": " (cut at the 5 MB size cap)", "page cap": " (first 300 pages)"}

def _source_text_callout(text: str, reason: str = "") -> str:
    quoted = "\n".join("> " + ln for ln in text.splitlines())
    return f"> [!note]- Full source text{_CAP_SUFFIX.get(reason, '')}\n" + quoted

def _capture_context(meta: dict) -> str:
    why = str(meta.get("capture_why") or "").strip()
    return f"## Why saved\n{why}\n\n" if why else ""

def _topics_section(topics: list[str]) -> str:
    return "## Topics\n" + " · ".join(f"[[{t}]]" for t in topics)

def build_note(video_meta, model_meta, body, transcript, processed,
               processed_at=None) -> str:
    fm = _frontmatter(video_meta, model_meta, "summarized", processed, processed_at)
    topics = model_meta.get("topics")
    topics_block = f"{_topics_section(topics)}\n\n" if topics else ""
    return (f"{fm}\n\n{_capture_context(video_meta)}{body.strip()}\n\n"
            f"{topics_block}{_transcript_callout(transcript)}\n")

def build_stub_note(video_meta, status, processed, processed_at=None) -> str:
    fm = _frontmatter(video_meta, {"category": "other", "tags": []}, status,
                      processed, processed_at)
    reason = {
        "no-transcript": "No captions were available for this video; not summarized.",
        "summary-failed": "Summarization failed after repeated attempts; not summarized.",
        "metadata-failed": "Video metadata could not be fetched after repeated attempts "
                           "(deleted, private, or invalid id); not summarized.",
        "captions-failed": "Caption download failed after repeated attempts "
                           "(rate limit or unavailable subtitles); not summarized.",
    }.get(status, "Not summarized.")
    return f"{fm}\n\n{reason}\n"

def _capture_lines(post_meta: dict) -> list[str]:
    """Only notes from the article reader carry these, so every other note
    keeps its frontmatter byte for byte."""
    if "extraction" not in post_meta:
        return []
    return [
        f'extraction: {_yaml_str(post_meta.get("extraction") or "")}',
        f'word_count: {int(post_meta.get("word_count") or 0)}',
        f'source_format: {_yaml_str(post_meta.get("source_format") or "")}',
    ]

def _social_frontmatter(post_meta: dict, model_meta: dict, status: str, processed: str,
                        processed_at: str | None = None) -> str:
    processed_at = processed_at or now_stamp()
    platform = post_meta.get("platform") or "social"
    source_type = post_meta.get("source_type") or ("web" if platform == "generic" else "social")
    content_kind = post_meta.get("content_kind") or model_meta.get("content_kind") or "other"
    tags = [platform] + [t for t in model_meta.get("tags", []) if t != platform]
    kind = post_meta.get("kind", "text")
    if kind is None:
        kind = "text"
    date = post_meta.get("date", "")
    if date is None:
        date = ""
    return "\n".join([
        "---",
        f'title: {_yaml_str(post_meta.get("title", ""))}',
        f'platform: {_yaml_str(platform)}',
        f'source_type: {_yaml_str(source_type)}',
        f'content_kind: {_yaml_str(content_kind)}',
        f'author: {_yaml_str(post_meta.get("author", ""))}',
        f'url: {_yaml_str(post_meta.get("url", ""))}',
        # only meaningful for a url-less share: resolve_collision keys on this
        # instead of `url` when there is no url to key on (see _write_social_note).
        f'item_key: {_yaml_str(post_meta.get("item_key", ""))}',
        f'date: {_yaml_str(date)}',
        f'processed: {processed}',
        f'processed_at: {processed_at}',
        f'capture_why: {_yaml_str(post_meta.get("capture_why", ""))}',
        f'capture_context_at: {_yaml_str(post_meta.get("capture_context_at", ""))}',
        f'capture_source: {_yaml_str(post_meta.get("capture_source", "share"))}',
        f'type: {_yaml_str(kind)}',
        *_capture_lines(post_meta),
        f'category: {_yaml_str(model_meta.get("category", "other"))}',
        f'topic: {_yaml_str(model_meta.get("topic", "Other"))}',
        f'subtopic: {_yaml_str(model_meta.get("subtopic", "General"))}',
        "tags: [" + ", ".join(_yaml_str(t) for t in tags) + "]",
        f'status: {_yaml_str(status)}',
        "---",
    ])

def build_social_note(post_meta, model_meta, body, transcript, processed,
                      processed_at=None, source_text: str = "") -> str:
    fm = _social_frontmatter(post_meta, model_meta, "summarized", processed,
                             processed_at)
    tail = f"\n\n{_transcript_callout(transcript)}" if transcript else ""
    if source_text:                  # the full text stays even when the model saw less
        tail += f"\n\n{_source_text_callout(source_text, post_meta.get('extraction_reason') or '')}"
    return f"{fm}\n\n{_capture_context(post_meta)}{body.strip()}{tail}\n"

def _frontmatter_value(path: Path, key: str) -> str | None:
    """The value of one frontmatter key, or None if the file is missing,
    unreadable, or has no frontmatter. Read rather than parsed: this only
    needs one scalar and must never raise on a hand-edited note."""
    try:
        text = path.read_text()
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    head = text[3:end] if end != -1 else text[3:]
    m = re.search(rf"^{re.escape(key)}:\s*(.*)$", head, re.M)
    return m.group(1).strip().strip('"') if m else None

def resolve_collision(path: Path, identity: str, identity_key: str) -> Path:
    """Where to actually write, given a name that may already be taken.

    Same identity -> the same path, so reprocessing rewrites its own note.
    Different identity -> ' (2)', ' (3)', ... The filename no longer carries
    the video id, so two items sharing a title, save date and source would
    otherwise land on one name and one would vanish with no error."""
    path = Path(path)
    if not path.exists():
        return path
    if _frontmatter_value(path, identity_key) == identity:
        return path
    stem, suffix = path.stem, path.suffix
    n = 2
    while True:
        candidate = path.with_name(f"{stem} ({n}){suffix}")
        if not candidate.exists():
            log.warning("filename %r is taken by a different item; writing %r "
                        "instead", path.name, candidate.name)
            return candidate
        if _frontmatter_value(candidate, identity_key) == identity:
            return candidate
        n += 1

def build_social_stub_note(post_meta, status, shared_text, processed,
                           processed_at=None) -> str:
    fm = _social_frontmatter(post_meta, {"category": "other", "tags": []}, status,
                             processed, processed_at)
    if post_meta.get("stub_reason"):
        reason = post_meta["stub_reason"]
    elif status == "partial":
        why = post_meta.get("extraction_reason") or "unknown reason"
        reason = f"Only part of this page could be fetched ({why}); saved for manual review."
    else:
        reason = {
            "fetch-failed": "Could not fetch this post; saved for manual review.",
            "summary-failed": "Summarization failed after repeated attempts.",
            "invalid": "This share arrived without a usable url; saved for manual review.",
        }.get(status, "Not summarized.")
    shared = f"\n\n> Shared note: {shared_text}" if shared_text else ""
    return (f"{fm}\n\n{_capture_context(post_meta)}{reason}\n\n"
            f"Original: {post_meta.get('url', '')}{shared}\n")
