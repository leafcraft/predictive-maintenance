# predictive-maintenance — Predictive Maintenance

Condition sense → failure predict → work order (schedule → parts →
dispatch) → repair. The `maint × goods` ROI cell, internal-facing.
**YAML-first:** the mesh is defined in `configs/config.yaml`; your data
stays in your systems of record (plant historian + CMMS) and is reached
through connectors — no template storage as the system of record.

**Self-reliant agents, deterministic floors.** The reliability analyst,
the work-order planner, and the lifecycle planner each pull their *own*
context (`@pre_compose`) from the systems of record and enforce floors
the LLM **cannot waive** (`@chain`): the failure-probability severity
floor, the safety-critical hard-stop, the parts-availability gate before
scheduling, and the work-order priority floor. A seeded dev store
(`agency/_shared/store.py`) makes the **full chain run end-to-end with no
connectors set** — see *Run it day-0* below.

## Story

**Priya**, a reliability engineer at a polymer plant, never opens this
mesh — it works around her until it needs her judgement. At 06:14 the
plant historian streams a vibration spike on Extruder-7 into the
`sensor_reading` entry point. `condition_sensor_agent` pulls the
vibration / temp / current signature from the historian (via the MCP
gateway) and, seeing an anomaly, hands off to `failure_predictor_agent`.
The predictor scores **18 hours** of remaining useful life, failure mode
`bearing_wear`, confidence `0.62`, and writes an engineer briefing.
Because the prediction sits inside the 24-hour shutdown window at low
confidence, two wires fire at once: the deterministic safety floor routes
Priya in for review, and the work order pipeline begins.

`work_order_scheduler_agent` — the fail-closed actuator — classifies the
job `emergency`, mints a traceable `work_order_id`, and checks parts. The
replacement bearing's lead time (72h) exceeds the 18h RUL, so the
lead-time-vs-RUL gate marks parts unavailable and refuses to dispatch a
technician; instead the `parts_available == false` wire escalates to
`reliability_engineer_human`. Priya gets a single task with the briefing,
the work order, and the parts gap. She authorises a temporary load
reduction and an expedited part. When the part lands, the `parts_arrived`
entry point re-runs the WO pipeline against the *same* work order id,
parts now clear, and `technician_human` is dispatched. The technician
marks the job `completed`; the terminal hop lands in
`maint_audit_logger_agent`, a hash-chained WORM record. Priya touched the
flow once, at the one moment that needed a human.

## Why it works in an organization

Unplanned downtime is the most expensive failure mode in a plant: a
single line stoppage can cost more than a year of sensors. This mesh
converts raw condition data into *traceable, gated* work orders and
front-runs the two things that actually cause unplanned downtime — late
failure detection and parts shortages. It deflects the routine
(planned/corrective work auto-schedules and dispatches), forces a human
only on the irreversible or safety-critical decisions (shutdown-window
failures, lead-time gaps, replace/overhaul capex, OSHA-reportable
incidents), and gives procurement a 90-day parts forecast so capital
isn't tied up in overstock or lost to stockouts. Every terminal path is
WORM-audited, so the reliability team can prove *why* each decision was
made. The ROI cell is `maint × goods`, internal-facing: the payoff is
avoided downtime, fewer emergency-freight parts orders, and an auditable
PM-compliance trail.

## Configuration (three tiers)

**Tier 1 — Boot (required to start the SDK at all):**

| Var | Required | Purpose |
|---|---|---|
| `LEAFMESH_LICENSE_KEY` | yes | starts the SDK |
| `LEAFMESH_ENV_TOKEN` | yes | per-environment telemetry token (dev/staging/prod) |
| `OPENAI_API_KEY` | yes | every LLM agent runs `gpt-4o-mini` |

**Tier 2 — Host prerequisites + system-of-record adapters:**

| Var | Required | Purpose |
|---|---|---|
| `REDIS_HOST` / `REDIS_PORT` / `REDIS_PASSWORD` | optional | session + auto-storage; defaults to `localhost:6379`, no auth |
| `HISTORIAN_MCP_URL` | optional* | MCP gateway to your plant historian / IoT platform (PI, Ignition, ThingWorx); `condition_sensor_agent` calls its `read_condition_signals` tool |
| `CMMS_MCP_URL` | optional* | MCP gateway to your CMMS (Maximo, Fiix, eMaint); `maintenance_records_agent` calls `get_maintenance_history` |
| `ANTHROPIC_API_KEY` | optional | only if you enable the built-in summarizer (`claude-haiku`) |

\* The mesh boots without these, but `condition_sensor_agent` and
`maintenance_records_agent` return nothing useful until their gateway is
wired. The sensor and CMMS-sweep `wake_up` crons ship **commented out**
for exactly this reason — enable them after the connectors are live.

**Tier 3 — Connectors / escalation egress (optional):**

| Var | Required | Purpose |
|---|---|---|
| `PAGER_WEBHOOK` | optional | manager-escalation webhook target (defaults to a PagerDuty enqueue URL) |
| `SLACK_MAINT_CHANNEL` | optional | `channel_id` for the manager-escalation `provider: slack` target's `message_template` |
| `SLACK_BOT_TOKEN` / `SLACK_SIGNING_SECRET` | **not auto-consumed** | these are fields of a `channels: {slack: {...}}` block (`ChannelConfig`), NOT of the escalation target. There is no `channels:` block in this config, so they are unused; the escalation target reads only `channel_id` + `message_template`. They ship commented out in `env` |

## Agents

| Agent | Role | Responsibilities | Can perform |
|---|---|---|---|
| `condition_sensor_agent` | Condition-monitoring data engineer | Pulls vibration/temp/pressure/current for an asset from the plant historian; flags anomalies | `programmatic` + MCP (`read_condition_signals`); on anomaly → `failure_predictor_agent` |
| `failure_predictor_agent` | Predictive reliability analyst | `@pre_compose` pulls its own telemetry series + asset history + failure-prob score; scores RUL/mode/confidence; `@chain` enforces the failure-probability severity floor + safety-critical hard-stop (LLM-unwaivable) | `llm` (`gpt-4o-mini`) + Python module, `failure_modes` KB; → `work_order_scheduler_agent`; forced-safety / low-conf+<24h → `reliability_engineer_human` |
| `work_order_scheduler_agent` | Maintenance planner / dispatcher | `@pre_compose` pulls its own parts picture (on-hand + lead time, resolved from failure mode) + open WO; picks window + work type, runs the parts-availability gate (fail-closed lead-time-vs-RUL), refuses untraceable WOs, dispatches; `@chain` sets the work-order priority floor (P1/P2/P3) | `programmatic` + Python actuator; → `technician_human` / `reliability_engineer_human` (parts gap) / `maint_audit_logger_agent` |
| `technician_human` | Maintenance technician | Performs the repair; reports completed / parts_issue / escalate | `human` (2h timeout, default-deny → engineer); → `reliability_engineer_human` / `maint_audit_logger_agent` |
| `reliability_engineer_human` | Reliability engineer | Reviews shutdown / unscheduled / parts-gap decisions; directs corrective action | `human` (4h timeout, default-deny → ops manager); → `ops_manager_human` / `maint_audit_logger_agent` |
| `ops_manager_human` | Operations manager | Production-impact tier — capex sign-off, safety escalations, held flows | `human` (6h timeout, default-deny → hold); → `maint_audit_logger_agent` |
| `maint_audit_logger_agent` | Compliance records custodian | Writes the hash-chained WORM audit entry for every terminal path | `external`/`custom` (registered in `main.py`); terminal — no outbound edges |
| `spare_parts_forecaster_agent` | Spare-parts demand planner | Forecasts 30/60/90-day parts demand from fleet RUL trends; flags critical shortages + overstock for procurement | `llm`, `parts_master` KB, weekly cron `0 8 * * 1`; → `ops_manager_human` |
| `maintenance_records_agent` | Maintenance records analyst | WO ledger/MTBF, PM-compliance tracking, daily recurrence sweep (mode-dispatched in your CMMS) | `programmatic` + MCP (`get_maintenance_history`); → `reliability_engineer_human` (high-freq / recurrent) / `ops_manager_human` (compliance) |
| `equipment_lifecycle_agent` | Asset lifecycle planner | `@pre_compose` pulls the asset dossier (master + full maintenance history → age, lifetime spend, ratio); age-vs-design-life + cost-to-keep-vs-replace scoring; `@chain` enforces the decision floor (past-life + over-threshold → at least overhaul) | `llm` + Python module, `asset_master` KB; replace → `ops_manager_human` (capex); overhaul → `work_order_scheduler_agent` |
| `safety_incident_reporter_agent` | EHS / safety compliance officer | Classifies a safety incident; maps it to OSHA 29 CFR 1904.39 reporting windows + state filings | `llm` + Python OSHA tables, `osha_taxonomy` KB; serious/recordable → `ops_manager_human` |

## Trust — the deterministic floors (LLM-unwaivable)

- **Failure-probability severity floor** (`failure_predictor`, `@chain`):
  when the model-agnostic risk score says failure within 24h is likely
  (≥ 0.7), the RUL is clamped into the shutdown window and
  `safety_critical` is forced — an optimistic LLM can't talk it down.
- **Safety-critical hard-stop** (`failure_predictor`, `@chain`): a
  safety-critical asset OR a low-confidence prediction inside the 24h
  window forces `safety_critical` and routes the reliability engineer.
- **Parts-availability gate** (`work_order_scheduler`, before dispatch):
  a part not on hand, or whose lead time arrives after the predicted
  failure, is unavailable → no dispatch, fail-closed to the engineer.
- **Work-order priority floor** (`work_order_scheduler`, `@chain`): P1
  emergency / P2 corrective / P3 planned, derived deterministically — an
  emergency can't be down-ranked.
- **Lifecycle decision floor** (`equipment_lifecycle`, `@chain`):
  past-design-life + over the replace threshold forces at least overhaul.
- **OSHA windows** (`safety_incident_reporter`): 8h / 24h / 7-day
  statutory deadlines are Python tables, never hallucinated.

Plus: recurrent assets (3+ unscheduled WOs in 30d) escalate; every human
gate is **default-deny on timeout** (technician → engineer → ops manager
→ hold); every terminal path lands in the hash-chained WORM audit.

## Run it day-0 (no connectors)

The seeded dev store (`agency/_shared/store.py`) ships a realistic asset
(`extruder-7`, past design life, safety-critical, vibration in ISO 10816
zone D), an anomalous reading, and a parts inventory where the
replacement bearing's 72h lead time exceeds the RUL — so the failure
floor, the safety hard-stop, and the parts gate all fire on the demo
asset before any connector is wired.

```bash
cp env .env && pip install -r requirements.txt
python validate_config.py            # pre-flight
python examples/run_examples.py 02_anomaly_failure_predicted   # the demo flow
python main.py                       # boot the mesh (HITL waits for operators)
```

Or call the self-reliant agents directly (no mesh, no connectors):

```python
import asyncio
from agency.failure_predictor_agent import failure_predictor_agent
from agency.work_order_scheduler_agent import work_order_scheduler_agent

async def demo():
    # LLM is optimistic; the failure-probability floor + safety hard-stop
    # override it for the seeded safety-critical asset.
    pred = await failure_predictor_agent(
        {"predicted_failure_hours": 300, "confidence": 0.95,
         "failure_mode": "bearing_wear"},
        {"asset_id": "extruder-7"}, {})
    print(pred["predicted_failure_hours"], pred["safety_critical"])  # → 12.0 True

    # Parts gate holds: the bearing is out of stock (72h lead).
    wo = await work_order_scheduler_agent(
        None, {"predicted_failure_hours": pred["predicted_failure_hours"],
               "safety_critical": pred["safety_critical"],
               "failure_mode": "bearing_wear"}, {})
    print(wo["work_type"], wo["priority"], wo["parts_available"])    # → emergency P1 False

asyncio.run(demo())
```
