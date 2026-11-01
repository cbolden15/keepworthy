"""Guards the contract between the prompt files (which instruct the model to
emit a fenced ```meta block) and src/meta.py's parse_model_output (which only
recognises that exact fence). If a prompt file's fence is ever dropped or
malformed, parse_model_output silently falls back to defaults -- every note
would then lose its category/tags/topic and, since topic drives the filing
folder, silently land in Other/. These tests fail loudly if that coupling
breaks."""
from pathlib import Path

import pytest

from src.config import load_config
from src.meta import _META_BLOCK, parse_model_output

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"

# Realistic values for the keys every meta template declares. Both prompt
# files template the same five keys (category, topic, subtopic, tags, topics),
# so one fixed set covers both.
_REALISTIC_VALUES = {
    "category": "ai-dev",
    "topic": "AI & Dev",
    "subtopic": "Claude Code",
    "tags": "[ai, coding, topic/rag]",
    "topics": "[Retrieval Augmented Generation]",
}


def _extract_template_lines(prompt_text: str) -> list[str]:
    """Locate the ```meta template block using the SAME regex parse_model_output
    uses. If a prompt's fence is missing or malformed, this match fails here --
    which is exactly the failure this test exists to catch."""
    m = _META_BLOCK.search(prompt_text)
    assert m, "prompt file has no ```meta fence matching parse_model_output's regex"
    return m.group(1).splitlines()


def _build_realistic_model_output(prompt_text: str) -> str:
    """Build a plausible model response for this prompt: take the template's
    key lines (dropping in-fence instructional lines that aren't `key: ...`,
    e.g. summarize.md's "Pick the single best-fitting topic..." line) and
    substitute realistic values, then append a body after the `---` separator."""
    template_lines = _extract_template_lines(prompt_text)
    meta_lines = []
    for line in template_lines:
        if ":" not in line:
            continue
        key = line.split(":", 1)[0].strip()
        if key in _REALISTIC_VALUES:
            meta_lines.append(f"{key}: {_REALISTIC_VALUES[key]}")
    assert meta_lines, "no recognised meta keys found in prompt's template block"
    meta_block = "\n".join(meta_lines)
    return f"```meta\n{meta_block}\n```\n---\n## TL;DR\nA test summary body.\n"


@pytest.mark.parametrize("prompt_name", ["summarize.md", "summarize-social.md"])
def test_prompt_meta_fence_is_parseable(prompt_name):
    prompt_text = (PROMPTS_DIR / prompt_name).read_text()
    model_output = _build_realistic_model_output(prompt_text)

    meta, body = parse_model_output(model_output)

    # The parse must actually populate values from the block, not fall
    # through to parse_model_output's defaults.
    assert meta["category"] == "ai-dev"
    assert meta["topic"] == "AI & Dev"
    assert meta["subtopic"] == "Claude Code"
    assert meta["tags"] == ["ai", "coding", "topic/rag"]
    assert meta["topics"] == ["Retrieval Augmented Generation"]
    assert body.startswith("## TL;DR")


def _cfg(tmp_path):
    env = tmp_path / "config.env"
    env.write_text(f"PLAYLIST_URL=https://x\nVAULT_DIR={tmp_path}/vault\n")
    return load_config(env)


@pytest.mark.parametrize("prompt_name", ["summarize.md", "summarize-social.md"])
def test_prompt_offers_every_configured_subtopic(tmp_path, prompt_name):
    """Mirrors test_classify_prompt_offers_every_configured_subtopic in
    test_migrate.py: a subtopic the prompt never names can never be chosen
    by the model, which would quietly collapse a whole branch of the tree
    into General forever."""
    cfg = _cfg(tmp_path)
    text = (PROMPTS_DIR / prompt_name).read_text()
    for topic, subs in cfg.subtopics.items():
        for sub in subs:
            assert sub in text, f"{prompt_name} does not offer {topic}/{sub}"
