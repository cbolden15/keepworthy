# Security and private data

Do not commit credentials, cookie jars, HAR captures, saved-feed responses,
private notes, account snapshots, or deployment records. Local ignore rules are
a convenience; review staged files and scan commit history before pushing.

```sh
gitleaks git --redact --no-banner --log-opts=--all .
```

Source posts, captions, images, transcripts, and model output are untrusted data.
The plain text-only CLI path has no tools or external MCP servers. Image reading
and private finance joins require the explicit Linux sandbox. The offline media
worker has no external network, host homes, credential files, or runtime sockets.
Root helper configuration is read from owner-controlled root-owned files; tool
requests cannot override commands, paths, mounts, or principals.

Sure credentials must be independently provisioned read-only. The application
only issues enumerated GET requests and projects the minimum context needed for
local notes. Private context is joined after model execution and excluded from
saved-item exports. Use a dedicated OS identity when operating alongside other
private workloads; this repository does not configure system-wide privileges.

If you discover a credential leak, revoke it before cleaning history. Report
code vulnerabilities through the repository's private security advisory channel
when available. Do not include live account data or secrets in public issues.
