from __future__ import annotations

"""Memory Curator 每 owner 的 durable request、cursor 和 lease 仓库。"""

# LLM: Gateway 内存 tick 不是运行权威；所有触发、租约和成功游标必须先后写同一个 state.json。
# 模块用途: 在多线程/多进程下保证同一 owner 同时最多一个 Curator 提交。

import hashlib
import json
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

# LLM: 隔离失败退避冷却: 同 error_class 在此窗口内跳过重复隔离尝试(复用上次失败
# 审计, 不新增行), 期满才再试一条新审计——防 quarantine 长期失败时 run_log 无限刷屏。
_QUARANTINE_BACKOFF_SECONDS = 300


# LLM: 损坏 state 不能被空默认覆盖，否则会丢增量游标并全量重放历史。
# 类用途: 向维护器和 CLI 暴露稳定的 Curator state 损坏错误。
class CuratorStateCorruptError(RuntimeError):
    # LLM: error_class 是结构化分类(unreadable/schema_invalid/lease_*/quarantined),
    # 调用方据此隔离取证,禁止按 message 文本做自然语言匹配。
    def __init__(self, message: str, *, error_class: str = "unreadable") -> None:
        super().__init__(message)
        self.error_class = error_class


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
        # LLM: 损坏隔离哨兵存在时 fail-closed：load/request/acquire/commit 全部拒行,
        # 绝不写空 state、不接管 lease、不推进 cursor,等待人工恢复。
        if self._sentinel_path().exists():
            raise CuratorStateCorruptError(
                "memory curator state is quarantined, awaiting manual recovery",
                error_class="quarantined",
            )
        report = read_json_object_report(self.path, context="memory_curator.state")
        if report.load_error:
            raise CuratorStateCorruptError(
                "memory curator state is unreadable", error_class="unreadable"
            )
        try:
            return MemoryCuratorState.from_dict(report.payload)
        except ValueError as exc:
            raise CuratorStateCorruptError(
                "memory curator state failed schema validation", error_class="schema_invalid"
            ) from exc

    # LLM: 损坏 state 被原子 rename 到 quarantine/ 并写哨兵; 原文件保留为不可变副本
    # (含 cursor/lease/pending), 人工恢复可从副本取回权威游标, 绝不自动从头重跑。
    # 顺序: 先写哨兵再 rename —— 哨兵写失败时原文件未动; rename 失败时哨兵已存在,
    # 后续调用凭哨兵补做隔离, 状态机闭合, 无「已隔离但文件未移」的死角。
    # 函数用途: 原子隔离损坏 state.json 并登记哨兵; 已隔离返回 None(不重复执行)。
    def quarantine_corrupt(
        self,
        *,
        error_class: str,
        now: str = "",
    ) -> dict[str, object] | None:
        backoff = self._backoff_path()
        try:
            result = self._quarantine_corrupt_unlocked(error_class=error_class, now=now)
            backoff.unlink(missing_ok=True)  # 隔离成功: 清除失败退避(环境已恢复)
            return result
        except Exception:
            # 隔离写失败: 记录退避事实, 冷却窗口内不再重复尝试(防 run_log 无限刷屏)。
            try:
                attempt_count = 1
                if backoff.exists():
                    try:
                        prior = json.loads(backoff.read_text(encoding="utf-8"))
                        attempt_count = int(prior.get("attempt_count") or 0) + 1
                    except (OSError, UnicodeError, ValueError, TypeError):
                        attempt_count = 1
                self.record_quarantine_backoff(
                    error_class, now=now, attempt_count=attempt_count
                )
            except Exception:  # noqa: BLE001 退避留痕失败不掩盖原始隔离错误
                pass
            raise

    def _quarantine_corrupt_unlocked(
        self,
        *,
        error_class: str,
        now: str = "",
    ) -> dict[str, object] | None:
        sentinel = self._sentinel_path()
        if sentinel.exists() and not self.path.exists():
            return None  # 已完成隔离
        if not sentinel.exists() and not self.path.exists():
            return None  # 缺失初始状态, 非损坏场景
        if sentinel.exists() and self.path.exists():
            # 哨兵先于 rename 写入、rename 未完成: 凭哨兵记录的路径补做隔离。
            payload = _read_sentinel_payload(sentinel)
            target = self.path.parent / str(payload.get("quarantine_path") or "")
            if target == self.path or self.path.parent not in target.parents:
                raise CuratorStateCorruptError(
                    "memory curator quarantine marker has an invalid target",
                    error_class="quarantine_marker_invalid",
                )
            if target.exists():
                raise CuratorStateCorruptError(
                    "memory curator quarantine target already exists",
                    error_class="quarantine_target_exists",
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(self.path, target)
            return payload
        quarantined_at = normalize_iso_time(now, default=utc_now_iso(), allow_empty=False)
        original_sha256 = _sha256_file(self.path)
        stamp = quarantined_at.replace(":", "-").replace("+00:00", "Z").replace("+0000", "Z")
        quarantine_dir = self.path.parent / "quarantine"
        target = quarantine_dir / f"state-{stamp}-{original_sha256[:8]}.json"
        payload: dict[str, object] = {
            "schema": "memory-curator-corrupt-sentinel.v1",
            "original_path": self.path.name,
            "quarantine_path": str(target.relative_to(self.path.parent)),
            "original_sha256": original_sha256,
            "quarantined_at": quarantined_at,
            "error_class": error_class,
            "recovery": "manual",
        }
        write_json_file_atomic_unlocked(sentinel, payload, sort_keys=True)
        quarantine_dir.mkdir(parents=True, exist_ok=True)
        os.replace(self.path, target)
        return payload

    # LLM: 哨兵是唯一「已隔离」权威; 重复 run 凭它幂等, 不重复隔离、不伪造自愈。
    # 函数用途: 返回损坏隔离哨兵元数据; 未隔离返回 None。
    def corrupt_evidence(self) -> dict[str, object] | None:
        sentinel = self._sentinel_path()
        if not sentinel.exists():
            return None
        return _read_sentinel_payload(sentinel)

    # LLM: 恢复是运维显式动作: 先原子写回重建 state, 再删哨兵(最后一步)。
    # 删哨兵失败时哨兵仍在 → 下次恢复幂等重试; 哨兵已不在 → 拒绝重复执行。
    # 函数用途: 在人工恢复校验通过后, 原子落回恢复 state 并移除隔离哨兵。
    def restore_after_recovery(
        self,
        state: MemoryCuratorState,
        *,
        sentinel_evidence: dict[str, object],
    ) -> MemoryCuratorState:
        with locked_json_path(self.path):
            if not self._sentinel_path().exists():
                raise CuratorStateCorruptError(
                    "no quarantine marker: nothing to recover",
                    error_class="recover_noop",
                )
            current = _read_sentinel_payload(self._sentinel_path())
            if current.get("original_sha256") != sentinel_evidence.get("original_sha256"):
                raise CuratorStateCorruptError(
                    "quarantine evidence changed during recovery",
                    error_class="recover_evidence_changed",
                )
            self._write_unlocked(state)
            self._sentinel_path().unlink(missing_ok=False)
            return state

    # LLM: 隔离写失败不能无限刷 run_log: marker 存在即进入冷却窗口(与 error_class 无关),
    # 复用上一次失败审计(append 幂等, 不新增行), 冷却期满才再试一条新审计。
    # error_class 不参与匹配: 隔离失败(如 quarantine 目录被占)后哨兵可能半写残留,
    # 后续 tick 的损坏检测点漂移(load 报 unreadable → 哨兵检查报 quarantined),
    # 同 error_class 匹配会把同一故障的镜像误判为新故障, 每 tick 重试 → 无限刷屏。
    # 函数用途: 判断当前是否仍在隔离失败退避窗口内(环境级冷却)。
    def quarantine_backoff_eligible(self, error_class: str, now: str = "") -> bool:
        marker = self._backoff_path()
        if not marker.exists():
            return True
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
            last = datetime.fromisoformat(str(payload.get("last_attempt_at") or ""))
        except (OSError, UnicodeError, ValueError, TypeError):
            return True  # marker 损坏/缺失: 不因退避状态阻断隔离尝试本身
        if last.tzinfo is None:
            return True
        current = datetime.fromisoformat(
            normalize_iso_time(now, default=utc_now_iso(), allow_empty=False)
        )
        elapsed = current.astimezone(timezone.utc) - last.astimezone(timezone.utc)
        return elapsed.total_seconds() >= _QUARANTINE_BACKOFF_SECONDS

    # LLM: 退避复用只读上次失败事实, 不新建状态; 解析失败视为无退避证据。
    # 函数用途: 读取隔离失败退避 marker 内容(供复用上次失败审计)。
    def quarantine_backoff_evidence(self) -> dict[str, object]:
        marker = self._backoff_path()
        if not marker.exists():
            return {}
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    # LLM: 退避 marker 只记结构化失败事实; 隔离成功后必须清除, 否则冷却窗口
    # 会把后续真实损坏误判为退避(隔离成功即证明环境已恢复)。
    # 函数用途: 记录隔离失败的最后尝试时间/次数/分类/一次性审计 run_id。
    def record_quarantine_backoff(
        self,
        error_class: str,
        *,
        now: str = "",
        attempt_count: int = 1,
        run_id: str = "",
    ) -> None:
        payload = {
            "schema": "memory-curator-quarantine-backoff.v1",
            "error_class": str(error_class or "unreadable"),
            "last_attempt_at": normalize_iso_time(
                now, default=utc_now_iso(), allow_empty=False
            ),
            "attempt_count": max(1, int(attempt_count)),
            "run_id": str(run_id or "").strip(),
        }
        write_json_file_atomic_unlocked(self._backoff_path(), payload, sort_keys=True)

    # 函数用途: 定位损坏隔离哨兵路径(与 state.json 同目录)。
    def _sentinel_path(self) -> Path:
        return self.path.with_name("state.corrupt")

    # 函数用途: 定位隔离失败退避 marker 路径(与 state.json 同目录)。
    def _backoff_path(self) -> Path:
        return self.path.with_name("state.quarantine-backoff")

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


# LLM: 隔离哨兵读取失败同样 fail-closed: 无法证明隔离状态时不得假装已隔离或从头运行。
# 函数用途: 读取损坏隔离哨兵 JSON 元数据。
def _read_sentinel_payload(sentinel: Path) -> dict[str, object]:
    report = read_json_object_report(sentinel, context="memory_curator.corrupt_marker")
    if report.load_error:
        raise CuratorStateCorruptError(
            "memory curator quarantine marker is unreadable",
            error_class="quarantine_marker_unreadable",
        )
    payload = report.payload
    if not isinstance(payload, dict):
        raise CuratorStateCorruptError(
            "memory curator quarantine marker is not an object",
            error_class="quarantine_marker_unreadable",
        )
    return payload


# LLM: 原文件 hash 用于隔离副本命名与人工恢复比对, 不读取/保存正文。
# 函数用途: 计算 state.json 的 sha256 摘要。
def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


# LLM: lease 过期只按结构化 expires_at 判断；解析失败视为损坏而非永远占用。
# 函数用途: 判断当前 lease 是否仍有效。
def _lease_live(lease: dict[str, object], *, now: datetime) -> bool:
    if not lease:
        return False
    try:
        expires = datetime.fromisoformat(str(lease.get("expires_at") or ""))
    except ValueError as exc:
        raise CuratorStateCorruptError(
            "memory curator lease has invalid expires_at",
            error_class="lease_invalid_expires_at",
        ) from exc
    if expires.tzinfo is None:
        raise CuratorStateCorruptError(
            "memory curator lease expires_at lacks timezone",
            error_class="lease_naive_expires_at",
        )
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
