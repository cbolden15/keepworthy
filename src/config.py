import logging
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from src import note as note_mod

log = logging.getLogger("yt-obsidian")

@dataclass
class Config:
    playlist_url: str
    vault_dir: Path
    subfolder: str
    model: str
    prompt_path: Path
    state_path: Path
    max_attempts: int
    sub_lang: str
    topics: list[str]
    ingest_token: str
    ingest_port: int
    ingest_bind: str
    cookies_path: Path | None
    gemini_api_key: str
    gemini_model: str
    queue_path: Path
    social_prompt_path: Path
    content_root: str
    subtopics: dict[str, list[str]]
    open_brain_spool_dir: Path | None = None
    open_brain_saved_spool_dir: Path | None = None
    channels: list[str] = field(default_factory=list)
    finance_content: object | None = field(default=None, repr=False, compare=False)
    free_media_enabled: bool = False
    social_batch_size: int = 0
    model_sandbox: bool = False
    import_db_path: Path = Path("state/import.db")
    import_daily_limit: int = 20

_DEFAULTS = {
    "SUBFOLDER": "YouTube Summaries",
    "MODEL": "sonnet",
    "PROMPT_PATH": "prompts/summarize.md",
    "STATE_PATH": "state/state.json",
    "MAX_ATTEMPTS": "3",
    "SUB_LANG": "en",
    "TOPICS": "AI & Dev,Business & Marketing,Career,Finance,"
              "Health & Fitness,Personal",
    "INGEST_TOKEN": "",
    "INGEST_PORT": "8787",
    "INGEST_BIND": "0.0.0.0",
    "COOKIES_PATH": "",
    "GEMINI_API_KEY": "",
    "GEMINI_MODEL": "gemini-2.5-flash",
    "QUEUE_PATH": "state/queue.jsonl",
    "SOCIAL_PROMPT_PATH": "prompts/summarize-social.md",
    "CONTENT_ROOT": "saved-content",
    "OPEN_BRAIN_SPOOL_DIR": "",
    "CHANNELS": "",
    "SURE_CONTEXT_CONFIG": "",
    "FREE_MEDIA_ENABLED": "false",
    "SOCIAL_BATCH_SIZE": "0",
    "MODEL_SANDBOX": "false",
    "IMPORT_DB_PATH": "state/import.db",
    "IMPORT_DAILY_LIMIT": "20",
    "SUBTOPICS": "AI & Dev:Claude Code|AI Agents|Second Brain & Knowledge Systems|"
                 "Self-Hosting & Infrastructure|Dev Tools & Repos;"
                 "Business & Marketing:AI Agencies & Websites|Local Business AI|"
                 "Offers & Pricing|Solo & Side Businesses|SEO|Real Estate;"
                 "Personal:Events & Experiences",
}

def _parse_subtopics(value: str) -> dict[str, list[str]]:
    # ';' separates topics, ':' separates a topic from its list, '|' separates
    # subtopics. A typo here is a config mistake, not a crash: skip malformed
    # entries rather than raising, since a systemd service reads this file.
    out = {}
    for entry in (value or "").split(";"):
        entry = entry.strip()
        if not entry or ":" not in entry:
            continue
        topic, _, rest = entry.partition(":")
        topic = topic.strip()
        subs = []
        for sub in rest.split("|"):
            sub = sub.strip()
            if not sub:
                continue
            if not note_mod.is_safe_path_component(sub):
                log.warning("SUBTOPICS entry %r -> %r is not a safe path "
                            "component; dropping that subtopic", topic, sub)
                continue
            subs.append(sub)
        if topic and subs:
            out[topic] = subs
    return out

def normalize_channel_url(url: str) -> str:
    # One channel must always map to one state key, however it was pasted.
    url = url.strip()
    # Mobile share links carry tracking params (?si=...); they are not part of the channel.
    for sep in ("?", "#"):
        url = url.split(sep, 1)[0]
    url = url.rstrip("/")
    if url.endswith("/videos"):
        url = url[: -len("/videos")].rstrip("/")
    return url

def _parse_env(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        out[key.strip()] = val.strip()
    return out

class ConfigError(ValueError):
    """A config.env problem the owner fixes by editing the file."""

# Values copied verbatim from config.env.example; running with them is a setup mistake.
_EXAMPLE_VALUES = {
    "PLAYLIST_URL": "YOUR_PLAYLIST_ID",
    "VAULT_DIR": "/absolute/path/to/your/vault",
}

def _check_required(path: Path, raw: dict) -> None:
    for key in ("PLAYLIST_URL", "VAULT_DIR"):
        if not raw.get(key):
            raise ConfigError(f"{key} is not set in {path}. See config.env.example.")
        if _EXAMPLE_VALUES[key] in raw[key]:
            raise ConfigError(f"{key} still has the example value in {path}. "
                              f"Set it to your own value.")
    if not Path(raw["VAULT_DIR"]).expanduser().is_absolute():
        raise ConfigError(f"VAULT_DIR must be an absolute path, got {raw['VAULT_DIR']!r}.")

def load_config_or_exit(path: Path, environment: Mapping[str, str] | None = None) -> Config:
    """load_config for command entry points: a bad config prints one line and exits 2."""
    try:
        return load_config(path, environment)
    except ValueError as e:
        print(f"config error: {e}", file=sys.stderr)
        raise SystemExit(2)

def load_config(path: Path, environment: Mapping[str, str] | None = None) -> Config:
    try:
        text = Path(path).read_text()
    except FileNotFoundError:
        raise ConfigError(f"No config file at {path}. Create it with "
                          f"`cp config.env.example config.env`, then set "
                          f"PLAYLIST_URL and VAULT_DIR.") from None
    raw = _parse_env(text)
    _check_required(path, raw)
    if environment is not None and environment.get("OPEN_BRAIN_SPOOL_DIR"):
        raw["OPEN_BRAIN_SPOOL_DIR"] = environment["OPEN_BRAIN_SPOOL_DIR"]
    if environment is not None and environment.get("OPEN_BRAIN_SAVED_SPOOL_DIR"):
        raw["OPEN_BRAIN_SAVED_SPOOL_DIR"] = environment["OPEN_BRAIN_SAVED_SPOOL_DIR"]
    def get(k): return raw.get(k, _DEFAULTS.get(k))
    if get("MODEL_SANDBOX") not in ("true", "false"):
        raise ValueError("MODEL_SANDBOX must be true or false")
    if get("MODEL_SANDBOX") == "true":
        from src.model_sandbox import available
        if not available():
            raise ValueError("Install the root-owned model launcher before enabling MODEL_SANDBOX")
    if get("FREE_MEDIA_ENABLED") not in ("true", "false"):
        raise ValueError("FREE_MEDIA_ENABLED must be true or false")
    if get("FREE_MEDIA_ENABLED") == "true":
        if get("MODEL_SANDBOX") != "true":
            raise ValueError("FREE_MEDIA_ENABLED requires MODEL_SANDBOX=true")
        from src.local_media import available
        if not available():
            raise ValueError("Install and verify the root-owned offline media launcher before enabling FREE_MEDIA_ENABLED")
    social_batch_size = int(get("SOCIAL_BATCH_SIZE"))
    if not 0 <= social_batch_size <= 100:
        raise ValueError("SOCIAL_BATCH_SIZE must be between 0 and 100")
    try:
        import_daily_limit = int(get("IMPORT_DAILY_LIMIT"))
    except ValueError:
        import_daily_limit = -1
    if not 0 <= import_daily_limit <= 500:
        raise ValueError("IMPORT_DAILY_LIMIT must be between 0 and 500")
    if not get("IMPORT_DB_PATH"):
        raise ValueError("IMPORT_DB_PATH must be a path")
    finance_content = None
    if get("SURE_CONTEXT_CONFIG"):
        if get("MODEL_SANDBOX") != "true":
            raise ValueError("Sure enrichment requires MODEL_SANDBOX=true to protect private notes and credentials")
        from src.finance_content import load_finance_content
        if get("OPEN_BRAIN_SPOOL_DIR"):
            raise ValueError("Sure note enrichment requires local note processing; the YouTube Open Brain spool bypasses it")
        finance_content = load_finance_content(Path(get("SURE_CONTEXT_CONFIG")).expanduser())
    return Config(
        playlist_url=raw["PLAYLIST_URL"],
        vault_dir=Path(raw["VAULT_DIR"]).expanduser(),
        subfolder=get("SUBFOLDER"),
        model=get("MODEL"),
        prompt_path=Path(get("PROMPT_PATH")),
        state_path=Path(get("STATE_PATH")),
        max_attempts=int(get("MAX_ATTEMPTS")),
        sub_lang=get("SUB_LANG"),
        topics=[t.strip() for t in get("TOPICS").split(",") if t.strip()],
        ingest_token=get("INGEST_TOKEN"),
        ingest_port=int(get("INGEST_PORT")),
        ingest_bind=get("INGEST_BIND"),
        cookies_path=Path(get("COOKIES_PATH")) if get("COOKIES_PATH") else None,
        gemini_api_key=get("GEMINI_API_KEY"),
        gemini_model=get("GEMINI_MODEL"),
        queue_path=Path(get("QUEUE_PATH")),
        social_prompt_path=Path(get("SOCIAL_PROMPT_PATH")),
        content_root=get("CONTENT_ROOT"),
        subtopics=_parse_subtopics(get("SUBTOPICS")),
        open_brain_spool_dir=(
            Path(get("OPEN_BRAIN_SPOOL_DIR")).expanduser()
            if get("OPEN_BRAIN_SPOOL_DIR")
            else None
        ),
        open_brain_saved_spool_dir=(
            Path(get("OPEN_BRAIN_SAVED_SPOOL_DIR")).expanduser()
            if get("OPEN_BRAIN_SAVED_SPOOL_DIR")
            else None
        ),
        channels=[normalize_channel_url(c) for c in get("CHANNELS").split(",")
                  if c.strip()],
        finance_content=finance_content,
        free_media_enabled=get("FREE_MEDIA_ENABLED") == "true",
        social_batch_size=social_batch_size,
        model_sandbox=get("MODEL_SANDBOX") == "true",
        import_db_path=Path(get("IMPORT_DB_PATH")),
        import_daily_limit=import_daily_limit,
    )

def allowed_topics(cfg: Config) -> list[str]:
    return cfg.topics + ["Other"]
