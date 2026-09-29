# LLM: 子代理创建的 runtime.db 权威主链写入（Task→TaskRun→root AgentRun→pending AgentAttempt→Delegation→runtime_events）
#   只在这里；调用方是 SubAgentBaseService.create_run，顺序固定为“物化会话线程之后、落盘任务之前”。
#   有 home 上下文时 fail-closed：库未挂载或写入失败都抛错，任务不落地；LOCAL_UNMANAGED 只走投影。
#   成功后把链身份回存任务属性（runner 激活 attempt、授权门比对 task_id 共用）。改动时联查
#   test_runtime_db_main_chain 与 test_decision_subagent 的崩溃重试用例。
# 模块用途: 为新建子代理写入唯一权威运行记录，并把记录身份回存到任务属性。
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ..authorization_gate import AuthorizationError
from ..models import SubAgentTask

if TYPE_CHECKING:
    from .base import CreateRunParams


# LLM: 行为与原 SubAgentBaseService._write_authority_records 逐行一致，只把 self.manager 换成显式 manager；
#   有写库副作用，失败按原口径记告警并原样抛出。
# 函数用途: 创建子代理时写入权威运行链并回存链身份；没有权威库且非托管模式时直接返回。
def write_create_run_authority(manager: Any, task: SubAgentTask, params: CreateRunParams) -> None:
    """R1：create_run 权威主链写入（A.4/A.5/A.6/A.7/A.9）。

    单事务落 Task→TaskRun→root AgentRun→第一个 pending AgentAttempt→(child)
    Delegation→runtime_events；conversation_task_id/thread_id 同事务进
    tasks 行，ConversationTaskLink 不再独立权威。owner_home_dir 为空
    （无 home 上下文）时无权威库，只走投影（向后兼容）。

    F8（fail-closed）：有 home 上下文时权威写入不允许静默失败——缺记录
    的 run 会被授权门按"权威记录缺失"拒绝（B.6），成为不可管理的幻影；
    故 attach 降级（repo None）或写入失败一律抛错，任务创建不落地。
    """
    repo = getattr(manager, "runtime_db", None)
    if repo is None:
        from ...runtime_db.execution_mode import expects_managed_authority

        if expects_managed_authority(manager):
            raise AuthorizationError(
                f"create_run: 权威库不可用（ExecutionMode.MANAGED 但 "
                f"runtime.db 未挂载，run={task.id} 拒绝创建）"
            )
        return  # LOCAL_UNMANAGED（纯文件层/投影）→ 只走投影（显式选择）
    attrs = params.attributes if isinstance(params.attributes, dict) else {}
    owner_id = str(task.owner or params.owner or "").strip()
    if not owner_id:
        owner_id = str(getattr(manager, "owner_id", "") or "").strip()
    try:
        record = repo.record_run_creation(
            owner_id=owner_id,
            goal=str(task.goal or "").strip(),
            conversation_task_id=str(attrs.get("conversation_task_id") or "").strip(),
            thread_id=str(attrs.get("conversation_thread_id") or "").strip(),
            run_id=str(task.id or "").strip(),
            role=str(task.role or "").strip(),
            parent_run_id=str(params.parent_id or "").strip(),
            attempt_status="pending",
        )
    except (OSError, KeyError) as exc:
        # 权威写入失败不再吞掉：有 home 时记录缺失 = 门后拒绝（fail-closed）。
        logging.getLogger(__name__).warning(
            "runtime.db 权威写入失败(run=%s): %s", task.id, exc, exc_info=True
        )
        raise
    # seq 253 闭合：链身份回存任务属性——runner 启动时激活 pending attempt、
    # run scope 携带 DB task_id（授权门比对键）都要从这里拿，不另起查询。
    _persist_runtime_authority_attrs(task, record)


# LLM: 只在 record_run_creation 成功返回后调用；四个身份字段来自权威库回执，不另起查询。
# 函数用途: 把权威链身份回存到任务属性，供 runner 与授权门共用。
def _persist_runtime_authority_attrs(task: SubAgentTask, record: dict[str, object]) -> None:
    """回存权威链身份到任务属性（save 前调用）。

    只有 record_run_creation 成功返回才调用（LOCAL_UNMANAGED 早退不经过）。
    子代理 runner 启动（attempt 轮换）、授权门比对（scope task_id）共用这份
    单一权威来源，不另起查询。
    """
    attrs = dict(getattr(task, "attributes", {}) or {})
    attrs["runtime_authority"] = {
        "task_id": str(record.get("task_id") or "").strip(),
        "task_run_id": str(record.get("task_run_id") or "").strip(),
        "agent_run_id": str(record.get("agent_run_id") or "").strip(),
        "attempt_id": str(record.get("attempt_id") or "").strip(),
    }
    task.attributes = attrs
