from __future__ import annotations

"""Memory Curator 每 owner 的 durable request、cursor 和 lease 仓库。"""

# LLM: Gateway 内存 tick 不是运行权威；所有触发、租约和成功游标必须先后写同一个 state.json。
# 模块用途: 在多线程/多进程下保证同一 owner 同时最多一个 Curator 提交。

import os
import socket
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..common.json_io import (
    locked_json_path,
    read_json_object_report,
    write_json_file_atomic_unlocked,
)
from .candidate_models import normalize_iso_time, utc_now_iso
from .curator_models import (
    CURATOR_TRIGGER_REASONS,
    MemoryCuratorState,
)


# LLM: 损坏 state 不能被空默认覆盖，否则会丢增量游标并全量重放历史。
# 类用途: 向维护器和 CLI 暴露稳定的 Curator state 损坏错误。
class CuratorStateCorruptError(RuntimeError):
    pass


# LLM: lease 丢失表示另一进程已接管；旧运行不得继续提交或推进 cursor。
# 类用途: 区分 provider/Schema 失败和并发接管。
class CuratorLeaseLostError(RuntimeError):
    pass


# LLM: A single immutable object replaces a wide commit signature and prevents callers from
# accidentally mixing cursor/count fields from different runs.
# 类用途: 表示 Daily、Candidate 和 run audit 已准备完成后要提交的成功状态变化。
@dataclass(frozen=True)
class CuratorSuccessCommit:
    lease_id: str
    run_id: str
    reason: str
    per_thread_cursors: dict[str, str]
    last_processed_audit_event_id: str
    processed_messages: int
    processed_audit_events: int
    candidate_count: int
    daily_event_count: int
    last_daily_finalize_date: str = ""
    committed_at: str = ""


# LLM: StateStore 只管理机器状态，不读取对话、不调用模型、不写候选或 daily。
# 类用途: 对 memory/curator/state.json 做原子读改写。
class MemoryCuratorStateStore:
    # LLM: 构造器只绑定规范 state 路径；缺失状态的初始化由 load 合同统一处理。
    # 函数用途: 初始化当前 owner 的 Curator durable state 仓库。
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # LLM: 读取缺失文件返回初始 state；已存在但损坏必须 fail closed。
    # 函数用途: 获取当前 Curator 状态快照。
    def load(self) -> MemoryCuratorState:
        with locked_json_path(self.path):
            return self._load_unlocked()

    # LLM: Every request advances its reason generation so a same-reason trigger arriving under
    # an active lease cannot be consumed by the older run; output idempotency remains ledger-based.
    # 函数用途: 耐久登记 pre-compact/close/turn/admin 等请求，并区分租约期间到达的同类新触发。
    def request(self, reason: str, *, requested_at: str = "") -> MemoryCuratorState:
        normalized_reason = _reason(reason)
        now = normalize_iso_time(requested_at, default=utc_now_iso(), allow_empty=False)
        with locked_json_path(self.path):
            current = self._load_unlocked()
            reasons = list(current.pending_reasons)
            if normalized_reason not in reasons:
                reasons.append(normalized_reason)
            generations = dict(current.pending_reason_generations)
            generations[normalized_reason] = generations.get(normalized_reason, 0) + 1
            updated = replace(
                current,
                pending_reasons=reasons,
                pending_reason_generations=generations,
                pending_requested_at=now,
            )
            self._write_unlocked(updated)
            return updated

    # LLM: The lease snapshots the handled reason generation. A later request increments the
    # durable generation and therefore survives this run's successful commit.
    # 函数用途: 原子获取后台策展租约并固定本次处理的请求代次，冲突时返回 None。
    def acquire(
        self,
        *,
        reason: str,
        config_revision: str,
        lease_seconds: int,
        now: datetime | None = None,
    ) -> tuple[MemoryCuratorState, dict[str, object]] | None:
        normalized_reason = _reason(reason)
        current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        now_iso = current_time.isoformat()
        with locked_json_path(self.path):
            state = self._load_unlocked()
            if _lease_live(state.active_lease, now=current_time):
                return None
            pending = list(state.pending_reasons)
            generations = dict(state.pending_reason_generations)
            if normalized_reason not in pending:
                pending.append(normalized_reason)
                generations[normalized_reason] = generations.get(normalized_reason, 0) + 1
            elif generations.get(normalized_reason, 0) <= 0:
                # Existing v1 state may contain a pending reason without a generation.
                generations[normalized_reason] = 1
            reason_generation = generations[normalized_reason]
            run_id = "memory-curator-run-" + uuid.uuid4().hex
            recovery = _expired_lease_recovery(state.active_lease, now=current_time)
            lease = {
                "lease_id": "memory-curator-lease-" + uuid.uuid4().hex,
                "run_id": run_id,
                "reason": normalized_reason,
                "reason_generation": reason_generation,
                "acquired_at": now_iso,
                "expires_at": (current_time + timedelta(seconds=max(30, lease_seconds))).isoformat(),
                "pid": os.getpid(),
                "host": socket.gethostname(),
                "recovery": recovery,
            }
            updated = replace(
                state,
                last_run_at=now_iso,
                active_lease=lease,
                config_revision=str(config_revision or "").strip(),
                pending_reasons=pending,
                pending_reason_generations=generations,
                pending_requested_at=state.pending_requested_at or now_iso,
            )
            self._write_unlocked(updated)
            return updated, lease

    # LLM: 每个多文件提交前必须重新核对 lease_id，不能只相信启动时的内存对象。
    # 函数用途: 断言当前运行仍拥有 owner 策展租约。
    def assert_lease(self, lease_id: str) -> MemoryCuratorState:
        state = self.load()
        if str(state.active_lease.get("lease_id") or "") != str(lease_id or ""):
            raise CuratorLeaseLostError("memory curator lease is no longer owned by this run")
        return state

    # LLM: 只有 daily/candidate/run audit 全部成功后调用；这里是 cursor 唯一推进点。
    # 函数用途: 原子提交成功游标、累计计数并释放 lease。
    def commit_success(self, commit: CuratorSuccessCommit) -> MemoryCuratorState:
        with locked_json_path(self.path):
            return self._commit_success_unlocked(commit)

    # LLM: Only the multi-file committer may call this while it already holds the state path
    # lock; ordinary callers use commit_success to preserve locking.
    # 函数用途: 在外层事务锁内写入成功 state，作为整个 Curator 批次的最终提交标记。
    def _commit_success_unlocked(
        self,
        commit: CuratorSuccessCommit,
    ) -> MemoryCuratorState:
        current = self._load_unlocked()
        updated = build_success_state(current, commit)
        self._write_unlocked(updated)
        return updated

    # LLM: 失败释放 lease 但保留 pending reasons 和旧 cursor，保证之后安全重试不漏经历。
    # 函数用途: 记录稳定失败码和失败时间。
    def commit_failure(
        self,
        *,
        lease_id: str,
        failure_code: str,
        now: str = "",
    ) -> MemoryCuratorState:
        failed_at = normalize_iso_time(now, default=utc_now_iso(), allow_empty=False)
        with locked_json_path(self.path):
            current = self._load_unlocked()
            _require_lease(current, lease_id)
            updated = replace(
                current,
                last_failure_at=failed_at,
                last_failure_code=str(failure_code or "CURATOR_FAILED").strip(),
                active_lease={},
            )
            self._write_unlocked(updated)
            return updated

    # LLM: read hook 便于测试注入，生产读取仍走同一严格 parser。
    # 函数用途: 在持锁临界区读取 state。
    def _load_unlocked(self) -> MemoryCuratorState:
        report = read_json_object_report(self.path, context="memory_curator.state")
        if report.load_error:
            raise CuratorStateCorruptError("memory curator state is unreadable")
        try:
            return MemoryCuratorState.from_dict(report.payload)
        except ValueError as exc:
            raise CuratorStateCorruptError("memory curator state failed schema validation") from exc

    # LLM: 所有 state mutation 都用原子 replace，禁止裸 write_text 截断游标文件。
    # 函数用途: 在已持锁状态写回 state。
    def _write_unlocked(self, state: MemoryCuratorState) -> None:
        write_json_file_atomic_unlocked(self.path, state.to_dict(), sort_keys=True)


# LLM: trigger reason 是机器枚举，未知文字不能创建隐藏调度分支。
# 函数用途: 校验并规范化触发原因。
def _reason(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in CURATOR_TRIGGER_REASONS:
        raise ValueError(f"unsupported memory curator trigger reason: {normalized or '-'}")
    return normalized


# LLM: lease 过期只按结构化 expires_at 判断；解析失败视为损坏而非永远占用。
# 函数用途: 判断当前 lease 是否仍有效。
def _lease_live(lease: dict[str, object], *, now: datetime) -> bool:
    if not lease:
        return False
    try:
        expires = datetime.fromisoformat(str(lease.get("expires_at") or ""))
    except ValueError as exc:
        raise CuratorStateCorruptError("memory curator lease has invalid expires_at") from exc
    if expires.tzinfo is None:
        raise CuratorStateCorruptError("memory curator lease expires_at lacks timezone")
    return expires.astimezone(timezone.utc) > now.astimezone(timezone.utc)


# LLM: commit 只认精确 lease_id，run_id/reason 相似不能授权旧运行推进游标。
# 函数用途: 校验一次 state commit 的租约归属。
def _require_lease(state: MemoryCuratorState, lease_id: str) -> None:
    expected = str(state.active_lease.get("lease_id") or "")
    if not expected or expected != str(lease_id or ""):
        raise CuratorLeaseLostError("memory curator commit does not own the active lease")


# LLM: The success projection consumes only the leased reason generation; both distinct reasons
# and a newer request for the same reason remain durable for the next maintenance tick.
# 函数用途: 校验租约身份、推进成功游标，并只移除本次确实处理过的触发代次。
def build_success_state(
    current: MemoryCuratorState,
    commit: CuratorSuccessCommit,
) -> MemoryCuratorState:
    _require_lease(current, commit.lease_id)
    lease_run_id = str(current.active_lease.get("run_id") or "")
    if not commit.run_id or lease_run_id != str(commit.run_id):
        raise CuratorLeaseLostError("memory curator run_id does not own the active lease")
    handled_reason = _reason(commit.reason)
    lease_reason = str(current.active_lease.get("reason") or "")
    if lease_reason != handled_reason:
        raise CuratorLeaseLostError("memory curator reason does not own the active lease")
    try:
        lease_generation = int(current.active_lease.get("reason_generation") or 0)
    except (TypeError, ValueError) as exc:
        raise CuratorLeaseLostError("memory curator lease has invalid reason generation") from exc
    current_generation = current.pending_reason_generations.get(handled_reason, 0)
    if lease_generation <= 0 or current_generation < lease_generation:
        raise CuratorLeaseLostError("memory curator reason generation does not own the active lease")
    preserve_newer_same_reason = current_generation > lease_generation
    remaining_reasons = [
        reason
        for reason in current.pending_reasons
        if reason != handled_reason or preserve_newer_same_reason
    ]
    committed_at = normalize_iso_time(
        commit.committed_at,
        default=utc_now_iso(),
        allow_empty=False,
    )
    cursors = {
        str(key): str(value)
        for key, value in commit.per_thread_cursors.items()
        if key and value
    }
    cursor_values = list(cursors.values())
    return replace(
        current,
        last_processed_message_id=(
            cursor_values[-1] if cursor_values else current.last_processed_message_id
        ),
        last_processed_audit_event_id=(
            str(commit.last_processed_audit_event_id or "").strip()
            or current.last_processed_audit_event_id
        ),
        per_thread_cursors=cursors,
        last_success_at=committed_at,
        last_failure_code="",
        active_lease={},
        processed_count=current.processed_count
        + max(0, int(commit.processed_messages))
        + max(0, int(commit.processed_audit_events)),
        candidate_count=current.candidate_count + max(0, int(commit.candidate_count)),
        daily_event_count=current.daily_event_count + max(0, int(commit.daily_event_count)),
        pending_reasons=remaining_reasons,
        pending_requested_at=(current.pending_requested_at if remaining_reasons else ""),
        last_daily_finalize_date=(
            commit.last_daily_finalize_date or current.last_daily_finalize_date
        ),
        last_committed_run_id=commit.run_id,
    )


# LLM: Expired-lease recovery records identifiers and timestamps only; provider errors and user
# content are never copied into state or run audit.
# 函数用途: 为新 lease 生成结构化的上次崩溃接管来源。
def _expired_lease_recovery(
    lease: dict[str, object],
    *,
    now: datetime,
) -> dict[str, object]:
    if not lease:
        return {}
    if _lease_live(lease, now=now):
        return {}
    return {
        "kind": "expired_lease",
        "previous_run_id": str(lease.get("run_id") or ""),
        "previous_lease_id": str(lease.get("lease_id") or ""),
        "previous_expires_at": str(lease.get("expires_at") or ""),
        "recovered_at": now.astimezone(timezone.utc).isoformat(),
    }


__all__ = [
    "CuratorLeaseLostError",
    "CuratorSuccessCommit",
    "CuratorStateCorruptError",
    "MemoryCuratorStateStore",
    "build_success_state",
]
