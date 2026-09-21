# LLM: 调用方在原 task/creation 锁内关闭执行权并固定整树资源；清理只消费 batch，本模块不取 Goal/Gateway 锁。
# 模块用途: 为 Gateway 和本地控制提供共用的任务资源停止接线，不把访问范围当执行归属或扫描整个会话。
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..runtime_db.run_cancellation import (
    RuntimeCancellationConflict,
    RuntimeCancellationTarget,
    cancel_runtime_run,
)
from ..subagents.cancellation import FrozenSubagentStops, cleanup_subagent_stops
from ..tooling import process_resource_stop as process_resources
from ..tooling.process_resource_stop import FrozenProcessStop
from ..tooling.process_scope import ProcessExecutionScope


# LLM: 主和子资源来自同一控制事务；不携带重扫回调，失败与已提交清单必须一起交接。
# 类用途: 将原任务整棵树的资源固定下来，供 Gateway 或本地后台清理使用。
@dataclass(frozen=True)
class FrozenTaskResources:
    main: FrozenProcessStop | None
    children: FrozenSubagentStops

    # LLM: 只报告冻结阶段是否完整，不能替代进程退出核对。
    # 函数用途: 让控制应答保留主资源或孩子准备失败。
    @property
    def unconfirmed(self) -> bool:
        return self.children.unconfirmed or bool(self.main and (
            self.main.background_freeze_error or self.main.pty_request_error
        ))


# LLM: 调用方持原 task/creation 锁且已关闭主权限；主清单提交后，孩子失败不得丢掉已冻结主清单。
# 函数用途: 同步固定主资源和原子树，清理阶段不能把恢复后新增的资源算进来。
def freeze_task_resources(
    agent: object, scope: ProcessExecutionScope | None, request_id: str, *, related_request_ids: tuple[str, ...] = (),
) -> FrozenTaskResources:
    main = process_resources.freeze_process_stop(scope) if scope is not None else None
    try:
        children = agent.prepare_request_subagent_stop(
            request_id, reason="conversation_user_stop", related_request_ids=related_request_ids,
        )
    except Exception as exc:
        from ..runtime_errors import runtime_error_report

        children = FrozenSubagentStops(failed=({
            "cancellation_attempted": True, **runtime_error_report(exc, context="task_stop.children"),
        },))
    return FrozenTaskResources(main, children)


# LLM: 所有锁外执行，只处理给定清单；一项清理异常不抑制其它成员，不查询 agent 或当前树。
# 函数用途: 清理固定的主后台和子代理资源，保留各路已确认与未知结果。
def cleanup_task_resources(batch: FrozenTaskResources) -> dict[str, object]:
    import logging

    main = {"status": "not_selected"}
    try:
        if batch.main is not None:
            main = process_resources.cleanup_process_stop(batch.main)
    except Exception as exc:
        main = {"status": "unknown", "error_type": type(exc).__name__}
        logging.getLogger(__name__).exception("主任务资源清理未确认")
    return {"main": main, "children": cleanup_subagent_stops(batch.children)}


# LLM: None 表示已选持久任务无热请求，允许读唯一 main 行；空 binding 表示热请求尚未发布，不回退猜旧身份。
# 返回时 DB 事务已经释放；调用方仍须写任务停止状态并冻结资源，主链关闭不代表整树已停止。
# 函数用途: 确认主代理原执行轮不能再启动工具，交回同一执行归属供控制入口冻结资源。
def close_main_task_authority(
    repository: object,
    *,
    owner_home: object,
    task_id: str,
    thread_id: str,
    binding: dict | None = None,
) -> ProcessExecutionScope | None:
    if repository is None:
        return None
    row = (
        repository.get_agent_run(str(binding.get("agent_run_id") or ""))
        if binding else repository.main_agent_run_for_task(task_id)
    )
    if row is None and not binding:
        return None
    target = RuntimeCancellationTarget(
        task_id,
        str(binding["run_id"] if binding else row["run_id"]),
        str(binding["agent_run_id"] if binding else row["agent_run_id"]),
        str(binding["attempt_id"] if binding else row["current_attempt_id"]),
    )
    if (binding == {} or row is None or row["role"] != "main"
            or row["parent_agent_run_id"] or binding and binding.get("task_id") != task_id):
        raise RuntimeCancellationConflict(target, "main_binding_conflict")
    cancel_runtime_run(repository, target, reason="conversation_user_stop", source="conversation_control")
    return ProcessExecutionScope(
        owner_home=str(Path(str(owner_home)).expanduser().resolve(strict=False)) if owner_home else "",
        thread_id=thread_id, root_task_id=task_id, run_id=target.run_id,
    )
