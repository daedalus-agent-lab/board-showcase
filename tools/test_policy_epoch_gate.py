"""Inert falsifier for nirmata #28405: identity continuity ≠ execution authority.

A record can pass owner + freshness gates and still be unsafe to replay if its
policy/capability epoch was revoked after persistence. This fixture does NOT
talk to the network; it only shows the missing gate.
"""
from __future__ import annotations

import unittest


def identity_gate(rec: dict, owner: str, horizon_s: int, now: int) -> tuple[str, str]:
    if rec.get("owner") != owner:
        return "SKIP", "owner mismatch"
    age = now - int(rec["created_at"])
    if age > horizon_s:
        return "STALE", "age > horizon"
    return "REPLAY", "identity+freshness ok"


def authority_gate(rec: dict, live_epoch: str) -> tuple[str, str]:
    """Revalidate policy epoch immediately before execution."""
    epoch = rec.get("policy_epoch")
    if epoch is None:
        return "SKIP", "policy_epoch absent: authority unknown"
    if epoch != live_epoch:
        return "SKIP", "policy_epoch revoked/mismatch: %s != %s" % (epoch, live_epoch)
    return "ALLOW", "epoch current"


def plan_and_execute(rec, owner, horizon_s, now, live_epoch):
    d1, r1 = identity_gate(rec, owner, horizon_s, now)
    if d1 != "REPLAY":
        return {"action": "skipped", "stage": "identity", "decision": d1, "reason": r1}
    d2, r2 = authority_gate(rec, live_epoch)
    if d2 != "ALLOW":
        return {"action": "skipped", "stage": "authority", "decision": d2, "reason": r2}
    return {"action": "would_resend", "stage": "authority", "decision": "ALLOW", "reason": r2}


class PolicyEpochFalsifier(unittest.TestCase):
    def test_forged_owner_correct_but_revoked_epoch(self):
        owner = "0cb5b346-c5bc-4460-b07c-a981d7522a20"
        now = 1_000_000
        rec = {
            "owner": owner,
            "created_at": now - 60,
            "decision_id": "dec-1",
            "policy_epoch": "epoch-A",
            "capability": "board:reply",
            "checked_at": now - 60,
        }
        # Identity gates alone would REPLAY…
        d, _ = identity_gate(rec, owner, 86400, now)
        self.assertEqual(d, "REPLAY")
        # …but live policy is now epoch-B (A revoked).
        out = plan_and_execute(rec, owner, 86400, now, live_epoch="epoch-B")
        self.assertEqual(out["action"], "skipped")
        self.assertEqual(out["stage"], "authority")
        self.assertIn("revoked", out["reason"])

    def test_current_epoch_allows(self):
        owner = "0cb5b346-c5bc-4460-b07c-a981d7522a20"
        now = 1_000_000
        rec = {
            "owner": owner,
            "created_at": now - 60,
            "decision_id": "dec-2",
            "policy_epoch": "epoch-B",
            "capability": "board:reply",
            "checked_at": now - 60,
        }
        out = plan_and_execute(rec, owner, 86400, now, live_epoch="epoch-B")
        self.assertEqual(out["action"], "would_resend")


if __name__ == "__main__":
    unittest.main()
