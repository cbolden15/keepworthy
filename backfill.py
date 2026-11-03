#!/usr/bin/env python3
"""Manually summarize specific YouTube videos, bypassing the playlist poll.

Runs the same fetch -> clean -> summarize -> note path as the timer
(src.poll.process_video) for each video you pass on the command line, and marks
each one seen so a later playlist tick won't reprocess it.

Auth: needs CLAUDE_CODE_OAUTH_TOKEN in the environment and claude/yt-dlp on PATH,
exactly like the systemd unit. Example, from your checkout:

    cd ~/Projects/keepworthy
    env PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin" HOME="$HOME" \\
      CLAUDE_CODE_OAUTH_TOKEN="$(sed -n 's/^CLAUDE_CODE_OAUTH_TOKEN=//p' ~/.config/youtube-obsidian/auth.env)" \\
      python3.12 backfill.py https://www.youtube.com/watch?v=ID1 ID2 ...

Accepts full watch URLs, youtu.be links, or bare 11-char video IDs.
"""
import argparse
import logging
import re
import sys
from datetime import date
from pathlib import Path

from src import config as config_mod
from src.poll import _default_deps, process_video

log = logging.getLogger("yt-obsidian")

_ID_RE = re.compile(r"(?:v=|/shorts/|youtu\.be/|/watch/)?([0-9A-Za-z_-]{11})(?:[&?/]|$)")


def extract_id(token: str) -> str:
    """Return the 11-char video id from a URL or a bare id; raise if not found."""
    m = _ID_RE.search(token.strip())
    if not m:
        raise ValueError(f"could not extract a video id from: {token!r}")
    return m.group(1)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Summarize specific YouTube videos into Obsidian notes.")
    parser.add_argument("videos", nargs="+", help="video ids or YouTube URLs")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    project_root = Path(__file__).resolve().parent
    cfg = config_mod.load_config(project_root / "config.env")
    processed = date.today().isoformat()
    deps = _default_deps(cfg)

    ids = [extract_id(v) for v in args.videos]
    log.info("backfill start: %d videos", len(ids))
    outcomes = []
    for i, vid in enumerate(ids, 1):
        log.info("=== [%d/%d] %s ===", i, len(ids), vid)
        outcome = None
        for attempt in range(1, cfg.max_attempts + 1):
            try:
                outcome = process_video(cfg, project_root, vid, processed, deps)
            except Exception as e:  # defensive: keep going through the batch
                log.error("[%s] hard error: %s", vid, e)
                outcome = "error"
                break
            if outcome != "retry":
                break
            log.warning("[%s] retry attempt %d", vid, attempt)
        log.info("RESULT %s -> %s", vid, outcome)
        outcomes.append(outcome)
    log.info("=== backfill done: %s ===", ", ".join(f"{o}" for o in outcomes))
    # non-zero exit if anything did not summarize/stub cleanly
    return 0 if all(o in ("summarized", "no-transcript", "summary-failed") for o in outcomes) else 1


if __name__ == "__main__":
    sys.exit(main())
