
from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..agent.runtime_errors import runtime_error_report
from .models import SubagentsDispatchOptions


@dataclass(frozen=True)
class BackgroundLaunchUpdate:
    """Structured lifecycle update for create_subagents auto-start dispatch."""

    status: str
    error: str = ""


@dataclass(frozen=True)
class BackgroundLaunchReport:
    """Structured failures observed while recording background launch state."""

    load_errors: list[dict[str, object]] = field(default_factory=list)
    save_errors: list[dict[str, object]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.load_errors and not self.save_errors


# LLM: CLI dispatch 进程(create_subagents 自动派工的 subprocess)对任务
#   background_start 的生命周期标记(running/finished/failed)。记录构造统一走
#   process_control.build_background_start_record——R7a 实锤:这里曾手写 dict
#   覆盖,抹掉主代理落盘的 pid,孤儿回收进程层因此失效(terminated_processes
#   为空)。pid 保留契约见 build_background_start_record。
# 函数用途: 后台派工进程把自己"跑到哪一步了"写回每个任务,但绝不弄丢主代理
#   记下的进程号。
def mark_background_launch(
    agent,
    options: SubagentsDispatchOptions,
    update: BackgroundLaunchUpdate,
) -> BackgroundLaunchReport:
    from ..agent.subagents.process_control import (
        BackgroundStartUpdate,
        build_background_start_record,
    )

    launch_id = str(options.background_launch_id or "").strip()
    if not launch_id or not options.run_ids:
        return BackgroundLaunchReport()
    manager = getattr(agent, "subagents", None)
    load_errors: list[dict[str, object]] = []
    save_errors: list[dict[str, object]] = []
    record_update = BackgroundStartUpdate(launch_id=launch_id, status=update.status, error=update.error)
    for run_id in options.run_ids:
        try:
            task = manager.load(run_id)
        except Exception as exc:
            load_errors.append(_background_launch_error(exc, "background_launch.task.load", run_id))
            continue
        attrs = dict(getattr(task, "attributes", {}) or {})
        attrs["background_start"] = build_background_start_record(attrs.get("background_start"), record_update)
        task.attributes = attrs
        try:
            manager.save(task)
        except Exception as exc:
            save_error = _background_launch_error(exc, "background_launch.task.save", run_id)
            save_errors.append(save_error)
            task.attributes = dict(getattr(task, "attributes", {}) or {})
            task.attributes["background_start"] = {
                **dict(task.attributes.get("background_start") or {}),
                "save_error": save_error,
            }
            continue
    return BackgroundLaunchReport(load_errors=load_errors, save_errors=save_errors)


def _background_launch_error(exc: BaseException, context: str, run_id: str) -> dict[str, object]:
    report = runtime_error_report(exc, context=context)
    report["run_id"] = run_id
    return report
