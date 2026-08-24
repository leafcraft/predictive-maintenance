"""work_order_scheduler_agent — Wanda, the maintenance planner.

Plans a work order; it deliberately does NOT dispatch — the planner is not the
issuer (a second pass, behind the human gate, actuates). Four pieces are
deterministic policy that must not be hallucinated:

  @pre_compose load_parts — the planner pulls its OWN parts picture (on-hand +
     lead time, resolved from the failure mode) + any open work order on the
     asset, from the parts master (dev store; PROD: ERP/MRP). Not spoon-fed.

  Body (the plan):
  1. RUL → work-type matrix: failure < 24h OR safety-critical → emergency +
     engineer review; < 168h → corrective; else planned.
  2. Parts-availability gate (fail-closed lead-time-vs-RUL): a part not on hand,
     or whose lead time arrives after the predicted failure, is NOT available →
     the parts_available == false wire escalates to the engineer.
  3. Traceability (fail-closed): every plan carries a work_order_id; an
     unrecoverable payload REFUSES to mint an untraceable work order.
  4. Bounded re-work back-edge: a technician's parts_issue re-enters here; the
     rounds are COUNTED per work order and, past the bound, rework_exhausted is
     tripped so the loop terminates at the engineer instead of ping-ponging.
     The planned work order is stashed so the issuer can recover it across the
     human gate.

  @chain set_priority — the work-order PRIORITY floor: P1 emergency / P2
     corrective / P3 planned; a safety/emergency job is always at least P1.

All routing lives in configs/config.yaml `can_call`; this module computes the
yields those wires evaluate.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from leafmesh import LeafMeshLogger, pre_compose, chain

try:
    from ._shared import store
    from ._shared import session_stash
except ImportError:  # pragma: no cover
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _shared import store  # type: ignore
    from _shared import session_stash  # type: ignore

logger = LeafMeshLogger(__name__)

SHUTDOWN_WINDOW_HOURS = 24.0
PLANNED_THRESHOLD_HOURS = 168.0
MAX_REWORK_ROUNDS = 3            # bound the technician parts_issue back-edge


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
    """@pre_compose — pull the parts picture from the parts master (dev store)
    + any open work order on the asset."""
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
    """@chain — the work-order PRIORITY floor (LLM-unwaivable). Derived from
    work_type + the safety flag; an emergency/held job is always at least P1."""
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
    sid = session_stash.resolve_session_id(input_data, context)

    raw_hours = input_data.get("predicted_failure_hours")
    safety = bool(input_data.get("safety_critical"))
    open_wo = prepared.get("open_work_order") or {}
    # Identity passthrough — reuse a supplied work order (parts_arrived /
    # parts_issue re-entry), else the open WO on the asset.
    supplied_wo = str(input_data.get("work_order_id")
                      or upstream.get("work_order_id") or "")
    wo_id = supplied_wo or str(open_wo.get("work_order_id") or "")
    failure_mode = _failure_mode(input_data) or str(prepared.get("failure_mode") or "")

    # ── Bounded re-work back-edge ────────────────────────────────
    # A re-entry supplies an existing work order id; count the rounds and
    # terminate at the engineer once the bound is crossed.
    rework_rounds = 0
    rework_exhausted = False
    if supplied_wo:
        rework_rounds = session_stash.bump(sid, f"rework:{supplied_wo}")
        if rework_rounds >= MAX_REWORK_ROUNDS:
            rework_exhausted = True
            logger.warning(f"[wo] {supplied_wo} re-work bound reached "
                           f"({rework_rounds} rounds) → engineer (terminal)")

    # ── Fail-closed traceability gate ────────────────────────────
    if raw_hours in (None, "") and not safety and not wo_id:
        logger.warning("[wo] unrecoverable payload — held for engineer review")
        return {
            "work_order_id": "", "window_start_utc": "",
            "work_type": "unscheduled_review", "requires_engineer_review": True,
            "parts_available": False, "lead_time_hours": 0, "parts_short": [],
            "failure_mode": failure_mode, "rework_rounds": rework_rounds,
            "rework_exhausted": rework_exhausted,
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

    if rework_exhausted:
        requires_engineer = True    # exhausted loop terminates at the engineer

    window = (datetime.now(timezone.utc) + timedelta(hours=offset)).isoformat()
    if not wo_id:
        wo_id = f"wo-{uuid.uuid4().hex[:10]}"
    logger.info(f"[wo] {wo_id} hours_to_failure={hours:.0f} → {work_type} "
                f"engineer={requires_engineer}")

    # ── Stage 2: parts-availability gate (fail-closed) ───────────
    parts = prepared.get("parts_picture") or {}
    if not parts and failure_mode \
            and "_test_parts_short" not in upstream \
            and "_test_parts_available" not in upstream:
        parts = store.parts_availability(failure_mode)
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
    # Lead-time vs RUL gate: parts arriving after the predicted failure are NOT
    # available for this work order.
    if available and lead_time > hours:
        available = False
        logger.warning(f"[parts] {wo_id} lead_time={lead_time:.0f}h > "
                       f"RUL={hours:.0f}h → engineer review")

    plan = {
        "work_order_id": wo_id, "window_start_utc": window,
        "work_type": work_type, "requires_engineer_review": requires_engineer,
        "parts_available": available, "lead_time_hours": lead_time,
        "parts_short": parts_short, "failure_mode": failure_mode,
        "rework_rounds": rework_rounds, "rework_exhausted": rework_exhausted,
    }

    # Stash the plan so the issuer can recover it across the human gate.
    store.put_pending_wo(wo_id, plan)
    return plan
