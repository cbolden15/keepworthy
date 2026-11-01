# Optional Linux sandboxes

This is an operator-managed installation for Ubuntu 24.04, x86-64, and Python
3.12. Basic transcript-only processing does not need it. Legacy `youtube-obsidian`
runtime paths and `yt-*` helper names are retained for compatibility. Cross-platform media
installation is outside this release. Review the helper source and your host's
namespace policy before granting scoped sudo access; do not disable AppArmor or
mount host home directories to work around an error.

## Model launcher

Install Bubblewrap through your distribution. Use an existing authenticated
Claude Code installation; the isolated flags were verified with version
2.1.310. This repository does not distribute the proprietary CLI binary.

Copy that exact native executable to `/opt/youtube-obsidian/claude`, root-owned
and mode 0555. Install `bin/model_sandbox.py` as the root-owned executable
`/usr/local/sbin/yt-model-sandbox`, mode 0555. Its parent directories must not
be writable by the processing user.

Copy `examples/model-sandbox.json` to
`/etc/youtube-obsidian/model-sandbox.json`, root-owned and mode 0644 or stricter.
Set `operator_uid` to the processing user's numeric UID, and set the binary and
authentication-file references to your actual deployment. The configuration is
trusted operator input; it cannot be supplied in a model/tool request.

The referenced authentication file contains exactly one
`CLAUDE_CODE_OAUTH_TOKEN=...` line. Obtain it through your own CLI authentication
workflow, outside the repository. It must be an owner-only regular file owned
by the configured processing UID. The launcher does not inherit API billing
keys, SSH sockets, model hooks, MCP servers, or the caller's environment.

Restrict sudo to this exact root-owned launcher with no command-line arguments;
it accepts bounded JSON data on stdin. A sudoers administrator can grant the
processing user only `/usr/local/sbin/yt-model-sandbox ""`. Do not grant shell,
Python interpreter, arbitrary script, wildcard argument, or general sudo access.
Validate sudoers changes with your distribution's `visudo` tooling.

```sh
printf '%s' '{"version":1,"operation":"probe","model":"sonnet","prompt":"","payload":"","images":[],"probe_paths":["/home/operator","/var/lib/youtube-obsidian","/etc/youtube-obsidian"]}' | sudo -n /usr/local/sbin/yt-model-sandbox
```

The model namespace deliberately contains an empty temporary home. For the
probe above, replace `/home/operator` with your actual host home path: a
temporary model home is legitimate, while your host home must be absent. The
probe verifies selected images, hidden host paths, CLI startup, and OAuth
authentication. Do not enable `MODEL_SANDBOX=true` until it succeeds. A failed
installed sandbox never falls back to direct execution.

## Offline media worker

Install FFmpeg/ffprobe through your distribution. Prepare a fresh dependency
directory using `requirements-media.lock`; it pins every runtime dependency and
artifact hash. The runtime uses faster-whisper 1.2.1, CTranslate2 4.8.4, and PyAV
18.1.0. It needs CPU support, not a GPU service or paid transcription provider.

Download wheels as an unprivileged operator; package build scripts are disabled:

```sh
python3.12 -m venv /absolute/staging/bootstrap
/absolute/staging/bootstrap/bin/python -m pip download --require-hashes --only-binary=:all: --no-deps -r requirements-media.lock --dest /absolute/staging/wheels
```

Install those wheels in a disposable sandbox/container with no network or
credential mounts. Within that environment, use the offline command:

```sh
python3.12 -m pip install --no-index --find-links=/absolute/staging/wheels --no-deps --no-compile --require-hashes --only-binary=:all: -r requirements-media.lock --target /absolute/staging/site-packages
```

The absolute paths in that command refer to mounts inside your preparation
environment. Export only the resulting dependency directory. No other project,
existing virtual environment, user profile, or private deployment is required.

Download only the model files listed in `examples/whisper-model.json` from its
pinned Hugging Face repository revision. Check each SHA-256 before installation.
The weights are public data, not remote Python code. Loading uses
`local_files_only=True`, with external networking unavailable inside the worker.
Changing weights or dependencies requires updating the pinned metadata and
repeating the offline probe, fixed image eval, and media round trip.

Install these root-owned, non-user-writable paths:

| Path | Contents |
| --- | --- |
| `/opt/youtube-obsidian/media/site-packages` | Frozen installed dependency directory |
| `/opt/youtube-obsidian/media/model` | Verified model files |
| `/opt/youtube-obsidian/media/worker.py` | `bin/media_worker.py` |
| `/usr/local/sbin/yt-media-sandbox` | `bin/media_sandbox.py`, mode 0555 |
| `/etc/youtube-obsidian/media-sandbox.json` | Edited `examples/media-sandbox.json` |

Set the media configuration's numeric operator UID to the same processing
identity. Grant scoped no-argument sudo access to the media launcher only,
following the model-launcher procedure. The fixed request protocol accepts
media bytes, not host filenames, commands, mounts, credentials, or network URLs.

```sh
printf '%s' '{"version":1,"operation":"probe","base64":""}' | sudo -n /usr/local/sbin/yt-media-sandbox
```

The worker has private PID/user/network namespaces, no capabilities, no host
homes, no credential files, and no external network. Only public runtime
libraries, the immutable model/dependency tree, copied input, and temporary
output are mounted. Four CPU threads, an 8 GiB address-space limit, and a
600-second timeout bound execution. The model launcher separately permits
network access for your configured subscription, with only selected media
copies and no host finance files visible.

Enable `MODEL_SANDBOX=true`, `FREE_MEDIA_ENABLED=true`, and
`SOCIAL_BATCH_SIZE=3` in private configuration only after validating both paths.
Test with a source you are permitted to process. Inspect the retained transcript
and observations, not merely the process exit code. Eight video samples do not
prove every frame was read. Image observations are model output and can contain
errors even when the response schema is valid.

## Optional Sure context

Provision a dedicated Sure key with exactly the read scope. Prepare an owner-only
JSON file using `examples/sure-context.json`. The key file must also be owner-only,
and the adapter uses the loopback API beside Sure. Set `SURE_CONTEXT_CONFIG` to
that absolute JSON path. The normal CLI mode cannot enable this integration;
`MODEL_SANDBOX=true` is required to keep credentials and enriched notes outside
the model filesystem. See [contract](sure-context.md).

The live finance system is authoritative. This pipeline writes source notes,
not financial records, transactions, or trades. Local notes contain the joined
context, so private storage/backup/export rules still apply after processing.
