# "Save to Keepworthy" iOS Shortcut

Share a link from any app on your phone to the Keepworthy `/share` endpoint.
The shortcut asks why you are saving it, and that reason ends up on the note.

Before you start, run `python3.12 -m src.ingest_server` with `INGEST_TOKEN` set
in `config.env`, and make sure your phone can reach it. The server binds to
`127.0.0.1` by default, so set `INGEST_BIND` to an address your phone can
reach. Use a private network such as a home LAN or a VPN. Don't expose the
port to the internet.

## Build the shortcut (Shortcuts app)

1. New Shortcut → rename it to **Save to Keepworthy**.
2. Shortcut settings (ⓘ) → enable **Show in Share Sheet** → accepted types: **URLs** and **Text**.
3. Add action **Get URLs from Input** (input: Shortcut Input).
4. Add action **Ask for Input**:
   - Prompt: `Why are you saving this?`
   - Input type: **Text**
   - Require a one-line answer.
5. Add action **Get Contents of URL**:
   - URL: `http://<your-server>:8787/share` (the host and `INGEST_PORT` from `config.env`)
   - Method: **POST**
   - Headers: `Authorization` = `Bearer <INGEST_TOKEN from config.env>`
   - Request Body: **JSON** with fields:
     - `url` = *URLs* (magic variable from step 3)
     - `why` = *Provided Input* (magic variable from step 4)
     - `text` = app-provided caption or selected text, when useful (optional)
6. Add action **Show Notification**: "Saved ✓".

## Use

In any app: Share → **Save to Keepworthy**. Your phone has to be on the same
network as the server, or connected to its VPN.
If you see a failure banner, the link was not queued. Fix the connection and
share it again.

## Notes

- The endpoint answers right away with 202. The next poll run processes the
  link, every five minutes with the example timer.
- Notes land in your vault under the topic folder the model picks.
- Shared YouTube links go through the transcript pipeline and keep the same `why`.
- Event and ticket links go to `Personal/Events & Experiences` with the default
  subtopics.
- Adding a video straight to the playlist still works, but the note has no `why`.
