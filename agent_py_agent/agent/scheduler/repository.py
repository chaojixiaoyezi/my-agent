from __future__ import annotations

"""Owner-scoped durable scheduler job and run repository.

The owner store is the only job authority.  A run reservation snapshots the
prompt/thread/Skill references before any model or tool side effect, matching
the durable pre-admission pattern used by 通道运行时 and 长期助手.
"""

import hashlib
import json
import logging
import os
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..common.json_io import (
    append_jsonl_records,
    locked_json_path,
    read_json_object_report,
    read_jsonl_objects_report,
    write_json_file_atomic_unlocked,
)
from ..runtime_errors import runtime_error_report
from ..user_space.owner_quota import (
    OwnerQuotaAdmission,
    OwnerQuotaChange,
    OwnerQuotaEnforcer,
)
from .due_index import SchedulerDueIndex
from .schedule import (
    ScheduleValidationError,
    compute_next_run,
    default_misfire_grace_seconds,
    schedule_timestamp,
    validate_schedule,
)

_STORE_SCHEMA = "scheduler_store.v1"
_JOB_SCHEMA = "scheduler_job.v1"
_RUN_SCHEMA = "scheduler_run.v1"
_JOB_STATUSES = frozenset({"active", "paused", "deleted"})
_ACTIVE_RUN_STATUSES = frozenset({"queued", "claimed", "running", "waiting"})
# P0-4(HANDOFF 文档线): unknown = 崩溃执行终态(进程死亡被证实后归类),
# 与 done/failed/cancelled/skipped 并列, 终态不可改写。
_TERMINAL_RUN_STATUSES = frozenset({"done", "failed", "cancelled", "skipped", "unknown"})
_MAX_NAME_CHARS = 160
_MAX_PROMPT_CHARS = 32_000
_MAX_SKILL_REFS = 32
_MANUAL_DISPATCH_DELAY_SECONDS = 2.0

_LOGGER = logging.getLogger(__name__)

# LLM: ``waiting_runs`` is called once per conversation tick (runtime.prepare_tick ->
# SchedulerService.reconcile_waiting_runs) and used to re-parse the whole owner ledger every time
# even when the file had not changed. The projection below binds the parsed waiting rows to the
# exact bytes they came from: (mtime_ns, size, inode, device) + a blake2b digest of the raw file.
# A hit therefore requires the same stat key *and* the same content digest, so any external or
# in-process rewrite (even same-size, same-timestamp) invalidates it. store.json stays the single
# authority; this is a read-through projection of it, never a second source of state.
_WAITING_PROJECTION_MAX_ENTRIES = 4
_WAITING_PROJECTION_MAX_ROWS = 512
_WAITING_PROJECTION_CACHE: OrderedDict[tuple[str, str, str, str], _WaitingRunsProjection] = (
    OrderedDict()
)
_WAITING_PROJECTION_LOCK = threading.Lock()
# 结构化可观测计数:命中/回退/守卫。测试与现场体检用它证明缓存真的在生效、且异常时确实回退。
_WAITING_PROJECTION_STATS: dict[str, int] = {"hit": 0, "miss": 0, "guard": 0, "skip": 0}


class SchedulerRepositoryError(RuntimeError):
    """Base class for durable scheduler failures."""


class SchedulerStateError(SchedulerRepositoryError):
    """The owner scheduler ledger cannot be read safely."""


class SchedulerNotFoundError(SchedulerRepositoryError):
    """A requested job or run does not exist in this owner store."""


class SchedulerConflictError(SchedulerRepositoryError):
    """A version, claim, or lifecycle precondition changed concurrently."""


@dataclass(frozen=True)
class SchedulerJobCreateRequest:
    """Validated create inputs passed as one value through normalization."""

    name: str
    prompt: str
    thread_id: str
    schedule: dict[str, object]
    source_task_id: str = ""
    misfire_grace_seconds: object = None
    skill_refs: object = ()
    source_request_id: str = ""
    now: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "thread_id", str(self.thread_id or "").strip())
        object.__setattr__(self, "source_task_id", str(self.source_task_id or "").strip())
        object.__setattr__(self, "source_request_id", str(self.source_request_id or "").strip())


@dataclass(frozen=True)
class SchedulerRunFinish:
    """Bounded terminal result fields for one claimed scheduler run."""

    status: str
    response: str = ""
    delivery_status: str = ""
    delivery_reason: str = ""
    error_code: str = ""
    error_message: str = ""
    now: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", str(self.status or "").strip().lower())


class _SchedulerJobOperations:
    """Public job CRUD operations mixed into the owner repository."""

    def create_job(
        self,
        request: SchedulerJobCreateRequest,
    ) -> tuple[dict[str, object], bool]:
        current = _now(request.now)
        normalized = _new_job_payload(
            owner=self.owner,
            request=request,
            now=current,
        )
        with self._mutation_scope() as (store, admission):
            for raw in store["jobs"].values():
                job, _error = self._parse_job(raw)
                if (
                    job is not None
                    and job["status"] != "deleted"
                    and request.source_request_id
                    and job.get("source_request_id") == request.source_request_id
                ):
                    self._sync_due_index_unlocked(store)
                    return deepcopy(job), True
            store["jobs"][str(normalized["job_id"])] = normalized
            self._write_store_unlocked(store, admission)
        return deepcopy(normalized), False

    def list_jobs(
        self, *, include_deleted: bool = False
    ) -> tuple[list[dict[str, object]], list[str]]:
        store = self._store_snapshot()
        jobs, errors = self._valid_jobs(store)
        selected = [job for job in jobs if include_deleted or job["status"] != "deleted"]
        selected.sort(key=lambda job: (float(job.get("next_run_at") or 1e30), str(job["job_id"])))
        return deepcopy(selected), errors

    def get_job(self, job_id: str, *, include_deleted: bool = False) -> dict[str, object]:
        store = self._store_snapshot()
        job = self._require_job(store, job_id)
        if job["status"] == "deleted" and not include_deleted:
            raise SchedulerNotFoundError(f"scheduler job not found: {job_id}")
        return deepcopy(job)

    def update_job(
        self,
        job_id: str,
        *,
        patch: dict[str, object],
        expected_version: int | None,
        now: float | None = None,
    ) -> dict[str, object]:
        current = _now(now)
        with self._mutation_scope() as (store, admission):
            job = self._require_mutable_job(store, job_id, expected_version)
            updated = _updated_job(job, patch, now=current)
            store["jobs"][job_id] = updated
            self._write_store_unlocked(store, admission)
        return deepcopy(updated)

    def pause_job(
        self,
        job_id: str,
        *,
        expected_version: int | None,
        reason: str = "",
        now: float | None = None,
    ) -> dict[str, object]:
        current = _now(now)
        with self._mutation_scope() as (store, admission):
            job = self._require_mutable_job(store, job_id, expected_version)
            if job["status"] != "paused":
                job = {
                    **job,
                    "status": "paused",
                    "paused_at": current,
                    "paused_reason": str(reason or "manual_pause")[:500],
                    "updated_at": current,
                    "version": int(job["version"]) + 1,
                }
                store["jobs"][job_id] = job
            cancelled = self._cancel_waiting_runs_unlocked(store, job_id, current, "job_paused")
            self._write_store_unlocked(store, admission, history_records=cancelled)
        return deepcopy(job)

    def resume_job(
        self,
        job_id: str,
        *,
        expected_version: int | None,
        now: float | None = None,
    ) -> dict[str, object]:
        current = _now(now)
        with self._mutation_scope() as (store, admission):
            job = self._require_mutable_job(store, job_id, expected_version)
            next_run = _resume_next_run(job, current)
            updated = {
                **job,
                "status": "active",
                "next_run_at": next_run,
                "paused_at": 0.0,
                "paused_reason": "",
                "updated_at": current,
                "version": int(job["version"]) + 1,
            }
            store["jobs"][job_id] = updated
            self._write_store_unlocked(store, admission)
        return deepcopy(updated)

    def delete_job(
        self,
        job_id: str,
        *,
        expected_version: int | None,
        now: float | None = None,
    ) -> dict[str, object]:
        current = _now(now)
        with self._mutation_scope() as (store, admission):
            job = self._require_mutable_job(store, job_id, expected_version)
            deleted = {
                **job,
                "status": "deleted",
                "deleted_at": current,
                "next_run_at": 0.0,
                "updated_at": current,
                "version": int(job["version"]) + 1,
            }
            store["jobs"][job_id] = deleted
            cancelled = self._cancel_waiting_runs_unlocked(store, job_id, current, "job_deleted")
            self._write_store_unlocked(store, admission, history_records=cancelled)
        return deepcopy(deleted)


class _SchedulerRunOperations:
    """Public durable run reservation, claim, completion, and history operations."""

    def reserve_manual_run(self, job_id: str, *, now: float | None = None) -> dict[str, object]:
        current = _now(now)
        with self._mutation_scope() as (store, admission):
            job = self._require_job(store, job_id)
            if job["status"] == "deleted":
                raise SchedulerNotFoundError(f"scheduler job not found: {job_id}")
            if self._job_has_active_run(store, job_id):
                raise SchedulerConflictError("scheduler job already has an active run")
            run = _reserved_run(
                job,
                trigger="manual",
                scheduled_for=current,
                dispatch_after=current + _MANUAL_DISPATCH_DELAY_SECONDS,
                now=current,
            )
            store["runs"][str(run["run_id"])] = run
            self._write_store_unlocked(store, admission)
        return deepcopy(run)

    def reserve_due_runs(
        self,
        *,
        now: float | None = None,
        limit: int = 32,
    ) -> list[dict[str, object]]:
        current = _now(now)
        reserved: list[dict[str, object]] = []
        skipped: list[dict[str, object]] = []
        with self._mutation_scope() as (store, admission):
            jobs, _errors = self._valid_jobs(store)
            due = sorted(
                (
                    job
                    for job in jobs
                    if job["status"] == "active"
                    and float(job.get("next_run_at") or 0.0) > 0
                    and float(job["next_run_at"]) <= current
                ),
                key=lambda job: (float(job["next_run_at"]), str(job["job_id"])),
            )
            selected = 0
            max_selected = max(1, int(limit or 1))
            for job in due:
                if selected >= max_selected:
                    break
                job_id = str(job["job_id"])
                if self._job_has_active_run(store, job_id):
                    continue
                selected += 1
                scheduled_for = float(job["next_run_at"])
                lateness = max(0.0, current - scheduled_for)
                if lateness > int(job["misfire_grace_seconds"]):
                    skipped_run = _terminal_misfire_run(
                        job, scheduled_for=scheduled_for, now=current
                    )
                    skipped.append(skipped_run)
                    store["jobs"][job_id] = _advance_after_misfire(job, current)
                    continue
                run = _reserved_run(
                    job,
                    trigger="due",
                    scheduled_for=scheduled_for,
                    dispatch_after=current,
                    now=current,
                )
                store["runs"][str(run["run_id"])] = run
                store["jobs"][job_id] = _advance_after_reservation(job, current)
                reserved.append(run)
            # A scheduler poll is read-only unless it actually reserves a run
            # or advances a misfired job.  Writing an unchanged owner ledger
            # here used to refresh ``updated_at``, resync the due projection,
            # and run a full owner-quota scan on every background owner tick.
            # Large owner homes therefore consumed a CPU core while idle.
            if reserved or skipped:
                self._write_store_unlocked(store, admission, history_records=skipped)
        return deepcopy(reserved)

    def queued_runs(self, *, now: float | None = None, limit: int = 64) -> list[dict[str, object]]:
        current = _now(now)
        store = self._store_snapshot()
        rows: list[dict[str, object]] = []
        for raw in store["runs"].values():
            run, _error = self._parse_run(raw)
            if run is None or run["status"] != "queued":
                continue
            if float(run.get("dispatch_after") or 0.0) > current:
                continue
            rows.append(run)
        rows.sort(key=lambda run: (float(run["scheduled_for"]), str(run["run_id"])))
        return deepcopy(rows[: max(1, int(limit or 1))])

    def get_active_run(self, run_id: str) -> dict[str, object] | None:
        """Return one nonterminal run without exposing the mutable store object."""

        store = self._store_snapshot()
        run, _error = self._parse_run(store["runs"].get(str(run_id or "")))
        return deepcopy(run) if run is not None else None

    # LLM: Schedule list/get surfaces must project live queued/claimed/running/waiting facts from
    # the same owner ledger; callers must not infer activity from an empty last_run_at.
    # 函数用途: 一次读取当前 owner 的全部在途定时执行，供状态展示区分排队、执行与等待后续事件。
    def active_runs_by_job(self) -> tuple[dict[str, dict[str, object]], list[str]]:
        store = self._store_snapshot()
        runs, errors = self._valid_runs(store)
        active: dict[str, dict[str, object]] = {}
        for run in sorted(
            runs,
            key=lambda item: (float(item.get("scheduled_for") or 0.0), str(item["run_id"])),
        ):
            if str(run.get("status") or "") not in _ACTIVE_RUN_STATUSES:
                continue
            active[str(run["job_id"])] = deepcopy(run)
        return active, errors

    def attach_wake_signal(self, run_id: str, wake_signal_id: str) -> None:
        with self._mutation_scope() as (store, admission):
            run = self._require_run(store, run_id)
            if run["status"] not in _ACTIVE_RUN_STATUSES:
                return
            store["runs"][run_id] = {
                **run,
                "wake_signal_id": str(wake_signal_id or ""),
                "updated_at": time.time(),
            }
            self._write_store_unlocked(store, admission)

    def claim_run(
        self,
        run_id: str,
        *,
        lease_seconds: int,
        now: float | None = None,
    ) -> dict[str, object] | None:
        current = _now(now)
        with self._mutation_scope() as (store, admission):
            raw = store["runs"].get(run_id)
            run, _error = self._parse_run(raw)
            if run is None:
                return None
            expires = float(run.get("claim_expires_at") or 0.0)
            if run["status"] == "waiting":
                return None
            if run["status"] in {"claimed", "running"} and expires > current:
                return None
            if run["status"] not in _ACTIVE_RUN_STATUSES:
                return None
            claim_id = f"claim_{uuid.uuid4().hex}"
            claimed = {
                **run,
                "status": "claimed",
                "claim_id": claim_id,
                "claimed_at": current,
                "claim_expires_at": current + max(1, int(lease_seconds or 1)),
                # P0-4: 记录持有 claim 的进程身份(崩溃恢复判活的死亡证明来源);
                # runner_start_time 供 pid 复用核对(seq1562 收口), 不可读时 None。
                "runner_pid": os.getpid(),
                "runner_start_time": _process_start_time(os.getpid()),
                "updated_at": current,
            }
            store["runs"][run_id] = claimed
            self._write_store_unlocked(store, admission)
        return deepcopy(claimed)

    def mark_run_running(self, run_id: str, claim_id: str, *, now: float | None = None) -> None:
        current = _now(now)
        with self._mutation_scope() as (store, admission):
            run = self._require_claim(store, run_id, claim_id)
            store["runs"][run_id] = {
                **run,
                "status": "running",
                "started_at": float(run.get("started_at") or current),
                "updated_at": current,
            }
            self._write_store_unlocked(store, admission)

    def heartbeat_run(
        self,
        run_id: str,
        claim_id: str,
        *,
        lease_seconds: int,
        now: float | None = None,
    ) -> bool:
        current = _now(now)
        with self._mutation_scope() as (store, admission):
            raw = store["runs"].get(run_id)
            run, _error = self._parse_run(raw)
            if run is None or run.get("claim_id") != claim_id:
                return False
            if run["status"] not in {"claimed", "running"}:
                return False
            store["runs"][run_id] = {
                **run,
                "claim_expires_at": current + max(1, int(lease_seconds or 1)),
                "updated_at": current,
            }
            self._write_store_unlocked(store, admission)
        return True

    def release_run_claim(self, run_id: str, claim_id: str, *, now: float | None = None) -> bool:
        current = _now(now)
        with self._mutation_scope() as (store, admission):
            raw = store["runs"].get(run_id)
            run, _error = self._parse_run(raw)
            if run is None or run.get("claim_id") != claim_id:
                return False
            store["runs"][run_id] = {
                **run,
                "status": "queued",
                "claim_id": "",
                "claimed_at": 0.0,
                "claim_expires_at": 0.0,
                "updated_at": current,
            }
            self._write_store_unlocked(store, admission)
        return True

    def finish_run(
        self,
        run_id: str,
        claim_id: str,
        result: SchedulerRunFinish,
    ) -> dict[str, object]:
        terminal_status = result.status
        if terminal_status not in _TERMINAL_RUN_STATUSES:
            raise SchedulerConflictError(f"invalid terminal scheduler run status: {result.status}")
        current = _now(result.now)
        with self._mutation_scope() as (store, admission):
            run = self._require_claim(store, run_id, claim_id)
            terminal = _terminal_run_payload(run, result, now=current)
            store["runs"].pop(run_id, None)
            job_id = str(run["job_id"])
            raw_job = store["jobs"].get(job_id)
            job, _error = self._parse_job(raw_job)
            if job is not None:
                store["jobs"][job_id] = _job_after_run(job, terminal, now=current)
            self._write_store_unlocked(store, admission, history_records=[terminal])
        return deepcopy(terminal)

    def history(
        self,
        *,
        job_id: str = "",
        limit: int = 50,
    ) -> tuple[list[dict[str, object]], list[str]]:
        report = read_jsonl_objects_report(self.history_path, context="scheduler.history.read")
        errors = [
            str(item.get("error_type") or "SCHEDULER_HISTORY_READ_FAILED")
            for item in report.load_errors
        ]
        rows: dict[str, dict[str, object]] = {}
        for raw in report.records:
            run, error = self._parse_run(raw, terminal=True)
            if error:
                errors.append(error)
                continue
            if run is not None and (not job_id or run["job_id"] == job_id):
                rows[str(run["run_id"])] = run
        selected = sorted(
            rows.values(),
            key=lambda row: (
                float(row.get("ended_at") or row.get("created_at") or 0.0),
                str(row["run_id"]),
            ),
            reverse=True,
        )
        return deepcopy(selected[: max(1, min(int(limit or 50), 200))]), sorted(set(errors))

    # LLM: 这是只读能力投影,只回答"当前 owner 的调度账本长什么样",不参与任何裁决;调用方
    # 是模型显式发起的 scheduler status。它曾经进两次锁、每次都在锁内把整个 store.json 读+全量
    # 解析一遍(实测 N=10000 条 run: 锁内 12.3ms + 15.4ms,并发等锁者中位 13.4ms)。现在锁外
    # 只读一次字节、算一次摘要、做一次全量解析,锁内只复核"这份字节是否仍是当前文件";复核
    # 通过就直接用锁外结论,因此稳态锁持有时间与 run 条数无关。复核不通过或探针不可用一律
    # 退回锁内权威读取,坏账本仍返回 unavailable,返回字段与旧实现逐字一致。
    # 函数用途: 汇总定时任务与活跃 run 的只读快照(active/paused 计数、schedule 种类、活跃 run 数)。
    def runtime_snapshot(self) -> dict[str, object]:
        try:
            active_jobs, paused_jobs, active_runs, errors, schedule_kinds = (
                self._runtime_snapshot_summary()
            )
        except Exception as exc:  # noqa: BLE001 - capability projection must stay read-only and bounded
            return {
                "state": "unavailable",
                "health": "unavailable",
                "active_jobs": 0,
                "paused_jobs": 0,
                "active_runs": 0,
                "load_error_codes": [type(exc).__name__],
            }
        return {
            "state": "available",
            "health": "degraded" if errors else "healthy",
            "active_jobs": active_jobs,
            "paused_jobs": paused_jobs,
            "active_runs": active_runs,
            "load_error_codes": sorted(set(errors)),
            "schedule_kinds": schedule_kinds,
        }

    # 函数用途: 产出 runtime_snapshot 的字段元组;锁外解析 + 锁内一次 stat 复核,复核不过才退回权威读取。
    def _runtime_snapshot_summary(self) -> tuple[int, int, int, list[str], list[str]]:
        probe = _probe_store_bytes(self.store_path, digest=False)
        if probe is not None:
            try:
                # 探针字节与摘要绑定同一次读取:锁内复核 stat 键相等即可确认"解析的就是当前文件版本"。
                summary = self._runtime_summary_from_store(
                    self._load_store_from_bytes_unlocked(probe.data)
                )
            except Exception:  # noqa: BLE001 - 探针本身坏 ≠ 账本坏: 交给下面的权威读取判定
                # 锁外读到的那份字节可能是旧的/瞬时截断的,而磁盘上的账本此刻已经有效。因此
                # **不能**把"探针解析失败"当成"账本不可用"(那会让 runtime_snapshot 报
                # unavailable,而真实账本明明可读),一律退回锁内权威读取。
                summary = None
            if summary is not None:
                with locked_json_path(self.store_path):
                    if _store_stat_key(self.store_path) == probe.stat_key:
                        return summary
        with locked_json_path(self.store_path):
            return self._runtime_summary_from_store(self._load_store_unlocked())

    # LLM: 只读投影的统一入口。锁内**不允许**再出现读盘或全量解析:先在锁外读一次字节并解析,
    # 锁内只用一次 stat 复核"这份字节仍是当前文件";复核不过或探针不可用才退回锁内权威读取。
    # 单次调用绑定单次读到的字节,因此不需要跨调用的缓存/摘要比对(与 runtime_snapshot 同一形状)。
    # 写路径不适用:它必须在锁内做读-改-写,不在这里改动。
    # 函数用途: 取得一份"已验证仍是当前文件"的 owner 账本快照,供各类只读投影使用。
    def _store_snapshot(self) -> dict[str, Any]:
        probe = _probe_store_bytes(self.store_path, digest=False)
        if probe is not None:
            try:
                store = self._load_store_from_bytes_unlocked(probe.data)
            except Exception:  # noqa: BLE001 - 探针坏 ≠ 账本坏: 交给下面的权威读取判定
                store = None
            if store is not None:
                with locked_json_path(self.store_path):
                    if _store_stat_key(self.store_path) == probe.stat_key:
                        return store
        with locked_json_path(self.store_path):
            return self._load_store_unlocked()

    # LLM: 纯函数式的账本投影(不做 I/O、不读共享状态),因此锁内锁外调用都不改变语义:
    # jobs 侧与 list_jobs(include_deleted=False) 同源同过滤,schedule 种类只看未删除 job;
    # runs 侧用与旧实现逐字相同的 _parse_run + 活跃状态判定,坏 run 跳过而不是让整份投影失败。
    # 函数用途: 从已解析的 store 字典里数出 runtime_snapshot 需要的计数与错误码。
    def _runtime_summary_from_store(
        self, store: dict[str, Any]
    ) -> tuple[int, int, int, list[str], list[str]]:
        jobs, errors = self._valid_jobs(store)
        active_jobs = sum(1 for job in jobs if job["status"] == "active")
        paused_jobs = sum(1 for job in jobs if job["status"] == "paused")
        schedule_kinds = sorted(
            {str(job["schedule"]["kind"]) for job in jobs if job["status"] != "deleted"}
        )
        active_runs = sum(
            1
            for raw in store["runs"].values()
            if (run := self._parse_run(raw)[0]) is not None
            and run["status"] in _ACTIVE_RUN_STATUSES
        )
        return active_jobs, paused_jobs, active_runs, errors, schedule_kinds


# LLM: Waiting is a claim-free durable phase, separate from queued/claimed/running process work.
# Keep every transition CAS-backed through the same owner store and exact scheduler run id.
# 类用途: 管理模型工作片结束后、根任务仍在继续时的等待、重启对账读取和最终结算。
class _SchedulerWaitingRunOperations:
    # LLM: Restart reconciliation needs a bounded snapshot of runs whose model slice ended while
    # their same-id conversation task stayed active. This is read-only and never scans task prose.
    # The returned rows are always deepcopies of the cached projection, so callers can neither
    # mutate cached state nor observe a projection that no longer matches store.json bytes.
    # 函数用途: 读取等待任务终态的定时执行，供 Gateway 生命周期对账。
    def waiting_runs(self) -> tuple[list[dict[str, object]], list[str]]:
        # 读盘与内容摘要在锁外完成: 锁内只做 stat 复核 + 命中判定(或对已读字节做解析),
        # 因此锁持有时间只会比"锁内 read + 全量解析"更短,不会更长。
        probe = _probe_store_bytes(self.store_path)
        with locked_json_path(self.store_path):
            projection = self._waiting_runs_projection_unlocked(probe)
        return [deepcopy(row) for row in projection.rows], list(projection.errors)

    # LLM: A model slice may legitimately yield after spawning durable child work. Preserve the
    # scheduler run as waiting without a live process claim; queued dispatch must never reclaim it.
    # 函数用途: 主代理已派工但任务仍未终结时，把定时执行停在等待后续事件状态而不是误记完成。
    def park_run_waiting(
        self,
        run_id: str,
        claim_id: str,
        *,
        response: str = "",
        delivery_status: str = "",
        delivery_reason: str = "",
        now: float | None = None,
    ) -> dict[str, object]:
        current = _now(now)
        with self._mutation_scope() as (store, admission):
            run = self._require_claim(store, run_id, claim_id)
            waiting = {
                **run,
                "status": "waiting",
                "claim_id": "",
                "claim_expires_at": 0.0,
                "waiting_since": current,
                "response": str(response or "")[:4000],
                "delivery_status": str(delivery_status or "")[:80],
                "delivery_reason": str(delivery_reason or "")[:160],
                "updated_at": current,
            }
            store["runs"][run_id] = waiting
            self._write_store_unlocked(store, admission)
        return deepcopy(waiting)

    # LLM: Only the exact waiting scheduler run may be closed without a process claim. The caller
    # must first prove the same-id durable conversation task reached a structured terminal state.
    # 函数用途: 后续生命周期或重启对账确认任务终态后，结算等待中的定时执行。
    def finish_waiting_run(
        self,
        run_id: str,
        result: SchedulerRunFinish,
    ) -> dict[str, object]:
        terminal_status = result.status
        if terminal_status not in _TERMINAL_RUN_STATUSES:
            raise SchedulerConflictError(f"invalid terminal scheduler run status: {result.status}")
        current = _now(result.now)
        with self._mutation_scope() as (store, admission):
            run = self._require_run(store, run_id)
            if str(run.get("status") or "") != "waiting":
                raise SchedulerConflictError("scheduler run is not waiting for task completion")
            terminal = _terminal_run_payload(run, result, now=current)
            store["runs"].pop(run_id, None)
            job_id = str(run["job_id"])
            raw_job = store["jobs"].get(job_id)
            job, _error = self._parse_job(raw_job)
            if job is not None:
                store["jobs"][job_id] = _job_after_run(job, terminal, now=current)
            self._write_store_unlocked(store, admission, history_records=[terminal])
        return deepcopy(terminal)


class _SchedulerStoreSupport:
    """Private owner-store parsing, locking, quota admission, and validation support."""

    @contextmanager
    def _mutation_scope(self) -> Iterator[tuple[dict[str, Any], OwnerQuotaAdmission]]:
        """Lock quota before the scheduler authority to keep one lock order."""

        with self.quota_enforcer.admission() as admission:
            with locked_json_path(self.store_path):
                yield self._load_store_unlocked(), admission

    def _load_store_unlocked(self) -> dict[str, Any]:
        report = read_json_object_report(self.store_path, context="scheduler.store.read")
        return self._validate_store_unlocked(report.payload, report.load_error)

    def _load_store_from_bytes_unlocked(self, data: bytes) -> dict[str, Any]:
        """从"已经读进内存的 store.json 字节"解析,语义与 _load_store_unlocked 逐字一致。

        LLM: 缓存路径必须先拿到内容摘要再解析,若先摘要后重新读盘,解析到的字节和摘要绑定的
        字节可能不是同一份(外部原地改写窗口)→ 会缓存一份"内容摘要属于新文件、结果属于旧
        文件"的投影。这里让"读一次字节"同时供摘要和解析使用,绑定天然精确。坏 JSON/非对象/
        缺字段的处理与 read_json_object_report 同款(runtime_error_report + path),
        因此两条路径的异常语义不漂移,见 tests/test_scheduler_scan_costs.py 的一致性用例。
        """
        payload, load_error = _parse_json_object_bytes(self.store_path, data)
        return self._validate_store_unlocked(payload, load_error)

    def _validate_store_unlocked(
        self,
        payload: dict[str, Any],
        load_error: dict[str, object] | None,
    ) -> dict[str, Any]:
        """校验已解析的 store 负载并返回权威字典(空负载 → 空账本默认值)。"""
        if load_error is not None:
            raise SchedulerStateError("owner scheduler store is unreadable")
        if not payload:
            return {
                "schema_version": _STORE_SCHEMA,
                "owner": dict(self.owner),
                "jobs": {},
                "runs": {},
                "updated_at": 0.0,
            }
        store = payload
        if store.get("schema_version") != _STORE_SCHEMA:
            raise SchedulerStateError("unsupported owner scheduler store schema")
        if store.get("owner") != self.owner:
            raise SchedulerStateError("owner scheduler store identity mismatch")
        if not isinstance(store.get("jobs"), dict) or not isinstance(store.get("runs"), dict):
            raise SchedulerStateError("owner scheduler store jobs/runs must be objects")
        return store

    def _waiting_runs_projection_unlocked(self, probe: _StoreBytesProbe | None) -> _WaitingRunsProjection:
        """读 store.json 并返回 waiting 行投影;未变化的数据不重复解析。

        LLM: 命中条件是"锁内 stat 键与缓存一致 + 锁外读到的字节与锁内 stat 一致 + 字节摘要
        与缓存一致",三条都要成立:
        ① stat 键(mtime_ns/size/inode/device)覆盖"文件被替换/被截断/被原子改写";
        ② 锁外探针与锁内 stat 一致,保证"摘要绑定的那份字节"就是当前文件;
        ③ 内容摘要覆盖"时间戳粒度粗的文件系统上同尺寸原地改写"。
        任一条不成立都退到 _parse_waiting_projection_unlocked 全量解析(①②不成立时还打结构化
        告警/guard 计数),绝不返回陈旧结论。投影只缓存 waiting 行与错误码,调用方每次拿到 deepcopy。
        """
        cache_key = self.waiting_projection_cache_key()
        stat_key = _store_stat_key(self.store_path)
        if stat_key is None:
            # 文件缺失/不可 stat:不能绑定任何投影,冷路径(语义: 空账本或结构化读失败)。
            _drop_waiting_projection(cache_key)
            return self._parse_waiting_projection_unlocked(None)
        usable_probe = probe if (probe is not None and probe.stat_key == stat_key) else None
        if usable_probe is not None:
            hit, stale = _lookup_waiting_projection(
                cache_key, stat_key=stat_key, digest=usable_probe.digest
            )
            if hit is not None:
                _bump_projection_stat("hit")
                return hit
            if stale:
                _bump_projection_stat("guard")
                _LOGGER.warning(
                    "scheduler waiting projection guard fallback: stat key unchanged but content "
                    "digest differs; re-parsing owner store",
                    extra={
                        "event": "scheduler_waiting_projection_guard",
                        "store_path": str(self.store_path),
                        "stat_key": stat_key,
                    },
                )
                _drop_waiting_projection(cache_key)
        return self._parse_waiting_projection_unlocked(usable_probe)

    def waiting_projection_cache_key(self) -> tuple[str, str, str, str]:
        """投影缓存键: 账本路径 + owner 身份(owner 参与逐条校验,不能跨 owner 复用)。"""
        return (
            str(self.store_path),
            str(self.owner.get("provider") or ""),
            str(self.owner.get("kind") or ""),
            str(self.owner.get("id") or ""),
        )

    def _parse_waiting_projection_unlocked(
        self,
        probe: _StoreBytesProbe | None,
    ) -> _WaitingRunsProjection:
        """全量解析 store.json 并(可选)写入有界投影缓存;任何异常都不留下脏缓存。

        ``probe`` 是锁外已经读好并算好摘要的那份字节(与锁内 stat 一致时可用):解析因此不需要
        在锁内做 I/O 或哈希,锁持有时间不会比改动前更长。probe 不可用就回退唯一权威读取路径
        (锁内 read_json_object_report),由它给结构化结果;这条回退路不写缓存。
        """
        cache_key = self.waiting_projection_cache_key()
        try:
            if probe is not None:
                store = self._load_store_from_bytes_unlocked(probe.data)
            else:
                store = self._load_store_unlocked()
            runs, errors = self._valid_runs(store)
        except Exception:
            _drop_waiting_projection(cache_key)
            raise
        rows = tuple(_select_waiting_runs(runs))
        projection = _WaitingRunsProjection(
            stat_key=probe.stat_key if probe is not None else (-1, -1, -1, -1),
            digest=probe.digest if probe is not None else "",
            rows=rows,
            errors=tuple(errors),
        )
        _bump_projection_stat("miss")
        if probe is None or len(rows) > _WAITING_PROJECTION_MAX_ROWS:
            # 字节不可用 / 超界行数: 不缓存,退化为逐次全量解析(与改动前同,不会更差)。
            _bump_projection_stat("skip")
            return projection
        if _store_stat_key(self.store_path) != probe.stat_key:
            # 解析期间账本又被改写: 本次结论照常返回,但不写缓存(下次重新解析,不赌)。
            _bump_projection_stat("skip")
            return projection
        with _WAITING_PROJECTION_LOCK:
            _WAITING_PROJECTION_CACHE[cache_key] = projection
            _WAITING_PROJECTION_CACHE.move_to_end(cache_key)
            while len(_WAITING_PROJECTION_CACHE) > _WAITING_PROJECTION_MAX_ENTRIES:
                _WAITING_PROJECTION_CACHE.popitem(last=False)
        return projection

    def _write_store_unlocked(
        self,
        store: dict[str, Any],
        admission: OwnerQuotaAdmission,
        *,
        history_records: list[dict[str, object]] | None = None,
    ) -> None:
        store["updated_at"] = time.time()
        history_rows = self._new_history_rows_unlocked(history_records or [])
        store_blob = json.dumps(store, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        history_blob = "".join(
            json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in history_rows
        )
        changes = [OwnerQuotaChange(self.store_path, len(store_blob.encode("utf-8")))]
        if history_blob:
            changes.append(
                OwnerQuotaChange(
                    self.history_path,
                    len(history_blob.encode("utf-8")),
                    append=True,
                )
            )
        admission.check(changes)
        # The SQLite row is a wake-up projection, not job authority.  Publish it
        # before the owner ledger so a failed index update cannot return a false
        # "scheduled" success; a stale projection from a later owner-write
        # failure is harmless because execution always revalidates store.json.
        self._sync_due_index_unlocked(store)
        if history_rows:
            append_jsonl_records(self.history_path, history_rows)
        write_json_file_atomic_unlocked(self.store_path, store)

    def _sync_due_index_unlocked(self, store: dict[str, Any]) -> None:
        if self.due_index is not None:
            self.due_index.sync_owner(self.due_owner, store)

    def corruption_report(self) -> dict[str, object]:
        """P1-3(HANDOFF 文档线): 结构化损坏报告——坏 job/run 被隔离后仍可查询。

        坏记录(schema 不符/截断写/手改)由逐条解析跳过并在此汇总, 不再静默
        丢弃; 不覆盖原文件(只读)。健康记录照常调度(隔离不瘫痪)。
        """
        store = self._store_snapshot()
        _jobs, job_errors = self._valid_jobs(store)
        _runs, run_errors = self._valid_runs(store)
        return {
            "schema_version": store.get("schema_version"),
            "owner": dict(store.get("owner") or {}),
            "corrupt_jobs": job_errors,
            "corrupt_runs": run_errors,
            "corrupt": bool(job_errors or run_errors),
        }

    def recover_interrupted_executions(
        self,
        *,
        now: float | None = None,
    ) -> list[str]:
        """P0-4(HANDOFF 文档线): 崩溃 run 按进程死亡证明归 unknown 终态。

        仅当租约已过期(claim_expires_at <= now)且 runner 进程被证实死亡
        (pid 探活失败)才置 unknown; 进程仍存活(心跳慢/时钟偏差)保持原态
        (fail-closed 不猜)。返回本次归 unknown 的 run_id 列表。
        """
        current = _now(now)
        recovered: list[str] = []
        with self._mutation_scope() as (store, admission):
            for run_id, raw in list(store["runs"].items()):
                run, _error = self._parse_run(raw)
                if run is None:
                    continue
                if run["status"] not in {"claimed", "running"}:
                    continue
                if float(run.get("claim_expires_at") or 0.0) > current:
                    continue  # 租约未过期, 不是崩溃候选
                # P0-4 收口(seq1562): fail-closed 判定——
                # ① unverifiable(pid 缺失/<=0/OSError): 保持原态(不归 unknown)
                # ② alive 且 start_time 匹配(可核对时): 保持原态
                # ③ alive 但 start_time 不匹配: pid 复用 = 原进程已死(死亡证明)
                # ④ 仅 dead(ProcessLookupError 明确查无此进程)才是死亡证明
                pid = int(run.get("runner_pid") or 0)
                recorded_start = run.get("runner_start_time")
                state = _process_state(pid)
                if state == "unverifiable":
                    continue  # 不可证实, fail-closed 保持原态
                if state == "alive":
                    if recorded_start is None:
                        continue  # 无 start_time 可核对: 仅 pid 存活即保持
                    current_start = _process_start_time(pid)
                    if current_start is None or current_start == float(recorded_start):
                        continue  # 同一进程存活(或当前不可读), 保持原态
                    # current_start 可读且 != recorded_start: pid 复用, 原进程已死
                store["runs"][run_id] = {
                    **run,
                    "status": "unknown",
                    "ended_at": current,
                    "updated_at": current,
                }
                recovered.append(run_id)
            if recovered:
                self._write_store_unlocked(store, admission)
        return recovered

    def _valid_runs(self, store: dict[str, Any]) -> tuple[list[dict[str, object]], list[str]]:
        """逐条解析 runs: 坏记录跳过并收集错误(与 _valid_jobs 同容错模式)。"""
        runs: list[dict[str, object]] = []
        errors: list[str] = []
        for raw in store["runs"].values():
            run, error = self._parse_run(raw)
            if run is not None:
                runs.append(run)
            if error:
                errors.append(error)
        return runs, sorted(set(errors))

    def _valid_jobs(self, store: dict[str, Any]) -> tuple[list[dict[str, object]], list[str]]:
        jobs: list[dict[str, object]] = []
        errors: list[str] = []
        for raw in store["jobs"].values():
            job, error = self._parse_job(raw)
            if job is not None:
                jobs.append(job)
            if error:
                errors.append(error)
        return jobs, sorted(set(errors))

    def _parse_job(self, raw: object) -> tuple[dict[str, object] | None, str]:
        if not isinstance(raw, dict):
            return None, "SCHEDULER_JOB_INVALID"
        try:
            if raw.get("schema_version") != _JOB_SCHEMA:
                raise ValueError("job schema")
            if raw.get("owner") != self.owner:
                return None, "SCHEDULER_OWNER_MISMATCH"
            if not str(raw.get("job_id") or "").startswith("job_"):
                raise ValueError("job id")
            if str(raw.get("status") or "") not in _JOB_STATUSES:
                raise ValueError("job status")
            if not str(raw.get("thread_id") or ""):
                raise ValueError("thread")
            validate_schedule(raw.get("schedule"))
            _validate_name_prompt(str(raw.get("name") or ""), str(raw.get("prompt") or ""))
            _normalize_skill_refs(raw.get("skill_refs"))
            if int(raw.get("version") or 0) < 1:
                raise ValueError("job version")
            return dict(raw), ""
        except (TypeError, ValueError, ScheduleValidationError):
            return None, "SCHEDULER_JOB_INVALID"

    def _parse_run(
        self,
        raw: object,
        *,
        terminal: bool = False,
    ) -> tuple[dict[str, object] | None, str]:
        if not isinstance(raw, dict):
            return None, "SCHEDULER_RUN_INVALID"
        try:
            if raw.get("schema_version") != _RUN_SCHEMA:
                raise ValueError("run schema")
            if raw.get("owner") != self.owner:
                return None, "SCHEDULER_OWNER_MISMATCH"
            if not str(raw.get("run_id") or "").startswith("srun_"):
                raise ValueError("run id")
            allowed = _TERMINAL_RUN_STATUSES if terminal else _ACTIVE_RUN_STATUSES
            if str(raw.get("status") or "") not in allowed:
                raise ValueError("run status")
            if not str(raw.get("job_id") or "").startswith("job_"):
                raise ValueError("job id")
            return dict(raw), ""
        except (TypeError, ValueError):
            return None, "SCHEDULER_RUN_INVALID"

    def _require_job(self, store: dict[str, Any], job_id: str) -> dict[str, object]:
        normalized = str(job_id or "").strip()
        job, _error = self._parse_job(store["jobs"].get(normalized))
        if job is None:
            raise SchedulerNotFoundError(f"scheduler job not found: {normalized}")
        return job

    def _require_mutable_job(
        self,
        store: dict[str, Any],
        job_id: str,
        expected_version: int | None,
    ) -> dict[str, object]:
        job = self._require_job(store, job_id)
        if job["status"] == "deleted":
            raise SchedulerNotFoundError(f"scheduler job not found: {job_id}")
        if expected_version is not None and int(job["version"]) != int(expected_version):
            raise SchedulerConflictError(
                f"scheduler job version changed: expected {expected_version}, current {job['version']}"
            )
        return job

    def _require_run(self, store: dict[str, Any], run_id: str) -> dict[str, object]:
        run, _error = self._parse_run(store["runs"].get(str(run_id or "")))
        if run is None:
            raise SchedulerNotFoundError(f"scheduler run not found: {run_id}")
        return run

    def _require_claim(
        self, store: dict[str, Any], run_id: str, claim_id: str
    ) -> dict[str, object]:
        run = self._require_run(store, run_id)
        if run.get("claim_id") != str(claim_id or ""):
            raise SchedulerConflictError("scheduler run claim changed")
        return run

    def _job_has_active_run(self, store: dict[str, Any], job_id: str) -> bool:
        for raw in store["runs"].values():
            run, _error = self._parse_run(raw)
            if run is not None and run["job_id"] == job_id:
                return True
        return False

    def _cancel_waiting_runs_unlocked(
        self,
        store: dict[str, Any],
        job_id: str,
        current: float,
        error_code: str,
    ) -> list[dict[str, object]]:
        cancelled: list[dict[str, object]] = []
        for run_id, raw in list(store["runs"].items()):
            run, _error = self._parse_run(raw)
            if run is None or run["job_id"] != job_id or run["status"] in {
                "running",
                "waiting",
            }:
                continue
            terminal = {
                **run,
                "status": "cancelled",
                "ended_at": current,
                "updated_at": current,
                "error_code": error_code,
                "error_message": "",
                "claim_expires_at": 0.0,
            }
            cancelled.append(terminal)
            store["runs"].pop(run_id, None)
        return cancelled

    def _new_history_rows_unlocked(
        self,
        records: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        if not records:
            return []
        existing = {
            str(item.get("run_id") or "")
            for item in read_jsonl_objects_report(
                self.history_path,
                context="scheduler.history.dedupe",
            ).records
        }
        return [record for record in records if str(record.get("run_id") or "") not in existing]


class SchedulerRepository(
    _SchedulerJobOperations,
    _SchedulerRunOperations,
    _SchedulerWaitingRunOperations,
    _SchedulerStoreSupport,
):
    """Single owner-local facade over job, run, and store responsibilities."""

    def __init__(
        self,
        root: str | Path,
        *,
        owner_provider: str,
        owner_kind: str,
        owner_id: str,
        due_owner_id: str = "",
        default_timezone: str = "",
        quota_enforcer: OwnerQuotaEnforcer | None = None,
        due_index: SchedulerDueIndex | None = None,
    ) -> None:
        self.root = Path(root).expanduser().resolve(strict=False)
        self.store_path = self.root / "store.json"
        self.history_path = self.root / "history.jsonl"
        self.owner = {
            "provider": str(owner_provider or "local"),
            "kind": str(owner_kind or "main"),
            "id": str(owner_id or "local/main"),
        }
        self.due_owner = {
            "provider": str(owner_provider or "local"),
            "kind": str(owner_kind or "main"),
            "id": str(due_owner_id or owner_id or "main"),
        }
        self.default_timezone = str(default_timezone or "")
        self.quota_enforcer = quota_enforcer or OwnerQuotaEnforcer(
            self.root,
            max_bytes=0,
        )
        self.due_index = due_index
        self.root.mkdir(parents=True, exist_ok=True)


# LLM: One parsed ``waiting_runs`` result bound to the exact store.json bytes it was parsed from.
# ``stat_key``/``digest`` are the invalidation evidence; ``rows``/``errors`` are the projection.
# A projection is never authority: every mutation still writes store.json and every hit still
# re-verifies the file's stat key and content digest before it is served.
# 类用途: 让每轮对账不再重复解析没变过的 store.json,同时保证外部改写立刻可见。
@dataclass(frozen=True)
class _WaitingRunsProjection:
    stat_key: tuple[int, int, int, int]
    digest: str
    rows: tuple[dict[str, object], ...]
    errors: tuple[str, ...]


# LLM: One lock-free read of store.json bound to the stat key observed just before it. The probe
# is only usable while the in-lock stat still matches, which is what lets the locked section parse
# already-read bytes instead of doing I/O and hashing while holding the owner lock.
# 类用途: 把"读盘 + 内容摘要"移出临界区,锁内只做版本复核与命中判定。
@dataclass(frozen=True)
class _StoreBytesProbe:
    stat_key: tuple[int, int, int, int]
    digest: str
    data: bytes


def _store_stat_key(path: Path) -> tuple[int, int, int, int] | None:
    """store.json 的 (mtime_ns, size, inode, device) 指纹;不可读返回 None。"""
    try:
        status = path.stat()
    except OSError:
        return None
    return (status.st_mtime_ns, status.st_size, status.st_ino, status.st_dev)


def _read_store_bytes(path: Path) -> bytes | None:
    """读 store.json 原始字节(摘要与解析共用同一份,保证绑定精确);失败返回 None。"""
    try:
        return path.read_bytes()
    except OSError:
        return None


def _store_bytes_digest(data: bytes) -> str:
    """内容摘要(blake2b-128): 覆盖时间戳粒度粗的文件系统上的同尺寸原地改写。"""
    return hashlib.blake2b(data, digest_size=16).hexdigest()


def _probe_store_bytes(path: Path, *, digest: bool = True) -> _StoreBytesProbe | None:
    """锁外探针: 一次 stat + 一次读字节 (+ 可选内容摘要),三者绑定同一份内容。

    LLM: 命中判定要求"探针 stat == 锁内 stat",所以锁外读到的字节与锁内看到的文件版本一致;
    不一致(外部改写竞态)时调用方会丢弃探针,回退锁内权威读取路径。返回 None 表示文件缺失/
    不可读,调用方同样走权威路径。

    ``digest=False`` 只服务"单次调用、锁内只复核 stat 键"的读者(见 ``_store_snapshot``): 这类
    读者不复用结论,摘要算出来也没人比对,而 4-5MB 账本上 blake2b 要花几毫秒——不必要地拖慢每次
    读取。跨调用缓存(waiting 投影)必须保持 digest=True:它的命中判定依赖内容摘要兜住"同 stat
    不同内容"。
    """
    stat_key = _store_stat_key(path)
    if stat_key is None:
        return None
    data = _read_store_bytes(path)
    if data is None:
        return None
    return _StoreBytesProbe(
        stat_key=stat_key,
        digest=_store_bytes_digest(data) if digest else "",
        data=data,
    )


def _parse_json_object_bytes(
    path: Path,
    data: bytes,
) -> tuple[dict[str, Any], dict[str, object] | None]:
    """把原始字节解析成 JSON 对象,错误语义与 common.json_io.read_json_object_report 一致。

    返回 (payload, load_error): 坏 JSON/非法 UTF-8/根不是对象 → payload={} + 结构化错误;
    合法对象 → (payload, None)。调用方据此抛 SchedulerStateError,行为与权威读取路径相同。
    """
    context = "scheduler.store.read"
    try:
        payload = json.loads(data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        report = runtime_error_report(exc, context=context)
        report["path"] = str(path)
        return {}, report
    if isinstance(payload, dict):
        return payload, None
    report = runtime_error_report(
        ValueError(f"JSON root is {type(payload).__name__}, expected object"),
        context=context,
    )
    report["path"] = str(path)
    return {}, report


def _bump_projection_stat(name: str) -> None:
    """投影缓存结构化计数(命中/回退/守卫/跳过);缺键容忍,计数永不影响主链路。"""
    with _WAITING_PROJECTION_LOCK:
        _WAITING_PROJECTION_STATS[name] = int(_WAITING_PROJECTION_STATS.get(name, 0)) + 1


def _cached_waiting_projection(cache_key: tuple[str, str, str, str]) -> _WaitingRunsProjection | None:
    """读投影缓存条目(只读快照,不改变 LRU 顺序;命中判定请走 _lookup_waiting_projection)。"""
    with _WAITING_PROJECTION_LOCK:
        return _WAITING_PROJECTION_CACHE.get(cache_key)


# LLM: 命中判定与 LRU 更新必须**同锁原子**完成。投影缓存是多 owner 共享的(每个 owner 的
# store.json 文件锁互不互斥),旧实现"先 lookup 释放锁、再单独 move_to_end"中间会被其它 owner
# 的写入淘汰掉这条 key(上限只有 _WAITING_PROJECTION_MAX_ENTRIES 条),随后 move_to_end 直接
# KeyError 冒到等待任务对账路径。这里把"读条目 + 核对 stat 键 + 核对内容摘要 + 提到最新"放进
# 同一把锁:条目要么整条命中(并原子提级),要么整条不命中,不存在"确认过却又消失"的中间态。
# 函数用途: 原子判定投影是否命中;第二个返回值表示"有条目但内容摘要已变"(调用方据此走守卫回退)。
def _lookup_waiting_projection(
    cache_key: tuple[str, str, str, str],
    *,
    stat_key: tuple[int, int, int, int],
    digest: str,
) -> tuple[_WaitingRunsProjection | None, bool]:
    with _WAITING_PROJECTION_LOCK:
        entry = _WAITING_PROJECTION_CACHE.get(cache_key)
        if entry is None or entry.stat_key != stat_key:
            return None, False
        if entry.digest != digest:
            return None, True
        _WAITING_PROJECTION_CACHE.move_to_end(cache_key)
        return entry, False


def _drop_waiting_projection(cache_key: tuple[str, str, str, str]) -> None:
    """主动丢弃缓存条目(解析失败/内容摘要守卫/文件消失时的保守回退)。"""
    with _WAITING_PROJECTION_LOCK:
        _WAITING_PROJECTION_CACHE.pop(cache_key, None)


def _select_waiting_runs(runs: list[dict[str, object]]) -> list[dict[str, object]]:
    """从逐条解析结果中筛出 waiting 行并按 (waiting_since, run_id) 排序(逐字保留原语义)。"""
    selected = [run for run in runs if str(run.get("status") or "") == "waiting"]
    selected.sort(
        key=lambda item: (
            float(item.get("waiting_since") or item.get("started_at") or 0.0),
            str(item["run_id"]),
        )
    )
    return selected


def _process_state(pid: int) -> str:
    """进程状态三态(os.kill 信号 0, P0-4 崩溃恢复的死亡证明, seq1562 收口)。

    alive: 进程存在(含 PermissionError=有进程但无权)。
    dead: ProcessLookupError=明确查无此进程/已被回收(唯一死亡证明)。
    unverifiable: 其它 OSError 或 pid<=0——调用方必须 fail-closed 保持
    原态, 不得据此归 unknown。
    """
    if pid <= 0:
        return "unverifiable"
    try:
        os.kill(pid, 0)
        return "alive"
    except ProcessLookupError:
        return "dead"
    except PermissionError:
        return "alive"
    except OSError:
        return "unverifiable"


def _process_start_time(pid: int) -> float | None:
    """进程启动时刻(Linux /proc/<pid>/stat 字段 22, starttime ticks)。

    供 P0-4 的 pid 复用核对: pid 存活但启动时刻与 claim 记录不匹配 = 原
    进程已死、pid 被复用(死亡证明)。跨平台不可读时返回 None(调用方仅做
    pid 探活, fail-closed 不猜)。
    """
    try:
        stat = Path(f"/proc/{int(pid)}/stat").read_text(encoding="utf-8")
        fields = stat.rsplit(")", 1)[-1].split()
        return float(fields[19])  # starttime 是第 22 个字段(0-based 19)
    except (OSError, ValueError, IndexError, TypeError):
        return None


def _new_job_payload(
    *,
    owner: dict[str, str],
    request: SchedulerJobCreateRequest,
    now: float,
) -> dict[str, object]:
    normalized_name, normalized_prompt = _validate_name_prompt(request.name, request.prompt)
    normalized_schedule = validate_schedule(request.schedule)
    normalized_refs = _normalize_skill_refs(request.skill_refs)
    thread = request.thread_id
    if not thread:
        raise ScheduleValidationError("thread_id is required")
    grace = _grace_seconds(request.misfire_grace_seconds, normalized_schedule)
    next_run = _initial_next_run(normalized_schedule, now)
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "name": normalized_name,
                "prompt": normalized_prompt,
                "thread_id": thread,
                "schedule": normalized_schedule,
                "skill_refs": normalized_refs,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": _JOB_SCHEMA,
        "job_id": f"job_{uuid.uuid4().hex}",
        "owner": dict(owner),
        "name": normalized_name,
        "prompt": normalized_prompt,
        "thread_id": thread,
        "source_task_id": request.source_task_id,
        "schedule": normalized_schedule,
        "status": "active",
        "next_run_at": next_run,
        "misfire_grace_seconds": grace,
        "delivery": {"mode": "origin"},
        "skill_refs": normalized_refs,
        "source_request_id": request.source_request_id,
        "create_fingerprint": fingerprint,
        "version": 1,
        "created_at": now,
        "updated_at": now,
        "paused_at": 0.0,
        "paused_reason": "",
        "deleted_at": 0.0,
        "last_run_at": 0.0,
        "last_run_status": "",
        "last_error_code": "",
    }


def _updated_job(
    job: dict[str, object], patch: dict[str, object], *, now: float
) -> dict[str, object]:
    name = str(patch.get("name") if "name" in patch else job["name"])
    prompt = str(patch.get("prompt") if "prompt" in patch else job["prompt"])
    normalized_name, normalized_prompt = _validate_name_prompt(name, prompt)
    schedule = validate_schedule(patch.get("schedule") if "schedule" in patch else job["schedule"])
    skill_refs = _normalize_skill_refs(
        patch.get("skill_refs") if "skill_refs" in patch else job.get("skill_refs")
    )
    grace = _grace_seconds(
        patch.get("misfire_grace_seconds")
        if "misfire_grace_seconds" in patch
        else job.get("misfire_grace_seconds"),
        schedule,
    )
    next_run = float(job.get("next_run_at") or 0.0)
    if schedule != job["schedule"]:
        next_run = _initial_next_run(schedule, now)
    return {
        **job,
        "name": normalized_name,
        "prompt": normalized_prompt,
        "schedule": schedule,
        "skill_refs": skill_refs,
        "misfire_grace_seconds": grace,
        "next_run_at": next_run,
        "updated_at": now,
        "version": int(job["version"]) + 1,
    }


def _reserved_run(
    job: dict[str, object],
    *,
    trigger: str,
    scheduled_for: float,
    dispatch_after: float,
    now: float,
) -> dict[str, object]:
    return {
        "schema_version": _RUN_SCHEMA,
        "run_id": f"srun_{uuid.uuid4().hex}",
        "job_id": str(job["job_id"]),
        "job_version": int(job["version"]),
        "owner": deepcopy(job["owner"]),
        "thread_id": str(job["thread_id"]),
        "source_task_id": str(job.get("source_task_id") or ""),
        "name": str(job["name"]),
        "prompt": str(job["prompt"]),
        "skill_refs": deepcopy(job.get("skill_refs") or []),
        "trigger": trigger,
        "scheduled_for": float(scheduled_for),
        "dispatch_after": float(dispatch_after),
        "status": "queued",
        "wake_signal_id": "",
        "claim_id": "",
        "claimed_at": 0.0,
        "claim_expires_at": 0.0,
        "created_at": now,
        "updated_at": now,
        "started_at": 0.0,
        "ended_at": 0.0,
        "delivery_status": "",
        "delivery_reason": "",
        "response": "",
        "error_code": "",
        "error_message": "",
    }


def _terminal_misfire_run(
    job: dict[str, object],
    *,
    scheduled_for: float,
    now: float,
) -> dict[str, object]:
    return {
        **_reserved_run(
            job,
            trigger="due",
            scheduled_for=scheduled_for,
            dispatch_after=now,
            now=now,
        ),
        "status": "skipped",
        "ended_at": now,
        "error_code": "SCHEDULER_MISFIRE_GRACE_EXPIRED",
        "error_message": "scheduled occurrence exceeded its configured misfire grace",
    }


def _advance_after_reservation(job: dict[str, object], now: float) -> dict[str, object]:
    schedule = validate_schedule(job["schedule"])
    next_run = (
        0.0 if schedule["kind"] == "at" else float(compute_next_run(schedule, after=now) or 0.0)
    )
    return {
        **job,
        "next_run_at": next_run,
        "updated_at": now,
        "version": int(job["version"]) + 1,
    }


def _advance_after_misfire(job: dict[str, object], now: float) -> dict[str, object]:
    schedule = validate_schedule(job["schedule"])
    if schedule["kind"] == "at":
        return {
            **job,
            "status": "paused",
            "next_run_at": 0.0,
            "paused_at": now,
            "paused_reason": "misfire_grace_expired",
            "updated_at": now,
            "version": int(job["version"]) + 1,
            "last_run_at": now,
            "last_run_status": "skipped",
            "last_error_code": "SCHEDULER_MISFIRE_GRACE_EXPIRED",
        }
    return {
        **job,
        "next_run_at": float(compute_next_run(schedule, after=now) or 0.0),
        "updated_at": now,
        "version": int(job["version"]) + 1,
        "last_run_at": now,
        "last_run_status": "skipped",
        "last_error_code": "SCHEDULER_MISFIRE_GRACE_EXPIRED",
    }


def _job_after_run(
    job: dict[str, object],
    run: dict[str, object],
    *,
    now: float,
) -> dict[str, object]:
    updated = {
        **job,
        "last_run_at": now,
        "last_run_status": str(run["status"]),
        "last_error_code": str(run.get("error_code") or ""),
        "updated_at": now,
        "version": int(job["version"]) + 1,
    }
    if (
        job["status"] != "deleted"
        and run.get("trigger") == "due"
        and validate_schedule(job["schedule"])["kind"] == "at"
    ):
        updated.update(
            {
                "status": "paused",
                "paused_at": now,
                "paused_reason": "one_shot_finished",
                "next_run_at": 0.0,
            }
        )
    return updated


# LLM: Claimed and waiting completion paths share this bounded terminal payload. Lifecycle
# authority remains at their callers; this helper only normalizes persisted result fields.
# 函数用途: 生成一条定时执行的统一终态记录，避免不同收口入口写出不同字段。
def _terminal_run_payload(
    run: dict[str, object],
    result: SchedulerRunFinish,
    *,
    now: float,
) -> dict[str, object]:
    return {
        **run,
        "status": result.status,
        "ended_at": now,
        "updated_at": now,
        "claim_id": "",
        "claim_expires_at": 0.0,
        "response": result.response[:4000],
        "delivery_status": result.delivery_status[:80],
        "delivery_reason": result.delivery_reason[:160],
        "error_code": result.error_code[:120],
        "error_message": result.error_message[:500],
    }


def _resume_next_run(job: dict[str, object], current: float) -> float:
    schedule = validate_schedule(job["schedule"])
    if schedule["kind"] == "at":
        target = schedule_timestamp(schedule)
        if current - target > int(job["misfire_grace_seconds"]):
            raise ScheduleValidationError(
                "one-shot time is outside its misfire grace; update the schedule first"
            )
        return max(target, current + _MANUAL_DISPATCH_DELAY_SECONDS)
    return float(compute_next_run(schedule, after=current) or 0.0)


def _initial_next_run(schedule: dict[str, object], current: float) -> float:
    if schedule["kind"] == "at":
        target = schedule_timestamp(schedule)
        if target <= current:
            # 通道运行时's one-shot timestamp validator reports both the parsed
            # value and the current clock.  That makes a failed model call
            # self-correcting without accepting ambiguous relative numbers or
            # relying on prompt wording as runtime authority.
            target_iso = datetime.fromtimestamp(target, timezone.utc).isoformat().replace(
                "+00:00", "Z"
            )
            current_iso = datetime.fromtimestamp(current, timezone.utc).isoformat().replace(
                "+00:00", "Z"
            )
            raise ScheduleValidationError(
                "one-shot time is in the past: "
                f"{target_iso}. Current time: {current_iso}. "
                "Use a future absolute ISO-8601 timestamp."
            )
        return target
    next_run = compute_next_run(schedule, after=current)
    if next_run is None:
        raise ScheduleValidationError("schedule has no future occurrence")
    return float(next_run)


def _grace_seconds(value: object, schedule: dict[str, object]) -> int:
    if value in (None, ""):
        return default_misfire_grace_seconds(schedule)
    if isinstance(value, bool):
        raise ScheduleValidationError("misfire_grace_seconds must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ScheduleValidationError("misfire_grace_seconds must be an integer") from exc
    if not 0 <= parsed <= 7 * 24 * 60 * 60:
        raise ScheduleValidationError("misfire_grace_seconds must be between 0 and 604800")
    return parsed


def _validate_name_prompt(name: str, prompt: str) -> tuple[str, str]:
    normalized_name = " ".join(str(name or "").split())
    normalized_prompt = str(prompt or "").strip()
    if not normalized_name:
        raise ScheduleValidationError("name is required")
    if len(normalized_name) > _MAX_NAME_CHARS:
        raise ScheduleValidationError(f"name exceeds {_MAX_NAME_CHARS} characters")
    if not normalized_prompt:
        raise ScheduleValidationError("prompt is required")
    if len(normalized_prompt) > _MAX_PROMPT_CHARS:
        raise ScheduleValidationError(f"prompt exceeds {_MAX_PROMPT_CHARS} characters")
    if "\x00" in normalized_prompt:
        raise ScheduleValidationError("prompt contains a NUL byte")
    return normalized_name, normalized_prompt


def _normalize_skill_refs(raw: object) -> list[dict[str, str]]:
    rows = raw if isinstance(raw, list) else []
    if len(rows) > _MAX_SKILL_REFS:
        raise ScheduleValidationError(f"skill_refs exceeds {_MAX_SKILL_REFS} entries")
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ScheduleValidationError("skill_refs entries must be objects")
        stable_id = str(row.get("stable_id") or "").strip()
        sha = str(row.get("content_sha256") or "").strip().lower()
        if not stable_id or not re_full_sha256(sha):
            raise ScheduleValidationError("skill_refs require stable_id and SHA-256")
        if stable_id not in seen:
            normalized.append({"stable_id": stable_id, "content_sha256": sha})
            seen.add(stable_id)
    return normalized


def re_full_sha256(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _now(value: float | None) -> float:
    return float(time.time() if value is None else value)


__all__ = [
    "SchedulerConflictError",
    "SchedulerJobCreateRequest",
    "SchedulerNotFoundError",
    "SchedulerRepository",
    "SchedulerRepositoryError",
    "SchedulerRunFinish",
    "SchedulerStateError",
]
