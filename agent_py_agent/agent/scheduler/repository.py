# LLM: 原owner store是唯一任务权威；规范调度写按quota→资产root→store/CAS，pending先恢复；正常冷快照只复用已有读锁。
#   便宜检查只缓存有界内容投影，时间条件每拍重算；关闭时保留原完整字节复核和waiting缓存。
# 模块用途: 持久化监控、运行、逐目标outbox和历史；目标授权在写前核验，回执不改变运行或配置身份。
from __future__ import annotations

"""Owner-scoped durable scheduler job and run repository.

The owner store is the only job authority.  A run reservation snapshots the
prompt/thread/Skill references before any model or tool side effect, matching
the durable pre-admission pattern used by 通道运行时 and 长期助手.

历史账本落盘走 append_private_jsonl_records（pw2）：目录 0700、文件 0600、存量宽权限下次写入收紧。
"""

import hashlib
import json
import logging
import math
import os
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..common.cache_freshness import cache_entry_trustworthy, cache_stat_signature
from ..common.heartbeat import process_start_time, start_time_relation
from ..common.json_io import (
    append_private_jsonl_records,
    locked_json_path,
    read_jsonl_objects_report,
    write_json_file_atomic_unlocked,
)
from ..common.nofollow_fs import ensure_private_dir, regular_file_exists_beneath
from ..runtime_errors import runtime_error_report
from ..user_space.owner_quota import (
    OwnerQuotaAdmission,
    OwnerQuotaChange,
    OwnerQuotaEnforcer,
)
from .delivery_policy import normalize_delivery
from .due_index import SchedulerDueIndex
from .event_effects import (
    SchedulerEventEffectOperations,
    _cached_history_report,
    _history_file_stamp,
)
from .event_source_repository import SchedulerEventSourceOperations
from .event_sources import sync_event_sources
from .execution_policy import normalize_execution, normalize_precheck
from .notification_binding import normalize_notification_binding
from .policy import apply_run_policy, normalize_policy
from .run_preparation import SchedulerRunPreparation
from .schedule import (
    ScheduleValidationError,
    compute_next_run,
    default_misfire_grace_seconds,
    schedule_timestamp,
    validate_schedule,
)
from .store_snapshot import read_atomic_store_bytes

_STORE_SCHEMA = "scheduler_store.v1"
_JOB_SCHEMA = "scheduler_job.v1"
_RUN_SCHEMA = "scheduler_run.v1"
_JOB_STATUSES = frozenset({"active", "paused", "deleted"})
_ACTIVE_RUN_STATUSES = frozenset({"queued", "claimed", "running", "waiting"})
# P0-4(HANDOFF 文档线): unknown = 崩溃执行终态(进程死亡被证实后归类),
# 与 done/failed/cancelled/skipped 并列, 终态不可改写。
_TERMINAL_RUN_STATUSES = frozenset({"done", "failed", "cancelled", "skipped", "unknown"})
# 任务名最多 160 个字符：够长可读又不会让存储行与展示行失控。
_MAX_NAME_CHARS = 160
# 任务提示词最多 32000 字符：覆盖长需求又不至于把整份账本撑大。
_MAX_PROMPT_CHARS = 32_000
# 单个任务最多带 32 个 Skill 引用：引用太多会让认领快照过大，实际任务用不到这么多。
_MAX_SKILL_REFS_COUNT = 32
# 手动派活默认推迟 2 秒再入队：给调用方一个可确认的窗口，避免同一时刻重复派发。
_MANUAL_DISPATCH_DELAY_SECONDS = 2.0
# 调度仓库单个状态文件允许缓存的最大字节数。
_STORE_PAYLOAD_CACHE_MAX_BYTES = 4 * 1024 * 1024
# 调度仓库状态缓存定期复核磁盘的最小间隔时间。
_STORE_PAYLOAD_CACHE_RECHECK_SECONDS = 60.0

_LOGGER = logging.getLogger(__name__)
# 调度历史运行 ID 集合缓存最多跟踪的文件路径数。
_HISTORY_RUN_IDS_CACHE_MAX_PATHS = 32
# 单个调度历史文件最多缓存的运行 ID 数量上限。
_HISTORY_RUN_IDS_CACHE_MAX_IDS = 1_000_000
# 校验调度历史文件首尾锚点时读取的字节数。
_HISTORY_RUN_IDS_ANCHOR_BYTES = 4096
_HISTORY_RUN_IDS_CACHE_LOCK = threading.Lock()


# LLM: 仅为原JSONL去重派生的不可变投影，不拥有终态；首尾锚点及完整LF边界共同限制同时间戳改写与追加增量。
# 类用途: 保存一代历史文件指纹、扫描位置、首尾摘要和已有运行ID。
@dataclass(frozen=True)
class _HistoryRunIdsProjection:
    stamp: tuple[int, int, int, int, int]
    offset: int
    anchor_digest: str
    complete_line: bool
    run_ids: frozenset[str]
    head_digest: str = ""


_HISTORY_RUN_IDS_CACHE: OrderedDict[str, _HistoryRunIdsProjection] = OrderedDict()


# LLM: 计算已扫描首尾锚点（各最多4096字节）的blake2b摘要与末尾是否为LF；短读或IO异常必须回退，不能冒充有效前缀。
# 函数用途: 校验历史文件首尾边界锚点未被改写，并返回尾部摘要、换行完整标志与首部摘要。
def _read_history_prefix_state(handle: Any, offset: int) -> tuple[str, bool, str] | None:
    if offset <= 0:
        empty = hashlib.blake2b(b"", digest_size=16).hexdigest()
        return empty, True, empty
    head_len = min(offset, _HISTORY_RUN_IDS_ANCHOR_BYTES)
    handle.seek(0)
    head = handle.read(head_len)
    if len(head) != head_len:
        return None
    start = max(0, offset - _HISTORY_RUN_IDS_ANCHOR_BYTES)
    handle.seek(start)
    anchor = handle.read(offset - start)
    if len(anchor) != offset - start:
        return None
    return (
        hashlib.blake2b(anchor, digest_size=16).hexdigest(),
        anchor.endswith(b"\n"),
        hashlib.blake2b(head, digest_size=16).hexdigest(),
    )


# LLM: 只接受同设备/inode、增长、原完整LF边界且首尾锚点摘要相同的追加；小文件扩展同步更新head_digest，任一坏行或竞态退回原全量报告器。
# 函数用途: 验证历史追加后只解析锚点与新增字节，返回新的不可变ID投影。
def _extend_history_run_ids(path, stamp, cached):
    if stamp is None or stamp[2:4] != cached.stamp[2:4] or stamp[1] <= cached.offset or not cached.complete_line:
        return None
    try:
        with path.open("rb") as handle:
            info = os.fstat(handle.fileno())
            if (info.st_ino, info.st_dev) != stamp[2:4]:
                return None
            start = max(0, cached.offset - _HISTORY_RUN_IDS_ANCHOR_BYTES)
            if start > 0:
                handle.seek(0)
                head = handle.read(_HISTORY_RUN_IDS_ANCHOR_BYTES)
                if (len(head) != _HISTORY_RUN_IDS_ANCHOR_BYTES
                        or hashlib.blake2b(head, digest_size=16).hexdigest() != cached.head_digest):
                    return None
            handle.seek(start)
            anchor = handle.read(cached.offset - start)
            if (len(anchor) != cached.offset - start or not anchor.endswith(b"\n")
                    or hashlib.blake2b(anchor, digest_size=16).hexdigest() != cached.anchor_digest):
                return None
            tail = handle.read(stamp[1] - cached.offset)
        if len(tail) != stamp[1] - cached.offset or not tail.endswith(b"\n"):
            return None
        added = set()
        for line in tail.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                return None
            added.add(str(row.get("run_id") or ""))
        if _history_file_stamp(path) != stamp:
            return None
        combined = anchor + tail
        new_anchor = combined[-_HISTORY_RUN_IDS_ANCHOR_BYTES:]
        new_digest = hashlib.blake2b(new_anchor, digest_size=16).hexdigest()
        head_digest = (
            cached.head_digest
            if start > 0
            else new_digest
            if len(combined) <= _HISTORY_RUN_IDS_ANCHOR_BYTES
            else hashlib.blake2b(combined[:_HISTORY_RUN_IDS_ANCHOR_BYTES], digest_size=16).hexdigest()
        )
        return _HistoryRunIdsProjection(stamp, stamp[1], new_digest, True, cached.run_ids | added, head_digest)
    except (OSError, UnicodeError, ValueError):
        return None


# LLM: 坏行仍沿原报告器跳过，去重集合语义不变；损坏/缺失/读中变化只返回本次结果，不缓存。
# 函数用途: 冷读或异常时重建历史ID，记录稳定的首尾锚点摘要与换行边界。
def _full_history_run_ids(path, stamp):
    report = read_jsonl_objects_report(path, context="scheduler.history.dedupe")
    ids = frozenset(str(row.get("run_id") or "") for row in report.records)
    if stamp is not None and not report.load_errors and _history_file_stamp(path) == stamp:
        try:
            with path.open("rb") as handle:
                prefix_state = _read_history_prefix_state(handle, stamp[1])
        except OSError:
            return ids, None
        if prefix_state is not None and _history_file_stamp(path) == stamp:
            return ids, _HistoryRunIdsProjection(
                stamp, stamp[1], prefix_state[0], prefix_state[1], ids, prefix_state[2],
            )
    return ids, None


# LLM: 同尺寸且同粗糙ctime周期内的原地改写通过首尾4KiB锚点校验发现；锁外读取，异常保守回退。
# 函数用途: 确认缓存命中的历史文件首尾锚点与换行边界未发生同时间戳改写。
def _history_anchor_matches(path: Path, cached: _HistoryRunIdsProjection) -> bool:
    try:
        with path.open("rb") as handle:
            return _read_history_prefix_state(handle, cached.offset) == (
                cached.anchor_digest,
                cached.complete_line,
                cached.head_digest,
            )
    except OSError:
        return False


# LLM: 锁只保护内存字典读写，全部文件stat/锚点/增量读取均在锁外执行；业务调用仍持原owner事务锁。
# 函数用途: 为历史追加去重复用最多32路径/一百万ID的线程安全增量索引。
def _cached_history_run_ids(path: Path) -> set[str]:
    key, stamp = str(path), _history_file_stamp(path)
    with _HISTORY_RUN_IDS_CACHE_LOCK:
        cached = _HISTORY_RUN_IDS_CACHE.get(key)
    if (cached is not None and stamp is not None and cached.stamp == stamp
            and _history_anchor_matches(path, cached)):
        with _HISTORY_RUN_IDS_CACHE_LOCK:
            if _HISTORY_RUN_IDS_CACHE.get(key) is cached:
                _HISTORY_RUN_IDS_CACHE.move_to_end(key)
        return set(cached.run_ids)
    with _HISTORY_RUN_IDS_CACHE_LOCK:
        if _HISTORY_RUN_IDS_CACHE.get(key) is cached:
            _HISTORY_RUN_IDS_CACHE.pop(key, None)
    entry = _extend_history_run_ids(path, stamp, cached) if cached is not None else None
    ids, entry = (entry.run_ids, entry) if entry is not None else _full_history_run_ids(path, stamp)
    if entry is not None and len(ids) <= _HISTORY_RUN_IDS_CACHE_MAX_IDS:
        with _HISTORY_RUN_IDS_CACHE_LOCK:
            _HISTORY_RUN_IDS_CACHE[key] = entry
            _HISTORY_RUN_IDS_CACHE.move_to_end(key)
            while (len(_HISTORY_RUN_IDS_CACHE) > _HISTORY_RUN_IDS_CACHE_MAX_PATHS
                   or sum(len(row.run_ids) for row in _HISTORY_RUN_IDS_CACHE.values()) > _HISTORY_RUN_IDS_CACHE_MAX_IDS):
                _HISTORY_RUN_IDS_CACHE.popitem(last=False)
    return set(ids)

# LLM: ``waiting_runs`` is called once per conversation tick (runtime.prepare_tick ->
# SchedulerService.reconcile_waiting_runs) and used to re-parse the whole owner ledger every time
# even when the file had not changed. The projection below binds the parsed waiting rows to the
# exact bytes they came from: (mtime_ns, size, inode, device) + a blake2b digest of the raw file.
# A hit therefore requires the same stat key *and* the same content digest, so any external or
# in-process rewrite (even same-size, same-timestamp) invalidates it. store.json stays the single
# authority; this is a read-through projection of it, never a second source of state.
# waiting 投影缓存最多缓存 4 个 owner 账本快照：每次对账只重解析未变化的文件，够日常并发又防缓存膨胀。
_WAITING_PROJECTION_MAX_ENTRIES_COUNT = 4
# 单个 waiting 投影最多 512 行：行数太多说明账本异常，按满行直接视为需要全量重读。
_WAITING_PROJECTION_MAX_ROWS_COUNT = 512
_WAITING_PROJECTION_CACHE: OrderedDict[tuple[str, str, str, str], _WaitingRunsProjection] = (
    OrderedDict()
)
_WAITING_PROJECTION_LOCK = threading.Lock()
# 结构化可观测计数:命中/回退/守卫。测试与现场体检用它证明缓存真的在生效、且异常时确实回退。
_WAITING_PROJECTION_STATS: dict[str, int] = {"hit": 0, "miss": 0, "guard": 0, "skip": 0}
# 开关打开时的三切片投影缓存最多缓存 64 个 owner 快照（按 owner 数 LRU）：测试机 owner 池上限 600，
# 但每拍真正被后台 worker 检查的活跃 owner 受全局 8 个并发 worker 约束、轮转窗口远小于池上限；
# 单条投影只含 waiting 完整行、queued 轻量键与 due 键（不保留 queued 完整记录），被逐出的
# owner 下次整读，只变慢不出错。
_STORE_PROJECTION_MAX_ENTRIES_COUNT = 64
# 单条三切片投影最多 512 行（waiting + queued 键 + due 键合计）：行数太多说明账本异常，直接视为需要全量重读。
_STORE_PROJECTION_MAX_ROWS_COUNT = 512
# 单条三切片投影的载荷字节近似上限（waiting 行字符串 + 轻量键固定开销）：行数上限挡不住
# "少量超大行"，超限不缓存（下次全读，只慢不错）。
_STORE_PROJECTION_MAX_BYTES_COUNT = 256 * 1024
# 便宜检查（background_tick_cheap_checks）下，距最近一次全量核验（读字节+摘要）超过这个秒数
# 就无条件重核一次：防时间戳精度与极端竞态。命中复用的条目不会刷新这个时间。
_STORE_PROJECTION_FULL_READ_INTERVAL_SECONDS = 60.0
_STORE_PROJECTION_CACHE: OrderedDict[tuple[str, str, str, str], _StoreProjection] = (
    OrderedDict()
)
_STORE_PROJECTION_LOCK = threading.Lock()
# 结构化可观测计数:命中/回退/守卫/便宜命中。测试与现场体检用它证明缓存真的在生效、且异常时确实回退。
_STORE_PROJECTION_STATS: dict[str, int] = {
    "hit": 0,
    "miss": 0,
    "guard": 0,
    "skip": 0,
    "cheap_hit": 0,
}



# LLM: 对外只暴露稳定类别和安全诊断字段，不要求调用方解析文案或向用户展示私有路径。
# 类用途: 表示调度仓库故障，并给命令、日志提供结构化原因。
class SchedulerRepositoryError(RuntimeError):
    """Base class for durable scheduler failures."""

    # LLM: 子类沿用同一错误载荷；details 只存异常类型、errno、行列等无正文事实。
    # 函数用途: 初始化可定位的故障类别，同时保留现有异常消息与继承关系。
    def __init__(self, message: str, *, error_code: str = "SCHEDULER_UNAVAILABLE", details: dict | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.details = dict(details or {})
        self.errno = self.details.get("errno")


class SchedulerStateError(SchedulerRepositoryError):
    """The owner scheduler ledger cannot be read safely."""


class SchedulerNotFoundError(SchedulerRepositoryError):
    """A requested job or run does not exist in this owner store."""


class SchedulerConflictError(SchedulerRepositoryError):
    """A version, claim, or lifecycle precondition changed concurrently."""


# LLM: 创建值对象携带 job 级显式策略，不携带展开后的模型配置或凭据；规范化仍由原仓库执行。
# 类用途: 将计划时间、来源会话、执行策略、通知开关及预检作为一次创建事务的输入。
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
    source_thread_id: str = ""
    policy: object = None
    execution: object = None
    precheck: object = None
    delivery: object = None
    rules: object = None
    creator: object = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "thread_id", str(self.thread_id or "").strip())
        object.__setattr__(self, "source_task_id", str(self.source_task_id or "").strip())
        object.__setattr__(self, "source_request_id", str(self.source_request_id or "").strip())
        object.__setattr__(self, "source_thread_id", str(self.source_thread_id or self.thread_id).strip())


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


# LLM: CRUD只写原owner store，目标和每条规则写前校验，通知变更不复制调度或执行账。
# 类用途: 提供同一仓库的计划创建、查询及版本化更新。
class _SchedulerJobOperations:
    """Public job CRUD operations mixed into the owner repository."""

    # LLM: 别名由原 due_index 分配，复合规则和旧单任务仍写同一个 store。
    # 函数用途: 在原owner仓库登记计划；新多通道目标写前复核原认证账，不允许直调仓库绕过工具权限。
    def create_job(
        self,
        request: SchedulerJobCreateRequest,
    ) -> tuple[dict[str, object], bool]:
        current = _now(request.now)
        normalized = sync_event_sources(_new_job_payload(
            owner=self.owner,
            request=request,
            now=current,
        ))
        from .delivery_policy import validate_repository_delivery
        validate_repository_delivery(self, normalized.get("delivery"))
        for rule in normalized.get("rules", []):
            validate_repository_delivery(self, rule.get("delivery"))
        from .event_source_lifecycle import validate_event_job
        validate_event_job(normalized)
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
            from .monitor_state import next_alias
            normalized["alias"] = self.due_index.allocate_job_alias() if self.due_index is not None else next_alias(store)
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

    # LLM: 原CAS内规范patch，目标认证失败在任何store写入之前抛出；已预留run保持原冻结策略。
    # 函数用途: 更新计划并验证多通道目标，不改原运行、创建者或执行位置。
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
            updated = sync_event_sources(_updated_job(job, patch, now=current), job)
            from .delivery_policy import validate_repository_delivery
            validate_repository_delivery(self, updated.get("delivery"))
            for rule in updated.get("rules", []):
                validate_repository_delivery(self, rule.get("delivery"))
            from .event_source_lifecycle import cancel_retired_event_runs, validate_event_job
            validate_event_job(updated)
            updated = cancel_retired_event_runs(store, updated, now=current)
            store["jobs"][job_id] = updated
            from .monitor_state import disabled_rule_ids
            disabled=disabled_rule_ids(job,updated)
            cancelled=self._cancel_waiting_runs_unlocked(store,job_id,current,'rule_disabled',rule_ids=disabled) if disabled else []
            self._write_store_unlocked(store, admission,history_records=cancelled)
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
                job = sync_event_sources(job, retire=True)
                from .event_source_lifecycle import cancel_retired_event_runs
                job = cancel_retired_event_runs(store, job, now=current)
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
            if job.get("rules"):
                from .monitor_state import resume_monitor
                job = resume_monitor(job, current)
            next_run = float(job["next_run_at"]) if job.get("rules") else _resume_next_run(job, current)
            updated = sync_event_sources({
                **job,
                "status": "active",
                "next_run_at": next_run,
                "paused_at": 0.0,
                "paused_reason": "",
                "updated_at": current,
                "version": int(job["version"]) + 1,
            }, job, retire=True)
            store["jobs"][job_id] = updated
            self._write_store_unlocked(store, admission)
        return deepcopy(updated)

    # LLM: 原 job version CAS 只取消本次已有 run；新周期保留，active run 记录持久意图，排队取消保留原历史。
    # 函数用途: 请求停止本人监控的当前所有规则，不暂停或删除未来计划。
    def cancel_job_runs(self, job_id, *, expected_version):
        current = _now(None)
        with self._mutation_scope() as (store, admission):
            job = self._require_mutable_job(store, job_id, expected_version)
            cancelled = self._cancel_waiting_runs_unlocked(store, job_id, current, 'job_cancelled')
            updated = {**job, 'version': int(job['version']) + 1, 'updated_at': current,
                'cancel_pending_run_ids': list(dict.fromkeys([*(job.get('cancel_pending_run_ids') or []),
                    *(row['run_id'] for row in cancelled)]))}
            updated = sync_event_sources(updated, retire=True)
            store['jobs'][job_id] = updated
            for run_id, raw in list(store['runs'].items()):
                run, _error = self._parse_run(raw)
                if run is not None and run['job_id'] == job_id and run['status'] in {'running', 'waiting'}:
                    store['runs'][run_id] = {**run, 'cancel_requested_at': current, 'cancel_reason': 'job_cancelled'}
                    updated['cancel_pending_run_ids'].append(run_id)
            updated['cancel_pending_run_ids'] = list(dict.fromkeys(updated['cancel_pending_run_ids']))
            self._write_store_unlocked(store, admission, history_records=cancelled)
        return deepcopy(updated)

    # LLM: 只记录准确 run 已请求取消后的资源清理确认；终态仍归执行者或 waiting 对账，不能用该字段替代 claim 死亡事实。
    # 函数用途: 在原 run 保存清理是否确认，Gateway 重启后能继续原取消意图。
    def record_cancel_cleanup(self, run_id, *, confirmed):
        with self._mutation_scope() as (store, admission):
            run, _error = self._parse_run(store['runs'].get(run_id))
            if run is not None and run.get('cancel_requested_at'):
                store['runs'][run_id] = {**run, 'cancel_cleanup_confirmed': confirmed is True}
            elif confirmed:
                store['jobs'] = {identifier: {**raw, 'cancel_pending_run_ids': [key for key in raw.get('cancel_pending_run_ids', ()) if key != run_id]}
                    if run_id in raw.get('cancel_pending_run_ids', ()) else raw for identifier, raw in store['jobs'].items()}
            else:
                return
            self._write_store_unlocked(store, admission)

    # LLM: 取消范围由原 job tombstone 与 run/history ID 决定；只复用同版历史解析，归属逐次校验，返回与缓存隔离。
    # 函数用途: 为冷启动及重复删除取回尚待取消的准确执行，包含已归档的排队运行。
    def pending_cancellation_runs(self):
        store = self._store_snapshot()
        runs, _errors = self._valid_runs(store)
        result = [run for run in runs if run.get('cancel_requested_at')]
        pending = {key for job in store['jobs'].values()
            for key in job.get('cancel_pending_run_ids', ())}
        if pending:
            active_ids = {row['run_id'] for row in result}
            rows = _cached_history_report(self.history_path, context='scheduler.cancel.history').records
            for row in rows:
                if row.get('run_id') in pending - active_ids and row.get('status') in _TERMINAL_RUN_STATUSES:
                    run, _error = self._parse_run(row, terminal=True)
                    if run is not None:
                        result.append(deepcopy(run))
        return result

    # LLM: 原 job CAS 保存通知绑定；调用方已验证认证入口，本方法不接受任意 patch 或改来源，预留 run 不变。
    # 函数用途: 原子保存未来通知接管并增加 job 版本，保留原创建和来源审计。
    def update_notification_binding(self, job_id, binding, *, expected_version):
        normalized = normalize_notification_binding(binding)
        if normalized is None or normalized['owner'] != self.owner:
            raise SchedulerConflictError('notification owner mismatch')
        current = _now(None)
        with self._mutation_scope() as (store, admission):
            job = self._require_mutable_job(store, job_id, expected_version)
            previous = job.get('notification_binding')
            if normalized['version'] != int((previous or {}).get('version') or 0) + 1:
                raise SchedulerConflictError('notification binding version changed')
            updated = {**job, 'notification_binding': deepcopy(normalized),
                'notification_binding_history': [*(job.get('notification_binding_history') or []),
                    *([deepcopy(previous)] if previous else [])],
                'delivery': {**normalize_delivery(job.get('delivery')), 'mode': 'origin'},
                'version': int(job['version']) + 1, 'updated_at': current}
            store['jobs'][job_id] = updated
            self._write_store_unlocked(store, admission)
        return deepcopy(updated)

    # LLM: 原 CAS 关闭未来并持久记录准确 active run 取消意图，已删除重复请求不新增账；运行清理由控制服务消费。
    # 函数用途: 留下监控 tombstone 和历史，取消排队并请求正在执行或等待的整树停止。
    def delete_job(
        self,
        job_id: str,
        *,
        expected_version: int | None,
        now: float | None = None,
    ) -> dict[str, object]:
        current = _now(now)
        with self._mutation_scope() as (store, admission):
            job = self._require_job(store, job_id)
            if job['status'] == 'deleted':
                if expected_version not in {job['version'], job.get('delete_expected_version')}:
                    raise SchedulerConflictError('job version changed')
                return deepcopy(job)
            job = self._require_mutable_job(store, job_id, expected_version)
            deleted = {
                **job,
                "status": "deleted",
                "deleted_at": current,
                "delete_expected_version": expected_version,
                "next_run_at": 0.0,
                "updated_at": current,
                "version": int(job["version"]) + 1,
            }
            deleted = sync_event_sources(deleted, retire=True)
            store["jobs"][job_id] = deleted
            cancelled = self._cancel_waiting_runs_unlocked(store, job_id, current, "job_deleted")
            for run_id, raw in list(store['runs'].items()):
                run, _error = self._parse_run(raw)
                if run is not None and run['job_id'] == job_id and run['status'] in {'running', 'waiting'}:
                    store['runs'][run_id] = {**run, 'cancel_requested_at': current, 'cancel_reason': 'job_deleted'}
            deleted['cancel_pending_run_ids'] = list(dict.fromkeys([*(job.get('cancel_pending_run_ids') or []),
                *(row['run_id'] for row in cancelled),
                *(key for key, row in store['runs'].items() if row.get('job_id') == job_id and row.get('cancel_requested_at'))]))
            self._write_store_unlocked(store, admission, history_records=cancelled)
        return deepcopy(deleted)


# LLM: run 的预留、领取与终态只写原 owner 仓库；错过周期和退避不创建第二个执行者，恢复仍服从原 claim CAS。
# 类用途: 管理定时运行的单次身份、实际开工和结果历史，修改时联测过期与交付重试。
# LLM: 可选内容投影只排除不存在的候选，规则与取消仍在原写事务重核，不缓存时间结论。
# LLM: 预留只生成原 owner 账本中的 run；候选快路不授权写入，实际预约仍遵循原锁顺序及状态复核。
# 类用途: 集中手动和到期预约，避免执行/历史操作类继续堆积预留职责。
class _SchedulerReservationOperations:
    """Public durable manual and due run reservation operations."""

    # LLM: 手动运行准确 rule_id，原活动 run 和状态检查不绕过。
    # 函数用途: 手动运行准确 rule_id，原活动 run 和状态检查不绕过。 修改需同步原控制、恢复或权限测试。
    def reserve_manual_run(self, job_id: str, *, rule_id: str = "", now: float | None = None) -> dict[str, object]:
        current = _now(now)
        with self._mutation_scope() as (store, admission):
            job = self._require_job(store, job_id)
            if job["status"] == "deleted":
                raise SchedulerNotFoundError(f"scheduler job not found: {job_id}")
            if job.get("rules"):
                from .monitor_state import manual_rule
                job = manual_rule(job, rule_id, current)
            if self._job_has_active_run(store, job_id, str(job.get("rule_id") or "")):
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

    # LLM: 投影及无锁快照只排除无候选空轮；退避、独立规则和活动运行仍在原 quota→owner 锁内实时重核。
    # 函数用途: 为真正可预约的到期计划预留运行，不为已有在途任务或未来周期持写锁空转。
    def reserve_due_runs(
        self,
        *,
        now: float | None = None,
        limit: int = 32,
        cheap_checks: bool = False,
    ) -> list[dict[str, object]]:
        current = _now(now)
        if cheap_checks and not self._cheap_due_candidates_exist(now=current):
            return []
        if not _snapshot_due_candidates_exist(self, now=current):
            return []
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
                    and float(job.get("backoff_until") or 0.0) <= current
                ),
                key=lambda job: (float(job["next_run_at"]), str(job["job_id"])),
            )
            from .monitor_state import due_rule_views
            due = [view for job in due for view in due_rule_views(job, current)]
            selected = 0
            max_selected = max(1, int(limit or 1))
            for job in due:
                if selected >= max_selected:
                    break
                job_id = str(job["job_id"])
                if self._job_has_active_run(store, job_id, str(job.get("rule_id") or "")):
                    continue
                selected += 1
                scheduled_for = float(job["next_run_at"])
                lateness = max(0.0, current - scheduled_for)
                if lateness > int(job["misfire_grace_seconds"]) and normalize_policy(job.get("policy"))["misfire"] == "skip":
                    skipped_run = _terminal_misfire_run(
                        job, scheduled_for=scheduled_for, now=current
                    )
                    skipped.append(skipped_run)
                    from .monitor_state import merge_rule_view
                    store["jobs"][job_id] = merge_rule_view(store["jobs"][job_id], _advance_after_misfire(job, current))
                    continue
                from .monitor_state import condition_matches, merge_rule_view
                if not condition_matches(store["jobs"][job_id], job, current):
                    skipped_run = _terminal_misfire_run(job, scheduled_for=scheduled_for, now=current)
                    skipped_run.update(error_code="SCHEDULER_CONDITION_UNMET", error_message="结构化依赖未完成、过期或条件未满足")
                    skipped.append(skipped_run)
                    store["jobs"][job_id] = merge_rule_view(store["jobs"][job_id], _advance_after_reservation(job, current))
                    continue
                run = _reserved_run(
                    job,
                    trigger="due",
                    scheduled_for=scheduled_for,
                    dispatch_after=current,
                    now=current,
                )
                store["runs"][str(run["run_id"])] = run
                store["jobs"][job_id] = merge_rule_view(store["jobs"][job_id], _advance_after_reservation(job, current))
                reserved.append(run)
            # A scheduler poll is read-only unless it actually reserves a run
            # or advances a misfired job.  Writing an unchanged owner ledger
            # here used to refresh ``updated_at``, resync the due projection,
            # and run a full owner-quota scan on every background owner tick.
            # Large owner homes therefore consumed a CPU core while idle.
            if reserved or skipped:
                self._write_store_unlocked(store, admission, history_records=skipped)
        return deepcopy(reserved)


# LLM: 执行状态与历史仍由同一 owner 账本及 claim CAS 管理；预留职责在独立组合类中，不新增存储或权限。
# 类用途: 提供原运行的队列查询、领取、执行、收尾及历史读取操作。
class _SchedulerRunOperations:
    """Public durable run claim, completion, and history operations."""

    # LLM: 便宜路径只排除仍在未来的轻量键；exclude_active_lanes可先排除被活动同车道阻挡的积压再限量，最终派发仍须原事务CAS。
    # 函数用途: 按当前时间列出排队运行，未到期的空轮可省去完整账本读取。
    def queued_runs(
        self,
        *,
        now: float | None = None,
        limit: int = 64,
        cheap_checks: bool = False,
        exclude_active_lanes: bool = False,
    ) -> list[dict[str, object]]:
        current = _now(now)
        if cheap_checks:
            hit = self._cheap_projection_hit()
            if hit is not None and not any(dispatch_after <= current for dispatch_after, _scheduled_for, _run_id in hit.queued_keys):
                return []
        store = self._store_snapshot()
        if exclude_active_lanes:
            runs, _errors = self._valid_runs(store)
            rows = _ready_job_runs(runs, current)
            return deepcopy(rows[: max(1, int(limit or 1))])
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

    # LLM: 只读候选每车道一条，先排除被活动同车道阻挡的积压再限量，不能挤掉别的job或规则；最终派发仍须原事务CAS。
    # 函数用途: 按当前时间选择不互相重叠的排队候选，避免同job/同rule积压造成队首阻塞。
    def dispatchable_queued_runs(self, *, now: float | None = None, limit: int = 64, cheap_checks: bool = False) -> list[dict[str, object]]:
        return self.queued_runs(now=now, limit=limit, cheap_checks=cheap_checks, exclude_active_lanes=True)

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

    # LLM: 绑定活跃 run 的唤醒信号编号；若已绑定同一编号则跳过无变化的整库重写与配额扫描。
    # 函数用途: 记录当前排队或等待中的定时运行所关联的唤醒信号编号。
    def attach_wake_signal(self, run_id: str, wake_signal_id: str) -> None:
        normalized_wake_id = str(wake_signal_id or "")
        with self._mutation_scope() as (store, admission):
            run = self._require_run(store, run_id)
            if run["status"] not in _ACTIVE_RUN_STATUSES:
                return
            if str(run.get("wake_signal_id") or "") == normalized_wake_id:
                return
            store["runs"][run_id] = {
                **run,
                "wake_signal_id": normalized_wake_id,
                "updated_at": time.time(),
            }
            self._write_store_unlocked(store, admission)

    # LLM: 原owner事务内复核同job在途者；租约过期仍须PID死亡证明，不因另一rule或旧queued副本授第二份执行权。
    # 函数用途: 原子领取job唯一执行权，并沿原恢复协议处理已证实死亡的领取者。
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
            resolved = _resolve_expired_claim(run, current)
            if resolved is None:
                return None
            if resolved is not run:
                if resolved["status"] == "unknown":
                    from .recovery import settle_dead_run

                    terminal = settle_dead_run(self, store, run, current=current)
                    self._write_store_unlocked(store, admission, history_records=[terminal])
                    return None
                run = resolved
                store["runs"][run_id] = run
                # 已确认未开工且旧 runner 死亡，先提交 queued，再产生新领取 epoch。
                self._write_store_unlocked(store, admission)
            if run["status"] not in _ACTIVE_RUN_STATUSES:
                return None
            if _job_run_blocker(self, store, run) is not None:
                return None
            claim_epoch = int(run.get("claim_epoch") or 0) + 1
            claim_id = f"claim_{claim_epoch}_{uuid.uuid4().hex}"
            claimed = {
                **run,
                "status": "claimed",
                "claim_id": claim_id,
                "claim_epoch": claim_epoch,
                "claimed_at": current,
                "claim_expires_at": current + max(1, int(lease_seconds or 1)),
                # P0-4: 记录持有 claim 的进程身份(崩溃恢复判活的死亡证明来源);
                # runner_start_time 是跨平台启动指纹字符串(schedstart 统一口径), 不可读时 None。
                "runner_pid": os.getpid(),
                "runner_start_time": process_start_time(os.getpid()),
                "updated_at": current,
            }
            store["runs"][run_id] = claimed
            self._write_store_unlocked(store, admission)
        return deepcopy(claimed)

    # LLM: 开始沿原claim CAS，事件代次必须仍有效；实际模型/运输仍需原副作用开始CAS，不能只靠这一快照。
    # 函数用途: 登记当前领取者开始工作，并拒绝已经暂停或更新的事件运行。
    def mark_run_running(self, run_id: str, claim_id: str, *, now: float | None = None) -> None:
        current = _now(now)
        with self._mutation_scope() as (store, admission):
            run = self._require_claim(store, run_id, claim_id)
            from .event_sources import event_run_current
            if not event_run_current(self._require_job(store, run['job_id']), run):
                raise SchedulerConflictError('事件订阅已停止或更新')
            from .monitor_state import record_run_start
            record_run_start(self, store, run, current)
            store["runs"][run_id] = {
                **run,
                "status": "running",
                "started_at": float(run.get("started_at") or current),
                "updated_at": current,
            }
            self._write_store_unlocked(store, admission)

    # LLM: Heartbeat is a CAS on claim identity and monotonic epoch; a late old owner cannot extend a replacement.
    # 函数用途: 仅为当前 claim epoch 续租，旧 runner 的迟到心跳返回 False。
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
            if run is None or not _claim_matches(run, claim_id):
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

    # LLM: Release uses the same identity/epoch CAS as heartbeat and finish, so stale owners cannot requeue a newer claim.
    # 函数用途: 只释放当前 runner 的租约，避免旧执行者把新 epoch 退回队列。
    def release_run_claim(self, run_id: str, claim_id: str, *, now: float | None = None) -> bool:
        current = _now(now)
        with self._mutation_scope() as (store, admission):
            raw = store["runs"].get(run_id)
            run, _error = self._parse_run(raw)
            if run is None or not _claim_matches(run, claim_id):
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

    # LLM: 终态必须匹配原 claim/epoch，在原 quota→锁事务移出活动 run、更新 job 并追加历史，不能由回复正文触发。
    # 函数用途: 保存准确领取者的执行结果；与 waiting 和死亡恢复共用原终态状态变更入口。
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
            self._record_terminal_unlocked(store, run, terminal, now=current)
            self._write_store_unlocked(store, admission, history_records=[terminal])
        return deepcopy(terminal)

    # LLM: 正常 claim、waiting 与死亡恢复共用同一终态状态变更；调用方必须先持原锁并证明自己的状态或死亡事实。
    # 函数用途: 移出活动 run 并更新原 job；历史提交仍由同一 _write_store_unlocked 事务入口执行。
    def _record_terminal_unlocked(self, store, run, terminal, *, now: float) -> None:
        store["runs"].pop(str(run["run_id"]), None)
        job_id = str(run["job_id"])
        job, _error = self._parse_job(store["jobs"].get(job_id))
        if job is not None:
            store["jobs"][job_id] = _job_after_run(job, terminal, now=now)

    # LLM: 原始历史解析可缓存，owner/schema、去重排序与限量仍逐次执行；坏行诊断不缓存，公开结果保留深复制。
    # 函数用途: 查询原owner历史，文件未变时省去重复JSONL解析。
    def history(
        self,
        *,
        job_id: str = "",
        limit: int = 50,
    ) -> tuple[list[dict[str, object]], list[str]]:
        report = _cached_history_report(self.history_path, context="scheduler.history.read")
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

    # LLM: 只读健康投影不参与执行裁决；完整现读快照不取写锁，job/run 损坏均报告 degraded，读取失败标记 unavailable。
    # 函数用途: 汇总监控与活动运行数量，保留无法查询的原因，不把不可用的零计数当没有监控。
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

    # LLM: 健康读取与普通查询共用完整只读快照，不申请写锁，不用上一代缓存冒充当前状态。
    # 函数用途: 从一次原子快照汇总监控与活动运行，并保留损坏事实。
    def _runtime_snapshot_summary(self) -> tuple[int, int, int, list[str], list[str]]:
        return self._runtime_summary_from_store(self._store_snapshot())

    # LLM: 原快照和缓存保持；规范Owner先恢复残留资产事务，并以原根读锁排除半提交，正常冷读不创建锁或文件。
    # 函数用途: 读取一致的调度快照，不让未提交监控状态进入查询或缓存。
    def _store_snapshot(self) -> dict[str, Any]:
        with self._asset_read_scope():
            stamp = _store_payload_stamp(self.store_path)
            hit = self._store_payload_cache_hit(stamp)
            if hit is not None:
                return hit
            probe = _probe_store_bytes(self.store_path, digest=False)
            if probe is not None:
                try:
                    store = self._load_store_from_bytes_unlocked(probe.data)
                except SchedulerStateError:
                    store = None
                if store is not None and _store_stat_key(self.store_path) == probe.stat_key:
                    self._remember_store_payload(stamp, store, len(probe.data))
                    return store
            return self._load_store_unlocked()

    # LLM: 纯函数式的账本投影(不做 I/O、不读共享状态),因此锁内锁外调用都不改变语义:
    # jobs 侧与 list_jobs(include_deleted=False) 同源同过滤,schedule 种类只看未删除 job;
    # runs 侧共用 _valid_runs；坏 run 跳过计数但错误码必须进入健康事实，不影响健康记录执行。
    # 函数用途: 从已解析的 store 字典里数出 runtime_snapshot 需要的计数与错误码。
    def _runtime_summary_from_store(
        self, store: dict[str, Any]
    ) -> tuple[int, int, int, list[str], list[str]]:
        jobs, errors = self._valid_jobs(store)
        active_jobs = sum(1 for job in jobs if job["status"] == "active")
        paused_jobs = sum(1 for job in jobs if job["status"] == "paused")
        schedule_kinds = sorted(
            {str(rule["schedule"]["kind"]) for job in jobs if job["status"] != "deleted" for rule in (job.get("rules") or [job])}
        )
        runs, run_errors = self._valid_runs(store)
        return active_jobs, paused_jobs, len(runs), errors + run_errors, schedule_kinds


# LLM: waiting是无进程claim的持久阶段；可选缓存仅提供当前内容投影，每条终态仍回原owner账本按准确run做CAS。
# 类用途: 管理模型工作片结束后、根任务仍在继续时的等待、重启对账读取和最终结算。
class _SchedulerWaitingRunOperations:
    # LLM: 默认按现读签名和摘要复核；开启后才用2秒可信窗口/60秒重读的有界内容投影，读取仍不取写锁。
    # 函数用途: 读取等待任务终态的执行，缓存未变账本的解析结果，不创建锁文件或等待写方。
    def waiting_runs(self, *, cheap_checks: bool = False) -> tuple[list[dict[str, object]], list[str]]:
        if cheap_checks:
            hit = self._cheap_projection_hit()
            if hit is None:
                hit = self._store_projection_unlocked(_probe_store_bytes(self.store_path))
            return [deepcopy(row) for row in hit.waiting_rows], list(hit.errors)
        probe = _probe_store_bytes(self.store_path)
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
            self._record_terminal_unlocked(store, run, terminal, now=current)
            self._write_store_unlocked(store, admission, history_records=[terminal])
        return deepcopy(terminal)


# LLM: 集中原解析/owner校验/配额锁顺序；损坏字段不得参与领取，不因租约超时或缓存缺失推断活动run已结束。
# 类用途: 提供调度账本读写、恢复、身份与同job活动运行检查。
class _SchedulerStoreSupport:
    """Private owner-store parsing, locking, quota admission, and validation support."""

    # LLM: 仅同owner、可信五元stat代次、60秒非滑动期限内命中；命中仍核普通文件边界，不作为写CAS或授权。
    # 函数用途: 返回未变化账本的深副本，不读取或重新解析store.json正文。
    def _store_payload_cache_hit(self, stamp: tuple | None) -> dict | None:
        lock = getattr(self, "_store_payload_cache_lock", None)
        if lock is None or stamp is None or not cache_entry_trustworthy(stamp[3]):
            return None
        with lock:
            payload = self._cached_store_payload
            if (payload is None or stamp != self._cached_store_stamp or payload["owner"] != self.owner
                    or time.monotonic() - self._store_payload_verified_at >= _STORE_PAYLOAD_CACHE_RECHECK_SECONDS):
                return None
            try:
                if not regular_file_exists_beneath(self.root, (self.store_path.name,)):
                    return None
            except OSError:
                return None
            return deepcopy(payload)

    # LLM: 仅保存读取前后五元指纹一致且离开粗时间窗的成功完整账本；每实例一代、最多4MiB原字节。
    # 函数用途: 发布由本次原no-follow读取和校验得到的稳定账本副本。
    def _remember_store_payload(self, stamp: tuple | None, store: dict, byte_count: int) -> None:
        lock = getattr(self, "_store_payload_cache_lock", None)
        if (lock is None or stamp is None or byte_count > _STORE_PAYLOAD_CACHE_MAX_BYTES
                or byte_count != stamp[2] or not cache_entry_trustworthy(stamp[3])
                or _store_payload_stamp(self.store_path) != stamp):
            return
        with lock:
            self._cached_store_payload = deepcopy(store)
            self._cached_store_stamp = stamp
            self._store_payload_verified_at = time.monotonic()

    # LLM: 只清进程内副本，不改权威文件；写方仍持原quota/文件锁和既有CAS。
    # 函数用途: 成功写入账本后丢弃旧的解析缓存。
    def _invalidate_store_payload(self) -> None:
        lock = getattr(self, "_store_payload_cache_lock", None)
        if lock is not None:
            with lock:
                self._cached_store_payload = None
                self._cached_store_stamp = None

    # LLM: 只对原data/scheduler规范路径生效；恢复先quota→root→store，已持root的事务不递归恢复或重入quota。
    # 函数用途: 调度器独立入口自动恢复未提交资产批量，并丢弃旧内存投影。
    def _recover_pending_asset_batch(self) -> bool:
        from ..assets import asset_root_lock, asset_root_lock_held
        from ..assets.workspace_assets import _BATCH_FILE

        home = self.root.parent.parent
        if self.root.parts[-2:] != ('data', 'scheduler') or asset_root_lock_held(home):
            return False
        if not regular_file_exists_beneath(home, (_BATCH_FILE,)):
            return False
        with self.quota_enforcer.admission(), asset_root_lock(home):
            pass
        self._invalidate_store_payload()
        _drop_store_projection(self.waiting_projection_cache_key())
        _drop_waiting_projection(self.waiting_projection_cache_key())
        return True

    # LLM: 只读锁内新发现pending须先释放读锁，再按quota→root→store恢复并有限重读，不能读锁内升级取写锁。
    # 函数用途: 为调度只读快照提供与资产发表互斥的原根锁。
    @contextmanager
    def _asset_read_scope(self):
        from ..assets import asset_root_lock_held
        from ..assets.workspace_assets import _BATCH_FILE, _read_lock

        home = self.root.parent.parent
        if self.root.parts[-2:] != ('data', 'scheduler') or asset_root_lock_held(home):
            yield
            return
        for _attempt in range(2):
            self._recover_pending_asset_batch()
            with _read_lock(home, ('.asset-batch.lock',)):
                if not regular_file_exists_beneath(home, (_BATCH_FILE,)):
                    yield
                    return
        raise SchedulerStateError('asset transaction kept changing', error_code='SCHEDULER_ASSET_RECOVERY_REQUIRED')

    # LLM: 规范调度写共享资产root锁，固定quota→root→store序；旧独立scheduler路径仍维持原锁，恢复回调不重入root。
    # 函数用途: 在读取和修改调度账本前先恢复监控事务，保护已确认的普通调度写入。
    @contextmanager
    def _mutation_scope(self) -> Iterator[tuple[dict[str, Any], OwnerQuotaAdmission]]:
        """Lock quota before the scheduler authority to keep one lock order."""
        from contextlib import nullcontext

        from ..assets import asset_root_lock, asset_root_lock_held

        with self.quota_enforcer.admission() as admission:
            home = self.root.parent.parent
            scope = (asset_root_lock(home) if self.root.parts[-2:] == ('data', 'scheduler')
                and not asset_root_lock_held(home) else nullcontext())
            with scope, locked_json_path(self.store_path):
                yield self._load_store_unlocked(), admission

    # LLM: 写方已持root/store锁，独立读取先恢复pending；仍沿原缓存/no-follow，不吞损坏或权限错误。
    # 函数用途: 读取校验原账本并缓存稳定代次，缺失与坏内容保留原语义。
    def _load_store_unlocked(self) -> dict[str, Any]:
        self._recover_pending_asset_batch()
        stamp = _store_payload_stamp(self.store_path)
        hit = self._store_payload_cache_hit(stamp)
        if hit is not None:
            return hit
        content = _read_store_bytes(self.store_path)
        if content is None:
            return self._validate_store_unlocked({}, None)
        store = self._load_store_from_bytes_unlocked(content)
        self._remember_store_payload(stamp, store, len(content))
        return store

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

    # LLM: 只接受原 schema/owner 与对象形状；空缺账本维持初始化语义，坏内容不回落历史或其他 owner。
    # 函数用途: 校验同一代账本，给命令提供内容、schema、身份不同的稳定诊断码。
    def _validate_store_unlocked(
        self,
        payload: dict[str, Any],
        load_error: dict[str, object] | None,
    ) -> dict[str, Any]:
        """校验已解析的 store 负载并返回权威字典(空负载 → 空账本默认值)。"""
        if load_error is not None:
            raise SchedulerStateError("owner scheduler store is unreadable", error_code="SCHEDULER_STORE_INVALID_CONTENT",
                details={key: load_error[key] for key in ("error_type", "line_no", "column") if key in load_error})
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
            raise SchedulerStateError("unsupported owner scheduler store schema", error_code="SCHEDULER_STORE_SCHEMA_MISMATCH")
        if store.get("owner") != self.owner:
            raise SchedulerStateError("owner scheduler store identity mismatch", error_code="SCHEDULER_STORE_OWNER_MISMATCH")
        if not isinstance(store.get("jobs"), dict) or not isinstance(store.get("runs"), dict):
            raise SchedulerStateError("owner scheduler store jobs/runs must be objects", error_code="SCHEDULER_STORE_INVALID_CONTENT")
        return store

    # LLM: 投影前恢复pending时弃用旧probe；其它stat/摘要及缓存判定保持，投影不授予写权。
    # 函数用途: 查找 waiting 的有界缓存，文件变化或内容变化时现读解析，错误不留脏缓存。
    def _waiting_runs_projection_unlocked(self, probe: _StoreBytesProbe | None) -> _WaitingRunsProjection:
        if self._recover_pending_asset_batch():
            probe = None
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

    # LLM: probe 与现读 stat 匹配才缓存；fallback 仍为 no-follow 完整快照，读取失败移除缓存而非返回旧结论。
    # 函数用途: 解析本次 waiting 记录，使用原有界缓存减少重复扫描，全程不取写锁。
    def _parse_waiting_projection_unlocked(
        self,
        probe: _StoreBytesProbe | None,
    ) -> _WaitingRunsProjection:
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
        if probe is None or len(rows) > _WAITING_PROJECTION_MAX_ROWS_COUNT:
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
            while len(_WAITING_PROJECTION_CACHE) > _WAITING_PROJECTION_MAX_ENTRIES_COUNT:
                _WAITING_PROJECTION_CACHE.popitem(last=False)
        return projection

    # LLM: 回执只读原job outbox；不创建run、不改变配置版本或恢复执行，目标引用由发送层校验。
    # 函数用途: 读取一个准确运行的独立目标回执，不返回其它运行或owner内容。
    def delivery_receipts(self, run_id):
        run = self.effect_run(run_id)
        if run is None or run['owner'] != self.owner:
            raise SchedulerNotFoundError('定时运行不存在')
        job = self.get_job(run['job_id'], include_deleted=True)
        return deepcopy(job.get('delivery_outbox', {}).get(run_id, {}))

    # LLM: quota→原store锁内只更新原运行的outbox；committed不可降级，网络运输不在锁内。
    # 函数用途: 原子保存每目标状态、整封已发回执和canonical编号，支持部分失败重投。
    def record_delivery_receipt(self, run_id, target_key, receipt):
        run = self.effect_run(run_id)
        if run is None or run['owner'] != self.owner or not isinstance(target_key, str) or not target_key:
            raise SchedulerNotFoundError('定时运行不存在')
        with self._mutation_scope() as (store, admission):
            job = self._require_job(store, run['job_id'])
            outbox = job.setdefault('delivery_outbox', {})
            targets = outbox.setdefault(run_id, {})
            if targets.get(target_key, {}).get('state') == 'committed':
                return
            targets[target_key] = {**deepcopy(receipt), 'updated_at': time.time()}
            policy = run.get('delivery', {})
            expected = {spec.get('binding_id') or spec['target_kind'] for spec in policy.get('targets', [])}
            if policy.get('include_origin'):
                expected.add('origin')
            covered = {spec for row in targets.values() for spec in row.get('specs', [])}
            if expected <= covered and all(row.get('state') == 'committed' for row in targets.values()):
                for row in targets.values():
                    row['run_delivery_complete'] = True
            _compact_delivery_outbox(outbox, run_id)
            store['jobs'][run['job_id']] = job
            self._write_store_unlocked(store, admission)

    # LLM: 原quota→原文件锁→历史追加→原子写顺序不变；成功写入后只失效当前实例解析缓存。
    # 函数用途: 持久化当前owner账本并阻止后续查询使用旧副本。
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
            # 历史账本是宿主数据：私有追加（0600 文件、0700 目录），内容逐字节不变（pw2）。
            append_private_jsonl_records(self.history_path, history_rows)
        write_json_file_atomic_unlocked(self.store_path, store)
        self._invalidate_store_payload()

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

    # LLM: 与 claim_run 共用过期与死亡判定；已开跑只记 unknown，恢复沿原终态/history 提交，不自动重放副作用。
    # 函数用途: 启动或周期清理明确死亡的领取，显式控制可只复核准确 job；迁移旧版误留活动账本的 unknown。
    def recover_interrupted_executions(self, *, now: float | None = None, job_id: str = '', expected_version: int | None = None) -> list[str]:
        from .recovery import recover_runs

        return recover_runs(self, current=_now(now), job_id=job_id, expected_version=expected_version)

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

    # LLM: Mutating completion paths must match claim_id and embedded claim_epoch before touching the owner ledger.
    # 函数用途: 为 running、finish 等写操作核对当前领取者身份，旧 epoch 不得改写新 claim。
    def _require_claim(
        self, store: dict[str, Any], run_id: str, claim_id: str
    ) -> dict[str, object]:
        run = self._require_run(store, run_id)
        if not _claim_matches(run, claim_id):
            raise SchedulerConflictError("scheduler run claim changed")
        return run

    # LLM: 复合规则只阻挡自身车道，旧单任务仍按原 job 车道。
    # 函数用途: 复合规则只阻挡自身车道，旧单任务仍按原 job 车道。 修改需同步原控制、恢复或权限测试。
    def _job_has_active_run(self, store: dict[str, Any], job_id: str, rule_id: str = "") -> bool:
        for raw in store["runs"].values():
            run, _error = self._parse_run(raw)
            if run is not None and run["job_id"] == job_id and (not rule_id or run.get("rule_id") == rule_id):
                return True
        return False

    def _cancel_waiting_runs_unlocked(
        self,
        store: dict[str, Any],
        job_id: str,
        current: float,
        error_code: str,
        *, rule_ids: frozenset[str] | None = None,
    ) -> list[dict[str, object]]:
        cancelled: list[dict[str, object]] = []
        for run_id, raw in list(store["runs"].items()):
            run, _error = self._parse_run(raw)
            if run is None or run["job_id"] != job_id or (rule_ids is not None and run.get('rule_id') not in rule_ids) or run["status"] in {
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

    # LLM: 原owner锁内先查规范history的有界增量ID投影；坏行回退仍沿原过滤，不推断运行终态或改变同批去重口径。
    # 函数用途: 剔除已经持久化的run_id，避免每次finish_run从0重解析历史。
    def _new_history_rows_unlocked(
        self,
        records: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        if not records:
            return []
        existing = _cached_history_run_ids(self.history_path)
        return [record for record in records if str(record.get("run_id") or "") not in existing]


# LLM: 纯解析原job/run的schema、owner和冻结来源，不写账、不迁移、不建立新权威；错误仍由原repository报告。
# 类用途: 将原记录校验与store I/O分开，供原所有读取和CAS共用。
class _SchedulerRecordParsing:
    # LLM: schema/owner/计划、执行策略及带版本通知接管共用这里校验；错误只返回结构化码，读路径不迁移或覆盖原文件。
    # 函数用途: 识别可信的当前 owner 计划，将非法记录排除于实际调度之外。
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
            if raw.get("rules") is not None:
                from .monitor_state import validate_monitor
                validate_monitor(raw)
            else:
                validate_schedule(raw.get("schedule"))
                _validate_name_prompt(str(raw.get("name") or ""), str(raw.get("prompt") or ""))
            _normalize_skill_refs(raw.get("skill_refs"))
            normalize_policy(raw.get("policy"))
            normalize_execution(raw.get("execution"))
            normalize_precheck(raw.get("precheck"))
            normalize_delivery(raw.get("delivery"))
            binding = normalize_notification_binding(raw.get('notification_binding'))
            if binding is not None and binding['owner'] != self.owner:
                raise ValueError('notification owner')
            pending = raw.get('cancel_pending_run_ids', [])
            if (not isinstance(pending, list) or any(not isinstance(key, str) or not key.startswith('srun_') for key in pending)
                    or len(pending) != len(set(pending))):
                raise ValueError('cancel pending IDs')
            if int(raw.get("version") or 0) < 1:
                raise ValueError("job version")
            from .event_source_lifecycle import validate_event_job
            validate_event_job(raw)
            return dict(raw), ""
        except (TypeError, ValueError, ScheduleValidationError):
            return None, "SCHEDULER_JOB_INVALID"

    # LLM: 旧run默认epoch 0；读取时校验冻结执行策略、事件来源及通知接管目标，损坏来源不能变成普通任务绕过开始CAS。
    # 函数用途: 解析新旧 scheduler run，拒绝非法 epoch 和配置，确保后续 CAS 有确定基线。
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
            claim_epoch = raw.get("claim_epoch", 0)
            if isinstance(claim_epoch, bool) or not isinstance(claim_epoch, int) or claim_epoch < 0:
                raise ValueError("claim epoch")
            normalize_execution(raw.get("execution"))
            normalize_precheck(raw.get("precheck"))
            normalize_delivery(raw.get("delivery"))
            from .event_source_lifecycle import validate_event_run
            validate_event_run(raw)
            binding = normalize_notification_binding(raw.get('notification_binding'))
            if binding is not None and binding['owner'] != self.owner:
                raise ValueError('notification owner')
            requested = raw.get('cancel_requested_at', 0)
            if (isinstance(requested, bool) or not isinstance(requested, (int, float))
                    or not math.isfinite(requested) or requested < 0
                    or 'cancel_cleanup_confirmed' in raw and type(raw['cancel_cleanup_confirmed']) is not bool):
                raise ValueError('cancel intent')
            parsed = dict(raw)
            parsed["claim_epoch"] = claim_epoch
            return parsed, ""
        except (TypeError, ValueError):
            return None, "SCHEDULER_RUN_INVALID"



# LLM: 此mixin只服务开启的内容投影；关闭路径保留主线原有waiting缓存、完整字节摘要与无写锁快照。
#   三类切片都不缓存now判断，写方仍使用原owner锁、规范账本与CAS。
# 类用途: 开关打开时的 store.json 三切片投影缓存（命中判定、全量核验与 due 判据）。
class _SchedulerProjectionSupport:
    """store.json 三切片投影缓存：全量核验、便宜命中与 due 判据（E10 一期，仅开关打开时使用）。"""

    # LLM: 投影只绑定原子快照字节与现读身份；不可恢复来源旧写锁，异常仍走原读取错误。
    # 函数用途: 复核完整投影或解析本次账本，供开关开启的三处读取共用。
    # LLM: 恢复单任务事务后不再使用恢复前probe；正常只读投影的校验和预算保持。
    # 函数用途: 从一致账本建立调度/等待投影，避免沿用半提交代次。
    def _store_projection_unlocked(self, probe: _StoreBytesProbe | None) -> _StoreProjection:
        """只读复核原子快照内容投影，保留当前主线的错误与无写锁读取语义。"""
        if self._recover_pending_asset_batch():
            probe = None
        cache_key = self.waiting_projection_cache_key()
        stat_key = _store_stat_key(self.store_path)
        if stat_key is None:
            # 文件缺失/不可 stat:不能绑定任何投影,冷路径(语义: 空账本或结构化读失败)。
            _drop_store_projection(cache_key)
            return self._parse_store_projection_unlocked(None)
        usable_probe = probe if (probe is not None and probe.stat_key == stat_key) else None
        if usable_probe is not None:
            hit, stale = _lookup_store_projection(
                cache_key, stat_key=stat_key, digest=usable_probe.digest
            )
            if hit is not None:
                _bump_store_projection_stat("hit")
                return hit
            if stale:
                _bump_store_projection_stat("guard")
                _LOGGER.warning(
                    "scheduler waiting projection guard fallback: stat key unchanged but content "
                    "digest differs; re-parsing owner store",
                    extra={
                        "event": "scheduler_waiting_projection_guard",
                        "store_path": str(self.store_path),
                        "stat_key": stat_key,
                    },
                )
                _drop_store_projection(cache_key)
        return self._parse_store_projection_unlocked(usable_probe)

    # LLM: 先恢复pending再用原廉价投影，不能因缓存判断为空而漏掉恢复；其它nofollow/身份/时限不变。
    # 函数用途: 用文件身份和原访问边界复用有界内容投影，命中只记录诊断计数。
    def _cheap_projection_hit(self) -> _StoreProjection | None:
        self._recover_pending_asset_batch()
        try:
            stat_key = _store_stat_key(self.store_path)
            if stat_key is None:
                return None
            if not regular_file_exists_beneath(self.root, (self.store_path.name,)):
                return None
            hit = _lookup_cheap_projection(
                self.waiting_projection_cache_key(), stat_key=stat_key
            )
        except Exception:
            _LOGGER.warning(
                "scheduler cheap projection check failed; falling back to full read",
                exc_info=True,
                extra={
                    "event": "scheduler_cheap_projection_error",
                    "store_path": str(self.store_path),
                },
            )
            return None
        if hit is not None:
            _bump_store_projection_stat("cheap_hit")
        return hit

    # LLM: 有效到期键已排除活动 job/rule 并合并退避；每拍重算 now，缓存缺失仍回完整快照而非判空。
    # 函数用途: 从可信只读投影判断本拍是否可能需要预约，空轮不进写事务。
    def _cheap_due_candidates_exist(self, *, now: float) -> bool:
        hit = self._cheap_projection_hit()
        if hit is None:
            return True
        return any(next_run_at <= now for next_run_at, _job_id in hit.due_job_keys)

    # LLM: 只缓存与时间无关的切片；读取时可信、发布时身份和载荷界限必须同时满足，不决定变更权限。
    # 函数用途: 完整读取原账本并建立有界投影，等待行错误与主线保持一致。
    def _parse_store_projection_unlocked(
        self,
        probe: _StoreBytesProbe | None,
    ) -> _StoreProjection:
        """解析等待行及轻量候选键，只在读取可信、身份未变且载荷有界时缓存。"""
        cache_key = self.waiting_projection_cache_key()
        try:
            if probe is not None:
                store = self._load_store_from_bytes_unlocked(probe.data)
            else:
                store = self._load_store_unlocked()
            runs, errors = self._valid_runs(store)
            jobs, _job_errors = self._valid_jobs(store)
        except Exception:
            _drop_store_projection(cache_key)
            raise
        rows = tuple(_select_waiting_runs(runs))
        queued_keys, queued_ok = _collect_queued_keys(runs)
        due_job_keys, due_ok = _collect_due_job_keys(jobs, runs)
        projection = _StoreProjection(
            stat_key=probe.stat_key if probe is not None else (-1, -1, -1, -1),
            digest=probe.digest if probe is not None else "",
            waiting_rows=rows,
            queued_keys=queued_keys,
            due_job_keys=due_job_keys,
            errors=tuple(errors),
            verified_at=time.time(),
        )
        _bump_store_projection_stat("miss")
        total_rows = len(rows) + len(queued_keys) + len(due_job_keys)
        payload_bytes = _estimate_projection_payload_bytes(rows, queued_keys, due_job_keys)
        if probe is None or total_rows > _STORE_PROJECTION_MAX_ROWS_COUNT:
            # 字节不可用 / 超界行数: 不缓存,退化为逐次全量解析(与改动前同,不会更差)。
            _bump_store_projection_stat("skip")
            return projection
        if not queued_ok or not due_ok or payload_bytes is None:
            # 坏数值键(跳过会与基线的报错行为不一致) / 载荷超字节上限或估算不可靠
            # (MC4-1202: None 表示嵌套体积超门限/无法可靠估算): 本次结论照常返回,但不缓存。
            _bump_store_projection_stat("skip")
            return projection
        if _store_stat_key(self.store_path) != probe.stat_key:
            # 解析期间账本又被改写: 本次结论照常返回,但不写缓存(下次重新解析,不赌)。
            _bump_store_projection_stat("skip")
            return projection
        if not probe.trusted_at_read:
            # MC4-1201: 读取那一刻 mtime 还在 2 秒窗口内 → 不入缓存;用读取前冻结的事实判断,
            # 不能用发布时的文件年龄代替(跨窗口延迟发布时旧结果不得变成可信缓存)。
            _bump_store_projection_stat("skip")
            return projection
        _write_store_projection_cache(cache_key, projection)
        return projection


# LLM: 组合原job/run/store，job级准入沿原quota→owner事务，不建立进程内全owner执行锁或平行权威文件。
#   开启时的有界投影仅服务读取，事件、规则、原子快照及写入准入仍使用主线原实现。
# 类用途: 组合持久调度能力并为同job派发提供可重试的原子准入。
class SchedulerRepository(
    SchedulerEventEffectOperations,
    SchedulerEventSourceOperations,
    SchedulerRunPreparation,
    _SchedulerJobOperations,
    _SchedulerReservationOperations,
    _SchedulerRunOperations,
    _SchedulerWaitingRunOperations,
    _SchedulerProjectionSupport,
    _SchedulerStoreSupport,
    _SchedulerRecordParsing,
):
    """Single owner-local facade over job, run, and store responsibilities."""

    # LLM: 保留owner/quota/due-index装配；仅增加每实例有界解析副本和线程锁，不建立第二存储权威。
    # 函数用途: 初始化原调度仓库和可失效的只读账本缓存。
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
        self._store_payload_cache_lock = threading.Lock()
        self._cached_store_payload: dict | None = None
        self._cached_store_stamp: tuple | None = None
        self._store_payload_verified_at = 0.0
        # 目录缺失时逐级按 0700 新建（pbfix 2026-10-04：统一走 nofollow_fs.ensure_private_dir；已存在的目录一律不动）。
        ensure_private_dir(self.root)

    # LLM: 已冻结 dict 且无迁移时只读深复制；忙碌或首次冻结和显式来源迁移始终委托原 mixin 的锁/CAS/证明校验。
    # 函数用途: 避免 queued 运行每拍重复准备来源时取得原 owner 变更锁，并发写繁忙时退回加锁路径。
    def prepare_run_context(self, run_id: str, context: dict, *, origin_migration: dict | None = None) -> dict:
        if origin_migration is None:
            try:
                run, _error = self._parse_run(self._store_snapshot()["runs"].get(run_id))
            except SchedulerStateError as exc:
                if exc.error_code != "SCHEDULER_STORE_BUSY":
                    raise
                run = None
            if run is not None and isinstance(run.get("execution_context"), dict):
                return deepcopy(run)
        return super().prepare_run_context(run_id, context, origin_migration=origin_migration)

    # LLM: 相同或空 wake 输入只返回原快照深副本；真正换绑仍委托原 owner/quota 锁重新验证并写入。
    # 函数用途: 为活跃运行挂接唤醒信号，幂等重放不取变更锁也不清空已有信号。
    def attach_wake_signal(self, run_id: str, wake_signal_id: str) -> dict[str, object] | None:
        normalized_wake_id = str(wake_signal_id or "")
        unchanged = _attached_wake_snapshot(self, run_id, normalized_wake_id)
        if unchanged is not None:
            return unchanged
        return super().attach_wake_signal(run_id, normalized_wake_id)

    # LLM: 必须在物化线程/发wake之前持久占job额度；崩溃后同run幂等重试，其它run延后且不杀当前执行。
    # 函数用途: 原子准入一条排队运行，拒绝同job已在途的第二条。
    def admit_run_dispatch(self, run_id: str, *, now: float | None = None) -> dict | None:
        with self._mutation_scope() as (store, admission):
            run = self._require_run(store, run_id)
            if run["status"] != "queued" or _job_run_blocker(self, store, run) is not None:
                return None
            if not run.get("dispatch_admitted"):
                run = {**run, "dispatch_admitted": True, "updated_at": _now(now)}
                store["runs"][run_id] = run
                self._write_store_unlocked(store, admission)
            return deepcopy(run)


# LLM: queued带原wake或派发准入标记代表dispatched；claimed/running/waiting均未终结，不能靠时间猜完成。
# 函数用途: 判断一条原运行是否已经占用job执行额度。
def _run_holds_job(run):
    return run["status"] != "queued" or bool(run.get("wake_signal_id") or run.get("dispatch_admitted"))


# LLM: 复合规则按(job_id, rule_id)分车道，单规则job按(job_id, "")互斥；与_job_has_active_run粒度完全一致。
# 函数用途: 返回一条调度运行的单飞互斥车道键。
def _run_lane_key(run):
    return str(run["job_id"]), str(run.get("rule_id") or "")


# LLM: 只比较同车道，绝不以owner串行化或跨独立rule误拦；旧多wake按原时间/ID选唯一候选，正在执行的记录始终优先。
# 函数用途: 返回阻挡本次派发或领取的准确活动run；无阻挡返回None。
def _job_run_blocker(repository, store, run):
    lane = _run_lane_key(run)
    peers = [parsed for raw in store["runs"].values()
             if (parsed := repository._parse_run(raw)[0]) is not None
             and _run_lane_key(parsed) == lane and parsed["run_id"] != run["run_id"]]
    active = [peer for peer in peers if peer["status"] != "queued"]
    if active:
        return min(active, key=_run_order)
    if run["status"] != "queued":
        return None
    dispatched = [peer for peer in peers if _run_holds_job(peer)]
    if _run_holds_job(run):
        dispatched = [peer for peer in dispatched if _run_order(peer) < _run_order(run)]
    return min(dispatched, key=_run_order) if dispatched else None


# LLM: 仅用原冻结时间与run_id做稳定排序，不依赖字典遍历或进程本地先后。
# 函数用途: 在升级遗留多份派发记录中选择确定的先行者。
def _run_order(run):
    return float(run["scheduled_for"]), str(run["run_id"])


# LLM: 只读单快照按车道线性建阻挡集合，排序沿原时间/ID；已准入queued优先于同车道未准入旧行，不授执行权。
# 函数用途: 限量前剔除同车道重复候选，让不同job/规则在一个轮询内都能入选。
def _ready_job_runs(runs, current):
    ordered = sorted((run for run in runs if run["status"] == "queued"), key=_run_order)
    busy = {_run_lane_key(run) for run in runs if run["status"] != "queued"}
    dispatched = {}
    for run in ordered:
        if run["status"] == "queued" and _run_holds_job(run):
            dispatched.setdefault(_run_lane_key(run), run["run_id"])
    selected, seen = [], set()
    for run in ordered:
        lane = _run_lane_key(run)
        if (run["status"] != "queued" or lane in busy or lane in seen
                or float(run.get("dispatch_after") or 0) > current
                or dispatched.get(lane, run["run_id"]) != run["run_id"]):
            continue
        selected.append(run)
        seen.add(lane)
    return selected


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


# LLM: 只保存已完整读取的时间无关切片；stat/digest绑定字节，verified_at限制60秒重读，命中不延长核验时刻。
#   2秒窗口内的读取不能入缓存；便宜命中仍核原文件边界，变更和执行权限从不来自投影。
# 类用途: 在开启优化时共享等待行、排队轻量键与到期键，不保存本拍是否有事的判断。
@dataclass(frozen=True)
class _StoreProjection:
    stat_key: tuple[int, int, int, int]
    digest: str
    waiting_rows: tuple[dict[str, object], ...]
    queued_keys: tuple[tuple[float, float, str], ...]
    due_job_keys: tuple[tuple[float, str], ...]
    errors: tuple[str, ...]
    verified_at: float


# LLM: 完整只读快照的 stat 与字节来源绑定，摘要供跨调用投影复核；不取写锁、不授权任何变更。
#   trusted_at_read只记录读取前是否已离开保护窗口，不允许发布时重新推断该事实。
# 类用途: 保存一代原账本字节与指纹，让查询和 waiting 缓存复用完整内容。
@dataclass(frozen=True)
class _StoreBytesProbe:
    stat_key: tuple[int, int, int, int]
    digest: str
    data: bytes
    trusted_at_read: bool = False


# LLM: 读取前即冻结可信窗口；ctime检测恢复mtime的同尺寸写，lstat不跟随文件链接，异常沿原读口保留。
# 函数用途: 获取账本解析缓存使用的完整文件代次。
def _store_payload_stamp(path: Path) -> tuple[int, int, int, int, int] | None:
    try:
        status = path.stat(follow_symlinks=False)
        return cache_stat_signature(status) if cache_entry_trustworthy(status.st_mtime_ns) else None
    except OSError:
        return None


def _store_stat_key(path: Path) -> tuple[int, int, int, int] | None:
    """store.json 的 (mtime_ns, size, inode, device) 指纹;不可读返回 None。"""
    try:
        status = path.stat()
    except OSError:
        return None
    return (status.st_mtime_ns, status.st_size, status.st_ino, status.st_dev)


# LLM: no-follow 完整读取保留 OSError；权限故障不能变成不存在或空账本。
# 函数用途: 读取 store.json 一代字节，供摘要和解析共用。
def _read_store_bytes(path: Path) -> bytes | None:
    try:
        return read_atomic_store_bytes(path)
    except OSError as exc:
        code = "SCHEDULER_STORE_BUSY" if isinstance(exc, BlockingIOError) else "SCHEDULER_STORE_UNREADABLE"
        raise SchedulerStateError("owner scheduler snapshot could not be read", error_code=code,
            details={"error_type": type(exc).__name__, "errno": exc.errno}) from exc


def _store_bytes_digest(data: bytes) -> str:
    """内容摘要(blake2b-128): 覆盖时间戳粒度粗的文件系统上的同尺寸原地改写。"""
    return hashlib.blake2b(data, digest_size=16).hexdigest()


# LLM: 原子快照与现读stat共同绑定字节；读取前冻结可信窗口，保留主线no-follow完整快照与错误事实，不恢复来源旧写锁。
# 函数用途: 读取一代完整账本并选择性生成摘要，供查询或跨调用 waiting 缓存校验。
def _probe_store_bytes(path: Path, *, digest: bool = True) -> _StoreBytesProbe | None:
    stat_key = _store_stat_key(path)
    if stat_key is None:
        return None
    trusted_at_read = cache_entry_trustworthy(stat_key[0])
    data = _read_store_bytes(path)
    if data is None:
        return None
    return _StoreBytesProbe(
        stat_key=stat_key,
        digest=_store_bytes_digest(data) if digest else "",
        data=data,
        trusted_at_read=trusted_at_read,
    )


# LLM: 解析失败只报告异常类型与行列，不向外暴露正文；schema/owner 校验继续由原仓库裁决。
# 函数用途: 解析同一代字节，并为坏 JSON、UTF-8 或根类型提供结构化诊断。
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
        if isinstance(exc, json.JSONDecodeError):
            report.update(line_no=exc.lineno, column=exc.colno)
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
# 的写入淘汰掉这条 key(上限只有 _WAITING_PROJECTION_MAX_ENTRIES_COUNT 条),随后 move_to_end 直接
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


# 只读调度投影大小估算最多递归的层数，超限即不用廉价缓存。
_PROJECTION_ESTIMATE_MAX_DEPTH_COUNT = 8
# 只读调度投影单容器最多估算的项数，超限即走原账本读取。
_PROJECTION_ESTIMATE_MAX_ITEM_COUNT = 1024

# LLM: 计数仅供诊断，不决定调度状态或是否读取；与投影缓存共用短锁。
# 函数用途: 累计命中、完整读取和放弃缓存的观察次数。
def _bump_store_projection_stat(name: str) -> None:
    """投影缓存结构化计数(命中/回退/守卫/跳过);缺键容忍,计数永不影响主链路。"""
    with _STORE_PROJECTION_LOCK:
        _STORE_PROJECTION_STATS[name] = int(_STORE_PROJECTION_STATS.get(name, 0)) + 1


# LLM: 仅供只读观测取得不可变投影；不代表当前身份或可执行状态，不更新校验时钟。
# 函数用途: 取得现存投影供测试和诊断查看。
def _cached_store_projection(cache_key: tuple[str, str, str, str]) -> _StoreProjection | None:
    """读投影缓存条目(只读快照,不改变 LRU 顺序;命中判定请走 _lookup_store_projection)。"""
    with _STORE_PROJECTION_LOCK:
        return _STORE_PROJECTION_CACHE.get(cache_key)


# LLM: 命中判定与 LRU 更新必须**同锁原子**完成。投影缓存是多 owner 共享的(每个 owner 的
# store.json 文件锁互不互斥),旧实现"先 lookup 释放锁、再单独 move_to_end"中间会被其它 owner
# 的写入淘汰掉这条 key(上限只有 _STORE_PROJECTION_MAX_ENTRIES_COUNT 条),随后 move_to_end 直接
# KeyError 冒到等待任务对账路径。这里把"读条目 + 核对 stat 键 + 核对内容摘要 + 提到最新"放进
# 同一把锁:条目要么整条命中(并原子提级),要么整条不命中,不存在"确认过却又消失"的中间态。
# digest 命中说明字节刚被完整读过并核对过,顺带刷新 verified_at(60 秒全量核验基准);
# 便宜命中(_lookup_cheap_projection)不刷新它,到点强制重核。
# 函数用途: 原子判定投影是否命中;第二个返回值表示"有条目但内容摘要已变"(调用方据此走守卫回退)。
def _lookup_store_projection(
    cache_key: tuple[str, str, str, str],
    *,
    stat_key: tuple[int, int, int, int],
    digest: str,
) -> tuple[_StoreProjection | None, bool]:
    now = time.time()
    with _STORE_PROJECTION_LOCK:
        entry = _STORE_PROJECTION_CACHE.get(cache_key)
        if entry is None or entry.stat_key != stat_key:
            return None, False
        if entry.digest != digest:
            return None, True
        entry = replace(entry, verified_at=now)
        _STORE_PROJECTION_CACHE[cache_key] = entry
        _STORE_PROJECTION_CACHE.move_to_end(cache_key)
        return entry, False


# LLM: 便宜命中(一期候选②): 只做一次 stat,不读字节、不算摘要。条件与设计逐字对应:
#   ① stat 键(mtime_ns/size/inode/device)与缓存一致(原子替换必然换 inode → 失效);
#   ② 文件 mtime 已离开 2 秒粗粒度保护窗口(cache_freshness 的唯一实现,防低精度时间片里
#      "先读后写"把旧内容留在缓存里);
#   ③ 距最近一次全量核验(读字节+摘要)不足 60 秒(到点强制重核一次,防时间戳精度与极端竞态)。
#   命中不刷新 verified_at;未命中返回 None,调用方走原路径(宁多查不漏查)。
# 函数用途: 判断缓存投影能否仅凭文件身份复用;命中时原子提级 LRU。
def _lookup_cheap_projection(
    cache_key: tuple[str, str, str, str],
    *,
    stat_key: tuple[int, int, int, int],
) -> _StoreProjection | None:
    now = time.time()
    with _STORE_PROJECTION_LOCK:
        entry = _STORE_PROJECTION_CACHE.get(cache_key)
        if entry is None or entry.stat_key != stat_key:
            return None
        if not cache_entry_trustworthy(stat_key[0]):
            return None
        if now - entry.verified_at >= _STORE_PROJECTION_FULL_READ_INTERVAL_SECONDS:
            return None
        _STORE_PROJECTION_CACHE.move_to_end(cache_key)
        return entry


# LLM: 移除进程内优化状态，不写用户账本；下一读取必须重建，不允许返回旧内容。
# 函数用途: 在读取失败或身份失配后丢弃缓存。
def _drop_store_projection(cache_key: tuple[str, str, str, str]) -> None:
    """主动丢弃缓存条目(解析失败/内容摘要守卫/文件消失时的保守回退)。"""
    with _STORE_PROJECTION_LOCK:
        _STORE_PROJECTION_CACHE.pop(cache_key, None)


# LLM: 仅保留排队候选轻量键，坏数值标记不缓存，不能吞掉原读取错误或缓存now判断。
# 函数用途: 提取排队时刻与运行编号，实际消费仍重新读取完整运行。
def _collect_queued_keys(
    runs: list[dict[str, object]],
) -> tuple[tuple[tuple[float, float, str], ...], bool]:
    """收集排队候选轻量键；无法转换的数值只令投影不可缓存，原读取仍可报告错误。"""
    keys: list[tuple[float, float, str]] = []
    ok = True
    for run in runs:
        if str(run.get("status") or "") != "queued":
            continue
        try:
            keys.append(
                (
                    float(run.get("dispatch_after") or 0.0),
                    float(run.get("scheduled_for") or 0.0),
                    str(run["run_id"]),
                )
            )
        except (TypeError, ValueError):
            ok = False
    return tuple(keys), ok


# LLM: 冷读和关闭优化都只在无候选时免锁；坏数值仍回原事务保留原错误语义，快照不授权预约。
# 函数用途: 现读同一代合法 job/run，判断是否值得进入预约事务。
def _snapshot_due_candidates_exist(repository, *, now: float) -> bool:
    store = repository._store_snapshot()
    jobs, _job_errors = repository._valid_jobs(store)
    runs, _run_errors = repository._valid_runs(store)
    keys, ok = _collect_due_job_keys(jobs, runs)
    return not ok or any(due_at <= now for due_at, _job_id in keys)


# LLM: 只接受主线解析的本 owner 活跃 run；不变的 wake 深复制返回，忙碌或其他情况仍交原写事务验证。
# 函数用途: 在取锁前识别相同或空唤醒信号的幂等挂接，并发写繁忙时退回加锁路径。
def _attached_wake_snapshot(repository, run_id: str, wake_signal_id: str) -> dict[str, object] | None:
    try:
        run, _error = repository._parse_run(repository._store_snapshot()["runs"].get(str(run_id or "")))
    except SchedulerStateError as exc:
        if exc.error_code == "SCHEDULER_STORE_BUSY":
            return None
        raise
    if run is not None and (not wake_signal_id or str(run.get("wake_signal_id") or "") == wake_signal_id):
        return deepcopy(run)
    return None


# LLM: 输入须为原解析器的合法 job/run；普通 job 按 job 阻挡、监控按 rule 阻挡，坏时间拒缓存并保守回退。
# 函数用途: 收集合并退避后的可预约时间键，排除已有活动运行但不阻挡其他独立规则。
def _collect_due_job_keys(
    jobs: list[dict[str, object]],
    runs: list[dict[str, object]],
) -> tuple[tuple[tuple[float, str], ...], bool]:
    keys: list[tuple[float, str]] = []
    ok = True
    active_jobs = {str(run["job_id"]) for run in runs}
    active_rules = {(str(run["job_id"]), str(run.get("rule_id") or "")) for run in runs}
    for job in jobs:
        if job["status"] != "active":
            continue
        job_id = str(job["job_id"])
        rules = job.get("rules")
        if rules is None and job_id in active_jobs:
            continue
        try:
            job_next = float(job.get("next_run_at") or 0.0)
            job_backoff = float(job.get("backoff_until") or 0.0)
        except (TypeError, ValueError):
            ok = False
            continue
        if job_next <= 0:
            continue
        for rule in rules if rules is not None else [job]:
            if rules is not None and (not rule["enabled"] or (job_id, str(rule["id"])) in active_rules):
                continue
            try:
                next_run_at = float(rule.get("next_run_at") or 0.0)
                backoff_until = float(rule.get("backoff_until") or 0.0)
            except (TypeError, ValueError):
                ok = False
                continue
            if next_run_at > 0:
                keys.append((max(job_next, job_backoff, next_run_at, backoff_until), job_id))
    return tuple(sorted(keys)), ok


# 函数用途: 有界估算一个 JSON 值在缓存里的近似字节数; 超深/超宽/未知类型返回 None(不可靠,不缓存)。
# LLM: 只估算JSON投影载荷；超深、超宽或未知类型返回None拒绝缓存，不阻塞业务读取。
# 函数用途: 递归有界估算一个值的驻留载荷。
def _estimate_value_bytes(value: object, depth: int) -> int | None:
    if depth > _PROJECTION_ESTIMATE_MAX_DEPTH_COUNT:
        return None
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    if isinstance(value, (bool, int, float)) or value is None:
        return 8
    if isinstance(value, dict):
        return _estimate_mapping_bytes(value, depth)
    if isinstance(value, (list, tuple)):
        return _estimate_sequence_bytes(value, depth)
    return None


# 函数用途: 有界估算字典(键+值)的字节数; 元素超预算或子值不可靠返回 None。
# LLM: 字典估算包含嵌套键和值，预算失效则禁止缓存，不截断原业务数据。
# 函数用途: 估算对象载荷并拒绝无法可靠估算的结构。
def _estimate_mapping_bytes(mapping: dict[object, object], depth: int) -> int | None:
    if len(mapping) > _PROJECTION_ESTIMATE_MAX_ITEM_COUNT:
        return None
    total = 8
    for key, item in mapping.items():
        item_bytes = _estimate_value_bytes(item, depth + 1)
        if item_bytes is None:
            return None
        total += len(str(key).encode("utf-8")) + item_bytes
    return total


# 函数用途: 有界估算列表/元组的字节数; 元素超预算或子值不可靠返回 None。
# LLM: 列表估算受元素数量与递归预算限制；不改变原返回记录。
# 函数用途: 累加序列载荷，超预算时仅放弃优化。
def _estimate_sequence_bytes(items: list[object] | tuple[object, ...], depth: int) -> int | None:
    if len(items) > _PROJECTION_ESTIMATE_MAX_ITEM_COUNT:
        return None
    total = 8
    for item in items:
        item_bytes = _estimate_value_bytes(item, depth + 1)
        if item_bytes is None:
            return None
        total += item_bytes
    return total


# 函数用途: 有界估算单条 waiting 行的驻留字节(含嵌套字段); 不可靠返回 None → 不缓存。
# LLM: waiting完整行必须计入嵌套载荷；未知估算结果不能当成零字节。
# 函数用途: 估算一条等待运行的缓存载荷。
def _estimate_row_bytes(row: dict[str, object]) -> int | None:
    return _estimate_value_bytes(row, 0)


# 函数用途: 近似估算单条投影的载荷字节; 任一行不可靠或累计超门限返回 None(不缓存)。
# LLM: 三种切片总载荷必须有界；超额返回None，只禁止缓存，不丢弃业务事实。
# 函数用途: 计算一条投影能否放入缓存的载荷界限。
def _estimate_projection_payload_bytes(
    waiting_rows: tuple[dict[str, object], ...],
    queued_keys: tuple[tuple[float, float, str], ...],
    due_job_keys: tuple[tuple[float, str], ...],
) -> int | None:
    """有界估算等待完整行与两类轻量键的载荷，超额或未知返回None，业务结果仍完整。"""
    total = len(queued_keys) * 96 + len(due_job_keys) * 64
    for row in waiting_rows:
        row_bytes = _estimate_row_bytes(row)
        if row_bytes is None:
            return None
        total += row_bytes
        if total > _STORE_PROJECTION_MAX_BYTES_COUNT:
            return None
    return total


# LLM: 仅所有计划角色都已登记且完成的运行可压缩；尚未登记完、未发或待补档的债务不删除，当前完成运行优先保留。
# 函数用途: 去除已完成信封正文并只保留最近128条完成运行的轻量回执。
def _compact_delivery_outbox(outbox, current_run_id):
    complete = []
    for run_id, targets in outbox.items():
        if not targets or not all(row.get('state') == 'committed' and row.get('run_delivery_complete') is True for row in targets.values()):
            continue
        for row in targets.values():
            row.pop('frozen', None)
            row.pop('business_result', None)
        complete.append((max(float(row.get('updated_at', 0)) for row in targets.values()), run_id))
    complete.sort(reverse=True)
    keep = {current_run_id} if any(run_id == current_run_id for _stamp, run_id in complete) else set()
    for _stamp, run_id in complete:
        if len(keep) < 128:
            keep.add(run_id)
    for _stamp, run_id in complete:
        if run_id not in keep:
            outbox.pop(run_id)


# LLM: 同锁登记与LRU逐出，不延长验证时钟、不写主账；key包含规范路径和owner。
# 函数用途: 发布一个已校验投影并回收最旧owner缓存。
def _write_store_projection_cache(cache_key: tuple[str, str, str, str], projection: _StoreProjection) -> None:
    """原子写入投影缓存并按上限逐出最旧条目(锁内只做赋值与 LRU 维护)。"""
    with _STORE_PROJECTION_LOCK:
        _STORE_PROJECTION_CACHE[cache_key] = projection
        _STORE_PROJECTION_CACHE.move_to_end(cache_key)
        while len(_STORE_PROJECTION_CACHE) > _STORE_PROJECTION_MAX_ENTRIES_COUNT:
            _STORE_PROJECTION_CACHE.popitem(last=False)


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


# LLM: Lease expiry is not proof of death; only a missing PID or a verified PID/starttime mismatch is reclaimable.
#   启动指纹统一用 common.heartbeat.process_start_time 的跨平台字符串；比较走 heartbeat.start_time_relation
#   （唯一三态实现）：只有 different 判死，格式不可比（数字 vs 旧 macOS lstart 文本）、空串、坏值一律 unverifiable。
# 函数用途: 普通 claim 和恢复器共用 runner 三态判断，身份读不出时返回 unverifiable 并阻止接管。
def _runner_liveness(run: dict[str, object]) -> str:
    raw_pid = run.get("runner_pid")
    if isinstance(raw_pid, bool):
        return "unverifiable"
    try:
        pid = int(raw_pid or 0)
    except (TypeError, ValueError):
        return "unverifiable"
    state = _process_state(pid)
    if state == "dead":
        return "dead"
    if state != "alive":
        return "unverifiable"
    recorded_start = run.get("runner_start_time")
    if recorded_start is None:
        return "alive"
    current_start = process_start_time(pid)
    if current_start is None:
        return "unverifiable"
    relation = start_time_relation(recorded_start, current_start)
    return {"same": "alive", "different": "dead"}.get(relation, "unverifiable")


# LLM: Build recovery updates from one owner-store snapshot; callers apply the returned patch under the existing store lock.
# 函数用途: 挑出已过期且死亡证明成立的 run，给 recovery 与普通 claim 共用同一裁决。
def _collect_expired_dead_runs(
    runs: dict[str, object],
    parse_run: Callable[[object], tuple[dict[str, object] | None, str]],
    current: float,
) -> dict[str, dict[str, object]]:
    recovered: dict[str, dict[str, object]] = {}
    for run_id, raw in runs.items():
        run, _error = parse_run(raw)
        if run is None:
            continue
        resolved = _resolve_expired_claim(run, current)
        if resolved is None or resolved is run:
            continue
        recovered[run_id] = resolved
    return recovered


# LLM: Both ordinary claims and crash recovery must use this single expiry/death-proof decision; None means hold the old claim.
#   死亡证明成立后按结构化状态分流：claimed（还没 mark running，动作未开始）转 queued 可重领；running（动作可能已产生
#   副作用）转 unknown 终态、不自动重跑——定时动作的 operation_id 重放（SLP-2B）接好之前，不能把“可能做过”当“没做过”。
# 函数用途: 只在 claim 到期且旧 runner 被证实死亡时生成 queued 或 unknown 过渡，其余情况保留或阻止接管。
def _resolve_expired_claim(
    run: dict[str, object], current: float
) -> dict[str, object] | None:
    if run["status"] not in {"claimed", "running"}:
        return run
    if float(run.get("claim_expires_at") or 0.0) > current:
        return None
    if _runner_liveness(run) != "dead":
        return None
    if run["status"] == "running":
        return _unknown_after_dead_runner(run, now=current)
    return _queued_after_dead_runner(run, now=current)


# LLM: 已开跑 run 的执行者死亡后只能落 unknown 终态（与 SLP-2A 之前的崩溃恢复同形）；不清 claim 字段，留作核对证据。
# 函数用途: 生成“执行者已死、动作结果未知”的终态记录，之后不会再被领取。
def _unknown_after_dead_runner(run: dict[str, object], *, now: float) -> dict[str, object]:
    return {**run, "status": "unknown", "ended_at": now, "updated_at": now}


# LLM: This transition is allowed only after _runner_liveness proves the old owner dead; the next claim increments the preserved epoch.
# 函数用途: 清掉死亡 runner 的租约并落成 queued，保留 run 内容供下一位唯一领取者继续。
def _queued_after_dead_runner(run: dict[str, object], *, now: float) -> dict[str, object]:
    return {
        **run,
        "status": "queued",
        "claim_id": "",
        "claimed_at": 0.0,
        "claim_expires_at": 0.0,
        "runner_pid": 0,
        "runner_start_time": None,
        "updated_at": now,
    }


# LLM: New claim_<epoch>_<nonce> tokens fence writes without changing SchedulerService's claim_id-only API; legacy claim_<nonce> means epoch 0.
# 函数用途: 从不透明 claim_id 中提取旧版 epoch 0 或新格式中的正整数 epoch。
def _claim_epoch_from_id(claim_id: str) -> int | None:
    parts = str(claim_id or "").split("_", 2)
    if len(parts) == 2 and parts[0] == "claim":
        return 0
    if len(parts) != 3 or parts[0] != "claim" or not parts[2]:
        return None
    try:
        epoch = int(parts[1])
    except ValueError:
        return None
    return epoch if epoch > 0 else None


# LLM: Every mutation validates both the current opaque claim and its monotonic epoch, fencing stale scheduler workers.
# 函数用途: 同时核对 claim_id 和 epoch，供心跳、释放、运行态变更及终态提交共用。
def _claim_matches(run: dict[str, object], claim_id: str) -> bool:
    expected_id = str(claim_id or "")
    if not expected_id or str(run.get("claim_id") or "") != expected_id:
        return False
    token_epoch = _claim_epoch_from_id(expected_id)
    if token_epoch is None:
        return False
    try:
        current_epoch = int(run.get("claim_epoch") or 0)
    except (TypeError, ValueError):
        return False
    return current_epoch == token_epoch


# LLM: 纯规范化创建输入，幂等摘要包含 execution/precheck/policy/delivery；不同明确策略不能被判成同一登记。
# 函数用途: 构造新的计划记录，落盘及配额由上层原事务负责。
def _new_job_payload(
    *,
    owner: dict[str, str],
    request: SchedulerJobCreateRequest,
    now: float,
) -> dict[str, object]:
    if request.rules is not None:
        from .monitor_state import new_monitor
        return new_monitor(owner, request, now)
    normalized_name, normalized_prompt = _validate_name_prompt(request.name, request.prompt)
    normalized_schedule = validate_schedule(request.schedule)
    normalized_refs = _normalize_skill_refs(request.skill_refs)
    policy = normalize_policy(request.policy)
    execution = normalize_execution(request.execution)
    if normalized_schedule['kind'] == 'event_source':
        execution = {**execution, 'mode': 'isolated'}
    precheck = normalize_precheck(request.precheck)
    delivery = normalize_delivery(request.delivery)
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
                "policy": policy,
                "execution": execution,
                "precheck": precheck,
                "delivery": delivery,
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
        "source_thread_id": request.source_thread_id,
        "source_task_id": request.source_task_id,
        "creator": deepcopy(request.creator),
        "run_count": 0,
        "schedule": normalized_schedule,
        "status": "active",
        "next_run_at": next_run,
        "misfire_grace_seconds": grace,
        "delivery": delivery,
        "skill_refs": normalized_refs,
        "policy": policy,
        "execution": execution,
        "precheck": precheck,
        "consecutive_failures": 0,
        "backoff_until": 0.0,
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


# LLM: 只改显式 patch，保留未给出的对象和已确认 checkpoint；已预留 run 的快照不在这里改写。
# 函数用途: 校验计划和通知更新并增加版本，让新周期读取新的完整策略；不改来源或已预留运行。
def _updated_job(
    job: dict[str, object], patch: dict[str, object], *, now: float
) -> dict[str, object]:
    if job.get("rules") is not None or "rules" in patch:
        from .monitor_state import update_monitor
        return update_monitor(job, patch, now)
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
        "policy": normalize_policy(patch.get("policy") if "policy" in patch else job.get("policy")),
        "execution": {**normalize_execution(patch.get("execution") if "execution" in patch else job.get("execution")),
            **({'mode': 'isolated'} if schedule['kind'] == 'event_source' else {})},
        "precheck": normalize_precheck(patch.get("precheck") if "precheck" in patch else job.get("precheck")),
        "delivery": normalize_delivery(patch.get("delivery") if "delivery" in patch else job.get("delivery")),
        "updated_at": now,
        "version": int(job["version"]) + 1,
    }


# LLM: New durable runs start at epoch 0; every executable claim advances it and embeds it in claim_id for CAS validation.
# 函数用途: 构造初始 queued run，冻结通知策略、带版本通知绑定及 scheduler 持久字段，供普通领取与恢复流程沿同一 epoch 递增。
def _reserved_run(
    job: dict[str, object],
    *,
    trigger: str,
    scheduled_for: float,
    dispatch_after: float,
    now: float,
) -> dict[str, object]:
    run_id = f"srun_{uuid.uuid4().hex}"
    execution = normalize_execution(job.get("execution"))
    payload = {
        "schema_version": _RUN_SCHEMA,
        "run_id": run_id,
        "job_id": str(job["job_id"]),
        "rule_id": str(job.get("rule_id") or ""),
        "use_llm": job.get("use_llm", True),
        "tool": deepcopy(job.get("tool")),
        "creator": deepcopy(job.get("creator")),
        "job_version": int(job["version"]),
        "owner": deepcopy(job["owner"]),
        "thread_id": f"thread-{run_id}" if execution["mode"] == "isolated" else str(job["thread_id"]),
        "origin_thread_id": str(job.get("source_thread_id") or job["thread_id"]),
        "source_thread_id": str(job.get("source_thread_id") or job["thread_id"]),
        "execution": execution,
        "precheck": deepcopy(job.get("precheck")),
        "delivery": normalize_delivery(job.get("delivery")),
        "notification_binding": deepcopy(normalize_notification_binding(job.get('notification_binding'))),
        "precheck_checkpoint": deepcopy(job.get("precheck_checkpoint")),
        "source_task_id": str(job.get("source_task_id") or ""),
        "name": str(job["name"]),
        "prompt": str(job.get("prompt") or job["name"]),
        "skill_refs": deepcopy(job.get("skill_refs") or []),
        "trigger": trigger,
        "scheduled_for": float(scheduled_for),
        "dispatch_after": float(dispatch_after),
        "queue_deadline_at": now + normalize_policy(job.get("policy"))["queue_timeout_seconds"]
                             if normalize_policy(job.get("policy"))["queue_timeout_seconds"] else 0.0,
        "misfire_policy": normalize_policy(job.get("policy"))["misfire"],
        "lateness_seconds": max(0.0, now - scheduled_for),
        "status": "queued",
        "wake_signal_id": "",
        "claim_id": "",
        "claim_epoch": 0,
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
    if (job.get('schedule') or {}).get('kind') == 'event_source':
        state = job['event_source_state'][str(job.get('rule_id') or '')]
        payload.update(event_source={'generation': state['generation'], 'source': deepcopy(job['schedule']['source'])}, event_batch=[])
    return payload


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


# LLM: 原终态事务同时提交失败策略与 checkpoint；只有 done 且预检定义未变可确认摘要，不以模型正文判断成功。
# 函数用途: 将一次运行的结果投回 job，保留暂停/删除及自然周期语义。
def _job_after_run(
    job: dict[str, object],
    run: dict[str, object],
    *,
    now: float,
) -> dict[str, object]:
    if job.get("rules") is not None:
        from .monitor_state import finish_rule
        return finish_rule(job, run, now)
    updated = apply_run_policy({
        **job,
        "last_run_at": now,
        "last_run_status": str(run["status"]),
        "last_error_code": str(run.get("error_code") or ""),
        "updated_at": now,
        "version": int(job["version"]) + 1,
    }, run, now=now)
    observation = run.get("precheck_observation") or {}
    if (run.get("status") == "done" and observation.get("status") == "changed"
            and run.get("precheck") == job.get("precheck")):
        updated["precheck_checkpoint"] = {
            "fingerprint": observation["fingerprint"], "run_id": run["run_id"], "confirmed_at": now,
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
    if schedule['kind'] == 'event_source':
        return 0.0
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


# LLM: 调度持久化沿原字段保存完整版本引用；公开 Skill 的旧两字段保持，包的类型和激活字段不可被投影丢弃。
# 函数用途: 验证并去重选定能力，拒绝同 ID 的矛盾版本而不替换既有定时任务。
def _normalize_skill_refs(raw: object) -> list[dict[str, str]]:
    from ..capability.task_references import normalize_skill_reference

    rows = raw if isinstance(raw, list) else []
    if len(rows) > _MAX_SKILL_REFS_COUNT:
        raise ScheduleValidationError(f"skill_refs exceeds {_MAX_SKILL_REFS_COUNT} entries")
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        try:
            reference = normalize_skill_reference(row)
        except ValueError as exc:
            raise ScheduleValidationError(str(exc)) from exc
        stable_id = reference["stable_id"]
        if stable_id not in seen:
            normalized.append(reference)
            seen.add(stable_id)
        elif reference not in normalized:
            raise ScheduleValidationError("skill_refs contains conflicting versions")
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
