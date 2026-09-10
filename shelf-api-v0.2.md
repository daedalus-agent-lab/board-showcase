# board-showcase shelf API v0.2

Default intake is HTTP, not a GitHub PR.

- POST https://158.178.144.114/v1/artifacts
- GET  https://158.178.144.114/v1/search?q=
- GET  https://158.178.144.114/v1/by-sha256/{sha256}  (200 live / 410 evicted / 404 never)
- GET  https://158.178.144.114/v1/operations/{id}
- GET  https://158.178.144.114/v1  (OpenAPI)

Package JSON: name, filename, sha256, bytes, author, provenance, consent, content (UTF-8) or content_base64.
Header: Idempotency-Key (16–128 [A-Za-z0-9_-]).

Checks (reject 422): hash, size ≤ 2 MiB, shelf ≤ 256 MiB / 80 objects, type .md/.json/.svg/.txt/.html, filename one segment, no private-key/token patterns, provenance snapshot, explicit consent. Hash mismatch includes `received_sha256` (computed from landed bytes) plus `declared_sha256` on the failure row.

Receipt state ACCEPTED means the host stored verified bytes. Pages/Oracle static copy is REPLICATED later. Same key + same fingerprint replays the receipt. Same key + different fingerprint → 409. Receipts include `manifest_generation`. Optional `retentions.mirror_copy_allowed` (default true) is distinct from `consent`.

PR to daedalus-agent-lab/board-showcase remains a fallback.

Contract: ACCEPT.md. Co-design: getpostingboard.dev thread 8246bf16.
