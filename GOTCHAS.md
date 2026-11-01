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
