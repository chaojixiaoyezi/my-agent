from __future__ import annotations

"""Memory v2 retention 的只读扫描与计划生成。"""

# LLM: 本模块只能读取 typed policy、ConversationThread、task/subagent state 和文件元数据；不得执行删除。
# 模块用途: 为每个 owner 构造可重验证的 retention dry-run 计划，并在坏状态时关闭式报告错误。

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..common.json_io import read_json_object_report, read_jsonl_objects_report
from ..conversation.models import ConversationThread
from .candidates import CandidateService
from .retention_models import (
    TASK_TERMINAL_STATUSES,
    MemoryRetentionAction,
    MemoryRetentionError,
    MemoryRetentionPolicy,
    MemoryRetentionReport,
    retention_action_id,
)

_DAY_SECONDS = 86_400
_SUBAGENT_SCRATCH_PATHS = (
    Path("inbox"),
    Path("outbox"),
    Path("compactions"),
    Path("artifacts") / "tool_outputs",
)


# LLM: plan 是纯读取；任一 policy/state/candidate ledger 损坏都会留 error，apply 必须拒绝该计划。
# 函数用途: 扫描完整会话、审计、Daily、工具大输出、候选、Curator、Compact 和终态任务。
def build_retention_plan(
    *,
    home: object,
    candidates: CandidateService,
    conversation_roots: tuple[Path, ...] = (),
    now: datetime | None = None,
) -> MemoryRetentionReport:
    current = _normalize_now(now)
    policy_path = Path(home.owner_retention_json)
    policy, policy_error = _load_policy(policy_path)
    fingerprint = file_fingerprint(policy_path)
    if policy_error is not None or policy is None:
        return MemoryRetentionReport(
            applied=False,
            actions=(),
            errors=((policy_error,) if policy_error is not None else ()),
            policy_fingerprint=fingerprint,
        )
    if policy.legal_hold:
        return MemoryRetentionReport(
            applied=False,
            actions=(),
            legal_hold=True,
            policy_fingerprint=fingerprint,
        )
    context = _RetentionScanContext(
        home=home,
        candidates=candidates,
        policy=policy,
        conversation_roots=_conversation_roots(home, conversation_roots),
        now=current,
    )
    actions = _collect_retention_actions(context)
    unique = {action.action_id: action for action in actions}
    ordered = tuple(sorted(unique.values(), key=lambda item: (item.category, str(item.path))))
    return MemoryRetentionReport(
        applied=False,
        actions=ordered,
        errors=tuple(context.errors),
        policy_fingerprint=fingerprint,
    )


# LLM: One scan context carries typed policy and shared fail-closed errors across all categories.
# 类用途: 汇总一次只读 retention 计划所需的 owner、策略、会话根和扫描时间。
@dataclass
class _RetentionScanContext:
    home: object
    candidates: CandidateService
    policy: MemoryRetentionPolicy
    conversation_roots: tuple[Path, ...]
    now: datetime
    errors: list[MemoryRetentionError] = field(default_factory=list)

    # LLM: Legal-hold task IDs are normalized once and reused by every scanner.
    # 函数用途: 返回禁止清理的任务编号集合。
    @property
    def held_task_ids(self) -> frozenset[str]:
        return frozenset(self.policy.legal_hold_task_ids)


# LLM: Every retention category contributes proposals to one plan; none of these scanners mutate disk.
# 函数用途: 按任务保护关系组合会话、候选、运行记录和回收站的清理动作。
def _collect_retention_actions(context: _RetentionScanContext) -> list[MemoryRetentionAction]:
    task_actions, selected_task_roots = _task_actions(
        home=context.home,
        policy=context.policy,
        held_task_ids=context.held_task_ids,
        now=context.now,
        errors=context.errors,
    )
    actions = list(task_actions)
    actions.extend(_protected_runtime_actions(context, selected_task_roots))
    actions.extend(_aged_file_actions(context, selected_task_roots))
    actions.extend(
        _trash_actions(
            home=context.home,
            days=context.policy.trash_days,
            now=context.now,
            errors=context.errors,
        )
    )
    return actions


# LLM: Runtime artifacts with task/conversation authority share the same legal-hold and terminal-state inputs.
# 函数用途: 汇总工具正文、子代理临时目录、候选和完整会话动作。
def _protected_runtime_actions(
    context: _RetentionScanContext,
    selected_task_roots: frozenset[Path],
) -> list[MemoryRetentionAction]:
    common = {
        "home": context.home,
        "policy": context.policy,
        "held_task_ids": context.held_task_ids,
        "selected_task_roots": selected_task_roots,
        "now": context.now,
        "errors": context.errors,
    }
    actions = _tool_output_actions(**common)
    actions.extend(_subagent_scratch_actions(**common))
    actions.extend(
        _candidate_actions(
            candidates=context.candidates,
            policy=context.policy,
            now=context.now,
            errors=context.errors,
        )
    )
    actions.extend(
        _conversation_actions(
            home=context.home,
            policy=context.policy,
            held_task_ids=context.held_task_ids,
            roots=context.conversation_roots,
            now=context.now,
            errors=context.errors,
        )
    )
    return actions


# LLM: Simple age categories are still explicit policy fields; this table does not infer deletion classes from paths.
# 函数用途: 汇总 audit、Daily、Curator、Compact、cache 和 tmp 的按龄动作。
def _aged_file_actions(
    context: _RetentionScanContext,
    selected_task_roots: frozenset[Path],
) -> list[MemoryRetentionAction]:
    home = context.home
    policy = context.policy
    categories = (
        ("audit", Path(home.owner_audit_dir), policy.audit_days, "*.jsonl"),
        ("daily", Path(home.owner_memory_daily_dir), policy.daily_days, "*.jsonl"),
        ("curator_run", Path(home.owner_memory_curator_runs_dir), policy.curator_run_days, "*.jsonl"),
        ("compact", Path(home.owner_compact_dir), policy.compact_days, "*"),
        ("cache", Path(home.owner_cache_dir), policy.cache_days, "*"),
        ("tmp", Path(home.owner_tmp_dir), policy.tmp_days, "*"),
    )
    actions: list[MemoryRetentionAction] = []
    for category, root, days, pattern in categories:
        actions.extend(
            _expired_file_actions(
                category=category,
                root=root,
                days=days,
                pattern=pattern,
                now=context.now,
                excluded_roots=selected_task_roots,
                errors=context.errors,
            )
        )
    return actions


# LLM: runtime 只接受当前 schema；旧 v1 key 由 MemoryMigrationService 一次性转换。
# 函数用途: 严格读取 retention.json 并给损坏返回稳定错误。
def _load_policy(
    path: Path,
) -> tuple[MemoryRetentionPolicy | None, MemoryRetentionError | None]:
    report = read_json_object_report(path, context="memory_retention.policy")
    if report.load_error is not None:
        return None, MemoryRetentionError(
            "MEMORY_RETENTION_POLICY_UNREADABLE",
            str(path),
            str(report.load_error.get("error_type") or "unreadable JSON"),
        )
    try:
        return MemoryRetentionPolicy.from_payload(report.payload), None
    except (TypeError, ValueError) as exc:
        return None, MemoryRetentionError(
            "MEMORY_RETENTION_POLICY_INVALID",
            str(path),
            str(exc),
        )


# LLM: 候选清理只允许 rejected/expired/superseded 且严格早于 cutoff 的精确 candidate_id。
# 函数用途: 规划非活跃候选账本记录删除。
def _candidate_actions(
    *,
    candidates: CandidateService,
    policy: MemoryRetentionPolicy,
    now: datetime,
    errors: list[MemoryRetentionError],
) -> list[MemoryRetentionAction]:
    if policy.rejected_candidate_days <= 0:
        return []
    cutoff = now.timestamp() - policy.rejected_candidate_days * _DAY_SECONDS
    try:
        records = candidates.list(statuses={"rejected", "expired", "superseded"})
    except Exception as exc:  # noqa: BLE001 - convert repository corruption to stable plan error.
        errors.append(
            MemoryRetentionError(
                "MEMORY_RETENTION_CANDIDATES_UNREADABLE",
                str(candidates.path),
                f"{type(exc).__name__}: {exc}",
            )
        )
        return []
    actions: list[MemoryRetentionAction] = []
    authority_fingerprint = file_fingerprint(candidates.path)
    for record in records:
        updated = _timestamp(record.updated_at)
        if updated is None:
            errors.append(
                MemoryRetentionError(
                    "MEMORY_RETENTION_CANDIDATE_TIMESTAMP_INVALID",
                    str(candidates.path),
                    f"candidate {record.candidate_id} has invalid updated_at",
                )
            )
            continue
        if updated >= cutoff:
            continue
        actions.append(
            _action(
                category="rejected_candidate",
                operation="candidate_delete",
                path=candidates.path,
                reason=f"terminal_candidate_older_than_{policy.rejected_candidate_days}_days",
                proof=_ActionProof(
                    authority_path=candidates.path,
                    authority_fingerprint=authority_fingerprint,
                    record_id=record.candidate_id,
                    cutoff_timestamp=cutoff,
                ),
            )
        )
    return actions


# LLM: 完成任务必须有明确 terminal status 和 updated_at；未知/运行中状态永远保护整个恢复目录。
# 函数用途: 规划超过 completed_task_days 的终态任务移入回收站。
def _task_actions(
    *,
    home: object,
    policy: MemoryRetentionPolicy,
    held_task_ids: frozenset[str],
    now: datetime,
    errors: list[MemoryRetentionError],
) -> tuple[list[MemoryRetentionAction], frozenset[Path]]:
    if policy.completed_task_days <= 0:
        return [], frozenset()
    root = Path(home.owner_tasks_dir)
    cutoff = now.timestamp() - policy.completed_task_days * _DAY_SECONDS
    actions: list[MemoryRetentionAction] = []
    selected: set[Path] = set()
    for state_path in sorted(root.rglob("work/state.json")) if root.exists() else ():
        task_root = state_path.parent.parent
        state = _read_state(state_path, errors, code="MEMORY_RETENTION_TASK_STATE_INVALID")
        if state is None or not _safe_child(task_root, root) or task_root.is_symlink():
            continue
        task_id = str(state.get("task_id") or "").strip()
        status = str(state.get("status") or "").strip().upper()
        if not task_id or not status:
            errors.append(_state_error("MEMORY_RETENTION_TASK_STATE_INVALID", state_path))
            continue
        if task_id in held_task_ids or status not in TASK_TERMINAL_STATUSES:
            continue
        updated = _timestamp(state.get("updated_at"))
        if updated is None:
            errors.append(_state_error("MEMORY_RETENTION_TASK_TIMESTAMP_INVALID", state_path))
            continue
        if updated >= cutoff:
            continue
        destination = _trash_destination(
            home,
            category="completed_task",
            path=task_root,
            now=now,
        )
        actions.append(
            _action(
                category="completed_task",
                operation="trash_tree",
                path=task_root,
                reason=f"terminal_{status}_older_than_{policy.completed_task_days}_days",
                destination=destination,
                proof=_ActionProof(
                    authority_path=state_path,
                    authority_status=status,
                    authority_fingerprint=file_fingerprint(state_path),
                    source_fingerprint=tree_fingerprint(task_root),
                    record_id=task_id,
                    cutoff_timestamp=cutoff,
                ),
            )
        )
        selected.add(task_root.resolve(strict=False))
    return actions, frozenset(selected)


# LLM: 工具正文只有在父任务结构化终态且终态时间超过 cutoff 后可清；运行中任务一律保留恢复材料。
# 函数用途: 规划 task work 下 tool_outputs 大正文目录移入回收站。
def _tool_output_actions(
    *,
    home: object,
    policy: MemoryRetentionPolicy,
    held_task_ids: frozenset[str],
    selected_task_roots: frozenset[Path],
    now: datetime,
    errors: list[MemoryRetentionError],
) -> list[MemoryRetentionAction]:
    if policy.tool_output_days_after_terminal <= 0:
        return []
    root = Path(home.owner_tasks_dir)
    cutoff = now.timestamp() - policy.tool_output_days_after_terminal * _DAY_SECONDS
    actions: list[MemoryRetentionAction] = []
    for state_path in sorted(root.rglob("work/state.json")) if root.exists() else ():
        task_root = state_path.parent.parent
        if task_root.resolve(strict=False) in selected_task_roots:
            continue
        state = _read_state(state_path, errors, code="MEMORY_RETENTION_TASK_STATE_INVALID")
        if state is None:
            continue
        task_id = str(state.get("task_id") or "").strip()
        status = str(state.get("status") or "").strip().upper()
        updated = _timestamp(state.get("updated_at"))
        if (
            not task_id
            or task_id in held_task_ids
            or status not in TASK_TERMINAL_STATUSES
            or updated is None
            or updated >= cutoff
        ):
            continue
        for output_dir in _task_tool_output_dirs(task_root):
            actions.append(
                _action(
                    category="tool_output",
                    operation="trash_tree",
                    path=output_dir,
                    reason=(
                        f"terminal_{status}_tool_output_older_than_"
                        f"{policy.tool_output_days_after_terminal}_days"
                    ),
                    destination=_trash_destination(
                        home,
                        category="tool_output",
                        path=output_dir,
                        now=now,
                    ),
                    proof=_ActionProof(
                        authority_path=state_path,
                        authority_status=status,
                        authority_fingerprint=file_fingerprint(state_path),
                        source_fingerprint=tree_fingerprint(output_dir),
                        record_id=task_id,
                        cutoff_timestamp=cutoff,
                    ),
                )
            )
    return actions


# LLM: 旧 subagent scratch 清理继续使用同一 v2 plan/apply；只有子代理自身 terminal state 能授权删除。
# 函数用途: 规划终态子代理的 inbox/outbox/compactions/tool_outputs 临时目录。
def _subagent_scratch_actions(
    *,
    home: object,
    policy: MemoryRetentionPolicy,
    held_task_ids: frozenset[str],
    selected_task_roots: frozenset[Path],
    now: datetime,
    errors: list[MemoryRetentionError],
) -> list[MemoryRetentionAction]:
    if policy.subagent_scratch_days <= 0:
        return []
    task_root_dir = Path(home.owner_tasks_dir)
    cutoff = now.timestamp() - policy.subagent_scratch_days * _DAY_SECONDS
    actions: list[MemoryRetentionAction] = []
    for agents_dir in sorted(task_root_dir.rglob("work/agents")) if task_root_dir.exists() else ():
        task_root = agents_dir.parent.parent
        if task_root.resolve(strict=False) in selected_task_roots:
            continue
        task_state = _read_state(
            task_root / "work" / "state.json",
            errors,
            code="MEMORY_RETENTION_TASK_STATE_INVALID",
        )
        if task_state is None or str(task_state.get("task_id") or "") in held_task_ids:
            continue
        for run_root in sorted(path for path in agents_dir.iterdir() if path.is_dir()):
            authority = _subagent_state_path(run_root)
            if authority is None:
                continue
            state = _read_state(
                authority,
                errors,
                code="MEMORY_RETENTION_SUBAGENT_STATE_INVALID",
            )
            if state is None:
                continue
            status = str(state.get("status") or "").strip().upper()
            updated = _timestamp(state.get("updated_at"))
            if status not in TASK_TERMINAL_STATUSES or updated is None or updated >= cutoff:
                continue
            for relative in _SUBAGENT_SCRATCH_PATHS:
                scratch = run_root / relative
                if not scratch.exists() or scratch.is_symlink():
                    continue
                actions.append(
                    _action(
                        category="subagent_scratch",
                        operation="trash_tree",
                        path=scratch,
                        reason=(
                            f"terminal_{status}_scratch_older_than_"
                            f"{policy.subagent_scratch_days}_days"
                        ),
                        destination=_trash_destination(
                            home,
                            category="subagent_scratch",
                            path=scratch,
                            now=now,
                        ),
                        proof=_ActionProof(
                            authority_path=authority,
                            authority_status=status,
                            authority_fingerprint=file_fingerprint(authority),
                            source_fingerprint=tree_fingerprint(scratch),
                            cutoff_timestamp=cutoff,
                        ),
                    )
                )
    return actions


# LLM: ConversationStore 原文按 thread 整体处理；有 active_task_ids、held task 或坏 transcript 时绝不删除。
# 函数用途: 规划超过 conversation_days 的完整会话及其 thread-scoped 附属文件移入回收站。
def _conversation_actions(
    *,
    home: object,
    policy: MemoryRetentionPolicy,
    held_task_ids: frozenset[str],
    roots: tuple[Path, ...],
    now: datetime,
    errors: list[MemoryRetentionError],
) -> list[MemoryRetentionAction]:
    if policy.conversation_days <= 0:
        return []
    cutoff = now.timestamp() - policy.conversation_days * _DAY_SECONDS
    actions: list[MemoryRetentionAction] = []
    for root in roots:
        for thread_path in sorted((root / "threads").glob("*.json")):
            report = read_json_object_report(
                thread_path,
                context="memory_retention.conversation_thread",
            )
            if report.load_error is not None:
                errors.append(
                    MemoryRetentionError(
                        "MEMORY_RETENTION_CONVERSATION_STATE_INVALID",
                        str(thread_path),
                        str(report.load_error.get("error_type") or "unreadable thread"),
                    )
                )
                continue
            try:
                thread = ConversationThread.from_dict(report.payload)
            except (TypeError, ValueError) as exc:
                errors.append(
                    MemoryRetentionError(
                        "MEMORY_RETENTION_CONVERSATION_STATE_INVALID",
                        str(thread_path),
                        f"{type(exc).__name__}: {exc}",
                    )
                )
                continue
            if thread.thread_id != thread_path.stem or not thread.thread_id:
                errors.append(
                    MemoryRetentionError(
                        "MEMORY_RETENTION_CONVERSATION_ID_MISMATCH",
                        str(thread_path),
                        "thread_id does not match authority filename",
                    )
                )
                continue
            if thread.active_task_ids or held_task_ids.intersection(thread.task_ids):
                continue
            if thread.updated_at <= 0:
                errors.append(
                    _state_error("MEMORY_RETENTION_CONVERSATION_TIMESTAMP_INVALID", thread_path)
                )
                continue
            if thread.updated_at >= cutoff:
                continue
            related, related_errors = _conversation_related_paths(root, thread)
            errors.extend(related_errors)
            if related_errors:
                continue
            actions.append(
                _action(
                    category="conversation",
                    operation="conversation_trash",
                    path=thread_path,
                    reason=f"inactive_conversation_older_than_{policy.conversation_days}_days",
                    destination=_trash_destination(
                        home,
                        category="conversation",
                        path=thread_path,
                        now=now,
                    ),
                    proof=_ActionProof(
                        authority_path=thread_path,
                        authority_status=thread.status,
                        authority_fingerprint=file_fingerprint(thread_path),
                        source_fingerprint=paths_fingerprint(related),
                        record_id=thread.thread_id,
                        cutoff_timestamp=cutoff,
                        related_paths=related,
                    ),
                )
            )
    return actions


# LLM: thread-scoped 文件通过结构化 ID 组成；message JSONL 坏行会阻断整个会话删除。
# 函数用途: 收集一个会话的原文、任务链接、目标、观察、guidance、claim 和 wake 文件。
def _conversation_related_paths(
    root: Path,
    thread: ConversationThread,
) -> tuple[tuple[Path, ...], list[MemoryRetentionError]]:
    thread_id = thread.thread_id
    candidates = [
        root / "threads" / f"{thread_id}.json",
        root / "messages" / f"{thread_id}.jsonl",
        root / "observations" / f"{thread_id}.jsonl",
        root / "goals" / f"{thread_id}.json",
        root / "background_claims" / f"{thread_id}.json",
        root / "guidance" / f"thread.{thread_id}.jsonl",
        *(root / "tasks" / f"{task_id}.json" for task_id in thread.task_ids),
        *(root / "wake_queue" / "dedupe").glob(f"{thread_id}.*.json"),
    ]
    errors: list[MemoryRetentionError] = []
    messages = root / "messages" / f"{thread_id}.jsonl"
    if messages.exists():
        report = read_jsonl_objects_report(
            messages,
            context="memory_retention.conversation_messages",
        )
        if report.load_errors or any(
            str(row.get("thread_id") or "") != thread_id for row in report.records
        ):
            errors.append(
                MemoryRetentionError(
                    "MEMORY_RETENTION_CONVERSATION_MESSAGES_INVALID",
                    str(messages),
                    "conversation transcript contains unreadable or cross-thread rows",
                )
            )
    for queue in (root / "wake_queue" / "urgent", root / "wake_queue" / "normal"):
        for path in queue.glob("*.json") if queue.exists() else ():
            report = read_json_object_report(
                path,
                context="memory_retention.conversation_wake",
            )
            if report.load_error is not None:
                errors.append(
                    MemoryRetentionError(
                        "MEMORY_RETENTION_CONVERSATION_WAKE_INVALID",
                        str(path),
                        "wake state is unreadable",
                    )
                )
                continue
            if str(report.payload.get("thread_id") or "") == thread_id:
                candidates.append(path)
    paths = tuple(
        sorted(
            {
                path.resolve(strict=False)
                for path in candidates
                if path.exists() and path.is_file() and not path.is_symlink()
            },
            key=str,
        )
    )
    return paths, errors


# LLM: 普通按龄文件只根据配置 cutoff 和 mtime；symlink 被记录为错误而不是跟随。
# 函数用途: 规划 audit、Daily、Curator run、Compact、cache 和 tmp 文件清理。
def _expired_file_actions(
    *,
    category: str,
    root: Path,
    days: int,
    pattern: str,
    now: datetime,
    excluded_roots: frozenset[Path],
    errors: list[MemoryRetentionError],
) -> list[MemoryRetentionAction]:
    if days <= 0 or not root.exists():
        return []
    cutoff = now.timestamp() - days * _DAY_SECONDS
    actions: list[MemoryRetentionAction] = []
    for path in sorted(root.rglob(pattern)):
        if path.is_symlink():
            errors.append(
                MemoryRetentionError(
                    "MEMORY_RETENTION_SYMLINK_SKIPPED",
                    str(path),
                    "retention never follows symbolic links",
                )
            )
            continue
        if not path.is_file() or _inside_any(path, excluded_roots):
            continue
        mtime = _mtime(path)
        if mtime >= cutoff:
            continue
        actions.append(
            _action(
                category=category,
                operation="delete_file",
                path=path,
                reason=f"older_than_{days}_days",
                proof=_ActionProof(
                    source_fingerprint=file_fingerprint(path),
                    cutoff_timestamp=cutoff,
                ),
            )
        )
    return actions


# LLM: trash 永久清除只接受程序 tombstone 的 moved_at；未知散落文件继续按 mtime 清理并精确指纹。
# 函数用途: 规划回收站中过期容器和文件的最终删除。
def _trash_actions(
    *,
    home: object,
    days: int,
    now: datetime,
    errors: list[MemoryRetentionError],
) -> list[MemoryRetentionAction]:
    root = Path(home.owner_trash_dir)
    if days <= 0 or not root.exists():
        return []
    cutoff = now.timestamp() - days * _DAY_SECONDS
    actions: list[MemoryRetentionAction] = []
    retention_root = root / "retention"
    for tombstone in sorted(retention_root.glob("*/*/tombstone.json")) if retention_root.exists() else ():
        report = read_json_object_report(tombstone, context="memory_retention.trash_tombstone")
        moved = _timestamp(report.payload.get("moved_at")) if report.load_error is None else None
        if moved is None:
            errors.append(_state_error("MEMORY_RETENTION_TOMBSTONE_INVALID", tombstone))
            continue
        if moved < cutoff:
            container = tombstone.parent
            actions.append(
                _action(
                    category="trash",
                    operation="delete_tree",
                    path=container,
                    reason=f"trashed_older_than_{days}_days",
                    proof=_ActionProof(
                        authority_path=tombstone,
                        authority_fingerprint=file_fingerprint(tombstone),
                        source_fingerprint=tree_fingerprint(container),
                        cutoff_timestamp=cutoff,
                    ),
                )
            )
    return actions


# LLM: 默认只发现 owner runtime 下标准 ConversationStore；显式 roots 仍拒绝根目录和 symlink。
# 函数用途: 形成去重后的会话存储根集合。
def _conversation_roots(home: object, explicit: tuple[Path, ...]) -> tuple[Path, ...]:
    discovered = list(explicit)
    runtime = Path(home.owner_workspace_dir) / "runtime" / "workspaces"
    if runtime.exists():
        discovered.extend(path for path in runtime.glob("*/conversations") if path.is_dir())
    result: list[Path] = []
    for raw in discovered:
        path = Path(raw).expanduser().resolve(strict=False)
        if path == Path(path.anchor) or path.is_symlink() or not (path / "threads").is_dir():
            continue
        result.append(path)
    return tuple(dict.fromkeys(result))


# LLM: Proof fields bind a plan action to exact authority/source snapshots for apply-time revalidation.
# 类用途: 聚合 retention 动作的状态权威、指纹、记录编号、截止时间和关联路径。
@dataclass(frozen=True)
class _ActionProof:
    authority_path: Path | None = None
    authority_status: str = ""
    authority_fingerprint: str = ""
    source_fingerprint: str = ""
    record_id: str = ""
    cutoff_timestamp: float | None = None
    related_paths: tuple[Path, ...] = ()


# LLM: The action helper generates one stable ID and prevents scanners from defining competing idempotency semantics.
# 函数用途: 将基础动作字段与可重验证证据组合成完整 retention 动作。
def _action(
    *,
    category: str,
    operation: str,
    path: Path,
    reason: str,
    destination: Path | None = None,
    proof: _ActionProof | None = None,
) -> MemoryRetentionAction:
    evidence = proof or _ActionProof()
    return MemoryRetentionAction(
        action_id=retention_action_id(
            category=category,
            operation=operation,
            path=path,
            record_id=evidence.record_id,
            cutoff_timestamp=evidence.cutoff_timestamp,
        ),
        category=category,
        operation=operation,
        path=path,
        reason=reason,
        destination=destination,
        authority_path=evidence.authority_path,
        authority_status=evidence.authority_status,
        authority_fingerprint=evidence.authority_fingerprint,
        source_fingerprint=evidence.source_fingerprint,
        record_id=evidence.record_id,
        cutoff_timestamp=evidence.cutoff_timestamp,
        related_paths=evidence.related_paths,
    )


# LLM: 状态读取不容忍坏 JSON 或空对象；调用方据此保护相应目录。
# 函数用途: 加载 task/subagent state 并记录稳定错误。
def _read_state(
    path: Path,
    errors: list[MemoryRetentionError],
    *,
    code: str,
) -> dict[str, Any] | None:
    report = read_json_object_report(path, context="memory_retention.state")
    if report.load_error is not None or not report.payload:
        errors.append(_state_error(code, path))
        return None
    return report.payload


# LLM: 任务输出目录只认规范 work/blobs 和已有 subagent artifacts 结构，不按正文或文件名猜任务状态。
# 函数用途: 找出一个任务下可按终态清理的工具大输出目录。
def _task_tool_output_dirs(task_root: Path) -> tuple[Path, ...]:
    canonical = task_root / "work" / "blobs" / "tool_outputs"
    found = [canonical] if canonical.is_dir() and not canonical.is_symlink() else []
    agents = task_root / "work" / "agents"
    if agents.exists():
        found.extend(
            path
            for path in agents.glob("*/artifacts/tool_outputs")
            if path.is_dir() and not path.is_symlink()
        )
    return tuple(sorted(dict.fromkeys(found), key=str))


# LLM: canonical_state 优先于 state，二者都不存在时不猜子代理终态。
# 函数用途: 定位子代理唯一可用状态权威。
def _subagent_state_path(run_root: Path) -> Path | None:
    for name in ("canonical_state.json", "state.json"):
        path = run_root / name
        if path.is_file() and not path.is_symlink():
            return path
    return None


# LLM: trash 目标绑定时间、类别和源路径 hash；绝不能落到 owner_trash_dir 之外。
# 函数用途: 为可恢复删除生成唯一目标目录。
def _trash_destination(home: object, *, category: str, path: Path, now: datetime) -> Path:
    digest = hashlib.sha256(str(path.resolve(strict=False)).encode("utf-8")).hexdigest()[:12]
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    return Path(home.owner_trash_dir) / "retention" / category / f"{stamp}-{path.name}-{digest}"


# LLM: file fingerprint 是 apply 重验证证据，missing 与真实空文件必须区分。
# 函数用途: 计算单文件 SHA-256 或 missing 标记。
def file_fingerprint(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return "missing"


# LLM: tree fingerprint 组合相对路径、类型和文件 hash；symlink 以显式标记参与漂移检测但不会跟随。
# 函数用途: 计算目录在 plan 时的稳定快照指纹。
def tree_fingerprint(path: Path) -> str:
    if not path.exists():
        return "missing"
    if path.is_file():
        return file_fingerprint(path)
    material: list[str] = []
    for item in sorted(path.rglob("*")):
        relative = item.relative_to(path)
        if item.is_symlink():
            material.append(f"L:{relative}")
        elif item.is_file():
            material.append(f"F:{relative}:{file_fingerprint(item)}")
        elif item.is_dir():
            material.append(f"D:{relative}")
    return hashlib.sha256("\n".join(material).encode("utf-8")).hexdigest()


# LLM: related path hash 同时绑定文件路径和正文 hash，任一新增/删除/修改会阻止旧 conversation plan。
# 函数用途: 计算一组精确文件的稳定指纹。
def paths_fingerprint(paths: tuple[Path, ...]) -> str:
    material = "\n".join(
        f"{path}:{file_fingerprint(path)}" for path in sorted(paths, key=str)
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


# LLM: 时间只用于保留期 cutoff，不参与长期事实冲突或真伪判定。
# 函数用途: 将 epoch 或 ISO 时间转换为 UTC epoch。
def _timestamp(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if float(value) > 0 else None
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except (OverflowError, ValueError):
        return None


# LLM: naive 测试时间按 UTC 解释，生产默认始终 aware UTC。
# 函数用途: 规范当前 retention 运行时间。
def _normalize_now(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


# LLM: 路径包含检查使用 resolve，不接受简单字符串前缀。
# 函数用途: 判断目标是否位于一个权威根目录内。
def _safe_child(path: Path, root: Path) -> bool:
    try:
        relative = path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except (OSError, RuntimeError, ValueError):
        return False
    return bool(relative.parts)


# LLM: 已整体选中的 task 不再生成内部文件动作，避免双删和状态歧义。
# 函数用途: 判断一个文件是否落在任一排除目录内。
def _inside_any(path: Path, roots: frozenset[Path]) -> bool:
    resolved = path.resolve(strict=False)
    for root in roots:
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


# LLM: mtime 读取失败返回正无穷，保证扫描不会把不可 stat 文件误判为很旧。
# 函数用途: 读取文件修改时间用于按龄类别。
def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return float("inf")


# LLM: 状态错误保持稳定 code/path，不泄露状态文件正文。
# 函数用途: 构造一条状态损坏诊断。
def _state_error(code: str, path: Path) -> MemoryRetentionError:
    return MemoryRetentionError(code, str(path), "structured retention state is invalid")


__all__ = [
    "build_retention_plan",
    "file_fingerprint",
    "paths_fingerprint",
    "tree_fingerprint",
]
