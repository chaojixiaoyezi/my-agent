
from __future__ import annotations

import time
from dataclasses import dataclass

from .models import SubagentsDispatchOptions


@dataclass(frozen=True)
class BackgroundLaunchUpdate:
    """Structured lifecycle update for create_subagents auto-start dispatch."""

    status: str
    error: str = ""


def mark_background_launch(agent, options: SubagentsDispatchOptions, update: BackgroundLaunchUpdate) -> None:
    launch_id = str(options.background_launch_id or "").strip()
    if not launch_id or not options.run_ids:
        return
    manager = getattr(agent, "subagents", None)
    now = time.time()
    for run_id in options.run_ids:
        try:
            task = manager.load(run_id)
        except Exception:
            continue
        attrs = dict(getattr(task, "attributes", {}) or {})
        attrs["background_start"] = {
            "launch_id": launch_id,
            "status": update.status,
            "updated_at": now,
            "error": update.error,
        }
        task.attributes = attrs
        try:
            manager.save(task)
        except Exception:
            continue
