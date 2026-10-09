# LLM: completed 只证明业务完成；仅在规范 state→record 锁内对账旧精确身份，不重新领取执行权、不重放业务。
# 模块用途: 为同 migration-id 补完释放后的诊断或确认死亡持有者的收尾，保留历史且保护后来租约。
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from ..common.json_io import locked_json_path
from .curator_state import stale_lease_reclaimable


# LLM: 规范读取/写盘失败不能冒称完成；记录可写时补失败审计，写不出只能报告并保留耐久 pending，不承诺保存不了的事实。
# 函数用途: 尝试同编号收尾对账，失败按最新记录补审计或返回保守投影。
def reconcile_completed_cleanup(store: object, directory: Path, initial: dict) -> dict:
    try:
        return _reconcile_locked(store, directory, initial)
    except Exception as exc:
        return _record_reconcile_failure(directory, initial, exc)


# LLM: state 锁覆盖读、死亡判据和精确 ID 清空，record 锁覆盖重读与审计落盘；复用原迁移格式和写入点，不另立恢复账。
# 函数用途: 锁内只结清旧租约，旧 ID 消失或替换时绝不写规范 state。
def _reconcile_locked(store: object, directory: Path, initial: dict) -> dict:
    from . import agents_migration as migration

    with locked_json_path(store.path), locked_json_path(directory / migration._RECORD_NAME):
        record = migration._read_record(directory)
        if not migration._is_completed(record) or not _matches_cleanup_identity(directory, record, initial):
            raise ValueError("completed migration identity changed")
        if not isinstance(record.get("lease_cleanup", {}), dict):
            return record
        if not record.get("lease_cleanup_pending") and not record.get("lease_cleanup"):
            return record
        state = store._load_unlocked()
        outcome = _reconciliation_outcome(record, state.active_lease)
        if outcome == "unconfirmed":
            return record
        if outcome == "dead_holder_released":
            record = _append_cleanup_fact(record, state.active_lease, "dead_holder_confirmed")
            migration.write_private_json_file_atomic_unlocked(directory / migration._RECORD_NAME, record)
        settled = _settled_record(record, state.active_lease, outcome)
        if outcome == "dead_holder_released":
            store._write_unlocked(replace(state, active_lease={}))
        migration.write_private_json_file_atomic_unlocked(directory / migration._RECORD_NAME, settled)
        return settled


# LLM: 标记停止未确认或缺精确身份时拒绝；同 ID 仍存在只接受既有唯一“持有者确定死亡”判据，不用过期或 phase 猜死亡。
# 函数用途: 将规范旧租约事实分为消失、替换、确定死亡或仍未确认四类。
def _reconciliation_outcome(record: dict, current: dict) -> str:
    detail = record.get("lease_cleanup", {})
    old_id = record.get("cleanup_lease_id")
    if not isinstance(detail, dict) or detail.get("heartbeat_stop_error_class"):
        return "unconfirmed"
    if not isinstance(old_id, str) or not old_id.strip():
        return "unconfirmed"
    if current.get("lease_id") != old_id:
        return "old_lease_replaced" if current else "old_lease_absent"
    return "dead_holder_released" if stale_lease_reclaimable(current) else "unconfirmed"


# LLM: 历史诊断不得被成功投影抹掉；构造审计必须先于 state 写入，坏历史拒绝而不是静默丢弃。
# 函数用途: 保留旧 pending/诊断及精确租约对账证据，并构造可耐久清除 pending 的记录。
def _settled_record(record: dict, current: dict, outcome: str) -> dict:
    payload = _append_cleanup_fact(record, current, outcome)
    return {**payload, "lease_cleanup_pending": False, "lease_cleanup": {}}


# LLM: 每次对账事实留在原事务记录，构造新列表而不修改旧审计；确认死亡的意图先耐久保存，再允许清空 state。
# 函数用途: 为旧精确租约追加带时间和旧诊断的收尾审计，保持当前 pending 不变。
def _append_cleanup_fact(record: dict, current: dict, outcome: str) -> dict:
    history = record.get("lease_cleanup_history", [])
    if not isinstance(history, list):
        raise ValueError("invalid migration cleanup history")
    fact = {"lease_id": record["cleanup_lease_id"], "current_lease_id": current.get("lease_id", ""),
            "outcome": outcome, "previous_pending": bool(record.get("lease_cleanup_pending")),
            "previous_diagnostic": dict(record.get("lease_cleanup", {})),
            "reconciled_at": datetime.now(timezone.utc).isoformat()}
    return {**record, "lease_cleanup_history": [*history, fact]}


# LLM: 错误投影不写原 stale 副本，普通错误只记类别不抄正文；缺失/坏诊断不造成新的类型错误。
# 函数用途: 为未完成收尾构造本次结构化失败报告。
def _failure_projection(record: dict, error: Exception) -> dict:
    previous = record.get("lease_cleanup")
    detail = dict(previous) if isinstance(previous, dict) else {}
    detail.update(lease_id=record.get("cleanup_lease_id", ""), released=False, retired=False,
                  recovery="manual_recovery_required", reconcile_error_class=type(error).__name__)
    return {**record, "lease_cleanup_pending": True, "lease_cleanup": detail}


# LLM: state 锁已退出；只取 record 锁补审计，不反向取 state 或覆盖并发诊断，记录写不出时保留原错误分类。
# 函数用途: 尽力耐久记录本次收尾失败，二次写盘错误只补报告。
def _record_reconcile_failure(directory: Path, initial: dict, error: Exception) -> dict:
    try:
        return _record_failure_locked(directory, initial, error)
    except Exception as secondary:
        result = _failure_projection(initial, error)
        result["lease_cleanup"]["diagnostic_write_error_class"] = type(secondary).__name__
        return result


# LLM: 锁内重读且核对同一 completed/旧租约身份；并发已成功结清的事实不被旧失败重新设为 pending。
# 函数用途: 在当前记录上追加失败审计；未知的当前租约不写成“已消失”。
def _record_failure_locked(directory: Path, initial: dict, error: Exception) -> dict:
    from . import agents_migration as migration

    with locked_json_path(directory / migration._RECORD_NAME):
        latest = migration._read_record(directory)
        if not migration._is_completed(latest) or not _matches_cleanup_identity(directory, latest, initial):
            return _failure_projection(initial, error)
        payload = _append_cleanup_fact(latest, {"lease_id": None}, "reconcile_failed")
        payload["lease_cleanup_history"][-1]["reconcile_error_class"] = type(error).__name__
        if latest.get("lease_cleanup_pending") or latest.get("lease_cleanup"):
            payload = _failure_projection(payload, error)
        migration.write_private_json_file_atomic_unlocked(directory / migration._RECORD_NAME, payload)
        return payload


# LLM: 锁内记录必须仍属于请求目录与原精确租约；只验 completed 不足以阻止重读前替换，晚到失败审计共用同一身份闸门。
# 函数用途: 核对目录迁移编号、初读编号和旧收尾租约身份一致。
def _matches_cleanup_identity(directory: Path, current: dict, initial: dict) -> bool:
    return directory.name == f"agents-migration-{current.get('migration_id')}" and all(
        current.get(key) == initial.get(key) for key in ("migration_id", "cleanup_lease_id"))
