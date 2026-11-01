# Read-only Sure context

This optional local adapter connects extracted public finance symbols to the
owner's configured Sure account scope. It reads only accounts, holdings, and
securities through enumerated GET endpoints on a loopback API. It never imports
financial data or places a trade. The request cannot select a tenant, arbitrary
query, server, URL, or credential.

The input and output contracts are versioned in
`schemas/sure-context-request.v1.schema.json` and
`schemas/sure-context-response.v1.schema.json`. The examples use synthetic UUIDs
and values; they are not a deployed account snapshot.

Provision a dedicated read-scoped key outside this repository. The configured
principal and optional account-ID allowlist bound every lookup. Responses
project account type/subtype, supported security identity, dated positive/zero
position observations, safe record references, and snapshot metadata. Balances,
quantities, prices, institution/account names, and credentials are not part of
the model's input or public saved-item export.

Only explicit `$TICKER` and supported exchange-prefixed symbols are extracted
from source text. Company names and ambiguous identity hints remain unresolved.
Educational content without symbols can receive account-type context. A missing
security record does not prove non-ownership. Current ownership remains unknown
while upstream freshness is unverified; dated observations are preserved as
evidence, with source claims kept separate from personal context.

The adapter bounds requests, response sizes, pagination, refresh duration, and
retries. It rejects redirects and proxies. An optional private HMAC-authenticated
cache is bound to the credential, configured scope, account allowlist, and API
origin. Incomplete or inconsistent reads yield partial/unavailable context.
Failure preserves the source note rather than discarding it.

The content join runs after public summarization. Private notes use atomic
owner-only writes. Transient failures can create bounded retry intents beside
the processing state; automatic consumption of those intents is not implemented
in this release. Do not claim retry completion from intent creation alone.

Configuration requires explicit model filesystem isolation. See
[Linux setup](linux-sandboxes.md). Deployment credentials, real account scope,
private hostnames, key rotation records, and live API observations belong in
the operator's private storage, not this repository.
