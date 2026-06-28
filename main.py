"""predictive-maintenance — Predictive Maintenance.

YAML-first: the mesh is defined in configs/config.yaml. Sensor data
comes from your plant historian (HISTORIAN_MCP_URL) and work orders
live in your CMMS (CMMS_MCP_URL) via connectors — no template storage.
The only Python agents are the fail-closed work-order actuator and the
OSHA deadline tables; this file just boots the mesh and registers the
WORM audit sink.
"""
import asyncio
import signal
import sys

from dotenv import load_dotenv
from leafmesh import EventType, LeafMesh, LeafMeshLogger

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
    try:
        await leafmesh.enable_self_healing()
    except AttributeError:
        pass

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
