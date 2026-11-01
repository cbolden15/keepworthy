# "Save to Brain" iOS Shortcut

Share from any app into the homelab saved-content queue,
with your reason for saving preserved on the resulting note.

## Build the Shortcut (Shortcuts app)

1. New Shortcut → rename to **Save to Brain**.
2. Shortcut settings (ⓘ) → enable **Show in Share Sheet** → accepted types: **URLs** and **Text**.
3. Add action **Get URLs from Input** (input: Shortcut Input).
4. Add action **Ask for Input**:
   - Prompt: `Why are you saving this?`
   - Input type: **Text**
   - Require a one-line answer.
5. Add action **Get Contents of URL**:
   - URL: `http://summarizer.example.net:8787/share`
   - Method: **POST**
   - Headers: `Authorization` = `Bearer <INGEST_TOKEN from config.env>`
   - Request Body: **JSON** with fields:
     - `url` = *URLs* (magic variable from step 3)
     - `why` = *Provided Input* (magic variable from step 4)
     - `text` = app-provided caption or selected text, when useful (optional)
6. Add action **Show Notification**: "Saved to Brain ✓".

## Use

In any app: Share → **Save to Brain**. Requires Tailscale active on the phone.
Failure banner = item was NOT queued; fix connectivity and reshare.

## Notes

- The endpoint answers instantly (202) — processing happens within ~5 minutes.
- Notes land in the Brain vault under the topic folder Claude picks.
- Shared YouTube URLs use the transcript pipeline and retain the same `why` context.
- Event and ticket links route to `Personal/Events & Experiences`.
- Adding a video directly to the playlist still works, but has no capture context and
  is conservatively routed to `hold` by the downstream ledger.
