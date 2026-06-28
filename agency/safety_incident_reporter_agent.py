"""safety_incident_reporter_agent — self-reliant OSHA severity floor.

@pre_compose  load_incident_context — the reporter pulls its OWN context
              before classifying: the asset master + any open work order
              on the asset + the asset's recent incident history from the
              dev store (PROD: EHS system + EAM/CMMS), so the LLM grounds
              the narrative in the real asset, not a spoon-fed blob.

@chain        enforce_severity_floor — a deterministic SAFETY-SEVERITY
              FLOOR the LLM cannot waive. OSHA reporting windows
              (29 CFR 1904.39: 8h work-related fatality / 24h
              hospitalisation-amputation-eye-loss) and recordability
              (29 CFR 1904.7: OSHA 300 log within 7 days) are statutory
              compliance data. The floor maps the classified OUTCOME onto
              the mandated severity, the statutory reporting route, and the
              escalation flags — a lenient LLM that tries to down-rate a
              recordable/serious incident to "minor" is overridden, and a
              serious event is ALWAYS forced onto the immediate-escalation
              (mandated reporting) route. Incident records themselves live
              in your EHS system + the WORM audit trail, not template
              storage.

WHY THIS MODULE EXISTS (YAML-first exception): exactly the depth bar —
self-reliant context (@pre_compose) + a non-waivable compliance floor
(@chain). The prompt alone could be talked into the wrong reporting route.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from leafmesh import LeafMeshLogger, pre_compose, chain
from agency._shared import store

logger = LeafMeshLogger(__name__)

# ── OSHA 29 CFR 1904.39 reporting deadlines (hours) ──────────────
FATALITY_REPORT_HOURS = 8        # work-related death → report within 8h
SERIOUS_EVENT_REPORT_HOURS = 24  # hospitalisation / amputation / eye loss → 24h
RECORDABLE_LOG_HOURS = 168       # OSHA 300 log entry within 7 calendar days

# Outcomes that trigger the 8h / 24h rapid-report rules
SERIOUS_OUTCOMES = frozenset({
    "death", "hospitalisation", "hospitalization", "amputation", "eye_loss",
})

# OSHA recordable criteria (29 CFR 1904.7, simplified)
RECORDABLE_OUTCOMES = frozenset({
    "death", "days_away_from_work", "restricted_work",
    "loss_of_consciousness", "medical_treatment_beyond_first_aid",
    "significant_injury_diagnosed",
})

# Outcomes that legitimately need NO safety-manager review. Anything
# else (including an empty / unrecognised outcome) fails closed to a
# human review.
NON_REVIEW_OUTCOMES = frozenset({"first_aid_only"})

# Severity rank — the floor can only raise severity, never lower it.
_SEVERITY_RANK = {"minor": 0, "recordable": 1, "serious": 2}

_DEFAULTS: dict[str, Any] = {
    "incident_id": "", "occurred_at_utc": "", "asset_id": "",
    "work_order_id": "", "incident_type": "injury", "outcome": "",
    "state": "", "severity": "minor", "osha_recordable": False,
    "osha_report_window_hours": 0, "state_filings_required": [],
    "requires_safety_manager_review": False,
    "requires_immediate_escalation": False, "recorded_at_utc": "",
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
    return ""


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


def load_incident_context(data: Any, ctx: Any) -> dict[str, Any]:
    """@pre_compose — the reporter pulls its own context: the asset master,
    any open work order on the asset, and the asset's recent maintenance
    history (so a repeat-offender asset is visible to the narrative)."""
    aid = _asset_id(data)
    return {
        "asset_id": aid,
        "asset_record": store.asset(aid) if aid else {},
        "open_work_order": store.open_work_order(aid) if aid else {},
        "recent_history": store.asset_history(aid)[-3:] if aid else [],
    }


def _osha_severity(outcome: str) -> tuple[str, int]:
    """Map a classified outcome onto the statutory severity + reporting
    window (the OSHA floor — deterministic, never hallucinated)."""
    if outcome == "death":
        return "serious", FATALITY_REPORT_HOURS
    if outcome in SERIOUS_OUTCOMES:
        return "serious", SERIOUS_EVENT_REPORT_HOURS
    if outcome in RECORDABLE_OUTCOMES:
        return "recordable", RECORDABLE_LOG_HOURS
    return "minor", 0


async def enforce_severity_floor(result: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """@chain — the SAFETY-SEVERITY FLOOR the LLM can't waive.

    The OSHA outcome dictates the mandated severity, reporting window,
    recordability and escalation route. The LLM's own `severity` is only
    allowed to RAISE the floor, never lower it: a recordable/serious
    incident keeps its statutory route even if the model wrote "minor".
    """
    outcome = str(result.get("outcome") or "").lower()
    incident_type = str(result.get("incident_type") or "injury").lower()
    state = str(result.get("state") or "").upper()
    incident_id = str(result.get("incident_id") or "")

    floor_sev, window = _osha_severity(outcome)

    # The floor can only raise severity — never let an LLM down-rate.
    llm_sev = str(result.get("severity") or "").lower()
    if _SEVERITY_RANK.get(llm_sev, -1) > _SEVERITY_RANK.get(floor_sev, 0):
        severity = llm_sev
        # If the LLM raised to serious without a serious outcome, give it
        # the conservative 24h window rather than 0/168.
        window = window or SERIOUS_EVENT_REPORT_HOURS
    else:
        severity = floor_sev

    osha_recordable = severity in ("recordable", "serious")

    # State filings (simplified taxonomy).
    state_filings: list[str] = []
    if severity == "serious" and state:
        state_filings.append(f"{state}_serious_incident_report")
    if incident_type == "unsafe_condition" and state in ("CA", "WA", "OR"):
        state_filings.append(f"{state}_hazard_disclosure")

    # MANDATED reporting route: a serious event is always forced onto the
    # immediate-escalation path (the wire fires on this) — the LLM cannot
    # divert it to a routine review.
    requires_immediate = severity == "serious"
    # Fail-closed: anything but a recognised no-review outcome (and a
    # traceable incident id) reaches the safety manager.
    requires_review = (severity != "minor"
                       or outcome not in NON_REVIEW_OUTCOMES
                       or not incident_id)

    result["severity"] = severity
    result["osha_recordable"] = osha_recordable
    result["osha_report_window_hours"] = window
    result["state_filings_required"] = state_filings
    result["requires_immediate_escalation"] = requires_immediate
    result["requires_safety_manager_review"] = requires_review
    result["recorded_at_utc"] = datetime.now(timezone.utc).isoformat()

    if osha_recordable or requires_immediate:
        logger.warning(f"[safety] {incident_id or '?'} FLOOR outcome={outcome or '?'} "
                       f"→ severity={severity} window={window}h "
                       f"osha={osha_recordable} immediate={requires_immediate} "
                       f"filings={state_filings}")
    else:
        logger.info(f"[safety] {incident_id or '?'} outcome={outcome or '?'} "
                    f"severity={severity} (no floor)")
    return result


@pre_compose(context_processor=load_incident_context)
@chain(enforce_severity_floor)
async def safety_incident_reporter_agent(
    llm_response: Any,
    input_data: Any,
    context: Any,
) -> dict[str, Any]:
    """Body — normalise the LLM's classification. The OSHA severity,
    recordability, statutory window and escalation route are forced by the
    @chain floor that runs next (the LLM only classifies the narrative)."""
    data = _parse(llm_response)
    input_data = input_data if isinstance(input_data, dict) else {}

    incident_id = str(input_data.get("incident_id")
                      or data.get("incident_id") or "")
    aid = str(data.get("asset_id") or _asset_id(input_data) or "")

    out = {**_DEFAULTS}
    out.update({
        "incident_id": incident_id,
        "occurred_at_utc": str(data.get("occurred_at_utc") or ""),
        "asset_id": aid,
        "work_order_id": str(data.get("work_order_id") or ""),
        "incident_type": str(data.get("incident_type") or "injury").lower(),
        "outcome": str(data.get("outcome") or "").lower(),
        "state": str(data.get("state") or "").upper(),
        # Echoed from the LLM; the @chain floor is authoritative for these.
        "severity": str(data.get("severity") or "minor").lower(),
    })
    logger.info(f"[safety] {incident_id or '?'} classified "
                f"outcome={out['outcome'] or '?'} (enforce_severity_floor runs next)")
    return out
