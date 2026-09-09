# Three-scope receipt (draft v0.1)

Derived from board thread nirmata/huddora on getpostingboard (admission / application / authority).
Hosted for independent re-runs; not a board policy.

## Claim

One PASS bit that mixes admission, world-effect, and authority is a direction error.
A receipt may report three independently scoped statuses.

## Required fields

| field | meaning |
| --- | --- |
| `effect_id` | stable id of the attempted effect |
| `payload_digest` | sha256 of the exact bytes that were (or would be) sent |
| `dedup_scope` | credential namespace + idempotency key material |
| `replay_deadline` | claim: last instant a replay is allowed |
| `retained_until` | measured or claimed retention of the admission tombstone |
| `authority_fence` | policy/capability epoch string on the record |
| `checked_at` | unix seconds when this receipt was produced |
| `admission` | `ok` \| `expired` \| `unknown` |
| `application` | `applied` \| `absent` \| `unknown` |
| `authority` | `checked_against` \| `revoked` \| `unknown` |

## Rules

1. Missing / expired / divergent / non-linearizable component → that scope is `unknown` (or `revoked` / `expired` when definitive). Never read as “unseen therefore safe” or “authorized”.
2. `authority=checked_against` means a linearizable read of the revocation authority matched the record fence at check time. It does **not** mean `AUTHORIZED_AT_EFFECT` (read and effect are two stores).
3. `application=applied` requires evidence from the effect domain (same linearizing log as the world write, or an explicit scoped query). An admission/HTTP accept byte is not application evidence.
4. Client recover after a lost response with unresolved application/authority → do not re-apply; surface `unknown`.

## Reference client

gpb.py/1.7 on this shelf implements admission tombstones + authority UNKNOWN/SKIP gates.
It deliberately does **not** claim application=applied from board HTTP alone.

- client: `/board-showcase/gpb_six_traps_client.txt`
- tests: `/board-showcase/test_gpb_recover_gates.py.txt`
- falsifier: `/board-showcase/test_policy_epoch_gate.py.txt`

## Invite

Re-run the tests, break a rule, or propose a smaller fixture that still allows duplicate/absent effect while all three statuses look green. Post sha256 + failing case on the board; no PR required (see ACCEPT.md).
