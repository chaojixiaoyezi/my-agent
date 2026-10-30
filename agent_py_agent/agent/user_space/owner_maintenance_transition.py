# LLM: 只组合原owner维护写者与原claim短门；长扫描门外，每写T→原state锁→业务锁，最终原state完整bytes CAS。
# 模块用途: 逐步运行私有保留/缓存/归档/rcb、runtime安全清理及LocalStore维护，写原maintenance.json。
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from ..common.json_io import locked_json_path, read_json_object_report
from ..common.nofollow_fs import read_bytes_beneath
from ..common.request_execution_ports import gateway_transaction_writer
from ..memory_store.retention_scan import file_fingerprint
from ..memory_store.retention_transition import RetentionPolicyChangedError
from .home_retention import apply_owner_retention, maintenance_fence

# 原maintenance source完整快照预算，单位字节；超限拒绝，不截断后冒称同一来源。
_MAINTENANCE_SOURCE_MAX_BYTES = 1_048_576


# LLM: 区分missing/空/坏文件；只读原canonical位置，返回本次CAS前像，不签权限。
# 函数用途: 读取原owner维护状态完整字节。
def _state_bytes(home):
    return read_bytes_beneath(home.owner_home_dir, ("data", "maintenance.json"),
                             max_bytes=_MAINTENANCE_SOURCE_MAX_BYTES, require_dir_fd=True)


# LLM: callback来自原C/W执行门；逐写重核原policy/state，已有局部副作用不回滚或伪造未进入。
# LLM: TTL/事件删除仍在原保留安全门后；runtime安全清理沿commit执行，不被retention未到期或hold推迟。
# 函数用途: 沿原活scope逐步提交维护并最终CAS同一状态，不新增维护账或权限来源。
# LLM: 在runtime清理等任何领域动作前核准确CAS前像的代际及epoch，不能等最终写回才拒绝坏维护头。
# 函数用途: 先验证原状态版本，再沿原逐写门运行已有维护。
def run_with_transition(home, now, transition, *, expected_policy_fingerprint=None, expected_state_fingerprint=None):
    from .owner_maintenance import (
        OwnerMaintenanceResult,
        _backfill_hung_runs,
        _cleanup_owner_runtime,
        _maintain_local_store_events,
        _maintenance_state_path,
        _prune_local_store_derived_records,
        _reclaim_text_vector_cache_orphans,
        _tighten_archive_permissions,
        _write_maintenance_result,
        owner_maintenance_due,
        reclaim_owner_orphan_lock_files,
        run_owner_storage_maintenance_slice,
    )

    before = _state_bytes(home)
    header = json.loads(before) if before else {}
    maintenance_fence(header)
    maintenance_fence(header.get('storage_maintenance', {}))
    digest = "missing" if before is None else hashlib.sha256(before).hexdigest()
    if expected_state_fingerprint is not None and digest != expected_state_fingerprint:
        raise ValueError("original_maintenance_source_changed")
    policy = file_fingerprint(home.owner_retention_json)
    if expected_policy_fingerprint is not None and policy != expected_policy_fingerprint:
        raise RetentionPolicyChangedError("original_retention_source_changed_before_plan")
    path = _maintenance_state_path(home.owner_home_dir)

    # LLM: 原transition先T再取state锁；不重新采新state/policy复活旧源，只运输一个原维护步骤。
    # 函数用途: 每个真实写步骤与最终CAS前拒绝并发来源变化。
    def commit(operation):
        # LLM: 最后核原ports活窗口再锁state，防迟到回调和检查后改写；目标CAS仍归原写者。
        # 函数用途: 校验本次固定来源并完成一个原维护步骤。
        def execute():
            with gateway_transaction_writer("owner_maintenance_write"), locked_json_path(path):
                if _state_bytes(home) != before:
                    raise ValueError("original_maintenance_state_changed")
                if file_fingerprint(home.owner_retention_json) != policy:
                    raise RetentionPolicyChangedError("original_retention_policy_changed")
                return operation()
        return transition(execute)

    runtime_cleanup = _cleanup_owner_runtime(home, now=now, commit_transition=commit)
    if not owner_maintenance_due(home.owner_home_dir, now=now):
        return OwnerMaintenanceResult(False, "not_due")
    retention = apply_owner_retention(home, now=datetime.fromtimestamp(now, timezone.utc),
        commit_transition=commit, expected_policy_fingerprint=policy)
    derived_prune = (_prune_local_store_derived_records(home.owner_home_dir,
                         now=datetime.fromtimestamp(now, timezone.utc), commit_transition=commit)
                     if retention.applied and not retention.legal_hold and not retention.load_errors
                     else {"executed": False, "reason": "retention_not_safe"})
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
               "local_store_events_maintenance": local_events, "derived_records_pruned": derived_prune,
               "runtime_cleanup": runtime_cleanup,
               "storage_maintenance": run_owner_storage_maintenance_slice(
                   home, json.loads(before) if before else {}, retention, now=now, coordinator=True)}

    # LLM: 外层commit已持原state锁且完成字节CAS；仍写唯一原文件，不制造已确认物理退出或新调度事实。
    # 函数用途: 用实际执行结果提交本轮原维护状态。
    def finish():
        previous = read_json_object_report(path, context="owner_maintenance.state").payload
        return _write_maintenance_result(path, retention, previous, now, effects)
    return commit(finish)
