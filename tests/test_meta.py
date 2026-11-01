from pathlib import Path
from src.meta import parse_model_output

FIX = Path(__file__).parent / "fixtures" / "model_output.md"

def test_parse_extracts_meta_and_body():
    meta, body = parse_model_output(FIX.read_text())
    assert meta["category"] == "ai-dev"
    assert meta["tags"] == ["ai", "llm", "agents", "tutorial", "topic/rag"]
    assert meta["topics"] == ["Retrieval Augmented Generation", "pgvector"]
    assert body.startswith("## TL;DR")
    assert "```meta" not in body

def test_parse_missing_meta_is_safe():
    meta, body = parse_model_output("## TL;DR\njust a body, no meta")
    assert meta == {"category": "other", "tags": [], "topics": [], "topic": "Other",
                     "subtopic": "General"}
    assert body.strip().startswith("## TL;DR")

def test_parse_malformed_meta_is_safe():
    meta, body = parse_model_output("```meta\ngarbage no colons\n```\n---\n## Body\nx")
    assert meta["category"] == "other"
    assert meta["tags"] == []
    assert body.strip().startswith("## Body")

def test_parse_model_output_topic():
    from src.meta import parse_model_output
    text = "```meta\ncategory: ai-dev\ntopic: AI & Dev\ntags: [x]\n```\n---\nBody here"
    meta, body = parse_model_output(text)
    assert meta["topic"] == "AI & Dev"
    assert body == "Body here"

def test_parse_model_output_topic_default():
    from src.meta import parse_model_output
    meta, _ = parse_model_output("```meta\ncategory: other\n```\n---\nB")
    assert meta["topic"] == "Other"

def test_parse_model_output_reads_subtopic():
    text = "```meta\ntopic: AI & Dev\nsubtopic: Claude Code\n```\n---\nbody"
    meta, _ = parse_model_output(text)
    assert meta["subtopic"] == "Claude Code"

def test_parse_model_output_subtopic_defaults_to_general():
    text = "```meta\ntopic: AI & Dev\n```\n---\nbody"
    meta, _ = parse_model_output(text)
    assert meta["subtopic"] == "General"

def test_parse_model_output_blank_subtopic_defaults_to_general():
    text = "```meta\ntopic: AI & Dev\nsubtopic:\n```\n---\nbody"
    meta, _ = parse_model_output(text)
    assert meta["subtopic"] == "General"
