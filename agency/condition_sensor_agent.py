"""condition_sensor_agent — Cass, the condition-monitoring reader.

The PROACTIVE reader. Two live trigger surfaces fan it across the fleet:
  * `wake_up` — a scheduled PM sweep, and
  * `listen_events` — a live IIoT condition stream,
each multiplied by `instances: 3` into parallel copies. Every copy ATOMICALLY
CLAIMS the next asset from the inspection backlog (store.claim_next_asset) so the
copies split the fleet instead of re-reading the same asset — the merge would
otherwise honestly show duplicated work. A copy that claims a specific asset
(a reactive reading naming one) reads that; a copy with nothing left to claim
announces `available:false` and hands off nothing.

Day-0 it reads the seed store; it announces `available:false` (never a synthetic
reading) when there is no historian and no seed. On an anomaly it hands off to
Rex (the reliability analyst). Programmatic: code reads and flags; it does not
decide the RUL — that is Rex's job.
"""
from __future__ import annotations

from typing import Any

from leafmesh import LeafMeshLogger

try:
    from ._shared import store
except ImportError:  # pragma: no cover
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _shared import store  # type: ignore

logger = LeafMeshLogger(__name__)


def _named_asset(data: Any) -> str:
    """An explicitly-named asset in the trigger payload (a reactive reading),
    or "" when this is a backlog sweep with no named asset."""
    if isinstance(data, dict):
        if data.get("asset_id"):
            return str(data["asset_id"])
        for blob in (data.get("payload"), data.get("upstream_yields")):
            if isinstance(blob, dict) and blob.get("asset_id"):
                return str(blob["asset_id"])
    return ""


def _instance_id(data: Any) -> str:
    inst = data.get("_instance") if isinstance(data, dict) else None
    if isinstance(inst, dict) and inst.get("id") is not None:
        return str(inst["id"])
    return "1"


def _empty(asset_id: str, reason: str) -> dict[str, Any]:
    return {"asset_id": asset_id, "sensor_summary": {},
            "anomaly_detected": False, "available": False, "reason": reason}


async def condition_sensor_agent(llm_response: Any, input_data: Any,
                                 context: Any) -> dict[str, Any]:
    input_data = input_data if isinstance(input_data, dict) else {}
    named = _named_asset(input_data)
    inst_id = _instance_id(input_data)

    # The primary copy takes a named asset (reactive reading); every other copy,
    # and any copy on a backlog sweep, atomically claims the next asset.
    if named and inst_id in ("1", "0"):
        asset_id = named
    else:
        claimed = store.claim_next_asset()
        if not claimed.get("available"):
            logger.info(f"[condition] instance {inst_id}: backlog empty — nothing to read")
            return _empty("", "inspection backlog empty for this copy")
        asset_id = claimed["asset_id"]

    signals = store.read_condition_signals(asset_id)
    if not signals.get("available"):
        logger.info(f"[condition] {asset_id}: {signals.get('reason')}")
        return _empty(asset_id, str(signals.get("reason") or "no source"))

    anomaly = bool(signals.get("anomaly_detected"))
    logger.info(f"[condition] {asset_id} read (instance {inst_id}) "
                f"anomaly={anomaly}")
    return {
        "asset_id": asset_id,
        "sensor_summary": signals.get("sensor_summary") or {},
        "anomaly_detected": anomaly,
        "available": True,
    }
