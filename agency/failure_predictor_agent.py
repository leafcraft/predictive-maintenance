"""failure_predictor_agent — Rex, the predictive reliability analyst.

A Super-Agent (plan → execute → verify) that PULLS its own data through tools
(read_condition_signals, read_asset_history, failure_probability_score — the
day-0 dev surface in agency/tools.py, the historian mcp block in prod) and
scores remaining useful life, failure mode and confidence. The MODEL decides and
pulls; this CODE fails closed.

@chain enforce_floors — two deterministic safety floors the LLM CANNOT waive,
re-derived in code from the store (not trusted from the model):
  (1) failure-probability severity floor — when the model-agnostic risk score
      says failure within 24h is likely (>= 0.7), the RUL is clamped into the
      shutdown window and safety_critical is forced, whatever the LLM estimated;
  (2) safety-critical hard-stop — a safety-critical asset (asset master) OR a
      low-confidence prediction inside the 24h window forces safety_critical and
      routes the reliability engineer.
The mesh wires fire on the POST-floor values, so a lenient LLM can't drop the gate.
"""
from __future__ import annotations

import json
import re
from typing import Any

from leafmesh import LeafMeshLogger, chain

try:
    from ._shared import store
except ImportError:  # pragma: no cover
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _shared import store  # type: ignore

logger = LeafMeshLogger(__name__)

# Failure-probability severity floor: at/above this, failure within 24h is
# treated as likely → RUL clamped into the window, safety forced.
FAILURE_PROB_FLOOR = 0.7
SHUTDOWN_WINDOW_HOURS = 24.0
FLOOR_CLAMPED_HOURS = 12.0        # the clamped RUL when the floor overrides
LOW_CONF = 0.7

_DEFAULTS: dict[str, Any] = {
    "asset_id": "", "predicted_failure_hours": 168.0, "failure_mode": "unknown",
    "confidence": 0.0, "safety_critical": False, "failure_prob_24h": 0.0,
    "engineer_briefing": "",
}


def _asset_id(data: Any) -> str:
    if isinstance(data, dict):
        if data.get("asset_id"):
            return str(data["asset_id"])
        for blob in (data.get("upstream_yields"), data.get("payload")):
            if isinstance(blob, dict):
                if blob.get("asset_id"):
                    return str(blob["asset_id"])
                for v in blob.values():
                    if isinstance(v, dict) and v.get("asset_id"):
                        return str(v["asset_id"])
    return store.DEMO_ASSET


def _parse(resp: Any) -> dict[str, Any]:
    if isinstance(resp, dict):
        return resp
    if isinstance(resp, str):
        try:
            return json.loads(resp)
        except ValueError:
            m = re.search(r"\{.*\}", resp, re.S)
            if m:
                try:
                    return json.loads(m.group(0))
                except ValueError:
                    pass
    return {}


async def enforce_floors(result: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """@chain — deterministic safety floors the LLM can't waive. Re-derives the
    risk score + asset criticality from the store (never trusts the model)."""
    result = result if isinstance(result, dict) else {}
    aid = str(result.get("asset_id") or "") or store.DEMO_ASSET
    score = store.failure_probability(aid)
    asset_rec = store.asset(aid)

    prob = float(score.get("failure_prob_24h") or 0.0)
    try:
        hours = float(result.get("predicted_failure_hours"))
    except (TypeError, ValueError):
        hours = _DEFAULTS["predicted_failure_hours"]
    conf = float(result.get("confidence") or 0.0)

    floors: list[str] = []

    # (1) Failure-probability severity floor.
    if prob >= FAILURE_PROB_FLOOR and hours >= SHUTDOWN_WINDOW_HOURS:
        hours = FLOOR_CLAMPED_HOURS
        result["safety_critical"] = True
        floors.append(f"failure_prob={prob:.2f}>={FAILURE_PROB_FLOOR}→RUL clamped {hours:.0f}h")

    # (2) Safety-critical hard-stop.
    if str(asset_rec.get("criticality") or "") == "safety_critical":
        result["safety_critical"] = True
        floors.append("asset criticality=safety_critical")
    if hours < SHUTDOWN_WINDOW_HOURS and conf < LOW_CONF:
        result["safety_critical"] = True
        floors.append(f"low-conf({conf:.2f}) inside {SHUTDOWN_WINDOW_HOURS:.0f}h window")

    result["predicted_failure_hours"] = max(0.0, hours)
    result["failure_prob_24h"] = prob
    if floors:
        logger.warning(f"[predictor] {aid} FLOORS: {'; '.join(floors)} "
                       f"→ safety_critical={result['safety_critical']}")
    else:
        logger.info(f"[predictor] {aid} RUL={hours:.0f}h conf={conf:.2f} "
                    f"prob={prob:.2f} (no floor)")
    return result


@chain(enforce_floors)
async def failure_predictor_agent(llm_response: Any, input_data: Any,
                                  context: Any) -> dict[str, Any]:
    """Normalise the model's RUL estimate into the yields contract; the safety
    floors (@chain) run next and are authoritative for safety_critical."""
    llm = _parse(llm_response)
    out = {**_DEFAULTS}
    aid = str(llm.get("asset_id") or "") or _asset_id(input_data)
    sig = store.signal_series(aid)

    try:
        hours = float(llm.get("predicted_failure_hours"))
    except (TypeError, ValueError):
        hours = _DEFAULTS["predicted_failure_hours"]
    try:
        conf = float(llm.get("confidence"))
    except (TypeError, ValueError):
        conf = 0.0

    out.update({
        "asset_id": aid,
        "predicted_failure_hours": max(0.0, hours),
        "failure_mode": str(llm.get("failure_mode")
                            or sig.get("dominant_signature") or "unknown"),
        "confidence": min(max(conf, 0.0), 1.0),
        "safety_critical": bool(llm.get("safety_critical")),
        "engineer_briefing": str(llm.get("engineer_briefing") or ""),
    })
    logger.info(f"[predictor] {aid} LLM RUL={out['predicted_failure_hours']:.0f}h "
                f"mode={out['failure_mode']} (enforce_floors runs next)")
    return out
