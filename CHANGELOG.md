# Changelog

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
