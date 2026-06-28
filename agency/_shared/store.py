"""Dev-mode store for predictive maintenance.

Stands in for the plant's systems of record so the FULL chain runs
end-to-end before any connector is wired:
  * plant historian / IIoT  → per-asset condition signal series
  * asset master            → install date, design life, criticality
  * CMMS                     → maintenance records + open work orders
  * spare-parts inventory   → on-hand, lead time, asset compatibility
  * spare-parts issues ledger → per-SKU consumption history (run-rate)

Replace each read with the matching MCP connector in prod; the function
signatures are the contract. The integration points (do nothing until
the calls here are wired):
  * HISTORIAN_MCP_URL → signal_series() / latest_reading()   (failure_predictor_agent)
  * CMMS_MCP_URL      → asset_history() / open_work_orders()  (maintenance_records_agent, scheduler)
  * (parts master)    → part_for_failure_mode() / on_hand()  (work_order_scheduler_agent)
Setting those env vars alone does nothing until you wire the calls here.

Single-process JSON files under ./data — survives the HITL wait because
the mesh is one process. Swap for the Redis/Postgres session store at
multi-replica scale; the get/put interface stays the same.

The seed is tuned so the INTERESTING paths are all reachable out of the
box: Extruder-7 carries a rising vibration anomaly inside the shutdown
window, and its replacement bearing's lead time exceeds the remaining
useful life — so the failure-probability floor, the safety-critical
hard-stop, and the parts-availability gate all fire on the demo asset.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()
_DIR = Path("./data")

# The demo asset — every default-call path resolves to this one.
DEMO_ASSET = "extruder-7"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _days(n: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=n)).isoformat()


def _path(k: str) -> Path:
    _DIR.mkdir(parents=True, exist_ok=True)
    return _DIR / f"{k}.json"


def _load(k: str) -> Any:
    p = _path(k)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (ValueError, OSError):
        return None


def _save(k: str, d: Any) -> None:
    p = _path(k)
    t = p.with_suffix(".json.tmp")
    t.write_text(json.dumps(d, indent=2, default=str))
    t.replace(p)


# ── Seed: the asset master (install date, design life, criticality) ──
_SEED_ASSETS = {
    "extruder-7": {
        "asset_id": "extruder-7", "asset_type": "extruder",
        "criticality": "safety_critical",     # leak / fire / pinch hazard on failure
        "installed_utc": _days(-365 * 11),     # 11 years old
        "design_life_years": 10,               # past design life
        "replacement_cost_cents": 9_000_000,   # $90k to replace
        "warranty_expires_utc": _days(-400),
        "line_id": "LINE-2",
    },
    "pump-107-feedwater": {
        "asset_id": "pump-107-feedwater", "asset_type": "centrifugal_pump",
        "criticality": "standard",
        "installed_utc": _days(-365 * 4),      # 4 years old
        "design_life_years": 15,
        "replacement_cost_cents": 2_400_000,   # $24k
        "warranty_expires_utc": _days(120),
        "line_id": "LINE-1",
    },
}

# ── Seed: the latest condition reading per asset + a short series ─────
# Extruder-7 carries a rising vibration anomaly INSIDE the shutdown
# window. pump-107 is nominal (a clean-pass control asset for the demo).
_SEED_READINGS = {
    "extruder-7": {
        "asset_id": "extruder-7",
        "captured_at_utc": None,               # stamped on first read
        "vibration_rms_mm_s": 11.8,            # ISO 10816 zone D (unacceptable)
        "vibration_baseline_mm_s": 3.1,
        "temp_c": 96.0,
        "temp_baseline_c": 71.0,
        "current_a": 24.9,
        "current_baseline_a": 19.0,
        "anomaly_detected": True,
        "anomaly_score": 0.91,
        "trend_3d": "rising",
        "dominant_signature": "bearing_wear",
        # short telemetry series — what a pre_compose feeds the RUL model
        "vibration_series_3d": [4.0, 5.2, 7.1, 9.0, 11.8],
        "temp_series_3d": [73, 78, 84, 90, 96],
    },
    "pump-107-feedwater": {
        "asset_id": "pump-107-feedwater",
        "captured_at_utc": None,
        "vibration_rms_mm_s": 2.6,
        "vibration_baseline_mm_s": 2.4,
        "temp_c": 62.0,
        "temp_baseline_c": 60.0,
        "current_a": 17.8,
        "current_baseline_a": 17.5,
        "anomaly_detected": False,
        "anomaly_score": 0.08,
        "trend_3d": "flat",
        "dominant_signature": "none",
        "vibration_series_3d": [2.4, 2.5, 2.5, 2.6, 2.6],
        "temp_series_3d": [60, 60, 61, 61, 62],
    },
}

# ── Seed: spare-parts inventory keyed by part SKU ────────────────────
# The bearing that fixes extruder-7's bearing_wear is OUT of stock with a
# 72h lead time — longer than the RUL — so the parts gate fires.
_SEED_PARTS = {
    "bearing-skf-22320": {
        "part_sku": "bearing-skf-22320", "description": "Spherical roller bearing",
        "on_hand": 0, "lead_time_hours": 72.0, "unit_cost_cents": 84_000,
    },
    "seal-kit-ext7": {
        "part_sku": "seal-kit-ext7", "description": "Extruder mechanical seal kit",
        "on_hand": 3, "lead_time_hours": 0.0, "unit_cost_cents": 21_000,
    },
    "pump-impeller-107": {
        "part_sku": "pump-impeller-107", "description": "Feedwater pump impeller",
        "on_hand": 2, "lead_time_hours": 0.0, "unit_cost_cents": 46_000,
    },
}

# Failure-mode → the part(s) needed to repair it.
_SEED_FAILURE_PART_MAP = {
    "bearing_wear": ["bearing-skf-22320"],
    "seal_leak": ["seal-kit-ext7"],
    "impeller_erosion": ["pump-impeller-107"],
}

# ── Seed: spare-parts CONSUMPTION history (issued-to-work-order events) ──
# What a real MRP/ERP issues-history query returns: each part draw against
# a work order, dated. The forecaster reasons over THIS — the trailing
# run-rate per SKU — not a spoon-fed demand blob. The bearing burns at a
# rising clip (3 draws in 90d) on the degrading extruder; the seal kit and
# impeller are slow movers (capital tied up → overstock candidates).
_SEED_PARTS_USAGE = {
    "bearing-skf-22320": [
        {"issued_at_utc": _days(-78), "qty": 1, "work_order_id": "wo-ext7-h1", "asset_id": "extruder-7"},
        {"issued_at_utc": _days(-41), "qty": 1, "work_order_id": "wo-ext7-h2", "asset_id": "extruder-7"},
        {"issued_at_utc": _days(-9),  "qty": 1, "work_order_id": "wo-ext7-h3", "asset_id": "extruder-7"},
    ],
    "seal-kit-ext7": [
        {"issued_at_utc": _days(-200), "qty": 1, "work_order_id": "wo-ext7-h0", "asset_id": "extruder-7"},
    ],
    "pump-impeller-107": [],   # no draws in window — pure slow-mover
}

# ── Seed: CMMS maintenance records + open work orders ────────────────
_SEED_RECORDS = {
    # Heavy, recurring corrective spend on an 11-year-old extruder — the
    # cost-to-keep has crept past the replace threshold (the lifecycle
    # floor's whole point). Costs in cents; ~$2.5M lifetime upkeep on a
    # $90k-to-replace asset → keep-vs-replace ratio well over 0.25.
    "extruder-7": [
        {"wo_kind": "corrective", "opened_at_utc": _days(-22), "closed_at_utc": _days(-22),
         "cost_cents": 6_400_000, "root_cause": "bearing_wear", "downtime_mins": 240},
        {"wo_kind": "corrective", "opened_at_utc": _days(-12), "closed_at_utc": _days(-12),
         "cost_cents": 5_800_000, "root_cause": "bearing_wear", "downtime_mins": 180},
        {"wo_kind": "corrective", "opened_at_utc": _days(-3), "closed_at_utc": _days(-3),
         "cost_cents": 7_100_000, "root_cause": "vibration_alarm", "downtime_mins": 300},
        {"wo_kind": "emergency", "opened_at_utc": _days(-200), "closed_at_utc": _days(-199),
         "cost_cents": 8_900_000, "root_cause": "seal_leak", "downtime_mins": 720},
        {"wo_kind": "planned", "opened_at_utc": _days(-60), "closed_at_utc": _days(-60),
         "cost_cents": 1_200_000, "root_cause": "pm_lubrication", "downtime_mins": 60},
    ],
    "pump-107-feedwater": [
        {"wo_kind": "planned", "opened_at_utc": _days(-90), "closed_at_utc": _days(-90),
         "cost_cents": 8_000, "root_cause": "pm_inspection", "downtime_mins": 45},
    ],
}

# An open work order on the demo asset — supports the parts_arrived
# re-entry (same work_order_id) without minting a new one.
_SEED_OPEN_WOS = {
    "extruder-7": {"work_order_id": "wo-ext7-open01", "work_type": "corrective",
                   "status": "awaiting_parts", "opened_at_utc": _days(-1)},
}


def _seeded(key: str, seed: Any) -> Any:
    s = _load(key)
    if s is None:
        s = seed
        _save(key, s)
    return s


# ── Reads (the plant's systems of record) ────────────────────────────
def asset(asset_id: str = DEMO_ASSET) -> dict[str, Any]:
    """Asset master record. PROD: EAM / asset register."""
    return dict(_seeded("assets", _SEED_ASSETS).get(asset_id, {}))


def latest_reading(asset_id: str = DEMO_ASSET) -> dict[str, Any]:
    """Most recent condition snapshot. PROD: plant historian latest tag."""
    readings = _seeded("readings", _SEED_READINGS)
    rec = dict(readings.get(asset_id, {}))
    if rec and not rec.get("captured_at_utc"):
        rec["captured_at_utc"] = _now()
    return rec


def signal_series(asset_id: str = DEMO_ASSET) -> dict[str, Any]:
    """Short telemetry series the RUL model reasons over. PROD: historian
    windowed query (e.g. last 72h of vibration/temp tags)."""
    r = latest_reading(asset_id)
    return {
        "asset_id": asset_id,
        "vibration_series_3d": r.get("vibration_series_3d", []),
        "temp_series_3d": r.get("temp_series_3d", []),
        "vibration_baseline_mm_s": r.get("vibration_baseline_mm_s", 0.0),
        "temp_baseline_c": r.get("temp_baseline_c", 0.0),
        "dominant_signature": r.get("dominant_signature", "unknown"),
        "trend_3d": r.get("trend_3d", "flat"),
    }


def asset_history(asset_id: str = DEMO_ASSET) -> list[dict[str, Any]]:
    """Maintenance work-order history. PROD: CMMS work-order search."""
    return list(_seeded("records", _SEED_RECORDS).get(asset_id, []))


def open_work_order(asset_id: str = DEMO_ASSET) -> dict[str, Any]:
    """The open work order on an asset, if any. PROD: CMMS open-WO query.
    Used for parts_arrived re-entry so the pipeline reuses the WO id."""
    return dict(_seeded("open_wos", _SEED_OPEN_WOS).get(asset_id, {}))


def part(part_sku: str) -> dict[str, Any]:
    """Spare-part inventory record. PROD: ERP/MRP parts master."""
    return dict(_seeded("parts", _SEED_PARTS).get(part_sku, {}))


def parts_for_failure_mode(failure_mode: str) -> list[str]:
    """Which part SKUs a failure mode needs. PROD: maintenance BOM."""
    return list(_seeded("failure_part_map", _SEED_FAILURE_PART_MAP).get(
        str(failure_mode or "").lower(), []))


def parts_inventory() -> list[dict[str, Any]]:
    """Full spare-parts inventory (on-hand + lead time + unit cost) across
    the fleet. PROD: ERP/MRP parts master snapshot."""
    return [dict(v) for v in _seeded("parts", _SEED_PARTS).values()]


def parts_usage(part_sku: str) -> list[dict[str, Any]]:
    """Consumption (issues-to-work-order) history for one SKU, oldest first.
    PROD: ERP/MRP parts-issue ledger."""
    rows = _seeded("parts_usage", _SEED_PARTS_USAGE).get(str(part_sku), [])
    return list(rows)


# ── Domain math the gate floors rely on (deterministic, no LLM) ──────
def parts_availability(failure_mode: str) -> dict[str, Any]:
    """Resolve the parts a repair needs against on-hand inventory + lead
    time. The parts-availability GATE's math: a part not on hand is only
    "available" once its lead time is known; the scheduler then compares
    that lead time against the RUL (fail-closed) before dispatching.

    Returns {parts_available, max_lead_time_hours, parts_short, parts}.
    """
    skus = parts_for_failure_mode(failure_mode)
    parts: list[dict[str, Any]] = []
    short: list[str] = []
    max_lead = 0.0
    for sku in skus:
        rec = part(sku)
        on_hand = int(rec.get("on_hand") or 0)
        lead = float(rec.get("lead_time_hours") or 0.0)
        parts.append({"part_sku": sku, "on_hand": on_hand, "lead_time_hours": lead})
        if on_hand <= 0:
            short.append(sku)
            max_lead = max(max_lead, lead)
    return {
        "parts_available": not short,
        "max_lead_time_hours": max_lead,
        "parts_short": short,
        "parts": parts,
        "failure_mode": str(failure_mode or "").lower(),
        "resolved": bool(skus),
    }


def _days_ago(iso: str) -> float:
    """Whole days between an ISO-8601 stamp and now (>= 0)."""
    try:
        t = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        return max((datetime.now(timezone.utc) - t).total_seconds() / 86400.0, 0.0)
    except (ValueError, TypeError):
        return 0.0


def parts_demand_signals(window_days: int = 90) -> list[dict[str, Any]]:
    """Deterministic per-SKU demand picture the spare-parts forecaster
    reasons over (and the @chain stockout floor reads). For each part:
    trailing consumption over `window_days`, a daily run-rate, on-hand
    cover, and days-to-stockout at the observed rate. PROD: this is where
    a demand-planning model output would land; the floor stays
    model-agnostic because it only consumes these signals.
    """
    window = max(int(window_days or 0), 1)
    out: list[dict[str, Any]] = []
    for rec in parts_inventory():
        sku = str(rec.get("part_sku") or "")
        on_hand = int(rec.get("on_hand") or 0)
        lead = float(rec.get("lead_time_hours") or 0.0)
        issues = [r for r in parts_usage(sku) if _days_ago(r.get("issued_at_utc", "")) <= window]
        consumed = sum(int(r.get("qty") or 0) for r in issues)
        run_rate = round(consumed / window, 4)            # units/day
        if run_rate > 0:
            days_to_stockout = round(on_hand / run_rate, 1)
        else:
            days_to_stockout = None                       # no observed demand
        out.append({
            "part_sku": sku,
            "on_hand": on_hand,
            "lead_time_hours": lead,
            "consumed_in_window": consumed,
            "window_days": window,
            "daily_run_rate": run_rate,
            "days_to_stockout": days_to_stockout,
        })
    return out


def failure_probability(asset_id: str = DEMO_ASSET) -> dict[str, Any]:
    """Deterministic failure-risk score from the condition signature — the
    math the failure-probability severity FLOOR reads. PROD: this is where
    a trained RUL/anomaly model output would land; the floor stays
    model-agnostic because it only consumes the score.

    Combines vibration excess over baseline (ISO 10816 banding), temp
    drift, and trend into a 0..1 probability of failure within 24h.
    """
    r = latest_reading(asset_id)
    if not r:
        return {"asset_id": asset_id, "failure_prob_24h": 0.0,
                "anomaly_score": 0.0, "trend": "flat", "vibration_zone": "A"}

    vib = float(r.get("vibration_rms_mm_s") or 0.0)
    base = float(r.get("vibration_baseline_mm_s") or 1.0) or 1.0
    temp_drift = float(r.get("temp_c") or 0.0) - float(r.get("temp_baseline_c") or 0.0)
    anomaly = float(r.get("anomaly_score") or 0.0)
    trend = str(r.get("trend_3d") or "flat")

    # ISO 10816 vibration severity zones (large machines, rough banding).
    if vib < 4.5:
        zone, zone_risk = "A", 0.05
    elif vib < 7.1:
        zone, zone_risk = "B", 0.25
    elif vib < 11.0:
        zone, zone_risk = "C", 0.55
    else:
        zone, zone_risk = "D", 0.85           # unacceptable — imminent damage

    vib_ratio = min(vib / base, 6.0) / 6.0    # 0..1 excess over baseline
    temp_risk = min(max(temp_drift, 0.0) / 40.0, 1.0)   # 40C drift → full
    trend_bump = 0.15 if trend == "rising" else 0.0

    prob = max(zone_risk, 0.45 * vib_ratio + 0.25 * temp_risk + 0.30 * anomaly) + trend_bump
    prob = round(min(prob, 0.99), 3)
    return {
        "asset_id": asset_id,
        "failure_prob_24h": prob,
        "anomaly_score": anomaly,
        "trend": trend,
        "vibration_zone": zone,
        "vibration_rms_mm_s": vib,
        "temp_drift_c": round(temp_drift, 1),
    }
