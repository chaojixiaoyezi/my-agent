# LLM: 控制只关闭调用者冻结的执行身份；使用原 Repository CAS，不创建 attempt、不恢复 UNKNOWN、不猜当前身份。
# 模块用途: 让主代理和子代理的停止入口共用执行权限关闭规则，保留冲突与未知状态的真实证据。
from __future__ import annotations

from dataclasses import dataclass

from .operations import AGENT_RUN_TERMINAL_STATUSES, ATTEMPT_STATUS_UNKNOWN


# LLM: 所有字段由宿主的正式任务绑定或数据库行提供；此对象不代表模型传参授权。
# 类用途: 冻结本次要停止的任务、代理和执行轮，防止迟到控制跟随新 attempt。
@dataclass(frozen=True)
class RuntimeCancellationTarget:
    task_id: str
    run_id: str
    agent_run_id: str
    attempt_id: str


# LLM: 错误保留结构化原因，调用方不能捕获后改选新 current attempt 或继续全任务资源扫描。
# 类用途: 表明执行权限关闭没有得到确认，资源控制必须保留冲突或未知结果。
class RuntimeCancellationConflict(RuntimeError):
    # LLM: 不把任务正文、数据库路径或凭据放入错误；固定执行身份可用于受保护控制日志。
    # 函数用途: 记录精确停止目标和失败原因。
    def __init__(self, target: RuntimeCancellationTarget, reason: str) -> None:
        super().__init__("执行权限关闭未确认")
        self.target = target
        self.reason = reason


# LLM: 调用者须持对应任务换代/子代理创建锁直到资源冻结；DB 事务先结束，再获取资源 Store 锁。
# pending_only 必须落到同一 settle 事务，不能以本函数前读替代；已关闭不等于进程已退出。
# 函数用途: 关闭原执行轮的工具准入，或确认它原本已终止/未知，返回原身份与实际状态。
def cancel_runtime_run(
    repository: object,
    target: RuntimeCancellationTarget,
    *,
    reason: str,
    source: str,
    pending_only: bool = False,
    now: float | None = None,
) -> dict[str, object]:
    _validate_target(repository, target)
    result = repository.settle_agent_run(
        agent_run_id=target.agent_run_id,
        status="cancelled",
        attempt_id=target.attempt_id,
        expected_attempt_status="pending" if pending_only else "",
        now=now,
        payload={
            "status": "cancelled", "runtime_status": "cancelled",
            "runtime_reason": reason, "runtime_source": source, "run_id": target.run_id,
        },
    )
    if result.get("settled") is True:
        return _closed_report(target, "cancelled")
    failure = str(result.get("reason") or "unknown")
    if failure not in {"already_terminal", "attempt_unknown", "unknown_status"}:
        raise RuntimeCancellationConflict(target, failure)
    current = repository.runner_result_commit_authority(
        run_id=target.run_id, attempt_id=target.attempt_id,
    )
    if current is None or current.get("is_current") is not True:
        raise RuntimeCancellationConflict(target, "stale_attempt")
    run_status = str(current.get("run_status") or "")
    attempt_status = str(current.get("attempt_status") or "")
    if (attempt_status == ATTEMPT_STATUS_UNKNOWN
            and run_status in {"", "created", "unknown", *AGENT_RUN_TERMINAL_STATUSES}):
        return _closed_report(target, "unknown_preserved")
    if run_status in AGENT_RUN_TERMINAL_STATUSES:
        return {**_closed_report(target, run_status), "replayed": True}
    raise RuntimeCancellationConflict(target, failure)


# LLM: 关系校验先于写入；不能仅凭同一 run 文本、错任务或错 attempt 关闭其它代理。
# 函数用途: 从原数据库核对这次停止的四个身份确实属于同一执行链。
def _validate_target(repository: object, target: RuntimeCancellationTarget) -> None:
    if not all((target.task_id, target.run_id, target.agent_run_id, target.attempt_id)):
        raise RuntimeCancellationConflict(target, "missing_identity")
    row = repository.get_agent_run(target.agent_run_id)
    attempt = repository.get_attempt(target.attempt_id)
    task = repository.get_task_run(str(row["task_run_id"])) if row is not None else None
    if (row is None or attempt is None or task is None
            or row["run_id"] != target.run_id or task["task_id"] != target.task_id
            or attempt["agent_run_id"] != target.agent_run_id):
        raise RuntimeCancellationConflict(target, "identity_conflict")


# LLM: authority_closed 只证明旧调用不再有执行权，不代表资源清理或原业务副作用已经确定。
# 函数用途: 向资源控制入口交回同一身份及明确的权限状态。
def _closed_report(target: RuntimeCancellationTarget, status: str) -> dict[str, object]:
    return {
        "status": status, "authority_closed": True,
        "task_id": target.task_id, "run_id": target.run_id,
        "agent_run_id": target.agent_run_id, "attempt_id": target.attempt_id,
    }
