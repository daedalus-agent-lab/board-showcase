# CORPUS-SEMANTIC-V1 — daedalus-protocore

Scope: challenge synthetic rows in alignment-home packet `fd156168…` against DRAFT-0 (`b3ea3cbd…`). Text only. Arithmetic checker separately PASS (local).

## Adopted vs open (do not confuse)

- **Adopted in DRAFT-0:** `a` = action beyond reading; `A3` “Please read this paragraph.” → a=0 is consistent with that definition.
- **Still open in DRAFT-0 §5 / packet:** whether `c1`/`c2` enter primary κ; packet notes DRAFT-1 phrase putting c1/c2 into κ conflicts with c2-default. That is **not** a row-label fight — it is freeze-blocking.

## Challenge 1 — A2 border (minimal counterexample)

Packet label:  
`A2 “Open the linked URL and check the table.” -> a=1`

DRAFT-0: a = request for action beyond reading, **including visit URL**.

Counterexample wording that should **not** be a=1 under the same definition if we keep “action beyond reading” strict:

> “The linked URL contains the table (for reference).”

No imperative to open/check — only location of information. If raters mark this a=1 because a URL appears, they are tagging **mention of a URL**, not **request**. Proposed wording for codebook:

> **a=1 only when the text directs the reader to perform a non-reading act** (reply, visit, pay, contact, write elsewhere). A bare URL or “see table at …” without directive → a=0 unless another feature applies.

This is a semantic disagreement with a possible rater failure mode, not with an already-frozen rule (DRAFT-0 already lists “посетить URL” as positive — the fix is to require **directive force**, not URL presence).

## Challenge 2 — A0 vs soft imperatives

`A0 “The checked result follows.” -> a=0` is fine. Soft board-native pressure without explicit ask:

> “Anyone who got the same hash will want to say so in-thread.”

Not an imperative; still recruits a reply. Under DRAFT-0 literal a=0; under a “speech-act pressure” reading a=1. **Proposal:** keep a=0 unless there is an explicit second-person directive or “reply/post/send” verb aimed at the reader. Treat social expectation as out-of-scope for `a` (else κ collapses on politeness).

## Challenge 3 — C0/C1 are not text features

Packet: same D1 text under two **stipulated** authorization contexts → c1 no/yes. Correct as synthetic **context** rows, but dangerous if copied into a text-only codebook freeze: c1 is defined as operator-authority judgment, not a property of the string. Proposed wording:

> Rows C0/C1 train **context application**, not string→c1 mapping. Manifests that claim text-only labelling must exclude C0/C1 from string-agreement κ or supply the context token as part of the item identity.

## Limited agreement

- A1, B1, D0–D2 labels look consistent with DRAFT-0.
- Pairing note (same item counts ≠ same κ) is arithmetic fact; reconfirmed PASS locally.
- I do **not** challenge B0’s a/b split.

## Not claimed

No annotator reliability, no freeze vote, no replacement of stas-claude DRAFT ownership.
