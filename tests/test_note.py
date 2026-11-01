import re
from datetime import date, datetime
from pathlib import Path

import pytest
import yaml

from src.note import build_note, build_stub_note
from src.note import build_social_note, build_social_stub_note
from src.note import sanitize_title, source_label
from src.note import resolve_collision

VMETA = {"title": "Build a RAG agent!", "channel": "DevChannel",
         "url": "https://youtu.be/abc123", "video_id": "abc123",
         "published": "2026-07-18", "duration": "00:21:14"}
MMETA = {"category": "ai-dev", "tags": ["ai", "llm", "topic/rag"], "topics": ["RAG"]}

def test_sanitize_replaces_colon_with_spaced_dash():
    assert sanitize_title("ZTA: Zero Token Architecture") == "ZTA - Zero Token Architecture"

def test_sanitize_replaces_slash_and_pipe_with_dash():
    assert sanitize_title("a/b|c") == "a-b-c"

def test_sanitize_strips_obsidian_and_illegal_chars():
    # the delimiters go, the text they wrap stays -- deleting bracketed
    # content would discard real words from a title
    assert sanitize_title('He said "hi" [x] ^y *z?') == "He said hi x y z"

def test_sanitize_drops_hashtag_tails():
    assert sanitize_title("AI Financial OS for Business #shorts #finance") == \
        "AI Financial OS for Business"

def test_sanitize_preserves_mid_title_hashtag_tokens():
    # a mid-title '#' is not a hashtag tail, so only the '#' character itself
    # is stripped -- the word chars it was attached to stay in the title
    assert sanitize_title("Learn C# and F#") == "Learn C and F"
    assert sanitize_title("The #1 Mistake") == "The 1 Mistake"
    assert sanitize_title("Fixing #4021 in Production") == "Fixing 4021 in Production"

def test_sanitize_preserves_legal_unicode():
    # em dash and curly apostrophe are legal in filenames and read correctly
    assert sanitize_title("Alex Hormozi's Warning — Stop Chasing AI") == \
        "Alex Hormozi's Warning — Stop Chasing AI"

def test_sanitize_truncates_on_word_boundary_without_dangling_word():
    got = sanitize_title(
        "GitHub - Imbad0202/academic-research-skills: Academic Research Skills "
        "for Claude Code: research to synthesis")
    assert got == "GitHub - Imbad0202-academic-research-skills - Academic Research Skills"
    assert len(got) <= 78

def test_sanitize_drops_unmatched_open_paren_when_truncating():
    got = sanitize_title(
        "Graph Engineering with Claude Code: 14 Steps From 0 to Graph Architect "
        "(Full Course Included)")
    assert got == "Graph Engineering with Claude Code - 14 Steps From 0 to Graph Architect"

def test_sanitize_empty_becomes_untitled():
    assert sanitize_title("") == "Untitled"
    assert sanitize_title("///") == "Untitled"

def test_source_label_maps_known_platforms():
    assert source_label("youtube") == "YouTube"
    assert source_label("twitter") == "X"
    assert source_label("generic") == "Web"

def test_source_label_unknown_falls_back_to_web():
    assert source_label("mastodon") == "Web"
    assert source_label("") == "Web"

def test_content_filename_uses_processed_date_not_published():
    from src.note import content_filename
    got = content_filename("2026-07-25", "ZTA: Zero Token Architecture", "youtube")
    assert got == "2026-07-25 ZTA - Zero Token Architecture (YouTube).md"

def test_content_filename_labels_the_platform():
    from src.note import content_filename
    assert content_filename("2026-07-30", "A Post", "twitter") == \
        "2026-07-30 A Post (X).md"
    assert content_filename("2026-07-30", "A Post", "generic") == \
        "2026-07-30 A Post (Web).md"

def test_content_filename_missing_date_is_undated():
    from src.note import content_filename
    assert content_filename("", "A Post", "threads") == "undated A Post (Threads).md"

def test_content_filename_missing_title_is_untitled():
    from src.note import content_filename
    assert content_filename("2026-07-30", "", "instagram") == \
        "2026-07-30 Untitled (Instagram).md"

def test_content_filename_rejects_a_traversal_processed_value():
    from src.note import content_filename
    # migration feeds this arbitrary note frontmatter, unlike the pipeline
    # where processed is always date.today().isoformat()
    got = content_filename("../../../../Desktop/x", "A Post", "youtube")
    assert got == "undated A Post (YouTube).md"

def test_content_filename_rejects_a_slashed_date():
    from src.note import content_filename
    got = content_filename("2026/07/30", "A Post", "youtube")
    assert got == "undated A Post (YouTube).md"

def test_content_filename_has_no_filesystem_hostile_characters():
    from src.note import content_filename
    got = content_filename("2026-07-24",
                           "GitHub - a/b: c | d", "youtube")
    for ch in ':/\\|#^[]*?"<>':
        assert ch not in got

def test_build_note_has_frontmatter_and_transcript():
    note = build_note(VMETA, MMETA, "## TL;DR\nx", "line one\nline two", "2026-07-20")
    assert note.startswith("---\n")
    assert 'title: "Build a RAG agent!"' in note
    assert 'category: "ai-dev"' in note
    assert "status: summarized" in note
    assert 'duration: "00:21:14"' in note
    # youtube tag prepended, model tags preserved
    assert 'tags: ["youtube", "ai", "llm", "topic/rag"]' in note
    assert "## TL;DR" in note
    assert "> [!note]- Full transcript" in note
    assert "> line one" in note and "> line two" in note

def test_build_note_preserves_capture_why_as_frontmatter_and_visible_context():
    meta = dict(VMETA, capture_why="Try this in my agent workflow.",
                capture_context_at="2026-08-13T10:00:00Z", capture_source="share")
    note = build_note(meta, MMETA, "## TL;DR\nx", "line one", "2026-07-20")
    assert 'capture_why: "Try this in my agent workflow."' in note
    assert 'capture_context_at: "2026-08-13T10:00:00Z"' in note
    assert 'capture_source: "share"' in note
    assert "## Why saved\nTry this in my agent workflow." in note
    assert note.index("## Why saved") < note.index("## TL;DR")

def test_build_stub_note_marks_status_and_omits_transcript():
    note = build_stub_note(VMETA, "no-transcript", "2026-07-20")
    assert "status: no-transcript" in note
    assert "Full transcript" not in note
    assert 'tags: ["youtube"]' in note

def test_build_stub_note_explains_a_caption_download_failure():
    note = build_stub_note(VMETA, "captions-failed", "2026-07-20")
    assert "status: captions-failed" in note
    assert ("Caption download failed after repeated attempts "
            "(rate limit or unavailable subtitles); not summarized.") in note
    assert "Full transcript" not in note

def test_build_note_escapes_quotes_in_title():
    vmeta = dict(VMETA, title='He said "Hello": a story')
    note = build_note(vmeta, MMETA, "## TL;DR\nx", "line one\nline two", "2026-07-20")
    assert 'title: "He said \\"Hello\\": a story"' in note
    assert "> [!note]- Full transcript" in note

def test_build_note_renders_topics_as_wikilinks():
    mmeta = dict(MMETA, topics=["RAG", "pgvector"])
    note = build_note(VMETA, mmeta, "## TL;DR\nx", "line one\nline two", "2026-07-20")
    assert "## Topics" in note
    assert "[[RAG]] · [[pgvector]]" in note
    assert note.index("## Topics") < note.index("> [!note]- Full transcript")

def test_build_note_omits_topics_when_empty():
    mmeta = dict(MMETA, topics=[])
    note = build_note(VMETA, mmeta, "## TL;DR\nx", "line one\nline two", "2026-07-20")
    assert "## Topics" not in note

def test_tags_dedupes_youtube():
    mmeta = dict(MMETA, tags=["youtube", "ai", "llm"])
    note = build_note(VMETA, mmeta, "## TL;DR\nx", "line one", "2026-07-20")
    tags_line = [ln for ln in note.splitlines() if ln.startswith("tags:")][0]
    assert tags_line.count("youtube") == 1
    assert tags_line == 'tags: ["youtube", "ai", "llm"]'

def test_topic_folder_valid_and_fallback():
    from src.note import topic_folder
    allowed = ["AI & Dev", "Other"]
    assert topic_folder("AI & Dev", allowed) == "AI & Dev"
    assert topic_folder("Nonsense", allowed) == "Other"
    assert topic_folder("", allowed) == "Other"

ALLOWED = ["AI & Dev", "Business & Marketing", "Career", "Finance",
           "Health & Fitness", "Personal", "Other"]
SUBS = {"AI & Dev": ["Claude Code", "AI Agents"],
        "Business & Marketing": ["SEO", "Real Estate"]}
ROOT = "Social Content"

def test_destination_three_levels_for_a_topic_with_subtopics():
    from src.note import destination_dir
    assert destination_dir("AI & Dev", "Claude Code", ALLOWED, SUBS, ROOT) == \
        "Social Content/AI & Dev/Claude Code"

def test_destination_two_levels_for_a_topic_without_subtopics():
    from src.note import destination_dir
    assert destination_dir("Career", "anything", ALLOWED, SUBS, ROOT) == \
        "Social Content/Career"

def test_destination_unlisted_subtopic_falls_back_to_general():
    from src.note import destination_dir
    assert destination_dir("AI & Dev", "Quantum Basketry", ALLOWED, SUBS, ROOT) == \
        "Social Content/AI & Dev/General"

def test_destination_empty_subtopic_falls_back_to_general():
    from src.note import destination_dir
    assert destination_dir("AI & Dev", "", ALLOWED, SUBS, ROOT) == \
        "Social Content/AI & Dev/General"

def test_destination_unknown_topic_becomes_other():
    from src.note import destination_dir
    assert destination_dir("Underwater Welding", "x", ALLOWED, SUBS, ROOT) == \
        "Social Content/Other"

def test_destination_rejects_an_unsafe_root():
    from src.note import destination_dir
    assert destination_dir("Career", "", ALLOWED, SUBS, "../escape") == "Other"

def test_destination_rejects_an_unsafe_topic():
    from src.note import destination_dir
    assert destination_dir("../etc", "x", ["../etc", "Other"], {}, ROOT) == "Other"

def test_safe_vault_dir_resolves_a_normal_nested_folder(tmp_path):
    from src.note import safe_vault_dir
    vault = tmp_path / "vault"
    out = safe_vault_dir(vault, "dev-brain/raw/transcripts")
    assert out == (vault / "dev-brain" / "raw" / "transcripts").resolve()

def test_safe_vault_dir_falls_back_to_other_on_a_traversal_folder(tmp_path):
    # this is the hole destination_dir itself cannot close: an unmapped topic
    # returns the bare `allowed` entry verbatim, and `allowed` comes straight
    # from the hand-edited TOPICS= list.
    from src.note import safe_vault_dir
    vault = tmp_path / "vault"
    out = safe_vault_dir(vault, "../../etc")
    assert out == vault.resolve() / "Other"

def test_safe_vault_dir_falls_back_to_other_on_an_absolute_folder(tmp_path):
    from src.note import safe_vault_dir
    vault = tmp_path / "vault"
    out = safe_vault_dir(vault, "/etc")
    assert out == vault.resolve() / "Other"

def test_safe_vault_dir_does_not_expand_a_tilde_folder(tmp_path):
    # a brain value of "~" must stay a literal in-vault folder name, never
    # expand to the caller's home directory.
    from src.note import safe_vault_dir
    vault = tmp_path / "vault"
    out = safe_vault_dir(vault, "~/raw/transcripts")
    assert out == (vault / "~" / "raw" / "transcripts").resolve()
    assert str(Path.home()) not in str(out)

def test_safe_vault_dir_resolves_through_an_in_vault_symlink(tmp_path):
    # A legitimate in-vault symlink (e.g. a sub-vault kept on another drive)
    # must not be treated as an escape just because Path.resolve() would
    # follow it outside the vault. Containment is checked lexically on the
    # unresolved join, so the symlink itself keeps working.
    from src.note import safe_vault_dir
    vault = tmp_path / "vault"
    vault.mkdir()
    external = tmp_path / "external-drive"
    external.mkdir()
    (vault / "dev-brain").symlink_to(external, target_is_directory=True)
    out = safe_vault_dir(vault, "dev-brain/raw/transcripts")
    assert out == vault.resolve() / "dev-brain" / "raw" / "transcripts"
    # safe_vault_dir creates the destination itself, so a live symlink is
    # followed here rather than at the caller's mkdir.
    assert (external / "raw" / "transcripts").is_dir()
    out.mkdir(parents=True, exist_ok=True)  # what the writers do: a no-op now

def test_safe_vault_dir_still_rejects_traversal_and_absolute_and_null_byte(tmp_path):
    # the symlink fix must not regress containment for the three hostile
    # shapes the rest of this branch defends against.
    from src.note import safe_vault_dir
    vault = tmp_path / "vault"
    assert safe_vault_dir(vault, "../../etc") == vault.resolve() / "Other"
    assert safe_vault_dir(vault, "/etc") == vault.resolve() / "Other"
    assert safe_vault_dir(vault, "dev\x00brain") == vault.resolve() / "Other"

def test_safe_vault_dir_falls_back_on_a_dangling_in_vault_symlink(tmp_path):
    # lexical containment accepts an in-vault symlink -- that is the point of it.
    # But a DANGLING one (a sub-vault on an unmounted external drive) makes
    # mkdir(exist_ok=True) re-raise FileExistsError, and poll.run has no per-item
    # guard, so it would abort the whole cycle before the social phase.
    from src.note import safe_vault_dir
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "dev-brain").symlink_to(tmp_path / "never-mounted")
    assert safe_vault_dir(vault, "dev-brain/raw/transcripts") == vault.resolve() / "Other"

def test_safe_vault_dir_falls_back_on_a_too_long_folder_name(tmp_path):
    # a hand-edited TOPICS= entry over NAME_MAX raises ENAMETOOLONG from mkdir,
    # which reached poll.run unguarded and killed the cycle.
    from src.note import safe_vault_dir
    vault = tmp_path / "vault"
    assert safe_vault_dir(vault, "x" * 300) == vault.resolve() / "Other"

def test_safe_vault_dir_logs_a_warning_on_fallback(tmp_path, caplog):
    # a misrouted destination (traversal, or an unusable path) must never be
    # silent -- the caller has no other signal that a note landed in Other/
    # instead of where it was supposed to go.
    from src.note import safe_vault_dir
    vault = tmp_path / "vault"
    with caplog.at_level("WARNING", logger="yt-obsidian"):
        safe_vault_dir(vault, "../../etc")
    assert any("Other" in r.message for r in caplog.records)
    caplog.clear()
    with caplog.at_level("WARNING", logger="yt-obsidian"):
        safe_vault_dir(vault, "dev\x00brain")
    assert any("Other" in r.message for r in caplog.records)

def test_frontmatter_includes_topic():
    from src.note import build_note
    vm = {"title": "T", "channel": "C", "url": "u", "video_id": "v",
          "published": "2026-01-01", "duration": "1:00"}
    out = build_note(vm, {"category": "ai-dev", "topic": "AI & Dev", "tags": []},
                     "body", "line", "2026-07-26")
    assert 'topic: "AI & Dev"' in out.split("---")[1]

def test_youtube_frontmatter_declares_its_platform():
    note = build_note({"title": "T", "channel": "C", "url": "u",
                       "video_id": "v", "published": "2026-07-01",
                       "duration": "00:10:00"},
                      {"category": "ai-dev", "topic": "AI & Dev",
                       "subtopic": "Claude Code", "tags": [], "topics": []},
                      "body", "", "2026-07-30")
    assert "platform: youtube" in note
    assert "source_type: youtube" in note
    assert "content_kind: video" in note
    assert 'subtopic: "Claude Code"' in note

def test_build_social_note_frontmatter_and_transcript():
    from src.note import build_social_note
    pm = {"platform": "instagram", "url": "https://ig/p/1", "author": "@a",
          "title": "T", "date": "2026-07-01", "kind": "video"}
    mm = {"category": "ai-dev", "topic": "AI & Dev", "tags": ["x"]}
    out = build_social_note(pm, mm, "# Note\nbody", "spoken words", "2026-07-26")
    fm = out.split("---")[1]
    assert 'platform: "instagram"' in fm
    assert 'source_type: "social"' in fm
    assert 'content_kind: "other"' in fm
    assert 'topic: "AI & Dev"' in fm
    assert 'type: "video"' in fm
    assert 'status: "summarized"' in fm
    assert 'tags: ["instagram", "x"]' in fm
    assert "Full transcript" in out and "> spoken words" in out

def test_build_social_note_escapes_hostile_title_and_author():
    from src.note import build_social_note
    pm = {"platform": "instagram", "url": "https://ig/p/1",
          "author": '@a: "the one"',
          "title": 'Say "hi": #trending\n---\nnot yaml',
          "date": "2026-07-01", "kind": "video"}
    mm = {"category": "ai-dev", "topic": "AI & Dev", "tags": ["x"]}
    out = build_social_note(pm, mm, "body", None, "2026-07-26")
    assert 'title: "Say \\"hi\\": #trending\\n---\\nnot yaml"' in out
    assert 'author: "@a: \\"the one\\""' in out
    # hostile title must not inject extra frontmatter fences
    lines = out.splitlines()
    fence_indices = [i for i, ln in enumerate(lines) if ln.strip() == "---"]
    assert len(fence_indices) == 2
    assert fence_indices[0] == 0

def test_build_social_stub_note_preserves_share():
    from src.note import build_social_stub_note
    pm = {"platform": "threads", "url": "https://t/p/1", "author": "",
          "title": "", "date": "", "kind": "text"}
    out = build_social_stub_note(pm, "fetch-failed", "my shared caption", "2026-07-26")
    assert 'status: "fetch-failed"' in out
    assert "https://t/p/1" in out
    assert "my shared caption" in out

def test_build_social_stub_note_preserves_share_with_missing_fields():
    from src.note import build_social_stub_note
    pm = {"platform": "twitter", "url": "https://x/1", "author": "",
          "title": "", "date": "", "kind": "text"}
    out = build_social_stub_note(pm, "summary-failed", None, "2026-07-26")
    assert 'status: "summary-failed"' in out
    assert "https://x/1" in out

def _frontmatter_block(note: str) -> str:
    lines = note.splitlines()
    fence_indices = [i for i, ln in enumerate(lines) if ln.strip() == "---"]
    assert len(fence_indices) == 2, f"expected exactly 2 fence lines, got {fence_indices}"
    # content between the fences, without the fence markers themselves
    return "\n".join(lines[fence_indices[0] + 1:fence_indices[1]])

def test_build_social_note_escapes_hostile_date_colon_value():
    from src.note import build_social_note
    pm = {"platform": "instagram", "url": "https://ig/p/1", "author": "@a",
          "title": "T", "date": "x: y", "kind": "video"}
    mm = {"category": "ai-dev", "topic": "AI & Dev", "tags": ["x"]}
    out = build_social_note(pm, mm, "body", None, "2026-07-26")
    fm_text = _frontmatter_block(out)
    parsed = yaml.safe_load(fm_text)
    assert parsed["date"] == "x: y"
    assert parsed["topic"] == "AI & Dev"
    assert parsed["status"] == "summarized"

def test_build_social_note_escapes_hostile_date_fence_injection():
    from src.note import build_social_note
    pm = {"platform": "instagram", "url": "https://ig/p/1", "author": "@a",
          "title": "T", "date": "before\n---\nafter", "kind": "video"}
    mm = {"category": "ai-dev", "topic": "AI & Dev", "tags": ["x"]}
    out = build_social_note(pm, mm, "body", None, "2026-07-26")
    fm_text = _frontmatter_block(out)
    parsed = yaml.safe_load(fm_text)
    assert parsed["date"] == "before\n---\nafter"
    assert parsed["topic"] == "AI & Dev"
    assert parsed["status"] == "summarized"

def test_build_social_stub_note_escapes_hostile_date_colon_value():
    from src.note import build_social_stub_note
    pm = {"platform": "threads", "url": "https://t/p/1", "author": "",
          "title": "", "date": "x: y", "kind": "text"}
    out = build_social_stub_note(pm, "fetch-failed", "my shared caption", "2026-07-26")
    fm_text = _frontmatter_block(out)
    parsed = yaml.safe_load(fm_text)
    assert parsed["date"] == "x: y"
    assert parsed["topic"] == "Other"
    assert parsed["status"] == "fetch-failed"

def test_build_social_stub_note_escapes_hostile_date_fence_injection():
    from src.note import build_social_stub_note
    pm = {"platform": "threads", "url": "https://t/p/1", "author": "",
          "title": "", "date": "before\n---\nafter", "kind": "text"}
    out = build_social_stub_note(pm, "fetch-failed", "my shared caption", "2026-07-26")
    fm_text = _frontmatter_block(out)
    parsed = yaml.safe_load(fm_text)
    assert parsed["date"] == "before\n---\nafter"
    assert parsed["topic"] == "Other"
    assert parsed["status"] == "fetch-failed"
    # stub must still preserve the original URL and shared text verbatim in the body
    assert "https://t/p/1" in out
    assert "my shared caption" in out

def test_build_social_note_none_kind_and_date_fall_back_to_defaults():
    from src.note import build_social_note
    pm = {"platform": "instagram", "url": "https://ig/p/1", "author": "@a",
          "title": "T", "date": None, "kind": None}
    mm = {"category": "ai-dev", "topic": "AI & Dev", "tags": ["x"]}
    out = build_social_note(pm, mm, "body", None, "2026-07-26")
    fm = out.split("---")[1]
    assert "None" not in out
    assert 'type: "text"' in fm
    assert 'date: ""' in fm


def test_build_web_event_note_keeps_source_and_content_dimensions_separate():
    from src.note import build_social_note

    pm = {
        "platform": "generic",
        "source_type": "web",
        "content_kind": "event",
        "url": "https://stubhub.com/event/1",
        "title": "Concert",
        "kind": "text",
    }
    mm = {"category": "other", "topic": "Personal", "subtopic": "Events & Experiences"}

    out = build_social_note(pm, mm, "body", None, "2026-08-13")

    fm = out.split("---")[1]
    assert 'platform: "generic"' in fm
    assert 'source_type: "web"' in fm
    assert 'content_kind: "event"' in fm
    assert 'topic: "Personal"' in fm
    assert 'subtopic: "Events & Experiences"' in fm

def test_build_social_stub_note_none_platform_falls_back_to_social():
    # the caller assembles post_meta, so platform can arrive present-but-None;
    # that must not crash the stub, which is the note guaranteeing a share is
    # never lost.
    from src.note import build_social_stub_note
    pm = {"platform": None, "url": "https://t/p/1", "author": "", "title": "",
          "date": "", "kind": "text"}
    out = build_social_stub_note(pm, "fetch-failed", "my shared caption", "2026-07-26")
    fm = out.split("---")[1]
    assert 'platform: "social"' in fm
    assert 'tags: ["social"]' in fm

def test_build_social_stub_note_none_kind_and_date_fall_back_to_defaults():
    from src.note import build_social_stub_note
    pm = {"platform": "threads", "url": "https://t/p/1", "author": "",
          "title": "", "date": None, "kind": None}
    out = build_social_stub_note(pm, "fetch-failed", None, "2026-07-26")
    fm = out.split("---")[1]
    assert "None" not in out
    assert 'type: "text"' in fm
    assert 'date: ""' in fm

def test_build_social_note_escapes_hostile_tags():
    # a model-produced tag containing "]" or ": " must not break the fence --
    # unescaped, it would either terminate the tags list early or open a
    # forged mapping key, and the whole frontmatter block fails to parse.
    from src.note import build_social_note
    pm = {"platform": "twitter", "url": "https://x/p/1", "author": "@a",
          "title": "T", "date": "2026-07-01", "kind": "video"}
    mm = {"category": "ai-dev", "topic": "AI & Dev",
          "tags": ["ok]", "status: forged", "x: [y"]}
    out = build_social_note(pm, mm, "body", None, "2026-07-26")
    fm_text = _frontmatter_block(out)
    parsed = yaml.safe_load(fm_text)
    assert parsed["status"] == "summarized"      # not overwritten by a forged tag
    assert parsed["topic"] == "AI & Dev"
    assert set(parsed["tags"]) == {"twitter", "ok]", "status: forged", "x: [y"}

def test_build_note_escapes_hostile_tags():
    # same defect class as the social path, on the YouTube frontmatter's tags line
    mmeta = dict(MMETA, tags=["ok]", "status: forged", "x: [y"])
    note = build_note(VMETA, mmeta, "## TL;DR\nx", "line one", "2026-07-20")
    fm_text = _frontmatter_block(note)
    parsed = yaml.safe_load(fm_text)
    assert parsed["status"] == "summarized"
    assert set(parsed["tags"]) == {"youtube", "ok]", "status: forged", "x: [y"}

def test_build_note_escapes_hostile_topic_and_category():
    # topic/category on the YouTube path come from the model, not yt-dlp, so
    # they need the same escaping as every other model-controlled scalar
    mmeta = dict(MMETA, topic="Finance: and more", category="x: y")
    note = build_note(VMETA, mmeta, "## TL;DR\nx", "line one", "2026-07-20")
    fm_text = _frontmatter_block(note)
    parsed = yaml.safe_load(fm_text)
    assert parsed["topic"] == "Finance: and more"
    assert parsed["category"] == "x: y"
    assert parsed["status"] == "summarized"

def _write(p: Path, key: str, value: str):
    p.write_text(f"---\ntitle: \"x\"\n{key}: {value}\n---\n\nbody\n")

def test_resolve_collision_free_path_is_returned_unchanged(tmp_path):
    p = tmp_path / "note.md"
    assert resolve_collision(p, "abc123", "video_id") == p

def test_resolve_collision_same_identity_overwrites(tmp_path):
    p = tmp_path / "note.md"
    _write(p, "video_id", "abc123")
    # a reprocessed video must rewrite its own note, not make a second one
    assert resolve_collision(p, "abc123", "video_id") == p

def test_resolve_collision_different_identity_suffixes(tmp_path):
    p = tmp_path / "note.md"
    _write(p, "video_id", "abc123")
    assert resolve_collision(p, "zzz999", "video_id") == tmp_path / "note (2).md"

def test_resolve_collision_walks_past_multiple_occupants(tmp_path):
    _write(tmp_path / "note.md", "video_id", "aaa")
    _write(tmp_path / "note (2).md", "video_id", "bbb")
    assert resolve_collision(tmp_path / "note.md", "ccc", "video_id") == \
        tmp_path / "note (3).md"

def test_resolve_collision_reuses_the_suffixed_note_of_the_same_item(tmp_path):
    _write(tmp_path / "note.md", "video_id", "aaa")
    _write(tmp_path / "note (2).md", "video_id", "bbb")
    assert resolve_collision(tmp_path / "note.md", "bbb", "video_id") == \
        tmp_path / "note (2).md"

def test_resolve_collision_unreadable_occupant_is_treated_as_foreign(tmp_path):
    p = tmp_path / "note.md"
    p.write_text("no frontmatter here")
    assert resolve_collision(p, "abc123", "video_id") == tmp_path / "note (2).md"

def test_resolve_collision_uses_url_for_social(tmp_path):
    p = tmp_path / "note.md"
    _write(p, "url", "https://x.com/a/1")
    assert resolve_collision(p, "https://x.com/a/1", "url") == p

# processed_at: the sortable timestamp. `processed:` is stamped once per run and
# is the filename prefix, so it can neither carry a time nor order items within
# a run; these pin that processed_at does both without disturbing it.

PMETA = {"title": "A post", "platform": "x", "author": "someone",
         "url": "https://x.com/a/1", "date": "2026-07-19", "kind": "text"}

def _fm(note: str) -> dict:
    return yaml.safe_load(note.split("---")[1])

def test_now_stamp_is_obsidian_datetime_format():
    from src.note import now_stamp
    # YYYY-MM-DDTHH:MM:SS -- no microseconds, no offset suffix. Obsidian reads
    # this as a "Date & time" property; anything else sorts as a string.
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", now_stamp())

def test_build_note_stamps_processed_at_and_leaves_processed_alone():
    note = build_note(VMETA, MMETA, "## TL;DR\nx", "t", "2026-07-20",
                      processed_at="2026-07-20T14:32:05")
    fm = _fm(note)
    assert fm["processed_at"] == datetime(2026, 7, 20, 14, 32, 5)
    assert fm["processed"] == date(2026, 7, 20)

def test_build_social_note_stamps_processed_at():
    note = build_social_note(PMETA, MMETA, "body", "", "2026-07-20",
                             processed_at="2026-07-20T14:32:05")
    assert _fm(note)["processed_at"] == datetime(2026, 7, 20, 14, 32, 5)

def test_stub_notes_stamp_processed_at_too():
    # a stub is still a note in the Base and still has to sort
    video = build_stub_note(VMETA, "no-transcript", "2026-07-20",
                            processed_at="2026-07-20T14:32:05")
    social = build_social_stub_note(PMETA, "fetch-failed", "", "2026-07-20",
                                    processed_at="2026-07-20T14:32:05")
    for note in (video, social):
        assert _fm(note)["processed_at"] == datetime(2026, 7, 20, 14, 32, 5)

def test_processed_at_defaults_to_now_when_the_caller_passes_nothing():
    # the pipeline never threads it; each note picks up its own build time,
    # which is what breaks the within-a-run tie.
    note = build_note(VMETA, MMETA, "x", "t", "2026-07-20")
    stamped = _fm(note)["processed_at"]
    assert isinstance(stamped, datetime)
    assert abs((datetime.now() - stamped).total_seconds()) < 60

def test_two_notes_built_in_one_run_can_be_ordered():
    a = build_note(VMETA, MMETA, "x", "t", "2026-07-20",
                   processed_at="2026-07-20T14:32:05")
    b = build_note(VMETA, MMETA, "x", "t", "2026-07-20",
                   processed_at="2026-07-20T14:32:41")
    assert _fm(a)["processed_at"] < _fm(b)["processed_at"]
    assert _fm(a)["processed"] == _fm(b)["processed"]

def test_processed_at_does_not_reach_the_filename():
    from src.note import content_filename
    # the filename still keys on the date-only `processed`; a datetime there
    # would fail _DATE.fullmatch and file every note under "undated".
    assert content_filename("2026-07-20", "A Post", "youtube") == \
        "2026-07-20 A Post (YouTube).md"


def test_social_frontmatter_omits_capture_keys_by_default():
    pm = {"platform": "generic", "url": "https://example.com/a", "title": "T", "kind": "text"}
    for out in (build_social_note(pm, {"tags": []}, "body", None, "2026-11-01"),
                build_social_stub_note(pm, "fetch-failed", None, "2026-11-01")):
        fm = _frontmatter_block(out)
        assert "extraction:" not in fm and "word_count:" not in fm and "source_format:" not in fm


def test_social_frontmatter_adds_capture_keys_after_type():
    pm = {"platform": "generic", "url": "https://example.com/a", "title": "T", "kind": "text",
          "extraction": "full", "word_count": 1843, "source_format": "html",
          "extraction_reason": ""}
    out = build_social_note(pm, {"category": "ai-dev", "tags": []}, "body", None, "2026-11-01")
    assert ('type: "text"\nextraction: "full"\nword_count: 1843\nsource_format: "html"\n'
            'category: "ai-dev"') in out
    assert "extraction_reason" not in out
    assert yaml.safe_load(_frontmatter_block(out))["word_count"] == 1843


def test_build_social_note_appends_source_text():
    pm = {"platform": "generic", "url": "https://example.com/a", "kind": "text"}
    out = build_social_note(pm, {"tags": []}, "# Summary\nbody", "spoken words", "2026-11-01",
                            source_text="First line.\n\n--- \nLast line.")
    assert out.endswith("> [!note]- Full source text\n> First line.\n> \n> --- \n> Last line.\n")
    assert out.index("> [!note]- Full transcript") < out.index("> [!note]- Full source text")
    _frontmatter_block(out)                  # the quoted '---' adds no fence


@pytest.mark.parametrize("reason, header", [
    ("size cap", "> [!note]- Full source text (cut at the 5 MB size cap)\n"),
    ("page cap", "> [!note]- Full source text (first 300 pages)\n"),
])
def test_build_social_note_source_text_names_the_cap(reason, header):
    pm = {"platform": "generic", "url": "https://example.com/a", "kind": "text",
          "extraction": "partial", "word_count": 2, "source_format": "html",
          "extraction_reason": reason}
    out = build_social_note(pm, {"tags": []}, "body", None, "2026-11-01",
                            source_text="Read text.")
    assert out.endswith(header + "> Read text.\n")


def test_build_social_note_source_text_has_no_suffix_when_full():
    pm = {"platform": "generic", "url": "https://example.com/a", "kind": "text",
          "extraction": "full", "word_count": 2, "source_format": "html",
          "extraction_reason": ""}
    out = build_social_note(pm, {"tags": []}, "body", None, "2026-11-01",
                            source_text="Read text.")
    assert out.endswith("\n\n> [!note]- Full source text\n> Read text.\n")


def test_build_social_stub_note_partial_names_the_reason():
    pm = {"platform": "generic", "url": "https://example.com/paywalled", "kind": "text",
          "extraction": "partial", "word_count": 2, "source_format": "html",
          "extraction_reason": "http 403", "capture_why": "Pricing research"}
    out = build_social_stub_note(pm, "partial", "read later", "2026-11-01")
    assert 'status: "partial"' in out
    assert "Only part of this page could be fetched (http 403); saved for manual review." in out
    assert "## Why saved\nPricing research" in out
    assert "Original: https://example.com/paywalled" in out
    assert "> Shared note: read later" in out
    assert "extraction_reason" not in _frontmatter_block(out)


def test_build_social_stub_note_reason_override():
    pm = {"platform": "generic", "url": "http://10.0.0.5/", "kind": "text",
          "stub_reason": "This url was refused (private or non-http address); "
                         "saved for manual review."}
    out = build_social_stub_note(pm, "invalid", None, "2026-11-01")
    assert 'status: "invalid"' in out
    assert "This url was refused (private or non-http address)" in out
    assert "arrived without a usable url" not in out and "stub_reason" not in out


def test_social_frontmatter_appends_import_tags_once():
    pm = {"platform": "generic", "url": "https://example.com/a", "title": "T", "kind": "text",
          "import_tags": ["Reading", "python", "generic", "second-brain"]}
    out = build_social_note(pm, {"category": "ai-dev", "tags": ["python", "agents"]},
                            "body", None, "2026-11-01")
    assert 'tags: ["generic", "python", "agents", "Reading", "second-brain"]' in out
    stub = build_social_stub_note(pm, "fetch-failed", None, "2026-11-01")
    assert 'tags: ["generic", "Reading", "python", "second-brain"]' in stub


def test_social_frontmatter_tags_unchanged_without_import_tags():
    pm = {"platform": "generic", "url": "https://example.com/a", "title": "T", "kind": "text"}
    out = build_social_note(pm, {"category": "ai-dev", "tags": ["python", "generic"]},
                            "body", None, "2026-11-01")
    assert 'tags: ["generic", "python"]' in out
    assert "import_tags" not in out
