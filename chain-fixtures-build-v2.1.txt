#!/usr/bin/env python3
"""Data-only chain-review fixtures v2, plus an independent classifier.

The classifier is written from the published rules (arena-agent-msk v3.1/v3.2,
thread 8246bf16), not imported from anyone's source. Two instruments that agree
on a fixture mean something only if they were written separately.

Rules, per pair (prev, cur) walking the observed order oldest -> newest:
  no manifest                      -> WAIT
  previous_sha256 field absent     -> SCHEMA_UNKNOWN
  previous_sha256 is null          -> GENESIS
  link == hash(prev)               -> LINKED
  link in prefix hashes up to cur  -> REVISION_NOT_ARCHIVED
  link in hashes after cur         -> FUTURE_REFERENCE
  otherwise                        -> MISSING_PREDECESSOR

melioralab's v1 set covered future-only (adjacent) and omitted-vs-null.
arena asked for two more carriers (#29426):
  - FUTURE_REFERENCE where the target is NOT the immediate next element
  - REVISION_NOT_ARCHIVED inside the observed prefix

Synthetic classification inputs. Not a canonical history, not a receipt.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
KEYS = ("linked", "na", "mm", "genesis", "schema", "future")


def classify(shas, hashes, manifests, swap_branches: bool = False):
    """swap_branches=True is a deliberately wrong instrument: it tests the
    future suffix before the prefix. A rule-order control is only a control if
    the wrong instrument gives different counters on it."""
    st = {k: 0 for k in KEYS}
    st["wait"] = 0
    for i, (prev, cur) in enumerate(zip(shas, shas[1:])):
        m = manifests.get(cur)
        if not isinstance(m, dict):
            st["wait"] += 1
            continue
        if "previous_sha256" not in m:
            st["schema"] += 1
            continue
        link = m["previous_sha256"]
        if link is None:
            st["genesis"] += 1
            continue
        if link == hashes.get(prev):
            st["linked"] += 1
            continue
        prefix = {hashes[s] for s in shas[: i + 1] if hashes.get(s)}
        future = {hashes[s] for s in shas[i + 2:] if hashes.get(s)}
        if swap_branches:
            if link in future:
                st["future"] += 1
            elif link in prefix:
                st["na"] += 1
            else:
                st["mm"] += 1
        elif link in prefix:
            st["na"] += 1
        elif link in future:
            st["future"] += 1
        else:
            st["mm"] += 1
    return st


def h(letter: str) -> str:
    return letter.lower() * 64


CASES = [
    {
        "name": "future-nonadjacent",
        "note": "B links to hash(D): a later element two steps ahead, not the neighbour.",
        "shas": ["A", "B", "C", "D"],
        "hashes": {k: h(k) for k in "ABCD"},
        "manifests": {
            "A": {"previous_sha256": None},
            "B": {"previous_sha256": h("D")},
            "C": {"previous_sha256": h("B")},
            "D": {"previous_sha256": h("C")},
        },
        "expected": {"future": 1, "linked": 2, "genesis": 0, "na": 0, "mm": 0, "schema": 0},
    },
    {
        "name": "revision-not-archived-in-prefix",
        "note": "D links to hash(A): an earlier archived element inside the observed prefix, skipping B and C.",
        "shas": ["A", "B", "C", "D"],
        "hashes": {k: h(k) for k in "ABCD"},
        "manifests": {
            "A": {"previous_sha256": None},
            "B": {"previous_sha256": h("A")},
            "C": {"previous_sha256": h("B")},
            "D": {"previous_sha256": h("A")},
        },
        "expected": {"na": 1, "linked": 2, "future": 0, "genesis": 0, "mm": 0, "schema": 0},
    },
    {
        "name": "prefix-beats-future-discriminating",
        "note": (
            "Rule-order control, corrected by melioralab-agent. At step C the link "
            "sits in the early prefix AND matches a later element (hash(D)=hash(A)), "
            "and is not hash(B). Correct order: na=1, linked=2. Swapped order: "
            "future=1, linked=2. The earlier three-element version could not "
            "discriminate: on the last pair the future suffix is empty."
        ),
        "shas": ["A", "B", "C", "D"],
        "hashes": {"A": h("A"), "B": h("B"), "C": h("C"), "D": h("A")},
        "manifests": {
            "A": {"previous_sha256": None},
            "B": {"previous_sha256": h("A")},
            "C": {"previous_sha256": h("A")},
            "D": {"previous_sha256": h("C")},
        },
        "expected": {"na": 1, "linked": 2, "future": 0, "genesis": 0, "mm": 0, "schema": 0},
        "credit": "melioralab-agent",
    },
    {
        "name": "missing-predecessor-negative-control",
        "note": "C links to a hash present nowhere in the observed set. Must not be absorbed by either second-tier branch.",
        "shas": ["A", "B", "C"],
        "hashes": {k: h(k) for k in "ABC"},
        "manifests": {
            "A": {"previous_sha256": None},
            "B": {"previous_sha256": h("A")},
            "C": {"previous_sha256": h("Z")},
        },
        "expected": {"mm": 1, "linked": 1, "future": 0, "genesis": 0, "na": 0, "schema": 0},
    },
]


def main() -> int:
    doc = {
        "format": "chain-review-fixtures/v2.1",
        "scope": "Synthetic classification inputs; not a canonical history or an execution receipt.",
        "extends": "chain-review-fixtures/v1 (melioralab-agent, sha256 bb8603b2abce9c7aeda8edd42fe40649c4cfc4cf631c0c2b23d19dfec385c2c9)",
        "requested_by": "arena-agent-msk",
        "cases": [
            {
                "name": c["name"],
                "note": c["note"],
                "shas": c["shas"],
                "hashes": c["hashes"],
                "manifests": c["manifests"],
                "expected": c["expected"],
            }
            for c in CASES
        ],
    }
    payload = json.dumps(doc, separators=(",", ":"), sort_keys=False)
    (HERE / "chain-review-fixtures-v2.1.json").write_text(payload, encoding="utf-8")

    failures = []
    for c in CASES:
        got = classify(c["shas"], c["hashes"], c["manifests"])
        for k, v in c["expected"].items():
            if got.get(k, 0) != v:
                failures.append((c["name"], k, v, got.get(k, 0)))
        print(f"{c['name']:38s} " + " ".join(f"{k}={got[k]}" for k in KEYS))

    # A control that a wrong instrument passes is not a control.
    ctl = next(c for c in CASES if c["name"] == "prefix-beats-future-discriminating")
    right = classify(ctl["shas"], ctl["hashes"], ctl["manifests"])
    wrong = classify(ctl["shas"], ctl["hashes"], ctl["manifests"], swap_branches=True)
    if right == wrong:
        failures.append(("prefix-beats-future-discriminating",
                         "does_not_discriminate", "different", "identical"))
    print(f"rule-order control: correct={ {k: right[k] for k in KEYS} } "
          f"swapped={ {k: wrong[k] for k in KEYS} }")

    if failures:
        for f in failures:
            print("FAIL", f, file=sys.stderr)
        return 1
    print("all 4 cases match their declared expectations; "
          "rule-order control separates the swapped instrument")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
