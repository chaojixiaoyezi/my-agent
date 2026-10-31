# LLM: owner 维护的唯一入口与状态文件 O/data/maintenance.json（schema owner-maintenance.v1，只加键、不改旧键含义）。
#   status/last_success_at 是旧汇总口径（有任何错误就 policy_unavailable）；「到底执行了没有」看 apply_outcome，
#   被隔离的路径级错误看 isolated_error_count，最近一次真正执行看 last_applied_at。改动联测 test_owner_maintenance
#   与 test_gateway_owner_maintenance。
# 模块用途: 保留Count0原维护；后台按原claim逐写私有状态及有界LocalStore事件裁剪，W不压shared索引。
from __future__ import annotations

import os
import time
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..backends.bounded_call import BoundedCallExitUnconfirmedError
from ..common.json_io import (
    locked_json_path,
    read_json_object_report,
    write_json_file_atomic_unlocked,
)
from ..common.nofollow_fs import NoFollowPathError, open_directory_beneath
from ..common.request_execution_ports import (
    RequestExecutionContextUnavailable,
    RequestExecutionOutcomeUncertainError,
)
from ..common.tool_write_context import ToolWriteContextUnavailable
from ..contracts.optimization_contracts import require_schema_generation
from .home_layout import MyAgentHomePaths
from .home_retention import OwnerRetentionPlan, apply_owner_retention, maintenance_fence

# 维护任务默认每天跑一次：过期内容清理按天为粒度足够，又不会频繁打扰 owner 目录。
_DEFAULT_INTERVAL_SECONDS = 86_400
# 配额账干净且上一轮已走完时，每拍配额对账默认最多处理的目录项数。
_QUOTA_STEADY_MAX_ENTRIES = 256
# 配额账干净且上一轮已走完时，每拍配额对账默认的扫描时间额度（毫秒）。
_QUOTA_STEADY_MAX_MS = 10
# 配额账缺失、脏或一轮未走完时的追赶额度：每拍至少处理这么多目录项，大owner在游标存活期内走完一轮。
_QUOTA_CATCH_UP_MAX_ENTRIES = 1024
# 配额追赶期每拍的扫描时间额度下限（毫秒）。
_QUOTA_CATCH_UP_MAX_MS = 40


# LLM: 只登记本owner使用<数据扩展名>.lock协议的私有目录；不递归、不包含local_users/operations或逻辑锁目录。
# 函数用途: 列出owner维护可回收伴生锁的固定目录，不创建目录。
def owner_lock_sidecar_dirs(home: MyAgentHomePaths) -> tuple[Path, ...]:
    root = home.owner_home_dir
    data_dir = getattr(home, "owner_data_dir", None) or (root / "data")
    return (
        getattr(home, "owner_memory_dir", None) or (root / "memory"),
        getattr(home, "owner_memory_daily_dir", None) or (root / "memory" / "daily"),
        getattr(home, "owner_memory_curator_runs_dir", None) or (root / "memory" / "curator" / "runs"),
        getattr(home, "owner_compact_dir", None) or (root / "compact"),
        getattr(home, "owner_cache_dir", None) or (root / "cache"),
        getattr(home, "owner_tmp_dir", None) or (root / "tmp"),
        getattr(home, "owner_scheduler_dir", None) or (root / "scheduler"),
        *(data_dir / name for name in ("decision", "context", "skill_learning", "skill_proposals")),
    )


# LLM: 从可信owner根逐段nofollow固定已登记目录FD；通用清扫器全程dirfd并排除逻辑锁，原transition逐文件核权。
# 函数用途: 回收owner陈旧孤儿伴生锁，防目录祖先替换越界，保留活锁、数据锁及逻辑锁。
def reclaim_owner_orphan_lock_files(home: MyAgentHomePaths, *, commit_transition=None) -> int:
    from ..gateway_parts.io import cleanup_orphan_lock_files

    removed = 0
    for directory in owner_lock_sidecar_dirs(home):
        try:
            relative = directory.relative_to(home.owner_home_dir)
            descriptor = open_directory_beneath(home.owner_home_dir, relative.parts, require_dir_fd=True)
            try:
                removed += cleanup_orphan_lock_files(directory, data_sidecars_only=True,
                    commit_transition=commit_transition, directory_fd=descriptor)
            finally:
                os.close(descriptor)
        except (OSError, NoFollowPathError):
            continue
    return removed


# LLM: 只给单进程本地主聊天收尾用；真实home、runner身份和原request门均检查，受限C/W即使有合法执行scope也不旁路维护。
# 函数用途: 到期才轻量触发CLI维护，子代理、受限环境或缺少准入时安静跳过。
def maybe_run_cli_owner_maintenance(agent: object, *, now: float | None = None) -> OwnerMaintenanceResult | None:
    from ..gateway_parts.request_entry_admission import (
        request_execution_required,
        require_request_execution,
    )
    from ..runtime_context import current_subagent_run_id

    home = getattr(agent, "home_paths", None)
    if not isinstance(home, MyAgentHomePaths) or current_subagent_run_id(agent) or request_execution_required():
        return None
    try:
        require_request_execution(agent=agent, home=home)
    except Exception:
        return None
    if not owner_maintenance_due(home.owner_home_dir, now=now):
        return None
    return run_owner_retention_if_due(home, now=now)


# LLM: status 保持旧口径；apply_outcome（applied / refused / legal_hold）、isolated_error_count 与 failed_action_count
#   供 Gateway 摘要区分「整次被拒」「执行期动作失败」和「执行了、只是隔离了 N 条」。没跑（not_due）时新字段为空值，
#   to_dict 也不输出它们。
# 类用途: 一次维护调用的结果：跑没跑、旧状态、执行结果、被隔离的错误条数与执行期失败的动作数。
@dataclass(frozen=True)
class OwnerMaintenanceResult:
    ran: bool
    status: str
    retention: OwnerRetentionPlan | None = None
    apply_outcome: str = ""
    isolated_error_count: int = 0
    failed_action_count: int = 0

    # LLM: 旧键原样输出；新键只在真正跑过时附加，not_due 的输出与旧版逐字相同。
    # 函数用途: 转成可打印或测试比对的字典。
    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"ran": self.ran, "status": self.status}
        if self.apply_outcome:
            payload["apply_outcome"] = self.apply_outcome
            payload["isolated_error_count"] = self.isolated_error_count
            payload["failed_action_count"] = self.failed_action_count
        if self.retention is not None:
            payload["retention"] = self.retention.to_dict()
        return payload


# LLM: 原关闭/间隔规则优先；已提交partial沿同一maintenance.json下一拍续作，不重新等待整周期。
#   建过配额账（受有限配额准入）的owner账脏或一轮未走完时同样下一拍续作对账，不让一次准入外写入拒绝增长一整天。
# 函数用途: 判断原维护是否到期，包含有游标的未完成片与待追赶的配额对账。
def owner_maintenance_due(
    owner_home: str | Path,
    *,
    now: float | None = None,
) -> bool:
    from .owner_quota import owner_quota_reconcile_pending

    home = Path(owner_home)
    policy = read_json_object_report(
        home / "retention.json",
        context="owner_maintenance.policy",
    ).payload
    if not bool(policy.get("maintenance_enabled", True)):
        return False
    interval = _positive_interval(policy.get("maintenance_interval_seconds"))
    if interval <= 0:
        return False
    marker = read_json_object_report(
        _maintenance_state_path(home),
        context="owner_maintenance.state",
    ).payload
    maintenance_fence(marker)
    maintenance_fence(marker.get('storage_maintenance', {}))
    if marker.get('maintenance_partial') is True or owner_quota_reconcile_pending(home, require_ledger=True):
        return True
    last_attempt = _timestamp(marker.get("last_attempt_at"))
    current = float(now if now is not None else time.time())
    return last_attempt <= 0 or current - last_attempt >= interval


# LLM: 最早核原请求/owner；Count0 None保原维护成本，正W不写shared；callback逐写T→业务锁，扫描门外。
#   维护状态只加键：apply_outcome / isolated_error_count / last_applied_at（旧文件缺这个键时按 0 起算）；
#   status 与 last_success_at 含义不变。写 maintenance.json 与审计属于副作用，都在 locked_json_path 内完成。
# LLM: 单进程保留执行后回收已登记孤儿伴生锁，逻辑锁和原request门不变；C/W仍用transition路径。
# LLM: LocalStore事件与派生记录只在原保留允许删除时推进，legal_hold/refused不新增删除；结果加到原effects。
# 函数用途: owner到期执行保留及辅助维护，写原结构化状态；未到期返回not_due。
def run_owner_retention_if_due(
    home: MyAgentHomePaths,
    *,
    now: float | None = None,
    include_global_indexes: bool = True,
    commit_transition=None,
    expected_policy_fingerprint=None,
    expected_state_fingerprint=None,
    quota_max_ms: float | None = None,
    quota_max_entries: int | None = None,
) -> OwnerMaintenanceResult:
    from ..gateway_parts.multiprocess_mode import current_worker_partition
    from ..gateway_parts.request_entry_admission import require_request_execution

    require_request_execution(home=home)
    include_global_indexes = include_global_indexes and current_worker_partition() is None
    current = float(now if now is not None else time.time())
    if commit_transition is not None:
        if include_global_indexes:
            raise ValueError("maintenance_transition_requires_private_indexes")
        from .owner_maintenance_transition import run_with_transition

        return run_with_transition(home, current, commit_transition,
            expected_policy_fingerprint=expected_policy_fingerprint,
            expected_state_fingerprint=expected_state_fingerprint,
            quota_max_ms=quota_max_ms, quota_max_entries=quota_max_entries)
    if not owner_maintenance_due(home.owner_home_dir, now=current):
        return OwnerMaintenanceResult(False, "not_due")
    state_path = _maintenance_state_path(home.owner_home_dir)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with locked_json_path(state_path):
        if not owner_maintenance_due(home.owner_home_dir, now=current):
            return OwnerMaintenanceResult(False, "not_due")
        previous = read_json_object_report(state_path, context="owner_maintenance.state").payload
        maintenance_fence(previous)
        maintenance_fence(previous.get('storage_maintenance', {}))
        if _quota_only_continuation_due(home.owner_home_dir, previous, current):
            return _run_quota_continuation_slice(
                state_path, home, previous, current,
                quota_max_ms=quota_max_ms, quota_max_entries=quota_max_entries)
        retention = apply_owner_retention(
            home,
            now=datetime.fromtimestamp(current, timezone.utc),
        )
        derived_prune = (_prune_local_store_derived_records(home.owner_home_dir,
                            now=datetime.fromtimestamp(current, timezone.utc))
                         if retention.applied and not retention.legal_hold and not retention.load_errors
                         else {"executed": False, "reason": "retention_not_safe"})
        reclaim_owner_orphan_lock_files(home)
        # 正文哈希缓存只是派生索引，孤儿键（换模型/迁移/绕过写入路径的改动留下）只有 index_all
        # 会回收，那是手动命令；挂进这里让它在 owner 维护（默认每天一次）里自动收口。
        dropped_cache_keys, reclaim_error = _reclaim_text_vector_cache_orphans(home)
        # global_index 四份只追加索引在这里按 key 内部压缩（纯投影，不读权威源）。
        # 单份失败不中断维护；成功与"试了但失败"分开汇总进维护状态，便于观察是否真的压过。
        compacted, compact_failed = (_compact_global_indexes(home, now=current)
                                     if include_global_indexes else ([], []))
        # memory_archive 的已有文件靠 S2/S3 的"下次写入收紧"覆盖不到，挂进这里在 owner 维护
        # （默认每天一次）里收口；只收紧不放松、不跟随符号链接，结果原样写进维护状态。
        permissions = _tighten_archive_permissions(home)
        # rcb：历史悬挂 run 一次性补收口迁移（幂等；迁移记录写 runtime.db metadata，
        # 重复调用是空操作）。失败只记结构化回执，绝不影响保留主流程。
        run_closeout_backfill = _backfill_hung_runs(home, now=current)
        local_events = (_maintain_local_store_events(home, now=current)
                        if retention.applied and not retention.legal_hold else
                        {"executed": False, "reason": "legal_hold" if retention.legal_hold else "retention_refused"})
        effects = {"text_vector_cache_reclaimed": dropped_cache_keys,
                   "derived_records_pruned": derived_prune,
                   "text_vector_cache_reclaim_error": reclaim_error,
                   "indexes_compacted": compacted, "indexes_compact_failed": compact_failed,
                   "memory_archive_permissions": permissions, "run_closeout_backfill": run_closeout_backfill,
                   "local_store_events_maintenance": local_events,
                   "storage_maintenance": run_owner_storage_maintenance_slice(home, previous, retention, now=current,
                       quota_max_ms=quota_max_ms, quota_max_entries=quota_max_entries, state_locked=True)}
        return _write_maintenance_result(state_path, retention, previous, current, effects)


# LLM: 所有既存owner库共用发现与显式owner引用保护；C逐叶沿原commit，冷库不创建、单库普通错误隔离，执行未知仍原抛。
# 函数用途: 在原保留门后裁剪全部派生库，单库回执兼容，多库分列真实结果。
def _prune_local_store_derived_records(home: Path, *, now: datetime, commit_transition=None) -> dict[str, Any]:
    from ..local_storage.records import owner_protected_ref_ids

    paths, errors = _local_store_database_paths(Path(home))
    if not paths:
        return {"executed": False, "reason": "error" if errors else "no_local_store", **({"discovery_errors": errors} if errors else {})}
    protected_refs = owner_protected_ref_ids(Path(home), now.timestamp())
    results = [_prune_derived_store(path, now=now, protected_refs=protected_refs, commit_transition=commit_transition) for path in paths]
    result = results[0] if len(results) == 1 else {
        "executed": any(item["executed"] for item in results),
        "stores": [{"database": str(path.relative_to(home)), **item} for path, item in zip(paths, results, strict=True)],
        "deleted_ids": [value for item in results for value in item.get("deleted_ids", [])]}
    if errors:
        result = {**result, "discovery_errors": errors, "counts_complete": False}
    return result


# LLM: 原Store负责TTL/引用/FTS事务；coordinator_io只选择逐末叶commit，不把长扫描包进T或缓存回调。
# 函数用途: 对一个已存在库执行带owner引用保护的派生TTL，普通故障保留可能部分写入事实。
def _prune_derived_store(db_path: Path, *, now: datetime, protected_refs=None, commit_transition=None) -> dict[str, Any]:
    try:
        from ..local_storage import LocalStore

        if commit_transition is None:
            result = LocalStore(db_path).prune_derived_records(now=now, protected_refs=protected_refs)
        else:
            from ..common.global_write_commit import global_write_commit_scope

            with global_write_commit_scope(commit_transition):
                result = LocalStore(db_path, coordinator_io=True).prune_derived_records(now=now, protected_refs=protected_refs)
        return {"executed": True, **result}
    except (RequestExecutionOutcomeUncertainError, RequestExecutionContextUnavailable,
            ToolWriteContextUnavailable, BoundedCallExitUnconfirmedError):
        raise
    except Exception as exc:
        return {"executed": False, "reason": "error", "error_type": type(exc).__name__, "side_effects_unknown": True}


# LLM: 三个固定库名与workspace一层只发现一次；事件维护显式包含JSONL-only的local.db候选，坏来源只报告一次。
# 函数用途: 共用LocalStore数据库与事件发现；默认不建库，事件恢复仍拒绝链接及特殊文件。
def _local_store_database_paths(home: Path, *, include_event_files: bool = False) -> tuple[list[Path], list[dict[str, str]]]:
    candidates = [home / name for name in ("data/local_store.sqlite3", "data/local_store/local.db", "local_store/local.db")]
    workspaces = home / "workspace" / "runtime" / "workspaces"
    result, errors = [], []
    try:
        if _real_owner_directory(home, workspaces):
            candidates.extend(path / "local_store" / "local.db" for path in sorted(workspaces.iterdir()))
    except OSError as exc:
        errors.append({"source": "workspace_scan", "error_type": type(exc).__name__})
    for path in dict.fromkeys(candidates):
        try:
            if _local_store_candidate_exists(home, path, include_event_files=include_event_files):
                result.append(path)
        except OSError as exc:
            errors.append({"source": str(path.relative_to(home)), "error_type": type(exc).__name__})
    return result, errors


# LLM: 不做扫描、不跟随父目录/叶/files链接；JSONL-only只归固定local.db候选，不能给legacy sqlite3误建新库。
# 函数用途: 核对同一候选数据库或可恢复事件文件是否真实存在；IO故障由调用者准确记一次。
def _local_store_candidate_exists(home: Path, path: Path, *, include_event_files: bool) -> bool:
    if (not _real_owner_directory(home, path.parent) or path.is_symlink()
            or (path.parent / "files").is_symlink()):
        return False
    if include_event_files:
        event_path = path.parent / "events.jsonl"
        if event_path.is_symlink() or (event_path.exists() and not event_path.is_file()):
            return False
        if path.exists() and not path.is_file():
            return False
        return path.is_file() or (path.name == "local.db" and event_path.is_file())
    return path.is_file()


# LLM: 冷owner不建runtime库；原cleanup在transition中执行，四类执行未知原抛，其余故障分列不遮住retention。
# 函数用途: 多进程维护沿原claim写门回收runtime安全欠账，不受保留到期或hold约束。
def _cleanup_owner_runtime(home: MyAgentHomePaths, *, now: float, commit_transition) -> dict[str, Any]:
    try:
        from ..runtime_db.repository import RuntimeRepository
        from ..runtime_db.schema import runtime_db_path

        path = runtime_db_path(home.owner_home_dir)
        if not _real_owner_directory(home, path.parent) or path.is_symlink() or not path.is_file():
            return {"executed": False, "reason": "no_runtime_db"}
        result = commit_transition(lambda: RuntimeRepository(path, existing_only=True).cleanup_stale_runtime(now=now))
        return {"executed": True, **result}
    except (RequestExecutionOutcomeUncertainError, RequestExecutionContextUnavailable,
            ToolWriteContextUnavailable, BoundedCallExitUnconfirmedError):
        raise
    except Exception as exc:
        return {"executed": False, "reason": "error", "error_type": type(exc).__name__, "side_effects_unknown": True}


# LLM: caller已持原maintenance锁；仍写原唯一schema/状态口径，不把局部成功变成新许可或第二账。
# 函数用途: 用原保留回执和附加维护事实更新同一个maintenance.json。
# LLM: R1先核header，未知扩展保真；partial只由已确认的领域回执派生，不能被日周期吞掉。
# 函数用途: 在原状态写者内保存扩展、来源游标及下一拍责任。
def _write_maintenance_result(state_path, retention, previous, current, effects):
    from .home_retention import maintenance_fence, require_maintenance_fence
    from .owner_quota import refresh_owner_quota_host_rows

    require_schema_generation(previous)
    expected = maintenance_fence(previous)
    canonical = read_json_object_report(state_path, context='owner_maintenance.fence').payload
    require_maintenance_fence(canonical, expected)
    fields = _outcome_fields(retention, previous, current)
    payload = {**previous, "schema_generation": 1, "maintenance_epoch": expected[1] + 1,
               "schema_version": "owner-maintenance.v1", "last_attempt_at": current, **fields,
               "load_errors": list(retention.load_errors), "legal_hold": retention.legal_hold, **effects}
    if isinstance(effects.get('storage_maintenance'), dict):
        storage = effects['storage_maintenance']
        payload['maintenance_partial'] = storage.get('continuation_pending', storage['partial'])
        payload['storage_maintenance'] = {**storage, 'maintenance_epoch': expected[1] + 1}
    write_json_file_atomic_unlocked(state_path, payload)
    # 维护状态在配额准入外写入；写完按当前内容核过data与root目录行并采纳新目录戳（锁忙或核验不过即留给准入核验）。
    refresh_owner_quota_host_rows(state_path.parent.parent, ('data', ''))
    return OwnerMaintenanceResult(True, fields["status"], retention, apply_outcome=fields["apply_outcome"],
        isolated_error_count=fields["isolated_error_count"], failed_action_count=fields["failed_action_count"])


# LLM: 维护状态里描述「这轮结果」的字段只在这里算：旧口径 status / last_success_at 含义不变；新增的 apply_outcome /
#   isolated_error_count / last_applied_at 说清执行事实（被拒或法律保留时 last_applied_at 沿用上次，旧文件按 0）。
#   从 run_owner_retention_if_due 抽出只为控制函数长度，纯计算、不做 IO。
# 函数用途: 由保留回执、上一次维护状态和本轮时刻算出状态、动作计数与执行结果字段。
def _outcome_fields(retention: OwnerRetentionPlan, previous: dict[str, Any], current: float) -> dict[str, Any]:
    status = _maintenance_status(retention)
    outcome = _apply_outcome(retention)
    return {
        "status": status,
        "last_success_at": current if status == "success" else _timestamp(previous.get("last_success_at")),
        "action_count": len(retention.actions),
        "failed_action_count": sum(
            action.status in {"failed", "collision", "state_changed"} for action in retention.actions
        ),
        # status 有路径级错误就记 policy_unavailable，会把"其实执行了"掩盖掉；下面三个键说清执行事实。
        "apply_outcome": outcome,
        "isolated_error_count": len(retention.isolated_errors),
        "last_applied_at": current if outcome == "applied" else _timestamp(previous.get("last_applied_at")),
    }


# LLM: 原callback只包构造目录与最后cache写，active集合长读门外，四类实际未知原对象上抛；Count0 None原调用不变。
#   回收只读 active 记忆算出保留集合，不加载嵌入模型、不联网；失败只记 0，绝不影响保留策略结果。
#   路径必须用 canonical 字段（owner_memory_long_term_jsonl），此前手拼 `memory/memory.jsonl` 在生产布局下
#   文件根本不存在，于是每天写一个假的 `text_vector_cache_reclaimed: 0`——看起来像"跑过、没有孤儿"。
# 函数用途: 在 owner 维护里回收正文哈希缓存中不属于任何 active 记忆的键。
def _reclaim_text_vector_cache_orphans(home: MyAgentHomePaths, *, commit_transition=None) -> tuple[int, str]:
    """返回 (回收数, 错误码)。错误码为空表示这次真的跑过；非空表示"0 不是因为没孤儿"。"""
    try:
        from ..memory_store.jsonl import JsonlMemory

        memory_path = home.owner_memory_long_term_jsonl
        if not memory_path.exists():
            return 0, "memory_file_missing"
        # 回收不需要 embedder：保留集合按 active 记录的正文哈希算（见 reclaim_text_cache_orphans）。
        memory = (JsonlMemory(memory_path) if commit_transition is None
                  else commit_transition(lambda: JsonlMemory(memory_path)))
        # 回收自己也会遇到"读不了缓存 / 拿不到锁"，它把错误说明交回来（V5），不能再被当成空串。
        if commit_transition is None:
            return memory.reclaim_text_cache_orphans()
        return memory.reclaim_text_cache_orphans(commit_transition=commit_transition)
    except (RequestExecutionOutcomeUncertainError, RequestExecutionContextUnavailable,
            ToolWriteContextUnavailable, BoundedCallExitUnconfirmedError):
        raise
    except Exception as exc:
        # 建缓存/读记忆失败也返回 0，但与"确实没有孤儿"必须分得开——否则又是一条假的结构化事实。
        return 0, f"{type(exc).__name__}: {exc}"
# LLM: 四类实际未知不能降级为空成功；Count0原派生整理故障仍保原回执。
#   压缩是派生数据的整理，失败绝不能影响 retention 结果；这里只回传计数与坏行数。
#   "试了但失败"（io_error / identity_changed）必须与"没到期"分开记：
#   只记 compacted=True 的话，maintenance.json 分不出"没到期"和"压失败了"，
#   失败会被静默吞掉——这正是本轮修掉的那类假结构化事实。
# 函数用途: 在维护事务里对四份 global_index 做"该压就压"，返回 (成功摘要, 失败摘要)。
def _compact_global_indexes(
    home: MyAgentHomePaths, *, now: float
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    try:
        from .home_index_compact import compact_global_indexes_if_due

        results = compact_global_indexes_if_due(home, now=now)
    except (RequestExecutionOutcomeUncertainError, RequestExecutionContextUnavailable,
            ToolWriteContextUnavailable, BoundedCallExitUnconfirmedError):
        raise
    except Exception:
        return [], []
    compacted: list[dict[str, object]] = []
    failed: list[dict[str, object]] = []
    for result in results:
        entry = {"path": result.path.name, **result.to_dict()}
        if result.compacted:
            compacted.append(entry)
            continue
        # "没到期"不是失败；只有真的试过却没成功的才算。
        if result.reason in _COMPACT_NOT_ATTEMPTED_REASONS:
            continue
        failed.append(entry)
    return compacted, failed


# LLM: 原callback逐chmod重核scope；四类实际未知透传，默认路径原统计/故障语义不变。
#   收紧动作本身不做 IO 之外的判断；memory_archive 不存在时返回全零（还没建归档属正常）。
#   导入或整体失败只记 error 字符串，维护状态照写，绝不让归档收紧拖垮 retention 主流程。
# 函数用途: 收紧本 owner memory_archive 目录树的权限，返回可直接写进维护状态的结构化事实。
def _tighten_archive_permissions(home: MyAgentHomePaths, *, commit_transition=None) -> dict[str, Any]:
    try:
        from ..memory_archive.storage import tighten_memory_archive_permissions

        archive = Path(home.owner_home_dir) / "memory_archive"
        if commit_transition is None:
            return tighten_memory_archive_permissions(archive)
        return tighten_memory_archive_permissions(archive, commit_transition=commit_transition)
    except (RequestExecutionOutcomeUncertainError, RequestExecutionContextUnavailable,
            ToolWriteContextUnavailable, BoundedCallExitUnconfirmedError):
        raise
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


# LLM: 这些原因表示"这次根本没动手"，不能算失败，否则维护状态天天报假失败。
# 函数用途: 判断压缩结果是否属于"没尝试"。
_COMPACT_NOT_ATTEMPTED_REASONS = frozenset(
    {"below_min_bytes", "below_growth_ratio", "in_cooldown", "missing"}
)


# LLM: 旧汇总口径，含义保持不变（持久化字段只加不改）：只要有任何错误（含已被隔离的路径级错误）就是 policy_unavailable。
#   判断是否真的执行过用 _apply_outcome，不要改这里。
# 函数用途: 给出维护状态文件里的旧 status 值。
def _maintenance_status(retention: OwnerRetentionPlan) -> str:
    if retention.legal_hold:
        return "legal_hold"
    if retention.load_errors:
        return "policy_unavailable"
    if any(action.status in {"failed", "collision"} for action in retention.actions):
        return "partial_failure"
    return "success"


# LLM: 只看 report.applied 与 legal_hold，不看错误列表：有路径级错误但执行了其余动作仍是 applied；
#   refused 与 retention.apply() 的整份拒绝是同一个判定（_has_policy_level_error，即 _POLICY_LEVEL_ERROR_CODES：
#   策略无效/读不了、候选账本读不了），这里不另造错误码集合。
# 函数用途: 给出本轮保留到底执行了没有：applied / refused / legal_hold。
def _apply_outcome(retention: OwnerRetentionPlan) -> str:
    if retention.legal_hold:
        return "legal_hold"
    return "applied" if retention.applied else "refused"


# LLM: 所有普通与transition维护写者共用此路径，不能另建epoch账本。
# 函数用途: 返回owner维护唯一状态文件位置。
def _maintenance_state_path(owner_home: Path) -> Path:
    return owner_home / "data" / "maintenance.json"


# LLM: 原callback覆盖repo构造与逐条rcb写；候选扫描门外，四类实际未知不降级或写假迁移完成。
#   rcb 迁移的维护侧入口：只接 owner home，自己定位 runtime.db（延迟导入 runtime_db，
#   保持本模块加载面不变）；无库、库坏或迁移失败都返回结构化回执（error_type），
#   绝不让迁移拖垮 retention 主流程。
# 函数用途: 在 owner 维护里对当前 owner 的 runtime.db 执行一次历史悬挂补收口迁移。
def _backfill_hung_runs(home: MyAgentHomePaths, *, now: float, commit_transition=None) -> dict[str, Any]:
    try:
        from ..runtime_db.repository import RuntimeRepository
        from ..runtime_db.run_closeout_backfill import apply_run_closeout_backfill
        from ..runtime_db.schema import runtime_db_path

        db_path = runtime_db_path(Path(home.owner_home_dir))
        if not db_path.exists():
            return {"executed": False, "reason": "no_runtime_db"}
        if commit_transition is None:
            return apply_run_closeout_backfill(RuntimeRepository(db_path), now=now)
        repo = commit_transition(lambda: RuntimeRepository(db_path))
        return apply_run_closeout_backfill(repo, now=now, commit_transition=commit_transition)
    except (RequestExecutionOutcomeUncertainError, RequestExecutionContextUnavailable,
            ToolWriteContextUnavailable, BoundedCallExitUnconfirmedError):
        raise
    except Exception as exc:  # noqa: BLE001 迁移失败不反噬保留主流程
        return {"executed": False, "reason": "error", "error_type": type(exc).__name__}


# LLM: 共用本owner单次候选发现，不再重复扫描同一workspace/DB；保留JSONL-only恢复和每库一批，四类未知原抛。
#   executed=False表示没有完整返回成功，非未写入证明；异常后的副作用未知及不完整计数须单列。
#   Home当前没有owner_local_store_dir字段，真实默认库由runtime_paths放在workspace/runtime/workspaces/*/local_store。
# 函数用途: 在原owner后台周期裁剪既有事件库，把执行或跳过事实交给同一maintenance.json。
def _maintain_local_store_events(home: MyAgentHomePaths, *, now: float, commit_transition=None) -> dict[str, Any]:
    try:
        databases, discovery_errors = _local_store_database_paths(home.owner_home_dir, include_event_files=True)
        if not databases:
            if discovery_errors:
                return {"executed": False, "reason": "error", "counts_complete": False,
                        "error_type": discovery_errors[0]["error_type"], "discovery_errors": discovery_errors}
            return {"executed": False, "reason": "no_local_store"}
        results = [_maintain_event_store(path.parent, now=now, commit_transition=commit_transition, db_path=path)
                   for path in databases]
        if len(results) == 1:
            summary = results[0]
        else:
            summary = {"executed": any(result["executed"] for result in results),
                       "ok": all(result.get("ok", False) for result in results),
                       "counts_complete": all(result.get("ok", False) for result in results),
                       "side_effects_unknown": any(result.get("side_effects_unknown", False) for result in results),
                       **{key: sum(result.get(key, 0) for result in results)
                          for key in ("selected", "deleted_db", "deleted_jsonl")},
                       "stores": [{"directory": str(path.parent.relative_to(home.owner_home_dir)),
                                   "database": str(path.relative_to(home.owner_home_dir)), **result}
                                  for path, result in zip(databases, results, strict=True)]}
        if discovery_errors:
            summary = {**summary, "ok": False, "counts_complete": False, "discovery_errors": discovery_errors}
        return summary
    except (RequestExecutionOutcomeUncertainError, RequestExecutionContextUnavailable,
            ToolWriteContextUnavailable, BoundedCallExitUnconfirmedError):
        raise
    except Exception as exc:
        return {"executed": False, "reason": "error", "error_type": type(exc).__name__}


# LLM: 普通模式沿原Store；C只临时运输当次commit，不把完整maintain包进T，不把回调留在Store或启动线程。
#   普通异常可能发生在SQL提交后，不能将未完成冒充零副作用，也不根据缺失阶段结果推算删除数。
# 函数用途: 对原库精确文件名推进一批维护，兼容目录旧调用，不误建legacy库的local.db。
def _maintain_event_store(directory: Path, *, now: float, commit_transition=None, db_path: Path | None = None) -> dict[str, Any]:
    try:
        from ..local_storage import LocalStore

        if commit_transition is None:
            result = LocalStore(db_path or directory / "local.db").maintain_events(max_batches=1, before=now)
        else:
            from ..common.global_write_commit import global_write_commit_scope

            with global_write_commit_scope(commit_transition):
                store = LocalStore(db_path or directory / "local.db", coordinator_io=True)
                result = store.maintain_events(max_batches=1, before=now)
        return {"executed": True, **result}
    except (RequestExecutionOutcomeUncertainError, RequestExecutionContextUnavailable,
            ToolWriteContextUnavailable, BoundedCallExitUnconfirmedError):
        raise
    except Exception as exc:
        return {"executed": False, "reason": "error", "error_type": type(exc).__name__,
                "side_effects_unknown": True, "counts_complete": False}


# LLM: Home为可信根，候选必须在同一owner内且每级都是真实目录；不跟随符号链接到别的owner或任意外置库。
# 函数用途: 由可信home或owner根排除目录别名、越界和缺失目录，不创建维护目标。
def _real_owner_directory(home: MyAgentHomePaths | Path, directory: Path) -> bool:
    current = Path(getattr(home, "owner_home_dir", home))
    try:
        parts = directory.relative_to(current).parts
    except ValueError:
        return False
    for part in parts:
        current = current / part
        if current.is_symlink():
            return False
    return directory.is_dir()


# LLM: 无效间隔沿原每日默认，0仍表示禁用维护；不改变原配置口径。
# 函数用途: 解析非负维护间隔。
def _positive_interval(value: object) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return _DEFAULT_INTERVAL_SECONDS
    return max(0, parsed)


# LLM: 历史缺失或非法时间按0，仅用于到期判断，不授予清理权限。
# 函数用途: 读取兼容旧状态的非负时间戳。
def _timestamp(value: object) -> float:
    try:
        return max(0.0, float(value or 0.0))
    except (TypeError, ValueError):
        return 0.0


__all__ = [
    "OwnerMaintenanceResult",
    "owner_maintenance_due",
    "run_owner_retention_if_due",
    "owner_lock_sidecar_dirs",
    "reclaim_owner_orphan_lock_files",
    "maybe_run_cli_owner_maintenance",
]


# LLM: 本函数只编排原领域已提交回执，不创建删除权威；调用方仍走原request/owner/transition并写同一maintenance.json。
# 函数用途: 按item、字节、时间预算推进登记类别，未接线与未知结果保留为partial。
def run_storage_maintenance_slice(previous: dict, adapters: dict, *, max_items: int = 512,
                                  max_bytes: int = 8 * 1024 * 1024, max_seconds: float = 0.05,
                                  should_yield=None, read_fence=None) -> dict:
    import math

    from .home_retention import maintenance_fence, require_maintenance_fence, storage_kind_registry

    require_schema_generation(previous)
    expected_fence = maintenance_fence(previous)
    if read_fence is not None:
        require_maintenance_fence(read_fence(), expected_fence)
    kinds = {kind.kind: kind for kind in storage_kind_registry()}
    if any(key not in kinds for key in adapters):
        raise ValueError('unknown_storage_kind')
    if (type(max_items) is not int or type(max_bytes) is not int
            or max_items < 0 or max_bytes < 0
            or type(max_seconds) not in (int, float) or not math.isfinite(max_seconds) or max_seconds < 0):
        raise ValueError('invalid_maintenance_budget')
    state = deepcopy(previous)
    cursors = state.setdefault('cursors', {})
    errors, completed = [], []
    missing = [key for key in kinds if not callable(adapters.get(key))]
    saved_pending = state.get('pending_kinds')
    if saved_pending is not None and (not isinstance(saved_pending, list)
            or any(key not in kinds for key in saved_pending)):
        raise ValueError('invalid_maintenance_pending_kinds')
    pending = set(saved_pending if saved_pending else adapters) & set(adapters)
    items, size = 0, 0
    deadline = time.monotonic() + max_seconds if max_seconds else None
    partial = False
    ordered = tuple(kinds)
    next_kind = state.get('next_kind', ordered[0])
    if next_kind not in kinds:
        raise ValueError('invalid_maintenance_kind_cursor')
    start = ordered.index(next_kind)
    for offset in range(len(ordered)):
        index = (start + offset) % len(ordered)
        key = ordered[index]
        adapter = adapters.get(key)
        if not callable(adapter) or key not in pending:
            continue
        if ((max_items and items >= max_items) or (max_bytes and size >= max_bytes)
                or (deadline is not None and time.monotonic() >= deadline)
                or (should_yield is not None and should_yield())):
            partial = True
            next_kind = key
            break
        budget = {'items': max_items - items if max_items else None,
                  'bytes': max_bytes - size if max_bytes else None, 'deadline': deadline}
        next_kind = ordered[(index + 1) % len(ordered)]
        try:
            if read_fence is not None:
                require_maintenance_fence(read_fence(), expected_fence)
            receipt = adapter(deepcopy(cursors.get(key, {})), budget)
            if (not isinstance(receipt, dict) or not isinstance(receipt.get('cursor'), dict)
                    or type(receipt.get('complete')) is not bool
                    or any(type(receipt.get(field)) is not int or receipt[field] < 0 for field in ('items', 'bytes'))):
                raise ValueError('invalid_storage_maintenance_receipt')
            # 领域已确认提交后才发布来源位置；若返回前失败，旧游标仍承担恢复责任。
            if read_fence is not None:
                require_maintenance_fence(read_fence(), expected_fence)
            cursors[key] = deepcopy(receipt['cursor'])
            items += receipt['items']
            size += receipt['bytes']
            if receipt['complete']:
                completed.append(key)
                pending.discard(key)
            else:
                partial = True
        except (RequestExecutionOutcomeUncertainError, RequestExecutionContextUnavailable,
                ToolWriteContextUnavailable, BoundedCallExitUnconfirmedError):
            raise
        except Exception as exc:
            code = getattr(exc, 'code', '')
            if isinstance(exc, ValueError) and exc.args == ('MAINTENANCE_FENCE_CONFLICT',):
                code = 'MAINTENANCE_FENCE_CONFLICT'
            errors.append({'kind': key, 'error_type': type(exc).__name__, 'error_code': code,
                           'side_effects_unknown': True})
            partial = True
            # 失败前可能已消耗额度；未知效果不能视为零并重新分给后续类别。
            break
    state.update({'schema_generation': 1, 'partial': partial or bool(pending) or bool(errors) or bool(missing),
                  'items': items, 'bytes': size, 'errors': errors, 'completed': completed,
                  'unbound_kinds': missing, 'next_kind': next_kind,
                  'pending_kinds': [key for key in kinds if key in pending]})
    return state


# LLM: 调用方显式给出的额度原样使用；只有已开启有限配额的owner随账本状态在稳态与追赶期之间切换。
# 函数用途: 解析并校验本拍配额对账的目录项与时间额度。
def _quota_maintenance_budget(home, quota_max_entries, quota_max_ms):
    import math

    from .owner_quota import owner_quota_enabled, owner_quota_reconcile_pending

    if quota_max_entries is None or quota_max_ms is None:
        catch_up = owner_quota_enabled(home.owner_home_dir) and owner_quota_reconcile_pending(home.owner_home_dir)
        if quota_max_entries is None:
            quota_max_entries = _QUOTA_CATCH_UP_MAX_ENTRIES if catch_up else _QUOTA_STEADY_MAX_ENTRIES
        if quota_max_ms is None:
            quota_max_ms = _QUOTA_CATCH_UP_MAX_MS if catch_up else _QUOTA_STEADY_MAX_MS
    if (type(quota_max_entries) is not int or quota_max_entries <= 0
            or type(quota_max_ms) not in (int, float) or not math.isfinite(quota_max_ms) or quota_max_ms <= 0):
        raise ValueError('invalid_quota_maintenance_budget')
    return quota_max_entries, quota_max_ms


# LLM: 配额对账只读元数据并在原配额锁内写配额账，不删除任何owner内容，因此不随保留策略不安全而停摆；
#   沿原quota锁非阻塞获取，避免maintenance→quota与既有quota→JSON锁序死锁。C/W必须经原transition提交，非C/W需已持原state锁。
# 函数用途: 在保留或回执适配被延期时仍推进一片配额对账，返回不含目录表的简短回执。
def _deferred_quota_reconciliation(home, *, coordinator, commit_transition, state_locked, max_entries, max_ms,
                                   include_cursor=False):
    from .owner_quota import (
        OwnerQuotaLockContention,
        OwnerQuotaUnavailable,
        reconcile_owner_quota_slice,
    )

    if coordinator and commit_transition is None:
        return {'complete': False, 'deferred_reason': 'coordinator_adapter_protocol_required'}
    if not coordinator and not state_locked:
        return {'complete': False, 'deferred_reason': 'retention_not_safe'}

    # 函数用途: 执行一片非阻塞配额对账。
    def operation():
        return reconcile_owner_quota_slice(home.owner_home_dir, max_entries=max_entries, max_ms=max_ms,
                                           blocking=False)
    try:
        report = commit_transition(operation) if coordinator else operation()
    except OwnerQuotaLockContention:
        return {'complete': False, 'deferred_reason': 'quota_busy'}
    except OwnerQuotaUnavailable:
        return {'complete': False, 'deferred_reason': 'quota_unavailable'}
    summary = {'complete': report['complete'], 'scanned_entries': report['scanned_entries'],
               'used_bytes': report['used_bytes'], 'inode_count': report['inode_count']}
    if include_cursor:
        summary['cursor'] = report['cursor']
    return summary


# LLM: 日周期尚未到期且已有上一拍完整保留头与storage_maintenance状态时返回True；到日周期仍走完整维护。
# 函数用途: 判断当前维护触发是否属于同一日周期内的存储/配额续扫。
def _quota_only_continuation_due(owner_home: Path, previous: dict[str, Any], current: float) -> bool:
    policy = read_json_object_report(owner_home / "retention.json", context="owner_maintenance.policy").payload
    if not bool(policy.get("maintenance_enabled", True)):
        return False
    interval = _positive_interval(policy.get("maintenance_interval_seconds"))
    last_attempt = _timestamp(previous.get("last_attempt_at"))
    if interval <= 0 or last_attempt <= 0 or current - last_attempt >= interval:
        return False
    if previous.get("apply_outcome") not in ("applied", "refused", "legal_hold"):
        return False
    return isinstance(previous.get("storage_maintenance"), dict)


# LLM: 在日周期内仅推进配额对账或剩余storage_maintenance适配器并刷新宿主目录行，不重跑apply_owner_retention与七项日维护步骤。
# 函数用途: 执行日周期内的轻量续扫片并原子更新maintenance.json。
def _run_quota_continuation_slice(state_path, home, previous, current, *, coordinator=False,
                                  commit_transition=None, runtime_cleanup=None,
                                  quota_max_ms=None, quota_max_entries=None):
    from types import SimpleNamespace

    from .home_retention import maintenance_fence, require_maintenance_fence
    from .owner_quota import owner_quota_enabled, refresh_owner_quota_host_rows

    raw_entries, raw_ms = quota_max_entries, quota_max_ms
    explicit_quota_budget = raw_entries is not None or raw_ms is not None
    track_quota = explicit_quota_budget or owner_quota_enabled(home.owner_home_dir)
    storage = deepcopy(previous.get('storage_maintenance', {}))
    require_schema_generation(previous)
    require_schema_generation(storage)
    expected = maintenance_fence(previous)
    require_maintenance_fence(storage, expected)
    quota_only = (
        previous.get('maintenance_partial') is not True
        or storage.get('deferred_reason') in ('retention_not_safe', 'coordinator_adapter_protocol_required')
        or (not storage.get('errors')
            and set(storage.get('unbound_kinds') or ()) <= {'global_indexes'}
            and set(storage.get('pending_kinds') or ()) <= {'cache_tmp_trash'})
    )
    if not quota_only:
        retention_stub = SimpleNamespace(
            load_errors=tuple(previous.get('load_errors') or ()),
            legal_hold=bool(previous.get('legal_hold', False)),
            applied=previous.get('apply_outcome') == 'applied',
        )
        storage = run_owner_storage_maintenance_slice(
            home, previous, retention_stub, now=current, coordinator=coordinator,
            quota_max_ms=raw_ms, quota_max_entries=raw_entries,
            state_locked=not coordinator, commit_transition=commit_transition)
        continuation = bool(storage.get('continuation_pending', storage.get('partial', False)))
        storage = {**storage, 'maintenance_epoch': expected[1] + 1}
    else:
        quota_max_entries, quota_max_ms = _quota_maintenance_budget(home, raw_entries, raw_ms)
        report = _deferred_quota_reconciliation(
            home, coordinator=coordinator, commit_transition=commit_transition,
            state_locked=not coordinator, max_entries=quota_max_entries, max_ms=quota_max_ms,
            include_cursor=not storage.get('deferred_reason'))
        continuation = (track_quota and report['complete'] is False
                        and report.get('deferred_reason') not in (
                            'coordinator_adapter_protocol_required', 'retention_not_safe'))
        if storage.get('deferred_reason'):
            storage.update(maintenance_epoch=expected[1] + 1, continuation_pending=continuation,
                           quota_reconciliation=report)
        else:
            cursor = report.pop('cursor', None)
            cursors = storage.setdefault('cursors', {})
            if isinstance(cursor, dict):
                cursors['cache_tmp_trash'] = cursor
            elif 'deferred_reason' in report:
                cursors['cache_tmp_trash'] = {**cursors.get('cache_tmp_trash', {}), 'complete': False,
                                              'deferred_reason': report['deferred_reason']}
            pending = [key for key in storage.get('pending_kinds') or () if key != 'cache_tmp_trash']
            if not report['complete']:
                pending.append('cache_tmp_trash')
            storage.update(maintenance_epoch=expected[1] + 1, items=int(report.get('scanned_entries', 0)), bytes=0,
                           completed=['cache_tmp_trash'] if report['complete'] else [],
                           pending_kinds=pending,
                           partial=bool(pending) or bool(storage.get('errors')) or bool(storage.get('unbound_kinds')),
                           continuation_pending=continuation)

    # 函数用途: 校验维护CAS前像并写入配额续扫后的维护状态。
    def finish():
        canonical = read_json_object_report(state_path, context='owner_maintenance.fence').payload
        require_maintenance_fence(canonical, expected)
        payload = {**previous, 'schema_generation': 1, 'maintenance_epoch': expected[1] + 1,
                   'schema_version': 'owner-maintenance.v1', 'maintenance_partial': continuation,
                   'storage_maintenance': storage}
        if runtime_cleanup is not None:
            payload['runtime_cleanup'] = runtime_cleanup
        write_json_file_atomic_unlocked(state_path, payload)
        refresh_owner_quota_host_rows(state_path.parent.parent, ('data', ''))
        return OwnerMaintenanceResult(
            True, str(previous.get('status') or 'success'), None,
            apply_outcome=str(previous.get('apply_outcome') or ''),
            isolated_error_count=int(previous.get('isolated_error_count') or 0),
            failed_action_count=int(previous.get('failed_action_count') or 0))

    return commit_transition(finish) if coordinator else finish()


# LLM: 原维护入口与唯一状态承接真实领域回执；C/W尚无逐写适配协议时只报告延期，不旁路原transition。
#   默认只发布副本，依赖原件存在性的读口切换未闭合前禁止删除；未知kind保持partial但不制造忙循环。
#   延期路径仍推进一片配额对账（C/W经原transition，普通维护经已持state锁），已开启有限配额或显式传入配额额度时，
#   一轮未走完保留待续；配额关闭的owner不因对账未完把维护标成每拍待续。
# 函数用途: 在普通维护周期分批核配额并安全封存旧回执，明确保留未接线和延期责任。
def run_owner_storage_maintenance_slice(home, previous, retention, *, now, coordinator=False,
                                        quota_max_ms=None, quota_max_entries=None, state_locked=False,
                                        commit_transition=None):
    from ..gateway_parts.owner_retention import seal_gateway_receipts_slice
    from ..gateway_parts.paths import gateway_paths_from_root
    from .home_retention import (
        maintenance_fence,
        preservation_receipt,
        require_maintenance_fence,
        storage_kind_registry,
    )
    from .owner_quota import (
        OwnerQuotaLockContention,
        owner_quota_enabled,
        reconcile_owner_quota_slice,
    )
    from .runtime_paths import runtime_paths_for_agent

    raw_entries, raw_ms = quota_max_entries, quota_max_ms
    track_quota = raw_entries is not None or raw_ms is not None or owner_quota_enabled(home.owner_home_dir)
    quota_max_entries, quota_max_ms = _quota_maintenance_budget(home, quota_max_entries, quota_max_ms)
    original = previous.get('storage_maintenance', {})
    require_schema_generation(original)
    if coordinator or retention.load_errors or (not retention.applied and not retention.legal_hold):
        state = run_storage_maintenance_slice(original, {})
        reconciliation = _deferred_quota_reconciliation(
            home, coordinator=coordinator, commit_transition=commit_transition,
            state_locked=state_locked, max_entries=quota_max_entries, max_ms=quota_max_ms)
        state.update(continuation_pending=track_quota and reconciliation['complete'] is False and reconciliation.get(
                         'deferred_reason') not in ('coordinator_adapter_protocol_required', 'retention_not_safe'),
                     deletion_enabled=False, quota_reconciliation=reconciliation,
                     deferred_reason='coordinator_adapter_protocol_required' if coordinator else 'retention_not_safe')
        return state
    if not state_locked:
        with locked_json_path(_maintenance_state_path(home.owner_home_dir)):
            return run_owner_storage_maintenance_slice(home, previous, retention, now=now,
                quota_max_ms=raw_ms, quota_max_entries=raw_entries, state_locked=True)

    expected = maintenance_fence(previous)

    # LLM: caller已持原maintenance锁；每次读取原canonical文件二元组，不将旧游标充当租约。
    # 函数用途: 给统一框架提供动作前/提交前的实时CAS来源。
    def read_fence():
        return read_json_object_report(_maintenance_state_path(home.owner_home_dir),
                                      context='owner_maintenance.adapter_fence').payload

    require_maintenance_fence(read_fence(), expected)
    root = runtime_paths_for_agent(None, home.owner_home_dir, home)['gateway_workspace']
    paths = gateway_paths_from_root(root)

    # LLM: 只扫描元数据，沿原quota锁非阻塞获取，避免maintenance→quota与既有quota→JSON锁序死锁。
    # 函数用途: 把有界配额对账的真实位置和完成事实交给原维护状态。
    def quota_step(cursor, budget):
        remaining_ms = max(0.001, (budget['deadline'] - time.monotonic()) * 1000)
        try:
            report = reconcile_owner_quota_slice(home.owner_home_dir, max_entries=min(quota_max_entries, budget['items']),
                                                 max_ms=min(quota_max_ms, remaining_ms), blocking=False)
        except OwnerQuotaLockContention:
            return {'cursor': {**cursor, 'complete': False, 'deferred_reason': 'quota_busy'},
                    'complete': False, 'items': 0, 'bytes': 0}
        return {'cursor': report['cursor'], 'complete': report['complete'],
                'items': report['scanned_entries'], 'bytes': 0}

    # LLM: legal_hold不发布或删除回执；canonical根必须在当前owner且祖先均为真实目录，冷owner不创建Gateway。
    # 函数用途: 在剩余预算内封存已结算旧终态，保留所有松散原件供未迁移读者使用。
    def receipt_step(cursor, budget):
        if retention.legal_hold:
            return {'cursor': {**cursor, 'complete': True, 'deferred_reason': 'legal_hold'},
                    'complete': True, 'items': 0, 'bytes': 0}
        if not _real_owner_directory(home, paths.terminal):
            return {'cursor': {**cursor, 'complete': True, 'deferred_reason': 'no_safe_gateway_terminal'},
                    'complete': True, 'items': 0, 'bytes': 0}
        remaining_ms = max(0.001, (budget['deadline'] - time.monotonic()) * 1000)
        report = seal_gateway_receipts_slice(paths, before=now - 7 * 86400, max_entries=budget['items'],
                                            max_bytes=budget['bytes'], max_ms=min(25, remaining_ms), remove_loose=False)
        position = {'complete': report['complete'], 'errors': report['errors'],
                    'reader_cutover_required': True, 'archived_requests': report['archived_requests']}
        return {'cursor': position, 'complete': report['complete'],
                'items': report['scanned_entries'], 'bytes': report['bytes']}

    # LLM: 每种原领域均显式绑定；除两个实际写适配器外只核规范锚点并保留，不复制领域清理器。
    # 函数用途: 固定本类别元数据，避免闭包误用最后一个类别。
    def preserve_adapter(kind):
        # LLM: 不写领域存储；完整回执只表示本拍保留核验，不表示正文或引用已全量验证。
        # 函数用途: 在统一预算及CAS保护下执行一次只读锚点核验。
        def step(cursor, budget):
            return preservation_receipt(kind, home, cursor, budget)
        return step

    adapters = {kind.kind: preserve_adapter(kind) for kind in storage_kind_registry()
                if kind.kind not in {'cache_tmp_trash', 'gateway_terminal'}}
    adapters.update(cache_tmp_trash=quota_step, gateway_terminal=receipt_step)
    original = {**original, 'schema_generation': expected[0], 'maintenance_epoch': expected[1]}
    if not original.get('continuation_pending', False):
        original.pop('pending_kinds', None)
    state = run_storage_maintenance_slice(original, adapters, max_items=quota_max_entries + len(adapters),
        max_seconds=max(0.1, quota_max_ms / 1000 + 0.05), read_fence=read_fence)
    state['coordinator_deferred'] = ['global_indexes']
    state['continuation_pending'] = bool(state['errors']) or any(
        key != 'global_indexes' and (key != 'cache_tmp_trash' or track_quota)
        for key in state['pending_kinds'])
    state['deletion_enabled'] = False
    return state
