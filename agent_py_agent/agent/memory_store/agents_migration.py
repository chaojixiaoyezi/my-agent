from __future__ import annotations

"""把由自动晋升写入 AGENTS.md 的条目安全清理成学习排除名单的迁移。"""

# LLM: 第 0 期（设计 v2 §3.6）：归属按「当前条目最后一次有效写入的来源」判定，而不是历史来源
#   前缀——「自动添加→用户删除→用户重新添加相同文字」时 entry_id 相同，必须判为用户条目；
#   有歧义的一律保留并报告。执行先快照（含 persona/backups、排除名单、归档与事务状态）、再
#   归档、再删除，删除走 PersonaRepository 的唯一 CAS 通道；apply 绑定 dry-run 计划摘要，
#   中途变化拒绝重跑；崩溃可幂等续作，续作只处理「当前重判定 ∩ 原计划」的条目，不覆盖迁移
#   期间用户的新修改；收尾让受影响线程的冻结人格快照受控失效。
# 模块用途: 计算并执行 AGENTS 自动晋升条目的清理迁移，记录快照、归档、事务状态与线程快照失效。

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from ..capability.persona_repository import (
    PersonaBatchMutationRequest,
    PersonaMutationRequest,
    PersonaRepository,
    _mutate_persona_batch_locked,
    persona_entry_id,
    versions_ledger_tail,
)
from ..common.json_io import (
    append_private_jsonl_records,
    locked_json_path,
    read_jsonl_objects_report,
    write_private_json_file_atomic,
    write_private_json_file_atomic_unlocked,
    write_private_text_file_atomic,
)
from ..user_space.owner_policy import LearningExclusionEntry, append_learning_exclusions_locked
from ..user_space.owner_quota import OwnerQuotaAdmission
from .curator_lease import curator_lease_heartbeat, retire_curator_lease
from .curator_state import CuratorStateCorruptError, CuratorSuccessCommit, MemoryCuratorStateStore

# 自动晋升写入 Persona 时使用的来源前缀（见 promotion._commit_persona）。
_PROMOTION_SOURCE_PREFIX = "memory-promotion:"
# 迁移租约时长：等待目标锁和迁移全过程续租；死亡回收仍走 Curator 的共享判据。
_MIGRATION_LEASE_SECONDS = 300
_MIGRATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SNAPSHOT_META = "snapshot.json"
_ARCHIVE_NAME = "archive.jsonl"
_RECORD_NAME = "record.json"
_SNAPSHOT_AGENTS = "agents.md"
_SNAPSHOT_BACKUPS = "backups"


# LLM: 迁移合同错误用稳定原因码（计划摘要缺失/过期），调用方直接转错误码，不解析文案。
# 类用途: 表达 apply 因计划漂移或缺失而被拒绝。
class AgentsCleanupError(RuntimeError):
    # 函数用途: 保存稳定原因码并生成错误消息。
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


# 类用途: 打包一次迁移的参数；plan_digest 由 dry-run 报告回传，apply 必须原样携带。
@dataclass(frozen=True)
class AgentsCleanupRequest:
    migration_id: str
    conversations: tuple[str, ...] = ()
    actor: str = "memory-admin-cli"
    reason: str = ""
    plan_digest: str = ""


# LLM: 计划含条目定位与正文快照（供归档）；to_dict 剔除正文，dry-run 不打印内容；
#   歧义条目一律保留，只在报告里列 entry_id；versions_tail 是计划时点的版本账本尾指纹，
#   apply 的删除阶段在人格锁内核对它，任何并发写入都会让计划失效（即使正文 sha 恢复原值）。
# 类用途: 描述一次 dry-run 的可清理集合、歧义集合、计划摘要与版本账本前提。
@dataclass(frozen=True)
class AgentsCleanupPlan:
    entries_total: int
    protected_entries: int
    removable: tuple[dict[str, object], ...]
    ambiguous_entry_ids: tuple[str, ...]
    sha256: str
    plan_digest: str
    versions_tail: str = ""

    # 函数用途: 把计划转成不含条目正文的结构化报告。
    def to_dict(self) -> dict[str, object]:
        return {
            "entries_total": self.entries_total,
            "protected_entries": self.protected_entries,
            "removable_count": len(self.removable),
            "removable_entry_ids": [str(item["entry_id"]) for item in self.removable],
            "removable": [
                {key: value for key, value in item.items() if key != "content"}
                for item in self.removable
            ],
            "ambiguous_entry_ids": list(self.ambiguous_entry_ids),
            "ambiguous_count": len(self.ambiguous_entry_ids),
            "sha256": self.sha256,
            "plan_digest": self.plan_digest,
        }


# 类用途: 一条条目的最后有效写入事件（版本账本行序、类型、来源、版本号）。
@dataclass(frozen=True)
class _WriteEvent:
    index: int
    kind: str
    source: str
    version: int


# 类用途: 携带版本账本的投影（每条目最后事件 + 全局 rollback 行序），供归属判定复用。
@dataclass(frozen=True)
class _HistoryIndex:
    last_events: dict[str, _WriteEvent]
    rollback_positions: tuple[int, ...]


# LLM: 删除进度收成一个对象后，事务记录写回与批次回调不再各传三个散参数；versions_tail 必须
#   是上一批在人格锁内采样（mutate_batch 返回）的值，不能锁外重采样（mc4-3101）。
# 类用途: 一批删除完成后的进度（sha、已删数、剩余数、该批完成后的版本账本尾指纹）。
@dataclass(frozen=True)
class _BatchProgress:
    sha: str
    removed: int
    remaining: int
    versions_tail: str = ""


# LLM: 删除循环只消费通道对象：迁移编号、批间 CAS 的 sha 与进度回调集中在一处，避免参数继续膨胀。
# 类用途: 删除批次循环的通道信息（迁移编号、起始 sha、每批进度回调）。
@dataclass(frozen=True)
class _RemovalChannel:
    migration_id: str
    initial_sha: str
    on_batch: Callable[[_BatchProgress], None]
    admission: OwnerQuotaAdmission


# LLM: 把迁移依赖收成不可变对象，临界区内重新读事务记录，不携带锁外旧状态。
# 类用途: 绑定迁移当前请求、规范 owner 仓库和候选来源。
@dataclass(frozen=True)
class _CleanupContext:
    home_paths: object
    persona: PersonaRepository
    candidate_service: object
    request: AgentsCleanupRequest
    lease_id: str = ""


# LLM: 完成记录与报告共用同一份计数快照，同一组数字只组装一次。
# 类用途: apply 收尾写入记录与报告共用的结果计数快照。
@dataclass(frozen=True)
class _ApplySummary:
    migration_id: str
    snapshot_reused: bool
    archived_count: int
    removed_count: int
    conversations_added: int
    expired_thread_snapshots: int


# LLM: 归属判定用版本历史的事件流：add/replace 记 write、remove 记 remove（batch 展开其
#   operations，只算 changed 的操作）；rollback 记录单独收集行序。判定规则（设计 v2 §3.6）：
#   无事件、最后事件是 remove、或最后写入之后发生 rollback → 歧义保留；最后写入来源是
#   memory-promotion 前缀 → 可清理；其它来源 → 保护。仓库外直接编辑（当前 sha 与版本历史
#   最后记录不一致）时，无法用历史证明未被触碰的条目一律降级为歧义（2902）。
# 函数用途: 计算当前可清理的自动晋升条目（只读，不修改任何文件）。
def plan_agents_cleanup(*, persona: PersonaRepository) -> AgentsCleanupPlan:
    report = persona.list_entries("agents")
    entries = report.get("entries")
    entry_rows = entries if isinstance(entries, list) else []
    rows = persona.history("agents")
    index = _history_index(rows)
    edited_ids = _hand_edited_entry_ids(persona, rows, entry_rows, report.get("sha256"))
    removable: list[dict[str, object]] = []
    ambiguous: list[str] = []
    protected = 0
    for entry in entry_rows:
        entry_id = str(entry.get("entry_id") or "").strip()
        if not entry_id:
            continue
        verdict = _classify_entry(entry_id, index)
        if verdict == "removable" and entry_id in edited_ids:
            verdict = "ambiguous"
        if verdict == "ambiguous":
            ambiguous.append(entry_id)
            continue
        if verdict == "removable":
            removable.append(_removable_item(entry, index.last_events[entry_id]))
            continue
        protected += 1
    sha256 = str(report.get("sha256") or "")
    versions_tail = versions_ledger_tail(persona.versions_path)
    digest = _plan_digest(
        sha256, [str(item["entry_id"]) for item in removable], ambiguous, versions_tail
    )
    return AgentsCleanupPlan(
        entries_total=len(entry_rows),
        protected_entries=protected,
        removable=tuple(removable),
        ambiguous_entry_ids=tuple(ambiguous),
        sha256=sha256,
        plan_digest=digest,
        versions_tail=versions_tail,
    )


# LLM: 仓库外直接编辑（当前文件 sha 与版本历史最后一条记录不一致）后，历史无法解释当前
#   文件里哪些行是用户手工写回的；与「最后记录内容」逐行对比，凡当前条目行不在最后记录里
#   的一律判为受影响（保留）。备份读不到时无法证明任何条目未被触碰，全部保守。
# 函数用途: 返回直接编辑后不能用历史证明未被触碰的条目 ID 集合。
def _hand_edited_entry_ids(
    persona: PersonaRepository,
    rows: list[dict[str, object]],
    entry_rows: list,
    current_sha: object,
) -> set[str]:
    if not rows:
        return set()
    last = rows[-1]
    if str(last.get("sha256") or "") == str(current_sha or ""):
        return set()
    history_text = _read_version_snapshot(persona, last)
    all_ids = {
        str(entry.get("entry_id") or "").strip()
        for entry in entry_rows
        if isinstance(entry, dict) and str(entry.get("entry_id") or "").strip()
    }
    if history_text is None:
        return all_ids
    history_lines = {line.strip() for line in history_text.splitlines()}
    edited: set[str] = set()
    for entry in entry_rows:
        if not isinstance(entry, dict):
            continue
        entry_id = str(entry.get("entry_id") or "").strip()
        content = str(entry.get("content") or "").strip()
        if not entry_id or not content:
            continue
        if f"- {content}" not in history_lines:
            edited.add(entry_id)
    return edited


# 函数用途: 读取版本记录 backup_ref 指向的快照内容；缺失、越界或读不动返回 None。
def _read_version_snapshot(persona: PersonaRepository, row: dict[str, object]) -> str | None:
    ref = str(row.get("backup_ref") or "")
    if not ref:
        return None
    path = (persona.owner_home / ref).resolve(strict=False)
    try:
        path.relative_to(persona.owner_home)
    except ValueError:
        return None
    if path.is_symlink() or not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


# 函数用途: 把版本账本折叠成每条目的最后事件与 rollback 行序。
def _history_index(rows: list[dict[str, object]]) -> _HistoryIndex:
    last: dict[str, _WriteEvent] = {}
    rollbacks: list[int] = []
    for index, row in enumerate(rows):
        action = str(row.get("action") or "")
        if action == "rollback":
            rollbacks.append(index)
            continue
        if action == "batch":
            _collect_batch_events(last, row, index)
            continue
        if action in {"add", "replace", "remove"}:
            _collect_single_event(last, row, index, action)
    return _HistoryIndex(last, tuple(rollbacks))


# 函数用途: 记录一条单操作记录的最后写入事件。
def _collect_single_event(
    last: dict[str, _WriteEvent],
    row: dict[str, object],
    index: int,
    action: str,
) -> None:
    entry_id = str(row.get("entry_id") or "").strip()
    if not entry_id:
        return
    kind = "remove" if action == "remove" else "write"
    last[entry_id] = _WriteEvent(
        index, kind, str(row.get("source") or ""), int(row.get("version") or 0)
    )


# 函数用途: 展开一条 batch 记录里真实改变文档的操作，更新最后写入事件。
def _collect_batch_events(
    last: dict[str, _WriteEvent],
    row: dict[str, object],
    index: int,
) -> None:
    source = str(row.get("source") or "")
    version = int(row.get("version") or 0)
    for operation in _changed_batch_operations(row.get("operations")):
        entry_id = str(operation.get("entry_id") or "").strip()
        kind = "remove" if str(operation.get("action") or "") == "remove" else "write"
        last[entry_id] = _WriteEvent(index, kind, source, version)


# 函数用途: 过滤 batch 记录里 changed 且有 entry_id 的操作。
def _changed_batch_operations(operations: object) -> list[dict[str, object]]:
    if not isinstance(operations, list):
        return []
    rows: list[dict[str, object]] = []
    for operation in operations:
        if not isinstance(operation, dict) or not bool(operation.get("changed")):
            continue
        if not str(operation.get("entry_id") or "").strip():
            continue
        rows.append(operation)
    return rows


# 函数用途: 按最后有效写入事件给一个当前条目归类（removable/protected/ambiguous）。
def _classify_entry(entry_id: str, index: _HistoryIndex) -> str:
    event = index.last_events.get(entry_id)
    if event is None or event.kind == "remove":
        return "ambiguous"
    if any(position > event.index for position in index.rollback_positions):
        return "ambiguous"
    if event.source.startswith(_PROMOTION_SOURCE_PREFIX):
        return "removable"
    return "protected"


# 函数用途: 把一条可清理条目转成计划条目（含正文快照供归档，to_dict 时剔除）。
def _removable_item(entry: dict[str, object], event: _WriteEvent) -> dict[str, object]:
    candidate_id = ""
    if event.source.startswith(_PROMOTION_SOURCE_PREFIX):
        candidate_id = event.source.rsplit(":", 1)[-1]
    return {
        "entry_id": str(entry.get("entry_id") or ""),
        "version": event.version,
        "candidate_id": candidate_id,
        "blocked": bool(entry.get("blocked")),
        "content": str(entry.get("content") or ""),
    }


# 函数用途: 生成计划摘要，绑定当前文档 sha、可清理/歧义条目集合与版本账本尾指纹。
def _plan_digest(
    sha256: str,
    removable_ids: list[str],
    ambiguous_ids: list[str],
    versions_tail: str = "",
) -> str:
    payload = {
        "sha256": sha256,
        "removable": sorted(removable_ids),
        "ambiguous": sorted(ambiguous_ids),
        "versions_tail": versions_tail,
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# LLM: 迁移执行在 curator 租约内进行（拿不到结构化拒绝，迁移期间不会有新的 curator 运行）；
#   单一临界区一次做完：计划校验→快照（续作逐文件校验）→归档→删除→名单→线程快照失效→完成
#   记录；删除通过人格批次的锁内入口一次完成，首次必须携带与当前计划一致的 plan_digest（防
#   漂移），续作跳过 digest 但只处理「当前重判定 ∩ 原计划」；完成后重跑幂等返回。
# 函数用途: 执行一次 AGENTS 清理迁移并返回结构化报告。
def apply_agents_cleanup(
    *,
    home_paths: object,
    persona: PersonaRepository,
    candidate_service: object,
    request: AgentsCleanupRequest,
    curator_state_store: object | None = None,
) -> dict[str, object]:
    migration_id = _validated_migration_id(request.migration_id)
    directory = _migration_dir(home_paths, migration_id)
    record = _read_record(directory)
    if _is_completed(record):
        return _completed_report(migration_id, directory, record)
    curator_state_store = curator_state_store or MemoryCuratorStateStore(persona.owner_home / "memory/curator/state.json")
    lease = _acquire_migration_lease(curator_state_store)
    return _execute_migration(_CleanupContext(home_paths, persona, candidate_service, request), curator_state_store, lease)


# LLM: 心跳必须先退出再释放精确租约；收尾失败不撤销已提交迁移，也不能覆盖主体异常或报告静默成功。
# 函数用途: 执行迁移主体并携带租约收尾诊断，异常仍抛原对象。
def _execute_migration(context: _CleanupContext, store: object, lease: dict[str, object]) -> dict[str, object]:
    context = replace(context, lease_id=str(lease["lease_id"]))
    try:
        with curator_lease_heartbeat(store, str(lease["lease_id"]), _MIGRATION_LEASE_SECONDS):
            report = _run_cleanup(context)
    except BaseException as exc:
        _cleanup_failed_migration(context, store, lease, exc)
        raise
    detail = _release_migration_lease(store, lease)
    _persist_cleanup_diagnostic(context, detail)
    if detail:
        return {**report, "ok": False, "lease_cleanup": detail, "notes": ["AGENTS_CLEANUP_LEASE_RELEASE_FAILED"]}
    return report


# LLM: 有主体异常时二次中断也不能换掉原对象；诊断尽力落到原记录，失败仍保留 pending，不强抢租约。
# 函数用途: 为失败主体安全收尾并附结构化诊断，绕过异常类自定义 add_note。
def _cleanup_failed_migration(context: _CleanupContext, store: object, lease: dict, primary: BaseException) -> None:
    try:
        detail = _release_after_primary(store, lease, primary)
    except BaseException as secondary:
        detail = {"lease_id": context.lease_id, "released": False, "retired": False,
            "release_error_class": type(secondary).__name__, "recovery": "manual_recovery_required"}
    try:
        _persist_cleanup_diagnostic(context, detail)
    except BaseException as secondary:
        detail["diagnostic_write_error_class"] = type(secondary).__name__
    if detail:
        primary.__dict__["lease_cleanup"] = detail
        primary.__dict__.setdefault("__notes__", []).append("agents migration lease cleanup: " + json.dumps(detail, sort_keys=True))


# LLM: 心跳停止未确认时仍可能在途写 state，禁止释放/退役；保留 executing 的 fail-closed 保护与原异常。
# 函数用途: 仅在心跳停止已确认后为失败主体释放原租约。
def _release_after_primary(store: object, lease: dict, primary: BaseException) -> dict[str, object]:
    stop_error = primary.__dict__.get("lease_heartbeat_stop_error_class")
    if stop_error:
        return {"lease_id": lease["lease_id"], "released": False, "retired": False,
            "heartbeat_stop_error_class": stop_error, "recovery": "manual_recovery_required"}
    return _release_migration_lease(store, lease)


# LLM: 完成事实不回滚；同编号重放保留收尾诊断。读改写共用 record 锁，锁内只用私有原子写 unlocked 入口，避免自死锁。
# 函数用途: 将租约收尾故障写入原迁移记录，不创建另一份执行权或恢复账。
def _persist_cleanup_diagnostic(context: _CleanupContext, detail: dict[str, object]) -> None:
    directory = _migration_dir(context.home_paths, context.request.migration_id)
    try:
        _persist_cleanup_diagnostic_locked(directory, detail)
    except Exception as exc:
        detail["diagnostic_write_error_class"] = type(exc).__name__


# LLM: 诊断只追加到已存在记录；读改写共用 record 锁，私有原子写不重复准入，避免并发覆盖和自死锁。
# 函数用途: 持锁合并并耐久保存迁移的收尾诊断。
def _persist_cleanup_diagnostic_locked(directory: Path, detail: dict[str, object]) -> None:
    with locked_json_path(directory / _RECORD_NAME):
        record = _read_record(directory)
        if record is not None:
            write_private_json_file_atomic_unlocked(directory / _RECORD_NAME,
                {**record, "lease_cleanup": detail, "lease_cleanup_pending": False})


# LLM: 租约已领取；quota admission 先于目标锁，固定 policy→人格锁覆盖重判定到 completed。
# 同锁入口只调用锁内写入版本；quota 关闭不能缩短人格锁，异常按相反顺序释放。
# 函数用途: 建立迁移唯一外层临界区，首次和续作使用相同锁集合与顺序。
def _run_cleanup(context: _CleanupContext) -> dict[str, object]:
    persona = context.persona
    with persona.quota_enforcer.admission() as admission:
        with locked_json_path(context.home_paths.owner_memory_policy_json), locked_json_path(persona.path_for("agents")):
            return _run_cleanup_locked(context, admission)


# LLM: 调用方持 policy/quota/persona；事务记录、计划和账本尾全部来自本次锁内状态。
# 函数用途: 同一持锁期间完成快照、归档、一次删除、名单、线程失效和耐久完成记录。
def _run_cleanup_locked(context: _CleanupContext, admission: OwnerQuotaAdmission) -> dict[str, object]:
    home_paths, persona = context.home_paths, context.persona
    candidate_service, request = context.candidate_service, context.request
    migration_id = _validated_migration_id(request.migration_id)
    directory = _migration_dir(home_paths, migration_id)
    record = _read_record(directory)
    if _is_completed(record):
        return _completed_report(migration_id, directory, record)
    plan = plan_agents_cleanup(persona=persona)
    if record is None:
        _require_matching_digest(request, plan)
        record = {**_new_record(migration_id, plan, request), "cleanup_lease_id": context.lease_id,
            "lease_cleanup_pending": True}
        _write_record(directory, record)
    else:
        # 接受恢复前的合法用户修改，但只用同锁重判定已采样的尾，不另行重采样。
        record = {**record, "updated_at": _now_iso(), "versions_tail": plan.versions_tail,
            "cleanup_lease_id": context.lease_id, "lease_cleanup_pending": True}
        _write_record(directory, record)
    conversations = _effective_conversations(record, request)
    snapshot = _ensure_snapshot(home_paths, directory, migration_id)
    targets = _resume_targets(record, plan)
    snapshot_map = _snapshot_entry_contents(directory)
    archived_count = _archive_planned(directory, targets, candidate_service, snapshot_map)

    # LLM: 进度写回仍在迁移外锁内；崩溃后只认耐久记录和再次重判定，不反读外部 tail。
    # 函数用途: 将一次原子删除后的版本尾与计数保存到恢复记录。
    def on_batch(progress: _BatchProgress) -> None:
        _update_record(directory, record, progress)

    channel = _RemovalChannel(
        migration_id=migration_id,
        initial_sha=plan.sha256,
        on_batch=on_batch,
        admission=admission,
    )
    removed_count = _remove_planned(persona, targets, channel)
    conversations_added = _append_conversations(home_paths, conversations, request)
    expired = _expire_thread_snapshots(persona, _planned_contents(record, snapshot_map))
    summary = _ApplySummary(
        migration_id=migration_id,
        snapshot_reused=bool(snapshot.get("reused")),
        archived_count=archived_count,
        removed_count=removed_count,
        conversations_added=conversations_added,
        expired_thread_snapshots=expired,
    )
    _write_record(directory, _final_record(record, directory, summary))
    return _apply_report(directory, plan, record, summary)


# LLM: 迁移与 curator 的互斥用 curator 自己的租约（同一 state 文件上的 CAS）：迁移先领租约、
#   结束后释放；拿不到就结构化拒绝，不让两条写入链并行（mc4-3102 的设计）。apply 未注入
#   store 时使用该 owner 的规范 state；dry-run 不领取租约。
# 函数用途: 为迁移领取 curator 租约，返回租约 dict；拿不到时抛出结构化错误。
def _acquire_migration_lease(store: object | None) -> dict[str, object] | None:
    if store is None:
        return None
    try:
        acquired = store.acquire(
            reason="migration",
            config_revision="agents-cleanup",
            lease_seconds=_MIGRATION_LEASE_SECONDS,
        )
    except CuratorStateCorruptError as exc:
        raise AgentsCleanupError("AGENTS_CLEANUP_CURATOR_STATE_UNAVAILABLE") from exc
    if acquired is None:
        raise AgentsCleanupError("AGENTS_CLEANUP_CURATOR_BUSY")
    _, lease = acquired
    return dict(lease)


# LLM: 释放沿精确 lease_id CAS，保留游标；失败尝试同 ID 退役，失败分类必须交回，不能承诺活 holder 自动过期。
# 函数用途: 提交并释放迁移租约。
def _release_migration_lease(store: object | None, lease: dict[str, object] | None) -> dict[str, object]:
    if store is None or not lease:
        return {}
    try:
        state = store.load()
        store.commit_success(
            CuratorSuccessCommit(
                lease_id=str(lease.get("lease_id") or ""),
                run_id=str(lease.get("run_id") or ""),
                reason="migration",
                per_thread_cursors=dict(state.per_thread_cursors),
                last_processed_audit_event_id="",
                processed_messages=0,
                processed_audit_events=0,
                candidate_count=0,
                daily_event_count=0,
                committed_at=_now_iso(),
            )
        )
    except Exception as exc:
        return _retire_failed_release(store, lease, exc)
    return {}


# LLM: 退役只有同一租约能改 phase；退役也失败时保留 fail-closed 的 executing，不抢新执行权。
# 函数用途: 返回释放/退役的稳定错误分类与恢复要求，不输出错误正文或假装已释放。
def _retire_failed_release(store: object, lease: dict[str, object], error: Exception) -> dict[str, object]:
    lease_id = str(lease["lease_id"])
    detail: dict[str, object] = {"lease_id": lease_id, "released": False, "retired": False,
        "release_error_class": type(error).__name__, "retire_error_class": "", "recovery": "manual_recovery_required"}
    try:
        retire_curator_lease(store, lease_id)
        _record_retirement_state(detail, store.load().active_lease)
    except Exception as exc:
        detail["retire_error_class"] = type(exc).__name__
    return detail


# LLM: I/O 错误保护与 phase 分类分开，只有读回同 ID interrupted 才承诺原期限后的领取。
# 函数用途: 将租约读回事实转换成释放失败的恢复分类，未改变时保留人工恢复要求。
def _record_retirement_state(detail: dict[str, object], current: dict[str, object]) -> None:
    lease_id = detail["lease_id"]
    detail["retired"] = current.get("lease_id") == lease_id and current.get("phase") == "interrupted"
    detail["lease_changed"] = current.get("lease_id") != lease_id
    if detail["retired"]:
        detail["recovery"] = "expired_lease"
        return
    if detail["lease_changed"]:
        detail["recovery"] = "lease_changed"


# LLM: apply 绑定 dry-run 计划摘要：缺失拒绝（必须先预览），与当前计划不一致说明文档或计划
#   已变化，拒绝并要求重新预览。
# 函数用途: 校验 apply 携带的计划摘要与当前计划一致。
def _require_matching_digest(request: AgentsCleanupRequest, plan: AgentsCleanupPlan) -> None:
    digest = str(request.plan_digest or "").strip()
    if not digest:
        raise AgentsCleanupError("AGENTS_CLEANUP_PLAN_DIGEST_REQUIRED")
    if digest != plan.plan_digest:
        raise AgentsCleanupError("AGENTS_CLEANUP_PLAN_STALE")


# LLM: 续作/首次统一按「当前重判定 ∩ 原计划」取目标：迁移期间用户把条目改成非 promotion 来源
#   或新写入的条目都不会被删；已删除的条目自然不在当前文档。
# 函数用途: 计算本次真正要删除的条目（原计划与当前重判定的交集）。
def _resume_targets(record: dict, plan: AgentsCleanupPlan) -> list[dict[str, object]]:
    planned = {
        str(item.get("entry_id") or "").strip()
        for item in record.get("planned_entries") or ()
        if isinstance(item, dict)
    }
    planned.discard("")
    return [item for item in plan.removable if str(item.get("entry_id") or "") in planned]


# LLM: 外层人格锁内一次送入全部目标；不用普通入口的 32 操作限制，不存在批间窗口。
# 函数用途: 一次删除已重判定目标，写回唯一删除进度并返回实际数量。
def _remove_planned(
    persona: PersonaRepository,
    targets: list[dict[str, object]],
    channel: _RemovalChannel,
) -> int:
    if not targets:
        return 0
    result = _remove_batch(persona, targets, channel.initial_sha, channel)
    removed = _changed_count(result)
    channel.on_batch(_BatchProgress(str(result.get("sha256") or channel.initial_sha),
        removed, len(targets) - removed, str(result.get("versions_tail") or "")))
    return removed


# LLM: 调用方持 policy/quota/persona；同一人格 CAS/配额/备份/账本提交实现，但不重复取锁。
# 函数用途: 将全部移除操作一次交给人格仓库的锁内批次提交。
def _remove_batch(
    persona: PersonaRepository,
    chunk: list[dict[str, object]],
    sha: str,
    channel: _RemovalChannel,
) -> dict[str, object]:
    migration_id = channel.migration_id
    record = _read_record(_migration_dir_for(persona.owner_home, migration_id))
    expected_tail = str(record.get("versions_tail") or "") if record else ""
    operations = tuple(
        PersonaMutationRequest(
            target="agents",
            action="remove",
            entry_id=str(item["entry_id"]),
        )
        for item in chunk
    )
    return _mutate_persona_batch_locked(persona, channel.admission,
        PersonaBatchMutationRequest(
            target="agents",
            operations=operations,
            expected_sha256=sha,
            source=f"migration:{migration_id}",
            expected_versions_tail=expected_tail,
        )
    )


# 函数用途: 统计一批删除操作里真实改变文档的条数。
def _changed_count(result: dict[str, object]) -> int:
    return sum(
        1
        for operation in result.get("operations") or ()
        if isinstance(operation, dict) and operation.get("changed")
    )


# LLM: 事务记录是续作与审计的唯一状态源：planned_entries 固定首次计划（续作目标的上限），
#   current_sha256/removed/remaining 随每批推进。
# 函数用途: 构造首次 apply 的 in_progress 事务记录。
def _new_record(
    migration_id: str,
    plan: AgentsCleanupPlan,
    request: AgentsCleanupRequest,
) -> dict[str, object]:
    return {
        "migration_id": migration_id,
        "status": "in_progress",
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "planned_entries": [
            {
                "entry_id": str(item["entry_id"]),
                "candidate_id": str(item.get("candidate_id") or ""),
                "version": int(item.get("version") or 0),
            }
            for item in plan.removable
        ],
        "expected_agents_sha256": plan.sha256,
        "current_sha256": plan.sha256,
        "versions_tail": plan.versions_tail,
        "removed_count": 0,
        "remaining_count": len(plan.removable),
        "conversations": [str(item) for item in request.conversations if str(item).strip()],
        "conversations_added": 0,
        "expired_thread_snapshots": 0,
        "actor": request.actor,
        "notes": [],
    }


# 函数用途: 读回事务记录；缺失、链接或坏内容按「没有记录」处理。
def _read_record(directory: Path) -> dict | None:
    path = directory / _RECORD_NAME
    if not path.exists() or path.is_symlink() or not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


# 函数用途: 判断事务记录是否已完成（完成后的重跑幂等返回）。
def _is_completed(record: dict | None) -> bool:
    return isinstance(record, dict) and str(record.get("status") or "") == "completed"


# LLM: completed 只代表迁移数据耐久完成，收尾 pending 期间和诊断写失败后都不能据缺诊断推定成功。
# 函数用途: 为已完成迁移生成幂等报告，保留收尾 pending 或失败事实。
def _completed_report(migration_id: str, directory: Path, record: dict) -> dict[str, object]:
    detail = record.get("lease_cleanup") or {}
    if record.get("lease_cleanup_pending"):
        detail = {"lease_id": record.get("cleanup_lease_id", ""), "released": False,
            "retired": False, "recovery": "cleanup_pending"}
    return {
        "ok": not bool(detail),
        "already_completed": True,
        "migration_id": migration_id,
        "status": "completed",
        "removed_count": int(record.get("removed_count") or 0),
        "snapshot_dir": str(directory),
        "archive_ref": str(directory / _ARCHIVE_NAME),
        "conversations_added": 0,
        "expired_thread_snapshots": 0,
        "notes": ["already_completed"],
        **({"lease_cleanup": detail} if detail else {}),
    }


# 函数用途: 每批删除后把进度与新的版本账本尾写回事务记录。
def _update_record(
    directory: Path,
    record: dict,
    progress: _BatchProgress,
) -> None:
    payload = dict(record)
    payload.update(
        {
            "status": "in_progress",
            "updated_at": _now_iso(),
            "current_sha256": progress.sha,
            "removed_count": progress.removed,
            "remaining_count": progress.remaining,
            "versions_tail": progress.versions_tail
            or str(record.get("versions_tail") or ""),
        }
    )
    _write_record(directory, payload)


# 函数用途: 构造 completed 事务记录。
def _final_record(
    record: dict,
    directory: Path,
    summary: _ApplySummary,
) -> dict[str, object]:
    payload = dict(record)
    payload.update(
        {
            "status": "completed",
            "updated_at": _now_iso(),
            "removed_count": summary.removed_count,
            "remaining_count": 0,
            "conversations_added": summary.conversations_added,
            "expired_thread_snapshots": summary.expired_thread_snapshots,
            "snapshot_dir": str(directory),
            "archive_ref": str(directory / _ARCHIVE_NAME),
        }
    )
    return payload


# 函数用途: 组装 apply 的结构化报告（不含条目正文）。
def _apply_report(
    directory: Path,
    plan: AgentsCleanupPlan,
    record: dict,
    summary: _ApplySummary,
) -> dict[str, object]:
    planned = record.get("planned_entries")
    planned_count = len(planned) if isinstance(planned, list) else 0
    return {
        "ok": True,
        "migration_id": summary.migration_id,
        "status": "completed",
        "entries_total": plan.entries_total,
        "protected_entries": plan.protected_entries,
        "planned_count": planned_count,
        "removed_count": summary.removed_count,
        "archived_count": summary.archived_count,
        "ambiguous_entry_ids": list(plan.ambiguous_entry_ids),
        "snapshot_dir": str(directory),
        "snapshot_reused": summary.snapshot_reused,
        "archive_ref": str(directory / _ARCHIVE_NAME),
        "conversations_added": summary.conversations_added,
        "expired_thread_snapshots": summary.expired_thread_snapshots,
        "notes": [],
    }


# LLM: 快照在首次 apply 时一次性写全：AGENTS.md、版本账本、候选账本、排除名单、已有归档与
#   事务记录、persona/backups 全树；snapshot.json 记录每个文件的 sha256+bytes，存在即复用，
#   绝不覆盖已有快照（它是恢复与归档内容的权威）。任何一步失败都在删除前中止（2903）。
# 函数用途: 确保迁移快照存在，返回快照元数据。
def _ensure_snapshot(home_paths: object, directory: Path, migration_id: str) -> dict[str, object]:
    meta_path = directory / _SNAPSHOT_META
    if meta_path.exists():
        # LLM: 续作复用前逐文件重校验（mc4-3104）：快照是删除的安全前提，两次 apply 之间副本
        #   可能被改动或损坏；校验不过就拒绝，绝不带着坏快照继续删除。
        _verify_snapshot_manifest(directory, meta_path)
        return {"reused": True, "snapshot_dir": str(directory)}
    snapshot_dir = directory / "snapshot"
    _ensure_dir(snapshot_dir)
    files: dict[str, object] = {}
    for name, source in _snapshot_sources(home_paths, directory).items():
        _copy_snapshot_file(source, snapshot_dir / name, files, name)
    _copy_snapshot_tree(
        _owner_backups_dir(home_paths),
        snapshot_dir / _SNAPSHOT_BACKUPS,
        files,
        _SNAPSHOT_BACKUPS,
    )
    meta = {"migration_id": migration_id, "created_at": _now_iso(), "files": files}
    write_private_json_file_atomic(meta_path, meta)
    return {"reused": False, "snapshot_dir": str(directory), "files": files}


# LLM: 续作复用快照前按清单逐文件核对 sha256+bytes：快照副本可能被改动、删除或替换成链接；
#   清单本身缺失/坏内容/结构不对同样视为不可信——拒绝而不是重建（重建会覆盖恢复依据，也会
#   掩盖磁盘问题）。任何一项不一致都在删除前中止（mc4-3104）。
# 函数用途: 校验已存在快照的清单与每个文件内容，不一致时抛出快照不可信错误。
def _verify_snapshot_manifest(directory: Path, meta_path: Path) -> None:
    try:
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise AgentsCleanupError("AGENTS_CLEANUP_SNAPSHOT_TAMPERED") from exc
    files = payload.get("files") if isinstance(payload, dict) else None
    if not isinstance(files, dict):
        raise AgentsCleanupError("AGENTS_CLEANUP_SNAPSHOT_TAMPERED")
    snapshot_dir = directory / "snapshot"
    for key, entry in files.items():
        _verify_snapshot_file(snapshot_dir, str(key), entry)


# 函数用途: 核对快照里单个文件与清单一致；链接、越界路径、读不动或内容不符都按篡改拒绝。
def _verify_snapshot_file(snapshot_dir: Path, key: str, entry: object) -> None:
    if not isinstance(entry, dict) or not _safe_snapshot_key(key):
        raise AgentsCleanupError("AGENTS_CLEANUP_SNAPSHOT_TAMPERED")
    target = snapshot_dir / key
    if target.is_symlink() or not target.is_file():
        raise AgentsCleanupError("AGENTS_CLEANUP_SNAPSHOT_TAMPERED")
    try:
        content = target.read_bytes()
    except OSError as exc:
        raise AgentsCleanupError("AGENTS_CLEANUP_SNAPSHOT_TAMPERED") from exc
    if hashlib.sha256(content).hexdigest() != str(entry.get("sha256") or ""):
        raise AgentsCleanupError("AGENTS_CLEANUP_SNAPSHOT_TAMPERED")
    if entry.get("bytes") != len(content):
        raise AgentsCleanupError("AGENTS_CLEANUP_SNAPSHOT_TAMPERED")


# 函数用途: 判断快照清单里的相对路径安全（拒绝空值、绝对路径与上级跳转）。
def _safe_snapshot_key(key: str) -> bool:
    if not key or key.startswith("/"):
        return False
    return ".." not in Path(key).parts


# 函数用途: 列出快照要保存的权威文件（含迁移自身的归档与事务记录）。
def _snapshot_sources(home_paths: object, directory: Path) -> dict[str, Path]:
    owner_home = Path(str(getattr(home_paths, "owner_home_dir", "") or ""))
    return {
        _SNAPSHOT_AGENTS: Path(str(getattr(home_paths, "owner_agents_md", "") or "")),
        "versions.jsonl": owner_home / "persona" / "versions.jsonl",
        "candidates.jsonl": Path(str(getattr(home_paths, "owner_memory_candidates_jsonl", "") or "")),
        "memory_policy.json": Path(str(getattr(home_paths, "owner_memory_policy_json", "") or "")),
        _ARCHIVE_NAME: directory / _ARCHIVE_NAME,
        _RECORD_NAME: directory / _RECORD_NAME,
    }


# LLM: 快照是删除的安全前提（2903）：任何一步读取/复制失败都在删除前中止；写完后按清单读回
#   校验 sha256，全部通过才继续。缺失、链接、非常规文件按「源不存在」跳过（合法缺失），
#   但存在却读不动一律抛出，绝不留半个坏快照继续删。
# 函数用途: 复制一个快照文件并记录 sha256/bytes；读取失败或读回校验失败时抛出。
def _copy_snapshot_file(source: Path, target: Path, files: dict[str, object], key: str) -> None:
    if not source.name or source.is_symlink() or not source.is_file():
        return
    content = source.read_text(encoding="utf-8")
    write_private_text_file_atomic(target, content)
    encoded = content.encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    readback = target.read_text(encoding="utf-8")
    if hashlib.sha256(readback.encode("utf-8")).hexdigest() != digest:
        raise OSError(f"snapshot readback mismatch: {key}")
    files[key] = {"sha256": digest, "bytes": len(encoded)}


# LLM: 全树复制沿用同一套「失败即中止」语义：任一条目读不动就抛出，由调用方在删除前停住。
# 函数用途: 递归复制 persona/backups 全树到快照，跳过链接与非常规文件。
def _copy_snapshot_tree(
    source_root: Path,
    target_root: Path,
    files: dict[str, object],
    prefix: str,
) -> None:
    if source_root.is_symlink() or not source_root.is_dir():
        return
    for path in sorted(source_root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(source_root).as_posix()
        _copy_snapshot_file(path, target_root / relative, files, f"{prefix}/{relative}")


# 函数用途: 返回 owner 的 persona/backups 目录。
def _owner_backups_dir(home_paths: object) -> Path:
    owner_home = Path(str(getattr(home_paths, "owner_home_dir", "") or ""))
    return owner_home / "persona" / "backups"


# LLM: 归档与线程快照失效都从快照的 AGENTS.md 解析条目内容：快照是迁移开始时的原文，
#   即使条目已被删除或替换，内容仍可按 entry_id 取回。
# 函数用途: 从快照的 AGENTS.md 解析 entry_id → 条目正文映射。
def _snapshot_entry_contents(directory: Path) -> dict[str, str]:
    path = directory / "snapshot" / _SNAPSHOT_AGENTS
    if not path.exists() or path.is_symlink() or not path.is_file():
        return {}
    contents: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("- "):
            continue
        content = stripped[2:].strip()
        if content:
            contents[persona_entry_id("agents", content)] = content
    return contents


# LLM: 归档先于删除，含被删条目正文与来源候选引用；重跑按 entry_id 去重，不重复归档；
#   内容优先取快照（迁移开始时原文），快照缺失时回退当前文档。
# 函数用途: 把本次将删除的条目追加到迁移归档，返回新增行数。
def _archive_planned(
    directory: Path,
    targets: list[dict[str, object]],
    candidate_service: object,
    snapshot_map: dict[str, str],
) -> int:
    archive_path = directory / _ARCHIVE_NAME
    existing = _read_archive_entry_ids(archive_path)
    records: list[dict[str, object]] = []
    for item in targets:
        entry_id = str(item["entry_id"])
        if entry_id in existing:
            continue
        candidate_id = str(item.get("candidate_id") or "")
        records.append(
            {
                "entry_id": entry_id,
                "candidate_id": candidate_id,
                "version": int(item.get("version") or 0),
                "content": snapshot_map.get(entry_id) or str(item.get("content") or ""),
                "source_message_refs": _candidate_refs(candidate_service, candidate_id),
                "archived_at": _now_iso(),
            }
        )
    if records:
        append_private_jsonl_records(archive_path, records)
    return len(records)


# LLM: 来源候选引用只做结构化复制；候选不存在或读取失败时归档仍保留（引用为空），不阻断迁移。
# 函数用途: 读取一条候选的来源消息引用。
def _candidate_refs(candidate_service: object, candidate_id: str) -> list[dict[str, object]]:
    if not candidate_id or candidate_service is None:
        return []
    try:
        candidate = candidate_service.get(candidate_id)
    except Exception:
        return []
    refs = getattr(candidate, "source_message_refs", ()) or ()
    return [dict(ref) for ref in refs if isinstance(ref, dict)]


# LLM: 归档去重只按 entry_id：同一条目重跑时不重复写正文。
# 函数用途: 读取归档里已有的 entry_id 集合。
def _read_archive_entry_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    report = read_jsonl_objects_report(path, context="agents_migration.archive")
    return {str(row.get("entry_id") or "") for row in report.records}


# LLM: 调用方已持 policy 锁，使用锁内入口不重复 flock；名单只追加、幂等；续作以原请求会话为准，新请求带了与记录
#   冲突的会话时在删除前就拒绝（结构化原因码），不能用新请求的空列表覆盖原请求。
# 函数用途: 把生效的会话列表追加到学习排除名单，返回新增条数。
def _append_conversations(
    home_paths: object,
    conversations: tuple[str, ...],
    request: AgentsCleanupRequest,
) -> int:
    if not conversations:
        return 0
    return append_learning_exclusions_locked(home_paths, _conversation_entries(conversations, request))


# 函数用途: 计算本次删除阶段生效的排除会话（记录优先；新请求与记录冲突时拒绝）。
def _effective_conversations(
    record: dict,
    request: AgentsCleanupRequest,
) -> tuple[str, ...]:
    recorded = tuple(
        str(item) for item in record.get("conversations") or () if str(item).strip()
    )
    requested = tuple(
        str(item).strip() for item in request.conversations if str(item).strip()
    )
    if requested and requested != recorded:
        raise AgentsCleanupError("AGENTS_CLEANUP_CONVERSATIONS_CONFLICT")
    return recorded or requested


# 函数用途: 把会话列表转成学习排除条目。
def _conversation_entries(
    conversations: tuple[str, ...],
    request: AgentsCleanupRequest,
) -> tuple[LearningExclusionEntry, ...]:
    return tuple(
        LearningExclusionEntry(
            channel_conversation_id=str(conversation).strip(),
            reason=request.reason or "agents-cleanup",
            added_at=_now_iso(),
            added_by=request.actor,
        )
        for conversation in conversations
        if str(conversation).strip()
    )


# LLM: 失效是清理的一部分：被删条目的内容从快照解析，即使条目已从文档删除也能失效对应快照；
#   目录不存在（没人冻结过）时自然返回 0。
# 函数用途: 让包含被清理条目的线程快照受控失效，返回失效数。
def _expire_thread_snapshots(persona: PersonaRepository, contents: list[str]) -> int:
    if not contents:
        return 0
    from ..prompting_parts.thread_persona import expire_thread_personas_containing

    return expire_thread_personas_containing(persona.owner_home, contents)


# 函数用途: 按计划条目从快照映射里取回被清理条目的内容列表（供快照失效）。
def _planned_contents(record: dict, snapshot_map: dict[str, str]) -> list[str]:
    contents: list[str] = []
    for item in record.get("planned_entries") or ():
        if not isinstance(item, dict):
            continue
        content = snapshot_map.get(str(item.get("entry_id") or ""))
        if content:
            contents.append(content)
    return contents


# LLM: migration_id 会进入目录名，只允许安全字符，防止路径穿越。
# 函数用途: 校验并返回迁移编号。
def _validated_migration_id(value: str) -> str:
    migration_id = str(value or "").strip()
    if not _MIGRATION_ID_PATTERN.match(migration_id):
        raise ValueError("migration_id must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}")
    return migration_id


# LLM: 迁移目录固定在 owner persona/migrations 下，按 migration_id 寻址。
# 函数用途: 返回一次迁移的目录路径。
def _migration_dir(home_paths: object, migration_id: str) -> Path:
    return _migration_dir_for(
        Path(str(getattr(home_paths, "owner_home_dir", "") or "")), migration_id
    )


# 函数用途: 由 owner home 与迁移编号定位迁移目录（删除批次的账本前提读取复用）。
def _migration_dir_for(owner_home: Path, migration_id: str) -> Path:
    return owner_home / "persona" / "migrations" / f"agents-migration-{migration_id}"


# LLM: 专用目录按 0700 收敛；chmod 失败不阻断（后续文件写仍按 0600）。
# 函数用途: 确保迁移目录存在并尽量收紧权限。
def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass


# 函数用途: 写事务记录到迁移目录（原子替换）。
def _write_record(directory: Path, record: dict) -> None:
    _ensure_dir(directory)
    write_private_json_file_atomic(directory / _RECORD_NAME, record)


# 函数用途: 返回当前 UTC 时间的 ISO 字符串。
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "AgentsCleanupError",
    "AgentsCleanupPlan",
    "AgentsCleanupRequest",
    "apply_agents_cleanup",
    "plan_agents_cleanup",
]
