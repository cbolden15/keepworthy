# Changelog

## 0.3.0 — Unreleased

- Capture articles, newsletter web pages, and PDFs shared through `/share`.
  Notes keep the full extracted text in a collapsed callout; the model reads
  up to 60,000 characters of it.
- Add the optional hash-pinned extra `requirements-capture.lock` (trafilatura
  2.3.1, pypdf 6.21.1). Without it, generic links fall back to meta tags and a
  partial note.
- Add `extraction`, `word_count`, and `source_format` frontmatter keys to notes
  from the article path.
- Add the `partial` note status for pages that could only be read in part.
  These notes are written once, without retries.
- Summarize capped pages (HTML over 5 MB, PDFs over 300 pages) from the text
  that was read, marked `extraction: partial`.
- Protect generic fetches against SSRF: http and https only, public addresses
  only on every redirect, connections pinned to the checked address, no
  cookies, and time, size, and page limits (20 seconds per network operation,
  60 seconds per fetch).
- Treat responses with a `Content-Encoding` other than `identity` as
  unsupported and hand them to the media path.

## 0.2.0 — 2026-11-01

- Publish the reusable pipeline as Keepworthy with clean source history.

- Add daily Instagram, Facebook, and Threads saved-feed discovery with a durable
  outbox, duplicate prevention, per-platform health, and visible pagination gaps.
- Add optional offline transcription and image/frame observations through Linux
  sandboxes. Free-only saves never fall back to paid transcription.
- Add optional read-only Sure context after summarization, with private writes
  and explicit unknown ownership when source freshness is unverified.
- Preserve direct transcript-only Claude CLI use without requiring root. Require
  explicit sandbox configuration for images, offline media, and private context.
- Add portable examples, frozen dependency hashes, and public-repository CI.
- Exclude deployment records, credentials, source media, and local databases.

The optional Linux setup is operator-managed. A cross-platform installer and
unverified Threads pagination are outside this release.
