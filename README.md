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

## Articles and PDFs

Share an article, a newsletter's web page, or a link to a PDF through `/share`
and the note holds a summary plus the full extracted text in a collapsed
`Full source text` callout. The model reads at most 60,000 characters of it;
the note keeps all of it.

Full extraction needs an optional, hash-pinned extra (trafilatura for HTML,
pypdf for PDF). Install it into the interpreter or venv that runs `src.poll`:

```sh
python3.12 -m pip install --require-hashes --only-binary=:all: -r requirements-capture.txt
```

The extra keeps its dependencies' own licenses: trafilatura is Apache-2.0,
pypdf and lxml are BSD-3-Clause, and certifi is MPL-2.0. `tld` offers MPL-1.1,
GPL-2.0-only, or LGPL-2.1-or-later, and Keepworthy uses it under MPL-1.1.

Each poll run logs the mode in use, `article capture: full (...)` or
`article capture: fallback (...)`. Without the extra, Keepworthy reads only the
page's title and description meta tags and marks the note partial.

Notes from this path carry three more frontmatter keys after `type`:

- `extraction`: `full`, or `partial` when only part of the page could be read
- `word_count`: words in the extracted text
- `source_format`: `html` or `pdf`

A capped page is summarized from the text that was read. That applies to HTML
over the 5 MB size cap and to a PDF over 300 pages, where the first 300 pages
are the text. The note is marked `extraction: partial`, and the source text
callout says where the text stops. The capped text still needs 150 words;
below that, the page is handled like the other partial reads below.

A page that can't be read in full goes through the media tools next, in case
it is mostly an embedded video. That covers paywalled or login pages, other
HTTP 4xx responses, pages under 150 words, PDFs over the 20 MB size cap,
encrypted PDFs, HTML or PDF text the extractor can't parse, and responses
sent with a `Content-Encoding` other than `identity`, which Keepworthy treats
as unsupported. If the media tools find nothing, Keepworthy writes a short
note with `status: "partial"` right away instead of retrying, so a paywalled
article gives you a partial note by design. Search for that status to find
them. Sharing the same link again later rewrites that note.

A 5xx response or a timeout is different: Keepworthy retries it on later runs
up to `MAX_ATTEMPTS`, then writes a `fetch-failed` note.

Fetch limits for the article reader: only `http` and `https`. Every address a
host resolves to, on every redirect, has to be public; private, loopback,
link-local, CGNAT, and cloud metadata addresses are refused and filed as
`invalid`. The connection goes to the address that was checked. No cookies or
credentials are sent. Each network operation gets 20 seconds and a whole
fetch gets 60 seconds (over HTTPS the worst case is about 80 seconds). A fetch
follows at most 5 redirects and reads at most 5 MB of HTML, or 20 MB and 300
pages of PDF.

These checks cover the article reader only. When a share falls back to the
media tools (a partial read other than capped text, or an unsupported type),
the existing fallback runs as before: a page metadata fetch and yt-dlp, with
the configured cookie file and without the article reader's address checks.

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
A second job, `tests-capture`, runs the same suite with
`requirements-capture.lock` installed; the default job covers the fallback.
For private repositories, hosted jobs are skipped to avoid enabling runner
billing; run the same checks locally. Actions and scanner binaries are pinned.

See [changelog](CHANGELOG.md), [security](SECURITY.md), and [license](LICENSE).
Third-party CLIs, model weights, and libraries are installed separately and
retain their own licenses.
