# LLM: CLI background dispatch helpers keep subagents-dispatch lifecycle writes out of the command entrypoint.
# 模块用途: 保存 create_subagents 后台启动进程的生命周期写回逻辑，避免 CLI 入口文件膨胀。

from __future__ import annotations

import time
from dataclasses import dataclass

from .models import SubagentsDispatchOptions


# LLM: BackgroundLaunchUpdate carries lifecycle state from subagents-dispatch back to task records.
# 类用途: 描述后台启动进程的 running/finished/failed 状态和可选错误摘要。
@dataclass(frozen=True)
class BackgroundLaunchUpdate:
    """Structured lifecycle update for create_subagents auto-start dispatch."""

    status: str
    error: str = ""


# LLM: mark_background_launch lets the subprocess publish running/finished/failed status to the task tree.
# 函数用途: 给 create_subagents 后台进程写生命周期，失败时只记录状态，不影响调度主流程。
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
