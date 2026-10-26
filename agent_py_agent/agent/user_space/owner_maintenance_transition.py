# LLM: 只组合原owner维护写者与原claim短门；长扫描门外，每写T→原state锁→业务锁，最终原state完整bytes CAS。
# 模块用途: 逐步运行私有保留/缓存/归档/rcb及LocalStore事件裁剪，写原maintenance.json，不创建新维护账。
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from ..common.json_io import locked_json_path, read_json_object_report
from ..common.nofollow_fs import read_bytes_beneath
from ..common.request_execution_ports import gateway_transaction_writer
from ..memory_store.retention_scan import file_fingerprint
from ..memory_store.retention_transition import RetentionPolicyChangedError
from .home_retention import apply_owner_retention

# 原maintenance source完整快照预算，单位字节；超限拒绝，不截断后冒称同一来源。
_MAINTENANCE_SOURCE_MAX_BYTES = 1_048_576


# LLM: 区分missing/空/坏文件；只读原canonical位置，返回本次CAS前像，不签权限。
# 函数用途: 读取原owner维护状态完整字节。
def _state_bytes(home):
    return read_bytes_beneath(home.owner_home_dir, ("data", "maintenance.json"),
                             max_bytes=_MAINTENANCE_SOURCE_MAX_BYTES, require_dir_fd=True)


# LLM: callback来自原C/W执行门；不改source快照，逐写同时重核原policy/state，已有局部副作用不回滚或伪造未进入。
# LLM: 新增owner孤儿伴生锁逐文件沿同一commit核policy/state；目录扫描不占T，逻辑锁不回收。
# LLM: 事件维护在T外调用，只将逐SQL/发表叶沿同一commit运输；不延长短写窗口来读JSONL或等事件锁。
# LLM: 原保留拒绝或legal_hold时不启动新的事件删除；最终写状态仍沿原CAS。
# 函数用途: 在门外扫描并沿原活scope逐步提交保留、孤儿锁及其它维护，最后写原状态。
def run_with_transition(home, now, transition, *, expected_policy_fingerprint=None, expected_state_fingerprint=None):
    from .owner_maintenance import (
        OwnerMaintenanceResult,
        _backfill_hung_runs,
        _maintain_local_store_events,
        _maintenance_state_path,
        _reclaim_text_vector_cache_orphans,
        _tighten_archive_permissions,
        _write_maintenance_result,
        owner_maintenance_due,
        reclaim_owner_orphan_lock_files,
    )

    before = _state_bytes(home)
    digest = "missing" if before is None else hashlib.sha256(before).hexdigest()
    if expected_state_fingerprint is not None and digest != expected_state_fingerprint:
        raise ValueError("original_maintenance_source_changed")
    policy = file_fingerprint(home.owner_retention_json)
    if expected_policy_fingerprint is not None and policy != expected_policy_fingerprint:
        raise RetentionPolicyChangedError("original_retention_source_changed_before_plan")
    if not owner_maintenance_due(home.owner_home_dir, now=now):
        return OwnerMaintenanceResult(False, "not_due")
    path = _maintenance_state_path(home.owner_home_dir)

    # LLM: 原transition先T再取state锁；不重新采新state/新policy以复活旧源，body只含一个原写步骤。
    # 函数用途: 在每个真实短写与最终CAS前拒绝并发来源变化。
    def commit(operation):
        # LLM: 最后核原ports活窗口再锁state；拒迟到copy_context/回调，state锁防检查后改写，目标CAS仍归原写者。
        # 函数用途: 校验本次固定来源并完成一个原维护步骤。
        def execute():
            with gateway_transaction_writer("owner_maintenance_write"), locked_json_path(path):
                if _state_bytes(home) != before:
                    raise ValueError("original_maintenance_state_changed")
                if file_fingerprint(home.owner_retention_json) != policy:
                    raise RetentionPolicyChangedError("original_retention_policy_changed")
                return operation()
        return transition(execute)

    retention = apply_owner_retention(home, now=datetime.fromtimestamp(now, timezone.utc),
        commit_transition=commit, expected_policy_fingerprint=policy)
    reclaim_owner_orphan_lock_files(home, commit_transition=commit)
    reclaimed, error = _reclaim_text_vector_cache_orphans(home, commit_transition=commit)
    permissions = _tighten_archive_permissions(home, commit_transition=commit)
    backfill = _backfill_hung_runs(home, now=now, commit_transition=commit)
    local_events = (_maintain_local_store_events(home, now=now, commit_transition=commit)
                    if retention.applied and not retention.legal_hold else
                    {"executed": False, "reason": "legal_hold" if retention.legal_hold else "retention_refused"})
    effects = {"text_vector_cache_reclaimed": reclaimed, "text_vector_cache_reclaim_error": error,
               "indexes_compacted": [], "indexes_compact_failed": [],
               "memory_archive_permissions": permissions, "run_closeout_backfill": backfill,
               "local_store_events_maintenance": local_events}

    # LLM: 外层commit已持原state锁且完成字节CAS；仍写唯一原文件，不制造已确认物理退出或新调度事实。
    # 函数用途: 用实际执行结果提交本轮原维护状态。
    def finish():
        previous = read_json_object_report(path, context="owner_maintenance.state").payload
        return _write_maintenance_result(path, retention, previous, now, effects)
    return commit(finish)
