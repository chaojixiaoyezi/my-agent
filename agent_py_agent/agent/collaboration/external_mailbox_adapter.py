"""协调器与外部助手的单写者文件邮箱。"""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import stat
import time
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..common.json_io import locked_json_path
from ..common.nofollow_fs import (
    _portable_path,
    _supports_dir_fd,
    ensure_private_dir,
    open_directory_beneath,
    write_text_atomic_beneath,
)
from ..common.opaque_id import validate_opaque_id
from .models import ExternalMailboxLayout

if TYPE_CHECKING:
    from .handoff_board import HandoffBoardService

logger = logging.getLogger(__name__)


# LLM: 外部邮箱路径越界或非法软链接异常。
# 类用途: 标识外部邮箱路径安全校验失败。
class ExternalMailboxSecurityError(RuntimeError):
    def __init__(self, message: str, *, error_code: str = "MAILBOX_SECURITY_VIOLATION") -> None:
        super().__init__(message)
        self.error_code = error_code


# LLM: 创建标准外部助手邮箱目录布局对象。
# 函数用途: 在指定协调根下生成单写者收发文件路径结构。
def create_external_mailbox_layout(
    root_dir: Path | str,
    actor_id: str,
) -> ExternalMailboxLayout:
    raw_actor = str(actor_id or "").strip()
    try:
        actor_clean = validate_opaque_id(raw_actor, kind="actor_id")
    except ValueError as exc:
        raise ExternalMailboxSecurityError(
            f"MAILBOX_SECURITY_VIOLATION: Invalid actor_id {actor_id!r}: {exc}",
            error_code="PATH_TRAVERSAL_FORBIDDEN",
        ) from exc
    root = Path(root_dir).resolve()
    actor_dir = root / actor_clean
    ensure_private_dir(actor_dir)
    outbox = actor_dir / "coordinator_outbox.jsonl"
    inbox = actor_dir / "actor_inbox.jsonl"
    return ExternalMailboxLayout(
        root_dir=root,
        coordinator_outbox_path=outbox,
        actor_inbox_path=inbox,
        actor_id=actor_clean,
    )


# 以受管目录打开的数据描述符为准，游标身份取同一次 fstat。
@contextmanager
def _mailbox_file(root, parts, flags):
    flags |= getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    create = bool(flags & os.O_CREAT)
    with ExitStack() as stack:
        if _supports_dir_fd():
            parent = open_directory_beneath(root, parts[:-1], create=create)
            stack.callback(os.close, parent)
            fd = os.open(parts[-1], flags, 0o600, dir_fd=parent)
            stack.callback(os.close, fd)
            named = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
        else:
            target = _portable_path(root, parts, create=create)
            try:
                before = target.lstat()
            except FileNotFoundError:
                before = None
            if before is not None and not stat.S_ISREG(before.st_mode):
                raise OSError(errno.EINVAL, "Mailbox leaf must be a regular file")
            fd = os.open(target, flags, 0o600)
            stack.callback(os.close, fd)
            named = target.lstat()
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not os.path.samestat(info, named):
            raise OSError(errno.EINVAL, "Mailbox leaf must be a stable regular single-link file")
        yield fd, info


def _read_window(stream, size):
    data = stream.read(size)
    if len(data) != size:
        raise OSError(errno.EIO, "Mailbox size changed before the selected bytes could be read")
    return data


# 旧行号只在迁移时扫描一次，使用固定窗口，避免对长行分配整行内存。
def _legacy_offset(stream, lines, size, window):
    offset = 0
    while lines > 0 and offset < size:
        data = _read_window(stream, min(window, size - offset))
        count = data.count(b"\n")
        if count >= lines:
            return offset + len(data) - len(data.split(b"\n", lines)[-1])
        offset += len(data)
        lines -= count
    return 0


def _resume_cursor(stream, info, previous, window, start_line):
    cursor = {
        "v": 2, "offset": 0, "st_dev": info.st_dev, "st_ino": info.st_ino, "generation": 0,
    }
    fields = ("offset", "generation", "st_dev", "st_ino")
    if previous.get("v") == 2 and all(type(previous.get(k)) is int and previous[k] >= 0 for k in fields):
        generation = previous["generation"]
        offset = previous["offset"]
        if ((previous["st_dev"], previous["st_ino"]) != (info.st_dev, info.st_ino)
                or offset > info.st_size):
            cursor["generation"] = generation + 1
        else:
            cursor.update(offset=offset, generation=generation)
            discard_start = previous.get("discard_start")
            if type(discard_start) is int and 0 <= discard_start < offset:
                cursor["discard_start"] = discard_start
        return cursor

    lines = previous.get("processed_lines", start_line)
    file_size = previous.get("file_size", 0)
    if type(lines) is int and lines > 0 and type(file_size) is int and 0 <= file_size <= info.st_size:
        cursor["offset"] = _legacy_offset(stream, lines, info.st_size, window)
    return cursor


def _parse_mailbox_window(data, cursor, window, start_line):
    messages, quarantined = [], []
    offset = cursor["offset"]

    if "discard_start" in cursor:
        end = data.find(b"\n") + 1
        if not end:
            cursor["offset"] += len(data)
            return messages, quarantined
        quarantined.append({
            "byte_start": cursor.pop("discard_start"),
            "byte_end": offset + end,
            "reason": "EXCEEDS_MAX_BYTES: line exceeds mailbox window",
        })
        offset += end
        data = data[end:]

    end = data.rfind(b"\n") + 1
    if not end and len(data) == window:
        # 余下页继续跳过这一行，见到 LF 时一次性报告完整字节范围。
        cursor.update(offset=offset + len(data), discard_start=offset)
        return messages, quarantined

    cursor["offset"] = offset + end
    for index, line in enumerate(data[:end].split(b"\n")[:-1], start_line + 1):
        next_offset = offset + len(line) + 1
        raw = line.strip()
        if raw:
            try:
                entry = json.loads(raw.decode("utf-8"))
            except (ValueError, RecursionError) as exc:
                reason = f"JSON_DECODE_ERROR: {exc}"
            else:
                reason = "RECORD_NOT_DICT"
                if isinstance(entry, dict):
                    messages.append(entry)
                    offset = next_offset
                    continue
            quarantined.append({
                "line": index,
                "byte_start": offset,
                "byte_end": next_offset,
                "reason": reason,
                "content": raw[:100].decode("utf-8", errors="replace"),
            })
        offset = next_offset
    return messages, quarantined


def _inbox_io_error(path, phase, exc):
    return {
        "path": str(path),
        "reason": f"{phase}: {exc}",
        "error_code": "MAILBOX_INBOX_IO_ERROR",
        "exception_type": type(exc).__name__,
        "errno": exc.errno,
        "retryable": True,
        "effect_outcome": "unknown" if phase == "cursor_write_failed" else "not_started",
        "next_action": "Retry sync_external_mailbox with the same mailbox and message IDs.",
    }


def _message_receipt(cursor, message_id):
    # 按身份、代次和消息 ID 做精确点查。分桶限制单目录大小，进程重启后仍可识别重复消息。
    key = json.dumps([cursor["st_dev"], cursor["st_ino"], cursor["generation"], message_id])
    digest = hashlib.sha256(key.encode("ascii")).hexdigest()
    return ".coordinator_seen", digest[:2], digest[2:]


def _receipt_exists(root, parts):
    try:
        with _mailbox_file(root, parts, os.O_RDONLY):
            return True
    except FileNotFoundError:
        return False


def _load_cursor(path):
    try:
        with _mailbox_file(path.parent, (path.name,), os.O_RDONLY) as (fd, _):
            with os.fdopen(fd, "r", encoding="utf-8", closefd=False) as stream:
                saved = json.load(stream)
    except (FileNotFoundError, ValueError, RecursionError):
        return {}
    return saved if isinstance(saved, dict) else {}


class ExternalMailboxAdapter:
    # max_file_bytes 限制每次收件箱读取的窗口。
    def __init__(
        self,
        allowed_roots: tuple[Path | str, ...] | list[Path | str],
        max_file_bytes: int = 262144,
    ) -> None:
        if type(max_file_bytes) is not int or max_file_bytes <= 0:
            raise ValueError("max_file_bytes must be a positive integer window size")
        self.allowed_roots = tuple(Path(r).resolve() for r in allowed_roots)
        self.max_file_bytes = max_file_bytes

    @staticmethod
    def _ensure_not_symlink(target: Path, *, message: str) -> None:
        try:
            st = os.lstat(target)
        except FileNotFoundError:
            return
        if stat.S_ISLNK(st.st_mode):
            raise ExternalMailboxSecurityError(message, error_code="SYMLINK_FORBIDDEN")

    # LLM: 校验目标路径是否无软链接且 realpath 完全限制在 allowed_roots 之内。
    # 函数用途: 防范路径穿越与符号链接逃逸攻击。
    def validate_confined_path(self, path: Path | str) -> Path:
        target = Path(path)
        self._ensure_not_symlink(
            target,
            message=f"MAILBOX_SECURITY_VIOLATION: Symlink rejected for mailbox path: {target}",
        )
        resolved = Path(os.path.realpath(target))
        is_confined = any(resolved.is_relative_to(root) for root in self.allowed_roots)
        if not is_confined:
            raise ExternalMailboxSecurityError(
                f"MAILBOX_SECURITY_VIOLATION: Path {target} resolves to {resolved} outside allowed roots",
                error_code="PATH_TRAVERSAL_FORBIDDEN",
            )
        return resolved

    def _read_actor_batch(self, layout, previous, *, start_line=0):
        path = layout.actor_inbox_path
        phase = "open_or_stat_failed"
        try:
            root, parts = self._confined_file(path)
            with _mailbox_file(root, parts, os.O_RDONLY) as (fd, info):
                phase = "read_failed"
                with os.fdopen(fd, "rb", buffering=0, closefd=False) as stream:
                    cursor = _resume_cursor(stream, info, previous, self.max_file_bytes, start_line)
                    stream.seek(cursor["offset"])
                    data = _read_window(stream, min(self.max_file_bytes, info.st_size - cursor["offset"]))
            messages, quarantined = _parse_mailbox_window(data, cursor, self.max_file_bytes, start_line)
            return messages, quarantined, cursor
        except FileNotFoundError:
            return [], [], None
        except ExternalMailboxSecurityError as exc:
            return [], [{"path": str(path), "reason": str(exc), "error_code": exc.error_code}], None
        except OSError as exc:
            return [], [_inbox_io_error(path, phase, exc)], None

    def _confined_file(self, path):
        resolved = self.validate_confined_path(path)
        for root in self.allowed_roots:
            if resolved != root and resolved.is_relative_to(root):
                return root, resolved.relative_to(root).parts
        raise ExternalMailboxSecurityError(
            "Mailbox data must be a file below an allowed root",
            error_code="PATH_TRAVERSAL_FORBIDDEN",
        )

    def read_actor_messages(
        self,
        layout: ExternalMailboxLayout,
        *,
        start_line: int = 0,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """按旧行号入口读取一页完整记录。持续同步使用持久化字节游标。"""
        messages, quarantined, _ = self._read_actor_batch(layout, {}, start_line=start_line)
        return messages, quarantined

    def write_coordinator_message(
        self,
        layout: ExternalMailboxLayout,
        message: dict[str, Any],
    ) -> None:
        """用原路径锁串行追加一条消息，保留已有数据与文件身份。"""
        target = layout.coordinator_outbox_path
        self.validate_confined_path(target.parent)
        root, parts = self._confined_file(target)
        target = root.joinpath(*parts)
        ensure_private_dir(target.parent)
        packet = dict(message)
        if not packet.get("message_id"):
            packet["message_id"] = uuid.uuid4().hex
        data = (json.dumps(packet, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        with locked_json_path(target), _mailbox_file(root, parts, os.O_WRONLY | os.O_APPEND | os.O_CREAT) as (fd, info):
            written = os.write(fd, data)
            if written != len(data):
                # 单写者锁内回收本次短写的尾部，保留此前已经追加的行。
                if written and os.fstat(fd).st_size == info.st_size + written:
                    os.ftruncate(fd, info.st_size)
                raise OSError(errno.EIO, f"Mailbox append wrote {written} of {len(data)} bytes")
            os.fsync(fd)

    def sync_external_mailbox(
        self,
        handoff_service: HandoffBoardService,
        layout: ExternalMailboxLayout,
        *,
        now: float | None = None,
    ) -> dict[str, Any]:
        cursor_path = self.validate_confined_path(layout.actor_inbox_path.parent) / ".coordinator_cursor.json"
        with locked_json_path(cursor_path):
            return self._sync_locked(handoff_service, layout, cursor_path, now)

    def _sync_locked(self, handoff_service, layout, cursor_path, now):
        current_time_val = float(now if now is not None else time.time())
        mailbox_dir = cursor_path.parent
        try:
            previous = _load_cursor(cursor_path)
        except OSError as exc:
            previous = None
            messages, quarantined, cursor = [], [_inbox_io_error(cursor_path, "cursor_read_failed", exc)], None
        if previous is not None:
            messages, quarantined, cursor = self._read_actor_batch(layout, previous)

        delivered_item_ids: list[str] = []
        acked_item_ids: list[str] = []
        submitted_item_ids: list[str] = []
        errors: list[dict[str, Any]] = []
        retryable_apply_error = False

        # 1. 消费外部助手的回复
        for msg in messages:
            action = str(msg.get("action") or msg.get("type") or "").strip().lower()
            case_id = str(msg.get("case_id") or "").strip()
            item_id = str(msg.get("item_id") or "").strip()
            if not case_id or not item_id or action not in {"ack", "record_ack", "submit", "submit_handoff"}:
                continue

            message_id = str(msg.get("message_id") or "").strip()
            receipt = _message_receipt(cursor, message_id) if message_id else None
            try:
                if receipt is not None and _receipt_exists(mailbox_dir, receipt):
                    continue
                if action in {"ack", "record_ack"}:
                    item = handoff_service.store.load_handoff_item(case_id, item_id)
                    if item is None or item.responsible_actor != layout.actor_id:
                        errors.append({
                            "item_id": item_id,
                            "action": action,
                            "error_code": "ACTOR_MISMATCH",
                            "error": f"item belongs to '{getattr(item, 'responsible_actor', '')}', not '{layout.actor_id}'",
                        })
                        continue
                    ack_payload = msg.get("recipient_ack") or msg.get("payload") or {}
                    ack_payload = dict(ack_payload) if isinstance(ack_payload, dict) else {}
                    if "actor_id" in ack_payload and ack_payload["actor_id"] != layout.actor_id:
                        errors.append({
                            "item_id": item_id, "action": action,
                            "error_code": "MAILBOX_ACTOR_MISMATCH",
                            "field": "recipient_ack.actor_id",
                            "error": "recipient_ack.actor_id 必须与当前邮箱 actor_id 完全一致",
                            "required_actor_id": layout.actor_id,
                            "effect_outcome": "not_started",
                            "next_action": "将 recipient_ack.actor_id 改为 required_actor_id，或省略该字段由邮箱绑定；不得代其他主体签收。",
                        })
                        continue
                    ack_payload["actor_id"] = layout.actor_id
                    frozen_ver = str(msg.get("frozen_input_version") or "")
                    ack_id = str(msg.get("ack_id") or msg.get("message_id") or "")
                    handoff_service.record_ack(
                        case_id=case_id,
                        item_id=item_id,
                        recipient_ack=dict(ack_payload) if isinstance(ack_payload, dict) else {},
                        frozen_input_version=frozen_ver or None,
                        ack_id=ack_id,
                        now=current_time_val,
                    )
                    acked_item_ids.append(item_id)
                elif action in {"submit", "submit_handoff"}:
                    item = handoff_service.store.load_handoff_item(case_id, item_id)
                    if item is None or item.responsible_actor != layout.actor_id:
                        errors.append({
                            "item_id": item_id,
                            "action": action,
                            "error_code": "ACTOR_MISMATCH",
                            "error": f"item belongs to '{getattr(item, 'responsible_actor', '')}', not '{layout.actor_id}'",
                        })
                        continue
                    evidence_ids = msg.get("evidence_ids") or ()
                    if isinstance(evidence_ids, (list, tuple)):
                        ev_tuple = tuple(str(x) for x in evidence_ids)
                    else:
                        ev_tuple = ()
                    sub_id = str(msg.get("submission_id") or msg.get("message_id") or "")
                    sub_payload = msg.get("payload") or {}
                    sub_payload = dict(sub_payload) if isinstance(sub_payload, dict) else {}
                    if "actor_id" in sub_payload and sub_payload["actor_id"] != layout.actor_id:
                        errors.append({
                            "item_id": item_id, "action": action,
                            "error_code": "MAILBOX_ACTOR_MISMATCH",
                            "field": "payload.actor_id",
                            "error": "payload.actor_id 必须与当前邮箱 actor_id 完全一致",
                            "required_actor_id": layout.actor_id,
                            "effect_outcome": "not_started",
                            "next_action": "将 payload.actor_id 改为 required_actor_id，或省略该字段由邮箱绑定；不得代其他主体提交。",
                        })
                        continue
                    sub_payload["actor_id"] = layout.actor_id
                    handoff_service.submit_handoff(
                        case_id=case_id,
                        item_id=item_id,
                        evidence_ids=ev_tuple,
                        submission_payload=dict(sub_payload) if isinstance(sub_payload, dict) else {},
                        submission_id=sub_id,
                        now=current_time_val,
                    )
                    submitted_item_ids.append(item_id)
                if receipt is not None:
                    # 原交接应用可重放。只有应用成功后才留下去重记录。
                    with _mailbox_file(mailbox_dir, receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL):
                        pass
            except OSError as exc:
                retryable_apply_error = True
                logger.warning(
                    "Transient I/O error applying external mailbox message item_id=%s action=%s: %s",
                    item_id,
                    action,
                    exc,
                )
                errors.append(
                    {
                        "item_id": item_id,
                        "action": action,
                        "error_code": "MAILBOX_APPLY_IO_ERROR",
                        "error": f"{type(exc).__name__}: {exc}",
                        "exception_type": type(exc).__name__,
                        "errno": exc.errno,
                        "effect_outcome": "unknown",
                        "retryable": True,
                        "next_action": (
                            f"Retry sync_external_mailbox with root_dir={str(layout.root_dir)!r} "
                            f"and actor_id={layout.actor_id!r}; keep original message IDs."
                        ),
                    }
                )
            except Exception as exc:  # noqa: BLE001
                errors.append({"item_id": item_id, "action": action, "error": str(exc)})

        if cursor is not None and cursor != previous and not retryable_apply_error:
            try:
                write_text_atomic_beneath(mailbox_dir, (cursor_path.name,), json.dumps(cursor) + "\n", durable=False)
            except OSError as exc:
                quarantined.append(_inbox_io_error(cursor_path, "cursor_write_failed", exc))

        # 2. 检查是否有待投递给该 external_assistant 的 published 交接项
        try:
            all_items = handoff_service.store.list_all_handoff_items(
                state="published",
                responsible_actor=layout.actor_id,
            )
        except OSError:
            if not any(err.get("error_code") == "MAILBOX_APPLY_IO_ERROR" for err in errors):
                raise
            all_items = []
        for item in all_items:
            # 仅处理尚未写入 mailbox 投递记录的项目
            delivery_receipt = item.delivery_receipt or {}
            if delivery_receipt.get("mailbox_outbox_path"):
                continue

            message_key = f"{item.case_id}:{item.item_id}".encode()
            packet = {
                "action": "dispatch_item",
                "message_id": f"mailbox_deliv_{hashlib.sha256(message_key).hexdigest()}",
                "item_id": item.item_id,
                "case_id": item.case_id,
                "request_id": item.request_id,
                "responsible_actor": item.responsible_actor,
                "frozen_input_version": item.frozen_input_version,
                "exclusive_write_set": list(item.exclusive_write_set),
                "dispatched_at": current_time_val,
            }
            self.write_coordinator_message(layout, packet)
            new_receipt = {
                "mailbox_outbox_path": str(layout.coordinator_outbox_path),
                "dispatched_at": current_time_val,
                "actor_id": layout.actor_id,
            }
            handoff_service.record_delivery(
                case_id=item.case_id,
                item_id=item.item_id,
                delivery_receipt=new_receipt,
                delivery_id=f"mailbox_deliv_{item.item_id}",
                now=current_time_val,
            )
            delivered_item_ids.append(item.item_id)

        return {
            "delivered_item_ids": delivered_item_ids,
            "acked_item_ids": acked_item_ids,
            "submitted_item_ids": submitted_item_ids,
            "quarantined": quarantined,
            "errors": errors,
        }
