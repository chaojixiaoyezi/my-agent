from __future__ import annotations

"""Memory v2 retention 计划的重验证与执行。"""

# LLM: apply 只能执行刚在同一锁内生成的 typed actions；每个目标仍需 fingerprint/status/cutoff 二次核验。
# 模块用途: 安全删除文件、批量清理终态候选、把任务/会话移入回收站并记录无正文审计。

import json
import shutil
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from ..common.json_io import read_json_object_report, write_json_file_atomic
from ..conversation.models import ConversationThread
from ..gateway_parts.io import locked_file_transition, update_json_file_atomic
from ..io import append_jsonl
from .candidates import CandidateService
from .retention_models import (
    TASK_TERMINAL_STATUSES,
    MemoryRetentionAction,
    MemoryRetentionError,
    MemoryRetentionReport,
)
from .retention_scan import file_fingerprint, paths_fingerprint, tree_fingerprint


# LLM: candidate actions 必须一次批量删除，否则首条写入会让同一计划其余 ledger fingerprint 失效。
# 函数用途: 执行一份已验证计划，并返回每条动作状态及结构化错误。
def execute_retention_plan(
    *,
    home: object,
    candidates: CandidateService,
    plan: MemoryRetentionReport,
    now: datetime,
    allowed_external_roots: tuple[Path, ...] = (),
) -> MemoryRetentionReport:
    results: list[MemoryRetentionAction] = []
    errors: list[MemoryRetentionError] = []
    candidate_actions = tuple(
        action for action in plan.actions if action.operation == "candidate_delete"
    )
    if candidate_actions:
        candidate_results, candidate_errors = _apply_candidate_actions(
            candidates,
            candidate_actions,
        )
        results.extend(candidate_results)
        errors.extend(candidate_errors)
    for action in plan.actions:
        if action.operation == "candidate_delete":
            continue
        try:
            result = _apply_action(
                home=home,
                action=action,
                now=now,
                allowed_external_roots=allowed_external_roots,
            )
        except Exception as exc:  # noqa: BLE001 - one failed target is reported without hiding later independent targets.
            result = replace(
                action,
                status="failed",
                reason=f"{action.reason}; {type(exc).__name__}: {exc}",
            )
            errors.append(
                MemoryRetentionError(
                    "MEMORY_RETENTION_ACTION_FAILED",
                    str(action.path),
                    f"{type(exc).__name__}: {exc}",
                )
            )
        if result.status == "state_changed":
            errors.append(
                MemoryRetentionError(
                    "MEMORY_RETENTION_STATE_CHANGED",
                    str(action.path),
                    "target changed after planning; deletion skipped",
                )
            )
        results.append(result)
    report = MemoryRetentionReport(
        applied=True,
        actions=tuple(results),
        errors=tuple(errors),
        policy_fingerprint=plan.policy_fingerprint,
    )
    _append_retention_audit(home, report=report, now=now)
    return report


# LLM: policy/legal-hold 阶段未进入 action executor 时，仍通过同一无正文审计格式记录安全停止。
# 函数用途: 记录一份未执行的 retention 报告。
def record_retention_report(
    *,
    home: object,
    report: MemoryRetentionReport,
    now: datetime,
) -> None:
    _append_retention_audit(home, report=report, now=now)


# LLM: 候选在一个 ledger snapshot 上复核状态和时间后批量删除；正文绝不进入报告。
# 函数用途: 应用所有 rejected/expired/superseded 候选清理动作。
def _apply_candidate_actions(
    candidates: CandidateService,
    actions: tuple[MemoryRetentionAction, ...],
) -> tuple[list[MemoryRetentionAction], list[MemoryRetentionError]]:
    errors: list[MemoryRetentionError] = []
    if any(action.authority_fingerprint != file_fingerprint(candidates.path) for action in actions):
        return (
            [replace(action, status="state_changed") for action in actions],
            [
                MemoryRetentionError(
                    "MEMORY_RETENTION_STATE_CHANGED",
                    str(candidates.path),
                    "candidate ledger changed after planning; deletion skipped",
                )
            ],
        )
    try:
        by_id = {record.candidate_id: record for record in candidates.list()}
    except Exception as exc:  # noqa: BLE001 - repository corruption becomes one stable apply error.
        return (
            [replace(action, status="failed") for action in actions],
            [
                MemoryRetentionError(
                    "MEMORY_RETENTION_CANDIDATES_UNREADABLE",
                    str(candidates.path),
                    f"{type(exc).__name__}: {exc}",
                )
            ],
        )
    selected: list[str] = []
    results: list[MemoryRetentionAction] = []
    for action in actions:
        record = by_id.get(action.record_id)
        if record is None:
            results.append(replace(action, status="missing"))
            continue
        updated = _timestamp(record.updated_at)
        eligible = bool(
            record.status in {"rejected", "expired", "superseded"}
            and updated is not None
            and action.cutoff_timestamp is not None
            and updated < action.cutoff_timestamp
        )
        if not eligible:
            results.append(replace(action, status="state_changed"))
            errors.append(
                MemoryRetentionError(
                    "MEMORY_RETENTION_STATE_CHANGED",
                    str(candidates.path),
                    f"candidate {action.record_id} is no longer eligible",
                )
            )
            continue
        selected.append(action.record_id)
    if selected:
        candidates.delete_terminal(selected)
    selected_ids = set(selected)
    results.extend(
        replace(action, status="deleted")
        for action in actions
        if action.record_id in selected_ids
    )
    order = {action.action_id: index for index, action in enumerate(actions)}
    results.sort(key=lambda action: order[action.action_id])
    return results, errors


# LLM: operation 是封闭的 host-owned 执行枚举；未知值必须失败，不能从 reason 文本猜动作。
# 函数用途: 分派一条非候选 retention 动作。
def _apply_action(
    *,
    home: object,
    action: MemoryRetentionAction,
    now: datetime,
    allowed_external_roots: tuple[Path, ...],
) -> MemoryRetentionAction:
    allowed_roots = (Path(home.owner_home_dir), *allowed_external_roots)
    _assert_safe_target(action.path, allowed_roots)
    if action.operation == "delete_file":
        return _delete_file(action)
    if action.operation == "trash_tree":
        return _trash_tree(home=home, action=action, now=now)
    if action.operation == "delete_tree":
        return _delete_tree(action)
    if action.operation == "conversation_trash":
        return _trash_conversation(home=home, action=action, now=now)
    raise ValueError(f"unsupported retention operation: {action.operation}")


# LLM: 普通文件删除前核对 hash 和 mtime cutoff；内容变化或变新都跳过。
# 函数用途: 删除一个过期 audit/Daily/Curator/Compact/cache/tmp 文件。
def _delete_file(action: MemoryRetentionAction) -> MemoryRetentionAction:
    if not action.path.exists():
        return replace(action, status="missing")
    if (
        action.path.is_symlink()
        or file_fingerprint(action.path) != action.source_fingerprint
        or action.cutoff_timestamp is None
        or action.path.stat().st_mtime >= action.cutoff_timestamp
    ):
        return replace(action, status="state_changed")
    action.path.unlink()
    return replace(action, status="deleted")


# LLM: task/tool/subagent 目录需要 authority state 与目录 fingerprint 同时不变，才可移入可恢复回收站。
# 函数用途: 将一棵过期目录连同 tombstone 安全移动。
def _trash_tree(
    *,
    home: object,
    action: MemoryRetentionAction,
    now: datetime,
) -> MemoryRetentionAction:
    if not action.path.exists():
        return replace(action, status="missing")
    if not _authority_still_eligible(action) or tree_fingerprint(action.path) != action.source_fingerprint:
        return replace(action, status="state_changed")
    destination = _validated_destination(home, action)
    destination.mkdir(parents=True, exist_ok=False)
    payload = destination / "payload"
    _write_tombstone(destination, action=action, payload=payload, now=now, status="moving")
    try:
        shutil.move(str(action.path), str(payload))
        _write_tombstone(destination, action=action, payload=payload, now=now, status="trashed")
    except Exception:
        if payload.exists() and not action.path.exists():
            action.path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(payload), str(action.path))
        shutil.rmtree(destination, ignore_errors=True)
        raise
    return replace(action, status="trashed")


# LLM: trash purge 也必须核对 tombstone 和整棵容器 fingerprint，防止 plan 后加入的新文件被一起删除。
# 函数用途: 永久删除超过 trash_days 的 retention 容器。
def _delete_tree(action: MemoryRetentionAction) -> MemoryRetentionAction:
    if not action.path.exists():
        return replace(action, status="missing")
    if not _authority_still_eligible(action) or tree_fingerprint(action.path) != action.source_fingerprint:
        return replace(action, status="state_changed")
    shutil.rmtree(action.path)
    return replace(action, status="deleted")


# LLM: Conversation 原文按 thread 整体移动；thread lock 内重验 active task、updated_at、相关文件和共享索引。
# 函数用途: 将一个过期 ConversationStore thread 及附属文件移入同一回收容器。
def _trash_conversation(
    *,
    home: object,
    action: MemoryRetentionAction,
    now: datetime,
) -> MemoryRetentionAction:
    if not action.path.exists():
        return replace(action, status="missing")
    root = action.path.parent.parent
    with locked_file_transition(action.path):
        try:
            payload = json.loads(action.path.read_text(encoding="utf-8"))
            thread = ConversationThread.from_dict(payload)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return replace(action, status="state_changed")
        if (
            thread.thread_id != action.record_id
            or thread.active_task_ids
            or action.cutoff_timestamp is None
            or thread.updated_at >= action.cutoff_timestamp
            or file_fingerprint(action.path) != action.authority_fingerprint
            or paths_fingerprint(action.related_paths) != action.source_fingerprint
        ):
            return replace(action, status="state_changed")
        for path in action.related_paths:
            _assert_safe_target(path, (root,))
        bindings_path = root / "channel_bindings.json"
        latest_path = root / "user_latest_threads.json"
        bindings = _read_index(bindings_path)
        latest = _read_index(latest_path)
        destination = _validated_destination(home, action)
        destination.mkdir(parents=True, exist_ok=False)
        payload_root = destination / "payload"
        moved: list[tuple[Path, Path]] = []
        _write_tombstone(
            destination,
            action=action,
            payload=payload_root,
            now=now,
            status="moving",
        )
        try:
            for source in action.related_paths:
                if not source.exists():
                    continue
                relative = source.relative_to(root)
                target = payload_root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(target))
                moved.append((source, target))
            update_json_file_atomic(
                bindings_path,
                lambda current: {
                    key: value
                    for key, value in current.items()
                    if str(value) != thread.thread_id
                },
            )
            update_json_file_atomic(
                latest_path,
                lambda current: {
                    key: value
                    for key, value in current.items()
                    if str(value) != thread.thread_id
                },
            )
            _write_tombstone(
                destination,
                action=action,
                payload=payload_root,
                now=now,
                status="trashed",
            )
        except Exception:
            for source, target in reversed(moved):
                if target.exists():
                    source.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(target), str(source))
            write_json_file_atomic(bindings_path, bindings)
            write_json_file_atomic(latest_path, latest)
            shutil.rmtree(destination, ignore_errors=True)
            raise
    return replace(action, status="trashed")


# LLM: authority 文件任何字节变化都保守跳过；状态仍须为原 terminal status 且时间仍早于 cutoff。
# 函数用途: 重验证 task/subagent/tombstone 权威状态。
def _authority_still_eligible(action: MemoryRetentionAction) -> bool:
    authority = action.authority_path
    if authority is None or not authority.is_file():
        return False
    if file_fingerprint(authority) != action.authority_fingerprint:
        return False
    report = read_json_object_report(authority, context="memory_retention.apply_authority")
    if report.load_error is not None:
        return False
    if action.category == "trash":
        timestamp = _timestamp(report.payload.get("moved_at"))
        return bool(
            timestamp is not None
            and action.cutoff_timestamp is not None
            and timestamp < action.cutoff_timestamp
        )
    status = str(report.payload.get("status") or "").strip().upper()
    timestamp = _timestamp(report.payload.get("updated_at"))
    return bool(
        status == action.authority_status
        and status in TASK_TERMINAL_STATUSES
        and timestamp is not None
        and action.cutoff_timestamp is not None
        and timestamp < action.cutoff_timestamp
    )


# LLM: index 损坏会让 conversation apply 整体回滚，不能删除原文后留下无法解析的共享路由。
# 函数用途: 读取 ConversationStore 的共享索引。
def _read_index(path: Path) -> dict[str, Any]:
    report = read_json_object_report(path, context="memory_retention.conversation_index")
    if report.load_error is not None:
        raise RuntimeError(f"conversation index is unreadable: {path.name}")
    return report.payload


# LLM: 回收目标必须存在于当前 owner trash/retention 下且不能等于 retention 根。
# 函数用途: 校验 action 预先生成的可恢复目标目录。
def _validated_destination(home: object, action: MemoryRetentionAction) -> Path:
    destination = action.destination
    if destination is None:
        raise ValueError("retention trash destination is missing")
    root = (Path(home.owner_trash_dir) / "retention").resolve(strict=False)
    resolved = destination.resolve(strict=False)
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("retention trash destination escaped owner trash") from exc
    if len(relative.parts) < 2:
        raise ValueError("retention trash destination is too broad")
    return resolved


# LLM: delete/move 源必须严格位于 owner 或显式 conversation root 内，且不能是根或 symlink。
# 函数用途: 防止 retention action 路径逃逸和过宽递归删除。
def _assert_safe_target(path: Path, roots: tuple[Path, ...]) -> None:
    if path.is_symlink():
        raise ValueError("retention never follows symbolic links")
    resolved = path.resolve(strict=False)
    for root in roots:
        resolved_root = Path(root).resolve(strict=False)
        try:
            relative = resolved.relative_to(resolved_root)
        except ValueError:
            continue
        if relative.parts:
            return
    raise ValueError("retention target escaped allowed roots or equals a root")


# LLM: tombstone 不保存被删正文，只记录结构化来源、权威状态和恢复位置。
# 函数用途: 写入 moving/trashed 两阶段回收记录。
def _write_tombstone(
    destination: Path,
    *,
    action: MemoryRetentionAction,
    payload: Path,
    now: datetime,
    status: str,
) -> None:
    write_json_file_atomic(
        destination / "tombstone.json",
        {
            "schema_version": "my-agent.memory-retention-tombstone.v2",
            "action_id": action.action_id,
            "category": action.category,
            "source_path": str(action.path),
            "payload_path": str(payload),
            "reason": action.reason,
            "authority_path": str(action.authority_path or ""),
            "authority_status": action.authority_status,
            "moved_at": now.isoformat(),
            "status": status,
        },
    )


# LLM: 审计只写动作元数据和错误码，不复制候选、对话、工具输出或长期记忆正文。
# 函数用途: 通过现有 owner audit ledger 记录一轮 retention 结果。
def _append_retention_audit(
    home: object,
    *,
    report: MemoryRetentionReport,
    now: datetime,
) -> None:
    append_jsonl(
        Path(home.owner_audit_log_jsonl),
        {
            "schema_version": "owner-audit.v1",
            "event_type": "owner_retention_applied",
            "owner_id": str(getattr(home, "owner_id", "") or ""),
            "applied": report.applied,
            "ok": report.ok,
            "legal_hold": report.legal_hold,
            "errors": [error.to_dict() for error in report.errors],
            "actions": [action.to_dict() for action in report.actions],
            "updated_at": now.isoformat(),
        },
        sort_keys=True,
    )


# LLM: 时间解析只用于 apply cutoff 重验证，不从 reason 或状态文案提取时间。
# 函数用途: 把 epoch/ISO 转成秒时间戳。
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


__all__ = ["execute_retention_plan", "record_retention_report"]
