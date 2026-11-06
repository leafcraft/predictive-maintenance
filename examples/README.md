# predictive-maintenance — example scenarios

Four realistic scenarios that exercise the major paths through this pod.

Day-0 note: the pod runs on the **seed dev store** (`agency/_shared/store.py`)
with no connector wired — `read_condition_signals` and the RUL tools read the
seed asset. Wire your historian (vault connections `historian_mcp` / `plant_iiot`)
and CMMS (vault connection `cmms_mcp`) for live data (see the configuration in Studio). `sensor_reading` now
enters through **Mira, the coordinator**, who opens the case and hands it to Rex.

## How to run

```bash
create-leafmesh --load .   # once: loads the team into the Redis in .env (or use create-leafmesh --start .)
python examples/run_examples.py                 # all scenarios
python examples/run_examples.py 01_             # one scenario (substring match)
```

## Scenario map

- **01_routine_sensor_reading** — `sensor_reading` — Normal-band reading — Mira opens the case, Rex scores it healthy, no work order, no gate fires.
- **02_anomaly_failure_predicted** — `sensor_reading` — Vibration spike on Extruder-7 — Rex's safety floors clamp RUL into the 24h window + force engineer review; Wanda plans an emergency work order; the parts gate holds → reliability engineer.
- **03_safety_critical_shutdown** — `shutdown_request` — Operator requests a manual shutdown — the reliability engineer is involved immediately.
- **04_weekly_parts_forecast** — `weekly_parts_forecast` — Mon 08:00 cron — Perry forecasts 90-day parts demand; the stockout floor forces the known bearing shortage.

## Audit trail

Every scenario appends one hash-chained entry to the template's
`logs/<audit>.jsonl` file. Tail it during a run to see the
ledger build up across scenarios.
