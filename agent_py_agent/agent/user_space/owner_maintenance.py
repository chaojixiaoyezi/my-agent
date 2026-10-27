# LLM: owner 维护的唯一入口与状态文件 O/data/maintenance.json（schema owner-maintenance.v1，只加键、不改旧键含义）。
#   status/last_success_at 是旧汇总口径（有任何错误就 policy_unavailable）；「到底执行了没有」看 apply_outcome，
#   被隔离的路径级错误看 isolated_error_count，最近一次真正执行看 last_applied_at。改动联测 test_owner_maintenance
#   与 test_gateway_owner_maintenance。
# 模块用途: 保留Count0原维护；后台按原claim逐写私有状态及有界LocalStore事件裁剪，W不压shared索引。
from __future__ import annotations

import os
import time
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
from .home_layout import MyAgentHomePaths
from .home_retention import OwnerRetentionPlan, apply_owner_retention

# 维护任务默认每天跑一次：过期内容清理按天为粒度足够，又不会频繁打扰 owner 目录。
_DEFAULT_INTERVAL_SECONDS = 86_400


# LLM: 只登记本owner使用<数据扩展名>.lock协议的私有目录；不递归、不包含local_users/operations或逻辑锁目录。
# 函数用途: 列出owner维护可回收伴生锁的固定目录，不创建目录。
def owner_lock_sidecar_dirs(home: MyAgentHomePaths) -> tuple[Path, ...]:
    return (home.owner_memory_dir, home.owner_memory_daily_dir, home.owner_memory_curator_runs_dir,
        home.owner_compact_dir, home.owner_cache_dir, home.owner_tmp_dir, home.owner_scheduler_dir,
        *(home.owner_data_dir / name for name in ("decision", "context", "skill_learning", "skill_proposals")))


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


def owner_maintenance_due(
    owner_home: str | Path,
    *,
    now: float | None = None,
) -> bool:
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
            expected_state_fingerprint=expected_state_fingerprint)
    if not owner_maintenance_due(home.owner_home_dir, now=current):
        return OwnerMaintenanceResult(False, "not_due")
    state_path = _maintenance_state_path(home.owner_home_dir)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with locked_json_path(state_path):
        if not owner_maintenance_due(home.owner_home_dir, now=current):
            return OwnerMaintenanceResult(False, "not_due")
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
        previous = read_json_object_report(
            state_path,
            context="owner_maintenance.state",
        ).payload
        effects = {"text_vector_cache_reclaimed": dropped_cache_keys,
                   "derived_records_pruned": derived_prune,
                   "text_vector_cache_reclaim_error": reclaim_error,
                   "indexes_compacted": compacted, "indexes_compact_failed": compact_failed,
                   "memory_archive_permissions": permissions, "run_closeout_backfill": run_closeout_backfill,
                   "local_store_events_maintenance": local_events}
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


# LLM: 只扫三个固定库名及workspace根下一层；父目录/DB/files链接拒绝，不创建或递归任意路径。
# 函数用途: 共用单进程和transition维护的全部既存LocalStore数据库发现。
def _local_store_database_paths(home: Path) -> tuple[list[Path], list[dict[str, str]]]:
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
            if (_real_owner_directory(home, path.parent) and not path.is_symlink() and path.is_file()
                    and not (path.parent / "files").is_symlink()):
                result.append(path)
        except OSError as exc:
            errors.append({"source": str(path.relative_to(home)), "error_type": type(exc).__name__})
    return result, errors


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
def _write_maintenance_result(state_path, retention, previous, current, effects):
    fields = _outcome_fields(retention, previous, current)
    payload = {"schema_version": "owner-maintenance.v1", "last_attempt_at": current, **fields,
               "load_errors": list(retention.load_errors), "legal_hold": retention.legal_hold, **effects}
    write_json_file_atomic_unlocked(state_path, payload)
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


# LLM: 共用本owner全部既存DB发现，保留JSONL-only事件恢复；每库一批，普通故障隔离，四类未知原抛。
#   executed=False表示没有完整返回成功，非未写入证明；异常后的副作用未知及不完整计数须单列。
#   Home当前没有owner_local_store_dir字段，真实默认库由runtime_paths放在workspace/runtime/workspaces/*/local_store。
# 函数用途: 在原owner后台周期裁剪既有事件库，把执行或跳过事实交给同一maintenance.json。
def _maintain_local_store_events(home: MyAgentHomePaths, *, now: float, commit_transition=None) -> dict[str, Any]:
    try:
        directories, discovery_errors = _local_event_store_directories(home)
        databases, errors = _local_store_database_paths(home.owner_home_dir)
        discovery_errors.extend(errors)
        databases = list(dict.fromkeys([*databases, *(directory / "local.db" for directory in directories)]))
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


# LLM: 仅扫描固定workspace根直接子目录，IO错误单列后继续其它来源；双文件缺失不建库，拒符号链接/特殊文件。
# 函数用途: 找到本owner已经初始化的兼容data库及规范workspace事件库。
def _local_event_store_directories(home: MyAgentHomePaths) -> tuple[list[Path], list[dict[str, str]]]:
    candidates = [Path(getattr(home, "owner_local_store_dir", home.owner_data_dir / "local_store"))]
    workspaces = home.owner_workspace_dir / "runtime" / "workspaces"
    errors = []
    try:
        if _real_owner_directory(home, workspaces):
            candidates.extend(path / "local_store" for path in sorted(workspaces.iterdir()))
    except OSError as exc:
        errors.append({"source": "workspace_scan", "error_type": type(exc).__name__})
    result = []
    for directory in dict.fromkeys(candidates):
        try:
            if not _real_owner_directory(home, directory) or (directory / "files").is_symlink():
                continue
            files = (directory / "local.db", directory / "events.jsonl")
            if any(path.is_symlink() or (path.exists() and not path.is_file()) for path in files):
                continue
            if any(path.is_file() for path in files):
                result.append(directory)
        except OSError as exc:
            errors.append({"source": str(directory.relative_to(home.owner_home_dir)), "error_type": type(exc).__name__})
    return result, errors


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


def _positive_interval(value: object) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return _DEFAULT_INTERVAL_SECONDS
    return max(0, parsed)


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
