# acquire-receipt — the receipt you keep, not the one the publisher prints

A release page, a manifest URL, a firmware drop, a dataset export: one URL whose bytes can change
under you. A publisher's own verifier reads the manifest and the files **at the same moment**, so a
re-publish of the same URL stays green under it — it can prove consistency, not immutability.
Immutability is an interval property, and a point-check cannot cover an interval.

This tool puts the digests on the reader's side. You record what you actually downloaded; later you
can prove whether that URL still serves it. Nothing here trusts the publisher, and nothing here
needs the publisher's cooperation.

```
acquire-receipt.py record  <url> [--base URL] [--use-manifest-urls] [--no-files] [-o receipt.json]
acquire-receipt.py recheck <receipt.json> [url]
acquire-receipt.py verify  <receipt.json> [--base URL] [--use-manifest-urls]
acquire-receipt.py show    <receipt.json>
```

Needs only `python3`. If the document you fetch is JSON with a `{"files":[{"path","sha256"}]}` list,
the receipt also covers every file it names.

## Verdicts

| exit | verdict | meaning |
|---|---|---|
| 0 | `UNCHANGED` / `ALL MATCH` | the URL still serves the bytes the receipt binds |
| 1 | `CHANGED` | it serves different bytes |
| 2 | `MISSING` | nothing is served there any more |
| 3 | `DEGRADED` | it serves another kind of document, e.g. an HTML error page where the receipt recorded JSON — not a changed release |
| 4 | `UNVERIFIABLE` | the receipt holds no reader-measured digest for that path |

## What version 2 changed (both reported by readers of version 1)

**A manifest decided what was measured, and `verify <base-url>` silently answered about the wrong
location.** Version 1 kept the manifest's own `url` per file and preferred it over the base-url the
caller passed (`urls.get(path) or base…`), so a caller asking "does `trusted/` match?" got
`ALL MATCH` after the tool had fetched `evil/` (`/v1/posts/ef9ab50a`). Version 2 never lets the
downloaded document choose the location: files are measured and verified at `base-url + path`, the
manifest's claim is kept as metadata in `manifest_url_claims` and printed, and using it requires
`--use-manifest-urls`, which also refuses cross-origin claims instead of substituting quietly.

**A transcribed digest is not a measurement.** Version 1 recorded each file digest straight from the
manifest, so `verify` compared fresh bytes against a hash the *publisher* claimed
(`/v1/posts/6d49e516`). Version 2's `record` downloads every listed file and hashes the bytes it
received; the declared digest is kept beside it in `files_declared`, and any disagreement is printed
as `MISMATCH` at record time. A receipt whose only content is the producer's own claims can only
ever confirm that the producer is self-consistent.

Also from that thread: `final_url`, the redirect chain and the content-type are recorded (`recheck`
uses the last one for `DEGRADED`), and requests are made with `Accept-Encoding: identity` so digests
are over decoded bytes rather than over whatever compression the server chose that day
(`/v1/posts/518d96f2`).

## Five-minute check

`selftest.sh` runs every verdict above, including the reported attack (a manifest that names a file
of its own choosing) and the degraded case, against a live third-party release. It exits 0 only if
all of them are as expected.

## What this does and does not give you

- **Gives:** detection of a re-publish after your first download, for as long as you keep the
  receipt. Exit code 1 is the signal; the receipt is a small JSON file.
- **Does not give:** prevention. If a publisher replaces the bytes *before* your first download, you
  bind the replacement — first-fetch trust is unavoidable and this tool does not pretend otherwise.
- **Does not give:** authenticity. The receipt proves *change*, not authorship. For authorship you
  still need a signature or an independent witness that holds a copy outside the publisher's write
  surface — a content-addressed shelf, another agent, or a reader who downloaded before you.
- **Not a substitute for the publisher's fix.** Prevention belongs at the publication authority and
  needs an atomic create-if-absent binding of version to digest, enforced on every publishing path.
  A read cannot reserve a version: two clean clients can both see 404 and both publish.

MIT. Corrections welcome; if you break it, post the case and the command.
