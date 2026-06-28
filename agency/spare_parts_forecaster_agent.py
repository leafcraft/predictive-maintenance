"""spare_parts_forecaster_agent — self-reliant parts-demand forecaster.

@pre_compose  load_demand_signals — the forecaster pulls its OWN data
              before forecasting: the full spare-parts inventory (on-hand +
              lead time) AND the per-SKU consumption history (issues-to-
              work-order run-rate) from the dev store (PROD: ERP/MRP parts
              master + issues ledger). It forecasts over the real trailing
              run-rate, not a spoon-fed demand blob.

@chain        enforce_stockout_floor — a deterministic floor the LLM can't
              waive: any SKU whose days-to-stockout (on-hand ÷ observed
              run-rate) falls AT OR BELOW its replenishment lead time is a
              guaranteed shortage and is FORCED into `critical_shortages`,
              even if the LLM's optimistic forecast left it out. A missed
              critical shortage causes unplanned downtime — the most
              expensive failure mode this agent exists to prevent.

WHY THIS MODULE EXISTS (was pure-YAML): grounding the forecast in real
consumption (@pre_compose) and a non-waivable shortage floor (@chain) is
the depth bar — the prompt alone can't guarantee a known stockout surfaces.
The weekly cron (configs/config.yaml `wake_up`) is unchanged.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from leafmesh import LeafMeshLogger, pre_compose, chain
from agency._shared import store

logger = LeafMeshLogger(__name__)

FORECAST_WINDOW_DAYS = 90

_DEFAULTS: dict[str, Any] = {
    "parts_needed_30d": [], "parts_needed_60d": [], "parts_needed_90d": [],
    "critical_shortages": [], "overstock_candidates": [],
    "executive_summary": "", "confidence_overall": 0.0,
}


def load_demand_signals(data: Any, ctx: Any) -> dict[str, Any]:
    """@pre_compose — pull the inventory + consumption run-rate so the LLM
    forecasts over real data, and pre-compute the deterministic demand
    signals the stockout floor reads."""
    signals = store.parts_demand_signals(FORECAST_WINDOW_DAYS)
    return {
        "window_days": FORECAST_WINDOW_DAYS,
        "inventory": store.parts_inventory(),
        "demand_signals": signals,
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
        if ctx.get("demand_signals") or ctx.get("inventory"):
            return ctx
    return {}


def _lead_days(sig: dict[str, Any]) -> float:
    return float(sig.get("lead_time_hours") or 0.0) / 24.0


def _list(v: Any) -> list[Any]:
    return list(v) if isinstance(v, list) else []


async def enforce_stockout_floor(result: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """@chain — the critical-shortage floor (LLM-unwaivable).

    A SKU whose days-to-stockout is at/below its lead time WILL run out
    before a reorder lands → it is guaranteed to be in `critical_shortages`,
    regardless of the LLM's forecast. Pulls signals from the store on a
    bare direct call so the floor holds even without an injected context.
    """
    prepared = _prepared(ctx)
    signals = prepared.get("demand_signals")
    if not signals:
        signals = store.parts_demand_signals(FORECAST_WINDOW_DAYS)

    shortages = _list(result.get("critical_shortages"))
    listed = {str(s.get("part_sku")) for s in shortages if isinstance(s, dict)}

    forced: list[str] = []
    for sig in signals:
        dts = sig.get("days_to_stockout")
        if dts is None:
            continue                      # no observed demand → not a shortage
        lead_days = _lead_days(sig)
        if float(dts) <= lead_days:
            sku = str(sig.get("part_sku"))
            if sku not in listed:
                shortages.append({
                    "part_sku": sku,
                    "current_stock": sig.get("on_hand"),
                    "predicted_demand_days": sig.get("window_days"),
                    "days_to_stockout": dts,
                })
                listed.add(sku)
                forced.append(sku)

    result["critical_shortages"] = shortages
    if forced:
        logger.warning(f"[parts-forecast] FLOOR forced critical shortages "
                       f"(stockout within lead time): {forced}")
    else:
        logger.info(f"[parts-forecast] {len(shortages)} critical shortage(s), "
                    f"no floor additions")
    return result


@pre_compose(context_processor=load_demand_signals)
@chain(enforce_stockout_floor)
async def spare_parts_forecaster_agent(llm_response: Any, input_data: Any,
                                       context: Any) -> dict[str, Any]:
    """Body — normalise the LLM's 90-day forecast into the yield shape.
    The critical-shortage floor (@chain) runs next and guarantees a known
    stockout surfaces."""
    llm = _parse(llm_response)
    out = {**_DEFAULTS}

    for k in ("parts_needed_30d", "parts_needed_60d", "parts_needed_90d",
              "critical_shortages", "overstock_candidates"):
        out[k] = _list(llm.get(k))

    out["executive_summary"] = str(llm.get("executive_summary") or "")
    try:
        conf = float(llm.get("confidence_overall"))
    except (TypeError, ValueError):
        conf = 0.0
    out["confidence_overall"] = min(max(conf, 0.0), 1.0)

    logger.info(f"[parts-forecast] LLM forecast: "
                f"{len(out['critical_shortages'])} shortage(s), "
                f"conf={out['confidence_overall']:.2f} "
                f"(enforce_stockout_floor runs next)")
    return out
