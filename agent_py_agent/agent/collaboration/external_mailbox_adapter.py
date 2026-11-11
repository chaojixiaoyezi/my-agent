# LLM: 外部交互式助手安全文件邮箱适配器（External Mailbox Adapter）：
#   实现协调器与外部交互式助手的严格单写者物理文件隔离：
#   1. 协调器仅原子写入协调器属主 outbox 文件（temp-write + os.replace）；
#   2. 仅安全读取外部助手属主 inbox/ACK 文件，严格限制字节数（<= 256 KiB）、限制真实路径处于授权协调根内、硬拒绝软链接；
#   3. 坏记录与损坏格式走 quarantine/skip 安全隔离，严禁死循环；
#   4. 绝对禁止生成外部 CLI/PTY/SDK 子进程或模拟发送终端按键。
# 模块用途: 提供外部助手有界文件邮箱传输适配与同步功能。
"""Bounded writer-separated external assistant file adapter."""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..common.nofollow_fs import ensure_private_dir
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
    root = Path(root_dir).resolve()
    actor_clean = str(actor_id or "").strip()
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


# LLM: 外部助手安全文件邮箱适配器，负责单写者文件原子写入与有界防逃逸读取。
# 类用途: 协调器与外部助手的安全文件交换适配器。
class ExternalMailboxAdapter:
    # LLM: 初始化邮箱适配器，记录允许的根目录集合与最大单文件读取上限（默认 256 KiB）。
    # 函数用途: 构造外部助手邮箱适配器。
    def __init__(
        self,
        allowed_roots: tuple[Path | str, ...] | list[Path | str],
        max_file_bytes: int = 262144,
    ) -> None:
        self.allowed_roots = tuple(Path(r).resolve() for r in allowed_roots)
        self.max_file_bytes = max_file_bytes

    # LLM: 校验目标路径是否无软链接且 realpath 完全限制在 allowed_roots 之内。
    # 函数用途: 防范路径穿越与符号链接逃逸攻击。
    def validate_confined_path(self, path: Path | str) -> Path:
        target = Path(path)
        if target.is_symlink():
            raise ExternalMailboxSecurityError(
                f"MAILBOX_SECURITY_VIOLATION: Symlink rejected for mailbox path: {target}",
                error_code="SYMLINK_FORBIDDEN",
            )
        resolved = Path(os.path.realpath(target))
        is_confined = any(
            str(resolved).startswith(str(root) + os.sep) or resolved == root
            for root in self.allowed_roots
        )
        if not is_confined:
            raise ExternalMailboxSecurityError(
                f"MAILBOX_SECURITY_VIOLATION: Path {target} resolves to {resolved} outside allowed roots",
                error_code="PATH_TRAVERSAL_FORBIDDEN",
            )
        return resolved

    # LLM: 安全读取外部助手写入的 inbox 文件，包含大小上限检查、软链接拒绝与格式异常跳过/隔离。
    # 函数用途: 读取外部助手上报的消息记录列表。
    def read_actor_messages(
        self,
        layout: ExternalMailboxLayout,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        valid_records: list[dict[str, Any]] = []
        quarantined: list[dict[str, Any]] = []

        path = layout.actor_inbox_path
        if not path.exists():
            return valid_records, quarantined

        try:
            self.validate_confined_path(path)
        except ExternalMailboxSecurityError as exc:
            quarantined.append({"path": str(path), "reason": str(exc), "error_code": exc.error_code})
            return valid_records, quarantined

        try:
            file_stat = path.stat()
        except OSError as exc:
            quarantined.append({"path": str(path), "reason": f"stat_failed: {exc}"})
            return valid_records, quarantined

        if file_stat.st_size > self.max_file_bytes:
            # 超过 256 KiB 限制，安全隔离并不读取内容
            quarantine_dir = path.parent / ".quarantine"
            ensure_private_dir(quarantine_dir)
            quarantine_dest = quarantine_dir / f"oversize_{int(time.time())}_{path.name}"
            try:
                shutil.move(str(path), str(quarantine_dest))
            except Exception as move_err:  # noqa: BLE001
                logger.warning("Failed to move oversized mailbox file: %s", move_err)
            quarantined.append({
                "path": str(path),
                "reason": f"EXCEEDS_MAX_BYTES: size={file_stat.st_size} max={self.max_file_bytes}",
                "quarantine_path": str(quarantine_dest),
            })
            return valid_records, quarantined

        # 逐行读取 JSONL
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError as exc:
            quarantined.append({"path": str(path), "reason": f"read_failed: {exc}"})
            return valid_records, quarantined

        has_corrupt = False
        valid_lines: list[str] = []
        for line_no, raw_line in enumerate(lines, 1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                if isinstance(data, dict):
                    valid_records.append(data)
                    valid_lines.append(raw_line)
                else:
                    has_corrupt = True
                    quarantined.append({
                        "line": line_no,
                        "reason": "RECORD_NOT_DICT",
                        "content": line[:100],
                    })
            except Exception as exc:  # noqa: BLE001
                has_corrupt = True
                quarantined.append({
                    "line": line_no,
                    "reason": f"JSON_DECODE_ERROR: {exc}",
                    "content": line[:100],
                })

        # 若存在坏行，可选择将损坏行记录日志并保留有效记录
        if has_corrupt:
            logger.warning("Quarantined %d corrupt records in %s", len(quarantined), path)

        return valid_records, quarantined

    # LLM: 协调器原子写入消息到 coordinator_outbox 文件（单写者；临时文件 + os.replace）。
    # 函数用途: 向外部助手的收件箱原子推送新交接或指令。
    def write_coordinator_message(
        self,
        layout: ExternalMailboxLayout,
        message: dict[str, Any],
    ) -> None:
        target_path = layout.coordinator_outbox_path
        self.validate_confined_path(target_path.parent)

        if target_path.exists() and target_path.is_symlink():
            raise ExternalMailboxSecurityError(
                f"MAILBOX_SECURITY_VIOLATION: coordinator_outbox is a symlink: {target_path}",
                error_code="SYMLINK_FORBIDDEN",
            )

        ensure_private_dir(target_path.parent)

        # 读取已有行并追加新行，写入同目录下临时文件后原子替换
        existing_lines: list[str] = []
        if target_path.exists():
            with open(target_path, encoding="utf-8") as f:
                existing_lines = f.readlines()

        serialized_message = json.dumps(message, ensure_ascii=False, sort_keys=True)
        existing_lines.append(serialized_message + "\n")

        pid = os.getpid()
        ns = time.time_ns()
        tmp_path = target_path.parent / f".tmp.{target_path.name}.{pid}_{ns}"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.writelines(existing_lines)
            f.flush()
            os.fsync(f.fileno())

        os.replace(tmp_path, target_path)

    # LLM: 同步外部邮箱：消费助手 inbox 的 ACK/提交，推送 published 交接项到 coordinator_outbox。
    # 函数用途: 批量同步外部邮箱的双向交接状态。
    def sync_external_mailbox(
        self,
        handoff_service: HandoffBoardService,
        layout: ExternalMailboxLayout,
        *,
        now: float | None = None,
    ) -> dict[str, Any]:
        current_time_val = float(now if now is not None else time.time())
        messages, quarantined = self.read_actor_messages(layout)

        delivered_item_ids: list[str] = []
        acked_item_ids: list[str] = []
        submitted_item_ids: list[str] = []
        errors: list[dict[str, Any]] = []

        # 1. 消费外部助手的回复
        for msg in messages:
            action = str(msg.get("action") or msg.get("type") or "").strip().lower()
            case_id = str(msg.get("case_id") or "").strip()
            item_id = str(msg.get("item_id") or "").strip()
            if not case_id or not item_id:
                continue

            try:
                if action in {"ack", "record_ack"}:
                    ack_payload = msg.get("recipient_ack") or msg.get("payload") or {}
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
                    evidence_ids = msg.get("evidence_ids") or ()
                    if isinstance(evidence_ids, (list, tuple)):
                        ev_tuple = tuple(str(x) for x in evidence_ids)
                    else:
                        ev_tuple = ()
                    sub_id = str(msg.get("submission_id") or msg.get("message_id") or "")
                    sub_payload = msg.get("payload") or {}
                    handoff_service.submit_handoff(
                        case_id=case_id,
                        item_id=item_id,
                        evidence_ids=ev_tuple,
                        submission_payload=dict(sub_payload) if isinstance(sub_payload, dict) else {},
                        submission_id=sub_id,
                        now=current_time_val,
                    )
                    submitted_item_ids.append(item_id)
            except Exception as exc:  # noqa: BLE001
                errors.append({"item_id": item_id, "action": action, "error": str(exc)})

        # 2. 检查是否有待投递给该 external_assistant 的 published 交接项
        all_items = handoff_service.store.list_all_handoff_items(
            state="published",
            responsible_actor=layout.actor_id,
        )
        for item in all_items:
            # 仅处理尚未写入 mailbox 投递记录的项目
            delivery_receipt = item.delivery_receipt or {}
            if delivery_receipt.get("mailbox_outbox_path"):
                continue

            packet = {
                "action": "dispatch_item",
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
