# LLM: 仅退役已结清终态任务的正文；canonical身份、去重证明、取消与学习责任原样保留。
# 模块用途: 用有界游标扫描会话任务，严格复核责任后经协调器写门压缩正文。
from __future__ import annotations

import math
import os
import stat
from datetime import datetime, timezone
from pathlib import Path

from ..common.opaque_id import validate_opaque_id
from ..runtime_errors import DataCorruptionError
from ..user_space.storage_lanes import (
    STORAGE_LANE_RECEIPT_MAX_ERRORS_COUNT,
    STORAGE_LANE_SLICE_BYTES,
    STORAGE_LANE_SLICE_ENTRIES,
    STORAGE_LANE_SLICE_MS,
    _real_directory,
    lane_cursor_window,
    owner_conversation_roots,
    owner_retention_override,
)
from .session_task_report import result_report_key
from .session_tasks import (
    SESSION_TASK_TERMINAL_STATUSES,
    SessionTask,
    SessionTaskStore,
    read_json_file_report,
)
from .store_layout import ConversationStorage
from .store_wake_publication import publication_receipt

# 终态会话任务正文默认保留三十天；任务身份和去重证明永久沿原记录保存。
SESSION_TASK_PAYLOAD_RETENTION_DAYS = 30


# LLM: 仅依据原metadata结构化事实；未知形状与未完成学习/取消交接不能变成删除许可。
# 函数用途: 返回必须保留正文的元数据责任，供扫描者和记录锁内末核共用。
def _metadata_keep_reason(metadata: dict) -> str:
    control = metadata.get("cancellation_control", {})
    if not isinstance(control, dict):
        return "cancellation_unknown"
    for key in ("outstanding_refs", "execution_outstanding_refs", "session_task_outstanding_refs", "resource_refs"):
        if key in control and not isinstance(control[key], list):
            return "cancellation_unknown"
    for key in ("model_worker_exit_unknown", "claim_finished", "resources_prepared", "resources_confirmed"):
        if key in control and type(control[key]) is not bool:
            return "cancellation_unknown"
    if any(control.get(key) for key in ("outstanding_refs", "execution_outstanding_refs", "session_task_outstanding_refs")):
        return "cancellation_pending"
    if (control.get("model_worker_exit_unknown") or control.get("execution_ref") and control.get("claim_finished") is not True
            or control.get("resources_confirmed") is False
            or control.get("resource_refs") and (control.get("resources_prepared") is not True
                                               or control.get("resources_confirmed") is not True)):
        return "cancellation_pending"
    enqueue = metadata.get("task_learning_enqueue")
    if enqueue is not None:
        if not isinstance(enqueue, dict):
            return "learning_unknown"
        if (enqueue.get("status") not in {"enqueued", "duplicate", "ineligible", "conflict"}
                or enqueue.get("status") == "ineligible" and enqueue.get("reason") in {
                    "canonical_completion_reader_unbound", "canonical_completion_unconfirmed", "trajectory_required", "queue_full"}):
            return "learning_pending"
    elif any(key in metadata for key in ("task_learning_candidate", "trusted_task_completion", "task_completion_proof")):
        return "learning_pending"
    return ""


# LLM: 只读准确责任回执；缺失、非普通文件、多链接、链接父链及损坏均保留，不初始化队列。
# 函数用途: 重核学习wake与原sender结果回报是否已耐久结清。
def _task_keep_reason(storage, task) -> str:
    reason = _metadata_keep_reason(task.metadata)
    if reason:
        return reason
    wake_id = task.metadata.get("task_learning_wake_id")
    if wake_id:
        validate_opaque_id(wake_id, kind="wake_signal_id")
        settled = False
        for path in (storage.wake_handled_dir / f"{wake_id}.json", storage.wake_quarantine_path(wake_id),
                     storage.wake_quarantine_archive_path(wake_id)):
            if not _real_directory(storage.root, path.parent.relative_to(storage.root).parts):
                continue
            try:
                info = os.lstat(path)
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                return "learning_unknown"
            settled = True
            break
        if not settled:
            return "learning_pending"
    if task.sender_thread_id:
        path = storage.wake_dedupe_path(task.sender_thread_id, result_report_key(task))
        if not _real_directory(storage.root, path.parent.relative_to(storage.root).parts):
            return "report_pending"
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return "report_pending"
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            return "report_unknown"
        if publication_receipt(storage, task.sender_thread_id, result_report_key(task)) != "handled":
            return "report_pending"
    return ""


# LLM: 仅枚举本根顶层名称；拒绝链接目录，scandir由上下文关闭，不做递归或正文全量加载。
# 函数用途: 给有界名称窗口提供候选流。
def _task_names(root):
    if not _real_directory(root, ("session_tasks",)):
        return
    with os.scandir(root / "session_tasks") as entries:
        for entry in entries:
            if entry.name.endswith(".json"):
                yield entry.name


# LLM: 不建第二份任务账；首次到期候选才取一次pin快照，未知责任保留；所有变更走原写门。
# 函数用途: 分片退役已结清会话任务正文，并返回真实文件差额与可续游标。
def maintain_session_task_payloads_lane(request) -> dict:
    from ..memory_store.retention_scan import current_retention_pins

    receipt = {"cursor": dict(request.cursor), "complete": False, "items": 0, "bytes": 0,
               "deleted_items": 0, "freed_bytes": 0, "protected": {}, "skipped": {}, "errors": []}
    if request.legal_hold:
        return {**receipt, "complete": True, "deferred_reason": "legal_hold"}
    budget = request.budget.capped(items=STORAGE_LANE_SLICE_ENTRIES, size=STORAGE_LANE_SLICE_BYTES, ms=STORAGE_LANE_SLICE_MS)
    if request.budget.exhausted():
        return receipt
    roots = owner_conversation_roots(request.home)
    days = owner_retention_override(request.home, "session_task_payload_days", SESSION_TASK_PAYLOAD_RETENTION_DAYS)
    root_index = request.cursor.get("root", 0)
    root_index = root_index if type(root_index) is int and 0 <= root_index <= len(roots) else 0
    after = request.cursor.get("after", "")
    after = after if isinstance(after, str) else ""
    pins = None
    for index in range(root_index, len(roots)):
        root = roots[index]
        storage = ConversationStorage(root, initialize=False)
        store = SessionTaskStore(storage)
        names = lane_cursor_window({"after": after}, _task_names(root), STORAGE_LANE_SLICE_ENTRIES)
        for name in names:
            if budget.exhausted(receipt["items"], receipt["bytes"]):
                return receipt
            path = root / "session_tasks" / name
            receipt["items"] += 1
            receipt["cursor"] = {"root": index, "after": name}
            try:
                info = os.lstat(path)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    reason = "unsafe_file"
                elif info.st_size > budget.max_bytes - receipt["bytes"]:
                    receipt["skipped"]["byte_budget"] = receipt["skipped"].get("byte_budget", 0) + 1
                    continue
                else:
                    receipt["bytes"] += info.st_size
                    report = read_json_file_report(path, context="session_task.retention")
                    if report.load_error or not report.payload:
                        raise ValueError("unreadable session task")
                    if report.payload.get("schema_version") != "session_task.v1":
                        raise ValueError("unknown session task schema")
                    if type(report.payload.get("updated_at")) not in (int, float):
                        raise ValueError("invalid session task timestamp")
                    if "metadata" in report.payload and not isinstance(report.payload["metadata"], dict):
                        raise ValueError("invalid session task metadata")
                    try:
                        task = SessionTask.from_dict(report.payload)
                    except OverflowError:
                        raise ValueError("invalid session task timestamp") from None
                    if task.task_id != path.stem:
                        raise ValueError("session task identity mismatch")
                    if task.status not in SESSION_TASK_TERMINAL_STATUSES:
                        reason = "nonterminal"
                    elif not math.isfinite(task.updated_at) or not 0 < task.updated_at <= request.now - days * 86400:
                        reason = "young_or_unknown_age"
                    elif "payload_retired" in task.metadata:
                        reason = "already_retired"
                    else:
                        reason = _task_keep_reason(storage, task)
                        if not reason:
                            if pins is None:
                                errors = []
                                pins, _ = current_retention_pins(request.home, roots, errors,
                                    now=datetime.fromtimestamp(request.now, timezone.utc))
                            if not pins.complete or not pins.complete_for(path, []):
                                return {**receipt, "deferred_reason": "pins_incomplete",
                                        "skipped": {**receipt["skipped"], "pins_incomplete": 1}, "complete": True}
                            if task.task_id in pins.ids or pins.protects(path, []):
                                reason = "pinned"
                            else:
                                body = store._body_index_path(task.body_dedupe_key) if task.body_dedupe_key else None
                                body_size = os.lstat(body).st_size if body is not None and body.exists() else 0
                                changed = request.run_write(lambda task=task: store.retire_payload(
                                    task.task_id, expected_updated_at=task.updated_at, now=request.now))
                                if changed:
                                    remaining_body = os.lstat(body).st_size if body is not None and body.exists() else 0
                                    receipt["freed_bytes"] += max(0, info.st_size - os.lstat(path).st_size + body_size - remaining_body)
                                    receipt["deleted_items"] += 1
                                    continue
                                reason = "changed_during_scan"
                receipt["protected"][reason] = receipt["protected"].get(reason, 0) + 1
            except (OSError, ValueError, TypeError, KeyError, DataCorruptionError) as exc:
                receipt["protected"]["read_or_write_error"] = receipt["protected"].get("read_or_write_error", 0) + 1
                if len(receipt["errors"]) < STORAGE_LANE_RECEIPT_MAX_ERRORS_COUNT:
                    receipt["errors"].append({"code": type(exc).__name__, "path": str(path.relative_to(request.home.owner_home_dir))})
        if len(names) == STORAGE_LANE_SLICE_ENTRIES:
            return receipt
        after = ""
        receipt["cursor"] = {"root": index + 1, "after": ""}
    return {**receipt, "cursor": {}, "complete": True}
