import base64
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from functools import partial
from src.summarize import run_claude as direct_claude, run_claude_social as direct_social

run_claude = partial(direct_claude, sandbox=True)
run_claude_social = partial(direct_social, sandbox=True)

spec = importlib.util.spec_from_file_location("sandbox_helper", Path(__file__).parents[1] / "bin/model_sandbox.py")
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


def test_default_transcript_mode_needs_no_root_launcher(tmp_path):
    def runner(cmd, **kw):
        assert cmd[0] == 'claude' and 'sudo' not in cmd
        assert cmd[cmd.index('--tools')+1] == ''
        assert '--strict-mcp-config' in cmd and kw['timeout'] == 200
        return SimpleNamespace(returncode=0, stdout='plain summary')
    assert direct_claude('P','T','sonnet',tmp_path,runner) == 'plain summary'


def test_direct_mode_refuses_images_without_a_sandbox(tmp_path):
    with pytest.raises(RuntimeError,match='MODEL_SANDBOX'):
        direct_social('P','T',[tmp_path/'image.jpg'],'sonnet',tmp_path)


def request(**over):
    return {"version": 1, "operation": "summarize", "model": "sonnet", "prompt": "PROMPT", "payload": "SOURCE", "images": [], **over}


def test_text_uses_fixed_launcher_and_strips_environment(tmp_path, monkeypatch):
    calls = {}
    monkeypatch.setenv("SURE_API_KEY", "private")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "paid-route")
    def runner(cmd, **kw):
        calls.update(cmd=cmd, **kw)
        return SimpleNamespace(returncode=0, stdout=json.dumps({"ok": True, "output": "SUMMARY"}))
    assert run_claude("PROMPT", "TRANSCRIPT", "sonnet", tmp_path, runner=runner) == "SUMMARY"
    assert calls["cmd"] == ["/usr/bin/sudo", "-n", "/usr/local/sbin/yt-model-sandbox"]
    assert calls["cwd"] == "/" and calls["close_fds"]
    assert calls["timeout"] == 200
    assert set(calls["env"]) == {"PATH", "LANG"}
    assert json.loads(calls["input"]) == request(payload="TRANSCRIPT")


def test_images_are_data_not_paths_and_only_selected_files_cross(tmp_path):
    img = tmp_path / "hostile-name.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"selected-image")
    (tmp_path / "secret.txt").write_text("private")
    sent = {}
    def runner(cmd, **kw):
        sent.update(json.loads(kw["input"]))
        return SimpleNamespace(returncode=0, stdout='{"ok":true,"output":"ok"}')
    assert run_claude_social("P", "source", [img], "sonnet", tmp_path, runner=runner) == "ok"
    assert str(tmp_path) not in json.dumps(sent) and "hostile-name" not in json.dumps(sent)
    assert helper.validate(sent) == [(".png", img.read_bytes())]
    assert "private" not in json.dumps(sent)


def test_symlink_and_hardlink_inputs_are_not_exported(tmp_path):
    real = tmp_path / "private.png"
    real.write_bytes(b"secret")
    link = tmp_path / "link.png"
    link.symlink_to(real)
    with pytest.raises(OSError):
        run_claude_social("P", "T", [link], "sonnet", tmp_path)
    link.unlink()
    import os
    os.link(real, link)
    with pytest.raises(ValueError):
        run_claude_social("P", "T", [link], "sonnet", tmp_path)


@pytest.mark.parametrize("stdout,code", [("SECRET DIAGNOSTIC", 1), ("invalid json", 0), ('{"ok":false}', 0)])
def test_failure_has_no_unsandboxed_fallback_or_diagnostic_leak(tmp_path, stdout, code):
    calls = []
    def runner(cmd, **kw):
        calls.append(cmd)
        return SimpleNamespace(returncode=code, stdout=stdout, stderr="SECRET DIAGNOSTIC")
    with pytest.raises(RuntimeError) as error:
        run_claude("P", "T", "sonnet", tmp_path, runner=runner)
    assert len(calls) == 1 and "SECRET" not in str(error.value)


@pytest.mark.parametrize("extra", [{"command": ["sh"]}, {"mounts": ["/"]}, {"oauth_token": "secret"}, {"image_paths": ["/etc/shadow"]}])
def test_helper_rejects_expanded_authority(extra):
    with pytest.raises(ValueError):
        helper.validate(request(**extra))


@pytest.mark.parametrize("over", [
    {"model": "--dangerously-skip-permissions"},
    {"images": [{"extension": ".png", "base64": base64.b64encode(b"not-an-image").decode()}]},
    {"probe_paths": ["/etc/shadow"]},
    {"payload": "x" * (2 * 1024 * 1024 + 1)},
    {"version": True},
])
def test_helper_rejects_invalid_inputs(over):
    with pytest.raises(ValueError):
        helper.validate(request(**over))


def test_namespace_mounts_are_an_allowlist_with_private_pid_namespace(tmp_path):
    cmd = helper.command(tmp_path)
    assert "--unshare-all" in cmd and "--disable-userns" in cmd
    assert "--cap-drop" in cmd and "--proc" in cmd
    sources = [cmd[i+1] for i, x in enumerate(cmd) if x == "--ro-bind"]
    assert not set(sources) & {"/", "/usr", "/home", "/var", "/run", "/usr/local"}
    assert str(tmp_path) in sources and str(helper.BINARY) in sources
    assert not any("sure" in x or "config.env" in x for x in sources)
