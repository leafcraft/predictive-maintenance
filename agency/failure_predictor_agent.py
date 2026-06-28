"""failure_predictor_agent — self-reliant predictive reliability analyst.

@pre_compose  load_condition — the analyst pulls its OWN context before
              reasoning: the short telemetry series + maintenance history
              for the asset from the dev store (PROD: HISTORIAN_MCP_URL /
              CMMS_MCP_URL), plus the deterministic failure-probability
              score the floor below reads. Not spoon-fed by a conductor.
@chain        enforce_floors — two deterministic floors the LLM CANNOT
              waive:
                (1) failure-probability severity floor — when the
                    model-agnostic risk score says failure within 24h is
                    likely (>= 0.7), the RUL is clamped into the shutdown
                    window and safety_critical is forced true, regardless
                    of an optimistic LLM verdict;
                (2) safety-critical hard-stop — a safety-critical asset
                    (asset master) OR a low-confidence prediction inside
                    the 24h window forces safety_critical true and routes
                    the reliability engineer. The wires fire on the
                    post-floor values, so a lenient LLM can't drop the gate.

Was pure-YAML; promoted to a module because "self-reliant + deterministic
floor" is exactly the depth bar — the prompt alone could be talked out of
the safety call.
"""
from __future__ import annotations

import json
import re
from typing import Any

from leafmesh import LeafMeshLogger, pre_compose, chain
from agency._shared import store

logger = LeafMeshLogger(__name__)

# Failure-probability severity floor: at/above this, failure within 24h
# is treated as likely → RUL clamped into the window, safety forced.
FAILURE_PROB_FLOOR = 0.7
SHUTDOWN_WINDOW_HOURS = 24.0
# A clamped RUL the floor assigns when it overrides an optimistic LLM.
FLOOR_CLAMPED_HOURS = 12.0
LOW_CONF = 0.7

_DEFAULTS: dict[str, Any] = {
    "predicted_failure_hours": 168.0, "failure_mode": "unknown",
    "confidence": 0.0, "safety_critical": False, "engineer_briefing": "",
}


def _asset_id(data: Any) -> str:
    if isinstance(data, dict):
        for k in ("asset_id",):
            if data.get(k):
                return str(data[k])
        for blob in (data.get("upstream_yields"), data.get("payload")):
            if isinstance(blob, dict):
                if blob.get("asset_id"):
                    return str(blob["asset_id"])
                for v in blob.values():
                    if isinstance(v, dict) and v.get("asset_id"):
                        return str(v["asset_id"])
    return store.DEMO_ASSET


def load_condition(data: Any, ctx: Any) -> dict[str, Any]:
    """@pre_compose — the analyst pulls its own context: telemetry series,
    asset master, recent history, and the deterministic risk score."""
    aid = _asset_id(data)
    return {
        "asset_id": aid,
        "asset_record": store.asset(aid),
        "signal_series": store.signal_series(aid),
        "recent_history": store.asset_history(aid)[-3:],
        "failure_score": store.failure_probability(aid),
    }


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


def _prepared(ctx: Any) -> dict[str, Any]:
    """Recover what load_condition stashed (SDK puts pre_compose output on
    the context under prepared_data; tolerate a couple of shapes)."""
    if isinstance(ctx, dict):
        for k in ("prepared_data", "prepared", "pre_compose"):
            v = ctx.get(k)
            if isinstance(v, dict) and v:
                return v
        if ctx.get("failure_score") or ctx.get("asset_record"):
            return ctx
    return {}


async def enforce_floors(result: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """@chain — deterministic floors the LLM can't waive."""
    aid = str(result.get("asset_id") or "") or store.DEMO_ASSET
    prepared = _prepared(ctx)
    score = prepared.get("failure_score") or store.failure_probability(aid)
    asset_rec = prepared.get("asset_record") or store.asset(aid)

    prob = float(score.get("failure_prob_24h") or 0.0)
    try:
        hours = float(result.get("predicted_failure_hours"))
    except (TypeError, ValueError):
        hours = _DEFAULTS["predicted_failure_hours"]
    conf = float(result.get("confidence") or 0.0)

    floors: list[str] = []

    # (1) Failure-probability severity floor — the model-agnostic score
    #     says failure within 24h is likely; clamp the RUL into the window
    #     and force the safety flag, regardless of an optimistic LLM.
    if prob >= FAILURE_PROB_FLOOR and hours >= SHUTDOWN_WINDOW_HOURS:
        hours = FLOOR_CLAMPED_HOURS
        result["safety_critical"] = True
        floors.append(f"failure_prob={prob:.2f}>={FAILURE_PROB_FLOOR}→RUL clamped {hours:.0f}h")

    # (2) Safety-critical hard-stop — a safety-critical asset always trips
    #     the engineer; a low-confidence prediction inside the shutdown
    #     window is treated defensively (never optimistically).
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


@pre_compose(context_processor=load_condition)
@chain(enforce_floors)
async def failure_predictor_agent(llm_response: Any, input_data: Any,
                                  context: Any) -> dict[str, Any]:
    llm = _parse(llm_response)
    out = {**_DEFAULTS}
    aid = _asset_id(input_data)
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
