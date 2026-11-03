"""predictive-maintenance — Predictive-Maintenance Pod (LeafMesh).

Boot order matters (SDK 2.4.131):
  1. import agency.tools  → the @global_tool decorators run BEFORE from_yaml/start,
     so Cass's claim_next_asset / read_condition_signals and Rex's telemetry tools
     are registered.
  2. LeafMesh.from_yaml(...) → auto_discover wires agency/*_agent.py to the YAML.
  3. audit_logger.register(...) → attach the WORM sink to the custom external
     maint_audit_logger_agent (after from_yaml, before start).
  4. leafmesh.start().

YAML-first: the mesh is defined in configs/config.yaml. Sensor data comes from
your plant historian (vault records 'historian_mcp' / 'plant_iiot') and work
orders live in your CMMS (vault record 'cmms_mcp') via connectors; day-0 the pod
runs on the seed dev store with no connector wired.
"""
import asyncio
import signal
import sys

from dotenv import load_dotenv
from leafmesh import EventType, LeafMesh, LeafMeshLogger

import agency.tools  # noqa: F401 — registers @global_tool tools before start()
from agency._shared import audit_logger as _audit_logger

load_dotenv()
logger = LeafMeshLogger(__name__)
leafmesh = LeafMesh.from_yaml("configs/config.yaml")

# Register the WORM audit sink as the intelligence function for the
# custom external maint_audit_logger_agent (SDK 2.4+ API).
_audit_logger.register(leafmesh, "maint_audit_logger_agent")


async def _on_agent_error(event):
    logger.warning(f"[maint-audit] AGENT_ERROR session={event.session_id}")

async def _on_manager_intervention(event):
    logger.warning(f"[maint-ops] MANAGER_INTERVENTION session={event.session_id}")

async def _on_intervention_needed(event):
    logger.error(f"[maint-ops] INTERVENTION_NEEDED session={event.session_id}")

async def _on_human_input_timeout(event):
    logger.warning(f"[maint-audit] HITL TIMEOUT session={event.session_id}")


async def main():
    await leafmesh.start()
    leafmesh.event_bus.subscribe(EventType.AGENT_ERROR,           _on_agent_error)
    leafmesh.event_bus.subscribe(EventType.MANAGER_INTERVENTION,  _on_manager_intervention)
    leafmesh.event_bus.subscribe(EventType.INTERVENTION_NEEDED,   _on_intervention_needed)
    leafmesh.event_bus.subscribe(EventType.HUMAN_INPUT_TIMEOUT,   _on_human_input_timeout)
    # Self-healing is automatic — the SDK enables it during start() on
    # every operator-facing mesh. No call is needed.

    logger.info("predictive-maintenance — Predictive Maintenance mesh running")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        await stop.wait()
    else:
        try: await stop.wait()
        except KeyboardInterrupt: pass

    await leafmesh.stop()


if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: pass
