from __future__ import annotations

"""Memory Curator 的可恢复多文件整批提交。"""

# LLM: Daily, candidates, run audit, and state are committed under their canonical file locks;
# state is always written last as the durable commit marker and a journal enables crash rollback.
# 模块用途: 为 Curator 提供整批校验、配额检查、备份、提交、异常回滚和进程重启恢复。

import hashlib
import json
import shutil
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..common.json_io import (
    locked_json_path,
    read_json_object_report,
    write_json_file_atomic,
    write_text_file_atomic,
    write_text_file_atomic_unlocked,
)
from ..user_space.owner_quota import OwnerQuotaAdmission, OwnerQuotaChange
from .candidate_models import CandidateObservation, MemoryCandidate, utc_now_iso
from .candidates import CandidateService, merge_candidate_observations
from .curator_run_log import (
    CuratorRunLog,
    CuratorRunRecord,
    load_run_records_unlocked,
    merge_run_record,
    run_log_path,
    run_records_text,
)
from .curator_state import CuratorSuccessCommit, MemoryCuratorStateStore, build_success_state
from .daily import (
    DailyMemoryEvent,
    DailyMemoryStore,
    _load_daily_unlocked,
    daily_memory_path,
    merge_daily_events,
)

CURATOR_TRANSACTION_SCHEMA_VERSION = "my-agent.memory-curator-transaction.v1"


# LLM: One object binds all four projections to the same run/lease and prevents a caller from
# committing a state cursor for a different extracted batch.
# 类用途: 表示一次已经通过 Schema 与证据校验的 Curator 整批提交请求。
@dataclass(frozen=True)
class CuratorBatchCommit:
    run_record: CuratorRunRecord
    state_commit: CuratorSuccessCommit
    daily_events: tuple[DailyMemoryEvent, ...] = ()
    candidate_observations: tuple[CandidateObservation, ...] = ()


# LLM: Result returns stable IDs/counts only; committed candidate bodies remain in the sole
# CandidateService authority.
# 类用途: 返回一次整批提交真正落盘的 Daily/Candidate 结果摘要。
@dataclass(frozen=True)
class CuratorBatchCommitResult:
    daily_events: tuple[DailyMemoryEvent, ...]
    candidates: tuple[MemoryCandidate, ...]


# LLM: Recovery failures are distinct from provider/schema failures because operators must not
# keep retrying a potentially half-written batch blindly.
# 类用途: 表示 Curator 事务清单或回滚数据无法安全恢复。
class CuratorCommitRecoveryError(RuntimeError):
    pass


# LLM: A normal disk/quota/serialization failure is reported separately from an unrecoverable
# rollback problem; callers can safely retry only after the batch preimage has been restored.
# 类用途: 表示一次整批提交失败但事务已成功回滚。
class CuratorBatchCommitError(RuntimeError):
    pass


# LLM: The committer owns no model or promotion capability; it only materializes a validated
# batch into the four canonical ledgers.
# 类用途: 协调 Daily、Candidate、run audit 与 state 的可恢复多文件事务。
class CuratorBatchCommitter:
    # LLM: 构造器要求四个规范仓库共享同一 owner memory 根，并创建仅供恢复的事务目录。
    # 函数用途: 绑定 Curator 整批提交所需的四个权威仓库。
    def __init__(
        self,
        *,
        candidate_service: CandidateService,
        daily_store: DailyMemoryStore,
        state_store: MemoryCuratorStateStore,
        run_log: CuratorRunLog,
    ) -> None:
        self.candidates = candidate_service
        self.daily = daily_store
        self.state = state_store
        self.run_log = run_log
        self.memory_root = _memory_root(candidate_service, daily_store, state_store, run_log)
        self.transactions_dir = self.state.path.parent / "transactions"
        self.transactions_dir.mkdir(parents=True, exist_ok=True)

    # LLM: All target content is computed and quota-checked before the first canonical replace;
    # an exception restores every preimage while locks are still held.
    # 函数用途: 提交一个成功 Curator 批次并返回实际幂等落点。
    def commit(self, request: CuratorBatchCommit) -> CuratorBatchCommitResult:
        _validate_batch_identity(request)
        paths = self._target_paths(request)
        with self._admissions() as admissions:
            with _locked_paths(paths):
                changes, daily_events, candidates = self._build_changes(request)
                _check_admissions(admissions, changes)
                transaction = self._prepare_transaction(request, changes)
                try:
                    self._apply_changes(changes)
                except Exception as exc:
                    try:
                        self._restore_transaction(transaction, paths_locked=True)
                    except Exception as rollback_exc:
                        raise CuratorCommitRecoveryError(
                            "curator batch rollback failed"
                        ) from rollback_exc
                    _cleanup_transaction(_transaction_directory(transaction))
                    raise CuratorBatchCommitError("curator batch commit failed") from exc
        _cleanup_committed_best_effort(_transaction_directory(transaction))
        return CuratorBatchCommitResult(tuple(daily_events), tuple(candidates))

    # LLM: A live lease is never rolled back by another instance; committed state only cleans
    # stale backups, while expired/unowned prepared transactions restore every preimage.
    # 函数用途: 在新一轮运行前恢复进程崩溃遗留的事务并返回恢复审计数量。
    def recover_incomplete(self, *, now: datetime | None = None) -> int:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        recovered = 0
        for directory in sorted(self.transactions_dir.iterdir()):
            if directory.is_symlink() or not directory.is_dir():
                raise CuratorCommitRecoveryError("curator transaction root contains non-directory")
            manifest_path = directory / "manifest.json"
            if not manifest_path.exists():
                _cleanup_transaction(directory)
                continue
            manifest = _load_manifest(manifest_path, self.memory_root)
            state = self.state.load()
            run_id = str(manifest["run_id"])
            if state.last_committed_run_id == run_id:
                _cleanup_transaction(directory)
                continue
            if _same_live_lease(state.active_lease, run_id=run_id, now=current):
                continue
            paths = [Path(item["path"]) for item in manifest["targets"]]
            with _locked_paths(paths):
                state = self.state._load_unlocked()
                if state.last_committed_run_id == run_id:
                    _cleanup_transaction(directory)
                    continue
                if _same_live_lease(state.active_lease, run_id=run_id, now=current):
                    continue
                self._restore_transaction(manifest, paths_locked=True)
            _cleanup_transaction(directory)
            # 审计记录记恢复实际发生的时刻（真实时钟），不用调用方注入的 now：
            # now 只用于 lease 过期判定（测试/回放可模拟未来），若审计也用它，
            # 跨日分片时 rollback 记录会被写进未来分片，run_log 排序失真
            # （recovered_rollback 与 succeeded 相对顺序错位）。
            self.run_log.append(
                _recovery_record(
                    manifest, finished_at=datetime.now(timezone.utc).isoformat()
                )
            )
            recovered += 1
        return recovered

    # LLM: Target locks cover every daily shard plus the three owner ledgers, with state sorted
    # among them but still written last by _apply_changes.
    # 函数用途: 计算本批次所有会被替换的规范文件路径。
    def _target_paths(self, request: CuratorBatchCommit) -> list[Path]:
        daily_paths = {
            daily_memory_path(self.daily.daily_dir, day=event.created_at[:10])
            for event in request.daily_events
        }
        run_path = run_log_path(self.run_log.runs_dir, request.run_record.finished_at)
        return sorted(
            {self.candidates.path, self.state.path, run_path, *daily_paths},
            key=lambda item: str(item.resolve(strict=False)),
        )

    # LLM: Pure merge helpers are shared with standalone stores, so transaction mode cannot
    # silently change candidate occurrence or daily sequence semantics.
    # 函数用途: 读取锁内当前态并构造每个目标文件的完整下一文本。
    def _build_changes(
        self,
        request: CuratorBatchCommit,
    ) -> tuple[dict[Path, str], list[DailyMemoryEvent], list[MemoryCandidate]]:
        current_state = self.state._load_unlocked()
        next_state = build_success_state(current_state, request.state_commit)
        candidate_rows = self.candidates._load_unlocked()
        if request.candidate_observations:
            candidate_rows, committed_candidates = merge_candidate_observations(
                candidate_rows,
                request.candidate_observations,
            )
        else:
            committed_candidates = []
        changes = {self.candidates.path: _candidate_text(candidate_rows)}
        committed_daily = self._daily_changes(request.daily_events, changes)
        run_path = run_log_path(self.run_log.runs_dir, request.run_record.finished_at)
        run_rows = merge_run_record(load_run_records_unlocked(run_path), request.run_record)
        changes[run_path] = run_records_text(run_rows)
        changes[self.state.path] = _state_text(next_state.to_dict())
        return changes, committed_daily, committed_candidates

    # LLM: Each date shard receives its entire sub-batch in model order while sequence remains
    # based on the existing authoritative chain.
    # 函数用途: 为所有 Daily 日分片生成完整新文本并收集实际事件。
    def _daily_changes(
        self,
        events: tuple[DailyMemoryEvent, ...],
        changes: dict[Path, str],
    ) -> list[DailyMemoryEvent]:
        grouped: dict[Path, list[DailyMemoryEvent]] = {}
        for event in events:
            path = daily_memory_path(self.daily.daily_dir, day=event.created_at[:10])
            grouped.setdefault(path, []).append(event)
        committed: list[DailyMemoryEvent] = []
        for path in sorted(grouped, key=lambda item: str(item.resolve(strict=False))):
            rows, selected = merge_daily_events(_load_daily_unlocked(path), grouped[path])
            changes[path] = _daily_text(rows)
            committed.extend(selected)
        return committed

    # LLM: Backups and the atomic manifest are complete before canonical mutation; an orphan
    # directory without a manifest is therefore safe to discard on restart.
    # 函数用途: 保存事务前镜像和无正文运行元数据，形成崩溃恢复点。
    def _prepare_transaction(
        self,
        request: CuratorBatchCommit,
        changes: dict[Path, str],
    ) -> dict[str, object]:
        directory = self.transactions_dir / _safe_run_name(request.run_record.run_id)
        if directory.exists():
            raise CuratorCommitRecoveryError("curator transaction already exists")
        directory.mkdir(parents=True, exist_ok=False)
        targets: list[dict[str, object]] = []
        for index, path in enumerate(changes):
            _assert_target(path, self.memory_root)
            existed = path.exists()
            before = path.read_text(encoding="utf-8") if existed else ""
            backup = directory / f"before-{index:03d}.txt"
            write_text_file_atomic(backup, before)
            targets.append(
                {
                    "path": str(path.resolve(strict=False)),
                    "backup": backup.name,
                    "existed": existed,
                    "before_sha256": _sha256(before),
                    "after_sha256": _sha256(changes[path]),
                }
            )
        record = request.run_record
        manifest: dict[str, object] = {
            "schema_version": CURATOR_TRANSACTION_SCHEMA_VERSION,
            "run_id": record.run_id,
            "lease_id": record.lease_id,
            "reason": record.reason,
            "provider": record.provider,
            "model": record.model,
            "started_at": record.started_at,
            "prepared_at": utc_now_iso(),
            "targets": targets,
            "manifest_path": str((directory / "manifest.json").resolve(strict=False)),
        }
        write_json_file_atomic(directory / "manifest.json", manifest, sort_keys=True)
        return manifest

    # LLM: State is deliberately moved to the final replace; its last_committed_run_id is the
    # sole durable decision used by restart recovery.
    # 函数用途: 按 Daily、Candidate、run audit、state 最后提交的顺序替换文件。
    def _apply_changes(self, changes: dict[Path, str]) -> None:
        ordered = [path for path in changes if path != self.state.path]
        ordered.sort(key=lambda item: str(item.resolve(strict=False)))
        ordered.append(self.state.path)
        for path in ordered:
            self._write_target(path, changes[path])

    # LLM: This narrow method exists as a deterministic disk-failure seam for tests; production
    # behavior is the canonical atomic writer under an already-held path lock.
    # 函数用途: 原子替换一个已持锁的事务目标。
    def _write_target(self, path: Path, content: str) -> None:
        write_text_file_atomic_unlocked(path, content)

    # LLM: Restore verifies every backup/hash and never follows a manifest target outside the
    # owner memory root.
    # 函数用途: 将未提交事务的所有目标恢复到事务前状态。
    def _restore_transaction(
        self,
        manifest: dict[str, object],
        *,
        paths_locked: bool,
    ) -> None:
        if not paths_locked:
            raise CuratorCommitRecoveryError("curator rollback requires all target locks")
        manifest_path = Path(str(manifest["manifest_path"]))
        directory = manifest_path.parent
        for item in manifest["targets"]:
            path = Path(str(item["path"]))
            _assert_target(path, self.memory_root)
            backup = directory / str(item["backup"])
            if not backup.is_file():
                raise CuratorCommitRecoveryError("curator transaction backup is missing")
            before = backup.read_text(encoding="utf-8")
            if _sha256(before) != str(item["before_sha256"]):
                raise CuratorCommitRecoveryError("curator transaction backup hash mismatch")
            current_exists = path.exists()
            current = path.read_text(encoding="utf-8") if current_exists else ""
            before_matches = current_exists == bool(item["existed"]) and _sha256(
                current
            ) == str(item["before_sha256"])
            if before_matches:
                continue
            after_matches = current_exists and _sha256(current) == str(item["after_sha256"])
            if not after_matches:
                raise CuratorCommitRecoveryError(
                    "curator transaction target changed outside the prepared batch"
                )
            if bool(item["existed"]):
                write_text_file_atomic_unlocked(path, before)
            else:
                path.unlink(missing_ok=True)

    # LLM: Quota admissions are deduplicated by owner root and acquired before file locks,
    # preserving the repository-wide lock order.
    # 函数用途: 获取本批次涉及的 owner quota 临界区。
    def _admissions(self):
        return _AdmissionContext(
            [self.candidates.quota_enforcer, self.daily.quota_enforcer]
        )


# LLM: Context ownership is isolated so the committer class stays focused and duplicate
# enforcer objects for one owner cannot self-deadlock.
# 类用途: 按 owner root 去重并持有多文件提交所需的 quota admission。
class _AdmissionContext:
    # LLM: 输入 enforcer 只按 owner root 去重；不得绕过其 admission 或改变全仓锁序。
    # 函数用途: 初始化多 owner quota 临界区管理器。
    def __init__(self, enforcers: list[object | None]) -> None:
        self.enforcers = enforcers
        self.stack = ExitStack()
        self.admissions: list[_QuotaAdmission] = []

    # LLM: 进入顺序决定 quota->file lock 约束；同 owner 的重复实例只能获取一次。
    # 函数用途: 进入所有去重后的 owner quota admission。
    def __enter__(self) -> list[_QuotaAdmission]:
        seen: set[str] = set()
        for enforcer in self.enforcers:
            if enforcer is None:
                continue
            root = str(Path(enforcer.owner_root).resolve(strict=False))
            if root in seen:
                continue
            seen.add(root)
            admission = self.stack.enter_context(enforcer.admission())
            self.admissions.append(_QuotaAdmission(Path(enforcer.owner_root), admission))
        return self.admissions

    # LLM: ExitStack 原样处理退出和异常，不能把 commit/rollback 失败转成成功。
    # 函数用途: 退出已获取的 quota admission 并释放锁。
    def __exit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> object:
        return self.stack.__exit__(exc_type, exc, traceback)


# LLM: Pairing the canonical owner root with its admission avoids reaching into private quota
# fields when projecting transaction backup cost.
# 类用途: 保存一个去重后的 owner quota 临界区及其路径边界。
@dataclass(frozen=True)
class _QuotaAdmission:
    owner_root: Path
    admission: OwnerQuotaAdmission


# LLM: Every file lock is acquired in resolved-path order, matching ordinary single-file
# repository locks without introducing a second daemon or in-memory authority.
# 函数用途: 为一组规范目标建立确定性多文件临界区。
def _locked_paths(paths: list[Path]):
    stack = ExitStack()
    for path in sorted(set(paths), key=lambda item: str(item.resolve(strict=False))):
        stack.enter_context(locked_json_path(path))
    return stack


# LLM: Identity validation rejects a state/run mismatch before creating backups or touching a
# canonical file.
# 函数用途: 校验批次中的 run、lease 和成功 state 属于同一事务。
def _validate_batch_identity(request: CuratorBatchCommit) -> None:
    if request.run_record.run_id != request.state_commit.run_id:
        raise ValueError("curator batch run_id mismatch")
    if request.run_record.lease_id != request.state_commit.lease_id:
        raise ValueError("curator batch lease_id mismatch")
    if request.run_record.status != "succeeded" or request.run_record.phase != "final":
        raise ValueError("curator batch requires a final succeeded run audit")
    if request.run_record.daily_events != len(request.daily_events):
        raise ValueError("curator batch daily count mismatch")
    if request.run_record.candidates != len(request.candidate_observations):
        raise ValueError("curator batch candidate count mismatch")


# LLM: Each owner admission evaluates the entire final mutation and transaction backup cost;
# roots outside that owner are ignored by the canonical quota enforcer.
# 函数用途: 在首次写入前验证事务目标与恢复备份的容量。
def _check_admissions(
    admissions: list[_QuotaAdmission],
    changes: dict[Path, str],
) -> None:
    target_changes = [
        OwnerQuotaChange(path, len(content.encode("utf-8")))
        for path, content in changes.items()
    ]
    backup_bytes = sum(path.stat().st_size for path in changes if path.exists()) + 16_384
    for handle in admissions:
        transaction_probe = handle.owner_root / ".curator-transaction-probe"
        handle.admission.check(
            [*target_changes, OwnerQuotaChange(transaction_probe, backup_bytes)]
        )


# LLM: Candidate serialization reuses strict to_record output and creates no separate candidate
# audit or compatibility format.
# 函数用途: 序列化候选当前态完整 JSONL。
def _candidate_text(records: list[MemoryCandidate]) -> str:
    return "".join(
        json.dumps(item.to_record(), ensure_ascii=False, sort_keys=True) + "\n"
        for item in records
    )


# LLM: Daily serialization preserves authoritative sequence order already assigned by the pure
# merge helper.
# 函数用途: 序列化一个 Daily 日分片。
def _daily_text(records: list[DailyMemoryEvent]) -> str:
    return "".join(
        json.dumps(item.to_record(), ensure_ascii=False, sort_keys=True) + "\n"
        for item in records
    )


# LLM: State text matches the canonical atomic JSON writer's human-readable deterministic
# format, making backup/recovery hash comparisons stable.
# 函数用途: 序列化 Curator state.json。
def _state_text(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


# LLM: The common root is derived only from injected canonical repositories and must be the
# memory directory, preventing a transaction from widening to the owner workspace.
# 函数用途: 解析并验证事务允许操作的 owner memory 根目录。
def _memory_root(
    candidates: CandidateService,
    daily: DailyMemoryStore,
    state: MemoryCuratorStateStore,
    run_log: CuratorRunLog,
) -> Path:
    roots = {
        candidates.path.parent.resolve(strict=False),
        daily.daily_dir.parent.resolve(strict=False),
        state.path.parent.parent.resolve(strict=False),
        run_log.runs_dir.parent.parent.resolve(strict=False),
    }
    if len(roots) != 1:
        raise ValueError("curator repositories do not share one owner memory root")
    return roots.pop()


# LLM: Manifest parsing is exact and fail-closed; a malformed recovery file never causes broad
# deletion or an inferred target path.
# 函数用途: 严格读取并验证一个崩溃恢复事务清单。
def _load_manifest(path: Path, memory_root: Path) -> dict[str, object]:
    report = read_json_object_report(path, context="memory_curator.transaction")
    if report.load_error:
        raise CuratorCommitRecoveryError("curator transaction manifest is unreadable")
    manifest = dict(report.payload)
    required = {
        "schema_version",
        "run_id",
        "lease_id",
        "reason",
        "provider",
        "model",
        "started_at",
        "prepared_at",
        "targets",
        "manifest_path",
    }
    if set(manifest) != required or manifest.get("schema_version") != CURATOR_TRANSACTION_SCHEMA_VERSION:
        raise CuratorCommitRecoveryError("curator transaction manifest schema mismatch")
    if Path(str(manifest.get("manifest_path"))).resolve(strict=False) != path.resolve(strict=False):
        raise CuratorCommitRecoveryError("curator transaction manifest path mismatch")
    targets = manifest.get("targets")
    if not isinstance(targets, list) or not targets:
        raise CuratorCommitRecoveryError("curator transaction targets are invalid")
    for item in targets:
        _validate_manifest_target(item, path.parent, memory_root)
    target_paths = [str(item["path"]) for item in targets]
    if len(target_paths) != len(set(target_paths)):
        raise CuratorCommitRecoveryError("curator transaction targets contain duplicates")
    return manifest


# LLM: Recovery targets use exact keys, local backup basenames, and owner-memory-contained paths;
# symlink or traversal attempts fail closed.
# 函数用途: 校验一条事务目标记录。
def _validate_manifest_target(item: object, directory: Path, memory_root: Path) -> None:
    keys = {"path", "backup", "existed", "before_sha256", "after_sha256"}
    if not isinstance(item, dict) or set(item) != keys:
        raise CuratorCommitRecoveryError("curator transaction target schema mismatch")
    target = Path(str(item["path"]))
    _assert_target(target, memory_root)
    backup_name = str(item["backup"])
    if Path(backup_name).name != backup_name or not (directory / backup_name).is_file():
        raise CuratorCommitRecoveryError("curator transaction backup path is invalid")
    if not isinstance(item["existed"], bool):
        raise CuratorCommitRecoveryError("curator transaction existed flag is invalid")
    for key in ("before_sha256", "after_sha256"):
        value = str(item[key])
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise CuratorCommitRecoveryError("curator transaction hash is invalid")


# LLM: Only files strictly below the derived memory root are legal transaction targets;
# directories and symlinks are never followed.
# 函数用途: 限定事务路径边界。
def _assert_target(path: Path, memory_root: Path) -> None:
    resolved = path.resolve(strict=False)
    try:
        resolved.relative_to(memory_root)
    except ValueError as exc:
        raise CuratorCommitRecoveryError("curator transaction target escapes memory root") from exc
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise CuratorCommitRecoveryError("curator transaction target is not a regular file")


# LLM: A lease blocks recovery only when it belongs to the same run and has a valid future UTC
# expiry; malformed expiry fails closed.
# 函数用途: 判断事务对应的原运行是否仍合法持有 lease。
def _same_live_lease(
    lease: dict[str, object],
    *,
    run_id: str,
    now: datetime,
) -> bool:
    if not lease or str(lease.get("run_id") or "") != run_id:
        return False
    try:
        expires = datetime.fromisoformat(str(lease.get("expires_at") or ""))
    except ValueError as exc:
        raise CuratorCommitRecoveryError("curator transaction lease expiry is invalid") from exc
    if expires.tzinfo is None:
        raise CuratorCommitRecoveryError("curator transaction lease expiry lacks timezone")
    return expires.astimezone(timezone.utc) > now


# LLM: Recovery audit contains only old run/lease metadata and a stable code; restored content
# remains solely in canonical files.
# 函数用途: 构造一次崩溃事务回滚审计记录。
def _recovery_record(manifest: dict[str, object], *, finished_at: str) -> CuratorRunRecord:
    return CuratorRunRecord(
        run_id=str(manifest["run_id"]),
        lease_id=str(manifest["lease_id"]),
        status="recovered_rollback",
        phase="recovery",
        reason=str(manifest["reason"]),
        provider=str(manifest["provider"]),
        model=str(manifest["model"]),
        started_at=str(manifest["started_at"]),
        finished_at=finished_at,
        failure_code="CURATOR_INCOMPLETE_COMMIT_ROLLED_BACK",
        recovery={"kind": "transaction_rollback", "prepared_at": manifest["prepared_at"]},
    )


# LLM: Cleanup accepts only the exact transaction run directory selected by the caller and
# refuses the transactions root itself.
# 函数用途: 删除已提交、已回滚或未形成 manifest 的临时事务目录。
def _cleanup_transaction(directory: Path) -> None:
    if directory.name in {"", ".", "..", "transactions"}:
        raise CuratorCommitRecoveryError("refusing broad curator transaction cleanup")
    shutil.rmtree(directory, ignore_errors=False)


# LLM: Once state has been atomically committed, cleanup failure must not turn a successful
# batch into a false failure; restart recovery recognizes last_committed_run_id and retries it.
# 函数用途: 尽力删除已提交事务恢复材料，失败时保留给下一次安全清理。
def _cleanup_committed_best_effort(directory: Path) -> None:
    try:
        _cleanup_transaction(directory)
    except OSError:
        return


# LLM: The manifest carries its own exact path, avoiding reconstruction from untrusted run IDs
# during cleanup and rollback.
# 函数用途: 获取已验证或刚创建事务的目录。
def _transaction_directory(manifest: dict[str, object]) -> Path:
    return Path(str(manifest["manifest_path"])).parent


# LLM: Run identifiers become one local directory component only; unexpected text cannot alter
# recovery scope.
# 函数用途: 校验事务目录使用的 run_id。
def _safe_run_name(run_id: str) -> str:
    value = str(run_id or "").strip()
    if not value.startswith("memory-curator-run-") or not value.replace("-", "").isalnum():
        raise ValueError("invalid memory curator run_id")
    return value


# LLM: Hashes cover exact UTF-8 file text and are used only to validate transaction backups.
# 函数用途: 计算事务文件文本 SHA-256。
def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


__all__ = [
    "CURATOR_TRANSACTION_SCHEMA_VERSION",
    "CuratorBatchCommit",
    "CuratorBatchCommitError",
    "CuratorBatchCommitResult",
    "CuratorBatchCommitter",
    "CuratorCommitRecoveryError",
]
