"""Global tools for the predictive-maintenance pod — the day-0 tool surface.

These are the tools Cass (condition sensor) and Rex (reliability analyst) call,
in the turn, to PULL condition data. Each reads the dev store (seed signals) so
the pod runs with NO historian wired, and **each announces itself**: when there
is no real source and no seed, it returns `available: false` and says why — never
a synthetic reading that looks real. To go live, wire the historian mcp block in
config.yaml (each named action becomes its own tool); these dev tools are the
fallback contract.

`claim_next_asset` is the ATOMIC CLAIM tool that makes `instances: N` fan-out
correct: each parallel copy of Cass claims a disjoint asset from the inspection
backlog instead of every copy re-reading the same one.
"""
import os

from leafmesh import global_tool, LeafMeshLogger

try:
    from ._shared import store
except ImportError:  # pragma: no cover — direct-run fallback
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _shared import store  # type: ignore

logger = LeafMeshLogger(__name__)


def _historian_wired() -> bool:
    """True if a real condition-signal connector is configured."""
    return bool(os.getenv("HISTORIAN_MCP_URL"))


@global_tool(category="condition")
def claim_next_asset() -> dict:
    """Atomically claim the next asset from the PM inspection backlog.

    Pairs with `instances: N` on the condition sensor: each parallel copy calls
    this once so the copies split the fleet instead of duplicating work. Returns
    `{available: false}` when the backlog is empty (nothing left to claim).
    """
    claimed = store.claim_next_asset()
    logger.info(f"claim_next_asset -> {claimed.get('asset_id') or 'NONE'} "
                f"available={claimed.get('available')}")
    return claimed


@global_tool(category="condition")
def read_condition_signals(asset_id: str) -> dict:
    """Read the latest condition signature for an asset (vibration RMS + baseline,
    temperature, current, trend, dominant signature, anomaly flag).

    Reads the seed store when no historian is wired; announces `available: false`
    when there is genuinely nothing to read.

    Args:
        asset_id: The asset to read condition signals for (e.g. "extruder-7").
    """
    result = store.read_condition_signals(str(asset_id).strip())
    if not result.get("available") and _historian_wired():
        return {"available": False,
                "reason": "historian wired but returned nothing for this asset"}
    return result


@global_tool(category="reliability")
def read_asset_history(asset_id: str) -> dict:
    """Read an asset's maintenance work-order history (kind, cost, downtime,
    root cause), so RUL is reasoned over real failures, not a guess.

    Args:
        asset_id: The asset whose history to read.
    """
    history = store.asset_history(str(asset_id).strip())
    if not history and not _historian_wired():
        return {"available": False, "asset_id": asset_id,
                "reason": "no seed history for this asset; wire CMMS_MCP_URL"}
    return {"available": True, "asset_id": asset_id, "history": history}


@global_tool(category="reliability")
def failure_probability_score(asset_id: str) -> dict:
    """Read the deterministic failure-probability score for an asset — the
    model-agnostic risk signal the safety floor reads (0..1 within 24h, ISO
    10816 vibration zone, trend).

    Args:
        asset_id: The asset to score.
    """
    score = store.failure_probability(str(asset_id).strip())
    if not score.get("vibration_rms_mm_s") and not _historian_wired():
        # No reading behind the score and no real source — say so.
        return {"available": False, "asset_id": asset_id,
                "reason": "no seed reading for this asset; wire HISTORIAN_MCP_URL"}
    return {"available": True, **score}
