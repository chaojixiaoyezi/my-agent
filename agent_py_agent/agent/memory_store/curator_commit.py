from __future__ import annotations

"""Memory Curator 的可恢复多文件整批提交。"""

# LLM: 成功提交仍以 state 最后落盘为标记；恢复、清理、审计和新租约在同一规范文件锁集合内完成。
# 锁前清单只发现锁集合；quota 先于有序目标锁，锁内必须重读清单和 state，竞争不能变成模型错误。
# 模块用途: 为 Curator 提供整批校验、配额检查、备份、提交、异常回滚和进程重启恢复。

import hashlib
import json
import shutil
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from ..common.json_io import (
    locked_json_path,
    read_json_object_report,
    write_json_file_atomic,
    write_private_text_file_atomic_unlocked,
)
from ..common.nofollow_fs import ensure_private_dir
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
from .curator_state import (
    CuratorLeaseRequest,
    CuratorStateCorruptError,
    CuratorSuccessCommit,
    MemoryCuratorStateStore,
    build_success_state,
    stale_lease_reclaimable,
)
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


# LLM: 只用于取锁阶段竞争，不吞清理/读写中的 BlockingIOError；服务可退回 busy 后再调度。
# 类用途: 标记完整接管锁集合繁忙或发现集合已换代，尚未执行恢复和领取。
class _CuratorRecoveryBusy(CuratorCommitRecoveryError):
    pass


# LLM: 清单快照只用于发现全部目标锁，不能授权恢复；最终以锁内复核为准。
# 类用途: 保存一次接管准备阶段发现的事务、规范锁集合与时间口径。
@dataclass(frozen=True)
class _RecoveryPlan:
    manifests: dict[Path, dict[str, object] | None]
    paths: tuple[Path, ...]
    current: datetime
    blocking: bool = True


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
        # 目录缺失时逐级按 0700 新建（pbfix 2026-10-04：统一走 nofollow_fs.ensure_private_dir；已存在的目录一律不动）。
        ensure_private_dir(self.transactions_dir)

    # LLM: 先整批校验配额再写入；异常在原锁内回滚。成功清理也留在同锁内，不能与恢复者竞争删备份。
    # 函数用途: 原子提交 Curator 批次并清理事务，返回实际幂等落点。
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
                except CuratorStateCorruptError:
                    # state 仓库损坏不是 commit 失败: 原样冒泡, 由上层隔离留证,
                    # 不伪装成 CURATOR_COMMIT_FAILED(回滚写 state 同样会失败)。
                    raise
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

    # LLM: 独立恢复也必须同锁复核、恢复、清理和审计；不领取租约，默认等待规范锁。
    # 函数用途: 保留独立恢复接口，返回本次真正恢复的事务数。
    def recover_incomplete(self, *, now: datetime | None = None) -> int:
        plan = _recovery_plan(self, now)
        with _recovery_section(self, plan):
            return _recover_locked(self, plan)

    # LLM: 前台服务使用这个完整接管入口；任一目标锁竞争立即 busy，不等待持锁清理的对端。
    # 函数用途: 在同一临界区恢复旧事务并领取新租约，模型执行在解锁之后。
    def recover_and_acquire(self, request: CuratorLeaseRequest):
        plan = replace(_recovery_plan(self, request.now), blocking=False)
        try:
            with _recovery_section(self, plan):
                _recover_locked(self, plan)
                return self.state._acquire_unlocked(request)
        except _CuratorRecoveryBusy:
            return None

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
    #   事务前镜像（before-*.txt）是记忆正文副本：在各自路径锁下按私有原子写（0600，事务目录 0700）；manifest 无正文，照旧。
    # 函数用途: 保存事务前镜像和无正文运行元数据，形成崩溃恢复点。
    def _prepare_transaction(
        self,
        request: CuratorBatchCommit,
        changes: dict[Path, str],
    ) -> dict[str, object]:
        directory = self.transactions_dir / _safe_run_name(request.run_record.run_id)
        if directory.exists():
            raise CuratorCommitRecoveryError("curator transaction already exists")
        # 目录缺失时按 0700 新建（pdp 2026-10-03：私有写只动自己建的东西；已存在的目录一律不动）。
        directory.mkdir(parents=True, exist_ok=False, mode=0o700)
        targets: list[dict[str, object]] = []
        for index, path in enumerate(changes):
            _assert_target(path, self.memory_root)
            existed = path.exists()
            before = path.read_text(encoding="utf-8") if existed else ""
            backup = directory / f"before-{index:03d}.txt"
            with locked_json_path(backup):
                write_private_text_file_atomic_unlocked(backup, before)
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
    # behavior is the private atomic writer under an already-held path lock（日事件/候选等目标装记忆正文，
    #   一律 0600，已有 0644 的目标在这次替换时收紧）。
    # 函数用途: 以仅本人可读写的权限原子替换一个已持锁的事务目标。
    def _write_target(self, path: Path, content: str) -> None:
        write_private_text_file_atomic_unlocked(path, content)

    # LLM: Restore verifies every backup/hash and never follows a manifest target outside the
    # owner memory root. 恢复写回同样走私有原子写（0600），不把旧文件的 0644 带回来。
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
                write_private_text_file_atomic_unlocked(path, before)
            else:
                path.unlink(missing_ok=True)

    # LLM: 所有恢复写入也先取 owner quota admission，再按规范路径取目标锁，不能倒置。
    # 函数用途: 获取去重后的 owner quota 临界区，接管入口可在竞争时立即退出。
    def _admissions(self, *, blocking: bool = True):
        return _AdmissionContext(
            [self.candidates.quota_enforcer, self.daily.quota_enforcer], blocking=blocking
        )


# LLM: Context ownership is isolated so the committer class stays focused and duplicate
# enforcer objects for one owner cannot self-deadlock.
# 类用途: 按 owner root 去重并持有多文件提交所需的 quota admission。
class _AdmissionContext:
    # LLM: 去重和阻塞方式只控制同一 admission，不另建 quota 锁或权威。
    # 函数用途: 初始化多 owner quota 临界区管理器及竞争策略。
    def __init__(self, enforcers: list[object | None], *, blocking: bool = True) -> None:
        self.enforcers = enforcers
        self.blocking = blocking
        self.stack = ExitStack()
        self.admissions: list[_QuotaAdmission] = []

    # LLM: 取锁中途失败必须释放已取得的 admission，非阻塞竞争尤其不能留下半个锁集合。
    # 函数用途: 安全进入所有 quota admission，失败时归还已取得的锁。
    def __enter__(self) -> list[_QuotaAdmission]:
        try:
            return self._enter_all()
        except BaseException:
            self.stack.close()
            raise

    # LLM: quota 必须早于目标文件；同 owner 重复对象仍只取一次，阻塞与非阻塞同源。
    # 函数用途: 按 owner 根去重后取得每个 admission。
    def _enter_all(self) -> list[_QuotaAdmission]:
        seen: set[str] = set()
        for enforcer in self.enforcers:
            if enforcer is None:
                continue
            root = str(Path(enforcer.owner_root).resolve(strict=False))
            if root in seen:
                continue
            seen.add(root)
            admission = self.stack.enter_context(enforcer.admission(blocking=self.blocking))
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


# LLM: 规范路径有序取同一双层锁；中途取锁失败时也释放已取得的锁，禁止非阻塞锁泄漏。
# 函数用途: 建立完整目标文件临界区，支持接管时的即忙即退。
@contextmanager
def _locked_paths(paths: list[Path], *, blocking: bool = True):
    with ExitStack() as stack:
        for path in sorted(set(paths), key=lambda item: str(item.resolve(strict=False))):
            stack.enter_context(locked_json_path(path, blocking=blocking))
        yield


# LLM: 发现阶段可能与对端清理交错，失败先留为未知；持完整规范锁后必须重读，不能把快照用于回滚。
# 函数用途: 尝试发现一个事务的锁集合，不在此阶段恢复、清理或领取。
def _discover_manifest(directory: Path, memory_root: Path):
    if directory.is_symlink() or not directory.is_dir():
        return None
    path = directory / "manifest.json"
    if not path.exists():
        return None
    try:
        return _load_manifest(path, memory_root)
    except CuratorCommitRecoveryError:
        return None


# LLM: candidates 是每个成功提交都会持有的规范锁；与 state、实际日期审计及全部旧目标一起排序取锁。
# 函数用途: 在锁前仅发现完整目标集合；恢复审计绝不采用回放的未来时间。
def _recovery_plan(committer: CuratorBatchCommitter, now: datetime | None):
    manifests = {directory: _discover_manifest(directory, committer.memory_root)
                 for directory in sorted(committer.transactions_dir.iterdir())}
    audit = run_log_path(committer.run_log.runs_dir, datetime.now(timezone.utc).isoformat())
    paths = {committer.candidates.path, committer.state.path, audit}
    for manifest in manifests.values():
        if manifest is not None:
            paths.update(Path(item["path"]) for item in manifest["targets"])
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return _RecoveryPlan(manifests, tuple(path.resolve(strict=False) for path in paths), current)


# LLM: 只有取锁阶段的 BlockingIOError 是 busy；进入临界区后的清理/审计/领取异常必须报恢复失败。
# 函数用途: 依既有 quota→有序规范文件锁进入接管临界区，并保证失败释放完整锁集合。
def _enter_recovery_locks(stack: ExitStack, committer: CuratorBatchCommitter, plan: _RecoveryPlan):
    try:
        stack.enter_context(committer._admissions(blocking=plan.blocking))
        stack.enter_context(_locked_paths(list(plan.paths), blocking=plan.blocking))
    except BlockingIOError as exc:
        raise _CuratorRecoveryBusy("curator recovery locks are busy") from exc


# LLM: 模型尚未执行；I/O/清理/审计等失败不能被上层归成 CURATOR_MODEL_FAILED，state 损坏仍单列。
# 函数用途: 管理完整恢复和领取临界区的生命周期、锁释放及准确的失败分类。
@contextmanager
def _recovery_section(committer: CuratorBatchCommitter, plan: _RecoveryPlan):
    with ExitStack() as stack:
        try:
            _enter_recovery_locks(stack, committer, plan)
            yield
        except (CuratorStateCorruptError, CuratorCommitRecoveryError):
            raise
        except Exception as exc:
            raise CuratorCommitRecoveryError("curator recovery or lease acquisition failed") from exc


# LLM: 发现后若又出现未锁住的事务则退出重试；等待者只处理锁内仍存在且已严格复核的清单。
# 函数用途: 持完整锁集合后重新枚举权威事务目录并恢复尚未处理的事务。
def _recover_locked(committer: CuratorBatchCommitter, plan: _RecoveryPlan) -> int:
    directories = sorted(committer.transactions_dir.iterdir())
    if set(directories) - plan.manifests.keys():
        raise _CuratorRecoveryBusy("curator recovery discovery changed")
    return sum(_recover_directory(committer, directory, plan) for directory in directories)


# LLM: 缺失仅在规范锁内复核后跳过；真实清理失败不吞。换代目标不得绕过发现阶段的锁集合。
# 函数用途: 严格重读当前清单；已处理事务不再恢复，坏清单原样报错。
def _read_recovery_manifest(committer: CuratorBatchCommitter, directory: Path, plan: _RecoveryPlan):
    if directory.is_symlink() or not directory.is_dir():
        raise CuratorCommitRecoveryError("curator transaction root contains non-directory")
    path = directory / "manifest.json"
    if not path.exists():
        _cleanup_transaction(directory)
        return None
    manifest = _read_manifest(path, committer.memory_root)
    targets = {Path(item["path"]).resolve(strict=False) for item in manifest["targets"]}
    if not targets.issubset(plan.paths):
        raise _CuratorRecoveryBusy("curator recovery targets changed")
    return manifest


# LLM: 真实时钟决定恢复审计分片；发现等待跨日时必须先重新发现锁集合，不能锁错分片仍写入。
# 函数用途: 在真正开始恢复时确定审计时间并检查其分片锁已持有。
def _recovery_time(committer: CuratorBatchCommitter, plan: _RecoveryPlan) -> str:
    finished_at = datetime.now(timezone.utc).isoformat()
    path = run_log_path(committer.run_log.runs_dir, finished_at).resolve(strict=False)
    if path not in plan.paths:
        raise _CuratorRecoveryBusy("curator recovery audit day changed")
    return finished_at


# LLM: 复核、恢复、清理及审计都必须留在完整临界区；随后锁内领取会再次重读恢复后的 state。
# 函数用途: 根据当前提交标记和共享死亡判据恢复一个事务，不触碰仍活跃的运行。
def _recover_directory(committer: CuratorBatchCommitter, directory: Path, plan: _RecoveryPlan) -> int:
    manifest = _read_recovery_manifest(committer, directory, plan)
    if manifest is None:
        return 0
    state = committer.state._load_unlocked()
    run_id = str(manifest["run_id"])
    if state.last_committed_run_id == run_id:
        _cleanup_transaction(directory)
        return 0
    if _same_live_lease(state.active_lease, run_id=run_id, now=plan.current):
        return 0
    finished_at = _recovery_time(committer, plan)
    committer._restore_transaction(manifest, paths_locked=True)
    _cleanup_transaction(directory)
    committer.run_log._append_unlocked(_recovery_record(manifest, finished_at=finished_at))
    return 1


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


# LLM: 锁前只发现目标；与锁内复核共用严格读取器，但发现结果不能授权修改任何规范文件。
# 函数用途: 读取事务清单以确定要取得的规范锁集合。
def _load_manifest(path: Path, memory_root: Path) -> dict[str, object]:
    return _read_manifest(path, memory_root)


# LLM: 同一严格解析器供发现与锁内重读，保持路径、备份和 Schema 的精确 fail-closed 合同。
# 函数用途: 从磁盘重新读取并验证当前事务清单，绝不复用发现时的内容。
def _read_manifest(path: Path, memory_root: Path) -> dict[str, object]:
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


# LLM: A lease blocks recovery only when it belongs to the same run, has a valid future UTC
# expiry, and its holder is not provably dead; the death check is the same function acquire
# uses, so recovery and early lease handover can never disagree (mc4-1802). Malformed expiry
# fails closed.
# 函数用途: 判断事务对应的原运行是否仍合法持有 lease（未过期且持有者未确定死亡）。
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
    if expires.astimezone(timezone.utc) <= now:
        return False
    # 未过期：持有者已确定死亡时不再算 live，旧事务先在本锁内恢复、之后 acquire 才能换租约；
    # 不确定（权限/异主机/信息不全/Windows）继续保守等待，与 acquire 判据完全一致。
    return not stale_lease_reclaimable(lease)


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


# LLM: 未提交恢复的清理失败必须阻断接管；与成功提交后的尽力清理分开，但共用相同边界和删除原语。
# 函数用途: 严格删除已回滚或未形成 manifest 的事务材料，失败原样交给恢复错误分类。
def _cleanup_transaction(directory: Path) -> None:
    _remove_transaction_directory(directory)


# LLM: 两种清理策略共用唯一删除原语，只接受精确事务目录，禁止删除整个 transactions 根。
# 函数用途: 验证清理范围后删除事务目录，不忽略任何文件系统错误。
def _remove_transaction_directory(directory: Path) -> None:
    if directory.name in {"", ".", "..", "transactions"}:
        raise CuratorCommitRecoveryError("refusing broad curator transaction cleanup")
    shutil.rmtree(directory, ignore_errors=False)


# LLM: state 已提交是成功权威，后续删除失败不改变成功；仍在目标锁内清理，不能与恢复者争删。
# 函数用途: 尽力删除已提交事务恢复材料，失败时保留给下一次安全清理。
def _cleanup_committed_best_effort(directory: Path) -> None:
    try:
        _remove_transaction_directory(directory)
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
