# LLM: 显式宿主控制（CLI 等）只经原授权门、原 creation guard 与原停止/清理原语作用于 canonical 子代理；TaskRegistry 只是投影。
#   控制回执先于撤权落盘；暂停/放弃投影不伪造取消结果；正向清理只由原执行轮宿主的收尾写入，空的本地进程登记不是退出证明。
#   恢复只对清理已正向完成、谱系与检查点未变的同一 run 发布 PAUSED→PENDING，从不直写 RUNNING，也不放宽 conversation_user_stop。
# 模块用途: 子代理任务的暂停、放弃与恢复控制，以及启动、激活、协作、迟到回执和心跳共用的控制回执判据。
from __future__ import annotations

import time

from .models import SUBAGENT_RECOVERY_CLOSED_STATUSES, TaskStatus, task_status_in

TASK_CONTROL_ATTR = "task_control"
TASK_CONTROL_SCHEMA = "subagent_task_control.v1"
# 原 RuntimeDB 撤权事件的来源标记，区分会话/模型停止。
TASK_CONTROL_SOURCE = "task_control"
_STOP_TARGETS = {"pause": TaskStatus.PAUSED.value, "abandon": TaskStatus.ABANDONED.value}
_STOP_REASONS = {"pause": "task_control_pause", "abandon": "task_control_abandon"}
# 暂停只作用于仍在原生命周期内的 run；已结束的失败只能放弃，已关闭终态保持单调。
_PAUSABLE_STATUSES = frozenset({
    TaskStatus.PLANNING.value,
    TaskStatus.PENDING.value,
    TaskStatus.RUNNING.value,
    TaskStatus.BLOCKED.value,
})
_POSITIVE_CLEANUP_STATES = frozenset({"confirmed", "not_required"})


# LLM: code 是给控制入口映射退出码/提示的结构化原因，不从文本推断。
# 类用途: 表示一次显式控制被原 canonical 事实拒绝，任务状态未被本次控制改写。
class TaskControlError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# LLM: 只读 canonical attributes 里的回执副本；缺失或坏形状一律视为无回执，不补猜。
# 函数用途: 取出任务当前的精确控制回执。
def task_control_record(task: object) -> dict[str, object]:
    attrs = getattr(task, "attributes", None)
    record = attrs.get(TASK_CONTROL_ATTR) if isinstance(attrs, dict) else None
    return dict(record) if isinstance(record, dict) else {}


# LLM: 代次只由控制 mutation 单调递增；坏值按 0，不能让旧快照借坏值越过新代次。
# 函数用途: 读取控制回执的代次，供整份保存和恢复做精确比较。
def task_control_generation(task: object) -> int:
    try:
        return max(0, int(task_control_record(task).get("generation") or 0))
    except (TypeError, ValueError):
        return 0


# LLM: PAUSED 或已落盘但尚未投影的控制都不得进入启动预留、执行轮激活、协作选择、迟到启动回执或继续执行。
# 函数用途: 判断任务是否被显式控制挡住执行。
def task_control_blocks_execution(task: object) -> bool:
    if task_status_in(getattr(task, "status", ""), {TaskStatus.PAUSED.value}):
        return True
    return task_control_record(task).get("state") == "requested"


# LLM: 只认回执里冻结的 exact attempt；空 attempt 或别的执行轮都不能代为确认清理。
# 函数用途: 判断这个执行轮的宿主收尾是否欠一份正向清理证明。
def task_control_cleanup_pending_for(task: object, attempt_id: str) -> bool:
    cleanup = task_control_record(task).get("cleanup")
    return (
        bool(attempt_id)
        and isinstance(cleanup, dict)
        and cleanup.get("state") == "pending"
        and cleanup.get("attempt_id") == attempt_id
    )


# LLM: 只由原执行轮所在进程的 runner 会话收尾调用：业务执行已在本进程退出，才把同一执行轮的清理写成 confirmed，
#   并同次结束同一 session 句柄；回执已换代、已确认或执行轮不符时不写任何字段。
# 函数用途: 原宿主收尾消费精确控制回执，在句柄消失前持久化正向清理证明。
def confirm_task_control_cleanup(
    manager: object, run_id: str, *, attempt_id: str, session: dict[str, object], runner_status: str,
) -> bool:
    confirmed: list[bool] = []

    # LLM: reducer 只在 canonical guard 内改回执的 cleanup 与同一 session 句柄，不碰状态、结果或执行轮字段。
    # 函数用途: 对最新任务确认这一执行轮的清理。
    def confirm(current: object) -> None:
        control = task_control_record(current)
        if control.get("run_id") != getattr(current, "id", "") or not task_control_cleanup_pending_for(current, attempt_id):
            return
        now = time.time()
        attrs = dict(getattr(current, "attributes", None) or {})
        attrs[TASK_CONTROL_ATTR] = {**control, "cleanup": {
            **dict(control["cleanup"]), "state": "confirmed", "confirmed_at": now, "runner_exit": runner_status,
            "session_id": str(session.get("session_id") or ""), "worker_pid": session.get("worker_pid"),
            "worker_birth_token": str(session.get("worker_birth_token") or ""),
            "process_epoch": str(session.get("process_epoch") or ""),
        }}
        previous = attrs.get("runner_session")
        if isinstance(previous, dict) and previous.get("session_id") == session.get("session_id"):
            attrs["runner_session"] = {**previous, "status": runner_status, "heartbeat_at": now, "ended_at": now}
        current.attributes = attrs
        confirmed.append(True)

    manager.mutate(run_id, confirm)
    return bool(confirmed)


# LLM: host 是调用方按实际配置 owner/home/store/workspace 构造的原宿主；授权、回执、撤权和投影都在同一 creation guard 内，
#   冻结批次的清理在锁外执行，返回结构化回执，不打印、不吞原授权或冲突错误。
# 函数用途: 对一个子代理 run 执行显式 pause/abandon/resume 控制。
def control_task(host: object, run_id: str, *, action: str) -> dict[str, object]:
    normalized = str(action or "").strip().lower()
    if normalized == "resume":
        return _resume(host, run_id)
    if normalized not in _STOP_TARGETS:
        raise ValueError(f"unknown task control action: {action!r}")
    from .cancellation import cleanup_subagent_tree

    report, batch = _prepare_stop(host, run_id, normalized)
    if batch is None:
        return report
    # 原冻结批次只在所有控制锁外清理；本地清理结果不是原执行轮已退出的证明。
    cleanup = cleanup_subagent_tree(batch, report["run_id"])
    latest = _report(host.subagents.load(report["run_id"]), normalized, report["outcome"], ok=report["ok"],
                     failed=report.get("failed", []))
    return {**latest, "local_cleanup": cleanup}


# LLM: 授权、回执落盘与原 prepare_subagent_stops 同在一把 creation guard；只停精确 run，不扩大到后代或共享宿主。
# 函数用途: 先持久化精确控制回执，再复用原撤权/投影/资源冻结，返回回执与待锁外清理的批次。
def _prepare_stop(host: object, run_id: str, action: str):
    from ..runtime_db.operations import RuntimeConflictError
    from .authorization_gate import OperationRequest, authorize_operation
    from .cancellation import CancelSubagentTaskRequest, prepare_subagent_stops

    manager = host.subagents
    target = _STOP_TARGETS[action]
    owner = _owner_id(host)
    with manager.creation_guard():
        task = authorize_operation(manager, OperationRequest(operation="cancel", run_id=run_id, requester_owner=owner))
        if task_status_in(task.status, {target}):
            return _report(task, action, "already_applied", ok=True), None
        if not _stoppable(task, action):
            if task_status_in(task.status, SUBAGENT_RECOVERY_CLOSED_STATUSES):
                task = _settle_requested(manager, task, "preserved")
            return _report(task, action, "not_controllable", ok=False), None
        expected_attempt = str(task.runner_active_attempt_id or "")
        now = time.time()

        # LLM: 锁内复核同一执行轮仍可控制，回执只写 task_control；撤权由随后原 prepare 完成。
        # 函数用途: 把下一代精确控制回执写入最新 canonical 任务。
        def request(current: object) -> None:
            if str(current.runner_active_attempt_id or "") != expected_attempt or not _stoppable(current, action):
                raise RuntimeConflictError("任务控制目标的执行身份已变化，不能沿旧授权写回执")
            attrs = dict(current.attributes or {})
            attrs[TASK_CONTROL_ATTR] = _requested_record(current, action, owner, now)
            current.attributes = attrs

        task = manager.mutate(task.id, request)
        generation = task_control_generation(task)
        batch = prepare_subagent_stops(host, [CancelSubagentTaskRequest(
            task, _STOP_REASONS[action], source=TASK_CONTROL_SOURCE,
            projection=target, control_generation=generation,
        )], include_descendants=False)
        root = next((stop.report for stop in batch.stops if stop.report.get("run_id") == task.id), {})
        latest = manager.load(task.id)
        if root.get("cancel_status") == "preserved":
            latest = _settle_requested(manager, latest, "preserved", generation=generation)
        applied = task_status_in(latest.status, {target}) and task_control_record(latest).get("state") == "applied"
        outcome = "applied" if applied else "preserved" if root.get("cancel_status") == "preserved" else "unconfirmed"
        return _report(latest, action, outcome, ok=applied and not batch.failed, failed=list(batch.failed)), batch


# LLM: 恢复在原 creation guard 内用新鲜 resume 授权复核：精确暂停回执、同一谱系/检查点、无活动执行轮、原宿主清理已正向完成；
#   reducer 再核同一代次后才发布 PENDING，链接只按 interrupted→active 精确 CAS，交由原调度续派同一 run。
# 函数用途: 把一个已暂停且清理完成的子代理恢复为待派工。
def _resume(host: object, run_id: str) -> dict[str, object]:
    from ..conversation.goal_delegation import transition_delegated_goal
    from .authorization_gate import OperationRequest, authorize_operation

    manager = host.subagents
    owner = _owner_id(host)
    with manager.creation_guard():
        task = authorize_operation(manager, OperationRequest(operation="resume", run_id=run_id, requester_owner=owner))
        _assert_resumable(task)
        generation = task_control_generation(task)
        now = time.time()

        # LLM: 只把同一代暂停回执推进为恢复，状态写 PENDING；执行轮由原派工重新预留与激活。
        # 函数用途: 在最新 canonical 任务上发布同 run 恢复。
        def resume(current: object) -> None:
            _assert_resumable(current, generation=generation)
            control = task_control_record(current)
            attrs = dict(current.attributes or {})
            attrs[TASK_CONTROL_ATTR] = {
                **control, "generation": generation + 1, "action": "resume",
                "target_status": TaskStatus.PENDING.value, "state": "applied", "requester_owner": owner,
                "previous_status": str(current.status or ""), "resumed_from_generation": generation, "resumed_at": now,
            }
            current.attributes = attrs
            current.status = TaskStatus.PENDING.value
            current.failure_type = ""
            current.ended_at = 0.0
            current.updated_at = now

        task = manager.mutate(task.id, resume)
        link = _reopen_paused_link(host, task.id)
        transition_delegated_goal(manager, task, expected_status="paused", status="active")
        manager.actions._append_task_work_log(task, f"task_control: status=PENDING resumed_from_generation={generation}")
    return {**_report(task, "resume", "applied", ok=True), "conversation_link": link}


# LLM: 每一项都是结构化 canonical 事实；清理只认原宿主 confirmed 或撤权时本无活动执行轮的 not_required。
# 函数用途: 校验暂停任务可否恢复，不满足时抛出带原因码的拒绝。
def _assert_resumable(task: object, *, generation: int | None = None) -> None:
    record = task_control_record(task)
    if not task_status_in(getattr(task, "status", ""), {TaskStatus.PAUSED.value}):
        raise TaskControlError("TASK_NOT_PAUSED", f"只有 PAUSED 任务可以恢复，当前状态 {getattr(task, 'status', '')}")
    if (record.get("action") != "pause" or record.get("state") != "applied"
            or record.get("target_status") != TaskStatus.PAUSED.value):
        raise TaskControlError("TASK_CONTROL_MISSING", "缺少可恢复的精确暂停回执")
    if generation is not None and task_control_generation(task) != generation:
        raise TaskControlError("TASK_CONTROL_CHANGED", "暂停回执已换代，拒绝沿旧回执恢复")
    if (record.get("run_id") != getattr(task, "id", "") or record.get("lineage") != _lineage(task)
            or record.get("checkpoint") != _checkpoint(task)
            or getattr(task, "takeover_by", "") or getattr(task, "superseded_by", "")):
        raise TaskControlError("TASK_LINEAGE_CHANGED", "任务谱系或检查点已变化，不能恢复同一 run")
    cleanup = record.get("cleanup") if isinstance(record.get("cleanup"), dict) else {}
    if cleanup.get("state") not in _POSITIVE_CLEANUP_STATES:
        raise TaskControlError("TASK_CLEANUP_UNCONFIRMED", "原执行轮尚未由其宿主确认清理完成，暂不能恢复")
    if str(getattr(task, "runner_active_attempt_id", "") or ""):
        raise TaskControlError("TASK_ATTEMPT_ACTIVE", "任务仍有活动执行轮，不能恢复")


# LLM: 暂停只接原生命周期内的 run；放弃接受任何未关闭状态（含暂停与已结束失败），已关闭终态单调不改。
# 函数用途: 判断一个 canonical 任务能否接受这次停止类控制。
def _stoppable(task: object, action: str) -> bool:
    status = getattr(task, "status", "")
    if action == "pause":
        return task_status_in(status, _PAUSABLE_STATUSES)
    return not task_status_in(status, SUBAGENT_RECOVERY_CLOSED_STATUSES)


# LLM: 冻结的执行轮决定谁欠清理证明；同一执行轮已有的正向/待确认证明随换代回执沿用，不能被新回执洗掉。
# 函数用途: 生成下一代 requested 控制回执。
def _requested_record(task: object, action: str, owner: str, now: float) -> dict[str, object]:
    previous = task_control_record(task).get("cleanup")
    previous = previous if isinstance(previous, dict) else {}
    attempt_id = str(task.runner_active_attempt_id or "")
    cleanup = {"state": "pending" if attempt_id else "not_required", "attempt_id": attempt_id}
    if (previous.get("attempt_id") and previous.get("state") in {"pending", "confirmed"}
            and (not attempt_id or previous.get("attempt_id") == attempt_id)):
        cleanup = dict(previous)
    return {
        "schema_version": TASK_CONTROL_SCHEMA,
        "generation": task_control_generation(task) + 1,
        "action": action,
        "target_status": _STOP_TARGETS[action],
        "state": "requested",
        "source": TASK_CONTROL_SOURCE,
        "requester_owner": owner,
        "run_id": task.id,
        "attempt_id": attempt_id,
        "previous_status": str(task.status or ""),
        "previous_failure_type": str(task.failure_type or ""),
        "lineage": _lineage(task),
        "checkpoint": _checkpoint(task),
        "requested_at": now,
        "cleanup": cleanup,
    }


# LLM: 只收束仍是 requested 的同一代回执；控制已投影或换代时不写。
# 函数用途: 把未能生效的控制回执记为终局，解除对执行的阻挡。
def _settle_requested(manager: object, task: object, state: str, *, generation: int | None = None):
    record = task_control_record(task)
    if record.get("state") != "requested":
        return task
    expected = task_control_generation(task) if generation is None else generation

    # 函数用途: 锁内把同一代 requested 回执改成终局状态。
    def settle(current: object) -> None:
        control = task_control_record(current)
        if control.get("state") != "requested" or task_control_generation(current) != expected:
            return
        attrs = dict(current.attributes or {})
        attrs[TASK_CONTROL_ATTR] = {**control, "state": state, "settled_at": time.time()}
        current.attributes = attrs

    return manager.mutate(task.id, settle)


# 函数用途: 冻结 run 的身份与亲属，恢复时逐项比对。
def _lineage(task: object) -> dict[str, str]:
    return {name: str(getattr(task, name, "") or "") for name in (
        "id", "owner", "parent_id", "root_id", "agent_thread_id", "task_dir",
    )}


# 函数用途: 冻结 run 的原检查点定位，恢复只沿同一检查点续跑。
def _checkpoint(task: object) -> dict[str, str]:
    return {name: str(getattr(task, name, "") or "") for name in (
        "checkpoint_json", "agent_run_checkpoint_json",
    )}


# LLM: 只做 interrupted→active 精确 CAS；链接缺失或已被别处改写时如实返回，不新建链接。
# 函数用途: 恢复时让父会话的子任务链接回到活动索引。
def _reopen_paused_link(host: object, run_id: str) -> dict[str, str]:
    store = getattr(host, "conversation_store", None)
    if store is None:
        return {"status": "unavailable"}
    link = store.tasks.update_status({"task_id": run_id, "status": "active", "expected_status": "interrupted"})
    return {"status": "reopened" if link is not None else "unchanged"}


# 函数用途: 取原宿主配置的 owner 身份，供原授权门核对。
def _owner_id(host: object) -> str:
    return str(getattr(getattr(host, "home_paths", None), "owner_id", "") or "")


# 函数用途: 把控制结果整理成结构化回执。
def _report(task: object, action: str, outcome: str, *, ok: bool, **extra: object) -> dict[str, object]:
    control = task_control_record(task)
    cleanup = control.get("cleanup") if isinstance(control.get("cleanup"), dict) else {}
    return {
        "ok": ok, "action": action, "outcome": outcome,
        "run_id": str(getattr(task, "id", "") or ""), "status": str(getattr(task, "status", "") or ""),
        "control_generation": task_control_generation(task), "control_state": str(control.get("state") or ""),
        "cleanup_state": str(cleanup.get("state") or ""), **extra,
    }


__all__ = [
    "TASK_CONTROL_ATTR",
    "TaskControlError",
    "confirm_task_control_cleanup",
    "control_task",
    "task_control_blocks_execution",
    "task_control_cleanup_pending_for",
    "task_control_generation",
    "task_control_record",
]
