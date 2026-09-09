"""Optional independent verifier hook (ACCEPT.md / v2bot shadow-pilot).

Non-fatal: never raises into accept path. Call after ACCEPTED receipt is durable.
Endpoint contract (shadow-pilot.v2.site):
  POST /v1/assignments {submitter, scalar, pinned_ref, bounds?, confidence?, commitment_note?}
  → {assignment_id, seq, evidence_digest, ...}
  Then poll GET /v1/events?after={seq-1} for attestation_signed matching task/scalar.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any


DEFAULT_TIMEOUT_S = 3.0
DEFAULT_POLL_S = 2.0


def _http_json(url: str, *, data: dict | None = None, timeout: float) -> tuple[int, Any]:
    body = None if data is None else json.dumps(data, ensure_ascii=False).encode("utf-8")
    headers = {"Accept": "application/json", "User-Agent": "daedalus-shelf-verifier/0.4.2"}
    method = "GET"
    if body is not None:
        headers["Content-Type"] = "application/json"
        method = "POST"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            try:
                return int(r.status), json.loads(raw.decode("utf-8"))
            except Exception:
                return int(r.status), {"raw": raw[:200].decode("utf-8", "replace")}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return int(e.code), json.loads(raw)
        except Exception:
            return int(e.code), {"error": raw[:300]}
    except Exception as e:
        return 0, {"error": f"{type(e).__name__}: {e}"}


def request_attestation(
    *,
    base_url: str,
    submitter: str,
    sha256: str,
    pinned_ref: str,
    bounds: str = "",
    confidence: str = "shelf-accept",
    commitment_note: str = "optional shelf verifier hook after ACCEPTED",
    timeout_s: float = DEFAULT_TIMEOUT_S,
    poll_s: float = DEFAULT_POLL_S,
) -> dict[str, Any]:
    """Return verifier.attestation dict or {status: unavailable, reason}."""
    base = base_url.rstrip("/")
    payload = {
        "submitter": submitter,
        "scalar": sha256,
        "pinned_ref": pinned_ref,
        "bounds": bounds or f"sha256={sha256}",
        "confidence": confidence,
        "commitment_note": commitment_note,
    }
    code, resp = _http_json(f"{base}/v1/assignments", data=payload, timeout=timeout_s)
    if code != 200 or not isinstance(resp, dict) or not resp.get("ok", True):
        return {
            "status": "unavailable",
            "reason": f"assignments HTTP {code}",
            "detail": resp,
        }
    assignment_id = resp.get("assignment_id") or (resp.get("receipt") or {}).get("id")
    assign_seq = resp.get("seq") or (resp.get("receipt") or {}).get("seq")
    evidence = resp.get("evidence_digest")
    # Poll briefly for attestation_signed (controller may be async).
    deadline = time.time() + max(0.0, poll_s)
    after = max(0, int(assign_seq or 1) - 1)
    while time.time() <= deadline:
        c2, ev = _http_json(f"{base}/v1/events?after={after}", timeout=timeout_s)
        if c2 == 200 and isinstance(ev, dict):
            for e in ev.get("events") or []:
                if e.get("event_type") != "attestation_signed":
                    continue
                task = str(e.get("task_id") or "")
                if sha256[:16] in task or sha256 in task:
                    return {
                        "status": "attested",
                        "attestation_id": e.get("attestation_id"),
                        "seq": e.get("seq"),
                        "evidence_digest": e.get("evidence_digest") or evidence,
                        "assignment_id": assignment_id,
                        "verdict": e.get("verdict"),
                        "endpoint": base,
                    }
        time.sleep(0.4)
    # Assignment issued but attestation not yet visible — still useful receipt.
    if assignment_id:
        return {
            "status": "assigned",
            "assignment_id": assignment_id,
            "seq": assign_seq,
            "evidence_digest": evidence,
            "endpoint": base,
            "note": "assignment_issued; attestation_signed not observed within poll window",
        }
    return {"status": "unavailable", "reason": "no assignment_id", "detail": resp}


def maybe_attest_from_env(
    *,
    submitter: str,
    sha256: str,
    pinned_ref: str,
    bounds: str = "",
) -> dict[str, Any] | None:
    """If SHELF_VERIFIER_URL is set, attempt attestation; else return None (hook off)."""
    url = (os.environ.get("SHELF_VERIFIER_URL") or "").strip()
    if not url:
        return None
    try:
        timeout_s = float(os.environ.get("SHELF_VERIFIER_TIMEOUT_S") or DEFAULT_TIMEOUT_S)
    except ValueError:
        timeout_s = DEFAULT_TIMEOUT_S
    try:
        poll_s = float(os.environ.get("SHELF_VERIFIER_POLL_S") or DEFAULT_POLL_S)
    except ValueError:
        poll_s = DEFAULT_POLL_S
    try:
        return request_attestation(
            base_url=url,
            submitter=submitter,
            sha256=sha256,
            pinned_ref=pinned_ref,
            bounds=bounds,
            timeout_s=timeout_s,
            poll_s=poll_s,
        )
    except Exception as e:
        return {"status": "unavailable", "reason": f"{type(e).__name__}: {e}"}
