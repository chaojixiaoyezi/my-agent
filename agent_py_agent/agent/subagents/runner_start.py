# LLM: 启动接纳与标记共用原 creation guard 和 RuntimeDB pending；不建立第二份执行权，调用方在锁外启动进程。
# 模块用途: 固定后台工作将要执行的轮次，并阻止旧启动回执覆盖停止或换代后的任务。
from __future__ import annotations

from ..runtime_db.operations import RuntimeConflictError
from .models import FailureType, TaskStatus, task_status_in
from .process_control import BackgroundStartUpdate, build_background_start_record


# LLM: 调用方持 creation guard；这里只确认原启动接纳尚属当前执行轮，不把启动回执当进程存活证明。
# 函数用途: 重复创建或投递时复用已有后台接纳，避免为同一轮再开线程。
def existing_runner_launch(manager: object, run_id: str, expected_attempt_id: str | None) -> bool:
    task = manager.load(run_id)
    record = (task.attributes or {}).get("background_start") or {}
    if (task_status_in(task.status, {"DONE", "CANCELLED", "ABANDONED", "TAKEN_OVER"})
            or not record.get("launch_id") or record.get("status") not in {"launching", "running"}):
        return False
    original = str(record.get("attempt_id") or "")
    if expected_attempt_id is not None and expected_attempt_id != original:
        raise RuntimeConflictError("已接纳启动与请求执行轮不符")
    try:
        assert_expected_runner_attempt(manager, run_id, original)
    except RuntimeConflictError:
        return False
    if manager.runtime_db is not None:
        attempt = manager.runtime_db.get_attempt(original)
        return bool(attempt and attempt["status"] in {"pending", "running"} and not attempt["ended_at"])
    return True


# LLM: supplied ID 是宿主冻结输入，不能改领 current；无输入的正常派工在锁内预留 pending，明确恢复才可接纳 user stop。
# 函数用途: 给排队工作绑定原执行轮；无数据库的显式模式用空 ID 表示，不伪造数据库身份。
def reserve_runner_start(
    manager: object, run_id: str, *, expected_attempt_id: str | None = None,
    resume_user_stop: bool = False,
) -> str:
    from .recovery_eligibility import user_stopped_run_is_resumable
    from .services.lifecycle_runner_attempts import _assert_runner_attempt_start_allowed

    with manager.creation_guard():
        task = manager.load(run_id)
        _assert_runner_attempt_start_allowed(
            task, resumable_user_stop=resume_user_stop and user_stopped_run_is_resumable(task),
        )
        if expected_attempt_id is not None:
            assert_expected_runner_attempt(manager, run_id, expected_attempt_id, pending_only=True)
            return expected_attempt_id
        repo = manager.runtime_db
        if repo is None:
            return ""
        run = repo.agent_run_for_run_id(run_id)
        if run is None:
            raise RuntimeConflictError("子代理缺少原 RuntimeDB run，不能预留执行轮")
        attempt = repo.queue_pending_attempt(str(run["agent_run_id"]), source="runner_dispatch")
        return str(attempt["attempt_id"])


# LLM: 此检查服务于短锁内接纳/标记；实际激活必须继续在 DB 事务核对，不能把本次读取当激活权。
# 函数用途: 确认收到的编号仍属于这个任务，空编号仅在显式无数据库模式可用。
def assert_expected_runner_attempt(
    manager: object, run_id: str, expected_attempt_id: str, *, pending_only: bool = False,
) -> None:
    repo = manager.runtime_db
    if repo is None:
        if expected_attempt_id:
            raise RuntimeConflictError("无数据库模式不能消费数据库执行轮身份")
        return
    run = repo.agent_run_for_run_id(run_id)
    if not expected_attempt_id or run is None or str(run["current_attempt_id"] or "") != expected_attempt_id:
        raise RuntimeConflictError("预留执行轮已失效")
    if pending_only:
        attempt = repo.get_attempt(expected_attempt_id)
        if attempt is None or attempt["status"] != "pending" or float(attempt["ended_at"] or 0) != 0:
            raise RuntimeConflictError("预留执行轮不再等待启动")


# LLM: 条件比较与窄 mutation 同在 creation guard；launch/attempt/status 三者匹配才写，停止后的回执不得重新标 running。
# 函数用途: 两种后台执行方式共用一个启动记录写入口，保留新任务字段和新进程身份。
def update_background_start(
    manager: object, run_id: str, update: BackgroundStartUpdate, *, channel_failure: bool = False,
) -> None:
    with manager.creation_guard():
        assert_expected_runner_attempt(manager, run_id, update.attempt_id, pending_only=update.replace_launch)

        # LLM: reducer 只读锁内最新 canonical 任务；抛错时不写文件，不能恢复旧 task 整体快照。
        # 函数用途: 原子校验这份回执的归属，并合并它负责的启动字段。
        def apply(task):
            previous = (task.attributes or {}).get("background_start") or {}
            if task_status_in(task.status, {"CANCELLED", "ABANDONED", "TAKEN_OVER"}):
                raise RuntimeConflictError("任务已关闭，拒绝旧启动回执")
            if update.replace_launch:
                if previous.get("status") in {"launching", "running"}:
                    raise RuntimeConflictError("已有启动尚未回收，拒绝重复接纳")
            elif (
                previous.get("launch_id") != update.launch_id
                or previous.get("attempt_id", "") != update.attempt_id
                or previous.get("status") == "reclaimed"
                or previous.get("status") in {"finished", "failed"} and update.status == "running"
            ):
                raise RuntimeConflictError("后台启动回执已失效")
            task.attributes = dict(task.attributes or {})
            task.attributes["background_start"] = build_background_start_record(previous, update)
            if channel_failure:
                task.status = TaskStatus.CHANNEL_ERROR.value
                task.channel_status = "BROKEN"
                task.failure_type = FailureType.BACKGROUND_DISPATCH_STARTUP.value
                task.result = update.error

        manager.mutate(run_id, apply)
