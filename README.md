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
advances a work order**: if nobody answers a request in time, its Inbox card
closes and the ticket records that no reply came. Nothing is issued on silence.
A healthy asset (remaining life beyond the 30-day planning horizon, not
safety-critical) gets no work order; its score is recorded in the audit trail.
Routine work (planned/corrective with parts on hand) auto-issues and dispatches;
a human is forced only on the irreversible or safety-critical decisions. Every
terminal path lands in a hash-chained WORM ledger, so the reliability team can
prove *why* each decision was made. ROI cell: `maint × goods`, internal-facing —
avoided downtime, fewer emergency-freight parts orders, an auditable PM trail.

## Modern capabilities (SDK 2.4.131)

- **`scaling.copies` (asset fan-out)** — Cass runs as **3 parallel copies** per fleet
  sweep, each ATOMICALLY CLAIMING a disjoint asset (`claim_next_asset`) so copies
  split the fleet instead of duplicating work. (2.4.484's `copies.mode: auto` with
  `claim_tool:` could replace this manual split — an owner decision, not done here.)
- **`listen_events` (IIoT stream)** — Cass binds a live MQTT condition stream via
  the top-level `plant_iiot` broker. Off on day 0: create vault connection
  `plant_iiot` (its URL), then uncomment the broker and `listen_events` (in
  `configs/config.yaml` before the first start, in Studio's YAML editor after it).
- **`timezone`** — every live `wake_up` (Cass's daily sweep, Perry's weekly
  forecast) sets a `timezone` so it fires in plant-local time, not UTC.
- **`super_agent` (dict form)** — Rex runs the plan→execute→verify orchestrator
  over the telemetry, pulling its own data via tools.
- **`playbook`** — the craft (RUL reading, safety policy, demand forecast,
  lifecycle decision order, OSHA taxonomy) lives in `responsibility`, loaded on
  demand; only the short `description` rides in the prompt.
- **`mcp` (commented)** — the historian block ships commented, picking the vault
  connection `historian_mcp` (the address + token live only in the vault); day-0
  the dev-store tools stand in and announce `available:false` when there is no
  real source.

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
| *(no setting)* — human members | Human approvals | Answer in Studio's Inbox. To notify an external tool instead, create a vault connection (an "Add custom" record on the Infrastructure page, service type 'Outbound webhook') and set `webhook_config: {connection: <name>}` on the seat. |

### Tier 2 — real systems of record (a vault connection each; inert until then)
Addresses and tokens are entered **only in the vault** (Studio → Infrastructure →
Integrations → MCP for the historian / CMMS servers, Infrastructure → Brokers &
queues → MQTT for the `plant_iiot` broker) — the SDK refuses a `url:` / `token:` written in the config, even as
`${ENV_VAR}`.

| Setting | Enables |
|---|---|
| *(vault connection `historian_mcp`)* | Live condition reads — then uncomment Rex's (`failure_predictor_agent`) `mcp` block. The day-0 tools in `agency/tools.py` check this vault record themselves; nothing goes in `.env`. |
| *(vault connection `plant_iiot`)* | The IIoT stream (uncomment the `plant_iiot` broker + Cass's `listen_events`) |
| *(vault connection `cmms_mcp`)* | Live CMMS WO ledger / PM-compliance / recurrence sweep (`maintenance_records_agent` already picks it — no uncomment needed) |

> The historian `mcp` block and the `plant_iiot` broker ship **commented** in
> `configs/config.yaml`, already naming their connections: create the
> connection, then uncomment — in the file before the first start, in Studio's
> YAML editor after it (applies live, no restart; see *Changing the configuration after the first start*).

### Tier 3 — escalation egress (optional)
| Setting | Purpose |
|---|---|
| *(vault connection `escalation_webhook`)* | Manager-escalation webhook (e.g. PagerDuty): create vault connection `escalation_webhook` (an "Add custom" record on the Infrastructure page, service type 'Outbound webhook'), then uncomment the webhook target (in `configs/config.yaml` before the first start, in Studio's YAML editor after it) |
| `SLACK_MAINT_CHANNEL` | `channel_id` for the manager-escalation Slack target, which ships **commented**: it needs a Slack adapter. Connect Slack on Infrastructure → Messaging → Slack (vault record, e.g. `slack_maint`), give `ops_manager_human` `channels: {slack: {connection: slack_maint}}`, set this value, then uncomment the target. |

## The team (13 members)

| Agent | Role (human job title) | Responsibilities | Can perform |
|---|---|---|---|
| `mira_coordinator_agent` | Maintenance Coordinator | Owns the case end to end; opens it, keeps live status, is the reliability team's one contact | LLM; orchestrates Rex→Wanda→Ivan; routes exceptions to the engineer; **never reads a sensor or approves** |
| `condition_sensor_agent` | Condition-Monitoring Engineer | Reads vibration/temp/current per asset, flags anomalies | Programmatic + **scaling.copies** (atomic-claim fleet fan-out) + **listen_events** (IIoT) + dev-store tools; on anomaly → Rex |
| `failure_predictor_agent` | Predictive Reliability Analyst | Scores RUL, failure mode, confidence; writes the engineer briefing | LLM + **super_agent** + **playbook** + telemetry tools; `@chain` **safety floors** (fail closed); → Wanda / engineer |
| `work_order_scheduler_agent` | Maintenance Planner | Picks window + work type, runs the parts gate, sets priority; bounds re-work | Programmatic; `@pre_compose` parts + `@chain` priority floor; → issuer (routine) / engineer (review/gap) |
| `work_order_issuer_agent` | Work-Order Issuer | Issues the approved pack, dispatches the technician, records the ledger | Programmatic **finisher**; auth re-check → invariants → render pack → **delete-on-block** → dispatch |
| `reliability_engineer_human` | Reliability engineer, on call (Human) | Reviews shutdown / parts-gap decisions; approves, rejects or holds | Human gate, **1h** (P1 on-call response) — approved → issuer; rejected / held_for_review → audit |
| `technician_human` | Maintenance technician (Human) | Does the repair; reports completed / parts_issue | Human gate, **8h** (one shift) — parts_issue → planner (bounded); completed / held_for_review → audit |
| `ops_manager_human` | Operations manager (Human) | Production-impact tier: capex sign-off, safety-incident escalations, critical parts shortages, PM-compliance misses | Human gate, **4h** (leaves time inside OSHA's 8h fatality window) — every answer → audit |
| `maint_audit_logger_agent` | Compliance records custodian | Hash-chains every issue, dispatch, approval, hold, and forecast | External (custom sink); terminus |
| `spare_parts_forecaster_agent` | Spare-Parts Demand Planner | 30/60/90-day parts forecast; critical shortages + overstock | LLM + **playbook**, weekly cron + **timezone**; `@chain` stockout floor; → ops manager |
| `maintenance_records_agent` | Maintenance Records Analyst | WO ledger/MTBF, PM-compliance, recurrence sweep (CMMS) | Programmatic + MCP connector; → engineer / ops manager |
| `equipment_lifecycle_agent` | Asset Lifecycle Planner | Continue / overhaul / replace scoring | LLM + **playbook**; `@pre_compose` dossier + `@chain` decision floor; → ops manager / planner |
| `safety_incident_reporter_agent` | EHS / Safety Compliance Officer | OSHA severity + statutory filing windows | LLM + **playbook** + OSHA tables; `@chain` severity floor; → ops manager |

The **LeafMesh Manager** supervises; it is not a router in the middle of the
team. It escalates to the Ops Manager on a non-retryable error (auth, a bad
request, credit, bad config), when 3 different agents fail in one case, or when
restarts run out. A malformed hand-off only raises an alert.

**If nobody answers.** Each human request waits for the time in the table above.
If nobody answers within it, the request waits no longer: its Inbox card closes
and the ticket records that no reply came. Nothing is issued, dispatched or
approved on silence, and nobody is chased automatically. (Each seat's
`fallback_response` — `held_for_review` → audit — states the intended
default-deny; leafmesh 2.4.486 does not route it for these seats yet.)

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
  the last 12 months' maintenance spend at ≥ 25% of the replacement cost forces at
  least an overhaul.
- **OSHA windows** (`safety_incident_reporter`, `@chain`): 8h / 24h / 7-day
  statutory deadlines are Python tables, never hallucinated.

Plus: bounded re-work back-edge (technician `parts_issue` → planner, counter →
engineer after 3 rounds); nothing is issued on silence; every answer a person
can give has a route, and every terminal path lands in the hash-chained WORM
audit.

## Run it day-0 (no connectors)

The seeded dev store ships a realistic fleet — `extruder-7` (11 years old on a
10-year design life, safety-critical, vibration in ISO 10816 zone D, $480k to
replace, $152.8k of maintenance in the last 12 months), a nominal pump and
conveyor, and a parts inventory where the replacement bearing (`brg-sph-22320`)
is out of stock with a 72h lead time — longer than the RUL — so the failure
floor, the safety hard-stop, the parts gate and the lifecycle floor all fire on
the demo asset before any connector is wired. The three bearing work orders in
the CMMS history are the same ones the parts-issue ledger draws against.

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

Any approval the case needs (technician, reliability engineer, ops manager)
lands in **Studio → Inbox** — answer it there. On approval, Ivan renders the approved work-order pack to `./out/` and appends
`./data/wo_ledger.json`; every terminal path hash-chains into
`./logs/maint_audit.jsonl`.

> Packaging note: `pip install leafmesh` (core) includes all LLM providers and
> runs the pod. For real connectors add the extra each one names at start-up:
> `leafmesh[mcp]` (historian / CMMS), `leafmesh[mqtt]` (the `plant_iiot` stream),
> `leafmesh[slack]` (the Slack escalation target). Avoid `leafmesh[all]` on
> Python 3.12+ (it pulls `cchardet`, which has no wheel) — add only the extras you
> need.

---

*Built with `create-leafmesh`. The config is the department; the PRD is the brief.*
*Design doctrine: `.claude/skills/leafmesh/building-agents-thoroughly.md`.*

## Changing the configuration after the first start

`configs/config.yaml` is only the first-start seed. On the first start (an empty store) the agency imports it as version 1 and from then on keeps its configuration in the store. After that the file is not read again: an edit to it is **not** applied, even after a restart (the boot log says "that edit was NOT applied").

To change a running agency:

- **Studio** — its settings pages, or the raw YAML editor.
- **API** — `PUT /api/yaml/raw` with an `X-API-Key` header and the body `{"yaml_content": "<the whole YAML>", "save_to_disk": true}`, or `PATCH /api/yaml/config` with `{"updates": {...}, "save_to_disk": true}`.

Each save is a new version and applies live on every pod — no restart. (A few settings, such as brokers, make the agency reload itself in place within seconds.) So wherever this README or a comment says "uncomment X in `configs/config.yaml`", do it in the file before the first start, or in Studio's YAML editor after it.

## Where the data store is set

From leafmesh 2.4.482 the store (Redis or Postgres) is set **only in `.env`** — never in `configs/config.yaml`. A `persistence:` block in the YAML is ignored with a warning on leafmesh 2.4.482–2.4.485; from 2.4.486 the agency refuses to start if that block differs from `.env`, and names each `.env` line to add. The project's `.env` sets the store explicitly (`LEAFMESH_STORE`, `LEAFMESH_AGENCY_NAME`, `REDIS_*`).

- Redis (default): `REDIS_HOST`, `REDIS_PORT`, `REDIS_PASSWORD`, `REDIS_DB`; optional `REDIS_DEFAULT_TTL`, `REDIS_SESSION_TTL`, `REDIS_SSL*`, `REDIS_CLUSTER_NODES`.
- Postgres instead: `LEAFMESH_STORE=postgres` plus `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` (optional `POSTGRES_SSLMODE`).
- Or the whole server in one value: `REDIS_URL=redis://user:pw@host:6379/0` (`rediss://` turns TLS on) or `DATABASE_URL=postgresql://user:pw@host:5432/db`. The single names fill in what the URL leaves out.
- With Docker Compose the app reaches Redis as `redis` (the service name); compose sets that for you.
- Check it in Studio → **Infrastructure → Datastores**: it shows whether the store is reachable (never the address or password).

## Your vault key — back it up

`create-leafmesh` put a `LEAFMESH_VAULT_KEY` in your `.env` when it made this project. It is the master key that encrypts every connection and credential you store in the vault (in Redis / Postgres).

- **Back it up like a master password.** Lose it and every stored connection has to be entered again.
- **Never change or regenerate it** for an existing project — a new key locks the existing vault. To change it, use `leafmesh vault rotate-key`.
- Every copy of the app (every replica) must get the **same** value. Docker Compose passes it in from `.env` as an environment variable.
- Never commit `.env` (it is already in `.gitignore`).
