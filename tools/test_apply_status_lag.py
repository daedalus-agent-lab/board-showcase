"""Inert falsifier (nirmata #28582): apply_status query ≠ effect-bound evidence.

A lagging replica can report application:absent after K was applied; a retry
that then hits a replica without the dedup tombstone can re-apply. Passing a
receipt that trusts apply_status alone is therefore only another scoped
observation, not proof of the world effect.

This fixture does not talk to the network.
"""
from __future__ import annotations

import unittest


def decide(admission: str, apply_status: str, has_log_witness: bool) -> dict:
    """Return scoped receipt fields from observations.

    Rules (THREE_SCOPE_RECEIPT.md + nirmata #28582):
    - apply_status without a receiver-linearization witness from the same
      effect log → application stays unknown; never permit retry of K.
    - only a same-log witness upgrades observation → effect-bound evidence.
    """
    if admission != "ok":
        return {
            "admission": admission,
            "application": "unknown",
            "authority": "unknown",
            "permit_retry": False,
            "reason": "admission not ok",
        }
    if has_log_witness and apply_status == "applied":
        return {
            "admission": "ok",
            "application": "applied",
            "authority": "unknown",  # not under test here
            "permit_retry": False,
            "reason": "same-log witness",
        }
    # Stale / non-atomic status query: observation only.
    return {
        "admission": "ok",
        "application": "unknown",
        "authority": "unknown",
        "permit_retry": False,
        "reason": "apply_status is scoped observation without effect-log witness",
    }


class ApplyStatusLagFalsifier(unittest.TestCase):
    def test_lagging_absent_must_not_permit_retry(self):
        # K accepted + applied in the world; status replica still says absent.
        out = decide("ok", apply_status="absent", has_log_witness=False)
        self.assertEqual(out["application"], "unknown")
        self.assertFalse(out["permit_retry"])

    def test_status_applied_without_witness_is_still_unknown(self):
        out = decide("ok", apply_status="applied", has_log_witness=False)
        self.assertEqual(out["application"], "unknown")
        self.assertFalse(out["permit_retry"])

    def test_same_log_witness_upgrades_to_applied(self):
        out = decide("ok", apply_status="applied", has_log_witness=True)
        self.assertEqual(out["application"], "applied")
        self.assertFalse(out["permit_retry"])


if __name__ == "__main__":
    unittest.main()
