# LLM: 只执行同锁生成的类型化留存动作，逐目标复核策略、法律保留、引用、指纹、状态与截止时间；原短提交门的执行未知不能洗成动作失败。
# 模块用途: 执行候选及文件留存清理，记录可恢复回收与无正文审计，并保护快照清单引用及目标 CAS。
from __future__ import annotations

"""Memory v2 retention 计划的重验证与执行。"""

import json
import os
import shutil
import stat
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from ..backends.bounded_call import BoundedCallExitUnconfirmedError
from ..common.json_io import (
    append_private_jsonl_records,
    locked_json_path,
    read_json_object_report,
    write_private_json_file_atomic,
    write_private_text_file_atomic_unlocked,
)
from ..common.nofollow_fs import NoFollowPathError, ensure_private_dir, open_directory_beneath
from ..common.request_execution_ports import (
    RequestExecutionContextUnavailable,
    RequestExecutionOutcomeUncertainError,
)
from ..common.tool_write_context import ToolWriteContextUnavailable
from ..conversation.models import ConversationThread
from ..gateway_parts.io import locked_file_transition, update_json_file_atomic
from .candidate_models import MemoryCandidate
from .candidates import (
    RETENTION_ELIGIBLE_CANDIDATE_STATUSES,
    CandidateService,
    candidate_retention_eligible,
)
from .reference_pins import ReferencePinSnapshot
from .retention_models import (
    TASK_TERMINAL_STATUSES,
    MemoryRetentionAction,
    MemoryRetentionError,
    MemoryRetentionReport,
)
from .retention_scan import (
    _load_policy,
    _runtime_unlocked,
    current_retention_pins,
    current_tool_manifest_references,
    file_fingerprint,
    paths_fingerprint,
    runtime_terminal,
    scratch_inventory,
    scratch_tree_pinned,
    security_audit_content,
    security_audit_paths,
    task_scratch_unlocked,
    trash_payload_pinned,
    tree_fingerprint,
    valid_fingerprint,
)
from .retention_transition import RetentionPolicyChangedError


# LLM: None保原executor；callback按原候选批次/单动作/审计短提交，扫描引用门外，四实际类型优先。
#   candidate 必须一次批量删除；清单阶段只扫描一次当前引用，快照动作失败或变化时保护仍被引用的清单。
#   报告的 errors 只含执行期错误；计划带来的 isolated_errors（apply 隔离掉的扫描期路径级错误）原样带进报告和审计。
# LLM: 仅已通过策略/法律保留的executor追加tmp/cache空目录清扫；扫描门外，每次rmdir仍沿原transition核权。
# LLM: 本次now沿候选phase/actions透传到原删除提交，冷归档年龄不得另取真实墙钟；CAS及原短门保持。
# 函数用途: 统一执行保留计划；同阶段引用视图按源代次复核后复用，保留原动作状态与错误。
def execute_retention_plan(
    *,
    home: object,
    candidates: CandidateService,
    plan: MemoryRetentionReport,
    now: datetime,
    allowed_external_roots: tuple[Path, ...] = (),
    commit_transition=None,
    audit_transition=None,
) -> MemoryRetentionReport:
    results, errors = _commit_step(lambda: _apply_candidate_phase(candidates, plan, now=now), commit_transition)
    manifest_references = None
    pin_snapshot = ReferencePinSnapshot(home, allowed_external_roots, now, current_retention_pins)
    for action in plan.actions:
        if action.operation == "candidate_delete":
            continue
        try:
            if action.category == "tool_manifest" and manifest_references is None:
                manifest_references = current_tool_manifest_references(home)
            result = _commit_step(lambda: _apply_action(
                home=home,
                action=action,
                now=now,
                allowed_external_roots=allowed_external_roots,
                manifest_references=manifest_references,
                pin_snapshot=pin_snapshot,
            ), commit_transition)
        except (RequestExecutionOutcomeUncertainError, RequestExecutionContextUnavailable,
                ToolWriteContextUnavailable, BoundedCallExitUnconfirmedError, RetentionPolicyChangedError):
            raise
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
    for root in (home.owner_tmp_dir, home.owner_cache_dir):
        _prune_empty_ephemeral_dirs(Path(root), now=now.timestamp(), commit_transition=commit_transition)
    report = MemoryRetentionReport(
        applied=True,
        actions=tuple(results),
        errors=tuple(errors),
        policy_fingerprint=plan.policy_fingerprint,
        isolated_errors=plan.isolated_errors,
    )
    _commit_step(lambda: _append_retention_audit(home, report=report, now=now), audit_transition)
    return report


# LLM: None直接原调用不增I/O；实际callback只运输原短门，不能替代policy/目标CAS或延后借权。
# 函数用途: 同步执行一个原保留步骤并返回原对象。
def _commit_step(operation, transition):
    return operation() if transition is None else transition(operation)


# LLM: 计划里的 candidate_delete 动作必须一次批量删除（见 _apply_candidate_actions），不能与其它动作逐条交错；
#   没有候选动作时返回两个空列表。从 execute_retention_plan 抽出只为控制函数长度，语义不变。
# LLM: 可选now保持原内部两参数调用兼容；真实executor必须透传运行时钟，不能因抽层丢失。
# 函数用途: 按同一次保留时钟执行候选清理批次，返回动作结果与结构化错误。
def _apply_candidate_phase(
    candidates: CandidateService,
    plan: MemoryRetentionReport,
    *,
    now: datetime | None = None,
) -> tuple[list[MemoryRetentionAction], list[MemoryRetentionError]]:
    candidate_actions = tuple(
        action for action in plan.actions if action.operation == "candidate_delete"
    )
    if not candidate_actions:
        return [], []
    candidate_results, candidate_errors = _apply_candidate_actions(candidates, candidate_actions, now=now)
    return list(candidate_results), list(candidate_errors)


# LLM: policy/legal-hold 阶段未进入 action executor 时，仍通过同一无正文审计格式记录安全停止。
# 函数用途: 记录一份未执行的 retention 报告。
def record_retention_report(
    *,
    home: object,
    report: MemoryRetentionReport,
    now: datetime,
) -> None:
    _append_retention_audit(home, report=report, now=now)


# LLM: 每批一次摘要/只读借用，原cutoff/CAS保持；传入now给删除提交，避免顺手归档使用另一时钟。
# 函数用途: 按本次时钟清理过期未晋升候选，坏摘要或锁前变化单列state_changed。
def _apply_candidate_actions(
    candidates: CandidateService,
    actions: tuple[MemoryRetentionAction, ...],
    *,
    now: datetime | None = None,
) -> tuple[list[MemoryRetentionAction], list[MemoryRetentionError]]:
    errors: list[MemoryRetentionError] = []
    observed_fingerprint = file_fingerprint(candidates.path)
    if any(not _fingerprint_matches(action.authority_fingerprint, observed_fingerprint) for action in actions):
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
        records = getattr(candidates, "readonly_rows", candidates.list)(statuses=RETENTION_ELIGIBLE_CANDIDATE_STATUSES)
        by_id = {record.candidate_id: record for record in records}
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
    expected_snapshots: dict[str, MemoryCandidate] = {}
    results: list[MemoryRetentionAction] = []
    for action in actions:
        record = by_id.get(action.record_id)
        if record is None:
            results.append(replace(action, status="missing"))
            continue
        from .candidates import candidate_retention_time

        updated = _timestamp(candidate_retention_time(record))
        eligible = bool(
            candidate_retention_eligible(record)
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
        expected_snapshots[action.record_id] = record
    locked_changed: set[str] = set()
    if selected:
        candidates.delete_terminal(selected, expected_snapshots=expected_snapshots, skipped_state_changed=locked_changed, now=now)
    for action in actions:
        if action.record_id in locked_changed:
            results.append(replace(action, status="state_changed"))
            errors.append(MemoryRetentionError("MEMORY_RETENTION_STATE_CHANGED", str(candidates.path),
                f"candidate {action.record_id} changed before locked deletion"))
    selected_ids = set(selected) - locked_changed
    results.extend(
        replace(action, status="deleted")
        for action in actions
        if action.record_id in selected_ids
    )
    order = {action.action_id: index for index, action in enumerate(actions)}
    results.sort(key=lambda action: order[action.action_id])
    return results, errors


# LLM: 所有动作先拒绝无效摘要；scratch/Daily/Compact/Curator及trash执行前重核引用，不可读直接拒绝。
#   operation是宿主枚举；清单仍复核快照阶段后的引用，不信旧规划。
# LLM: tmp/cache文件确已删除才按同一action cutoff清空过期祖先，沿规范根的nofollow目录句柄rmdir，不删根或跨链接。
# 函数用途: 分派动作并复核当前源代次；安全审计仅按原追加锁整理记录，不用旧规划替代执行检查。
def _apply_action(
    *,
    home: object,
    action: MemoryRetentionAction,
    now: datetime,
    allowed_external_roots: tuple[Path, ...],
    manifest_references: tuple[frozenset[str], bool] | None = None,
    pin_snapshot: ReferencePinSnapshot | None = None,
) -> MemoryRetentionAction:
    allowed_roots = (Path(home.owner_home_dir), *allowed_external_roots)
    _assert_safe_target(action.path, allowed_roots)
    if not valid_fingerprint(action.source_fingerprint):
        return replace(action, status="state_changed")
    if action.operation == "scratch_clear":
        return _clear_scratch(home, action, now, allowed_external_roots=allowed_external_roots,
                              pin_snapshot=pin_snapshot)
    if action.operation == "security_audit_compact":
        if action.path not in security_audit_paths(home) or action.cutoff_timestamp is None:
            return replace(action, status="state_changed")
        with locked_json_path(action.path):
            if file_fingerprint(action.path) != action.source_fingerprint:
                return replace(action, status="state_changed")
            content, removed = security_audit_content(action.path, action.cutoff_timestamp)
            if file_fingerprint(action.path) != action.source_fingerprint:
                return replace(action, status="state_changed")
            if removed:
                write_private_text_file_atomic_unlocked(action.path, content)
        return replace(action, status="compacted" if removed else "state_changed")
    if action.category in {"daily", "compact", "curator_run", "trash"}:
        pin_errors: list[MemoryRetentionError] = []
        pins, complete = (pin_snapshot.current(pin_errors, target=action.path if action.category == "trash" else None) if pin_snapshot is not None else
                          current_retention_pins(home, allowed_external_roots, pin_errors, now=now))
        if action.category == "trash" and not complete:
            complete = pins.complete_for(action.path, pin_errors)
        if not complete:
            raise OSError("MEMORY_RETENTION_PIN_SOURCE_UNREADABLE")
        protected = (trash_payload_pinned(pins, action.path, pin_errors) if action.category == "trash"
                     else pins.protects(action.path, pin_errors))
        if protected:
            return replace(action, status="state_changed")
    if action.operation == "delete_file":
        result = _delete_file(action)
        if result.status == "deleted" and action.category in {"tmp", "cache"}:
            root = Path(home.owner_tmp_dir if action.category == "tmp" else home.owner_cache_dir)
            _prune_ephemeral_parents(root, action.path.parent, cutoff=action.cutoff_timestamp)
        return result
    if action.operation == "trash_tree":
        return _trash_tree(home=home, action=action, now=now)
    if action.operation == "trash_file":
        if action.category == "tool_manifest":
            referenced, complete = manifest_references or current_tool_manifest_references(home)
            if not complete or action.path.stem in referenced:
                return replace(action, status="state_changed")
        return _trash_file(home=home, action=action, now=now)
    if action.operation == "delete_tree":
        return _delete_tree(action)
    if action.operation == "conversation_trash":
        return _trash_conversation(home=home, action=action, now=now)
    raise ValueError(f"unsupported retention operation: {action.operation}")


# LLM: 每个动作仅重核一次全源代次；坏源、所属身份或任一子证据命中均保护整棵，不放宽完整性门。
# 函数用途: 为本次scratch动作检查同规划口径的完整证据保护。
def _scratch_is_unpinned(home, action, now, roots, snapshot, members, task, state):
    errors = []
    pins, complete = (snapshot.current(errors, target=action.path) if snapshot is not None else
                      current_retention_pins(home, roots, errors, now=now))
    if not complete:
        complete = pins.complete_for(action.path, errors)
    return complete and not scratch_tree_pinned(pins, action.path, members, errors, task_root=task, state=state)


# LLM: 重新从owner逐段nofollow定位父链，对比已开FD与当前位置inode；只能缩小竞态窗口，不授予发布互斥锁。
# 函数用途: 防止拿到目录句柄后目录已被移出scratch，仍沿旧FD删除output文件。
def _scratch_directory_still_at(owner, parts, descriptor):
    current = open_directory_beneath(owner, parts, require_dir_fd=True)
    try:
        before, after = os.fstat(descriptor), os.fstat(current)
        return stat.S_ISDIR(before.st_mode) and (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)
    finally:
        os.close(current)


# LLM: scratch按动作复核策略/状态/指纹及pin代次，每次实际删除仍重核FD当前位置；不声称跨进程发表原子性。
# 函数用途: 只删已验证且无引用的旧普通文件和空子目录，保留output/inputs和新证据。
def _clear_scratch(home, action, now, *, allowed_external_roots=(), pin_snapshot=None):
    if action.cutoff_timestamp is None:
        return replace(action, status="state_changed")
    policy, error = _load_policy(Path(home.owner_retention_json))
    if error is not None or policy is None or policy.legal_hold or not _runtime_unlocked(home, now):
        return replace(action, status="state_changed")
    if action.category == "task_tmp":
        try:
            parts = action.path.relative_to(Path(home.owner_tasks_dir)).parts
        except ValueError:
            return replace(action, status="state_changed")
        if (len(parts) != 3 or parts[-1] != "tmp" or policy.task_tmp_idle_days <= 0
                or not task_scratch_unlocked(home, action.path.parent, policy.legal_hold_task_ids, now)):
            return replace(action, status="state_changed")
        task = action.path.parent
        authority = task / "work/state.json"
        report = read_json_object_report(authority, context="memory_retention.task_scratch") if authority.exists() else None
        if report is not None and report.load_error:
            return replace(action, status="state_changed")
        state = report.payload if report is not None else {}
    elif action.category == "run_scratch":
        authority = action.authority_path
        if authority is None or file_fingerprint(authority) != action.authority_fingerprint:
            return replace(action, status="state_changed")
        task = authority.parent.parent
        runs = Path(home.owner_home_dir) / "runs"
        relative = task.relative_to(runs)
        if len(relative.parts) not in {1, 2} or action.path not in {task / "tmp", task / "work/tmp", task / "work/blobs/tool_outputs"}:
            return replace(action, status="state_changed")
        report = read_json_object_report(authority, context="memory_retention.run_scratch")
        if report.load_error or report.payload.get("task_id") in policy.legal_hold_task_ids or policy.tmp_days <= 0:
            return replace(action, status="state_changed")
        state = report.payload
        status = str(state.get("status") or "").upper()
        terminal = ((status, _timestamp(state.get("updated_at"))) if status in TASK_TERMINAL_STATUSES
                    else runtime_terminal(home, task, state, now) if status == "RUNNING" else None)
        if terminal is None or terminal[1] is None or terminal[1] >= action.cutoff_timestamp:
            return replace(action, status="state_changed")
    else:
        return replace(action, status="state_changed")
    try:
        digest, members = scratch_inventory(action.path, action.cutoff_timestamp)
        if digest != action.source_fingerprint:
            return replace(action, status="state_changed")
        if not _scratch_is_unpinned(home, action, now, allowed_external_roots, pin_snapshot, members, task, state):
            return replace(action, status="state_changed")
        # 从owner根逐段nofollow打开，避免中间目录被换成链接后误删兄弟输入/输出。
        root_parts = action.path.relative_to(Path(home.owner_home_dir)).parts
        fd = open_directory_beneath(Path(home.owner_home_dir), root_parts, require_dir_fd=True)
        os.close(fd)
        for relative, expected in members:
            parts = Path(relative).parts
            if stat.S_ISDIR(expected[-1]):
                continue
            descriptor = open_directory_beneath(Path(home.owner_home_dir), root_parts + parts[:-1], require_dir_fd=True)
            try:
                observed = os.stat(parts[-1], dir_fd=descriptor, follow_symlinks=False)
                from .retention_scan import _file_identity

                if _file_identity(observed) != expected:
                    return replace(action, status="state_changed")
                if not _scratch_directory_still_at(Path(home.owner_home_dir), root_parts + parts[:-1], descriptor):
                    return replace(action, status="state_changed")
                os.unlink(parts[-1], dir_fd=descriptor)
            finally:
                os.close(descriptor)
        for relative, expected in sorted(members, key=lambda item: len(Path(item[0]).parts), reverse=True):
            if stat.S_ISDIR(expected[-1]):
                parts = Path(relative).parts
                descriptor = open_directory_beneath(Path(home.owner_home_dir), root_parts + parts[:-1], require_dir_fd=True)
                try:
                    observed = os.stat(parts[-1], dir_fd=descriptor, follow_symlinks=False)
                    if not stat.S_ISDIR(observed.st_mode) or (observed.st_dev, observed.st_ino) != expected[:2]:
                        return replace(action, status="state_changed")
                    if not _scratch_directory_still_at(Path(home.owner_home_dir), root_parts + parts[:-1], descriptor):
                        return replace(action, status="state_changed")
                    os.rmdir(parts[-1], dir_fd=descriptor)
                finally:
                    os.close(descriptor)
        return replace(action, status="deleted")
    except (OSError, NoFollowPathError):
        return replace(action, status="state_changed")


# LLM: 只有计划和当前摘要均合法且相等才允许unlink；mtime仍须早于cutoff。
# 函数用途: 删除一个通过指纹安全门的过期文件。
def _delete_file(action: MemoryRetentionAction) -> MemoryRetentionAction:
    if not action.path.exists():
        return replace(action, status="missing")
    if (
        action.path.is_symlink()
        or not _fingerprint_matches(action.source_fingerprint, file_fingerprint(action.path))
        or action.cutoff_timestamp is None
        or action.path.stat().st_mtime >= action.cutoff_timestamp
    ):
        return replace(action, status="state_changed")
    action.path.unlink()
    return replace(action, status="deleted")


# LLM: task/tool/subagent的权威状态、目录及计划摘要均须合法不变，坏成员不可漂白为组合摘要。
# 函数用途: 将一棵过期目录连同 tombstone 安全移动。
def _trash_tree(
    *,
    home: object,
    action: MemoryRetentionAction,
    now: datetime,
) -> MemoryRetentionAction:
    if not action.path.exists():
        return replace(action, status="missing")
    if not _authority_still_eligible(action) or not _fingerprint_matches(action.source_fingerprint, tree_fingerprint(action.path)):
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


# LLM: 单文件摘要必须为合法小写64hex且当前值相等；原trash_file两阶段墓碑和回滚合同不变。
#   没有authority状态文件可核对，按"计划时字节指纹 + cutoff"重验证；
#   文件在 plan 之后被改过或变新都跳过（state_changed）。移走最后一个文件后顺手删空的父目录
#   （仅空目录，沿用 home_layout._remove_dir_if_empty 的口径），例如快照的日期目录。
# 函数用途: 把一个过期单文件（快照 / 工具清单）连同 tombstone 安全移入可恢复回收站。
def _trash_file(
    *,
    home: object,
    action: MemoryRetentionAction,
    now: datetime,
) -> MemoryRetentionAction:
    if not action.path.exists():
        return replace(action, status="missing")
    if (
        action.path.is_symlink()
        or not _fingerprint_matches(action.source_fingerprint, file_fingerprint(action.path))
        or action.cutoff_timestamp is None
        or action.path.stat().st_mtime >= action.cutoff_timestamp
    ):
        return replace(action, status="state_changed")
    destination = _validated_destination(home, action)
    destination.mkdir(parents=True, exist_ok=False)
    payload = destination / "payload"
    _write_tombstone(destination, action=action, payload=payload, now=now, status="moving")
    try:
        shutil.move(str(action.path), str(payload))
        _prune_empty_directory(action.path.parent)
        _write_tombstone(destination, action=action, payload=payload, now=now, status="trashed")
    except Exception:
        if payload.exists() and not action.path.exists():
            action.path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(payload), str(action.path))
        shutil.rmtree(destination, ignore_errors=True)
        raise
    return replace(action, status="trashed")


# LLM: 仅删除已经空掉的直接父目录；rmdir 原子拒绝有新内容的目录，绝不递归删除未规划的子目录或链接。
# 函数用途: 文件回收后尝试移除空日期目录，失败不改变已完成的回收结果。
def _prune_empty_directory(directory: Path) -> None:
    try:
        directory.rmdir()
    except OSError:
        pass


# LLM: 调用方负责捕获OSError/NoFollowPathError；仅打开root以下祖先FD作年龄观察，每个FD原栈关闭，不执行删除。
# 函数用途: 核对目标的所有中间祖先都不新于cutoff，根目录不参与年龄判断。
def _ephemeral_ancestors_expired(root: Path, parts: tuple[str, ...], cutoff: float) -> bool:
    for depth in range(1, len(parts)):
        ancestor = open_directory_beneath(root, parts[:depth], require_dir_fd=True)
        try:
            if os.fstat(ancestor).st_mtime > cutoff:
                return False
        finally:
            os.close(ancestor)
    return True


# LLM: 只接受根以下相对目录；有cutoff时逐段nofollow打开并核验所有祖先FD的mtime，根不参与年龄判断且始终不删。
#   目标也核验mtime<=cutoff；dirfd rmdir原子拒绝非空，链接和文件系统竞争只跳过，不声称mtime是原子CAS。
# 函数用途: 删除一个过期空临时目录；任何新鲜祖先都保护整条分支，不跨链接或删根。
def _remove_empty_ephemeral_dir(root: Path, parts: tuple[str, ...], *, before: float | None = None) -> bool:
    if not parts:
        return False
    try:
        if before is not None and not _ephemeral_ancestors_expired(root, parts, before):
            return False
        descriptor = open_directory_beneath(root, parts[:-1], require_dir_fd=True)
        try:
            info = os.stat(parts[-1], dir_fd=descriptor, follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode) or (before is not None and info.st_mtime > before):
                return False
            os.rmdir(parts[-1], dir_fd=descriptor)
            return True
        finally:
            os.close(descriptor)
    except (OSError, NoFollowPathError):
        return False


# LLM: 文件删除收尾传入action cutoff；无cutoff保持原内部调用口径，非空/新鲜/链接/越界即停，不删除规范根。
# 函数用途: 向上清理过期且已空的祖先；unlink刷新mtime后保守保留父目录，失败不影响已完成的文件删除。
def _prune_ephemeral_parents(root: Path, directory: Path, *, cutoff: float | None = None) -> None:
    try:
        parts = directory.relative_to(root).parts
    except ValueError:
        return
    while parts and _remove_empty_ephemeral_dir(root, parts, before=cutoff):
        parts = parts[:-1]


# LLM: 只扫描既有普通根，自底向上不跟随链接；每个目录共用同一cutoff并由删除函数重核整条祖先链。
#   扫描在门外、rmdir逐次入原短门；不额外回溯删除，以保留逐目录removed计数及短门边界。
# 函数用途: 回收至少闲置一小时且祖先也过期的空子目录，不创建或删除根目录。
def _prune_empty_ephemeral_dirs(root: Path, *, now: float, min_age_seconds: float = 3600.0,
                                 commit_transition=None) -> int:
    try:
        descriptor = open_directory_beneath(root, (), require_dir_fd=True)
    except (OSError, NoFollowPathError):
        return 0
    removed = 0
    cutoff = now - min_age_seconds
    try:
        for directory, _dirs, _files, _fd in os.fwalk(".", topdown=False, follow_symlinks=False, dir_fd=descriptor):
            parts = Path(directory).parts
            if not parts:
                continue
            # LLM: 绑定本次相对目录，真实年龄和空目录仍由原写步骤重新验证。
            # 函数用途: 对本次枚举目录执行一个有界rmdir步骤。
            def operation(parts=parts):
                return _remove_empty_ephemeral_dir(root, parts, before=cutoff)
            removed += bool(_commit_step(operation, commit_transition))
    except OSError:
        pass
    finally:
        os.close(descriptor)
    return removed


# LLM: trash purge须核对合法tombstone和整棵容器摘要，任何坏成员拒绝永久删除。
# 函数用途: 永久删除超过 trash_days 的 retention 容器。
def _delete_tree(action: MemoryRetentionAction) -> MemoryRetentionAction:
    if not action.path.exists():
        return replace(action, status="missing")
    if not _authority_still_eligible(action) or not _fingerprint_matches(action.source_fingerprint, tree_fingerprint(action.path)):
        return replace(action, status="state_changed")
    shutil.rmtree(action.path)
    return replace(action, status="deleted")


# LLM: Conversation在原thread锁内重验合法单文件/组合摘要、active task、时间及索引，坏成员拒绝移动。
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
            or not _fingerprint_matches(action.authority_fingerprint, file_fingerprint(action.path))
            or not _fingerprint_matches(action.source_fingerprint, paths_fingerprint(action.related_paths))
        ):
            return replace(action, status="state_changed")
        for path in action.related_paths:
            _assert_safe_target(path, (root,))
        bindings_path = root / "channel_bindings.json"
        latest_path = root / "user_latest_threads.json"
        bindings = _read_index(bindings_path)
        latest = _read_index(latest_path)
        destination = _validated_destination(home, action)
        # 落点目录逐级按 0700 新建（pbfix 2026-10-04：统一走 nofollow_fs.ensure_private_dir）。
        ensure_private_dir(destination.parent)
        destination.mkdir(mode=0o700)
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
                ensure_private_dir(target.parent)
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
                    ensure_private_dir(source.parent)
                    shutil.move(str(target), str(source))
            # 回滚写回同样走私有原子写（0600/0700），存量宽权限索引下次写入即收紧；内容逐字节不变。
            write_private_json_file_atomic(bindings_path, bindings)
            write_private_json_file_atomic(latest_path, latest)
            shutil.rmtree(destination, ignore_errors=True)
            raise
    return replace(action, status="trashed")


# LLM: 权威文件必须有合法且不变摘要，状态仍须为原terminal且时间早于cutoff；不可读不等于missing。
# 函数用途: 重验证 task/subagent/tombstone 权威状态。
def _authority_still_eligible(action: MemoryRetentionAction) -> bool:
    authority = action.authority_path
    if authority is None or not authority.is_file():
        return False
    if not _fingerprint_matches(action.authority_fingerprint, file_fingerprint(authority)):
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


# LLM: 只有两个有效SHA256相等才通过，不允许哨兵值相等授权破坏操作。
# 函数用途: 统一执行端的指纹合法性与相等检查。
def _fingerprint_matches(expected: object, observed: object) -> bool:
    return valid_fingerprint(expected) and valid_fingerprint(observed) and expected == observed


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
    # 回收墓碑属宿主运行数据：私有原子写（0600/0700）；内容逐字节不变。
    write_private_json_file_atomic(
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


# LLM: 审计只写动作元数据和错误码，不复制候选、对话、工具输出或长期记忆正文。被隔离的路径级错误只记条数与按码计数
#   （isolated_error_count / isolated_error_codes，新增键，旧读取方忽略即可），不重复写路径。
# 函数用途: 通过现有 owner audit ledger 记录一轮 retention 结果。
def _append_retention_audit(
    home: object,
    *,
    report: MemoryRetentionReport,
    now: datetime,
) -> None:
    # owner 审计账属宿主运行数据：私有追加（0600/0700），存量宽权限账本下次写入即收紧；内容逐字节不变。
    append_private_jsonl_records(
        Path(home.owner_audit_log_jsonl),
        [{
            "schema_version": "owner-audit.v1",
            "event_type": "owner_retention_applied",
            "owner_id": str(getattr(home, "owner_id", "") or ""),
            "applied": report.applied,
            "ok": report.ok,
            "legal_hold": report.legal_hold,
            "errors": [error.to_dict() for error in report.errors],
            "actions": [action.to_dict() for action in report.actions],
            # 执行了、但有路径级错误被隔离时，审计不能看起来"全干净"：记条数与按码计数，不记路径。
            "isolated_error_count": len(report.isolated_errors),
            "isolated_error_codes": _error_code_counts(report.isolated_errors),
            "updated_at": now.isoformat(),
        }],
        sort_keys=True,
    )


# LLM: 只数结构化 error_code，不读 message 或路径；键按字母序，便于审计比对。
# 函数用途: 把一组保留错误按错误码计数。
def _error_code_counts(errors: tuple[MemoryRetentionError, ...]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for error in errors:
        counts[error.error_code] = counts.get(error.error_code, 0) + 1
    return dict(sorted(counts.items()))


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
