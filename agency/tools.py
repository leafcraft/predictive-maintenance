"""Global tools for the predictive-maintenance pod — the day-0 tool surface.

These are the tools Cass (condition sensor) and Rex (reliability analyst) call,
in the turn, to PULL condition data. Each reads the dev store (seed signals) so
the pod runs with NO historian wired, and **each announces itself**: when there
is no real source and no seed, it returns `available: false` and says why — never
a synthetic reading that looks real. To go live, create the vault record
'historian_mcp' and uncomment the historian mcp block in the configuration (each named
action becomes its own tool); these dev tools are the fallback contract. They
check the vault record themselves; nothing is read from .env.

`claim_next_asset` is the ATOMIC CLAIM tool that makes `scaling.copies.count: N` fan-out
correct: each parallel copy of Cass claims a disjoint asset from the inspection
backlog instead of every copy re-reading the same one.
"""
import os

from leafmesh import global_tool, LeafMeshLogger

try:
    from ._shared import store
    from ._shared.wiring import vault_record_exists
except ImportError:  # pragma: no cover — direct-run fallback
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _shared import store  # type: ignore
    from _shared.wiring import vault_record_exists  # type: ignore

logger = LeafMeshLogger(__name__)


async def _historian_wired() -> bool:
    """True once the vault record 'historian_mcp' exists (Infrastructure →
    Integrations → MCP)."""
    return await vault_record_exists("historian_mcp")


async def _cmms_wired() -> bool:
    """True once the vault record 'cmms_mcp' exists."""
    return await vault_record_exists("cmms_mcp")


@global_tool(category="condition")
def claim_next_asset() -> dict:
    """Atomically claim the next asset from the PM inspection backlog.

    Pairs with `scaling.copies.count: N` on the condition sensor: each parallel copy calls
    this once so the copies split the fleet instead of duplicating work. Returns
    `{available: false}` when the backlog is empty (nothing left to claim).
    """
    claimed = store.claim_next_asset()
    logger.info(f"claim_next_asset -> {claimed.get('asset_id') or 'NONE'} "
                f"available={claimed.get('available')}")
    return claimed


@global_tool(category="condition")
async def read_condition_signals(asset_id: str) -> dict:
    """Read the latest condition signature for an asset (vibration RMS + baseline,
    temperature, current, trend, dominant signature, anomaly flag).

    Reads the seed store when no historian is wired; announces `available: false`
    when there is genuinely nothing to read.

    Args:
        asset_id: The asset to read condition signals for (e.g. "extruder-7").
    """
    result = store.read_condition_signals(str(asset_id).strip())
    if not result.get("available") and await _historian_wired():
        return {"available": False,
                "reason": "historian wired but returned nothing for this asset"}
    return result


@global_tool(category="reliability")
async def read_asset_history(asset_id: str) -> dict:
    """Read an asset's maintenance work-order history (kind, cost, downtime,
    root cause), so RUL is reasoned over real failures, not a guess.

    Args:
        asset_id: The asset whose history to read.
    """
    history = store.asset_history(str(asset_id).strip())
    if not history:
        reason = ("CMMS record 'cmms_mcp' exists but this dev tool reads only the seed; "
                  "use maintenance_records_agent for live history"
                  if await _cmms_wired() else
                  "no seed history for this asset; create vault record 'cmms_mcp' for the live CMMS")
        return {"available": False, "asset_id": asset_id, "reason": reason}
    return {"available": True, "asset_id": asset_id, "history": history}


@global_tool(category="reliability")
async def failure_probability_score(asset_id: str) -> dict:
    """Read the deterministic failure-probability score for an asset — the
    model-agnostic risk signal the safety floor reads (0..1 within 24h, ISO
    10816 vibration zone, trend).

    Args:
        asset_id: The asset to score.
    """
    score = store.failure_probability(str(asset_id).strip())
    if not score.get("vibration_rms_mm_s"):
        # No reading behind the score — say so, never a made-up zero.
        reason = ("historian record 'historian_mcp' exists; read the series with the "
                  "historian tools — this dev score reads only the seed"
                  if await _historian_wired() else
                  "no seed reading for this asset; create vault record 'historian_mcp' for live reads")
        return {"available": False, "asset_id": asset_id, "reason": reason}
    return {"available": True, **score}
