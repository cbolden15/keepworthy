# Operational gotchas

- **Build Pipeline, YAML command scalars:** pip's `--only-binary=:all:` followed
  by another argument contains a colon-space sequence. In an unquoted YAML
  scalar it becomes a mapping delimiter and breaks workflow parsing. Use a
  block scalar for shell commands with this syntax. The full test suite parses
  the CI workflow and checks action pins, permissions, and private-runner gates.

- **Python version:** use Python 3.12. Some platform-default interpreters are
  older and cannot parse the application's type annotations or frozen tools.
- **Internal Meta endpoints:** a 200 response can contain a GraphQL error or
  login page. Validate response shape and pagination, not status alone.
- **Saved-feed boundaries:** validate new records only until the previous
  boundary. Unrelated older records must not fail a no-change import.
- **Namespace libraries:** Ubuntu FFmpeg's BLAS symlinks use `/etc/alternatives`.
  The media namespace mounts it read-only alongside public runtime libraries.
- **Filesystem isolation:** changing a model's working directory does not
  confine absolute file reads. Images and private integrations require the
  explicit Linux sandbox. Failed sandbox calls must never fall back to direct
  execution.
- **Private writes:** manual shell umasks can be permissive. Free-media and Sure
  notes use the atomic owner-only writer independently of systemd's umask.
- **Freshness:** a successful finance read is not proof of current holdings.
  Preserve dated evidence and unknown current ownership unless the upstream
  freshness contract can be established.
- **Queue replay:** acknowledge only after durable delivery. Keep the processing
  batch across interruptions; dedupe by source identity when it is redelivered.
- **Note delivery:** the machine displaying your vault needs its own verified
  synchronization path. A healthy upstream writer alone does not prove notes
  reached that machine.
- **Capture extra license:** trafilatura is Apache-2.0 from 1.8.0 on; earlier
  releases are GPLv3+. Never pin it below 1.8.0.
- tld is MPL-1.1 OR GPL-2.0-only OR LGPL-2.1-or-later; Keepworthy uses it
  under MPL-1.1. Other licenses in the extra: pypdf and lxml BSD-3-Clause,
  certifi MPL-2.0, regex Apache-2.0 AND CNRI-Python, the rest MIT, BSD, or
  Apache.
- **Capture lock age gate:** the lock takes only releases at least 14 days
  old, so the pypdf pin trails upstream. 6.21.4 (2026-10-28, two security
  fixes) clears the gate on 2026-11-11; bump the pin then and move
  `--exclude-newer` to 14 days before the regeneration date. Regenerate from
  the repo root with:

  ```sh
  { printf '%s\n' '# Optional article and PDF extraction; regenerate with the command in GOTCHAS.md.' '--only-binary=:all:' '--require-hashes'; printf 'trafilatura==2.3.1\npypdf==6.21.1\n' | uv pip compile - --generate-hashes --python-version 3.12 --only-binary :all: --exclude-newer 2026-10-18T00:00:00Z --no-header --no-annotate --quiet; } > requirements-capture.lock
  ```

- **Article fetches carry no cookies:** `fetch_article` must never use the
  social cookie jar, `COOKIES_PATH`, or an `Authorization` header. A shared url
  is untrusted and can point at any host.
- **Article fetch deadline:** `MAX_FETCH_S` (60 s) bounds a whole fetch on top
  of the 20 s per-operation `TIMEOUT_S`. Over HTTPS the worst case is about
  80 s.
- **Extraction CPU time is unbounded:** the 60 s fetch deadline covers the
  network read only. HTML parsing and PDF text extraction run after it with no
  time limit.
- **Pocket tag separator:** Pocket's `list.csv` separates tags with `|`, not
  `,`. Raindrop uses `,`.
- **Raindrop columns vary:** column names and the folder nesting separator
  differ between Raindrop export versions. The parser matches columns by
  name, case-insensitive, and an export missing `url` or `created` fails with
  the column named. Run a new export through `--dry-run` first.
- **Two calendars:** the import drip counts its daily limit per UTC day, while
  a note's `processed:` date and filename use the local date.
- **`fetch_page_text` has no opener:** it fetches through `article._fetch` now.
  The `cookies_path` and `opener` parameters are accepted and ignored so old
  callers keep working; tests pass `resolver` and `connector` fakes instead.
- **A missing host may only mean offline:** macOS `getaddrinfo` returns
  `EAI_NONAME` for every name while the machine is offline (glibc returns
  `EAI_AGAIN`). An imported link therefore waits a day, as well as
  `MAX_ATTEMPTS` tries, before a missing host becomes a `dead-link` note. The
  first miss is stamped on the queue item as `import_missing_since`.
