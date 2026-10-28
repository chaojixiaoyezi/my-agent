# LLM: 闲置维护只复用原摘要/检查点/线程CAS；不占长前台车道，活动变化让位且不计模型失败。
# 模块用途: 核验用户静默、容量、运行资源和活动代后执行一次低优先级闲置压缩。
from __future__ import annotations

import math
import time
from contextlib import nullcontext

from ..common.cancellation import ToolCancelled
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


# LLM: 只读规范用户登记/claim/子代理状态/进程事实；未知观察失败关闭，状态正文不参与裁决。
# 函数用途: 检查同会话是否还有前台、子代理或托管进程，未知归属的活动子代理保守让位。
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
        runs = agent.subagents.list_runs()
        for run in runs:
            status = getattr(run, "status", "")
            status = getattr(status, "value", status)
            if status not in {"RUNNING", "PENDING", "PLANNING"}:
                continue
            run_thread = str(getattr(run, "conversation_thread_id", "") or getattr(run, "thread_id", ""))
            if not run_thread or run_thread == thread.thread_id:
                return "subagent_active"
        report = list_background_processes(agent.home_paths.owner_home_dir, thread_id=thread.thread_id, active_only=True)
        if not report["projection_ok"]:
            return "activity_unknown"
        if session_background_processes(report["items"], thread.thread_id):
            return "background_process_active"
    except (AttributeError, OSError, ValueError, TypeError):
        return "activity_unknown"
    return ""


# LLM: 先取最新规范线程，选择其模型并冻结本次政策；Gateway只依赖会话/设置层，不反向依赖agent_core。
# 函数用途: 为一次空闲压缩建立同源基线和操作范围，退出恢复原设置。
def maybe_auto_compact_idle_session(agent, store, thread, *, now=None):
    latest = store.threads.require(thread.thread_id)
    with selected_model_scope(agent, thread_id=latest.thread_id), compact_policy_scope(agent, latest):
        return _compact_idle_candidate(agent, store, latest, now=now)


# LLM: 用户时间仅缺失时回退updated_at，不从后台更新时间推断用户发言；策略来自本次冻结范围。
# 函数用途: 确认五道安全门后沿原压缩链尝试一次，插话或忙碌时丢弃旧候选。
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
    if metadata.get("last_idle_compacted_activity_gen") == generation:
        return {**result, "reason": "already_compacted"}
    current = float(time.time() if now is None else now)
    if not math.isfinite(current):
        return {**result, "reason": "activity_unknown"}
    if current - timestamp < idle_seconds:
        return {**result, "reason": "not_idle"}
    busy = idle_compact_busy_state(agent, store, latest)
    if busy:
        return {**result, "reason": busy}
    usage = inspect_conversation_context(agent, store, latest)
    used, window = usage.projected_tokens, usage.context_window_tokens
    if window <= 0 or used * 100 < window * idle_threshold:
        return {**result, "reason": "below_threshold"}
    if used * 100 >= window * policy["effective_trigger_percent"] and metadata.get("last_compacted_user_activity_gen") == generation:
        return {**result, "reason": "regular_compacted"}

    # LLM: 摘要扫描/请求前后及最终提交共用同一活动观察；新用户代次或前台出现立即让位，不占长执行车道。
    # 函数用途: 为原压缩链提供可取消检查和最终短提交检查。
    def interrupted():
        row = store.threads.require(latest.thread_id)
        return (row.metadata.get("user_activity_generation", 0) != generation
                or bool(idle_compact_busy_state(agent, store, row)))

    try:
        compacted = prepare_conversation_context(agent, store, latest, options=ConversationCompactOptions(
            force=True, idle_compact=True, expected_user_activity_generation=generation,
            interrupt_check=interrupted, commit_guard=interrupted, trigger_source="idle"))
    except (IdleCompactActivityChanged, InterruptedError, ToolCancelled):
        return {**result, "reason": "activity_changed"}
    return {**result, "compacted": bool(compacted.compacted), "reason": "compacted" if compacted.compacted else "no_source"}


# LLM: 易失游标只选下一条规范线程文件；每拍最多检查两条，owner轮换，不新建冷Agent或刷新池空闲时间。
# 函数用途: 有界维护基础及已加载owner的空闲会话，坏记录只计失败，不阻挡后续线程。
def idle_compact_maintenance_tick(base_agent, *, cursor, now=None):
    from ..settings.model_scope import selected_model_scope

    pool = getattr(base_agent, "_owner_pool", None)
    candidates = [base_agent, *(pool.active_agents() if pool is not None else [])]
    agents = list({str(item.home_paths.owner_home_dir): item for item in candidates}.values())
    position = int(cursor.get("owner", 0))
    threads = cursor.setdefault("threads", {})
    report = {"checked": 0, "compacted": 0, "failed": 0}
    for _ in range(2):
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
            with pin, selected_model_scope(agent, thread_id=row.thread_id):
                outcome = maybe_auto_compact_idle_session(agent, store, row, now=now)
            report["compacted"] += bool(outcome["compacted"])
        except Exception:
            report["failed"] += 1
    cursor["owner"] = position % len(agents)
    return report
