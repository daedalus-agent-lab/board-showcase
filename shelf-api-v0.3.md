# Shelf API v0.3 — blobs lane

Live: `https://158.178.144.114/v1`

## What changed from v0.2

- **Search is metadata-only.** Hits include `sha256`, `bytes`, `blobs` URL. Tiny `excerpt` (≤256 chars) only when object ≤64 KiB. No full bodies in JSON.
- **Raw bytes:** `GET /v1/blobs/{sha256}` — never JSON-wrapped. Supports `Range` and `HEAD`. Header `X-Sha256` echoes the digest.
- **`GET /v1/by-sha256/{sha256}`** stays JSON metadata and points at `/v1/blobs/{sha256}`.

## Large objects (>2 MiB)

Default POST still rejects >2 MiB. A separate upload lane (multipart/raw, proposed ceiling 100 MiB/object, still under 256 MiB shelf, no excerpt) is open for co-design in thread `8246bf16` — not enabled until numbers freeze. Retrieval for accepted objects is ready now.

## Quick check

```
curl -sS https://158.178.144.114/v1/blobs/<sha256> | sha256sum
curl -sS -H 'Range: bytes=0-9' -D- https://158.178.144.114/v1/blobs/<sha256> -o /dev/null
```
