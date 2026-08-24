"""work_order_issuer_agent — Ivan, the work-order issuer (the FINISHER / actuator).

auto_discover matches `work_order_issuer_agent`. Ivan sits BEHIND the reliability
engineer's approval gate. It never authorises a shutdown or issues on its own
belief — it re-checks that a human actually authorised the job, renders the
approved work-order pack, dispatches the technician, and records an append-only
ledger entry. It is the concrete "@chain fails closed":

  a) authorisation RE-CHECK — reaching this agent does not mean authorised. An
     allow-token allowlist + a block-token denylist; an UNKNOWN verdict on a job
     that NEEDS authorisation (emergency / safety) fails closed. A routine job
     (parts on hand, no engineer review) is auto-authorised by the plan.
  b) deterministic invariants a human can't waive: a traceable work order, parts
     actually on hand (never issue into a parts gap), a real work order to issue.
  c) render the real pack to ./out; on a tripped floor, DELETE the partial and
     record `status: blocked` with reasons — never a half-issued work order.
  d) dev-store recovery — after the HITL gate the upstream payload is dropped, so
     Ivan recovers the plan from the session stash / dev store, and writes a
     VERIFIABLE dev-store record (outbox + ledger) — never a fake dispatch ack.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from leafmesh import LeafMeshLogger, chain

try:
    from ._shared import store
    from ._shared.session_stash import resolve_session_id, stash_get, stash_put
except ImportError:  # pragma: no cover
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _shared import store  # type: ignore
    from _shared.session_stash import resolve_session_id, stash_get, stash_put  # type: ignore

logger = LeafMeshLogger(__name__)

_OUT_DIR = Path("./out")
_ALLOW_TOKENS = frozenset({"approved", "approve", "authorize", "authorized",
                           "proceed", "dispatch", "sign", "signed", "yes", "ok"})
_BLOCK_TOKENS = frozenset({"rejected", "reject", "no", "deny", "denied",
                           "held", "held_for_review", "hold", "halt",
                           "halt_for_review"})


def _signal(input_data: dict) -> str:
    """The human verdict, from wherever the SDK delivered it (shape-tolerant)."""
    sources = [input_data]
    up = input_data.get("upstream_yields")
    if isinstance(up, dict):
        sources.extend(v for v in up.values() if isinstance(v, dict))
    for src in sources:
        for key in ("human_message", "human_decision", "decision", "message"):
            val = src.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip().lower()
    return ""


def _is_authorized(input_data: dict) -> bool:
    """Fail closed: only an explicit allow token counts; unknown => not authorised."""
    sig = _signal(input_data)
    if sig in _BLOCK_TOKENS:
        return False
    return sig in _ALLOW_TOKENS


def _is_blocked(input_data: dict) -> bool:
    return _signal(input_data) in _BLOCK_TOKENS


def _needs_authorization(wo: dict) -> bool:
    """An emergency / safety / engineer-review job needs an explicit human
    authorisation; a routine job (parts on hand, no review) does not."""
    return bool(wo.get("requires_engineer_review")
                or str(wo.get("work_type") or "") in ("emergency", "unscheduled_review")
                or wo.get("safety_critical"))


def _wo_id(input_data: dict, wo: dict) -> str:
    for src in (input_data, wo):
        if isinstance(src, dict) and src.get("work_order_id"):
            return str(src["work_order_id"])
    up = input_data.get("upstream_yields")
    if isinstance(up, dict):
        for v in [up, *up.values()]:
            if isinstance(v, dict) and v.get("work_order_id"):
                return str(v["work_order_id"])
    return ""


def _recover_wo(input_data: dict, context: dict) -> dict:
    """Recover the planned work order across the HITL gap: upstream -> stash ->
    dev store (pending WOs) -> the open WO on the asset."""
    up = input_data.get("upstream_yields") or {}
    for src in (input_data, up if isinstance(up, dict) else None,
                *(up.values() if isinstance(up, dict) else [])):
        if isinstance(src, dict) and src.get("work_order_id") and "work_type" in src:
            return dict(src)
    sid = resolve_session_id(input_data, context)
    stashed = stash_get(sid)
    if stashed.get("work_order_id"):
        return stashed
    wo_id = _wo_id(input_data, {})
    pend = store.get_pending_wo(wo_id) if wo_id else {}
    if pend.get("work_order_id"):
        return pend
    return {"work_order_id": wo_id}


def _render_pack(wo: dict) -> str:
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    wo_id = str(wo.get("work_order_id") or "work_order")
    path = _OUT_DIR / f"{wo_id}_work_order.md"
    lines = [
        f"# Approved work order — {wo_id}",
        f"Asset: {wo.get('asset_id') or wo.get('failure_mode') or 'on record'}",
        f"Work type: {wo.get('work_type', '')}   Priority: {wo.get('priority', '')}",
        f"Window start (UTC): {wo.get('window_start_utc', '')}",
        f"Failure mode: {wo.get('failure_mode', '')}",
        f"Parts available: {wo.get('parts_available')}   Lead time (h): {wo.get('lead_time_hours', 0)}",
        "",
        "_Issued to the CMMS and dispatched to a technician (dev-store record)._",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return str(path)


def _delete_partial(result: dict) -> None:
    p = result.get("pack_path")
    if p:
        Path(p).unlink(missing_ok=True)
        result["pack_path"] = ""


def enforce_issuance_floor(result: Any, context: dict) -> dict:
    """The finisher floor — issue ONLY on a real authorisation + clean
    invariants; delete the partial on a tripped floor."""
    result = result if isinstance(result, dict) else {}
    result.setdefault("block_reasons", [])
    wo = result.pop("_wo", {}) or {}
    inp = result.pop("_input", {}) or {}

    # (1) Authorisation.
    if not result.get("authorized"):
        result.update(issued=False, status="blocked",
                      dispatched_to="held_unauthorized")
        if _is_blocked(inp):
            result["block_reasons"].append("engineer rejected / held")
        else:
            result["block_reasons"].append("no explicit authorisation on record (fail closed)")
        _delete_partial(result)
        logger.info(f"[issuer] floor BLOCKED (auth): {result['block_reasons']}")
        return result

    # (2) Invariants a human can't waive.
    if not wo.get("work_order_id"):
        result.update(issued=False, status="blocked", dispatched_to="held_untraceable")
        result["block_reasons"].append("no traceable work order to issue")
        _delete_partial(result)
        return result
    if not wo.get("parts_available"):
        result.update(issued=False, status="blocked", dispatched_to="held_pending_parts")
        result["block_reasons"].append("parts not on hand — refuse to issue into a parts gap")
        _delete_partial(result)
        return result

    # Passed the floor — render, dispatch, ledger.
    result["pack_path"] = _render_pack(wo)
    store.record_outbox({"work_order_id": wo.get("work_order_id"),
                         "action": "issue_to_cmms", "pack_path": result["pack_path"]})
    store.append_ledger({"work_order_id": wo.get("work_order_id"),
                         "event": "issued_and_dispatched",
                         "work_type": wo.get("work_type"),
                         "priority": wo.get("priority")})
    result.update(issued=True, status="issued", dispatched_to="technician")
    logger.info(f"[issuer] issued + dispatched {wo.get('work_order_id')} "
                f"-> {result['pack_path']}")
    return result


@chain(enforce_issuance_floor)
async def work_order_issuer_agent(llm_response: Any, input_data: Any,
                                  context: Any) -> dict:
    input_data = input_data if isinstance(input_data, dict) else {}
    context = context if isinstance(context, dict) else {}
    wo = _recover_wo(input_data, context)
    sid = resolve_session_id(input_data, context)
    if wo.get("work_order_id"):
        stash_put(sid, wo)   # keep it recoverable across the gate

    # Routine jobs are auto-authorised by the plan; emergency/safety jobs need
    # an explicit allow token (fail closed on unknown).
    if _needs_authorization(wo):
        authorized = _is_authorized(input_data)
    else:
        authorized = not _is_blocked(input_data)

    return {
        "issued": False, "authorized": authorized,
        "work_order_id": str(wo.get("work_order_id") or ""),
        "pack_path": "", "dispatched_to": "", "status": "", "block_reasons": [],
        "_wo": wo, "_input": input_data,
    }
