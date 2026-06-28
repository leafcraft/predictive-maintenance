"""Per-session scratch state for predictive maintenance's bounded loops.

Used for the cycles that MUST terminate (Rule 3 — bound every retry
back-edge) and for cheap idempotency, counted across an asset's event
life:

  * a sensor event's in-flight asset (event idempotency — the same
    vibration alarm re-firing must not mint a second work order)
  * work-order rework rounds (technician escalate ⇄ scheduler) →
    escalate to the reliability engineer at the cap

In-process dict keyed by session_id. Single-replica dev default. For a
multi-replica deployment, swap the primitives below for Redis hashes
(the SDK's Redis client is available on the mesh instance).
"""
from __future__ import annotations

import threading
from typing import Any

_LOCK = threading.Lock()
_STASH: dict[str, dict[str, Any]] = {}


def stash_get(session_id: str) -> dict[str, Any]:
    with _LOCK:
        return dict(_STASH.get(session_id or "_nosession", {}))


def stash_put(session_id: str, data: dict[str, Any]) -> None:
    with _LOCK:
        _STASH[session_id or "_nosession"] = dict(data)


def bump(session_id: str, key: str, by: int = 1) -> int:
    """Increment a counter and return the NEW value."""
    sid = session_id or "_nosession"
    with _LOCK:
        bucket = _STASH.setdefault(sid, {})
        bucket[key] = int(bucket.get(key, 0)) + by
        return bucket[key]


def peek(session_id: str, key: str, default: int = 0) -> int:
    sid = session_id or "_nosession"
    with _LOCK:
        return int(_STASH.get(sid, {}).get(key, default))
