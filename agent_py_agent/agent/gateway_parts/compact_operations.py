# LLM: 闲置维护只复用原摘要/检查点/线程CAS；不占长前台车道，活动变化让位且不计模型失败。
# 模块用途: 核验用户静默、容量、运行资源和活动代后执行一次低优先级闲置压缩。
from __future__ import annotations

import math
import time
from contextlib import nullcontext
from itertools import chain, islice

from ..common.cancellation import ToolCancelled
from ..common.json_io import read_json_object_report
from ..conversation.background_process_view import list_background_processes
from ..conversation.compact import (
    ConversationCompactOptions,
    inspect_conversation_context,
    prepare_conversation_context,
)
from ..conversation.compact_threshold_policy import (
    compact_policy_scope,
    resolve_thread_compact_policy,
)
from ..conversation.run_claim import detached_task_claim_scope_id, user_input_turn_on_lane
from ..conversation.store_threads import IdleCompactActivityChanged
from ..settings.config_layers import compact_idle_parameters
from ..settings.model_scope import selected_model_scope
from .background_resource_report import session_background_processes


# LLM: 只读任务/claim/子代理的含错报告及原审批账；任何坏读失败关闭，状态正文不参与裁决。
# 函数用途: 按原run属性定位同会话运行/待审批，未知归属保守让位，不续租或清理审批。
def idle_compact_busy_state(agent, store, thread):
    if user_input_turn_on_lane(store, thread.thread_id) or thread.active_task_ids:
        return "foreground_active"
    scopes = [thread.thread_id, *(detached_task_claim_scope_id(thread.thread_id, task) for task in thread.task_ids)]
    for scope in scopes:
        claim, error = store.claims.load_report(thread.thread_id, claim_scope_id=scope)
        if error:
            return "activity_unknown"
        if claim.get("status") in {"running", "claimed"}:
            return "foreground_active"
    try:
        _tasks, task_errors = store.tasks.list_report(thread.thread_id)
        report = agent.subagents.list_runs_report()
        if task_errors or report.load_errors:
            return "activity_unknown"
        for run in report.runs:
            status = getattr(run, "status", "")
            status = getattr(status, "value", status)
            attributes = getattr(run, "attributes", {}) or {}
            run_thread = str(getattr(run, "conversation_thread_id", "") or getattr(run, "thread_id", "")
                             or attributes.get("conversation_thread_id", ""))
            if not run_thread or run_thread == thread.thread_id:
                requests = getattr(run, "capability_requests", ()) or ()
                if any(getattr(getattr(r, "status", ""), "value", getattr(r, "status", "")) == "OPEN" for r in requests):
                    return "pending_approval"
                if status == "WAITING_APPROVAL":
                    return "pending_approval"
                if status == "BLOCKED":
                    return "subagent_blocked"
                if status in {"RUNNING", "PENDING", "PLANNING"}:
                    return "subagent_active"
        pending = _idle_pending_approval_state(agent, store, thread)
        if pending:
            return pending
        report = list_background_processes(agent.home_paths.owner_home_dir, thread_id=thread.thread_id, active_only=True)
        if not report["projection_ok"]:
            return "activity_unknown"
        if session_background_processes(report["items"], thread.thread_id):
            return "background_process_active"
    except (AttributeError, OSError, ValueError, TypeError, RuntimeError):
        return "activity_unknown"
    return ""


# LLM: 仅观察同owner原审批/私卡路径，限制1024条；未知坏账拒绝，不调用会吞读错或续租的展示列表。
# 函数用途: 保护仍可处理的审批/确认；明确到期只跳过，坏期限/未知归属失败关闭，不消费或清理记录。
def _idle_pending_approval_state(agent, store, thread):
    from ..path_access_policy import PRIVATE_INTERACTIONS_OWNER_PARTS

    approvals = (store.storage.root / "subagent_tool_approvals").glob("*/*.json")
    confirmations = agent.home_paths.owner_home_dir.joinpath(*PRIVATE_INTERACTIONS_OWNER_PARTS).glob("*.json")
    for index, path in enumerate(islice(chain(approvals, confirmations), 1025)):
        if index == 1024:
            return "activity_unknown"
        if path.name.startswith("."):
            continue
        report = read_json_object_report(path, context="conversation.idle.approvals")
        if report.load_error or not report.payload:
            return "activity_unknown"
        row = report.payload
        if row.get("status") not in {"pending", "active", "waiting_input", "waiting_confirmation"}:
            continue
        expires = row.get("expires_at")
        if expires is not None:
            if type(expires) not in {int, float} or not math.isfinite(expires):
                return "activity_unknown"
            if expires <= time.time():
                continue
        scope = row.get("scope", {})
        tid = row.get("thread_id") or (scope.get("thread_id") if isinstance(scope, dict) else None)
        if not tid:
            return "activity_unknown"
        if tid == thread.thread_id:
            return "pending_approval"
    return ""


# LLM: 禁用先返回；模型scope显式禁止旧线程模型迁移，容量及其它只读资格不写线程；政策沿同次scope冻结。
# 函数用途: 为一次空闲压缩建立只读基线，只有真正提交摘要才更新线程。
def maybe_auto_compact_idle_session(agent, store, thread, *, now=None):
    if not all(compact_idle_parameters(getattr(agent, "config", None))):
        return {"thread_id": thread.thread_id, "compacted": False, "reason": "disabled"}
    latest = store.threads.require(thread.thread_id)
    with selected_model_scope(agent, thread_id=latest.thread_id, active=False, initialize_thread=False), compact_policy_scope(agent, latest):
        return _compact_idle_candidate(agent, store, latest, now=now)


# LLM: 用户代与canonical消息指纹联合幂等；摘要前捕获、摘要中及末写重核，坏读/变化只让位不计模型失败。
# 函数用途: 确认只读安全门后沿原摘要链压缩；直接追加或同长改写也会废弃旧候选。
def _compact_idle_candidate(agent, store, thread, *, now=None):
    policy = resolve_thread_compact_policy(agent, thread)
    idle_threshold, idle_seconds = compact_idle_parameters(getattr(agent, "config", None))
    result = {"thread_id": thread.thread_id, "compacted": False}
    if not idle_threshold or not idle_seconds:
        return {**result, "reason": "disabled"}
    latest = store.threads.require(thread.thread_id)
    metadata = latest.metadata or {}
    generation = metadata.get("user_activity_generation", 0)
    timestamp = metadata.get("last_user_activity_at", latest.updated_at)
    if (type(generation) is not int or generation < 0 or type(timestamp) not in {int, float}
            or not math.isfinite(timestamp)):
        return {**result, "reason": "activity_unknown"}
    current = float(time.time() if now is None else now)
    if not math.isfinite(current):
        return {**result, "reason": "activity_unknown"}
    if current - timestamp < idle_seconds:
        return {**result, "reason": "not_idle"}
    busy = idle_compact_busy_state(agent, store, latest)
    if busy:
        return {**result, "reason": busy}
    marker, errors = store.messages.tail_marker_report(latest.thread_id)
    if errors:
        return {**result, "reason": "activity_unknown"}
    previous = metadata.get("idle_auto_compact", {})
    if (previous.get("last_idle_compacted_activity_gen") == generation and previous.get("message_tail") == marker):
        return {**result, "reason": "already_compacted"}
    usage = inspect_conversation_context(agent, store, latest)
    used, window = usage.projected_tokens, usage.context_window_tokens
    if window <= 0 or used * 100 < window * idle_threshold:
        return {**result, "reason": "below_threshold"}
    if (used * 100 >= window * policy["effective_trigger_percent"]
            and metadata.get("last_compacted_user_activity_gen") == generation
            and metadata.get("last_compacted_message_tail") == marker):
        return {**result, "reason": "regular_compacted"}

    # LLM: 摘要前后及最终提交共用活动代与完整消息指纹；坏读或变更立即让位，不占长执行车道。
    # 函数用途: 为原压缩链提供可取消检查和最终短提交检查。
    def interrupted():
        row = store.threads.require(latest.thread_id)
        current_tail, errors = store.messages.tail_marker_report(latest.thread_id)
        return (row.metadata.get("user_activity_generation", 0) != generation
                or bool(errors) or current_tail != marker
                or bool(idle_compact_busy_state(agent, store, row)))

    try:
        compacted = prepare_conversation_context(agent, store, latest, options=ConversationCompactOptions(
            force=True, idle_compact=True, expected_user_activity_generation=generation, expected_message_tail=marker,
            interrupt_check=interrupted, commit_guard=interrupted, trigger_source="idle"))
    except (IdleCompactActivityChanged, InterruptedError, ToolCancelled):
        return {**result, "reason": "activity_changed", "cas_aborted": True}
    return {**result, "compacted": bool(compacted.compacted), "reason": "compacted" if compacted.compacted else "no_source"}


# LLM: 易失游标只选规范线程，每拍按配置上限检查；不在资格检查前建立会迁移模型的scope，不新建冷Agent。
# 函数用途: 有界维护基础及已加载owner的空闲会话，坏记录只计失败，不阻挡后续线程。
def idle_compact_maintenance_tick(base_agent, *, cursor, now=None):
    pool = getattr(base_agent, "_owner_pool", None)
    candidates = [base_agent, *(pool.active_agents() if pool is not None else [])]
    agents = list({str(item.home_paths.owner_home_dir): item for item in candidates}.values())
    position = int(cursor.get("owner", 0))
    threads = cursor.setdefault("threads", {})
    report = {"checked": 0, "compacted": 0, "failed": 0}
    maximum = getattr(getattr(base_agent, "config", None), "memory_compact_idle_max_per_tick", 2)
    maximum = max(0, maximum) if type(maximum) is int else 2
    for _ in range(maximum):
        agent = agents[position % len(agents)]
        position += 1
        store = getattr(agent, "conversation_store", None)
        if store is None:
            continue
        key = str(agent.home_paths.owner_home_dir)
        files = sorted(store.storage.threads_dir.glob("thread-*.json"))
        if not files:
            continue
        previous = threads.get(key, "")
        path = next((item for item in files if item.stem > previous), files[0])
        threads[key] = path.stem
        row, error = store.threads.load_report(path.stem)
        report["checked"] += 1
        if error or row is None or row.owner_home and row.owner_home != key:
            report["failed"] += 1
            continue
        try:
            pin = pool.pin(agent, touch=False) if pool is not None and agent is not base_agent else nullcontext()
            with pin:
                outcome = maybe_auto_compact_idle_session(agent, store, row, now=now)
            report["compacted"] += bool(outcome["compacted"])
        except Exception:
            report["failed"] += 1
    cursor["owner"] = position % len(agents)
    return report
