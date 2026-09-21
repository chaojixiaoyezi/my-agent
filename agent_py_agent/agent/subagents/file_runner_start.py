# LLM: 显式无数据库模式只用原 canonical task 接纳执行轮；本模块的 reducer 必须在 creation 与 canonical 锁内调用。
# 模块用途: 让文件模式的排队启动可撤销且只消费一次，普通快照保存不能回滚启动身份。
from __future__ import annotations

import copy
import time

from ..common.id_generator import new_id
from ..runtime_db.operations import RuntimeConflictError
from .process_control import (
    BackgroundStartUpdate,
    build_background_start_record,
    reclaim_background_start,
)


# LLM: launch 与 attempt 在预留时绑定，不能把旧 pending 移交给另一个宿主；实际持久提交归调用方 mutate。
# 函数用途: 在最新任务上预留文件执行轮，重复接纳复用原编号，换代必须先关闭旧启动。
def reserve_file_runner_start(task: object, launch_id: str) -> str:
    if task.runner_active_attempt_id:
        raise RuntimeConflictError("文件执行轮仍在运行，不能再次排队")
    previous = (task.attributes or {}).get("background_start") or {}
    if previous.get("status") in {"reserved", "launching", "running"}:
        original = str(previous.get("attempt_id") or "")
        assert_file_runner_attempt(task, original, pending_only=True)
        if launch_id and previous.get("launch_id") != launch_id:
            raise RuntimeConflictError("文件预留已经绑定其它启动")
        return original
    attempt_id = new_id("attempt_id")
    task.attributes = dict(task.attributes or {})
    task.attributes["background_start"] = build_background_start_record(
        previous, BackgroundStartUpdate(launch_id or f"runner-start-{attempt_id}", "reserved",
                                        replace_launch=True, attempt_id=attempt_id),
    )
    return attempt_id


# LLM: running 不是消费证明；接纳核对状态和 activated_at，已失败宿主仍可补写同身份诊断但不能再次激活。
# 函数用途: 校验旧排队工作仍有启动资格，停止、换代或已消费时拒绝。
def assert_file_runner_attempt(task: object, expected: str, *, pending_only: bool = False) -> None:
    record = (task.attributes or {}).get("background_start") or {}
    if (not expected or record.get("attempt_id") != expected or not record.get("launch_id")
            or pending_only and record.get("status") not in {"reserved", "launching", "running"}
            or expected in task.runner_abandoned_attempt_ids):
        raise RuntimeConflictError("文件预留执行轮已失效")
    if pending_only and (float(record.get("activated_at") or 0) > 0 or task.runner_active_attempt_id):
        raise RuntimeConflictError("文件预留执行轮已被消费")


# LLM: 必须与 RUNNING/active 指针在同一次 canonical mutation 内提交；None 仅为同步入口，不接受显式空 ID。
# 函数用途: 消费原预留；直接同步启动先撤销旧排队工作，再登记自己的新身份。
def activate_file_runner_start(task: object, expected: str | None, attempt_id: str, now: float) -> None:
    if expected is None:
        if task.runner_active_attempt_id:
            raise RuntimeConflictError("文件执行轮仍在运行，不能重复启动")
        revoke_file_runner_start(task)
        task.attributes = dict(task.attributes or {})
        task.attributes["background_start"] = build_background_start_record(
            {}, BackgroundStartUpdate(f"runner-start-{attempt_id}", "reserved", attempt_id=attempt_id),
        )
    else:
        assert_file_runner_attempt(task, expected, pending_only=True)
    record = task.attributes["background_start"]
    task.attributes["background_start"] = {**record, "activated_at": now}


# LLM: 撤销接纳独立于业务终态；二次 stop 也要关闭已取消任务后来领取的 pending，不能只看 active 指针。
# 函数用途: 作废当前文件启动身份并保留原记录，避免旧工作借恢复资格重启。
def revoke_file_runner_start(task: object) -> None:
    record = (task.attributes or {}).get("background_start") or {}
    if record.get("status") not in {"reserved", "launching", "running"}:
        return
    attempt_id = str(record.get("attempt_id") or "")
    if attempt_id and attempt_id not in task.runner_abandoned_attempt_ids:
        task.runner_abandoned_attempt_ids.append(attempt_id)
    reclaim_background_start(task)


# LLM: full save 只能单调回收同一身份；预留、换代与消费独占窄 mutation，字段缺失也不能删除 canonical 事实。
# 函数用途: 合并整任务保存中的启动字段，保留新一轮与一次性消费记录。
def preserve_background_start(task: object, existing: object) -> None:
    canonical = (existing.attributes or {}).get("background_start")
    incoming = (task.attributes or {}).get("background_start") or {}
    task.attributes = dict(task.attributes or {})
    if not isinstance(canonical, dict):
        task.attributes.pop("background_start", None)
        return
    record = copy.deepcopy(canonical)
    if (incoming.get("status") == "reclaimed"
            and record.get("status") in {"reserved", "launching", "running"}
            and incoming.get("launch_id") == record.get("launch_id")
            and incoming.get("attempt_id") == record.get("attempt_id")):
        record.update(status="reclaimed", updated_at=time.time())
    task.attributes["background_start"] = record


# LLM: 预留或激活前后的旧副本都属于旧代；不能仅用结果计数判断新启动，否则旧副本可清除活动指针。
# 函数用途: 判断整任务快照是否落后于当前文件启动，供原保存边界保留新生命周期。
def file_runner_start_is_newer(task: object, existing: object) -> bool:
    record = (existing.attributes or {}).get("background_start") or {}
    incoming = (task.attributes or {}).get("background_start") or {}
    return bool(record.get("attempt_id") and (
        incoming.get("attempt_id") != record.get("attempt_id")
        or incoming.get("launch_id") != record.get("launch_id")
        or float(incoming.get("activated_at") or 0) != float(record.get("activated_at") or 0)
    ))
