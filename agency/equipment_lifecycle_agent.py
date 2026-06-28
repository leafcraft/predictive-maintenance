"""equipment_lifecycle_agent — self-reliant asset lifecycle planner.

@pre_compose  load_asset_dossier — the planner pulls its OWN dossier
              before deciding: the asset master (install date, design
              life, replacement cost, warranty) and the full maintenance
              history (lifetime spend + failure count) from the dev store
              (PROD: EAM + CMMS). The LLM then reasons over real numbers
              instead of inventing them.
@chain        enforce_decision_floor — a deterministic floor the LLM
              can't waive: when the asset is past design life AND its
              cost-to-keep has crossed the replace threshold, the
              recommended_action is forced to at least "overhaul" and the
              capex/planning flags are recomputed from the action — a
              lenient "continue" can't slip a worn-out asset through.

Was pure-YAML; promoted because grounding the decision in real records
(@pre_compose) and a non-waivable threshold (@chain) is the depth bar.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from leafmesh import LeafMeshLogger, pre_compose, chain
from agency._shared import store

logger = LeafMeshLogger(__name__)

# Deterministic floor thresholds (mirror the prompt's DECISION POLICY).
PAST_LIFE_PCT = 1.0
REPLACE_RATIO = 0.25

_DEFAULTS: dict[str, Any] = {
    "asset_id": "", "asset_type": "", "age_years": 0.0,
    "age_pct_of_design_life": 0.0, "cumulative_maint_cents": 0,
    "cost_to_keep_1y_cents": 0, "replacement_cost_cents": 0,
    "keep_vs_replace_ratio": 0.0, "failures_lifetime": 0,
    "warranty_window_days": 0, "warranty_action_window": False,
    "recommended_action": "continue", "requires_capex_approval": False,
    "requires_maintenance_planning": False, "assessed_at_utc": "",
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


def _age_years(installed_utc: str) -> float:
    try:
        inst = datetime.fromisoformat(str(installed_utc).replace("Z", "+00:00"))
        return round((datetime.now(timezone.utc) - inst).days / 365.0, 2)
    except (ValueError, TypeError):
        return 0.0


def load_asset_dossier(data: Any, ctx: Any) -> dict[str, Any]:
    """@pre_compose — pull the asset master + full maintenance history and
    pre-derive the deterministic numbers the LLM should not invent."""
    aid = _asset_id(data)
    rec = store.asset(aid)
    history = store.asset_history(aid)
    cumulative = sum(int(h.get("cost_cents") or 0) for h in history)
    failures = sum(1 for h in history if h.get("wo_kind") in ("corrective", "emergency"))
    age = _age_years(rec.get("installed_utc", ""))
    design = float(rec.get("design_life_years") or 0.0) or 1.0
    repl = int(rec.get("replacement_cost_cents") or 0)
    keep_1y = int(cumulative / age) if age else 0
    ratio = round(keep_1y / repl, 3) if repl else 0.0
    return {
        "asset_id": aid, "asset_record": rec, "history_count": len(history),
        "age_years": age, "age_pct_of_design_life": round(age / design, 3),
        "cumulative_maint_cents": cumulative, "failures_lifetime": failures,
        "cost_to_keep_1y_cents": keep_1y, "replacement_cost_cents": repl,
        "keep_vs_replace_ratio": ratio,
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
    if isinstance(ctx, dict):
        for k in ("prepared_data", "prepared", "pre_compose"):
            v = ctx.get(k)
            if isinstance(v, dict) and v:
                return v
        if ctx.get("asset_record") or ctx.get("age_pct_of_design_life"):
            return ctx
    return {}


async def enforce_decision_floor(result: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """@chain — past design life + over the replace threshold forces at
    least an overhaul; flags are recomputed from the final action. Falls
    back to a store dossier so the floor holds even on a bare direct call
    (where @pre_compose did not inject the dossier)."""
    prepared = _prepared(ctx)
    if not prepared.get("age_pct_of_design_life") and result.get("asset_id"):
        prepared = load_asset_dossier({"asset_id": result["asset_id"]}, {})
    age_pct = float(result.get("age_pct_of_design_life")
                    or prepared.get("age_pct_of_design_life") or 0.0)
    ratio = float(result.get("keep_vs_replace_ratio")
                  or prepared.get("keep_vs_replace_ratio") or 0.0)
    action = str(result.get("recommended_action") or "continue")

    if age_pct >= PAST_LIFE_PCT and ratio >= REPLACE_RATIO and action == "continue":
        action = "overhaul"
        logger.warning(f"[lifecycle] {result.get('asset_id')} floor: past life "
                       f"({age_pct:.2f}) + ratio {ratio:.2f} → forced overhaul")

    result["recommended_action"] = action
    # Flags recomputed deterministically from the FINAL action.
    result["requires_capex_approval"] = (action == "replace")
    result["requires_maintenance_planning"] = (action == "overhaul")
    return result


@pre_compose(context_processor=load_asset_dossier)
@chain(enforce_decision_floor)
async def equipment_lifecycle_agent(llm_response: Any, input_data: Any,
                                    context: Any) -> dict[str, Any]:
    llm = _parse(llm_response)
    aid = _asset_id(input_data)
    prepared = _prepared(context)
    if not prepared.get("age_pct_of_design_life"):
        # @pre_compose did not inject (e.g. a bare direct call) — pull the
        # dossier ourselves so the deterministic numbers are still real.
        prepared = load_asset_dossier({"asset_id": aid}, {})
    rec = prepared.get("asset_record") or store.asset(aid)

    out = {**_DEFAULTS}
    # Deterministic numbers come from the dossier; the LLM supplies the
    # action + warranty judgement + narrative.
    out.update({
        "asset_id": aid,
        "asset_type": str(rec.get("asset_type") or llm.get("asset_type") or ""),
        "age_years": float(prepared.get("age_years") or 0.0),
        "age_pct_of_design_life": float(prepared.get("age_pct_of_design_life") or 0.0),
        "cumulative_maint_cents": int(prepared.get("cumulative_maint_cents") or 0),
        "cost_to_keep_1y_cents": int(prepared.get("cost_to_keep_1y_cents") or 0),
        "replacement_cost_cents": int(prepared.get("replacement_cost_cents") or 0),
        "keep_vs_replace_ratio": float(prepared.get("keep_vs_replace_ratio") or 0.0),
        "failures_lifetime": int(prepared.get("failures_lifetime") or 0),
        "warranty_window_days": int(llm.get("warranty_window_days") or 0),
        "warranty_action_window": bool(llm.get("warranty_action_window")),
        "recommended_action": str(llm.get("recommended_action") or "continue"),
        "assessed_at_utc": datetime.now(timezone.utc).isoformat(),
    })
    logger.info(f"[lifecycle] {aid} age%={out['age_pct_of_design_life']:.2f} "
                f"ratio={out['keep_vs_replace_ratio']:.2f} "
                f"action={out['recommended_action']} (floor runs next)")
    return out
