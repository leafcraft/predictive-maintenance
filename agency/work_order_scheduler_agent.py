"""work_order_scheduler_agent — fail-closed work-order ACTUATOR.

WHY THIS MODULE EXISTS (YAML-first exception): this agent is the
actuator that creates work orders and dispatches a human technician —
the predictive-maintenance analogue of a release agent. Five pieces are
deterministic policy that must not be hallucinated or left to lenient
yield defaults:

  @pre_compose load_parts — the planner pulls its OWN parts picture: it
     resolves the failure mode to the part SKUs and reads on-hand +
     lead time from the dev store (PROD: ERP/MRP parts master), plus any
     open work order on the asset (for parts_arrived re-entry). Not
     spoon-fed parts state by a conductor.

  Body (the actuator):
  1. RUL → work-type matrix: failure < 24h OR safety-critical →
     emergency + engineer review; < 168h → corrective; else planned.
  2. Parts-availability gate BEFORE scheduling a dispatch (fail-closed):
     a part not on hand, OR a part whose lead time arrives AFTER the
     predicted failure, is NOT available for this work order — no
     dispatch, the parts_available == false wire escalates to the
     reliability engineer.
  3. Traceability (fail-closed): every dispatch carries a
     work_order_id. When the payload can't be recovered the agent
     REFUSES to mint an untraceable work order.

  @chain set_priority — the work-order PRIORITY floor: priority is
     derived deterministically from work_type + safety_critical (P1
     emergency / P2 corrective / P3 planned), and a safety-critical or
     emergency job is ALWAYS at least P1 — the LLM upstream cannot
     down-rank an emergency.

All routing lives in configs/config.yaml `can_call` wires; this module
computes the yields those wires evaluate.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from leafmesh import LeafMeshLogger, pre_compose, chain
from agency._shared import store

logger = LeafMeshLogger(__name__)

SHUTDOWN_WINDOW_HOURS = 24.0
PLANNED_THRESHOLD_HOURS = 168.0


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


def _failure_mode(data: Any) -> str:
    if isinstance(data, dict):
        if data.get("failure_mode"):
            return str(data["failure_mode"])
        uy = data.get("upstream_yields")
        if isinstance(uy, dict):
            if uy.get("failure_mode"):
                return str(uy["failure_mode"])
            for v in uy.values():
                if isinstance(v, dict) and v.get("failure_mode"):
                    return str(v["failure_mode"])
    return ""


def load_parts(data: Any, ctx: Any) -> dict[str, Any]:
    """@pre_compose — the planner pulls its own parts picture from the
    parts master (dev store) + any open work order on the asset."""
    aid = _asset_id(data)
    mode = _failure_mode(data)
    avail = store.parts_availability(mode) if mode else {
        "parts_available": True, "max_lead_time_hours": 0.0,
        "parts_short": [], "parts": [], "resolved": False}
    return {
        "asset_id": aid,
        "failure_mode": mode,
        "parts_picture": avail,
        "open_work_order": store.open_work_order(aid),
    }


def _prepared(ctx: Any) -> dict[str, Any]:
    if isinstance(ctx, dict):
        for k in ("prepared_data", "prepared", "pre_compose"):
            v = ctx.get(k)
            if isinstance(v, dict) and v:
                return v
        if ctx.get("parts_picture") or ctx.get("open_work_order"):
            return ctx
    return {}


async def set_priority(result: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """@chain — the work-order PRIORITY floor (LLM-unwaivable).

    Priority is derived from work_type + the safety flag, NOT from any
    upstream free-text. A safety-critical or emergency job is always at
    least P1; corrective P2; planned P3. A held/untraceable job is P1
    (it needs a human now).
    """
    wt = str(result.get("work_type") or "")
    requires_engineer = bool(result.get("requires_engineer_review"))
    if wt in ("emergency", "unscheduled_review") or requires_engineer:
        priority = "P1"
    elif wt == "corrective":
        priority = "P2"
    else:
        priority = "P3"
    result["priority"] = priority
    logger.info(f"[wo] {result.get('work_order_id') or '?'} priority floor → {priority}")
    return result


@pre_compose(context_processor=load_parts)
@chain(set_priority)
async def work_order_scheduler_agent(llm_response, input_data, context):
    input_data = input_data if isinstance(input_data, dict) else {}
    prepared = _prepared(context)
    upstream = (context.get("upstream") or {}) if isinstance(context, dict) else {}

    raw_hours = input_data.get("predicted_failure_hours")
    safety = bool(input_data.get("safety_critical"))
    open_wo = prepared.get("open_work_order") or {}
    # Identity passthrough — reuse an existing work order if the caller
    # supplied one (parts_arrived re-entry), else the open WO on the asset.
    wo_id = str(input_data.get("work_order_id")
                or upstream.get("work_order_id")
                or open_wo.get("work_order_id") or "")

    # ── Fail-closed traceability gate ────────────────────────────
    if raw_hours in (None, "") and not safety and not wo_id:
        logger.warning("[wo] unrecoverable payload — held for engineer review")
        return {
            "work_order_id": "", "window_start_utc": "",
            "work_type": "unscheduled_review", "requires_engineer_review": True,
            "parts_available": False, "lead_time_hours": 0, "parts_short": [],
            "dispatched_to": "held_untraceable", "dispatched_at_utc": "",
            "eta_minutes": 0,
        }

    # ── Stage 1: schedule window + work type ─────────────────────
    try:
        hours = float(raw_hours) if raw_hours not in (None, "") else PLANNED_THRESHOLD_HOURS
    except (TypeError, ValueError):
        hours = PLANNED_THRESHOLD_HOURS

    if hours < SHUTDOWN_WINDOW_HOURS or safety:
        work_type, requires_engineer, offset = "emergency", True, 0.0
    elif hours < PLANNED_THRESHOLD_HOURS:
        work_type, requires_engineer, offset = "corrective", False, max(1.0, hours / 4.0)
    else:
        work_type, requires_engineer, offset = "planned", False, hours / 2.0

    window = (datetime.now(timezone.utc) + timedelta(hours=offset)).isoformat()
    if not wo_id:
        wo_id = f"wo-{uuid.uuid4().hex[:10]}"
    logger.info(f"[wo] {wo_id} hours_to_failure={hours:.0f} → {work_type} "
                f"engineer={requires_engineer}")

    # ── Stage 2: parts-availability gate BEFORE scheduling a dispatch.
    #    Real parts state comes from the @pre_compose store read; on a
    #    bare direct call with a failure_mode but no injected picture, pull
    #    it from the store so the gate holds regardless. The _test_*
    #    context overrides keep the unit tests hermetic.
    parts = prepared.get("parts_picture") or {}
    if not parts and input_data.get("failure_mode") \
            and "_test_parts_short" not in upstream \
            and "_test_parts_available" not in upstream:
        parts = store.parts_availability(str(input_data["failure_mode"]))
    test_available = upstream.get("_test_parts_available")
    if "_test_lead_time_hours" in upstream:
        lead_time = float(upstream.get("_test_lead_time_hours") or 0.0)
    else:
        lead_time = float(parts.get("max_lead_time_hours") or 0.0)
    if "_test_parts_short" in upstream:
        parts_short = list(upstream.get("_test_parts_short") or [])
    else:
        parts_short = list(parts.get("parts_short") or [])

    if test_available is not None:
        available = bool(test_available)
    else:
        available = bool(parts.get("parts_available", not parts_short))
    # Lead-time vs RUL gate (fail-closed): parts that arrive after the
    # predicted failure are NOT available for this work order.
    if available and lead_time > hours:
        available = False
        logger.warning(f"[parts] {wo_id} lead_time={lead_time:.0f}h > "
                       f"RUL={hours:.0f}h → engineer review")

    base = {
        "work_order_id": wo_id, "window_start_utc": window,
        "work_type": work_type, "requires_engineer_review": requires_engineer,
        "parts_available": available, "lead_time_hours": lead_time,
        "parts_short": parts_short,
    }

    if not available:
        # No dispatch — parts_available == false escalates to the engineer.
        return {**base, "dispatched_to": "held_pending_parts",
                "dispatched_at_utc": "", "eta_minutes": 0}

    # ── Stage 3: dispatch (parts OK) ─────────────────────────────
    dispatched_to = upstream.get("_test_dispatched_to", "technician")
    eta_minutes = int(upstream.get("_test_eta_min") or 60)
    now = datetime.now(timezone.utc).isoformat()
    logger.info(f"[dispatcher] {wo_id} → {dispatched_to} ETA {eta_minutes}min")
    return {**base, "dispatched_to": dispatched_to,
            "dispatched_at_utc": now, "eta_minutes": eta_minutes}
