# LLM: CLI 标记调用原 lifecycle 服务的条件 mutation，不直接导入领域实现或恢复独立 load/save 写链。
# 模块用途: 把派工进程状态写回准确的启动记录，并报告未确认写入。
from __future__ import annotations

import os
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


# LLM: CLI 使用宿主同一个条件更新入口；原 launch/attempt 的比较和 mutation 共用 creation guard，拒绝时不继续派工。
# 函数用途: 后台派工进程把自己"跑到哪一步了"写回每个任务,但绝不弄丢主代理
#   记下的进程号。
def mark_background_launch(
    agent,
    options: SubagentsDispatchOptions,
    update: BackgroundLaunchUpdate,
) -> BackgroundLaunchReport:
    from ..agent.subagents.process_control import (
        BackgroundStartUpdate,
    )

    launch_id = str(options.background_launch_id or "").strip()
    if not launch_id or not options.run_ids:
        return BackgroundLaunchReport()
    manager = getattr(agent, "subagents", None)
    load_errors: list[dict[str, object]] = []
    save_errors: list[dict[str, object]] = []
    for run_id in options.run_ids:
        try:
            manager.lifecycle.update_background_start(run_id, BackgroundStartUpdate(
                launch_id=launch_id, status=update.status, error=update.error, pid=os.getpid(),
                attempt_id=options.expected_attempt_ids[run_id],
            ))
        except FileNotFoundError as exc:
            load_errors.append(_background_launch_error(exc, "background_launch.task.load", run_id))
        except Exception as exc:
            save_errors.append(_background_launch_error(exc, "background_launch.task.update", run_id))
    return BackgroundLaunchReport(load_errors=load_errors, save_errors=save_errors)


def _background_launch_error(exc: BaseException, context: str, run_id: str) -> dict[str, object]:
    report = runtime_error_report(exc, context=context)
    report["run_id"] = run_id
    return report
