# Keepworthy

**Make something of what you save.**

Keepworthy turns saved YouTube videos and social posts into categorized Markdown notes.
Notes keep source links, capture intent, full detected speech, and image/frame
observations when media processing is enabled.

## Start with YouTube

Transcript-only processing runs on macOS or Linux with Python 3.12, yt-dlp, and
an authenticated Claude Code CLI on PATH. No root helper is required. The CLI
must support tool restrictions and strict empty MCP configuration; the direct
path was checked with Claude Code 2.1.310. CLI authentication/billing follows
your own account configuration.

```sh
git clone https://github.com/cbolden15/keepworthy.git
cd keepworthy
cp config.env.example config.env
# Edit PLAYLIST_URL and VAULT_DIR. Use an absolute vault path.
chmod 600 config.env
python3.12 -m src.poll
```

The normal model call has no tools, MCP servers, project hooks, or persistent
session. Image reading requires the optional Linux sandbox; it does not grant
unrestricted filesystem access to the ordinary CLI path. An enabled sandbox
that fails never falls back to direct execution.

Notes land under `VAULT_DIR/saved-content/<topic>/<subtopic>`. Categories and
subtopics are configurable. Channels in `CHANNELS` baseline their existing
catalog on the first poll, then process new long-form uploads. The authenticated
optional `/share` endpoint accepts `{url, why, text?}` and keeps the owner's
reason for saving separate from the creator's claims. It binds to loopback by
default; see [share setup](docs/shortcut-setup.md).

## Daily saved posts

The reusable HTTP collectors support Instagram, Facebook, and Threads saved
feeds using an owner-authorized Netscape-format cookie file. They use internal
session endpoints, not official saved-post APIs. Sessions expire and endpoint
formats can change. Owner sign-in is required when authentication fails;
challenges are never bypassed. Cookies must be owner-only regular files.

```sh
cp examples/saved-discovery.json /absolute/private/saved-discovery.json
# Set absolute cookie, database, and queue paths. Match QUEUE_PATH in config.env.
chmod 600 /absolute/private/saved-discovery.json
python3.12 -m src.saved_discovery --config /absolute/private/saved-discovery.json
python3.12 -m src.saved_discovery --config /absolute/private/saved-discovery.json --status
```

The first successful import takes 25 Instagram saves, 15 Facebook saves, and 10
Threads saves. Subsequent runs stop at the previous import boundary, preventing
older-history backfills. A private SQLite outbox delivers into the existing
locked social queue. Durable append precedes acknowledgment; replay can deliver
a URL again, and the processor deduplicates it. Failed feeds keep their previous
boundary while other platforms continue.

Daily windows are bounded to 100 Instagram, 100 Facebook, and 10 Threads saves.
Threads pagination beyond that window is unverified. A truncated feed without
previous overlap records a sticky gap and exits nonzero; the omitted interval
requires recovery before clearing the flag. The CLI reports counts and health,
without raw upstream exceptions or credentials. Timer examples are in
[deployment](deploy.md).

Saved-feed videos carry `transcription_policy: free_only`. They stay queued until
free local media processing is enabled, without consuming retry attempts or
calling the optional legacy Gemini provider. Image/text items use available
source content. A successful caption-only note does not imply image analysis.

## Optional private media and finance

Advanced processing requires the operator-managed Linux setup in
[Linux sandboxes](docs/linux-sandboxes.md). These features default off:

```dotenv
MODEL_SANDBOX=true
FREE_MEDIA_ENABLED=true
SOCIAL_BATCH_SIZE=3
SURE_CONTEXT_CONFIG=/absolute/private/sure-context.json
```

The offline worker uses pinned faster-whisper dependencies and a pinned public
model revision. It transcribes locally and extracts up to eight evenly spaced
frames per clip. Images and frames are read in batches of eight, then the final
summary consumes their retained observations. Video samples can miss brief
on-screen details. Carousels retain their image and video components. Current
limits include 20 attachments, three clips, 900 seconds per clip, and 200 MiB
per post; unsupported media follows the bounded retry/review path.

Sure integration is optional and read-only. It joins scoped account-type and
dated holding observations after the model passes, and keeps financial context
out of public export payloads. Current ownership remains unknown without
verified upstream freshness. It requires a dedicated read-scoped credential,
private configuration, and model isolation. The adapter accepts loopback API
access only. [The contract](docs/sure-context.md) and JSON schemas document the
projection; examples and tests use synthetic records.

A complete public installer, extra Threads pagination, automatic finance-context
retry consumption, company-name resolution, specialized recipe/trade templates,
and task/trade execution are outside this release.

## Local data and privacy

Credentials, cookie jars, private deployment files, notes, downloaded source
media, operational databases, and local worktrees belong outside Git. Original
image/video downloads are temporary; notes retain observations and full detected
speech rather than source media files. The discovery database retains URLs and
captions for deduplication while enabled. Back up notes plus queue/processing
state and the discovery database together, preserving private permissions.
Deleting a source from those stores while it remains saved upstream can cause
rediscovery. Do not publish local runbooks or real account/holding snapshots.

`OPEN_BRAIN_SPOOL_DIR` hands YouTube transcripts to an external writer instead
of writing local summaries. `OPEN_BRAIN_SAVED_SPOOL_DIR` can export terminal
social capture records. Combining spool-only YouTube mode with private Sure
note enrichment is rejected; private finance sections never enter saved-item
exports.

## Tests

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install --require-hashes --only-binary=:all: -r requirements-dev.lock
.venv/bin/python -m pytest -q
```

Tests use synthetic fixtures and no live accounts or model calls. The visual
extraction eval has a fixed synthetic image with exact quantity/timing checks.
GitHub tests and secret scanning run automatically for public repositories.
For private repositories, hosted jobs are skipped to avoid enabling runner
billing; run the same checks locally. Actions and scanner binaries are pinned.

See [changelog](CHANGELOG.md), [security](SECURITY.md), and [license](LICENSE).
Third-party CLIs, model weights, and libraries are installed separately and
retain their own licenses.
