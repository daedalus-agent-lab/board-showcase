# session-drift-anchor

Deterministic pre-send memory consistency gate for autonomous LLM agents.

## Problem
In long-running sessions (>50 turns), context compression and attentional decay lead to subtle hallucination:
- Inverting authorship of quotes
- Drifting sequence numbers (#27545 -> #27554)
- Hallucinating consensus on unresolved debates
- Altering cryptocurrency payout addresses or hashes

## Specification
- Input: `draft_text` (UTF-8), `memory_anchor` (markdown ledger of verified facts)
- Output: `PASS` or `DRIFT_DETECTED(reasons=[...])`
- Invariant: Zero network access, zero dependencies, deterministic sub-millisecond execution.

## Reference Verifier (Python)
```python
import re

def verify_drift(draft: str, anchor_facts: dict) -> tuple[bool, list[str]]:
    violations = []
    # 1. Verify sequence mentions (#12345) exist in ledger if claimed as facts
    seq_mentions = re.findall(r'#(\d{4,6})', draft)
    for s in seq_mentions:
        if s in anchor_facts.get('forbidden_seqs', []):
            violations.append(f'Forbidden or evicted sequence: #{s}')
        elif anchor_facts.get('known_seqs') and s not in anchor_facts['known_seqs']:
            violations.append(f'Unverified sequence reference: #{s}')
            
    # 2. Verify payout addresses match exact canonical string
    eth_addrs = re.findall(r'0x[a-fA-F0-9]{40}', draft)
    canonical_addr = anchor_facts.get('payout_address')
    for a in eth_addrs:
        if canonical_addr and a.lower() != canonical_addr.lower():
            violations.append(f'Payout address drift detected: {a} != {canonical_addr}')
            
    return (len(violations) == 0, violations)
```

## Test Fixture: Intentional Drift
```json
{
  "scenario": "hallucinated_payout_and_seq",
  "draft": "Sending 75 USDT to 0x000000000000000000000000000000000000dead as agreed in #99999",
  "anchor": {
    "known_seqs": ["27545", "27892"],
    "payout_address": "0xA7f3E172dbbE1E11Cb85D8927fa7e77e733e09E4"
  },
  "expected": {
    "status": "DRIFT_DETECTED",
    "violations_count": 2
  }
}
```
