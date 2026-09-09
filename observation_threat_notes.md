# Observation envelope draft — threat notes

Companion to `observation_envelope_draft.json` for astranaut01 (#28262).
Data-only; no executable code; no signature.

## Fields kept strictly separate
- `subject_sha256` — what was observed (release archive bytes)
- `tool_sha256` — what did the observing
- `issuer` / `runner` — who claimed / who executed (here SAME_PROCESS)
- `observed_at` + `clock_uncertainty_ms` — when, with honest uncertainty
- `predicate_type` / `outcome` — what claim class and structured result
- `limitations` — non-claims

## Attacks called out
1. **Cross-request replay** — unsigned JSON can be pasted elsewhere as if new. Signed version needs nonce + reject-on-reuse.
2. **Tool/subject swap** — keep subject digest, replace tool with a liar. Allowlist tools; require both digests.
3. **Self-issued trust root** — issuer==runner is transparency at best, not attestation.
4. **False issuer/runner equality** — never interpret equality as two independent parties; see `issuer_runner_relation`.

## Non-goals for this draft
Executable verifier, real signature, append-only observation log, NTP anchoring.
