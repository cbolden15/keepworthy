# Deployment examples

Start with the manual transcript-only command in README. The systemd files are
user-unit examples for a Linux checkout at `~/Projects/keepworthy`.
Edit the checkout location or interpreter if yours differs. They do not install
credentials, browser profiles, model weights, or private deployment records.

## Schedule processing and discovery

Create a private `config.env` in your checkout and an owner-only discovery JSON
outside the repo, based on `examples/saved-discovery.json`. Set its queue to the
same absolute location used by `QUEUE_PATH`. Keep cookies outside Git and set
`COOKIES_PATH` separately for the processor's metadata client.

```sh
mkdir -p ~/.config/systemd/user
cp systemd/yt-obsidian.service systemd/yt-obsidian.timer ~/.config/systemd/user/
cp systemd/saved-discovery.service systemd/saved-discovery.timer ~/.config/systemd/user/
systemd-analyze --user verify ~/.config/systemd/user/yt-obsidian.service ~/.config/systemd/user/saved-discovery.service
systemctl --user daemon-reload
# Run each oneshot manually and inspect its result before enabling its timer.
systemctl --user start saved-discovery.service
systemctl --user show saved-discovery.service -p Result -p ExecMainStatus
systemctl --user enable --now saved-discovery.timer yt-obsidian.timer
```

The discovery example reads `~/.config/youtube-obsidian/saved-discovery.json` and
runs daily at 06:00 UTC with up to five minutes of jitter. Adjust the timezone
explicitly for your own schedule. The processor runs every five minutes, in
bounded social batches. Both user timers require a persistent user session or
operator-approved lingering to run while logged out.

The model CLI uses your configured account. Optional direct-mode headless OAuth
can be supplied through `~/.config/youtube-obsidian/auth.env`, mode 0600. Never
commit it. The isolated launcher uses its separate trusted root-owned install
configuration described in [Linux sandboxes](docs/linux-sandboxes.md).

## Inspect and recover

```sh
systemctl --user show yt-obsidian.service -p ActiveState -p Result -p ExecMainStatus
systemctl --user list-timers yt-obsidian.timer saved-discovery.timer --all
journalctl --user -u saved-discovery.service -n 20 --no-pager
python3.12 -m src.saved_discovery --config /absolute/private/saved-discovery.json --status
```

An authentication failure needs owner sign-in and an atomic replacement of the
private cookie file. Do not bypass account challenges. Gaps require recovery of
the omitted interval using verified pagination before resetting their flag.

Stop timers and wait for active jobs before changing source or restoring data.
Back up the queue, its rotated processing batch, processing state, discovery
SQLite database, and notes together. Preserve permissions. Keep private
recovery records outside this repository. Disabling offline media keeps
free-only videos queued; it does not permit paid transcription fallback.

The optional ingest service is a separate authenticated HTTP endpoint. Review
its bind address and token before exposing it beyond loopback. Export/pull
mechanisms for your own notes are operator-managed and are not installed here.
