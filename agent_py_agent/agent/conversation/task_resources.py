# LLM: 主任务停止须在调用方原 task transition guard 内关闭执行权并冻结资源；本模块不取 Goal/Gateway 锁。
# 模块用途: 为 Gateway 和本地控制提供共用的主执行链停止接线，不把访问范围当资源归属或扫描整个会话。
from __future__ import annotations

from pathlib import Path

from ..runtime_db.run_cancellation import (
    RuntimeCancellationConflict,
    RuntimeCancellationTarget,
    cancel_runtime_run,
)
from ..tooling.process_scope import ProcessExecutionScope


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
