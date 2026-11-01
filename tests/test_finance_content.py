from dataclasses import replace
import json
import stat
from types import SimpleNamespace

import pytest

from src.finance_content import FinanceContent, entities_from_source, load_finance_content
from src import note, poll, social, state
from src.config import load_config
from tests.test_poll import make_cfg, base_deps
from tests.test_social import _cfg, _deps, _process
from tests.test_sure_context import API, NOW, A, H
from src.sure_context import SureContext


def processor(tmp_path):
    key = tmp_path / "sure-key"
    key.write_text("synthetic-key")
    key.chmod(0o600)
    reader = SureContext(key_file=key, scope_id="owner", opener=API(), now=lambda: NOW)
    return FinanceContent(reader, "https://sure.example")


def test_explicit_entities_are_bounded_and_not_guessed_from_words():
    entities = entities_from_source("Apple MSFT 100 dollars $100 $AAPL NASDAQ:EXM XNAS:EXM $AAPL")
    assert entities == [{"entity_id": "symbol-1", "ticker": "AAPL"},
                        {"entity_id": "symbol-2", "ticker": "EXM", "exchange_mic": "XNAS"}]
    assert len(entities_from_source(" ".join("$A" + str(i) for i in range(100)))) == 50


def test_finance_uses_real_adapter_and_preserves_claim_boundaries(tmp_path):
    result = processor(tmp_path).annotate({"topic": "Finance"}, "Creator's claims", "NASDAQ:EXM", "post-1")
    assert result.startswith("Creator's claims")
    assert "positive position observed in Sure on 2026-10-31" in result
    assert "current ownership unknown" in result
    assert f"https://sure.example/holdings/{H}" in result
    assert f"https://sure.example/accounts/{A}" in result
    assert "$99999" not in result and "PRIVATE NAME" not in result


def test_non_finance_and_disabled_paths_are_byte_identical():
    class Never:
        def context(self, request):
            raise AssertionError("must not access Sure")
    flow = FinanceContent(Never(), "https://sure.example")
    assert flow.annotate({"topic": "AI & Dev"}, "  body\n", "$EXM", "x") == "  body\n"
    assert social.finance_content.annotate(SimpleNamespace(), {"topic": "Finance"}, "body", "x", "id") == "body"


def test_context_failure_keeps_summary_and_does_not_log_private_exception(tmp_path, caplog):
    class Broken:
        def context(self, request):
            raise RuntimeError("PRIVATE VALUE")
    result = FinanceContent(Broken(), "https://sure.example").annotate({"topic": "Finance"}, "original", "taxes", "id")
    assert result.startswith("original") and "unavailable" in result
    assert "PRIVATE VALUE" not in result + caplog.text


def test_social_saved_note_contains_private_context_but_model_and_spool_do_not(tmp_path):
    spool = tmp_path / "spool"
    cfg = replace(_cfg(tmp_path, OPEN_BRAIN_SAVED_SPOOL_DIR=str(spool)), finance_content=processor(tmp_path))
    payloads = []
    def summarize(prompt, payload, images, model, cwd):
        payloads.append(payload)
        return "```meta\ntopic: Finance\ncategory: other\ntags: [finance]\n```\n---\nCreator claims"
    deps = _deps(run_claude_social=summarize, transcribe_audio=lambda *args: "NASDAQ:EXM")
    item = {"url": "https://www.instagram.com/p/test", "why": "research", "received_at": "2026-10-31T12:00:00Z"}
    assert _process(cfg, tmp_path, item, "2026-10-31", deps) == "summarized"
    files = list(cfg.vault_dir.rglob("*.md"))
    assert len(files) == 1
    saved = files[0].read_text()
    assert "positive position observed in Sure" in saved and "Full transcript" in saved
    assert stat.S_IMODE(files[0].stat().st_mode) == 0o600
    exported = next(spool.glob("*.json")).read_text()
    for text in [*payloads, exported]:
        assert "Your financial context" not in text and "sure.example" not in text and A not in text
    assert json.loads(exported)["summary"] == "Creator claims"


def test_youtube_finance_context_is_joined_after_summary_and_saved_privately(tmp_path):
    cfg = replace(make_cfg(tmp_path), finance_content=processor(tmp_path))
    sent = []
    deps = base_deps(
        clean_vtt=lambda _: "NASDAQ:EXM tutorial",
        run_claude=lambda prompt, transcript, *args: sent.append(transcript) or "public summary",
        parse_model_output=lambda _: ({"topic": "Finance", "tags": []}, "public summary"),
        build_note=note.build_note,
    )
    assert poll.process_video(cfg, tmp_path, "v1", "2026-10-31", deps) == "summarized"
    path = next(cfg.vault_dir.rglob("*.md"))
    assert "positive position observed in Sure" in path.read_text()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert sent == ["NASDAQ:EXM tutorial"]
    assert state.is_seen(state.load_state(cfg.state_path), "v1")


def test_education_without_symbols_gets_account_context(tmp_path):
    result = processor(tmp_path).annotate({"topic": "Finance"}, "Tax basics", "IRA account types", "education")
    assert "Connected account types:" in result and "brokerage: 1" in result
    assert "Open Sure holding" not in result

def test_transient_context_failure_creates_bounded_retry_without_repeating_summary(tmp_path):
    flow = processor(tmp_path)
    flow.reader.opener.fail = 500
    directory = tmp_path / "retry"
    first = flow.annotate({"topic": "Finance"}, "original", "$EXM", "id", retry_dir=directory)
    assert first.startswith("original") and "unavailable" in first
    path = next(directory.glob("*.json"))
    pending = json.loads(path.read_text())
    assert pending["max_attempts"] == 3 and pending["attempts"] == 0
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    flow.annotate({"topic": "Finance"}, "original", "$EXM", "id", retry_dir=directory)
    assert json.loads(path.read_text()) == pending
    flow.reader.opener.fail = None
    flow.annotate({"topic": "Finance"}, "original", "$EXM", "id", retry_dir=directory)
    assert not path.exists()


def test_startup_configuration_is_private_explicit_and_network_free(tmp_path, monkeypatch):
    monkeypatch.setattr('src.model_sandbox.available', lambda: True)
    config = tmp_path / "sure.json"
    options = {"key_file": str(tmp_path / "not-yet-provisioned"), "scope_id": "owner", "ui_origin": "https://sure.example"}
    config.write_text(json.dumps(options))
    config.chmod(0o600)
    env = tmp_path / "config.env"
    env.write_text(f"PLAYLIST_URL=https://x\nVAULT_DIR=/v\nMODEL_SANDBOX=true\nSURE_CONTEXT_CONFIG={config}\n")
    assert isinstance(load_config(env).finance_content, FinanceContent)
    config.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        load_config(env)
    config.chmod(0o600)
    config.write_text(json.dumps({**options, "api_key": "should-not-be-here"}))
    with pytest.raises(ValueError, match="fields"):
        load_config(env)


def test_spool_only_youtube_mode_cannot_silently_skip_enrichment(tmp_path, monkeypatch):
    monkeypatch.setattr('src.model_sandbox.available', lambda: True)
    env = tmp_path / "config.env"
    env.write_text("PLAYLIST_URL=https://x\nVAULT_DIR=/v\nMODEL_SANDBOX=true\nSURE_CONTEXT_CONFIG=/private/config.json\nOPEN_BRAIN_SPOOL_DIR=/spool\n")
    with pytest.raises(ValueError, match="bypasses"):
        load_config(env)


@pytest.mark.parametrize("origin", ["http://sure.example", "https://name:secret@sure.example", "https://sure.example/path", "javascript:alert(1)", "https://sure.example:not-a-port"])
def test_untrusted_ui_origins_are_not_used_for_financial_links(origin):
    with pytest.raises(ValueError):
        FinanceContent(None, origin)
