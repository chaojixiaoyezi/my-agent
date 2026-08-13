from __future__ import annotations

"""Owner-scoped durable scheduler job and run repository.

The owner store is the only job authority.  A run reservation snapshots the
prompt/thread/Skill references before any model or tool side effect, matching
the durable pre-admission pattern used by 通道运行时 and 长期助手.
"""

import hashlib
import json
import os
import time
import uuid
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
_ACTIVE_RUN_STATUSES = frozenset({"queued", "claimed", "running"})
# P0-4(HANDOFF 文档线): unknown = 崩溃执行终态(进程死亡被证实后归类),
# 与 done/failed/cancelled/skipped 并列, 终态不可改写。
_TERMINAL_RUN_STATUSES = frozenset({"done", "failed", "cancelled", "skipped", "unknown"})
_MAX_NAME_CHARS = 160
_MAX_PROMPT_CHARS = 32_000
_MAX_SKILL_REFS = 32
_MANUAL_DISPATCH_DELAY_SECONDS = 2.0


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
        with locked_json_path(self.store_path):
            store = self._load_store_unlocked()
            jobs, errors = self._valid_jobs(store)
        selected = [job for job in jobs if include_deleted or job["status"] != "deleted"]
        selected.sort(key=lambda job: (float(job.get("next_run_at") or 1e30), str(job["job_id"])))
        return deepcopy(selected), errors

    def get_job(self, job_id: str, *, include_deleted: bool = False) -> dict[str, object]:
        with locked_json_path(self.store_path):
            store = self._load_store_unlocked()
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
        with locked_json_path(self.store_path):
            store = self._load_store_unlocked()
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

        with locked_json_path(self.store_path):
            store = self._load_store_unlocked()
            run, _error = self._parse_run(store["runs"].get(str(run_id or "")))
        return deepcopy(run) if run is not None else None

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
                # P0-4: 记录持有 claim 的进程身份(崩溃恢复判活的死亡证明来源)
                "runner_pid": os.getpid(),
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
            terminal = {
                **run,
                "status": terminal_status,
                "ended_at": current,
                "updated_at": current,
                "claim_expires_at": 0.0,
                "response": result.response[:4000],
                "delivery_status": result.delivery_status[:80],
                "delivery_reason": result.delivery_reason[:160],
                "error_code": result.error_code[:120],
                "error_message": result.error_message[:500],
            }
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

    def runtime_snapshot(self) -> dict[str, object]:
        try:
            jobs, errors = self.list_jobs(include_deleted=False)
            with locked_json_path(self.store_path):
                store = self._load_store_unlocked()
                active_runs = sum(
                    1
                    for raw in store["runs"].values()
                    if (run := self._parse_run(raw)[0]) is not None
                    and run["status"] in _ACTIVE_RUN_STATUSES
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
            "active_jobs": sum(1 for job in jobs if job["status"] == "active"),
            "paused_jobs": sum(1 for job in jobs if job["status"] == "paused"),
            "active_runs": active_runs,
            "load_error_codes": sorted(set(errors)),
            "schedule_kinds": sorted({str(job["schedule"]["kind"]) for job in jobs}),
        }


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
        if report.load_error is not None:
            raise SchedulerStateError("owner scheduler store is unreadable")
        if not report.payload:
            return {
                "schema_version": _STORE_SCHEMA,
                "owner": dict(self.owner),
                "jobs": {},
                "runs": {},
                "updated_at": 0.0,
            }
        store = report.payload
        if store.get("schema_version") != _STORE_SCHEMA:
            raise SchedulerStateError("unsupported owner scheduler store schema")
        if store.get("owner") != self.owner:
            raise SchedulerStateError("owner scheduler store identity mismatch")
        if not isinstance(store.get("jobs"), dict) or not isinstance(store.get("runs"), dict):
            raise SchedulerStateError("owner scheduler store jobs/runs must be objects")
        return store

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
        with locked_json_path(self.store_path):
            store = self._load_store_unlocked()
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
                pid = int(run.get("runner_pid") or 0)
                if pid > 0 and _process_alive(pid):
                    continue  # 进程存活: 未证实死亡, fail-closed 保持原态
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
            if run is None or run["job_id"] != job_id or run["status"] == "running":
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


def _process_alive(pid: int) -> bool:
    """进程存活探活(os.kill 信号 0, P0-4 崩溃恢复的死亡证明)。

    存在(含 PermissionError=有进程但无权)算存活; ProcessLookupError=查无
    此进程(已死/被回收); 其它 OSError 按不可证实处理(不猜)。pid<=0 视为
    不可证实。
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


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
