import subprocess
from pathlib import Path

from src import model_sandbox


def run_claude(prompt_text: str, transcript: str, model: str, cwd: Path, runner=subprocess.run, *, sandbox=False) -> str:
    return run_claude_social(prompt_text, transcript, [], model, cwd, runner, sandbox=sandbox)


def run_claude_social(prompt_text, payload, image_paths, model, cwd, runner=subprocess.run, *, sandbox=False) -> str:
    if sandbox:
        return model_sandbox.run(prompt_text, payload, model, image_paths, runner=runner)
    if image_paths:
        raise RuntimeError("Image reading requires MODEL_SANDBOX=true and an installed Linux sandbox")
    result = runner(["claude", "--model", model, "--setting-sources", "", "--strict-mcp-config",
                     "--mcp-config", '{"mcpServers":{}}', "--disable-slash-commands",
                     "--no-session-persistence", "--permission-mode", "dontAsk", "--tools", "", "-p", prompt_text],
                    input=payload, text=True, capture_output=True, cwd=str(cwd), timeout=200)
    if result.returncode:
        raise RuntimeError("Claude summarization failed; check CLI installation and login")
    return result.stdout
