# Predictive-Maintenance Pod — PRD

A pod (coordinator + specialists + a finisher + human members + the Manager
supervisor) that runs an asset from a condition signal to an issued, audited work
order. Not a pipeline: every wire is conditional, every human gate default-denies,
the finisher produces the real artifact and fails closed, and the WORM sink is the
terminus. Systems (historian / CMMS / ERP-MRP) are TOOLS inside agents, never
members.

## Story

**Priya**, a reliability engineer at a polymer plant, is the human this pod
protects from interruption. At 06:14 the historian streams a vibration spike on
Extruder-7 into `sensor_reading`. **Mira** (coordinator) opens the case and hands
it to **Rex** (reliability analyst), who pulls the telemetry series and the
deterministic failure-probability score with his tools. Because the vibration
sits in ISO 10816 zone D, Rex's `@chain` safety floors clamp RUL into the 24h
window and force `safety_critical`. Two wires fire: Priya is routed in, and
**Wanda** (planner) begins the work order — `emergency`, traceable id, and the
parts gate holds (bearing 72h lead > RUL), escalating to Priya rather than
issuing. She authorises an expedited part; on `parts_arrived` the plan re-runs
against the same work order, parts clear, and **Ivan** (issuer/finisher)
re-checks Priya's authorisation, renders the pack to `./out`, ledgers it, and
dispatches **the technician**. Completed → hash-chained WORM audit. Priya touched
it once.

## Why it works in an organization

Unplanned downtime and parts shortages are the two most expensive failure modes
in a plant. This pod turns raw condition data into traceable, gated work orders,
auto-handles routine/corrective work, and forces a human only on irreversible or
safety-critical calls. A 90-day spare-parts forecast keeps capital off the shelf
and freight costs down. ROI cell: `maint × goods`, internal-facing — avoided
downtime, fewer emergency parts orders, an auditable PM-compliance trail.

## Pod shape

- **Coordinator** — `mira_coordinator_agent` (owns the case, orchestrates, keeps
  status; never does specialist work).
- **Specialists** — `condition_sensor_agent` (reads/flags), `failure_predictor_agent`
  (RUL + safety floors), `work_order_scheduler_agent` (plan + parts gate + priority),
  `spare_parts_forecaster_agent`, `equipment_lifecycle_agent`,
  `safety_incident_reporter_agent`, `maintenance_records_agent`.
- **Finisher** — `work_order_issuer_agent` (behind the human gate: auth re-check →
  invariants → render pack → delete-on-block → dispatch → ledger).
- **Human members** — reliability engineer, technician, ops manager (first-class,
  webhook/dual, default-deny).
- **Supervisor** — the LeafMesh Manager (static routing; escalates stalls / errors /
  timeouts to the ops manager).
- **WORM sink** — `maint_audit_logger_agent` (hash-chained, terminal on every branch).

## Flow

```
 sensor_reading / case_status ─▶ mira_coordinator_agent
        │ action==open_case                         │ action==escalate
        ▼                                           ▼
 failure_predictor_agent                    reliability_engineer_human
   │  (safety floors @chain)                       │ approved        │ held
   │ predicted_failure_hours>=0                    ▼                 ▼
   │                          ┌──── work_order_issuer_agent    maint_audit_logger_agent
   │ safety/low-conf          │        │ dispatched_to==technician
   ▼                          │        ▼
 reliability_engineer_human   │   technician_human
        ▲                     │        │ parts_issue (bounded)  │ completed/held
        │ review/gap/exhausted │        ▼                        ▼
 work_order_scheduler_agent ◀─┴── (re-work)              maint_audit_logger_agent
   │ parts_available && !review && !exhausted ─▶ work_order_issuer_agent

 scheduled_inspection (cron 0 6, tz) ─▶ condition_sensor_agent (instances:3, atomic claim) ─anomaly─▶ Rex
 plant_iiot MQTT stream (listen_events) ─▶ condition_sensor_agent
 weekly_parts_forecast (cron 0 8 mon, tz) ─▶ spare_parts_forecaster_agent ─▶ ops_manager (shortages) / audit
 equipment_lifecycle_assess ─▶ equipment_lifecycle_agent ─▶ ops_manager (replace) / scheduler (overhaul)
 safety_incident_report ─▶ safety_incident_reporter_agent ─▶ ops_manager_human
 work_order_ingest / asset_history / pm_compliance ─▶ maintenance_records_agent ─▶ engineer / ops_manager
 shutdown_request / engineer_reply ─▶ reliability_engineer_human
```

Human-to-human escalation is the Manager supervisor's job (no dual→dual human
wires). All human gates default-deny to `held_for_review`; every terminal branch
lands in `maint_audit_logger_agent`.

## Modern capabilities (SDK 2.4.131)

- **`instances: 3` + atomic claim** (`condition_sensor_agent`) — a fleet sweep /
  stream fans into 3 parallel copies, each `store.claim_next_asset()` (also the
  `claim_next_asset` global tool) pops a disjoint asset. `instances_handoff: each`
  so each anomalous asset routes its own prediction.
- **`listen_events`** (`condition_sensor_agent`) — binds an MQTT condition stream
  via the top-level `plant_iiot` broker; inert until `IIOT_MQTT_HOST`.
- **`timezone`** — on both live crons (Cass daily, Perry weekly) so they fire in
  plant-local time, not UTC.
- **`super_agent` (dict form)** (`failure_predictor_agent`) — plan→execute→verify
  over telemetry; `cost_ceiling` + `wall_clock` tuned via the dict (the only path).
- **`playbook`** (Rex, Perry, Ellis, Sana) — craft in `responsibility`, loaded on
  demand; only `description` in the prompt.
- **`mcp` (commented)** — the historian block on Cass ships commented (empty-URL
  http mcp is rejected at load); day-0 dev tools stand in and announce
  `available:false`.

## Actor inventory

| Agent | Job title | Type |
|---|---|---|
| `mira_coordinator_agent` | Maintenance coordinator | llm |
| `condition_sensor_agent` | Condition-monitoring engineer | programmatic + module (instances / listen_events) |
| `failure_predictor_agent` | Predictive reliability analyst | llm + super_agent + module (@chain floors) |
| `work_order_scheduler_agent` | Maintenance planner | programmatic + module (@pre_compose / @chain) |
| `work_order_issuer_agent` | Work-order issuer (finisher) | programmatic + module (@chain issuance floor) |
| `reliability_engineer_human` | Reliability engineer | human (webhook/dual) |
| `technician_human` | Maintenance technician | human (webhook/dual) |
| `ops_manager_human` | Operations manager | human (webhook/dual) |
| `maint_audit_logger_agent` | Compliance records custodian | external/custom |
| `spare_parts_forecaster_agent` | Spare-parts demand planner | llm + module (cron + tz) |
| `maintenance_records_agent` | Maintenance records analyst | programmatic (MCP connector) |
| `equipment_lifecycle_agent` | Asset lifecycle planner | llm + module (@pre_compose / @chain) |
| `safety_incident_reporter_agent` | EHS / safety compliance officer | llm + module (OSHA tables) |

## Data artifacts

- **MaintEvent** (`data_structures`): `asset_id`, `asset_type`, `reading`, `received_at_utc`.
- **RUL prediction**: `predicted_failure_hours`, `failure_mode`, `confidence`,
  `safety_critical`, `failure_prob_24h`, `engineer_briefing`.
- **Work-order plan**: `work_order_id`, `window_start_utc`, `work_type`, `priority`,
  `parts_available`, `lead_time_hours`, `parts_short`, `rework_rounds`, `rework_exhausted`.
- **Issued work order**: `issued`, `authorized`, `work_order_id`, `pack_path`,
  `dispatched_to`, `status`, `block_reasons`.
- **Parts forecast**: `parts_needed_{30,60,90}d`, `critical_shortages`,
  `overstock_candidates`, `confidence_overall`.
- **Lifecycle assessment**: `age_pct_of_design_life`, `keep_vs_replace_ratio`,
  `recommended_action`, `requires_capex_approval`.
- **Safety incident**: `severity`, `osha_recordable`, `osha_report_window_hours`,
  `state_filings_required`.
- **Audit entry**: `audit_event_id`, `hash_chain_prev/current` — WORM.

## Per-agent spec

- **`mira_coordinator_agent`** — llm; pure orchestration. Opens the case on a
  reading, answers status from the record, routes exceptions. → predictor
  (open_case) / engineer (escalate).
- **`condition_sensor_agent`** — programmatic + `agency/condition_sensor_agent.py`.
  Each instance atomically claims an asset, reads `store.read_condition_signals`
  (announces `available:false` with no source), flags anomalies. → predictor on
  `anomaly_detected == true`. Live `wake_up` (tz) + `listen_events`.
- **`failure_predictor_agent`** — llm + super_agent + `agency/failure_predictor_agent.py`.
  Pulls telemetry via tools. `@chain enforce_floors`: (1) failure-probability
  severity floor (score ≥ 0.7 → clamp RUL + force safety); (2) safety-critical
  hard-stop (safety-critical asset OR low-conf-inside-window). Re-derives the score
  from the store — never trusts the model. → scheduler / engineer.
- **`work_order_scheduler_agent`** — programmatic + `agency/work_order_scheduler_agent.py`.
  `@pre_compose load_parts`; body: RUL→work-type matrix, parts-availability gate
  (fail-closed lead-time-vs-RUL), refuse-untraceable-WO, bounded re-work counter
  (session stash → `rework_exhausted` at the cap); `@chain set_priority`. Stashes
  the plan for the issuer. → issuer (routine) / engineer (review/gap/exhausted).
- **`work_order_issuer_agent`** — programmatic FINISHER + `agency/work_order_issuer_agent.py`.
  Recovers the plan across the HITL gap (upstream → session stash → pending-WO
  store). `@chain enforce_issuance_floor`: allow/deny tokens (unknown fails closed
  on a job needing auth; routine auto-authorised), invariants (traceable WO + parts
  on hand), render pack to `./out`, DELETE on block, record outbox + ledger,
  dispatch. → technician (dispatched) / audit (always).
- **`spare_parts_forecaster_agent`** — llm + `agency/spare_parts_forecaster_agent.py`,
  weekly cron `0 8 * * 1` (tz). `@pre_compose load_demand_signals`;
  `@chain enforce_stockout_floor` forces a known stockout into critical_shortages.
  → ops_manager (shortages) / audit.
- **`equipment_lifecycle_agent`** — llm + `agency/equipment_lifecycle_agent.py`.
  `@pre_compose load_asset_dossier`; `@chain enforce_decision_floor` forces at least
  overhaul past design life + over threshold. → ops_manager (replace) / scheduler (overhaul).
- **`safety_incident_reporter_agent`** — llm + `agency/safety_incident_reporter_agent.py`
  (OSHA 8h/24h/7-day tables). `@pre_compose load_incident_context`;
  `@chain enforce_severity_floor` maps outcome → mandated severity/route. → ops_manager.
- **`maintenance_records_agent`** — programmatic; `${CMMS_MCP_URL}` tool
  `get_maintenance_history`, mode-dispatched. Inert until wired. → engineer / ops_manager.
- **Humans** — webhook/dual, `fallback_on_timeout` → `held_for_review` → audit.
- **`maint_audit_logger_agent`** — external/custom, registered via
  `sdk.intelligence("maint_audit_logger_agent")` in `main.py`; hash-chained WORM.

## Entry points

17: `sensor_reading`, `case_status`, `scheduled_inspection`, `work_order_filed`,
`parts_arrived`, `shutdown_request`, `engineer_reply`, `work_order_ingest`,
`asset_history_query`, `pm_compliance_report`, `weekly_parts_forecast`,
`shutdown_parts_plan`, `emergency_parts_request`, `equipment_lifecycle_assess`,
`safety_incident_report`, plus direct-call points for the predictor and the issuer.

## Controls / trust (deterministic floors)

- Failure-probability severity floor + safety-critical hard-stop (predictor `@chain`).
- Parts-availability gate (scheduler): fail-closed lead-time-vs-RUL, no issue on a gap.
- Work-order priority floor (scheduler `@chain`): P1/P2/P3, emergency can't down-rank.
- Issuance floor (issuer `@chain`): explicit auth (unknown fails closed) + parts + a
  traceable WO, or block and delete the partial.
- Lifecycle decision floor + OSHA statutory windows (Python, not LLM).
- Traceability (scheduler): refuses an untraceable work order.
- Bounded re-work back-edge (technician `parts_issue` → planner, counter → engineer).
- Human gates: default-deny on timeout; Manager supervisor escalates human→human.
- WORM audit: hash-chained, terminal on every branch.

## Dev store (day-0, no connectors)

`agency/_shared/store.py` stands in for the historian / CMMS / parts master so the
FULL chain runs end-to-end before any connector is wired. Seeded with a
safety-critical asset (`extruder-7`, past design life, ISO 10816 zone D), a nominal
pump + conveyor (so `instances: 3` claims three disjoint assets per sweep), an
anomalous reading + telemetry series, a parts inventory where the bearing is out of
stock (72h lead > RUL), recurring corrective records, and an open work order for
`parts_arrived` re-entry. It holds the domain math the floors read
(`failure_probability()`, `parts_availability()`, `parts_demand_signals()`), the
atomic fleet backlog (`claim_next_asset()`), and the issuer's outbox + ledger +
pending-WO recovery. `agency/tools.py` is the day-0 `@global_tool` surface; each
tool announces `available:false` with no real source. `agency/_shared/session_stash.py`
carries the bounded-loop counter and the HITL recovery stash. PROD swaps each store
read for the matching MCP connector; the function signatures are the contract.

## Config pointer

The department is `configs/config.yaml`. Python exists for the reader, the actuator
+ finisher, the self-reliant LLMs, the OSHA tables, the dev store + tools + session
stash, and the WORM sink. Validate with `python validate_config.py`; unit-test the
floors + finisher with `python -m pytest`.

## Reflection / decisions

- **Coordinator + finisher added** to make this a pod, not a pipeline: Mira owns
  the case; Ivan actuates behind the human gate (auth re-check, render, delete-on-
  block) — the planner no longer dispatches (reader ≠ assembler ≠ actuator).
- **Playbook replaced the mongo `knowledge:` blocks** — the craft is loaded on
  demand (per-agent registry requires ≥2.4.130), keeping the template core-only.
- **Floors are deterministic, re-derived from the store**, not prompt-hoped; wires
  fire on post-floor values (belt-and-braces).
- **State stays in the customer's systems** via connectors; the dev store + WORM
  file are dev stand-ins, never systems of record.
- **No dual→dual human edges** — the SDK warns on them; human→human escalation is
  the Manager supervisor's job.
