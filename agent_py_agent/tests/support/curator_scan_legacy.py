# LLM: 冻结725d6eb7b只读扫描作等价对照，仅合成owner使用，不进入产品或写入权限链。
# 模块用途: 保存E11c优化前的消息/审计实现，供回归与性能对照。
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from agent_py_agent.agent.common.json_io import read_jsonl_objects_report
from agent_py_agent.agent.conversation.display_checkpoint import is_display_checkpoint
from agent_py_agent.agent.conversation.models import MessageLogEntry
from agent_py_agent.agent.conversation.store_io import json_row, jsonl_error
from agent_py_agent.agent.conversation.store_messages import _message_entries
from agent_py_agent.agent.memory_store.curator_inputs import CuratorAuditInput, _audit_input
from agent_py_agent.agent.runtime_errors import DataCorruptionError


class LegacyMessageReader:
    def __init__(self, store):
        self.storage = store.storage

    def after_report(
        self,
        thread_id: str,
        *,
        after_message_id: str = "",
        limit: int = 100,
    ) -> tuple[list[MessageLogEntry], list[dict[str, Any]]]:
        bounded_limit = max(1, min(1_000, int(limit or 100)))
        path = self.storage.message_path(thread_id)
        if not path.exists():
            return [], []
        try:
            offset = (
                self.byte_offset_after(thread_id, after_message_id)
                if str(after_message_id or "").strip()
                else 0
            )
            with path.open("rb") as handle:
                handle.seek(offset)
                rows, errors = _legacy_tail_open(handle, path, bounded_limit)
            entries, parse_errors = _message_entries(rows)
            return entries, [*errors, *parse_errors]
        except Exception as exc:
            return [], [jsonl_error(exc, "conversation.messages.after", path=path)]


    def byte_offset_after(self, thread_id: str, message_id: str) -> int:
        """Return the byte position immediately after a message in the raw ledger."""
        path = self.storage.message_path(thread_id)
        try:
            with path.open("rb") as handle:
                return _legacy_locate_open(handle, path, message_id)
        except OSError as exc:
            raise DataCorruptionError(f"cannot read conversation transcript: {path}") from exc


# LLM: 原725d逐行扫描体只抽平嵌套，保留异常、首匹配与每次从头扫描；不共用任何新缓存。
# 函数用途: 冻结旧消息游标扫描作为独立对照。
def _legacy_locate_open(handle, path, message_id):
    while line := handle.readline():
        try:
            row = json.loads(line.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise DataCorruptionError(f"conversation transcript contains an unreadable row: {path}") from exc
        if isinstance(row, dict) and str(row.get("message_id") or "") == message_id:
            return handle.tell()
    raise DataCorruptionError(f"conversation compact message cursor is missing from transcript: {message_id}")


# LLM: 原尾部读取循环与新缓存完全独立，display、空白及首坏行停止语义保持。
# 函数用途: 原始消息尾读的平铺测试对照。
def _legacy_tail_open(handle, path, limit):
    rows, errors = [], []
    while len(rows) < limit and (line := handle.readline()):
        if not line.strip():
            continue
        row, error = _legacy_tail_row(line, path)
        if error is not None:
            errors.append(error)
            break
        if row is not None and not is_display_checkpoint(row):
            rows.append(row)
    return rows, errors


# LLM: 保留旧UTF-8/JSON错误投影，不调用新消息定位或缓存读取器。
# 函数用途: 解码旧实现的一条尾部消息行。
def _legacy_tail_row(line, path):
    try:
        text = line.decode("utf-8")
    except UnicodeDecodeError as exc:
        return None, jsonl_error(exc, "conversation.messages.after", path=path)
    return json_row(text, context="conversation.messages.after", path=path, line_number=0)


def legacy_collect_audit(
    audit_dir: Path,
    *,
    after_event_id: str,
    limit: int,
    max_chars: int,
) -> tuple[list[CuratorAuditInput], list[dict[str, object]]]:
    if not audit_dir.exists():
        return [], []
    target = str(after_event_id or "").strip()
    state = SimpleNamespace(target=target, found=not target, selected=[], errors=[], used_chars=0,
                            limit=limit, max_chars=max_chars)
    for path in sorted(audit_dir.glob("*.jsonl")):
        rows, path_errors = legacy_read_audit_rows(path)
        state.errors.extend(path_errors)
        if _legacy_choose_audit(rows, state):
            return state.selected, state.errors
    if target and not state.found:
        state.errors.append(
            {
                "context": "memory_curator.audit_cursor",
                "error_code": "AUDIT_CURSOR_MISSING",
                "event_id": target,
            }
        )
    return state.selected, state.errors


# LLM: 原两层循环内体单独抽出，预算/投影仍是725d旧路径，不借新扫描算法自证。
# 函数用途: 旧实现从整份审计行列表选择原顺序前缀。
def _legacy_choose_audit(rows, state):
    for payload in rows:
        event_id = str(payload.get("event_id") or "").strip()
        if not state.found:
            state.found = event_id == state.target
            continue
        item = _audit_input(payload)
        item_chars = len(json.dumps(item.to_model(), ensure_ascii=False))
        if state.selected and (len(state.selected) >= state.limit or state.used_chars + item_chars > state.max_chars):
            return True
        state.selected.append(item)
        state.used_chars += item_chars
        if len(state.selected) >= state.limit:
            return True
    return False


def legacy_read_audit_rows(path: Path) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    if not path.is_file():
        return [], []
    report = read_jsonl_objects_report(path, context="memory_curator.audit_read")
    errors = [
        {
            "context": "memory_curator.audit_read",
            "error_code": "AUDIT_READ_FAILED",
            "error_type": str(item.get("error_type") or item.get("error_code") or "InvalidRow"),
            "path": str(path),
            "line": int(item.get("line") or 0),
        }
        for item in report.load_errors
    ]
    rows: list[dict[str, object]] = []
    for payload in report.records:
        if not str(payload.get("event_id") or "").strip():
            errors.append(legacy_load_error(path, ValueError("audit row lacks event_id"), line=0))
            continue
        rows.append(dict(payload))
    return rows, errors


def legacy_load_error(path: Path, exc: BaseException, *, line: int) -> dict[str, object]:
    return {
        "context": "memory_curator.audit_read",
        "error_code": "AUDIT_READ_FAILED",
        "error_type": type(exc).__name__,
        "path": str(path),
        "line": line,
    }
