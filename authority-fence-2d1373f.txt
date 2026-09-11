#!/usr/bin/env python3
"""
authority_fence.py — a minimal, offline, stdlib-only experiment about one claim:

    A fresh read of an authority record cannot establish that a delayed action
    was NOT applied. Only the store that admits the action can establish that,
    and only within a bounded scope.

Three arms:

  A  UNFENCED   The worker re-reads the grant, sees it valid, and applies late.
                Revocation lands between the read and the apply. The apply
                succeeds. The read was true when taken and useless as a bound.

  B  FENCED     The same interleaving against a store that enforces admission
                inside the critical section that reads the epoch. The apply is
                refused and the store issues a bounded NOT_APPLIED witness.

  C  OBSERVER   A read-only third party tries to establish NOT_APPLIED by
                reading. C1 shows the inference "no APPLIED record => not
                applied" is false (an in-flight apply lands afterwards).
                C2 shows the honest output: UNKNOWN, permit_retry=false.

Plus a positive control: a deliberately BROKEN fence (check outside the lock)
that the harness must catch under randomized thread interleaving. A harness
that cannot fail is not a harness.

Run:  python3 authority_fence.py            # deterministic arms, JSON report
      python3 authority_fence.py --fuzz 400 # add randomized thread fuzz
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import threading
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Optional

SCHEMA = "authority-fence-witness/1"


# ---------------------------------------------------------------- primitives

def _h(*parts: str) -> str:
    d = hashlib.sha256()
    for p in parts:
        d.update(p.encode("utf-8"))
        d.update(b"\x1f")
    return d.hexdigest()


@dataclass(frozen=True)
class Authority:
    """What a worker holds after reading. A snapshot, not a promise."""
    grant_id: str
    subject: str
    epoch: int
    read_at_index: int          # log position the read observed
    read_said: str              # "VALID" at read time — recorded to make the point


@dataclass
class LogEntry:
    index: int
    kind: str                   # GRANT | REVOKE | APPLIED | REFUSED
    subject: str
    epoch: int
    request_key: Optional[str]
    prev_head: str
    head: str


# ---------------------------------------------------------------- the store

class Store:
    """
    The only entity that can admit an effect, and therefore the only entity
    that can witness that an effect was not admitted.

    Invariants (asserted by the test suite):
      I1  epoch[subject] is monotonically non-decreasing.
      I2  admission (read epoch + decide + append) happens in ONE critical
          section. A check outside the lock is the bug this file exists for.
      I3  idempotency is scoped by (subject, request_key): the log contains at
          most one APPLIED entry for that tuple; the same key may be used by
          another subject without replaying this subject's receipt.
      I4  a NOT_APPLIED witness is issued only by the admission path and names
          a log position; it is scoped to this store and the presented subject
          and epoch. It does not reserve/seal the request_key for later calls.
      I5  no APPLIED entry exists whose epoch is stale w.r.t. the epoch at its
          own log index, when the fence is enforced.
    """

    def __init__(self, store_id: str = "store-1", enforce_fence: bool = True,
                 broken_fence: bool = False, window_s: float = 0.0):
        self.store_id = store_id
        self.enforce_fence = enforce_fence
        self.broken_fence = broken_fence          # positive control only
        # Widens the check-then-act window of the BROKEN store so the defect is
        # observable in a short run. It does not create the defect: it makes an
        # existing one visible on a machine that would otherwise hide it behind
        # scheduling luck. The correct store ignores it — its check and its
        # append are in one critical section, so there is no window to widen.
        self.window_s = window_s
        self._lock = threading.RLock()
        self.log: list[LogEntry] = []
        self.head = "0" * 64
        self._epoch: dict[str, int] = {}
        self._revoked: dict[str, bool] = {}
        # Idempotency is deliberately subject-scoped: a request key is not a
        # global namespace shared by unrelated subjects.
        self._idem: dict[tuple[str, str], dict] = {}  # (subject, request_key) -> receipt
        self.effects: list[str] = []              # the side effect being guarded

    # --- append-only hash-chained log -------------------------------------

    def _append(self, kind: str, subject: str, epoch: int,
                request_key: Optional[str]) -> LogEntry:
        prev = self.head
        idx = len(self.log)
        head = _h(prev, str(idx), kind, subject, str(epoch), request_key or "-")
        e = LogEntry(idx, kind, subject, epoch, request_key, prev, head)
        self.log.append(e)
        self.head = head
        return e

    # --- writes -----------------------------------------------------------

    def grant(self, subject: str) -> Authority:
        with self._lock:
            ep = self._epoch.get(subject, 0) + 1
            self._epoch[subject] = ep
            self._revoked[subject] = False
            e = self._append("GRANT", subject, ep, None)
            return Authority(f"grant-{subject}-{ep}", subject, ep, e.index, "VALID")

    def revoke(self, subject: str) -> dict:
        """Revocation is a write that bumps the fence. It is NOT a message to
        the worker; the worker may never see it. That is the whole problem."""
        with self._lock:
            ep = self._epoch.get(subject, 0) + 1
            self._epoch[subject] = ep
            self._revoked[subject] = True
            e = self._append("REVOKE", subject, ep, None)
            return {"revoked_at_index": e.index, "new_epoch": ep, "head": e.head}

    # --- reads (available to anyone, including read-only observers) --------

    def read_authority(self, a: Authority) -> dict:
        """A fresh read. True at the instant taken. Bounds nothing after it."""
        with self._lock:
            return {
                "subject": a.subject,
                "presented_epoch": a.epoch,
                "store_epoch": self._epoch.get(a.subject, 0),
                "revoked": self._revoked.get(a.subject, False),
                "valid_now": (not self._revoked.get(a.subject, False)
                              and a.epoch == self._epoch.get(a.subject, 0)),
                "read_at_index": len(self.log) - 1,
                "head": self.head,
            }

    def snapshot(self) -> dict:
        with self._lock:
            return {"log_len": len(self.log), "head": self.head,
                    "effects": list(self.effects)}

    # --- the admission path ----------------------------------------------

    def apply_effect(self, a: Authority, request_key: str,
                     gap: Optional[Callable[[], None]] = None) -> dict:
        """
        Admit or refuse. `gap` is a test hook fired INSIDE or OUTSIDE the
        critical section depending on `broken_fence`, so the race is explicit
        and reproducible instead of hoped for.
        """
        if self.broken_fence:
            # POSITIVE CONTROL: check-then-act with the window wide open.
            stale = self._is_stale(a)
            if gap:
                gap()                      # revocation can land right here
            if self.window_s:
                time.sleep(self.window_s)  # make the existing window visible
            with self._lock:
                if self.enforce_fence and stale:
                    return self._refuse(a, request_key, "FENCE_STALE")
                return self._admit(a, request_key)

        if gap:
            gap()                          # revocation lands BEFORE the lock
        with self._lock:
            idem_key = (a.subject, request_key)
            if idem_key in self._idem:
                r = dict(self._idem[idem_key])
                r["replay"] = True
                return r
            if self.enforce_fence and self._is_stale_locked(a):
                return self._refuse(a, request_key, "FENCE_STALE")
            return self._admit(a, request_key)

    # --- internals --------------------------------------------------------

    def _is_stale(self, a: Authority) -> bool:
        with self._lock:
            return self._is_stale_locked(a)

    def _is_stale_locked(self, a: Authority) -> bool:
        return (self._revoked.get(a.subject, False)
                or a.epoch != self._epoch.get(a.subject, 0))

    def _admit(self, a: Authority, request_key: str) -> dict:
        e = self._append("APPLIED", a.subject, a.epoch, request_key)
        self.effects.append(request_key)
        receipt = {
            "schema": SCHEMA,
            "decision": "APPLIED",
            "scope": {"store_id": self.store_id, "subject": a.subject,
                      "request_key": request_key},
            "presented": {"grant_id": a.grant_id, "epoch": a.epoch},
            "observed": {"store_epoch": self._epoch.get(a.subject, 0),
                         "revoked": self._revoked.get(a.subject, False)},
            "position": {"log_index": e.index, "prev_head": e.prev_head,
                         "head": e.head},
            "issued_by": "store:admission",
            "replay": False,
        }
        self._idem[(a.subject, request_key)] = dict(receipt)
        return receipt

    def _refuse(self, a: Authority, request_key: str, reason: str) -> dict:
        e = self._append("REFUSED", a.subject, a.epoch, request_key)
        return {
            "schema": SCHEMA,
            "decision": "NOT_APPLIED",
            "reason": reason,
            "scope": {"store_id": self.store_id, "subject": a.subject,
                      "request_key": request_key},
            "presented": {"grant_id": a.grant_id, "epoch": a.epoch},
            "observed": {"store_epoch": self._epoch.get(a.subject, 0),
                         "revoked": self._revoked.get(a.subject, False)},
            "position": {"log_index": e.index, "prev_head": e.prev_head,
                         "head": e.head},
            "issued_by": "store:admission",
            "binding": "refused_at_this_store_for_this_subject_and_presented_epoch",
            "does_not_claim": [
                "no effect outside this store",
                "that this request_key is sealed or reserved",
                "exactly-once end to end",
                "the worker stopped running",
                "a global order of wall-clock time",
            ],
            # Definite only for this admission attempt: the effect was refused
            # here at this position. A later call must be evaluated afresh.
            "permit_retry": True,
        }


# ------------------------------------------------------- read-only observer

class ReadOnlyObserver:
    """
    A third party with read access and no ability to write the seal. It can
    read the log, poll, and wait. It cannot close the future.
    """

    def __init__(self, store: Store, name: str = "observer"):
        self._store = store       # deliberately: only read methods are used
        self.name = name

    def applied_record_present(self, subject: str, request_key: str) -> bool:
        for e in self._store.log:            # read-only scan
            if e.kind == "APPLIED" and e.subject == subject and e.request_key == request_key:
                return True
        return False

    def naive_conclusion(self, subject: str, request_key: str) -> dict:
        """The inference this article argues is unsound."""
        applied = self.applied_record_present(subject, request_key)
        return {
            "schema": SCHEMA,
            "decision": "APPLIED" if applied else "NOT_APPLIED",
            "reason": "ABSENCE_OF_RECORD" if not applied else "RECORD_PRESENT",
            "issued_by": f"reader:{self.name}",
            "unsound": not applied,
        }

    def honest_conclusion(self, subject: str, request_key: str,
                          polls: int = 3) -> dict:
        applied = False
        for _ in range(polls):
            if self.applied_record_present(subject, request_key):
                applied = True
                break
        if applied:
            # Positive evidence is one-sided and IS available to a reader.
            return {
                "schema": SCHEMA, "decision": "APPLIED",
                "reason": "RECORD_PRESENT_IN_LOG",
                "issued_by": f"reader:{self.name}",
                "binding": "existence only; says nothing about how many times",
                "permit_retry": False,
            }
        return {
            "schema": SCHEMA,
            "decision": "UNKNOWN",
            "reason": "NO_ADMISSION_AUTHORITY",
            "detail": ("absence of an APPLIED record at read time is compatible "
                       "with an admitted-but-not-yet-logged apply, an in-flight "
                       "apply, and a genuinely refused apply"),
            "observer_capability": "read_only",
            "issued_by": f"reader:{self.name}",
            "polls": polls,
            # An UNKNOWN outcome for a non-idempotent effect must NOT be retried:
            # retry converts "maybe once" into "maybe twice".
            "permit_retry": False,
            "escalate": ["obtain a store-issued NOT_APPLIED witness",
                         "or read a durable idempotency record under the same key"],
        }


# --------------------------------------------------------------- the arms

def arm_a_unfenced() -> dict:
    """Fresh read says VALID. Revocation lands. Apply still succeeds."""
    s = Store("A-unfenced", enforce_fence=False)
    auth = s.grant("agent-x")
    read = s.read_authority(auth)                 # t0: fresh, true, VALID
    rev = s.revoke("agent-x")                     # t1: operator revokes
    receipt = s.apply_effect(auth, "req-1")       # t2: delayed apply
    return {
        "arm": "A_unfenced_delayed_apply_succeeds",
        "read_at_t0": read,
        "revocation": rev,
        "apply_receipt": receipt,
        "effects": s.effects,
        "log": [asdict(e) for e in s.log],
        "conclusion": ("the read was true when taken and bounded nothing after it; "
                       "the apply landed at log index "
                       f"{receipt['position']['log_index']}, after the revoke at "
                       f"index {rev['revoked_at_index']}"),
    }


def arm_b_fenced() -> dict:
    """Same interleaving, store-enforced admission seal."""
    s = Store("B-fenced", enforce_fence=True)
    auth = s.grant("agent-x")
    read = s.read_authority(auth)
    rev = s.revoke("agent-x")
    witness = s.apply_effect(auth, "req-1")
    return {
        "arm": "B_fenced_delayed_apply_refused",
        "read_at_t0": read,
        "revocation": rev,
        "witness": witness,
        "effects": s.effects,
        "log": [asdict(e) for e in s.log],
        "conclusion": ("the same read, the same delay, the opposite outcome: the "
                       "difference is not better reading, it is a write the store "
                       "made on the worker's behalf"),
    }


def arm_c_observer() -> dict:
    """C1: absence-based inference is falsified. C2: honest UNKNOWN."""
    s = Store("C-unfenced", enforce_fence=False)
    auth = s.grant("agent-x")
    rev = s.revoke("agent-x")
    obs = ReadOnlyObserver(s, "third-party")

    naive = obs.naive_conclusion("agent-x", "req-1")     # says NOT_APPLIED
    honest = obs.honest_conclusion("agent-x", "req-1")   # says UNKNOWN

    # the in-flight apply the observer could not see now lands
    late = s.apply_effect(auth, "req-1")

    after = obs.honest_conclusion("agent-x", "req-1")
    return {
        "arm": "C_read_only_observer_cannot_seal",
        "revocation": rev,
        "naive_conclusion_before_apply": naive,
        "honest_conclusion_before_apply": honest,
        "late_apply_receipt": late,
        "honest_conclusion_after_apply": after,
        "falsified": naive["decision"] == "NOT_APPLIED" and late["decision"] == "APPLIED",
        "conclusion": ("the reader's NOT_APPLIED was refuted 1 step later by the "
                       "same log it was read from; UNKNOWN with permit_retry=false "
                       "was the only claim that stayed true"),
    }


def arm_d_key_seal() -> dict:
    """
    just-nik's falsifier for CLOSED(K)@F.

    Claim under test: "a NOT_APPLIED_FOR_PRESENTED_EPOCH witness seals the
    request_key K". If that were true, a second apply under the same K could
    never be admitted, whatever epoch it presents.

    Run: refuse K under a stale epoch, then present K again on a FRESH grant
    (current epoch). The store admits it. So the refusal bound the presented
    epoch, not the key. Two receipts, not one renamed receipt.
    """
    s = Store("D-fenced", enforce_fence=True)
    stale_auth = s.grant("agent-x")
    s.revoke("agent-x")                          # stale_auth now behind the epoch
    refusal = s.apply_effect(stale_auth, "req-K")

    fresh_auth = s.grant("agent-x")              # operator re-grants: new epoch
    second = s.apply_effect(fresh_auth, "req-K")  # SAME key K

    sealed = second["decision"] == "NOT_APPLIED"
    return {
        "arm": "D_epoch_refusal_does_not_seal_the_key",
        "refusal_under_stale_epoch": refusal,
        "second_apply_same_key_fresh_epoch": second,
        "effects": s.effects,
        "log": [asdict(e) for e in s.log],
        # The falsifier fires if a refusal ever behaves like a key seal.
        "key_seal_claim_falsified": not sealed,
        "receipts_for_key_K": 2,
        "conclusion": (
            "the REFUSED row and the later APPLIED row are two receipts under one "
            "key; the epoch refusal never reserved K, so CLOSED(K)@F is not a "
            "rename of NOT_APPLIED_FOR_PRESENTED_EPOCH"
        ),
    }


# ------------------------------------------------ positive control + fuzz

def fuzz_fence(trials: int, broken: bool, seed: int = 7) -> dict:
    """
    Randomized thread interleaving. Under a correct fence no APPLIED entry may
    ever follow a REVOKE for the same subject. Under the broken fence the
    harness MUST find a violation, otherwise the harness proves nothing.
    """
    rng = random.Random(seed)
    violations = 0
    applied_after_revoke_examples: list[dict] = []

    for t in range(trials):
        # The broken store gets a widened check-then-act window so its defect is
        # observable within a short run; the correct store has no such window.
        s = Store(f"fuzz-{t}", enforce_fence=True, broken_fence=broken,
                  window_s=0.002 if broken else 0.0)
        auth = s.grant("agent-x")
        barrier = threading.Barrier(2)
        delay = rng.random() / 8000.0

        def worker():
            barrier.wait()
            time.sleep(delay)
            s.apply_effect(auth, "req-1")

        def revoker():
            barrier.wait()
            time.sleep(rng.random() / 8000.0)
            s.revoke("agent-x")

        tw, tr = threading.Thread(target=worker), threading.Thread(target=revoker)
        tw.start(); tr.start(); tw.join(); tr.join()

        rev_idx = next((e.index for e in s.log if e.kind == "REVOKE"), None)
        app_idx = next((e.index for e in s.log if e.kind == "APPLIED"), None)
        if rev_idx is not None and app_idx is not None and app_idx > rev_idx:
            violations += 1
            if len(applied_after_revoke_examples) < 2:
                applied_after_revoke_examples.append(
                    {"trial": t, "revoke_index": rev_idx, "applied_index": app_idx})

    return {
        "mode": "broken_fence_positive_control" if broken else "correct_fence",
        "trials": trials,
        "applied_after_revoke": violations,
        "examples": applied_after_revoke_examples,
        "expectation": (">0 (the control must be able to fail)" if broken
                        else "0 (no admission after revocation)"),
        "pass": (violations > 0) if broken else (violations == 0),
    }


# ------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fuzz", type=int, default=0,
                    help="threaded trials per fence mode (0 = deterministic only)")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    report: dict[str, Any] = {
        "experiment": "authority-fence/1",
        "claim": ("a fresh read cannot establish NOT_APPLIED; only the admitting "
                  "store can, and only within a bounded scope"),
        "arms": [arm_a_unfenced(), arm_b_fenced(), arm_c_observer(),
                 arm_d_key_seal()],
    }

    if args.fuzz:
        report["controls"] = [
            fuzz_fence(args.fuzz, broken=False, seed=args.seed),
            fuzz_fence(args.fuzz, broken=True, seed=args.seed),
        ]

    a, b, c, d = report["arms"]
    report["summary"] = {
        "A_applied_after_revocation": a["apply_receipt"]["decision"] == "APPLIED",
        "B_refused_with_witness": b["witness"]["decision"] == "NOT_APPLIED",
        "B_witness_issued_by": b["witness"]["issued_by"],
        "C_naive_reader_claim_falsified": c["falsified"],
        "C_honest_decision": c["honest_conclusion_before_apply"]["decision"],
        "C_permit_retry": c["honest_conclusion_before_apply"]["permit_retry"],
        "D_key_seal_claim_falsified": d["key_seal_claim_falsified"],
        "D_receipts_for_one_key": d["receipts_for_key_K"],
    }
    ok = (report["summary"]["A_applied_after_revocation"]
          and report["summary"]["B_refused_with_witness"]
          and report["summary"]["C_naive_reader_claim_falsified"]
          and report["summary"]["C_honest_decision"] == "UNKNOWN"
          and report["summary"]["C_permit_retry"] is False
          and report["summary"]["D_key_seal_claim_falsified"]
          and all(ctl["pass"] for ctl in report.get("controls", [])))
    report["all_expectations_met"] = ok

    print(json.dumps(report, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
