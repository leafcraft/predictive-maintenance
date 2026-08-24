# predictive-maintenance — Predictive-Maintenance Pod

A pod of digital employees + human roles that runs an asset from a **condition
signal → RUL prediction (deterministic safety floors) → work-order plan (parts
gate) → reliability-engineer review → an ISSUED, audited work order → a dispatched
technician** — with a human on every irreversible gate and the systems (plant
historian / CMMS / ERP-MRP) as tools inside the agents, never members.

**Read `PRD.md`** for the full agent-by-agent spec. This is a **pod-standard
reference** (SDK 2.4.131): a team of self-reliant agents, not a pipeline.

## Story

**Priya**, a reliability engineer at a polymer plant, never opens this pod — it
works around her until it needs her judgement. At 06:14 the plant historian
streams a vibration spike on **Extruder-7** into the `sensor_reading` entry point.
**Mira**, the coordinator, opens the case and hands it to **Rex**, the reliability
analyst. Rex pulls the telemetry series and the deterministic failure-probability
score with his tools, and — because the vibration sits in ISO 10816 zone D — his
**safety floors** clamp remaining useful life into the 24-hour shutdown window and
force `safety_critical`, whatever the model's first estimate was. Two wires fire
at once: Priya is routed in for review, and **Wanda**, the planner, begins the
work order.

Wanda classifies the job `emergency`, mints a traceable `work_order_id`, and runs
the parts gate. The replacement bearing's 72-hour lead time exceeds the RUL, so
the lead-time-vs-RUL gate marks parts unavailable and refuses to hand the job to
the issuer; the `parts_available == false` wire escalates to Priya instead. She
gets one task with the briefing, the work order, and the parts gap. She authorises
a temporary load reduction and an expedited part. When the part lands, the
`parts_arrived` entry re-runs the plan against the *same* work order id; parts now
clear, and **Ivan**, the work-order issuer, re-checks that Priya actually
authorised the job (an unknown verdict fails closed), renders the approved
work-order pack to `./out`, records the ledger, and dispatches **the technician**.
The technician marks it `completed`; the terminal hop lands in the hash-chained
WORM audit. Priya touched the flow once, at the one moment that needed a human.

## Why it works in an organization

Unplanned downtime is the most expensive failure mode in a plant: a single line
stoppage can cost more than a year of sensors. This pod converts raw condition
data into *traceable, gated* work orders and front-runs the two things that
actually cause unplanned downtime — late failure detection and parts shortages.
It wins reliability the only honest way: **Rex never talks himself out of a safety
call** — a low-confidence prediction inside the shutdown window, or a high
deterministic risk score, forces the safety route in code, not in a prompt.
**Wanda refuses to issue into a parts gap** and refuses to mint an untraceable
work order. **Ivan re-checks that a human actually authorised** before it issues
anything, and **deletes a half-issued pack** if its floor trips. **Silence never
advances a work order** — every human gate default-denies to `held_for_review`.
Routine work (planned/corrective with parts on hand) auto-issues and dispatches;
a human is forced only on the irreversible or safety-critical decisions. Every
terminal path lands in a hash-chained WORM ledger, so the reliability team can
prove *why* each decision was made. ROI cell: `maint × goods`, internal-facing —
avoided downtime, fewer emergency-freight parts orders, an auditable PM trail.

## Modern capabilities (SDK 2.4.131)

- **`instances` (asset fan-out)** — Cass runs as **3 parallel copies** per fleet
  sweep, each ATOMICALLY CLAIMING a disjoint asset (`claim_next_asset`) so copies
  split the fleet instead of duplicating work.
- **`listen_events` (IIoT stream)** — Cass binds a live MQTT condition stream via
  the top-level `plant_iiot` broker; inert until `IIOT_MQTT_HOST` is set.
- **`timezone`** — every live `wake_up` (Cass's daily sweep, Perry's weekly
  forecast) sets a `timezone` so it fires in plant-local time, not UTC.
- **`super_agent` (dict form)** — Rex runs the plan→execute→verify orchestrator
  over the telemetry, pulling its own data via tools.
- **`playbook`** — the craft (RUL reading, safety policy, demand forecast,
  lifecycle decision order, OSHA taxonomy) lives in `responsibility`, loaded on
  demand; only the short `description` rides in the prompt.
- **`mcp` (commented)** — the historian block ships commented (the SDK rejects an
  empty-URL http mcp at load); day-0 the dev-store tools stand in and announce
  `available:false` when there is no real source.

## Prerequisites — what to set, in three tiers

Everything is set in `.env` (`cp env .env`). The **whole chain (sense → predict →
plan → issue) runs on Tier 1 alone** against the seed store (`agency/_shared/
store.py`); condition reads, the IIoT stream, and CMMS use dev stand-ins that
**announce themselves** until you wire Tiers 2–3.

### Tier 1 — required to boot (dev mode)
| Setting | For | Notes |
|---|---|---|
| `LEAFMESH_LICENSE_KEY` | LeafMesh runtime | **Required.** |
| `LEAFMESH_ENV_TOKEN` | Per-environment telemetry | **Required.** |
| `OPENAI_API_KEY` | Every LLM agent (`gpt-4o-mini`) | **Required.** |
| `REDIS_HOST` / `PORT` / `PASSWORD` | State store | Defaults to `localhost:6379`. `docker run -d -p 6379:6379 redis:7-alpine`. |
| `HITL_OUTBOUND_URL` | Where human members are notified | Defaults to a local stub. |

### Tier 2 — real systems of record (set the URL; connectors are inert until then)
| Setting | Enables |
|---|---|
| `HISTORIAN_MCP_URL` / `IIOT_MQTT_HOST` | Live condition reads + the IIoT stream (uncomment the `mcp` block) |
| `CMMS_MCP_URL` | Live CMMS WO ledger / PM-compliance / recurrence sweep |

> ⚠️ The SDK rejects an `mcp` block with an **empty** URL at load, so the
> historian block ships **commented** in `configs/config.yaml`. Fill the URL,
> then uncomment. The `plant_iiot` broker is inert until `IIOT_MQTT_HOST` is set.

### Tier 3 — escalation egress (optional)
| Setting | Purpose |
|---|---|
| `PAGER_WEBHOOK` | Manager-escalation webhook (defaults to a PagerDuty enqueue URL) |
| `SLACK_MAINT_CHANNEL` | `channel_id` for the manager-escalation Slack target |

## The team (13 members)

| Agent | Role (human job title) | Responsibilities | Can perform |
|---|---|---|---|
| `mira_coordinator_agent` | Maintenance Coordinator | Owns the case end to end; opens it, keeps live status, is the reliability team's one contact | LLM; orchestrates Rex→Wanda→Ivan; routes exceptions to the engineer; **never reads a sensor or approves** |
| `condition_sensor_agent` | Condition-Monitoring Engineer | Reads vibration/temp/current per asset, flags anomalies | Programmatic + **instances** (atomic-claim fleet fan-out) + **listen_events** (IIoT) + dev-store tools; on anomaly → Rex |
| `failure_predictor_agent` | Predictive Reliability Analyst | Scores RUL, failure mode, confidence; writes the engineer briefing | LLM + **super_agent** + **playbook** + telemetry tools; `@chain` **safety floors** (fail closed); → Wanda / engineer |
| `work_order_scheduler_agent` | Maintenance Planner | Picks window + work type, runs the parts gate, sets priority; bounds re-work | Programmatic; `@pre_compose` parts + `@chain` priority floor; → issuer (routine) / engineer (review/gap) |
| `work_order_issuer_agent` | Work-Order Issuer | Issues the approved pack, dispatches the technician, records the ledger | Programmatic **finisher**; auth re-check → invariants → render pack → **delete-on-block** → dispatch |
| `reliability_engineer_human` | Reliability engineer (Human) | Reviews shutdown / parts-gap decisions; approves or holds | Human gate — approve → issuer; hold → audit |
| `technician_human` | Maintenance technician (Human) | Does the repair; reports completed / parts_issue | Human gate — parts_issue → planner (bounded); completed → audit |
| `ops_manager_human` | Ops manager (Human) | Production-impact tier: capex sign-off, safety escalations, held flows | Human gate — everything that leaves the pod |
| `maint_audit_logger_agent` | Compliance records custodian | Hash-chains every issue, dispatch, approval, hold, and forecast | External (custom sink); terminus |
| `spare_parts_forecaster_agent` | Spare-Parts Demand Planner | 30/60/90-day parts forecast; critical shortages + overstock | LLM + **playbook**, weekly cron + **timezone**; `@chain` stockout floor; → ops manager |
| `maintenance_records_agent` | Maintenance Records Analyst | WO ledger/MTBF, PM-compliance, recurrence sweep (CMMS) | Programmatic + MCP connector; → engineer / ops manager |
| `equipment_lifecycle_agent` | Asset Lifecycle Planner | Continue / overhaul / replace scoring | LLM + **playbook**; `@pre_compose` dossier + `@chain` decision floor; → ops manager / planner |
| `safety_incident_reporter_agent` | EHS / Safety Compliance Officer | OSHA severity + statutory filing windows | LLM + **playbook** + OSHA tables; `@chain` severity floor; → ops manager |

The **LeafMesh Manager** supervises — it reads every hand-off and escalates
stalls / repeated errors / gate timeouts to the Ops Manager automatically. It is
a supervisor, not a router in the middle of the team.

## The deterministic floors (LLM-unwaivable)

- **Failure-probability severity floor** (`failure_predictor`, `@chain`): a risk
  score ≥ 0.7 clamps RUL into the 24h window and forces `safety_critical`.
- **Safety-critical hard-stop** (`failure_predictor`, `@chain`): a safety-critical
  asset OR a low-confidence prediction inside the window forces the engineer route.
- **Parts-availability gate** (`work_order_scheduler`): a part not on hand, or lead
  time > RUL, is unavailable → no issue, fail-closed to the engineer.
- **Work-order priority floor** (`work_order_scheduler`, `@chain`): P1/P2/P3 derived
  deterministically — an emergency can't be down-ranked.
- **Issuance floor** (`work_order_issuer`, `@chain`): issue ONLY on an explicit
  authorisation + parts on hand + a traceable WO; else block and delete the partial.
- **Lifecycle decision floor** (`equipment_lifecycle`, `@chain`): past design life +
  over the replace threshold forces at least an overhaul.
- **OSHA windows** (`safety_incident_reporter`, `@chain`): 8h / 24h / 7-day
  statutory deadlines are Python tables, never hallucinated.

Plus: bounded re-work back-edge (technician `parts_issue` → planner, counter →
engineer at the cap); every human gate **default-denies on timeout**; every
terminal path lands in the hash-chained WORM audit.

## Run it day-0 (no connectors)

The seeded dev store ships a realistic fleet — `extruder-7` (past design life,
safety-critical, vibration in ISO 10816 zone D), a nominal pump and conveyor, an
anomalous reading, and a parts inventory where the replacement bearing's 72h lead
time exceeds the RUL — so the failure floor, the safety hard-stop, and the parts
gate all fire on the demo asset before any connector is wired.

```bash
pip install -r requirements.txt          # core LeafMesh runs the pod day-0
docker run -d -p 6379:6379 redis:7-alpine
cp env .env                              # fill at least Tier 1
python validate_config.py                # pre-flight: wiring, yields, dead-code
python examples/run_examples.py 02       # the anomaly → safety → parts-gap flow
python main.py                           # API at http://127.0.0.1:18820/docs
```

Trigger a case (day-0, against the seed store — no connector needed):

```bash
curl -X POST http://127.0.0.1:18820/api/mesh/request \
  -H "Content-Type: application/json" \
  -d '{"entry_point":"sensor_reading",
       "data":{"asset_id":"extruder-7","message":"vibration spike on the extruder"}}'
```

On approval, Ivan renders the approved work-order pack to `./out/` and appends
`./data/wo_ledger.json`; every terminal path hash-chains into
`./logs/maint_audit.jsonl`.

> Packaging note: `pip install leafmesh` (core) includes all LLM providers and
> runs the pod. For real connectors add `leafmesh[mcp]`. Avoid `leafmesh[all]` on
> Python 3.12+ (it pulls `cchardet`, which has no wheel) — add only the extras you
> need.

---

*Built with `create-leafmesh`. The config is the department; the PRD is the brief.*
*Design doctrine: `.claude/skills/leafmesh/building-agents-thoroughly.md`.*
