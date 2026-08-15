from __future__ import annotations

"""Public runtime boundary for deterministic orphan supervision."""

from typing import Any


def supervise_orphan_runs(agent: Any) -> dict[str, object]:
    """Run the canonical supervisor without exposing orchestration internals.

    Gateway control handlers depend on this composition-level boundary.  The
    implementation stays owned by the existing orchestration service, so this
    module is an adapter rather than a second recovery path.
    """

    from .agent_core.orchestration.dispatch.capability_auto_sweep import (
        supervise_stalled_orphans,
    )

    return supervise_stalled_orphans(agent)


__all__ = ["supervise_orphan_runs"]
