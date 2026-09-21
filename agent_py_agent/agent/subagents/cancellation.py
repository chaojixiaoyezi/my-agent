# LLM: 子代理取消在原 creation guard 内固定整树、关闭原权限和冻结资源；清理只消费固定清单，禁止重查恢复后的树。
# 模块用途: 为模型、会话和宿主提供同一套子代理停止准备与清理，保留终态结果及部分失败。
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from ..conversation.goal_delegation import transition_delegated_goal
from ..runtime_db.operations import RuntimeConflictError
from ..runtime_db.run_cancellation import (
    RuntimeCancellationConflict,
    RuntimeCancellationTarget,
    cancel_runtime_run,
)
from ..runtime_errors import runtime_error_report
from ..tooling import process_resource_stop as process_resources
from ..tooling.process_resource_stop import FrozenProcessStop
from ..tooling.process_scope import ProcessExecutionScope
from .cancellation_hosts import (
    FrozenRunnerStop,
    cleanup_runner_stop,
    freeze_runner_stop,
    signal_runner_attempt,
)
from .model_capabilities import capability_request_requires_parent_resolution
from .models import SUBAGENT_RECOVERY_CLOSED_STATUSES, FailureType, SubAgentTask


# LLM: task 为调用方已授权的 canonical 快照；执行身份在创建锁内复核，普通进度/新增孩子不等于换代。
# 类用途: 声明要停止的原子代理及控制来源，不从自然语言推断资源范围。
@dataclass(frozen=True)
class CancelSubagentTaskRequest:
    task: SubAgentTask
    reason: str
    kill_process: bool = True
    source: str = "runtime"


# LLM: 只携带准备阶段选中的原资源和回执；不持 agent、查询回调或可扩大范围的 selector。
# 类用途: 把一个子代理已经关闭的权限及资源交给锁外清理。
@dataclass(frozen=True)
class FrozenSubagentStop:
    report: dict[str, object]
    runner: FrozenRunnerStop
    resources: FrozenProcessStop | None = None


# LLM: fixed stops 与 failures 来自同一创建事务；失败不能丢掉其它已经关闭和冻结的成员。
# 类用途: 保存一次子树停止的固定批次及准备错误，异步 worker 不再发现新孩子。
@dataclass(frozen=True)
class FrozenSubagentStops:
    stops: tuple[FrozenSubagentStop, ...] = ()
    failed: tuple[dict[str, object], ...] = ()

    # LLM: 此属性仅判断准备是否完整，不表示进程已经退出。
    # 函数用途: 让控制入口把部分冻结失败报告为未确认。
    @property
    def unconfirmed(self) -> bool:
        return bool(self.failed) or any(
            stop.resources is not None and (stop.resources.background_freeze_error or stop.resources.pty_request_error)
            for stop in self.stops
        )


# LLM: 调用方已完成 owner/父子授权；本入口再核原版本并在共同 C 内选择后代，任何耗时进程退出都在返回后。
# 函数用途: 一次固定整个停止范围，先向所有原执行轮发中断，再逐项关闭权限并冻结资源。
def prepare_subagent_stops(
    agent: object, requests: list[CancelSubagentTaskRequest], *, include_descendants: bool = True,
) -> FrozenSubagentStops:
    manager = agent.subagents
    with manager.creation_guard():
        tasks = list(manager.list_runs())
        by_id = {task.id: task for task in tasks}
        selected: dict[str, CancelSubagentTaskRequest] = {}
        for request in requests:
            current = manager.load(request.task.id)
            if _control_identity(current) != _control_identity(request.task):
                raise RuntimeConflictError("停止目标执行身份已变化，不能沿旧请求选择新的执行轮")
            ids = subtree_run_ids(tasks, current.id) if include_descendants else [current.id]
            by_id[current.id] = current
            for run_id in ids:
                selected.setdefault(run_id, CancelSubagentTaskRequest(
                    by_id[run_id], request.reason, request.kill_process, request.source,
                ))
        signals = {
            run_id: signal_runner_attempt(run_id, request.task.runner_active_attempt_id)
            for run_id, request in selected.items()
        }
        closed, failed = {}, []
        for run_id, request in selected.items():
            try:
                closed[run_id] = _close_authority(manager, request)
            except Exception as exc:
                failed.append(_failure(run_id, exc, "subagent_stop.authority"))
        stops = []
        for run_id, authority in closed.items():
            request = selected[run_id]
            runner = freeze_runner_stop(request.task, tasks, set(closed), kill_process=request.kill_process)
            report = {"run_id": run_id, "runtime_authority": authority, "thread_interrupt": signals[run_id]}
            try:
                report.update(_close_projection(agent, request, authority))
            except Exception as exc:
                failed.append(_failure(run_id, exc, "subagent_stop.projection"))
            resources = None
            if request.kill_process:
                try:
                    resources = process_resources.freeze_process_stop(ProcessExecutionScope(
                        owner_home=str(getattr(getattr(agent, "home_paths", None), "owner_home_dir", "") or ""),
                        run_id=run_id,
                    ))
                except Exception as exc:
                    failed.append(_failure(run_id, exc, "subagent_stop.resources"))
            stops.append(FrozenSubagentStop(report, runner, resources))
        return FrozenSubagentStops(tuple(stops), tuple(failed))


# LLM: 这些都是原 canonical 字段；不用进度版本作代次，也不把 projection 的空活动编号补成 later current。
# 函数用途: 比较已授权快照的亲属与执行身份，允许同轮新增孩子但拒绝停止后的换代请求。
def _control_identity(task: SubAgentTask) -> tuple:
    start = (task.attributes or {}).get("background_start") or {}
    cancel = (task.attributes or {}).get("cancel_subagents") or {}
    return (
        task.id, task.owner, task.parent_id, task.root_id, task.agent_thread_id,
        task.runner_active_attempt_id, task.runner_last_attempt_at, tuple(task.runner_abandoned_attempt_ids),
        start.get("launch_id"), start.get("attempt_id"), cancel.get("cancelled_at"),
    )


# LLM: 无 agent 参数，无法重新发现后代；逐项异常保留其它资源清理，原业务状态不由进程退出回写。
# 函数用途: 在所有控制锁外执行固定批次，并返回权限、后台、PTY 与宿主各自的真实结果。
def cleanup_subagent_stops(batch: FrozenSubagentStops) -> dict[str, object]:
    reports, failed = [], list(batch.failed)
    for stop in batch.stops:
        report = dict(stop.report)
        try:
            report["resources"] = process_resources.cleanup_process_stop(stop.resources) if stop.resources is not None else {"status": "not_requested"}
            if report["resources"].get("background_confirmed") is False:
                failed.append({
                    "run_id": report["run_id"], "cancellation_attempted": True,
                    "error_code": "BACKGROUND_STOP_UNCONFIRMED",
                })
        except Exception as exc:
            failed.append(_failure(str(report["run_id"]), exc, "subagent_stop.cleanup_resources"))
        try:
            pid_report = cleanup_runner_stop(stop.runner)
            if report["thread_interrupt"] != "not_found" or pid_report.get("status") == "no_pid":
                pid_report["thread_interrupt"] = report["thread_interrupt"]
            report["pid_report"] = pid_report
        except Exception as exc:
            failed.append(_failure(str(report["run_id"]), exc, "subagent_stop.cleanup_runner"))
        reports.append(report)
    if failed:
        logging.getLogger(__name__).warning("子代理停止存在未确认项: %s", failed)
    return {"ok": not failed and not batch.unconfirmed, "cancelled": reports, "failed": failed, "skipped": []}


# LLM: 单点宿主控制不扩大到后代；仍使用同一准备/清理边界，未托管模式不伪造数据库权限。
# 函数用途: 停止一个已授权子代理，供来源生命周期等精确宿主调用方使用。
def cancel_subagent_task(agent: object, request: CancelSubagentTaskRequest) -> dict[str, object]:
    return _cancel_selected(agent, request, include_descendants=False)


# LLM: 分支取消从同一个原快照固定整棵后代树；不在清理后重扫后来创建的孩子。
# 函数用途: 停止一个已授权子代理及其原后代，供模型和用户控制入口使用。
def cancel_subagent_tree(agent: object, request: CancelSubagentTaskRequest) -> dict[str, object]:
    return _cancel_selected(agent, request, include_descendants=True)


# LLM: 原根结果与后代结果只做投影；失败不能让工具结果声称完整停止。
# 函数用途: 将固定批次结果转换为既有单目标取消回执。
def _cancel_selected(agent: object, request: CancelSubagentTaskRequest, *, include_descendants: bool) -> dict[str, object]:
    batch = prepare_subagent_stops(agent, [request], include_descendants=include_descendants)
    return cleanup_subagent_tree(batch, request.task.id)


# LLM: 只按原根 ID 投影已经冻结的结果；资源清理期间不发现新任务，不更改业务历史。
# 函数用途: 完成一个固定分支的清理并区分原根与原后代结果。
def cleanup_subagent_tree(batch: FrozenSubagentStops, root_id: str) -> dict[str, object]:
    result = cleanup_subagent_stops(batch)
    root = next((row for row in result["cancelled"] if row["run_id"] == root_id), {"run_id": root_id})
    return {
        **root, "ok": result["ok"],
        "descendants": {"cancelled": [row for row in result["cancelled"] if row is not root],
                        "failed": result["failed"], "skipped": []},
    }


# LLM: C 内从 RuntimeDB 读取原 current；旧 canonical active 只可指向原轮或未激活 pending，数据库事务再次确认。
# 函数用途: 关闭一个任务的原执行权；文件模式固定原预留供投影撤销，已终态或 UNKNOWN 的历史保持原样。
def _close_authority(manager: object, request: CancelSubagentTaskRequest) -> dict[str, object]:
    task, repo = request.task, manager.runtime_db
    if repo is None:
        record = (task.attributes or {}).get("background_start") or {}
        return {"status": "not_managed", "run_id": task.id, "attempt_id": str(record.get("attempt_id") or "")}
    row = repo.agent_run_for_run_id(task.id)
    if row is None:
        raise RuntimeConflictError("子代理缺少执行权记录，不能确认停止")
    current_id = str(row["current_attempt_id"] or "")
    active_id = str(task.runner_active_attempt_id or "")
    pending_only = bool(active_id and active_id != current_id)
    task_run = repo.get_task_run(str(row["task_run_id"]))
    target = RuntimeCancellationTarget(str(task_run["task_id"]) if task_run else "", task.id, row["agent_run_id"], current_id)
    if pending_only:
        attempt = repo.get_attempt(current_id)
        if attempt is None or attempt["status"] != "pending" or attempt["ended_at"]:
            raise RuntimeCancellationConflict(target, "stale_attempt")
    return cancel_runtime_run(
        repo, target, reason=request.reason, source=request.source, pending_only=pending_only,
    )


# LLM: 已完成/失败或 UNKNOWN 不改业务终态和执行锁；文件接纳即便处于业务终态也须撤销，DB 事务早已退出。
# 函数用途: 暂停精确子目标并记录取消投影，清理遗留资源不会抹掉真实业务结果。
def _close_projection(agent: object, request: CancelSubagentTaskRequest, authority: dict) -> dict[str, object]:
    task = request.task
    now, closed_requests = time.time(), []
    attempt_id = str(task.runner_active_attempt_id or authority.get("attempt_id") or "")
    findings_ledger, findings_recorded = _findings_ledger_snapshot(task)
    preserved = authority["status"] not in {"cancelled", "not_managed"} or task.status in SUBAGENT_RECOVERY_CLOSED_STATUSES

    # LLM: reducer 只操作最新 canonical 行，不能从旧 task 覆盖并发字段；UNKNOWN 不经过此写路径。
    # 函数用途: 撤销原文件启动，把可取消任务收为 CANCELLED；保留业务终态和废弃轮记录。
    def close(current: SubAgentTask) -> None:
        if current.runner_active_attempt_id != task.runner_active_attempt_id:
            raise RuntimeConflictError("取消投影的执行轮发生变化")
        if agent.subagents.runtime_db is None:
            from .file_runner_start import revoke_file_runner_start

            revoke_file_runner_start(current)
        if preserved:
            return
        if attempt_id and attempt_id not in current.runner_abandoned_attempt_ids:
            current.runner_abandoned_attempt_ids.append(attempt_id)
        closed_requests.extend(_close_pending_capability_requests(current, request.reason))
        attrs = dict(current.attributes or {})
        attrs["cancel_subagents"] = {
            "cancel_status": "CANCELLED", "source": request.source, "reason": request.reason,
            "cancelled_at": now, "previous_status": str(current.status), "previous_failure_type": str(current.failure_type),
            "abandoned_attempt_id": attempt_id, "closed_capability_request_ids": closed_requests,
            "findings_ledger": findings_ledger, "findings_recorded": findings_recorded,
        }
        session = attrs.get("runner_session")
        if isinstance(session, dict):
            attrs["runner_session"] = {**session, "status": "cancelled", "heartbeat_at": now, "ended_at": now}
        current.attributes = attrs
        current.status, current.failure_type = "CANCELLED", FailureType.CANCELLED.value
        current.ended_at = current.updated_at = now
        current.runner_active_attempt_id = ""

    if not preserved or agent.subagents.runtime_db is None:
        task = agent.subagents.mutate(task.id, close)
    if not preserved:
        agent.subagents.actions._append_task_work_log(task, f"cancel_subagents: status=CANCELLED reason={request.reason}")
    transition_delegated_goal(agent.subagents, task, expected_status="active", status="paused")
    link = _sync_cancelled_conversation_link(agent, task.id) if not preserved else {"status": "preserved"}
    return {
        "status": task.status, "cancel_status": "preserved" if preserved else "CANCELLED",
        "abandoned_attempt_id": attempt_id if not preserved else "", "conversation_link": link,
        "findings_ledger": findings_ledger, "findings_recorded": findings_recorded,
    }


# LLM: 状态投影只更新既有链接，不创造新 task；异常交给批次回执，不能伪造同步完成。
# 函数用途: 让父会话的原子任务链接显示取消。
def _sync_cancelled_conversation_link(agent: object, task_id: str) -> dict[str, object]:
    store = getattr(agent, "conversation_store", None)
    if store is None:
        return {"status": "unavailable"}
    link = store.tasks.update_status({"task_id": task_id, "status": "cancelled"})
    return {"status": "updated" if link is not None else "not_linked"}


# LLM: 仅读取原任务声明的结论账地址，不以内容判定成功或产生额外产物。
# 函数用途: 在取消回执中保留已记录结论的地址和数量。
def _findings_ledger_snapshot(task: SubAgentTask) -> tuple[str, int]:
    path = str(task.agent_run_findings_jsonl or "")
    if not path:
        return "", 0
    try:
        with open(path, encoding="utf-8") as handle:
            return path, sum(1 for line in handle if line.strip())
    except OSError:
        return path, 0


# LLM: 取消仅关闭仍需父级裁决的原能力申请，不授予能力、不创建新的申请或裁决记录。
# 函数用途: 收起本次已停止任务的未决申请，避免继续等待父级处理。
def _close_pending_capability_requests(task: SubAgentTask, reason: str) -> list[str]:
    closed = []
    for request in task.capability_requests or []:
        if capability_request_requires_parent_resolution(request.status):
            request.status = "CLOSED"
            request.constraints = {**(request.constraints or {}), "denial_reason": f"subagent_cancelled: {reason}"}
            closed.append(request.id)
    return closed


# LLM: 只消费已读取的 canonical parent_id，不在遍历中重新查询，也不将展示 projection 作为亲属来源。
# 函数用途: 从同一创建事务的记录中列出原根及后代，抵御坏数据循环。
def subtree_run_ids(tasks: list[SubAgentTask], root_id: str) -> list[str]:
    by_id = {task.id: task for task in tasks}
    children: dict[str, list[str]] = {}
    for task in tasks:
        children.setdefault(task.parent_id, []).append(task.id)
    found, seen, pending = [], set(), [root_id]
    while pending:
        run_id = pending.pop()
        if run_id in seen:
            continue
        seen.add(run_id)
        if run_id in by_id:
            found.append(run_id)
        pending.extend(reversed(children.get(run_id, [])))
    return found


# LLM: 有副作用准备阶段的错误统一保留 attempted，不改写成未开始；日志本身不授予重试执行权。
# 函数用途: 为部分失败提供原 run 与结构化诊断，供控制入口和受保护日志核对。
def _failure(run_id: str, exc: Exception, context: str) -> dict[str, object]:
    return {"run_id": run_id, "cancellation_attempted": True, **runtime_error_report(exc, context=context)}
