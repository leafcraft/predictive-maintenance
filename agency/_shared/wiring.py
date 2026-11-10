"""Is a real system wired? The vault decides, not .env.

Day 0 the pod runs on the dev store. A real connector counts as wired once
its vault record exists (created on Studio's Infrastructure page). This
module only asks the vault whether that record is there; it never reads an
address or a token from .env.
"""
from __future__ import annotations


async def vault_record_exists(name: str) -> bool:
    """True when the vault holds a connection record called ``name``.

    Only "no such record" (or no vault at all: unit tests, a bare script)
    means "not wired": ``connection_rows`` raises ``UnresolvedVaultRef`` and
    the dev-store fallback runs and says so. A record this agent may not read
    (``SecretNotAllowed``, a PermissionError) still exists, so it counts as
    wired and the real call reports who may use it. Any other error (the
    store is unreachable, for example) is raised: an outage must never quietly
    switch a real system off and the dev store on.
    """
    from leafmesh import connection_rows
    from leafmesh.vault.refs import UnresolvedVaultRef
    try:
        await connection_rows(name)
    except UnresolvedVaultRef:
        return False
    except PermissionError:
        return True
    return True