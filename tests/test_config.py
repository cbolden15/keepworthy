from pathlib import Path
from src.config import load_config, Config, _parse_subtopics, normalize_channel_url

def test_load_config_parses_env(tmp_path):
    env = tmp_path / "config.env"
    env.write_text(
        "PLAYLIST_URL=https://www.youtube.com/playlist?list=PLxoAjG6tkGqIC-TdADJM2N43pdjP7OAuM\n"
        "VAULT_DIR=/home/example/Brain\n"
        "SUBFOLDER=YouTube Summaries\n"
        "MODEL=sonnet\n"
        "PROMPT_PATH=prompts/summarize.md\n"
        "STATE_PATH=state/state.json\n"
        "MAX_ATTEMPTS=3\n"
        "SUB_LANG=en\n"
        "# a comment line\n"
        "\n"
    )
    cfg = load_config(env)
    assert isinstance(cfg, Config)
    assert cfg.playlist_url.endswith("PLxoAjG6tkGqIC-TdADJM2N43pdjP7OAuM")
    assert cfg.vault_dir == Path("/home/example/Brain")
    assert cfg.subfolder == "YouTube Summaries"
    assert cfg.model == "sonnet"
    assert cfg.max_attempts == 3
    assert cfg.sub_lang == "en"

def test_load_config_defaults(tmp_path):
    env = tmp_path / "config.env"
    env.write_text("PLAYLIST_URL=https://x\nVAULT_DIR=/v\n")
    cfg = load_config(env)
    assert cfg.subfolder == "YouTube Summaries"
    assert cfg.model == "sonnet"
    assert cfg.max_attempts == 3
    assert cfg.sub_lang == "en"

def test_load_config_new_fields_defaults(tmp_path):
    env = tmp_path / "config.env"
    env.write_text("PLAYLIST_URL=https://x\nVAULT_DIR=/v\n")
    cfg = load_config(env)
    assert cfg.topics == ["AI & Dev", "Business & Marketing", "Career",
                          "Finance", "Health & Fitness", "Personal"]
    assert cfg.ingest_token == ""
    assert cfg.ingest_port == 8787
    assert cfg.ingest_bind == "0.0.0.0"
    assert cfg.cookies_path is None
    assert cfg.gemini_api_key == ""
    assert cfg.gemini_model == "gemini-2.5-flash"
    assert cfg.queue_path == Path("state/queue.jsonl")
    assert cfg.social_prompt_path == Path("prompts/summarize-social.md")
    assert cfg.content_root == "saved-content"
    assert cfg.open_brain_spool_dir is None
    assert cfg.subtopics == {
        "AI & Dev": ["Claude Code", "AI Agents", "Second Brain & Knowledge Systems",
                     "Self-Hosting & Infrastructure", "Dev Tools & Repos"],
        "Business & Marketing": ["AI Agencies & Websites", "Local Business AI",
                                  "Offers & Pricing", "Solo & Side Businesses",
                                  "SEO", "Real Estate"],
        "Personal": ["Events & Experiences"],
    }

def test_load_config_new_fields_explicit(tmp_path):
    env = tmp_path / "config.env"
    env.write_text(
        "PLAYLIST_URL=https://x\nVAULT_DIR=/v\n"
        "TOPICS=A,B\nINGEST_TOKEN=tok\nINGEST_PORT=9000\nINGEST_BIND=127.0.0.1\n"
        "COOKIES_PATH=/tmp/c.txt\nGEMINI_API_KEY=g\nGEMINI_MODEL=gemini-x\n"
        "QUEUE_PATH=state/q.jsonl\nSOCIAL_PROMPT_PATH=prompts/social-x.md\n"
    )
    cfg = load_config(env)
    assert cfg.social_prompt_path == Path("prompts/social-x.md")
    assert cfg.topics == ["A", "B"]
    assert cfg.ingest_token == "tok"
    assert cfg.ingest_port == 9000
    assert cfg.ingest_bind == "127.0.0.1"
    assert cfg.cookies_path == Path("/tmp/c.txt")
    assert cfg.gemini_api_key == "g"
    assert cfg.gemini_model == "gemini-x"
    assert cfg.queue_path == Path("state/q.jsonl")

def test_load_config_accepts_explicit_runtime_spool_override(tmp_path):
    env = tmp_path / "config.env"
    env.write_text("PLAYLIST_URL=https://x\nVAULT_DIR=/v\n")

    cfg = load_config(
        env,
        environment={"OPEN_BRAIN_SPOOL_DIR": "/v/.open-brain-ingress/youtube"},
    )

    assert cfg.open_brain_spool_dir == Path("/v/.open-brain-ingress/youtube")

def test_parse_subtopics_reads_topics_and_lists():
    got = _parse_subtopics("AI & Dev:Claude Code|AI Agents;Business & Marketing:SEO")
    assert got == {"AI & Dev": ["Claude Code", "AI Agents"],
                   "Business & Marketing": ["SEO"]}

def test_parse_subtopics_empty_value_yields_empty_map():
    assert _parse_subtopics("") == {}

def test_parse_subtopics_skips_entries_without_a_colon():
    assert _parse_subtopics("AI & Dev;Business & Marketing:SEO") == \
        {"Business & Marketing": ["SEO"]}

def test_parse_subtopics_skips_a_topic_with_an_empty_list():
    assert _parse_subtopics("AI & Dev:;Business & Marketing:SEO") == \
        {"Business & Marketing": ["SEO"]}

def test_parse_subtopics_drops_unsafe_subtopic_names():
    # a hand-edited config must not be able to steer a write out of the vault
    got = _parse_subtopics("AI & Dev:Claude Code|../etc|Good One")
    assert got == {"AI & Dev": ["Claude Code", "Good One"]}

def test_parse_subtopics_tolerates_whitespace():
    assert _parse_subtopics(" AI & Dev : Claude Code | AI Agents ") == \
        {"AI & Dev": ["Claude Code", "AI Agents"]}

def test_allowed_topics_appends_other(tmp_path):
    from src.config import allowed_topics
    env = tmp_path / "config.env"
    env.write_text("PLAYLIST_URL=https://x\nVAULT_DIR=/v\nTOPICS=A,B\n")
    assert allowed_topics(load_config(env)) == ["A", "B", "Other"]


def test_channels_default_empty(tmp_path):
    env = tmp_path / "config.env"
    env.write_text("PLAYLIST_URL=https://x\nVAULT_DIR=/v\n")
    assert load_config(env).channels == []


def test_channels_parsed_and_normalized(tmp_path):
    env = tmp_path / "config.env"
    env.write_text(
        "PLAYLIST_URL=https://x\nVAULT_DIR=/v\n"
        "CHANNELS= https://www.youtube.com/@a/ , https://www.youtube.com/@b/videos,,"
        "https://www.youtube.com/channel/UC123/videos/\n"
    )
    assert load_config(env).channels == [
        "https://www.youtube.com/@a",
        "https://www.youtube.com/@b",
        "https://www.youtube.com/channel/UC123",
    ]


def test_normalize_channel_url_is_idempotent():
    url = "https://www.youtube.com/@a"
    assert normalize_channel_url(url) == url
    assert normalize_channel_url(normalize_channel_url(url + "/videos/")) == url


def test_normalize_channel_url_strips_query_and_fragment():
    assert normalize_channel_url(
        "https://www.youtube.com/@x/videos?si=abc#t") == "https://www.youtube.com/@x"
    assert normalize_channel_url(
        "https://youtube.com/@x?si=abc") == "https://youtube.com/@x"


def _minimal_env(tmp_path, extra=""):
    env = tmp_path / "config.env"
    env.write_text(f"PLAYLIST_URL=https://x\nVAULT_DIR={tmp_path}/vault\n{extra}")
    return env


def test_saved_spool_dir_defaults_to_none(tmp_path):
    assert load_config(_minimal_env(tmp_path)).open_brain_saved_spool_dir is None


def test_saved_spool_dir_from_file_and_environment(tmp_path):
    env = _minimal_env(tmp_path, "OPEN_BRAIN_SAVED_SPOOL_DIR=~/yt-staging/open-brain-saved\n")
    cfg = load_config(env)
    assert cfg.open_brain_saved_spool_dir == Path("~/yt-staging/open-brain-saved").expanduser()
    override = load_config(env, environment={"OPEN_BRAIN_SAVED_SPOOL_DIR": "/srv/saved"})
    assert override.open_brain_saved_spool_dir == Path("/srv/saved")
