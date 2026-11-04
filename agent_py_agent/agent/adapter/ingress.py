from __future__ import annotations

"""LLM: This module owns the adapter-side durable ingress outbox before any provider or Gateway IO.

模块用途: 先保存外部消息的可信身份和规范正文，再由一个后台线程推进媒体、Gateway 提交、占位和结果回送。
"""

import hashlib
import json
import logging
import os
import re
import stat
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from ..common.json_io import locked_json_path
from ..common.nofollow_fs import ensure_private_dir
from ..gateway_parts.recovery_pending_index import directory_page
from .delivery import GatewayClaimLeaseConfig, GatewayReplyDeliveryWorker
from .inbound_images import InboundMediaInputError
from .ingress_index import IngressIndex

_LOGGER = logging.getLogger(__name__)
_SCHEMA_VERSION = 3
# 入站记录保留 7 天：足够排查与审计，又不长期占用存储。
_TERMINAL_RETENTION_SECONDS = 7 * 24 * 60 * 60
_INGRESS_STATES = frozenset(
    {"prepared", "payload_ready", "submitted", "placeholder_ready", "completed"}
)


# LLM: The digest covers the authenticated route and original provider body before media creates
# a machine-local path; the persisted payload must remain JSON-canonical and replayable.
# 类用途: 保存一条外部消息从首次接收直到移交结果 watcher 的完整持久状态。
@dataclass(frozen=True)
class GatewayIngressRecord:
    ingress_id: str
    channel: str
    user_id: str
    conversation_id: str
    provider_message_id: str
    canonical_payload_digest: str
    content: str
    metadata: dict[str, object]
    kind: str = 'message'
    timestamp: float = 0.0
    state: str = "prepared"
    gateway_payload: dict[str, object] | None = None
    submission: dict[str, object] | None = None
    progress_handle: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    processing_attempts: int = 0
    next_attempt_at: float = 0.0
    claim_owner: str = ""
    claim_epoch: int = 0
    claim_expires_at: float = 0.0

    # LLM: owner 编号来自配置宿主在 route_message 解析后的元数据；纯运输测试和旧记录的空值不表示主 owner。
    # 函数用途: 对外投影明确记录归属，不通过物理队列位置猜 owner。
    @property
    def owner_id(self) -> str:
        return str(self.metadata.get('owner_id') or '')

    # LLM: Closed transport states and required provider identity are validated at every disk read;
    # malformed records remain visible on disk and never enter an IO callback.
    # 函数用途: 拒绝缺身份或状态损坏的入站记录，避免错误路由继续推进。
    def __post_init__(self) -> None:
        state = str(self.state or "").strip().lower()
        if state not in _INGRESS_STATES:
            raise ValueError(f"invalid gateway ingress state: {state}")
        required = (
            self.ingress_id,
            self.channel,
            self.provider_message_id,
            self.canonical_payload_digest,
        )
        if self.kind not in {'message', 'observation'} or (self.kind == 'message' and not self.user_id):
            raise ValueError('invalid gateway ingress sender kind')
        if self.kind == 'observation' and (self.user_id or not isinstance(self.metadata.get('observation'), dict)):
            raise ValueError('observation cannot carry a human actor')
        if not all(str(value or "").strip() for value in required):
            raise ValueError("gateway ingress record is missing authenticated identity")
        claim_owner = str(self.claim_owner or "").strip()
        claim_epoch = max(0, int(self.claim_epoch or 0))
        claim_expires_at = max(0.0, float(self.claim_expires_at or 0.0))
        if claim_owner and claim_epoch <= 0:
            raise ValueError("gateway ingress claim owner requires a positive epoch")
        if not claim_owner:
            claim_expires_at = 0.0
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "claim_owner", claim_owner)
        object.__setattr__(self, "claim_epoch", claim_epoch)
        object.__setattr__(self, "claim_expires_at", claim_expires_at)

    # LLM: Disk schema is explicit and versioned; callers must not serialize an ad-hoc subset.
    # 函数用途: 把完整入站状态转换为可原子写入的 JSON 对象。
    def to_dict(self) -> dict[str, object]:
        return {"schema_version": _SCHEMA_VERSION, **asdict(self), 'owner_id': self.owner_id}

    # LLM: Recovery reconstructs only the versioned typed record and does not infer route or state
    # from filenames, logs, or user-visible text.
    # 函数用途: 从持久 JSON 恢复入站记录并重新执行同一套校验。
    @classmethod
    def from_dict(cls, data: dict[str, object]) -> GatewayIngressRecord:
        metadata = data.get("metadata")
        gateway_payload = data.get("gateway_payload")
        submission = data.get("submission")
        return cls(
            kind=str(data.get('kind') or 'message'),
            ingress_id=str(data.get("ingress_id") or "").strip(),
            channel=str(data.get("channel") or "").strip(),
            user_id=str(data.get("user_id") or "").strip(),
            conversation_id=str(data.get("conversation_id") or "").strip(),
            provider_message_id=str(data.get("provider_message_id") or "").strip(),
            canonical_payload_digest=str(
                data.get("canonical_payload_digest") or ""
            ).strip(),
            content=str(data.get("content") or ""),
            metadata=dict(metadata) if isinstance(metadata, dict) else {},
            timestamp=float(data.get("timestamp") or 0.0),
            state=str(data.get("state") or "prepared"),
            gateway_payload=(
                dict(gateway_payload) if isinstance(gateway_payload, dict) else None
            ),
            submission=dict(submission) if isinstance(submission, dict) else None,
            progress_handle=str(data.get("progress_handle") or ""),
            created_at=float(data.get("created_at") or 0.0),
            updated_at=float(data.get("updated_at") or 0.0),
            processing_attempts=max(0, int(data.get("processing_attempts") or 0)),
            next_attempt_at=max(0.0, float(data.get("next_attempt_at") or 0.0)),
            claim_owner=str(data.get("claim_owner") or "").strip(),
            claim_epoch=max(0, int(data.get("claim_epoch") or 0)),
            claim_expires_at=max(0.0, float(data.get("claim_expires_at") or 0.0)),
        )


# LLM: Submission handling returns only the next typed ingress state and optional provider handle;
# the worker owns the CAS and never lets channel callbacks mutate durable state directly.
# 类用途: 表示 Gateway 已响应后，控制回复已完成或普通消息已创建占位的推进结果。
@dataclass(frozen=True)
class GatewayIngressAdvance:
    state: str
    progress_handle: str = ""

    # LLM: Only worker-recognized post-submission transitions are accepted.
    # 函数用途: 防止回调跳过 watcher 移交或写入未知状态。
    def __post_init__(self) -> None:
        if self.state not in {"placeholder_ready", "completed"}:
            raise ValueError(f"invalid gateway ingress advance state: {self.state}")


# LLM: Canonicalization is a strict JSON round trip so digest equality means byte-equivalent
# structured input, independent of dictionary insertion order.
# 函数用途: 生成用于防重复和冲突隔离的规范入站正文。
def canonical_ingress_payload(
    *,
    channel: str,
    user_id: str,
    conversation_id: str,
    provider_message_id: str,
    content: str,
    metadata: dict[str, object],
) -> dict[str, object]:
    payload = {
        "channel": str(channel or "").strip(),
        "user_id": str(user_id or "").strip(),
        "conversation_id": str(conversation_id or "").strip(),
        "provider_message_id": str(provider_message_id or "").strip(),
        "content": str(content or ""),
        "metadata": metadata,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    decoded = json.loads(encoded)
    if not isinstance(decoded, dict):  # pragma: no cover - JSON object built above.
        raise ValueError("canonical gateway ingress payload must be an object")
    return decoded


# LLM: The digest algorithm and compact JSON encoding are stable protocol details shared by
# duplicate detection and quarantine evidence.
# 函数用途: 计算规范入站正文的稳定 SHA-256 摘要。
def canonical_payload_digest(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


# LLM: One provider message has one stable adapter identity. User/conversation/content stay in the
# canonical digest so a replay with changed authenticated facts is quarantined under that identity.
# 函数用途: 从可信通道消息字段创建首次落盘的 prepared 记录。
def build_gateway_ingress_record(
    *,
    channel: str,
    user_id: str,
    conversation_id: str,
    provider_message_id: str,
    content: str,
    metadata: dict[str, object],
    timestamp: float,
    now: float | None = None,
) -> GatewayIngressRecord:
    normalized_channel = str(channel or "").strip()
    normalized_message_id = str(provider_message_id or "").strip()
    ingress_anchor = json.dumps(
        [normalized_channel, normalized_message_id],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    ingress_id = hashlib.sha256(ingress_anchor.encode("utf-8")).hexdigest()
    canonical = canonical_ingress_payload(
        channel=normalized_channel,
        user_id=user_id,
        conversation_id=conversation_id,
        provider_message_id=normalized_message_id,
        content=content,
        metadata=metadata,
    )
    created_at = time.time() if now is None else float(now)
    canonical_metadata = canonical.get("metadata")
    return GatewayIngressRecord(
        ingress_id=ingress_id,
        channel=normalized_channel,
        user_id=str(canonical.get("user_id") or ""),
        conversation_id=str(canonical.get("conversation_id") or ""),
        provider_message_id=normalized_message_id,
        canonical_payload_digest=canonical_payload_digest(canonical),
        content=str(canonical.get("content") or ""),
        metadata=(
            dict(canonical_metadata) if isinstance(canonical_metadata, dict) else {}
        ),
        timestamp=float(timestamp or 0.0),
        created_at=created_at,
        updated_at=created_at,
    )


# LLM: The store is the adapter ingress authority. Same-id/same-body returns the existing record;
# same-id/different-body never overwrites it and writes separate quarantine evidence.
# 类用途: 原子保存入站记录、执行 CAS 推进并保留冲突隔离证据，供进程重启恢复。
class GatewayIngressStore:
    # LLM: 索引与扫描位置只属于当前 spool，不复用其它根的缓存。
    # 函数用途: 初始化规范存储、投影和有界维护运行态。
    def __init__(self, root: Path | None) -> None:
        self.root = Path(root).expanduser() if root is not None else None
        self._volatile_records: dict[str, GatewayIngressRecord] = {}
        self._volatile_quarantine: list[dict[str, object]] = []
        self._lock = threading.RLock()
        self._index: IngressIndex | None = None
        self._scans: dict[str, object] = {}
        self._scan_stamps: dict[str, str] = {}

    @property
    def durable(self) -> bool:
        return self.root is not None

    # LLM: 首写者拥有消息身份；规范文件、intent 与投影在同一路径锁内发布，冲突证据在锁外另存。
    # 函数用途: 首次登记消息；完全相同的重放复用原记录，正文或路由变化则隔离。
    def put_if_absent(
        self,
        record: GatewayIngressRecord,
    ) -> tuple[GatewayIngressRecord | None, bool, bool]:
        conflict: tuple[GatewayIngressRecord, GatewayIngressRecord] | None = None
        with self._lock:
            if self.root is None:
                current = self._volatile_records.get(record.ingress_id)
                if current is None:
                    self._volatile_records[record.ingress_id] = record
                    return record, True, False
                if not _same_ingress_body(current, record):
                    conflict = (current, record)
                else:
                    return current, False, False
            else:
                self._ensure_dirs()
                path = self._record_path(record.ingress_id)
                with locked_json_path(path):
                    current = self._read_record(path) if path.exists() else None
                    if path.exists() and current is None:
                        raise RuntimeError("gateway ingress record is unreadable")
                    if current is None:
                        self._write_indexed(path, record)
                        return record, True, False
                    if not _same_ingress_body(current, record):
                        conflict = (current, record)
                    else:
                        return current, False, False
        assert conflict is not None
        self._quarantine_conflict(*conflict)
        return conflict[0], False, True

    # LLM: 持原路径锁核对完整前像；规范状态先发布，查找投影错误留下可重放 intent。
    # 函数用途: 用比较并交换持久推进一条入站记录，避免旧 worker 覆盖恢复后的新状态。
    def compare_and_swap(
        self,
        expected: GatewayIngressRecord,
        replacement: GatewayIngressRecord,
    ) -> bool:
        _ensure_same_ingress_identity(expected, replacement)
        with self._lock:
            if self.root is None:
                current = self._volatile_records.get(expected.ingress_id)
                if current != expected:
                    return False
                self._volatile_records[expected.ingress_id] = replacement
                return True
            self._ensure_dirs()
            path = self._record_path(expected.ingress_id)
            with locked_json_path(path):
                current = self._read_record(path) if path.exists() else None
                if path.exists() and current is None:
                    raise RuntimeError("gateway ingress record is unreadable")
                if current != expected:
                    return False
                self._write_indexed(path, replacement)
                return True

    # LLM: 原跨进程 CAS 持续保护 owner/epoch，过期接管只能增加 epoch；锁内不执行运输回调。
    # 函数用途: 为一次外部 IO 阶段领取跨进程租约，返回带 owner/epoch/expiry 的权威快照。
    def claim(
        self,
        observed: GatewayIngressRecord,
        *,
        owner: str,
        now: float,
        ttl: float,
    ) -> GatewayIngressRecord | None:
        normalized_owner = str(owner or "").strip()
        if not normalized_owner:
            raise ValueError("gateway ingress claim owner is required")
        expires_at = float(now) + max(1.0, float(ttl))
        with self._lock:
            if self.root is None:
                current = self._volatile_records.get(observed.ingress_id)
                if current != observed or current.state == "completed":
                    return None
                if current.claim_owner and current.claim_expires_at > now:
                    return None
                claimed = replace(
                    current,
                    claim_owner=normalized_owner,
                    claim_epoch=current.claim_epoch + 1,
                    claim_expires_at=expires_at,
                )
                self._volatile_records[current.ingress_id] = claimed
                return claimed
            self._ensure_dirs()
            path = self._record_path(observed.ingress_id)
            with locked_json_path(path):
                current = self._read_record(path) if path.exists() else None
                if current != observed or current is None or current.state == "completed":
                    return None
                if current.claim_owner and current.claim_expires_at > now:
                    return None
                claimed = replace(
                    current,
                    claim_owner=normalized_owner,
                    claim_epoch=current.claim_epoch + 1,
                    claim_expires_at=expires_at,
                )
                self._write_indexed(path, claimed)
                return claimed

    # LLM: 只有原 owner/epoch 的完整规范前像可提交，释放清空 owner/expiry 但保留 epoch 历史。
    # 函数用途: 提交当前租约阶段的结果；租约被接管时拒绝旧 worker 写回。
    def commit_claim(
        self,
        expected: GatewayIngressRecord,
        replacement: GatewayIngressRecord,
        *,
        owner: str,
        epoch: int,
        release: bool = True,
    ) -> GatewayIngressRecord | None:
        _ensure_same_ingress_identity(expected, replacement)
        if expected.claim_owner != owner or expected.claim_epoch != int(epoch):
            return None
        committed = replace(
            replacement,
            claim_owner="" if release else owner,
            claim_epoch=int(epoch),
            claim_expires_at=0.0 if release else expected.claim_expires_at,
        )
        with self._lock:
            if self.root is None:
                current = self._volatile_records.get(expected.ingress_id)
                if current != expected:
                    return None
                self._volatile_records[expected.ingress_id] = committed
                return committed
            self._ensure_dirs()
            path = self._record_path(expected.ingress_id)
            with locked_json_path(path):
                current = self._read_record(path) if path.exists() else None
                if current != expected:
                    return None
                self._write_indexed(path, committed)
                return committed

    # LLM: A WAIT result releases only the matching epoch; a newer claimant cannot be cleared by a
    # stale worker.
    # 函数用途: 当前阶段没有状态变化时释放租约，供下一个轮询周期继续。
    def release_claim(
        self,
        record: GatewayIngressRecord,
        *,
        owner: str,
        epoch: int,
    ) -> GatewayIngressRecord | None:
        return self.commit_claim(
            record,
            record,
            owner=owner,
            epoch=epoch,
            release=True,
        )

    # LLM: Retry backoff is also a same-epoch commit, so a timed-out worker cannot defer a record
    # already reclaimed and advanced elsewhere.
    # 函数用途: 当前租约的外部 IO 失败后写退避并释放租约。
    def defer_claimed(
        self,
        record: GatewayIngressRecord,
        *,
        owner: str,
        epoch: int,
    ) -> GatewayIngressRecord | None:
        attempts = record.processing_attempts + 1
        delay = min(60.0, max(1.0, 2.0 ** min(attempts - 1, 6)))
        now = time.time()
        return self.commit_claim(
            record,
            replace(
                record,
                processing_attempts=attempts,
                next_attempt_at=now + delay,
                updated_at=now,
            ),
            owner=owner,
            epoch=epoch,
            release=True,
        )

    # LLM: Retry timing is mutable worker state and advances through the same CAS as every other
    # ingress transition; it cannot change route, body digest, or completed state.
    # 函数用途: 外部 IO 瞬时失败后写入有界指数退避，等待同一 worker 稍后恢复。
    def defer_after_failure(self, record: GatewayIngressRecord) -> bool:
        attempts = record.processing_attempts + 1
        delay = min(60.0, max(1.0, 2.0 ** min(attempts - 1, 6)))
        return self.compare_and_swap(
            record,
            replace(
                record,
                processing_attempts=attempts,
                next_attempt_at=time.time() + delay,
                updated_at=time.time(),
            ),
        )

    # LLM: 非完成态合同不含退避判断；只核一批索引候选，绝不调用全量诊断 records。
    # 函数用途: 返回按创建时间排序的有界未完成规范记录。
    def pending(self) -> list[GatewayIngressRecord]:
        return self._candidates("pending")

    # LLM: 调度使用独立 due 部分索引，前一批退避或有效租约不能饿死后面的活跃行。
    # 函数用途: 返回当前可尝试推进的有界候选，执行前仍走原 claim CAS。
    def due(self, now: float) -> list[GatewayIngressRecord]:
        return self._candidates("due", now=now)

    # LLM: 每条候选重新核读规范 JSON；索引过时只能降低本轮数量，不能授权外部 IO。
    # 函数用途: 从部分索引查询并核验活跃状态。
    def _candidates(self, kind: str, *, now: float = 0.0) -> list[GatewayIngressRecord]:
        with self._lock:
            if self.root is None:
                rows = sorted((r for r in self._volatile_records.values() if r.state != "completed"),
                              key=lambda r: (r.created_at, r.ingress_id))
                if kind == "due":
                    rows = [r for r in rows if r.next_attempt_at <= now
                            and (not r.claim_owner or r.claim_expires_at <= now)]
                return rows[:128]
            self._ensure_dirs()
            self._repair_intents()
            assert self._index is not None
            if self._index.get("legacy") == "1":
                self._scan_batch("records")
            result = []
            for ingress_id in self._index.candidates(kind, now=now):
                record = self._project_id(ingress_id)
                if record is not None and record.state != "completed":
                    if kind != "due" or (record.next_attempt_at <= now
                            and (not record.claim_owner or record.claim_expires_at <= now)):
                        result.append(record)
            return result

    # LLM: Records include terminal idempotency receipts so duplicate callbacks remain suppressed
    # across restarts until bounded retention pruning.
    # 函数用途: 返回所有有效入站记录，供恢复、测试和诊断读取。
    def records(self) -> list[GatewayIngressRecord]:
        with self._lock:
            if self.root is None:
                records = list(self._volatile_records.values())
            else:
                self._ensure_dirs()
                records = [
                    record
                    for path in sorted((self.root / "ingress" / "records").glob("*.json"))
                    if (record := self._read_record(path)) is not None
                ]
        return sorted(records, key=lambda item: (item.created_at, item.ingress_id))

    # LLM: Conflict evidence is a separate projection and cannot become executable ingress state.
    # 函数用途: 返回 same-id/different-body 的隔离记录，便于定向测试和运维诊断。
    def quarantine_records(self) -> list[dict[str, object]]:
        with self._lock:
            if self.root is None:
                return [dict(item) for item in self._volatile_quarantine]
            self._ensure_dirs()
            rows: list[dict[str, object]] = []
            for path in sorted((self.root / "ingress" / "quarantine").glob("*.json")):
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if isinstance(payload, dict):
                    rows.append(payload)
            return rows

    # LLM: 只按 expires 部分索引领取固定批次，删除前在原规范路径锁内重验真实七天窗口。
    # 函数用途: 清理过期终态与隔离证据，未完成责任永不自动删除。
    def prune_terminal_records(self, *, now: float | None = None) -> None:
        if self.root is None:
            return
        current_time = time.time() if now is None else float(now)
        cutoff = current_time - _TERMINAL_RETENTION_SECONDS
        with self._lock:
            self._ensure_dirs()
            assert self._index is not None
            for ingress_id in self._index.candidates("expires", now=current_time):
                path = self._record_path(ingress_id)
                with locked_json_path(path):
                    record = self._read_record(path) if path.exists() else None
                    if record is not None and record.state == "completed" and record.updated_at < cutoff:
                        self._index.begin(ingress_id)
                        path.unlink(missing_ok=True)
                        _sync_ingress_directory(path.parent)
                        record = None
                    self._index.settle(ingress_id, record, unreadable=path.exists() and record is None)
            for name in self._index.quarantine_expired(current_time):
                if re.fullmatch(r"[0-9a-f]{64}-[0-9a-f]{16}\.json", name) is None:
                    raise ValueError("invalid ingress quarantine projection key")
                path = self.root / "ingress" / "quarantine" / name
                with locked_json_path(path):
                    try:
                        if path.exists() and path.stat().st_mtime < cutoff:
                            path.unlink(missing_ok=True)
                        if not path.exists():
                            self._index.quarantine_drop(name)
                        else:
                            self._index.quarantine_put(name, path.stat().st_mtime + _TERMINAL_RETENTION_SECONDS)
                    except OSError as exc:
                        _LOGGER.warning("cannot prune ingress quarantine path=%s error=%s", path, exc)

    # LLM: intent 与已知记录按持久 keyset 审计；旧目录使用同一个流式游标续读，绝不每轮从头跳 N 项。
    # 函数用途: 每轮推进固定批次的责任恢复、规范审计和旧目录回填。
    def maintenance(self) -> None:
        if self.root is None:
            return
        with self._lock:
            self._ensure_dirs()
            self._repair_intents()
            assert self._index is not None
            for ingress_id in self._index.audit_ids():
                self._project_id(ingress_id)
            try:
                self._scan_batch("records")
                self._scan_batch("quarantine")
            except BaseException:
                self.close()
                raise

    # LLM: 原生发现每页已经关闭；兼容旧调用方的close不清持久cookie或恢复责任。
    # 函数用途: 清理无文件句柄的运行态提示，重启仍从持久位置继续。
    def close(self) -> None:
        with self._lock:
            self._scans.clear()
            self._scan_stamps.clear()

    # LLM: 单批最多128个真实目录项；内核cookie与本轮inode落盘，目录增删不重置尚未完成的旧积压。
    # 函数用途: 关闭每页目录句柄并提交可重启的发现位置，只有EOF才确认本轮迁移完成。
    def _scan_batch(self, kind: str) -> None:
        assert self.root is not None and self._index is not None
        directory = self.root / "ingress" / kind
        stamp = self._directory_stamp(directory)
        key = f"dir_{kind}"
        value = directory.stat()
        identity = f"{value.st_dev}:{value.st_ino}"
        cookie = self._index.get(f"cookie_{kind}")
        if self._index.get(f"identity_{kind}") != identity:
            cookie = ""
        if not cookie:
            audit_due = time.time() >= float(self._index.get(f"scan_after_{kind}", "0"))
            if stamp == self._index.get(key) and self._index.get("legacy") != "1" and not audit_due:
                return
            self._index.set(f"pass_stamp_{kind}", stamp)
        names, position, complete = directory_page(directory, cookie)
        for name in names:
            path = directory / name
            try:
                regular = stat.S_ISREG(path.lstat().st_mode)
            except FileNotFoundError:
                continue
            if not name.endswith(".json") or not regular:
                continue
            if kind == "records":
                if re.fullmatch(r"[0-9a-f]{64}\.json", name):
                    self._project_id(path.stem)
            else:
                if re.fullmatch(r"[0-9a-f]{64}-[0-9a-f]{16}\.json", name):
                    self._index.quarantine_put(name, path.stat().st_mtime + _TERMINAL_RETENTION_SECONDS)
        self._index.set(f"identity_{kind}", identity)
        self._index.set(f"cookie_{kind}", "" if complete else position)
        if complete:
            self._index.set(key, self._index.get(f"pass_stamp_{kind}", stamp))
            self._index.set(f"scan_after_{kind}", str(time.time() + 3600))
            if kind == "records":
                self._index.set("legacy", "0")

    # LLM: 目录戳仅决定是否需要发现新文件，绝不决定是否可执行消息。
    # 函数用途: 读取目录变化标记，不枚举文件。
    def _directory_stamp(self, directory: Path) -> str:
        value = directory.stat()
        return f"{value.st_dev}:{value.st_ino}:{value.st_mtime_ns}"

    # LLM: 不重放写入后像；只核当前规范文件，所以写前失败仍由原 lease/epoch 处理未知效果。
    # 函数用途: 逐批修复规范已发布但投影未确认的责任。
    def _repair_intents(self) -> None:
        assert self._index is not None
        for ingress_id in self._index.audit_ids(intents=True):
            self._project_id(ingress_id)

    # LLM: 修复与所有写者共用原跨进程路径锁；读取旧文件后不能无锁覆盖更新的投影。
    # 函数用途: 核读一个规范文件并修复其查找字段。
    def _project_id(self, ingress_id: str) -> GatewayIngressRecord | None:
        assert self._index is not None
        path = self._record_path(ingress_id)
        with locked_json_path(path):
            exists = path.exists()
            record = self._read_record(path) if exists else None
            self._index.settle(ingress_id, record, unreadable=exists and record is None)
            return record

    # LLM: intent 先提交；规范文件与投影之间任何错误都留下可核读责任，不把异常当成未发布证明。
    # 函数用途: 在已持规范路径锁时发布单条状态及其索引。
    def _write_indexed(self, path: Path, record: GatewayIngressRecord) -> None:
        assert self._index is not None
        self._index.begin(record.ingress_id)
        _write_private_json(path, record.to_dict())
        self._index.settle(record.ingress_id, record)
        self._index.set("dir_records", self._directory_stamp(path.parent))

    # LLM: 损坏记录和路径身份不符只记诊断并保留原文件，不把投影当作执行或删除许可。
    # 函数用途: 读取并校验单条入站记录，损坏时保留原文件供修复。
    def _read_record(self, path: Path) -> GatewayIngressRecord | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("gateway ingress JSON root must be an object")
            record = GatewayIngressRecord.from_dict(payload)
            if record.ingress_id != path.stem:
                raise ValueError("gateway ingress canonical path identity mismatch")
            return record
        except Exception as exc:
            _LOGGER.error("gateway ingress record unreadable path=%s error=%s", path, exc)
            return None

    # LLM: 隔离证据独立保存两份摘要及尝试正文，过期投影不改变首条规范记录。
    # 函数用途: 为同一 provider message 的冲突重放写入独立诊断证据。
    def _quarantine_conflict(
        self,
        current: GatewayIngressRecord,
        candidate: GatewayIngressRecord,
    ) -> None:
        evidence: dict[str, object] = {
            "schema_version": _SCHEMA_VERSION,
            "reason": "same_provider_message_id_different_body",
            "ingress_id": current.ingress_id,
            "channel": candidate.channel,
            "user_id": candidate.user_id,
            "conversation_id": candidate.conversation_id,
            "provider_message_id": candidate.provider_message_id,
            "current_payload_digest": current.canonical_payload_digest,
            "attempted_payload_digest": candidate.canonical_payload_digest,
            "attempted_content": candidate.content,
            "attempted_metadata": candidate.metadata,
            "quarantined_at": time.time(),
        }
        with self._lock:
            if self.root is None:
                if evidence not in self._volatile_quarantine:
                    self._volatile_quarantine.append(evidence)
                return
            self._ensure_dirs()
            suffix = candidate.canonical_payload_digest[:16]
            path = self.root / "ingress" / "quarantine" / f"{candidate.ingress_id}-{suffix}.json"
        with locked_json_path(path):
            _write_private_json(path, evidence)
            assert self._index is not None
            self._index.quarantine_put(path.name, path.stat().st_mtime + _TERMINAL_RETENTION_SECONDS)

    # LLM: 初始化不扫描旧记录；只记录是否存在迁移责任，正常登记不等待回填。
    # 函数用途: 创建入站目录并懒加载当前 spool 的私有投影。
    def _ensure_dirs(self) -> None:
        assert self.root is not None
        for path in (self.root / "ingress", self.root / "ingress" / "records", self.root / "ingress" / "quarantine"):
            if path.is_symlink():
                raise ValueError("ingress directory link rejected")
        legacy = (self.root / "ingress" / "records").exists()
        ensure_private_dir(self.root / "ingress" / "records")
        ensure_private_dir(self.root / "ingress" / "quarantine")
        if self._index is None:
            self._index = IngressIndex(self.root / "ingress", legacy=legacy)
            if not legacy:
                for kind in ("records", "quarantine"):
                    self._index.set(f"dir_{kind}", self._directory_stamp(self.root / "ingress" / kind))
                    self._index.set(f"scan_after_{kind}", str(time.time() + 3600))

    # LLM: 投影返回键也不可信，必须先核已冻结的SHA256格式，不能先拼路径再比较正文身份。
    # 函数用途: 在任何打开或锁操作之前校验入站键并返回规范路径。
    def _record_path(self, ingress_id: str) -> Path:
        assert self.root is not None
        if re.fullmatch(r"[0-9a-f]{64}", ingress_id) is None:
            raise ValueError("invalid ingress projection key")
        return self.root / "ingress" / "records" / f"{ingress_id}.json"


# LLM: This is the only adapter background thread. It advances ingress IO and then reuses the
# existing PendingGatewayReply state machine in the same loop; the reply worker must not be started
# separately by ChannelManager.
# 类用途: 用一个线程依次恢复入站提交、输入三态、结果轮询、占位清理和最终回复。
class GatewayAdapterDeliveryWorker:
    def __init__(
        self,
        store: GatewayIngressStore,
        *,
        reply_worker: GatewayReplyDeliveryWorker,
        prepare_payload: Callable[[GatewayIngressRecord], dict[str, object]],
        submit_payload: Callable[[GatewayIngressRecord], dict[str, object]],
        advance_submission: Callable[[GatewayIngressRecord], GatewayIngressAdvance],
        handoff_reply: Callable[[GatewayIngressRecord], None],
        poll_interval: float = 1.0,
        lease: GatewayClaimLeaseConfig | None = None,
    ) -> None:
        lease = lease or GatewayClaimLeaseConfig()
        self.store = store
        self.reply_worker = reply_worker
        self._prepare_payload = prepare_payload
        self._submit_payload = submit_payload
        self._advance_submission = advance_submission
        self._handoff_reply = handoff_reply
        self._poll_interval = max(0.01, float(poll_interval))
        self._claim_owner = lease.owner or (
            f"adapter-ingress:{os.getpid()}:{uuid.uuid4().hex}"
        )
        self._claim_ttl = lease.ttl_seconds
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._state_lock = threading.Lock()
        self._run_lock = threading.Lock()
        self._thread: threading.Thread | None = None

    # LLM: Enqueue performs only local durable IO. Provider media, Gateway POST, and channel IO are
    # deferred until after route_message returns and all store locks are released.
    # 函数用途: 登记外部消息并唤醒后台线程，返回是否首次创建或发生冲突隔离。
    def enqueue(
        self,
        record: GatewayIngressRecord,
    ) -> tuple[GatewayIngressRecord | None, bool, bool]:
        result = self.store.put_if_absent(record)
        self._wake.set()
        return result

    # LLM: Startup resumes both pre-POST ingress rows and existing post-POST reply watchers in one
    # thread, after pruning only expired terminal receipts.
    # 函数用途: 启动或唤醒唯一 adapter 投递线程。
    def start(self) -> None:
        with self._state_lock:
            if self._thread is not None and self._thread.is_alive():
                self._wake.set()
                return
            self.store.prune_terminal_records()
            self.reply_worker.store.prune_sent_receipts()
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="gateway-adapter-delivery",
                daemon=True,
            )
            self._thread.start()

    # LLM: 停机不删除未完责任；线程确已结束后显式释放流式迁移游标。
    # 函数用途: 停止后台线程并保留所有未完成状态。
    def stop(self, timeout: float = 6.0) -> None:
        self._stop.set()
        self._wake.set()
        with self._state_lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, timeout))
        if thread is None or not thread.is_alive():
            self.store.close()

    # LLM: 同一入口完成定批维护、到期调度与既有回复轮询；所有运输回调都在存储锁外运行。
    # 函数用途: 推进当前可运行的入站记录，再轮询一次既有三态回复 watcher。
    def run_once(self) -> int:
        if not self._run_lock.acquire(blocking=False):
            return 0
        try:
            now = time.time()
            self.store.maintenance()
            self.store.prune_terminal_records(now=now)
            ingresses = sum(
                self._process_record(record, now) for record in self.store.due(now)
            )
            return ingresses + self.reply_worker.run_once()
        finally:
            self._run_lock.release()

    # LLM: A record may cross all successful stages in one wakeup. Each external call precedes a
    # CAS, so response loss leaves the prior durable stage available for exact-body replay.
    # 函数用途: 按状态推进单条记录；失败只退避，不把瞬时错误写成用户最终消息。
    def _process_record(self, record: GatewayIngressRecord, now: float) -> int:
        if self._stop.is_set() or record.next_attempt_at > now:
            return 0
        current = record
        for _step in range(5):
            claimed = self.store.claim(
                current,
                owner=self._claim_owner,
                now=time.time(),
                ttl=self._claim_ttl,
            )
            if claimed is None:
                return 0
            try:
                replacement = self._next_record(claimed)
            except Exception as exc:
                _LOGGER.warning(
                    "gateway ingress stage failed ingress_id=%s state=%s error=%s",
                    claimed.ingress_id,
                    claimed.state,
                    exc,
                )
                self.store.defer_claimed(
                    claimed,
                    owner=self._claim_owner,
                    epoch=claimed.claim_epoch,
                )
                return 0
            if replacement is None:
                self.store.release_claim(
                    claimed,
                    owner=self._claim_owner,
                    epoch=claimed.claim_epoch,
                )
                return 1 if claimed.state == "completed" else 0
            committed = self.store.commit_claim(
                claimed,
                replacement,
                owner=self._claim_owner,
                epoch=claimed.claim_epoch,
                release=True,
            )
            if committed is None:
                return 0
            current = committed
            if current.state == "completed":
                return 1
        return 0

    # LLM: prepare确定的媒体错误只冻结原input_error协议回执；暂态/权限/路径错误仍原样退避，所有阶段沿原CAS。
    # 函数用途: 推进原入站状态，坏图片不提交模型任务，也不在prepared无限重试或丢图继续文字。
    def _next_record(
        self,
        record: GatewayIngressRecord,
    ) -> GatewayIngressRecord | None:
        now = time.time()
        common = {
            "updated_at": now,
            "processing_attempts": 0,
            "next_attempt_at": 0.0,
        }
        if record.state == "prepared":
            try:
                payload = self._prepare_payload(record)
            except InboundMediaInputError as exc:
                return replace(record, state='submitted', submission={'ok': False, 'kind': 'input_error',
                    'error_code': exc.error_code, 'message': str(exc)}, **common)
            return replace(
                record,
                state="payload_ready",
                gateway_payload=payload,
                **common,
            )
        if record.state == "payload_ready":
            submission = self._submit_payload(record)
            return replace(
                record,
                state="submitted",
                submission=submission,
                **common,
            )
        if record.state == "submitted":
            advance = self._advance_submission(record)
            return replace(
                record,
                state=advance.state,
                progress_handle=advance.progress_handle,
                **common,
            )
        if record.state == "placeholder_ready":
            self._handoff_reply(record)
            return replace(record, state="completed", **common)
        if record.state == "completed":
            return None
        raise ValueError(f"unsupported gateway ingress state: {record.state}")

    # LLM: 普通 IO 错误只结束本轮，未过期租约保护未知效果；任何线程退出都释放旧目录流。
    # 函数用途: 持续运行单次推进，直到生命周期请求停止；单次队列 IO 失败后在原可中断等待后重试。
    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    self.run_once()
                except OSError as exc:
                    _LOGGER.warning("gateway adapter delivery pass failed; retrying error=%s", exc)
                self._wake.wait(self._poll_interval)
                self._wake.clear()
        finally:
            self.store.close()


# LLM: Immutable ingress identity includes authenticated route and canonical body digest; state,
# prepared payload, submission, placeholder, and retry timing are the only mutable fields.
# 函数用途: 校验 CAS 前后仍是同一条外部消息。
def _ensure_same_ingress_identity(
    current: GatewayIngressRecord,
    candidate: GatewayIngressRecord,
) -> None:
    current_identity = (
        current.ingress_id,
        current.channel,
        current.user_id,
        current.conversation_id,
        current.provider_message_id,
        current.canonical_payload_digest,
        current.content,
        current.metadata,
    )
    candidate_identity = (
        candidate.ingress_id,
        candidate.channel,
        candidate.user_id,
        candidate.conversation_id,
        candidate.provider_message_id,
        candidate.canonical_payload_digest,
        candidate.content,
        candidate.metadata,
    )
    if current_identity != candidate_identity:
        raise ValueError(f"gateway ingress identity conflict for id={candidate.ingress_id}")


# LLM: Duplicate callbacks are equivalent only when both immutable identity and canonical digest
# match; equality of provider message id alone is insufficient.
# 函数用途: 判断重放是否与首次入站完全一致。
def _same_ingress_body(
    current: GatewayIngressRecord,
    candidate: GatewayIngressRecord,
) -> bool:
    try:
        _ensure_same_ingress_identity(current, candidate)
    except ValueError:
        return False
    return True


# LLM: 私有文件原子发布并同步父目录，只有耐久成功后才能确认写前责任标记。
# 函数用途: 在调用方已持路径锁时原子写入私有 JSON 文件。
def _write_private_json(path: Path, payload: dict[str, object]) -> None:
    temp_path = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    try:
        with temp_path.open("wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, path)
        _sync_ingress_directory(path.parent)
    finally:
        temp_path.unlink(missing_ok=True)


# LLM: POSIX 先耐久目录项再确认 intent；无 O_DIRECTORY 平台沿原文件同步语义，不假称目录同步。
# 函数用途: 同步规范文件发布或删除后的父目录。
def _sync_ingress_directory(directory: Path) -> None:
    if hasattr(os, "O_DIRECTORY"):
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


__all__ = [
    "GatewayAdapterDeliveryWorker",
    "GatewayIngressAdvance",
    "GatewayIngressRecord",
    "GatewayIngressStore",
    "build_gateway_ingress_record",
    "canonical_ingress_payload",
    "canonical_payload_digest",
]
