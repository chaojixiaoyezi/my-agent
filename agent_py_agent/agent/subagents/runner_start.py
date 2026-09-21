# LLM: 启动接纳共用 creation guard；受管模式读 RuntimeDB，文件模式读原 canonical。标记写入归 lifecycle 服务，启动留锁外。
# 模块用途: 预留后台工作将要执行的轮次，并为激活与生命周期标记提供准确身份核对。
from __future__ import annotations

from ..runtime_db.operations import RuntimeConflictError
from .models import task_status_in


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
# 函数用途: 给排队工作绑定非空的原执行轮，文件模式与数据库模式各守唯一事实源。
def reserve_runner_start(
    manager: object, run_id: str, *, expected_attempt_id: str | None = None,
    resume_user_stop: bool = False, launch_id: str = "",
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
            record = (task.attributes or {}).get("background_start") or {}
            if manager.runtime_db is None and launch_id and record.get("launch_id") != launch_id:
                raise RuntimeConflictError("文件预留与本次启动不符")
            return expected_attempt_id
        repo = manager.runtime_db
        if repo is None:
            from .file_runner_start import reserve_file_runner_start

            reserved = manager.mutate(run_id, lambda current: reserve_file_runner_start(current, launch_id))
            return str(reserved.attributes["background_start"]["attempt_id"])
        run = repo.agent_run_for_run_id(run_id)
        if run is None:
            raise RuntimeConflictError("子代理缺少原 RuntimeDB run，不能预留执行轮")
        attempt = repo.queue_pending_attempt(str(run["agent_run_id"]), source="runner_dispatch")
        return str(attempt["attempt_id"])


# LLM: 此检查服务于短锁内接纳/标记；实际激活必须继续在 DB 事务核对，不能把本次读取当激活权。
# 函数用途: 确认收到的非空编号仍属于这个任务；实际激活在对应原子提交边界再次核对。
def assert_expected_runner_attempt(
    manager: object, run_id: str, expected_attempt_id: str, *, pending_only: bool = False,
) -> None:
    repo = manager.runtime_db
    if repo is None:
        from .file_runner_start import assert_file_runner_attempt

        assert_file_runner_attempt(manager.load(run_id), expected_attempt_id, pending_only=pending_only)
        return
    run = repo.agent_run_for_run_id(run_id)
    if not expected_attempt_id or run is None or str(run["current_attempt_id"] or "") != expected_attempt_id:
        raise RuntimeConflictError("预留执行轮已失效")
    if pending_only:
        attempt = repo.get_attempt(expected_attempt_id)
        if attempt is None or attempt["status"] != "pending" or float(attempt["ended_at"] or 0) != 0:
            raise RuntimeConflictError("预留执行轮不再等待启动")
