from __future__ import annotations

# LLM: 复用 runner 心跳采样真实模型/工具活动，仅发布 exact attempt 的诊断；不能取消、接管或改任务终态。
# 模块用途: 长时间没有模型或工具进展时通知直属父级，区分慢请求和已证实失败，不启动新计时线程。
import time
from typing import Any

from ...capability.runtime_config_reload import capability_config_for_agent
from ...subagents.runner_completion_wake import notify_parent_on_activity_notice

ACTIVITY_DIAGNOSTIC_ATTR = "runtime_activity_diagnostic"


# LLM: 只读取本 worker 的模型调用账及 canonical run 活动；单调时间先计算时长，不与持久墙钟直接比较。
# 函数用途: 生成当前阶段的小快照，不含模型正文、工具参数或密钥。
def runner_activity_sample(worker: Any, task: Any, *, wall: float, monotonic: float) -> dict[str, Any]:
    run_id = str(task.id)
    attempt_id = str(getattr(task, "runner_active_attempt_id", "") or "")
    if (
        str(task.status) != "RUNNING" or not attempt_id
        or attempt_id != getattr(worker, "_runner_activity_attempt_id", "")
    ):
        return {}
    records = getattr(getattr(worker, "_model_call_ledger", None), "records", None)
    active = [r for r in records() if r.run_id == run_id and r.status in {"started", "first_token"}] if callable(records) else []
    activity = (getattr(task, "attributes", {}) or {}).get("runtime_activity") or {}
    if not isinstance(activity, dict):
        activity = {}
    if active:
        record = max(active, key=lambda r: r.started_at)
        phase = "first_token_wait" if record.first_token_at is None else "stream_idle"
        quiet = max(0.0, monotonic - record.last_activity_at)
        key = str(record.call_id)
        tool = ""
    else:
        kind = str(activity.get("kind") or "")
        at = float(activity.get("at") or 0.0)
        if at <= 0:
            return {}  # 没有可验证的阶段起点，不把缺少观测伪装为已经静默很久。
        phase = {
            "runner_tool_call_started": "tool_wait",
            "runner_model_request_started": "first_token_wait",
            "runner_model_stream_active": "stream_idle",
        }.get(kind, "between_steps")
        quiet = max(0.0, wall - at)
        key = f"{kind}:{at}"
        tool = str(activity.get("tool") or "")
    if activity.get("kind") == "runner_provider_retry_scheduled":
        phase = "provider_retry"
        quiet = max(0.0, wall - float(activity.get("at") or wall) - float(activity.get("delay_seconds") or 0))
        key = f"retry:{activity.get('at')}:{activity.get('attempt')}"
    return {
        "run_id": run_id, "attempt_id": attempt_id, "phase": phase,
        "activity_key": key, "quiet_seconds": round(quiet, 3), "tool": tool,
        "last_activity_at": wall - quiet,
    }


# LLM: 通知阈值不是执行超时；0 关闭该阶段提示，配置来自唯一 capability snapshot。
# 函数用途: 为首 token、流静默和长工具分别取提醒时间，健康慢流不会因总时长被提示。
def activity_notice_threshold(config: object, phase: str) -> float:
    field, default = {
        "first_token_wait": ("subagent_first_token_notice_seconds", 600),
        "stream_idle": ("subagent_stream_idle_notice_seconds", 180),
        "tool_wait": ("subagent_tool_wait_notice_seconds", 900),
        "between_steps": ("subagent_stream_idle_notice_seconds", 180),
        "provider_retry": ("subagent_first_token_notice_seconds", 600),
    }.get(phase, ("subagent_stream_idle_notice_seconds", 180))
    return max(0.0, float(getattr(config, field, default)))


# LLM: 此入口在既有 heartbeat 中调用；只在阶段异常/恢复时写 canonical 诊断，重复通知由 wake receipt 去重。
#   出错交由心跳调用方记录并重试，不改变原模型请求、工具执行或权限。
# 函数用途: 检查一次长等待，必要时提醒父级；没有新证据不周期性调用模型。
def observe_runner_activity(worker: Any, run_id: str) -> None:
    config = capability_config_for_agent(worker)
    if not bool(getattr(config, "subagent_activity_notices_enabled", True)):
        return
    task = worker.subagents.load(run_id)
    # 命名来源岗位已有专用活动监督，不能再叠加普通子代理提醒策略。
    from ...common.audit_activation import structured_audit_supervised_worker_attributes

    if structured_audit_supervised_worker_attributes(getattr(task, "attributes", {}) or {}):
        return
    sample = runner_activity_sample(worker, task, wall=time.time(), monotonic=time.monotonic())
    if not sample:
        return
    threshold = activity_notice_threshold(config, sample["phase"])
    previous = (task.attributes or {}).get(ACTIVITY_DIAGNOSTIC_ATTR) or {}
    if not isinstance(previous, dict):
        previous = {}
    if threshold <= 0:
        return
    if sample["quiet_seconds"] < threshold:
        _record_resumed_activity(worker.subagents, task, sample, previous)
        return
    if sample["phase"] in {"tool_wait", "between_steps"}:
        _include_pending_approval(worker, task, sample)
    key = f"activity-notice:{run_id}:{sample['attempt_id']}:{sample['phase']}:{sample['activity_key']}"
    if previous.get("notice_key") == key and previous.get("notified") is True:
        return
    notice = previous if previous.get("notice_key") == key else {
        "schema_version": "subagent-activity-diagnostic.v1", "state": "quiet",
        **sample, "notice_key": key, "threshold_seconds": threshold,
        "observed_at": time.time(), "notified": False,
        "failure_confirmed": False, "automatic_action": "none",
    }
    updated = _save_activity_diagnostic(worker.subagents, task, notice)
    if updated is None:
        return
    if notify_parent_on_activity_notice(worker.subagents, updated, notice):
        _save_activity_diagnostic(worker.subagents, updated, {**notice, "notified": True})


# LLM: 仅在工具长等待已触发时读同 root 的 canonical 审批账，不每个心跳扫文件，不推断授权决定。
# 函数用途: 区分等待审批和正在运行工具，只暴露审批 ID，不把工具参数或私密内容带入提醒。
def _include_pending_approval(worker: Any, task: Any, sample: dict[str, Any]) -> None:
    from ...conversation.agent_tool_approval import list_pending_subagent_tool_approvals

    root_id = str(getattr(task, "root_id", "") or "")
    if not root_id:
        return
    for row in list_pending_subagent_tool_approvals(worker, root_task_id=root_id):
        if str(row.get("run_id") or "") == str(task.id):
            sample["phase"] = "approval_wait"
            sample["permission_id"] = str(row["request"]["permission_id"])
            return


# LLM: 诊断只能写入仍 RUNNING 的 exact attempt；canonical mutation 不改 run 状态、租约或子树。
# 函数用途: 原子保存活动提醒，防止迟到心跳覆盖已结束或换代的任务。
def _save_activity_diagnostic(manager: Any, task: Any, notice: dict[str, Any]) -> Any | None:
    accepted = False

    # LLM: 在 run 状态锁内复核 attempt，仅替换本模块拥有的诊断字段。
    # 函数用途: 避免与停止、完成和工具活动刷新并发时覆盖其他状态。
    def update(current: Any) -> None:
        nonlocal accepted
        if str(current.status) != "RUNNING" or current.runner_active_attempt_id != notice["attempt_id"]:
            return
        current.attributes = {**dict(current.attributes or {}), ACTIVITY_DIAGNOSTIC_ATTR: notice}
        accepted = True

    updated = manager.mutate(task.id, update)
    return updated if accepted else None


# LLM: 活动恢复只更新诊断，不另发周期性“健康”模型通知；保留 notice_key 防止同阶段重复提醒。
# 函数用途: 后续 token 或工具活动出现后撤掉静默提示，不遗留“已卡住”的假状态。
def _record_resumed_activity(manager: Any, task: Any, sample: dict, previous: dict) -> None:
    if previous.get("state") != "quiet" or previous.get("attempt_id") != sample["attempt_id"]:
        return
    _save_activity_diagnostic(manager, task, {**previous, "state": "progress_resumed", "resumed_at": time.time()})
