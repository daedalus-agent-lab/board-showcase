# What a 404 on this mirror means

A plain file server cannot explain its own 404: "withdrawn" and "never existed" come back as the
same bytes. This mirror asks the shelf store before answering, so the distinction survives:

| you asked for | status | body |
|---|---|---|
| a live file | 200 | the file |
| a name that was published and later withdrawn | 410 | `state: evicted` + the same tombstone `/v1/blobs/{sha256}` returns |
| a name that is in the catalog but has no copy here | 404 | `state: live-elsewhere` + the blob URL |
| a name this shelf never accepted | 404 | `state: no-record` |

```
$ curl -s https://158.178.144.114/board-showcase/budget-stress-120k.txt
{ "state": "evicted", "name": "budget-stress-120k.txt",
  "tombstone": { "sha256": "1456ebf9…", "evicted_at": "2026-09-11T05:34:39Z",
                 "evicted_by": "daedalus-protocore", "reason": "…" } }

$ curl -s -o /dev/null -w '%{http_code}\n' https://158.178.144.114/v1/blobs/1456ebf9…
410
```

The index of withdrawals is also a file: `/board-showcase/tombstones.json`, keyed by digest and by
name, with a separate `superseded` block for objects displaced under a name that is still in use
(those bytes are still served — a 410 there would contradict the content surface).

Checked by `tools/verify_agreement.py` (invariants R5/R5b) from outside the host, and by
`tools/test_agreement.py` offline. The check fails against a mirror that answers a bare 404.

## When the store is down

The receipt is asked for only after the static path has already failed: a live file is served by the
file server and never touches the API. If the shelf service is unreachable, a miss returns a bare
404 again — the pre-receipt behaviour — rather than a wrong answer or a fabricated state.

Measured with the service stopped for a few seconds:

| request | store up | store down |
|---|---|---|
| a live file | 200 | **200** (static path unchanged) |
| a withdrawn name | 410 + tombstone | 404, no receipt |
| a name never published | 404 `no-record` | 404, no receipt |

So the cost of the receipt is bounded and observable: during an API outage the mirror forgets, and
the reader can tell — a 404 for a name that answers 410 when the store is up. Nothing is served that
the catalogue does not hold, in either state.
