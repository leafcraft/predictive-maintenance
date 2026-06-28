# predictive-maintenance — example scenarios

Four realistic scenarios that exercise the major paths through this mesh.

YAML-first note: `condition_sensor_agent` reads from your plant
historian (`HISTORIAN_MCP_URL`) and `maintenance_records_agent` from
your CMMS (`CMMS_MCP_URL`) via MCP connectors — wire those gateways
(see `configs/config.yaml`) before running scenarios 01 and 02
end-to-end.

## How to run

```bash
python examples/run_examples.py                 # all scenarios
python examples/run_examples.py 01_             # one scenario (substring match)
```

## Scenario map

- **01_routine_sensor_reading** — `sensor_reading` — Normal-band IIoT reading — condition_sensor_agent confirms, no anomaly, no downstream agents fire.
- **02_anomaly_failure_predicted** — `sensor_reading` — Vibration spike — anomaly=true → failure_predictor_agent runs RUL model → 18h to failure, safety-critical false.
- **03_safety_critical_shutdown** — `shutdown_request` — Operator requests manual shutdown — reliability_engineer immediately involved.
- **04_weekly_parts_forecast** — `weekly_parts_forecast` — Mon 08:00 cron — spare_parts_forecaster predicts 90-day parts demand.

## Audit trail

Every scenario appends one hash-chained entry to the template's
`logs/<audit>.jsonl` file. Tail it during a run to see the
ledger build up across scenarios.
