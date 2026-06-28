# Predictive Maintenance — PRD

## Story

**Priya**, a reliability engineer at a polymer plant, is the human this
mesh protects from interruption. At 06:14 the plant historian streams a
vibration spike on Extruder-7 into the `sensor_reading` entry point.
`condition_sensor_agent` pulls the vibration / temp / current signature
from the historian via its MCP gateway, sees an anomaly, and hands off
to `failure_predictor_agent`, which scores 18h remaining useful life,
mode `bearing_wear`, confidence 0.62. Because the prediction sits inside
the 24h shutdown window at low confidence, the deterministic safety floor
routes Priya in *and* the work-order pipeline starts.
`work_order_scheduler_agent` classes the job `emergency`, mints a
traceable work order, and finds the replacement bearing's 72h lead time
exceeds the 18h RUL — the lead-time-vs-RUL gate marks parts unavailable
and escalates to Priya instead of dispatching. When the expedited part
arrives, the `parts_arrived` entry point re-runs the pipeline against the
same work order, parts clear, and `technician_human` is dispatched. The
technician marks it `completed`; the flow terminates in the hash-chained
WORM audit. Priya touched it once, at the only step that needed judgement.

## Why it works in an organization

Unplanned downtime and parts shortages are the two most expensive failure
modes in a plant. This mesh turns raw condition data into traceable,
gated work orders, auto-handles routine/corrective work, and forces a
human only on irreversible or safety-critical calls (shutdown-window
failures, parts gaps, replace/overhaul capex, OSHA-reportable incidents).
A 90-day spare-parts forecast keeps capital off the shelf and freight
costs down. The ROI cell is `maint × goods`, internal-facing: avoided
downtime, fewer emergency parts orders, an auditable PM-compliance trail.

## Flow

```
 sensor_reading / scheduled_inspection
        │
        ▼
 condition_sensor_agent ──anomaly──▶ failure_predictor_agent
                                          │
              ┌───────────────────────────┤
              │ <24h & conf<0.7           │ (always)
              ▼                            ▼
   reliability_engineer_human    work_order_scheduler_agent
              │                       │   │   │
              │            dispatched │   │   │ parts_available==false
              │            ==tech     │   │   ▼
              │                       │   │  reliability_engineer_human
              │                       │   │
              ▼                       ▼   ▼ (non-tech, parts ok)
      ops_manager_human       technician_human   maint_audit_logger_agent
              │                       │  │
              ▼                       │  └─completed─▶ maint_audit_logger_agent
   maint_audit_logger_agent          └─escalate─▶ reliability_engineer_human

 weekly_parts_forecast (cron 0 8 * * 1) ─▶ spare_parts_forecaster_agent ─▶ ops_manager_human
 work_order_ingest / asset_history / pm_compliance ─▶ maintenance_records_agent ─▶ engineer / ops_manager
 equipment_lifecycle_assess ─▶ equipment_lifecycle_agent ─▶ ops_manager (replace) / scheduler (overhaul)
 safety_incident_report ─▶ safety_incident_reporter_agent ─▶ ops_manager_human
 shutdown_request ─▶ reliability_engineer_human
```
All human gates are default-deny on timeout; every terminal branch lands
in `maint_audit_logger_agent`.

## Actor inventory

| Agent | Job title | Type |
|---|---|---|
| `condition_sensor_agent` | Condition-monitoring data engineer | programmatic (MCP) |
| `failure_predictor_agent` | Predictive reliability analyst | llm + Python (@pre_compose/@chain) |
| `work_order_scheduler_agent` | Maintenance planner / dispatcher | programmatic + Python (@pre_compose/@chain) |
| `technician_human` | Maintenance technician | human |
| `reliability_engineer_human` | Reliability engineer | human |
| `ops_manager_human` | Operations manager | human |
| `maint_audit_logger_agent` | Compliance records custodian | external/custom |
| `spare_parts_forecaster_agent` | Spare-parts demand planner | llm (cron) |
| `maintenance_records_agent` | Maintenance records analyst | programmatic (MCP) |
| `equipment_lifecycle_agent` | Asset lifecycle planner | llm + Python (@pre_compose/@chain) |
| `safety_incident_reporter_agent` | EHS / safety compliance officer | llm + Python |

## Data artifacts

- **MaintEvent** (`data_structures`): `asset_id`, `asset_type`, `reading`,
  `received_at_utc`.
- **RUL prediction**: `predicted_failure_hours`, `failure_mode`,
  `confidence`, `safety_critical`, `engineer_briefing`.
- **Work order**: `work_order_id`, `window_start_utc`, `work_type`,
  `parts_available`, `lead_time_hours`, `parts_short`, `dispatched_to`,
  `eta_minutes`.
- **Parts forecast**: `parts_needed_{30,60,90}d`, `critical_shortages`,
  `overstock_candidates`, `confidence_overall`.
- **Lifecycle assessment**: `age_pct_of_design_life`,
  `keep_vs_replace_ratio`, `recommended_action`, `requires_capex_approval`.
- **Safety incident**: `severity`, `osha_recordable`,
  `osha_report_window_hours`, `state_filings_required`.
- **Audit entry**: `audit_event_id`, `hash_chain_prev/current` — WORM.

## Per-agent spec

- **`condition_sensor_agent`** — programmatic; `integration: mcp` →
  `${HISTORIAN_MCP_URL}` tool `read_condition_signals`. Yields
  `asset_id`, `sensor_summary`, `anomaly_detected`. Routes to the
  predictor only on `anomaly_detected == true`.
- **`failure_predictor_agent`** — llm + Python module
  (`agency/failure_predictor_agent.py`), `failure_modes` KB. Self-reliant:
  `@pre_compose load_condition` pulls its own telemetry series + asset
  master + recent history + the deterministic failure-probability score
  from the store (PROD: historian / CMMS). `@chain enforce_floors`
  carries two LLM-unwaivable floors — (1) the failure-probability
  severity floor (score ≥ 0.7 → RUL clamped into the 24h window + safety
  forced) and (2) the safety-critical hard-stop (safety-critical asset OR
  low-conf-inside-window → safety forced). The wires fire on post-floor
  values.
- **`work_order_scheduler_agent`** — programmatic + Python actuator
  (`agency/work_order_scheduler_agent.py`). `@pre_compose load_parts`
  pulls its own parts picture (on-hand + lead time, resolved from the
  failure mode) and any open WO from the store. Body: RUL→work-type
  matrix, the parts-availability gate before scheduling (fail-closed
  lead-time-vs-RUL), refuse-untraceable-WO rule. `@chain set_priority`
  is the work-order priority floor (P1/P2/P3, emergency can't be
  down-ranked).
- **`equipment_lifecycle_agent`** — llm + Python module
  (`agency/equipment_lifecycle_agent.py`). `@pre_compose
  load_asset_dossier` pulls the asset master + full maintenance history
  and pre-derives age / lifetime spend / keep-vs-replace ratio. `@chain
  enforce_decision_floor` forces at least an overhaul once the asset is
  past design life and over the replace threshold; capex/planning flags
  are recomputed from the final action.
- **`maintenance_records_agent`** — programmatic; `${CMMS_MCP_URL}` tool
  `get_maintenance_history`, mode-dispatched (ingest_wo / schedule_pm /
  report / pm_compliance / recurrence_sweep).
- **`spare_parts_forecaster_agent`** — llm, weekly cron `0 8 * * 1`,
  `parts_master` KB.
- **`safety_incident_reporter_agent`** — llm + Python OSHA tables
  (`agency/safety_incident_reporter_agent.py`): 8h/24h/7-day windows.
- **Humans** (`technician`/`reliability_engineer`/`ops_manager`) —
  default-deny `fallback_on_timeout`, escalating chain.
- **`maint_audit_logger_agent`** — external/custom, registered as a
  mesh intelligence in `main.py`; hash-chained WORM sink.

## Entry points

16 total: `sensor_reading`, `work_order_filed`, `scheduled_inspection`,
`parts_arrived`, `shutdown_request`, `work_order_ingest`,
`asset_history_query`, `pm_compliance_report`, `weekly_parts_forecast`,
`shutdown_parts_plan`, `emergency_parts_request`, plus direct-call
addressable points for the predictor, scheduler, records, lifecycle and
safety reporter.

## Controls / trust (deterministic floors)

- Failure-probability severity floor (predictor `@chain`): score ≥ 0.7
  clamps RUL into the 24h window + forces safety — LLM can't waive.
- Safety-critical hard-stop (predictor `@chain`): safety-critical asset
  OR low-conf-inside-window forces safety + engineer route.
- Parts-availability gate (scheduler): fail-closed, no dispatch when the
  part is short or lead time > RUL.
- Work-order priority floor (scheduler `@chain`): P1/P2/P3 deterministic.
- Lifecycle decision floor (lifecycle `@chain`): past-life + over the
  replace threshold forces at least overhaul.
- Traceability (scheduler): refuses to mint an untraceable work order.
- OSHA windows (safety reporter): statutory data in Python, not LLM.
- Human gates: default-deny on timeout, escalating chain.
- WORM audit: hash-chained, terminal on every branch.

## Dev store (day-0, no connectors)

`agency/_shared/store.py` stands in for the historian / CMMS / parts
master so the FULL chain runs end-to-end before any connector is wired.
Seeded with a realistic safety-critical asset (`extruder-7`, past design
life, vibration in ISO 10816 zone D), an anomalous reading + telemetry
series, a parts inventory where the replacement bearing is out of stock
(72h lead > RUL), recurring corrective records (heavy lifetime spend),
and an open work order for `parts_arrived` re-entry. It also holds the
deterministic domain math the floors read: `failure_probability()` (the
RUL/anomaly risk score) and `parts_availability()` (the parts gate's
resolution). PROD swaps each read for the matching MCP connector; the
function signatures are the contract. `agency/_shared/session_stash.py`
bounds the retry back-edges. The promoted self-reliant agents fall back
to these store reads when `@pre_compose` did not inject (e.g. a bare
direct call), so the floors hold regardless of how the agent is invoked.

## Config pointer

The department is `configs/config.yaml`. Python exists for the actuator
+ self-reliant agents (`agency/work_order_scheduler_agent.py`,
`agency/failure_predictor_agent.py`, `agency/equipment_lifecycle_agent.py`),
the OSHA tables (`agency/safety_incident_reporter_agent.py`), the dev
store + session stash (`agency/_shared/store.py`,
`agency/_shared/session_stash.py`), and the WORM sink
(`agency/_shared/audit_logger.py`). Validate with
`python validate_config.py`.

## Reflection / decisions

- **Self-reliant agents over conductor spoon-feeding** (predictor,
  scheduler, lifecycle): each `@pre_compose`s its own context from the
  systems of record and `@chain`s a floor the LLM can't waive. The
  forecaster stays pure-YAML (it has no floor to enforce — procurement
  reviews its output).
- **Floors are deterministic, not prompt-hoped**: the failure-prob
  severity floor, safety hard-stop, parts gate, priority floor, and
  lifecycle decision floor all live in Python and fire regardless of LLM
  optimism. The wires fire on post-floor values; belt-and-braces.
- **Kept Python only where determinism is non-negotiable**: the
  work-order actuator (fail-closed gates), OSHA statutory windows, the
  failure-risk + parts math, and the three floors.
- **State stays in the customer's systems** (historian, CMMS, parts
  master) via MCP connectors; the template ships a *dev store* stub
  (`agency/_shared/store.py`) so the demo runs day-0, plus the WORM
  audit file. Neither is a system of record.
- **Slack tokens are not consumed**: `SLACK_BOT_TOKEN` /
  `SLACK_SIGNING_SECRET` belong to a `channels:` block, not the
  escalation target; this config has no `channels:` block, so they ship
  commented out in `env`.
