# LLM: 原load与显式workspace reader共用同一canonical解析/normalizer，不初始化manager、不复制任务schema或查找器。
# 模块用途: 给无Agent的已认证领域读取原子任务，原写入和投影流程保持。
# LLM: 正MP沿原请求短事务限制既有写者，不改变main canonical/RuntimeDB/F4和Count0逻辑，检查所有原调用方锁序。
# 模块用途: 维护原子代理写入与收口，原执行撤权后拒绝迟到副作用。
# LLM: 正负缓存每次核locator指纹和目标存在性；只读predicate锁外筛选私有引用，只有命中项复制，错误仍保留。
# 模块用途: 保存子代理任务与派生视图，在保持实时逐文件核验时减少历史巡检解析和复制。
"""Persistence service for SubAgentManager task records.

Saves are split into two phases. The canonical task-local state is written first
and remains the source of truth; owner indexes, tree projections, status reports
and LocalStore mirrors are derived projections. Projection failures are recorded
as warnings instead of corrupting or blocking the canonical save.
"""

from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from ....common.cache_freshness import CACHE_TRUST_AGE_SECONDS, cache_stat_signature
from ....common.json_io import (
    locked_json_path,
    read_json_object_report,
    write_private_json_file_atomic,
)
from ....common.nofollow_fs import read_bytes_beneath
from ....common.opaque_id import validate_opaque_id
from ....common.request_execution_ports import gateway_short_transaction
from ....common.value_parsing import sequence_strings
from ....concurrency.exceptions import ConcurrencyConflictError
from ....runtime_db.operations import directory_id_for_opaque
from ....runtime_errors import runtime_error_report
from ...model_capabilities import capability_request_requires_parent_resolution
from ...models import (
    CAPABILITY_GRANTED_BLOCKER_FAILURE_TYPES,
    PROVIDER_SUPPLY_FAILURE_TYPES,
    RETRYABLE_RUNNER_FAILURE_TYPES,
    SUBAGENT_FAILED_RESULT_STATUSES,
    SUBAGENT_RECOVERY_CLOSED_STATUSES,
    SUBAGENT_RETRYABLE_FAILURE_STATUSES,
    CapabilityGap,
    CapabilityGrant,
    CapabilityRequest,
    ChannelProbeCheck,
    FailureHandoff,
    FailureType,
    InheritanceManifest,
    RuntimeIdentity,
    SecuritySignal,
    StatusReport,
    SubAgentTask,
    TakeoverRecord,
    TaskStatus,
    VerificationEvidence,
    VerificationStatus,
    failure_type_from_task_status,
    task_has_failure_status,
    task_has_status,
    task_status_in,
)
from ...utils import _apply_missing_paths
from ..agent_run_state import (
    build_agent_run_state,
    build_agent_state_locator,
    build_owner_agent_projection,
    canonical_state_path_from_payload,
    is_agent_state_locator,
    read_agent_state_payload,
    write_agent_run_state,
)
from ..checkpoint_artifacts import build_checkpoint_artifact_payloads
from ..output_alignment import (
    record_locked_files_change,
    sanitize_self_locked_delivery_targets,
)
from ..recovery.strategy import RECOVERY_COUNTER_NAMES, recovery_counters
from ..takeover.readiness import write_takeover_readiness_files
from ..task_workspace_adapter import sync_task_workspace_fields
from .model_normalizers import (
    _field_names,
    _normalize_context_manifest,
    _normalize_context_packs,
    _normalize_evidence_packet,
    _normalize_finding,
    _normalize_nested_model,
    _normalize_quality_contract,
    _normalize_status_report,
)
from .projections import sync_derived_projections

_HIGH_RISK_FAILURE_TYPES = {"tool_output_context_overflow", "context_overflow", "blackbox_output_overflow"}


# LLM: 缓存时效比较统一经过此局部时钟入口；不得替代持久化生命周期时间，测试只应打桩这里而非全局 time 模块。
# 函数用途: 返回解析缓存新鲜度判断所用的当前秒数，隔离测试时钟对其它时间调用的影响。
def _now() -> float:
    return time.time()


@dataclass(frozen=True)
class SubAgentListRunsReport:
    """Result of scanning persisted subagent runs.

    The run list stays best-effort, but load failures are kept as structured
    facts so tree/board callers do not confuse broken bookkeeping with "no
    subagent work exists".
    """

    runs: list[SubAgentTask] = field(default_factory=list)
    load_errors: list[dict[str, Any]] = field(default_factory=list)


# LLM: Canonical root verification accepts either the explicit root relation or the root run
# itself. It never derives lineage from names, descriptions, paths, or status prose.
# 函数用途: 复核索引选出的 run 确实属于指定根任务，防止陈旧查询行串树。
def _canonical_runs_for_root(
    runs: Iterable[SubAgentTask],
    root_task_id: str,
) -> list[SubAgentTask]:
    return [
        task
        for task in runs
        if str(getattr(task, "id", "") or "").strip() == root_task_id
        or str(getattr(task, "root_id", "") or "").strip() == root_task_id
    ]


# LLM: 索引只挑候选，canonical核根；无索引回退同样规范化ID空白，不引入第二种根匹配语义。
# 函数用途: 实现按根任务的“索引选 ID、文件复核”读取，并返回结构化错误。
def _list_runs_for_root_report(
    service: SubAgentPersistenceService,
    root_task_id: str,
) -> SubAgentListRunsReport:
    try:
        root_id = validate_opaque_id(root_task_id, kind="root_task_id")
    except Exception as exc:
        report = runtime_error_report(exc, context="subagents.load_root")
        report["root_task_id"] = str(root_task_id or "")
        return SubAgentListRunsReport(load_errors=[report])

    local_store = getattr(service.manager, "local_store", None)
    tree_reader = getattr(local_store, "list_agent_tree", None)
    if not callable(tree_reader):
        return service.list_runs_selected_report(
            lambda task: bool(_canonical_runs_for_root([task], root_id)),
        )

    try:
        tree = tree_reader(root_id)
        selected_ids = list(
            dict.fromkeys(
                str(getattr(record, "run_id", "") or "").strip()
                for record in list(getattr(tree, "runs", ()) or ())
                if str(getattr(record, "root_task_id", "") or "").strip()
                == root_id
                and str(getattr(record, "run_id", "") or "").strip()
            )
        )
    except Exception as exc:
        report = runtime_error_report(exc, context="subagents.load_root_index")
        report["root_task_id"] = root_id
        return SubAgentListRunsReport(load_errors=[report])

    selected = service.list_runs_by_ids_report(selected_ids)
    canonical = _canonical_runs_for_root(selected.runs, root_id)
    canonical_ids = {
        str(getattr(task, "id", "") or "").strip() for task in canonical
    }
    mismatched_ids = {
        str(getattr(task, "id", "") or "").strip()
        for task in selected.runs
        if str(getattr(task, "id", "") or "").strip() not in canonical_ids
    }
    load_errors = list(selected.load_errors)
    if mismatched_ids:
        report = runtime_error_report(
            ValueError("子代理根索引与 canonical task 不一致"),
            context="subagents.load_root_index",
        )
        report["root_task_id"] = root_id
        report["run_ids"] = sorted(mismatched_ids)
        load_errors.append(report)
    return SubAgentListRunsReport(runs=canonical, load_errors=load_errors)


# LLM: canonical文件仍是权威；锁外核双文件指纹与各自信任龄，公开副本经同一恢复坏账校验。
# 类用途: 管理子代理持久化与实时列表筛选；复制优化不跨轮冻结任务集合。
class SubAgentPersistenceService:
    """Read and write SubAgentTask records for SubAgentManager."""

    # LLM: 粗 mtime 保护窗口与指纹判定的唯一实现在 common/cache_freshness；本类不再自带第二套阈值。
    # 类用途: 管理子代理任务的持久化与列表读取；缓存规则改变时同步检查 list_runs 和 create_subagents 的状态复用测试。
    _RUN_CACHE_COARSE_MTIME_GUARD_SECONDS = CACHE_TRUST_AGE_SECONDS
    # 保留有界容量，同时覆盖常见历史扫描规模，避免超过512条就发生FIFO循环抖动。
    _MAX_RUN_CACHE_ENTRIES = 4096
    _MAX_RUN_CACHE_ITEM_BYTES = 256 * 1024
    _MAX_NEGATIVE_RUN_CACHE_ENTRIES = 4096

    # LLM: 解析、目标stat和复制在锁外；锁只保护有界缓存，canonical与locator共同决定失效。
    # projection may share it, but callers always receive deep copies and canonical files remain
    # authoritative across processes.
    # 缺失canonical仅缓存稳定locator的指纹与既有reader解析路径，不保存正文或新的状态权威。
    # 函数用途: 初始化线程安全的解析和缺失目标缓存，供Gateway历史任务巡检复用。
    def __init__(self, manager: Any):
        self.manager = manager
        # task.json 文件指纹未变时复用解析对象，避免每轮 dispatch 全量重读+重解析。
        # 近期 mtime 仍强制重读，以防多代替换后 inode 重用且时间戳同片；旧文件继续享有缓存收益。
        # 其余情况同时核对 inode、大小和 ctime，避免只看 mtime 漏掉原子替换。
        # 命中后仍只 stat；返回 deepcopy 副本，调用方修改不会污染缓存对象。
        self._run_cache: dict[str, tuple[tuple[int, int, int, int, int], SubAgentTask]] = {}
        self._run_cache_canonical: dict[str, tuple[Path, tuple]] = {}
        self._missing_canonical_cache: dict[str, tuple[tuple[int, int, int, int, int], Path]] = {}
        self._run_cache_lock = threading.RLock()

    @property
    def workspace(self) -> Path:
        return self.manager.workspace

    # LLM: 原manager与冷C只读同一canonical reader，不初始化manager或生成投影；坏记录不能回退主任务。
    # 函数用途: 读取精确run并沿原规范化恢复任务。
    def load(self, run_id: str) -> SubAgentTask:
        return load_subagent_task(self.workspace, run_id)

    # LLM: 数据规范化唯一实现位于同模块纯函数；保全部nested模型/当前schema，不把展示投影当权威。
    # 函数用途: 为原manager恢复canonical任务。
    def task_from_payload(self, data: dict[str, Any]) -> SubAgentTask:
        return task_from_payload(data)

    def list_runs(self) -> list[SubAgentTask]:
        """Scan the workspace for subagent task records."""

        return self.list_runs_report().runs

    # LLM: 默认列表复用同一逐文件扫描器且不筛选，保持每条返回深副本和原load_errors合同。
    # 函数用途: 读取全部任务及逐文件错误，不引入跨阶段快照或TTL。
    def list_runs_report(self) -> SubAgentListRunsReport:
        return self.list_runs_selected_report()

    # LLM: predicate限可信只读调用方，不得修改task或嵌套字段；返回锁外副本，筛选自身错误须原样传播。
    # 函数用途: 仅复制命中任务的列表，未命中项仍核新鲜度，筛选失败不伪装为空结果。
    def list_runs_selected(self, predicate: Callable[[SubAgentTask], bool] | None = None) -> list[SubAgentTask]:
        return self.list_runs_selected_report(predicate).runs

    # LLM: 仅接受可信无副作用predicate；逐文件核验和复制错误仍报load_errors，筛选错误在扫描淘汰后原样抛出。
    # 函数用途: 按条件返回独立副本和坏行报告，保留实时性、复制替身与筛选异常合同。
    def list_runs_selected_report(self, predicate: Callable[[SubAgentTask], bool] | None = None) -> SubAgentListRunsReport:
        copy_before_filter = (
            predicate is None or "_cached_run_copy" in self.__dict__
            or getattr(self._cached_run_copy, "__func__", None) is not SubAgentPersistenceService._DEFAULT_CACHED_RUN_COPY
        )

        runs: list[SubAgentTask] = []
        load_errors: list[dict[str, Any]] = []
        seen: set[str] = set()
        predicate_error: Exception | None = None
        for task_file in sorted(self.workspace.glob("*/task.json")):
            run_id = task_file.parent.name
            seen.add(run_id)
            try:
                task = self._cached_run_copy(run_id, task_file) if copy_before_filter else self._cached_run_ref(run_id, task_file)
                # 筛选故障不属于文件坏账；停用后续回调，但仍完成其余locator核验和缓存淘汰。
                if predicate_error is not None:
                    continue
                try:
                    matched = predicate is None or bool(predicate(task))
                except Exception as exc:
                    predicate_error = exc
                    continue
                if matched:
                    runs.append(task if copy_before_filter else copy.deepcopy(task))
            except Exception as exc:
                report = runtime_error_report(exc, context="subagents.load")
                report["run_id"] = run_id
                report["path"] = str(task_file)
                load_errors.append(report)
        self._evict_run_cache(seen)
        if predicate_error is not None:
            raise predicate_error
        runs.sort(key=lambda item: item.updated_at or item.created_at, reverse=True)
        return SubAgentListRunsReport(runs=runs, load_errors=load_errors)

    # LLM: Exact-id reads are the canonical-data half of an indexed lookup. The caller may use a
    # read projection to select ids, but every returned task is reloaded from its exact canonical
    # locator and malformed/missing rows remain structured errors rather than guessed absence.
    # 函数用途: 只读取给定的子代理记录，避免活动面板为几名直属子代理扫描并复制全部历史任务。
    def list_runs_by_ids_report(
        self,
        run_ids: Iterable[str],
    ) -> SubAgentListRunsReport:
        runs: list[SubAgentTask] = []
        load_errors: list[dict[str, Any]] = []
        selected = list(
            dict.fromkeys(
                str(run_id or "").strip()
                for run_id in run_ids
                if str(run_id or "").strip()
            )
        )
        for raw_run_id in selected:
            task_file: Path | None = None
            try:
                run_id = validate_opaque_id(raw_run_id, kind="run_id")
                task_file = self.workspace / run_id / "task.json"
                if not task_file.exists():
                    raise FileNotFoundError(f"子代理记录不存在: {run_id}")
                runs.append(self._cached_run_copy(run_id, task_file))
            except Exception as exc:
                report = runtime_error_report(exc, context="subagents.load_selected")
                report["run_id"] = raw_run_id
                report["path"] = str(task_file) if task_file is not None else ""
                load_errors.append(report)
        runs.sort(key=lambda item: item.updated_at or item.created_at, reverse=True)
        return SubAgentListRunsReport(runs=runs, load_errors=load_errors)

    # LLM: Root-tree lookup uses LocalStore only to select exact ids, then reloads every selected
    # canonical task file. Managed index errors stay explicit; local-unmanaged callers retain the
    # canonical full-scan path because they deliberately have no lookup projection.
    # 函数用途: 按一个根任务读取其子代理树，避免后台调度每秒复制全部历史 run。
    def list_runs_for_root_report(
        self,
        root_task_id: str,
    ) -> SubAgentListRunsReport:
        return _list_runs_for_root_report(self, root_task_id)

    # LLM: 兼容原复制接缝；私有引用在锁内取出且不原地修改，deepcopy严格发生在缓存锁外。
    # 函数用途: 返回独立任务副本，保留既有测试与精确ID读取的复制合同。
    def _cached_run_copy(self, run_id: str, task_file: Path) -> SubAgentTask:
        return copy.deepcopy(self._cached_run_ref(run_id, task_file))

    _DEFAULT_CACHED_RUN_COPY = _cached_run_copy

    # LLM: stat/解析在锁外，命中/发布同时核locator与canonical年龄和指纹；引用不可改，年轻canonical绝不复用。
    # 函数用途: 核对locator和canonical后复用快照，不让慢磁盘占住其它run的缓存锁。
    def _cached_run_ref(self, run_id: str, task_file: Path) -> SubAgentTask:
        stat = task_file.stat()
        signature = cache_stat_signature(stat)
        with self._run_cache_lock:
            cached = self._run_cache.get(run_id)
            target = self._run_cache_canonical.get(run_id)
            missing = self._missing_canonical_cache.get(run_id)
        age = _now() - stat.st_mtime_ns / 1e9
        if missing is not None and missing[0] == signature and not missing[1].exists():
            raise FileNotFoundError(f"子代理记录不存在: {run_id}")
        if cached is not None and cached[0] == signature and target is not None:
            try:
                canonical_stat = target[0].stat()
                unchanged = (cache_stat_signature(canonical_stat) == target[1]
                             and _now() - canonical_stat.st_mtime_ns / 1e9 >= self._RUN_CACHE_COARSE_MTIME_GUARD_SECONDS)
            except OSError:
                unchanged = False
            if unchanged and age >= self._RUN_CACHE_COARSE_MTIME_GUARD_SECONDS:
                return cached[1]
        canonical = None
        before = None
        if stat.st_size <= self._MAX_RUN_CACHE_ITEM_BYTES:
            payload = json.loads(task_file.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                canonical = canonical_state_path_from_payload(payload) or task_file
                try:
                    before = canonical.stat()
                except FileNotFoundError:
                    if is_agent_state_locator(payload):
                        if age >= self._RUN_CACHE_COARSE_MTIME_GUARD_SECONDS:
                            self._cache_missing_canonical(run_id, task_file, signature, canonical_path=canonical)
                        raise FileNotFoundError(f"canonical subagent state missing for locator: {canonical}") from None
        try:
            task = self.load(run_id)
        except FileNotFoundError as exc:
            with self._run_cache_lock:
                self._run_cache.pop(run_id, None)
                self._run_cache_canonical.pop(run_id, None)
            if age >= self._RUN_CACHE_COARSE_MTIME_GUARD_SECONDS:
                self._cache_missing_canonical(
                    run_id, task_file, signature,
                    canonical_path=getattr(exc, "canonical_path", None),
                )
            raise
        cacheable = False
        if canonical is not None and before is not None:
            try:
                cacheable = (before.st_size <= self._MAX_RUN_CACHE_ITEM_BYTES
                             and cache_stat_signature(canonical.stat()) == cache_stat_signature(before)
                             and cache_stat_signature(task_file.stat()) == signature
                             and age >= self._RUN_CACHE_COARSE_MTIME_GUARD_SECONDS
                             and _now() - before.st_mtime_ns / 1e9 >= self._RUN_CACHE_COARSE_MTIME_GUARD_SECONDS)
            except OSError:
                pass
        with self._run_cache_lock:
            self._missing_canonical_cache.pop(run_id, None)
            if cacheable:
                if run_id not in self._run_cache and len(self._run_cache) >= self._MAX_RUN_CACHE_ENTRIES:
                    evicted = next(iter(self._run_cache))
                    self._run_cache.pop(evicted, None)
                    self._run_cache_canonical.pop(evicted, None)
                self._run_cache[run_id] = (signature, task)
                self._run_cache_canonical[run_id] = (canonical, cache_stat_signature(before))
            else:
                self._run_cache.pop(run_id, None)
                self._run_cache_canonical.pop(run_id, None)
        return task

    # LLM: 路径解析和存在性检查在锁外；只复用原reader核验的Path，锁内仅发布有界负缓存。
    # 函数用途: 为稳定直达的缺失canonical建有界负缓存；传入已解析路径时不重读locator，探测失败不掩盖原错误。
    def _cache_missing_canonical(
        self, run_id: str, task_file: Path, signature: tuple[int, int, int, int, int],
        *,
        canonical_path: Path | None = None,
    ) -> None:
        try:
            if isinstance(canonical_path, Path):
                canonical = canonical_path
            else:
                payload = json.loads(task_file.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    return
                canonical = canonical_state_path_from_payload(payload)
                if canonical is None:
                    return
                declared = Path(str(payload.get("agent_run_workspace_dir") or "").strip())
                if declared != canonical.parent:
                    return
            if canonical.exists():
                return
            with self._run_cache_lock:
                if (
                    run_id not in self._missing_canonical_cache
                    and len(self._missing_canonical_cache) >= self._MAX_NEGATIVE_RUN_CACHE_ENTRIES
                ):
                    self._missing_canonical_cache.pop(next(iter(self._missing_canonical_cache)), None)
                self._missing_canonical_cache[run_id] = (signature, canonical)
        except (OSError, TypeError, ValueError):
            return

    # LLM: 正负缓存及canonical指纹同锁同淘汰；锁外复制持有快照引用，移除不破坏本次返回。
    # 函数用途: 同步清理已删除locator的正负缓存；其它run仍存在时也不能留下陈旧缺失项。
    def _evict_run_cache(self, seen: set[str]) -> None:
        with self._run_cache_lock:
            for cache in (self._run_cache, self._missing_canonical_cache, self._run_cache_canonical):
                for stale in [rid for rid in cache if rid not in seen]:
                    cache.pop(stale, None)

    # LLM: This is the canonical full-state write boundary. Closed lifecycle facts are monotonic;
    # only a structured same-run continuation or lifecycle-repair caller may explicitly reopen one.
    # 文件启动凭据只可由窄 mutation 预留/消费，full save 只允许同身份单调回收。
    # 函数用途: 原子保存子代理权威状态及派生投影，并阻止旧线程把终态写回运行态。
    # LLM: 原T/P3/P5短门先于既有业务锁；Count0零新增IO，保main原CAS/owner/attempt与用户确认，未知不自动写失败或释放。
    # 函数用途: 在原请求仍有效时完成这次已有写入，不持门等待模型或新建执行身份。
    def save(
        self,
        task: SubAgentTask,
        *,
        preserve_child_links: bool = True,
        allow_terminal_reactivation: bool = False,
    ) -> None:
        """Persist a task as JSON plus human-readable Markdown."""

        with gateway_short_transaction('child_canonical_save'):
            _apply_missing_paths(task, self.manager._build_work_order_paths(task.id, task.task_dir or None))
            # canonical state 有两类写者:业务状态保存与 runner-session 窄心跳。
            # 用独立 guard 串行化；不能直接锁 canonical_state.json，因为原子写入
            # 自己还会取该路径的非可重入锁。
            with locked_json_path(_canonical_state_guard_path(self.workspace, task)):
                if _restore_newer_closed_state(
                    self,
                    task,
                    allow_terminal_reactivation=allow_terminal_reactivation,
                ):
                    return
                _merge_concurrent_capability_and_runner_state(self, task)
                _persist_task_inside_guard(
                    self,
                    task,
                    preserve_child_links=preserve_child_links,
                )

    # LLM: Reducers run against the latest canonical task while holding the same cross-process
    # guard used by save and runner-session heartbeats. Keep reducers deterministic and bounded;
    # network/model calls and unrelated filesystem work must remain outside this critical section.
    # 函数用途: 在同一把文件锁内完成“读取最新状态、结构化修改、递增版本、写回”，避免旧快照覆盖。
    # LLM: 原T/P3/P5短门先于既有业务锁；Count0零新增IO，保main原CAS/owner/attempt与用户确认，未知不自动写失败或释放。
    # 函数用途: 在原请求仍有效时完成这次已有写入，不持门等待模型或新建执行身份。
    def mutate(
        self,
        run_id: str,
        reducer: Callable[[SubAgentTask], None],
        *,
        expected_revision: int | None = None,
        preserve_child_links: bool = True,
    ) -> SubAgentTask:
        with gateway_short_transaction('child_canonical_mutate'):
            normalized_run_id = validate_opaque_id(run_id, kind="run_id")
            initial = self.load(normalized_run_id)
            with locked_json_path(_canonical_state_guard_path(self.workspace, initial)):
                task = self.load(normalized_run_id)
                actual_revision = max(0, int(task.state_revision or 0))
                if expected_revision is not None and actual_revision != int(expected_revision):
                    raise ConcurrencyConflictError(
                        task_id=normalized_run_id,
                        expected_version=int(expected_revision),
                        actual_version=actual_revision,
                    )
                reducer(task)
                _advance_granted_capability_resume_state(task)
                _persist_task_inside_guard(
                    self,
                    task,
                    preserve_child_links=preserve_child_links,
                )
                return copy.deepcopy(task)

    # LLM: Narrow runner-session persistence may update only the newest canonical payload and
    # returns False when lifecycle or the exact active attempt fences the old lease.
    # 函数用途: 轻量保存 runner 心跳；终态后拒绝旧会话继续冒充运行中。
    # LLM: 原T/P3/P5短门先于既有业务锁；Count0零新增IO，保main原CAS/owner/attempt与用户确认，未知不自动写失败或释放。
    # 函数用途: 在原请求仍有效时完成这次已有写入，不持门等待模型或新建执行身份。
    def save_runner_session(
        self,
        run_id: str,
        session: dict[str, object],
        *,
        now: float,
    ) -> bool:
        """Persist runner liveness without rebuilding task projections."""

        with gateway_short_transaction('child_runner_session'):
            run_id = validate_opaque_id(run_id, kind="run_id")
            locator_path = self.workspace / run_id / "task.json"
            if not locator_path.exists():
                raise FileNotFoundError(f"子代理记录不存在: {run_id}")
            initial_task = self.task_from_payload(_read_state_payload(locator_path))
            with locked_json_path(_canonical_state_guard_path(self.workspace, initial_task)):
                payload = _read_state_payload(locator_path)
                if _runner_session_write_is_stale(payload, session):
                    return False
                _merge_runner_session_payload(payload, session, now=now)
                canonical_ref = _canonical_state_ref_from_payload(payload)
                if not canonical_ref:
                    raise FileNotFoundError(f"canonical subagent state missing for runner session: {run_id}")
                write_private_json_file_atomic(Path(canonical_ref), payload)
                # list_runs 的文件指纹来自 locator；只刷新这个轻量文件，
                # 让列表入口及时看到新 heartbeat，不重建其余派生投影。
                locator = read_json_object_report(locator_path).payload
                if locator:
                    locator["updated_at"] = now
                    write_private_json_file_atomic(locator_path, locator)
            return True


def _canonical_state_guard_path(workspace: Path, task: SubAgentTask) -> Path:
    # Lock metadata belongs to the system locator plane, not the model-visible
    # task workspace where artifact scans or the runner could mistake it for work.
    # task.id 是 run_id，拼进路径前必须过拒绝式校验（fail-closed：数据损坏
    # 时拒绝写盘而不是把锁文件写到任意目录）。
    run_id = str(getattr(task, "id", "") or "").strip()
    if run_id:
        run_id = validate_opaque_id(run_id, kind="run_id")
        return workspace / run_id / ".canonical_state.guard"
    task_dir = str(getattr(task, "task_dir", "") or "").strip()
    return workspace / (Path(task_dir).name if task_dir else "unknown-run") / ".canonical_state.guard"


# LLM: 原canonical guard内的唯一提交尾部，同时按精确启动/结算边沿更新恢复计数；重复保存不重复计数。
# full-state save and typed mutate: it increments the host revision once, preserves hierarchy and
# takeover projections, sanitizes locks, writes canonical state, then refreshes derived views.
# 函数用途: 在已持有权威锁时完成一次子代理状态提交，并统一递增版本和刷新派生投影。
def _persist_task_inside_guard(
    service: SubAgentPersistenceService,
    task: SubAgentTask,
    *,
    preserve_child_links: bool,
) -> None:
    previous_locked = _existing_locked_files(service, task)
    if preserve_child_links:
        _merge_existing_child_links(service, task)
        _merge_existing_takeover_state(service, task)
    _project_closed_audit_source_state(task)
    now = time.time()
    sanitize_self_locked_delivery_targets(task, now)
    record_locked_files_change(task, previous_locked, now)
    try:
        previous = service.load(task.id)
        existing_revision = int(previous.state_revision or 0)
    except (FileNotFoundError, json.JSONDecodeError, TypeError, ValueError):
        previous = None
        existing_revision = 0
    _update_recovery_counters(task, previous, service.manager)
    task.state_revision = max(existing_revision, int(task.state_revision or 0)) + 1
    task_dir, owner_projection = _prepare_and_write_state(service, task)
    sync_derived_projections(service.manager, task, task_dir, owner_projection)


# LLM: 最多16件/1MiB，nofollow限定任务域；返回逐路径内容指纹/缺失null，异常或超预算返回None，不把部分集合当进展。
# 函数用途: 用真实产物字节版本生成宿主进展证据，防止仅改进度文字解除熔断。
def _recovery_progress_fingerprint(task: SubAgentTask) -> dict[str, str | None] | None:
    root_text = str(task.task_workspace_dir or "")
    if not root_text:
        return None
    root = Path(root_text)
    rows = {}
    remaining = 1024 * 1024
    refs = sorted(set(task.artifact_refs or []))
    if len(refs) > 16:
        return None
    for raw in refs:
        if remaining <= 0:
            return None
        try:
            path = Path(raw)
            relative = path.relative_to(root) if path.is_absolute() else path
            content = read_bytes_beneath(root, relative.parts, max_bytes=min(256 * 1024, remaining), require_dir_fd=True)
            if content is None:
                rows[str(relative)] = None
                continue
            rows[str(relative)] = hashlib.sha256(content).hexdigest()
            remaining -= len(content)
        except (OSError, ValueError, TypeError):
            return None
    return rows


# LLM: 原提交门消费统一校验快照；BLOCKED同样扣失败预算；未知观察单独可见，不制造进展或无进展证据。
# 函数用途: 从真实attempt开始/收口边沿维护失败重试与无进展次数，保留原审计attempt和CAS。
def _update_recovery_counters(task: SubAgentTask, previous: SubAgentTask | None, manager: Any) -> None:
    source = previous if previous is not None else task
    old_attempts = max(0, int(previous.runner_attempts or 0)) if previous is not None else 0
    current_attempts = max(0, int(task.runner_attempts or 0))
    counters = recovery_counters(source)
    previous_extra = getattr(previous, "_extra_fields", {}) if previous is not None else {}
    extra = dict(getattr(task, "_extra_fields", {}))
    safe_resume = previous_extra.get("recovery_safe_resume") is True
    if previous is not None:
        failure = str(previous.failure_type or "")
        if task.runner_active_attempt_id and task.runner_active_attempt_id != previous.runner_active_attempt_id:
            extra["recovery_progress_baseline"] = _recovery_progress_fingerprint(task)
            extra["recovery_progress_baseline_attempt_id"] = task.runner_active_attempt_id
        starting = (bool(task.runner_active_attempt_id) and not previous.runner_active_attempt_id
                    and task_status_in(previous.status, SUBAGENT_RETRYABLE_FAILURE_STATUSES))
        if starting and failure in RETRYABLE_RUNNER_FAILURE_TYPES:
            key = "provider_retry_count" if failure in PROVIDER_SUPPLY_FAILURE_TYPES else "failure_retry_count"
            counters[key] += 1
        settled = current_attempts - old_attempts
        if settled == 1:
            fingerprint = _recovery_progress_fingerprint(task)
            baseline = previous_extra.get("recovery_progress_baseline")
            # 引用集合变化只能标未知；同一集合内产物消失也不是正向内容推进。
            observed = (isinstance(fingerprint, dict) and isinstance(baseline, dict)
                        and fingerprint.keys() == baseline.keys()
                        and all(value is None or (isinstance(value, str) and len(value) == 64
                                and all(char in "0123456789abcdef" for char in value)) for value in baseline.values())
                        and previous_extra.get("recovery_progress_baseline_attempt_id") == previous.runner_active_attempt_id)
            advanced = observed and any(value is not None and value != baseline[path]
                                        for path, value in fingerprint.items())
            if advanced:
                counters["no_progress_count"] = 0
            elif (observed and task.failure_type in RETRYABLE_RUNNER_FAILURE_TYPES
                  and task.failure_type not in PROVIDER_SUPPLY_FAILURE_TYPES
                  and task_status_in(task.status, SUBAGENT_RETRYABLE_FAILURE_STATUSES)):
                counters["no_progress_count"] += 1
            if not observed:
                counters["no_progress_unknown_count"] += 1
            safe_resume = False
            if task.failure_type == FailureType.HOST_SHUTDOWN_INTERRUPTED.value and previous.runner_active_attempt_id:
                from ....runtime_db.executor_liveness import exited_attempt_facts

                facts = exited_attempt_facts(getattr(manager, "runtime_db", None), task.id, previous.runner_active_attempt_id)
                safe_resume = bool(facts and facts.get("host_shutdown") is True and facts.get("uncertain_effects") is False)
        elif settled > 1:
            counters["recovery_history_unknown_count"] += settled
            safe_resume = False
        if starting:
            safe_resume = False
    extra.update(counters)
    extra["recovery_safe_resume"] = safe_resume
    task._extra_fields = extra


def _canonical_state_ref_from_payload(payload: dict[str, Any]) -> str:
    # 3.txt B.6：canonical_ref 由框架重算（agent_run_workspace_dir + 域内检查，
    # 锚 = 同 payload 的 task_workspace_dir），不信任 payload 自报的
    # canonical_state_ref 字符串。
    canonical = canonical_state_path_from_payload(payload)
    return str(canonical) if canonical else ""


def _merge_runner_session_payload(
    payload: dict[str, Any],
    session: dict[str, object],
    *,
    now: float,
) -> None:
    attrs_value = payload.get("attributes")
    attrs = dict(attrs_value) if isinstance(attrs_value, dict) else {}
    history_value = attrs.get("runner_session_history")
    history = list(history_value) if isinstance(history_value, list) else []
    previous = attrs.get("runner_session")
    if (
        isinstance(previous, dict)
        and previous.get("session_id") != session.get("session_id")
        and not any(
            isinstance(item, dict) and item.get("session_id") == previous.get("session_id")
            for item in history
        )
    ):
        history.append(previous)
    attrs["runner_session"] = dict(session)
    attrs["runner_session_history"] = history[-10:]
    payload["attributes"] = attrs
    payload["heartbeat_at"] = now
    payload["updated_at"] = now


# LLM: 精确 attempt 和 canonical 终态共同约束心跳；正常结束只允许原 session 收尾，旧轮不能覆盖新轮。
# 函数用途: 拒绝停止或换代后的旧会话更新，同时保留正常完成回执。
def _runner_session_write_is_stale(
    payload: dict[str, Any],
    session: dict[str, object],
) -> bool:
    expected = str(session.get("attempt_id") or "")
    current = str(payload.get("runner_active_attempt_id") or "")
    if expected and expected != current:
        previous = (payload.get("attributes") or {}).get("runner_session") or {}
        if (current or session.get("status") not in {"completed", "failed"}
                or previous.get("session_id") != session.get("session_id")):
            return True
    task_status = str(payload.get("status") or "").strip().upper()
    if task_status not in SUBAGENT_RECOVERY_CLOSED_STATUSES:
        return False
    session_status = str(session.get("status") or "").strip().lower()
    return not (
        task_status == TaskStatus.DONE.value
        and session_status == "completed"
    )


def _directory_id_for_task(service: SubAgentPersistenceService, task: SubAgentTask) -> str:
    """G1（3.txt B.3）：task.id（opaque）→ 框架目录 ID。

    有权威库（manager.runtime_db）→ 走 id_path_mapping 映射（查表→无则
    登记，首次登记即固定、重复幂等）；无库（纯投影环境）→ 退化本地校验，
    结果与 DB 登记值一致（directory_id 即 validate 后的安全路径段）。
    """
    db = getattr(getattr(service, "manager", None), "runtime_db", None)
    if db is not None:
        return db.directory_id_for(task.id, kind="run_id")
    return directory_id_for_opaque(task.id, kind="run_id")


# LLM: 详细状态只写原canonical；未来字段仅补充已知schema之外的键，不允许覆盖权限或身份字段。
# 函数用途: 沿原事务准备并保存完整状态、未知字段及派生定位记录。
def _prepare_and_write_state(
    service: SubAgentPersistenceService,
    task: SubAgentTask,
) -> tuple[Path, dict[str, Any]]:
    sync_task_workspace_fields(service.workspace, task)
    task_dir = Path(task.task_dir)
    task_dir.mkdir(parents=True, exist_ok=True)
    service.manager._ensure_work_order_files(task)
    task.updated_at = task.updated_at or time.time()
    if task.checkpoint_json:
        task.checkpoint_ref = task.checkpoint_json
    _refresh_system_tree_snapshot(task)
    task.latest_status_report = build_status_report(task)
    refresh_failure_handoff(task)
    output_report = (
        read_json_object_report(
            Path(task.output_json),
            parse_nested_string=True,
            context="subagent.persistence.output_json",
        )
        if task.output_json
        else None
    )
    output_payload = output_report.payload if output_report else {}
    checkpoint_artifacts = build_checkpoint_artifact_payloads(task, output_payload)
    if output_report and output_report.load_error:
        append_checkpoint_load_error(checkpoint_artifacts, output_report.load_error)
    if output_report and output_report.load_error:
        append_agent_run_checkpoint_load_error(task, output_report.load_error)
    _set_canonical_state_ref(task)
    write_inheritance_manifest(task)
    write_failure_handoff(task)
    write_checkpoint_output_files(task, checkpoint_artifacts)
    state = build_agent_run_state(task)
    # 未识别的未来字段只允许补空位，绝不能覆盖当前模型身份、权限及生命周期字段。
    extra = getattr(task, "_extra_fields", {})
    if extra:
        state.payload.update(copy.deepcopy({key: value for key, value in extra.items() if key not in state.payload}))
    write_agent_run_state(state)
    locator_payload = build_agent_state_locator(task, state)
    locator_dir = service.workspace / _directory_id_for_task(service, task)
    # locator 目录缺失时由首次私有写按 0700 建；已存在的目录一律不动。
    write_private_json_file_atomic(locator_dir / "task.json", locator_payload)
    write_private_json_file_atomic(locator_dir / "run.json", locator_payload)
    write_private_json_file_atomic(task_dir / "task.json", locator_payload)
    write_private_json_file_atomic(task_dir / "run.json", locator_payload)
    return task_dir, build_owner_agent_projection(task, state)


def _set_canonical_state_ref(task: SubAgentTask) -> None:
    task.attributes = dict(getattr(task, "attributes", {}) or {})
    if task.agent_run_workspace_dir:
        task.attributes["canonical_state_ref"] = str(
            Path(task.agent_run_workspace_dir) / "canonical_state.json"
        )


def _read_state_payload(path: Path, workspace: Path | None = None) -> dict[str, Any]:
    # workspace 提供时 canonical ref 跳转强制域内（本 service 读写路径）；缺省
    # 宽松模式留给只读投影调用方。
    return read_agent_state_payload(path, workspace=workspace)


def build_status_report(task: SubAgentTask) -> StatusReport:
    previous = task.latest_status_report if isinstance(task.latest_status_report, StatusReport) else StatusReport()
    progress = max(0.0, min(1.0, _safe_float(task.progress)))
    return StatusReport(
        run_id=task.id,
        version=max(0, int(previous.version or 0)) + 1,
        state=task.status,
        progress=progress,
        current_step=task.current_step or task.status,
        summary_delta=_summary_delta(task),
        budget_used=dict(task.budget_used or {}),
        artifact_refs=list(dict.fromkeys(task.artifact_refs)),
        evidence_refs=list(dict.fromkeys(task.evidence_refs)),
        blockers=list(dict.fromkeys(task.blockers)),
        checkpoint_ref=task.checkpoint_ref,
        next_recommended_action=(task.blockers[0] if task.blockers else ""),
        updated_at=task.updated_at or task.heartbeat_at or task.created_at,
    )


def normalize_runtime_identity(value: object) -> RuntimeIdentity:
    if isinstance(value, RuntimeIdentity):
        return value
    if not isinstance(value, dict):
        return RuntimeIdentity()
    payload = {key: value[key] for key in _field_names(RuntimeIdentity) if key in value}
    return RuntimeIdentity(**payload)


def normalize_security_signal(value: object) -> SecuritySignal:
    if isinstance(value, SecuritySignal):
        return value
    if not isinstance(value, dict):
        return SecuritySignal()
    payload = {key: value[key] for key in _field_names(SecuritySignal) if key in value}
    for key in ("evidence_refs", "artifact_refs"):
        payload[key] = sequence_strings(payload.get(key))
    payload["created_at"] = _safe_float(payload.get("created_at"))
    return SecuritySignal(**payload)


def normalize_inheritance_manifest(value: object) -> InheritanceManifest:
    if isinstance(value, InheritanceManifest):
        return value
    if not isinstance(value, dict):
        return InheritanceManifest()
    payload = {key: value[key] for key in _field_names(InheritanceManifest) if key in value}
    for key in ("inherited", "overridden", "dropped", "policy"):
        payload[key] = _dict_value(payload.get(key))
    payload["created_at"] = _safe_float(payload.get("created_at"))
    return InheritanceManifest(**payload)


def write_inheritance_manifest(task: SubAgentTask) -> None:
    if not task.inheritance_manifest_json:
        return
    Path(task.inheritance_manifest_json).write_text(
        json.dumps(asdict(task.inheritance_manifest), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def normalize_failure_handoff(value: object) -> FailureHandoff:
    if isinstance(value, FailureHandoff):
        return value
    if not isinstance(value, dict):
        return FailureHandoff()
    payload = {key: value[key] for key in _field_names(FailureHandoff) if key in value}
    for key in ("artifact_refs", "evidence_refs", "avoid_next_time"):
        payload[key] = sequence_strings(payload.get(key), allow_scalar=True)
    payload["created_at"] = _safe_float(payload.get("created_at"))
    return FailureHandoff(**payload)


def refresh_failure_handoff(task: SubAgentTask) -> FailureHandoff:
    if not should_write_failure_handoff(task):
        task.failure_handoff = FailureHandoff()
        return task.failure_handoff
    task.failure_handoff = FailureHandoff(
        run_id=task.id,
        status=task.status,
        failure_type=task.failure_type or failure_type_from_task_status(task.status),
        risk_level=_failure_handoff_risk_level(task),
        warning=task.latest_summary or _default_failure_warning(task),
        last_safe_checkpoint_ref=task.checkpoint_json or task.checkpoint_ref,
        artifact_refs=list(dict.fromkeys(task.artifact_refs)),
        evidence_refs=list(dict.fromkeys(task.evidence_refs)),
        avoid_next_time=_avoid_next_time(task),
        recommended_next_action=_recommended_next_action(task),
        auto_rescue=False,
        created_at=task.updated_at or task.heartbeat_at or task.created_at or time.time(),
    )
    return task.failure_handoff


def should_write_failure_handoff(task: SubAgentTask) -> bool:
    return task_has_failure_status(task) or bool(task.failure_type)


def _failure_handoff_risk_level(task: SubAgentTask) -> str:
    if task.failure_type in _HIGH_RISK_FAILURE_TYPES:
        return "high"
    if task_status_in(task.status, SUBAGENT_FAILED_RESULT_STATUSES):
        return "high"
    if task_has_status(task, TaskStatus.BLOCKED):
        return "medium"
    return "low"


def _default_failure_warning(task: SubAgentTask) -> str:
    if task.failure_type in _HIGH_RISK_FAILURE_TYPES:
        return "黑盒或工具输出存在撑爆上下文风险，已停止继续展开。"
    return "子代理未能正常完成，后续接管前请先读取 checkpoint 和 evidence refs。"


def _avoid_next_time(task: SubAgentTask) -> list[str]:
    avoid = ["不要机械重试同一工具调用；先读取 checkpoint、artifact manifest 和失败交接记录。"]
    if task.failure_type in _HIGH_RISK_FAILURE_TYPES:
        avoid.append("不要把黑盒大输出直接塞回 prompt；先外置文件，再读取摘要或切片。")
    avoid.extend(f"先处理 blocker: {item}" for item in task.blockers)
    return list(dict.fromkeys(avoid))


def _recommended_next_action(task: SubAgentTask) -> str:
    if task.failure_type in _HIGH_RISK_FAILURE_TYPES:
        return "先外置黑盒输出，再让接管代理读取摘要和 checkpoint。"
    if task.blockers:
        return f"先解决阻塞项：{task.blockers[0]}"
    return "读取 checkpoint、failure_handoff 和 evidence refs 后再决定是否接管。"


def write_failure_handoff(task: SubAgentTask) -> None:
    if not task.failure_handoff_json:
        return
    path = Path(task.failure_handoff_json)
    if not task.failure_handoff.run_id:
        path.unlink(missing_ok=True)
        return
    path.write_text(
        json.dumps(asdict(task.failure_handoff), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def append_checkpoint_load_error(
    checkpoint_artifacts: dict[str, object],
    error: dict[str, object],
) -> None:
    checkpoint = checkpoint_artifacts.get("checkpoint_json")
    if isinstance(checkpoint, dict):
        _append_load_error(checkpoint, error)


def append_agent_run_checkpoint_load_error(task: SubAgentTask, error: dict[str, object]) -> None:
    path_text = str(getattr(task, "agent_run_checkpoint_json", "") or "").strip()
    if not path_text:
        return
    path = Path(path_text)
    report = read_json_object_report(
        path,
        context="subagent.persistence.agent_run_checkpoint",
    )
    checkpoint = dict(report.payload)
    if report.load_error:
        _append_load_error(checkpoint, report.load_error)
    _append_load_error(checkpoint, error)
    path.write_text(json.dumps(checkpoint, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_checkpoint_output_files(task: SubAgentTask, checkpoint_artifacts: dict[str, object]) -> None:
    for field_name, artifact_payload in checkpoint_artifacts.items():
        _write_checkpoint_artifact(getattr(task, field_name, ""), artifact_payload)
    write_takeover_readiness_files(task)


def _summary_delta(task: SubAgentTask) -> dict[str, list[str]]:
    return {
        "facts_added": [task.latest_summary] if task.latest_summary else [],
        "facts_invalidated": [],
        "decisions_changed": [],
        "open_questions": list(task.blockers),
    }


def _dict_value(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _append_load_error(checkpoint: dict[str, Any], error: dict[str, object]) -> None:
    load_errors = checkpoint.get("load_errors")
    items = list(load_errors) if isinstance(load_errors, list) else []
    items.append(error)
    checkpoint["load_errors"] = items


def _write_checkpoint_artifact(path_text: str, artifact_payload: object) -> None:
    if not path_text:
        return
    path = Path(path_text)
    if isinstance(artifact_payload, str):
        path.write_text(artifact_payload, encoding="utf-8")
        return
    path.write_text(json.dumps(artifact_payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _merge_existing_child_links(service: SubAgentPersistenceService, task: SubAgentTask) -> None:
    try:
        existing = service.load(task.id)
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        return
    task.child_ids = _unique_strings([*existing.child_ids, *task.child_ids])


# LLM: Capability records form an independent append-only domain and are merged on every full save.
# runner_attempts is the monotonic generation fence: an older writer may add a capability delta,
# but cannot replace lifecycle/result/attempt facts settled by a newer runner generation.
#   活动诊断只允许专属窄 mutation 更新，full save 始终保留 canonical 版本。
#   显式文件模式还按预留/消费身份保留生命周期，防止结果计数尚未增长时旧快照清掉新启动。
# 函数用途: 保留并发授权、活动提醒与文件启动事实，阻止旧副本回滚执行状态。
# LLM: CAS合并沿用canonical版本优先规则，未知扩展与runner快照同步保真，不让旧写入洗掉新字段。
# 函数用途: 合并并发授权、执行状态及未知任务字段。
def _merge_concurrent_capability_and_runner_state(
    service: SubAgentPersistenceService,
    task: SubAgentTask,
) -> bool:
    try:
        existing = service.load(task.id)
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        return False
    incoming_requests = copy.deepcopy(task.capability_requests)
    incoming_grants = copy.deepcopy(task.capability_grants)
    incoming_gaps = copy.deepcopy(task.capability_gaps)
    incoming_allowed_skills = list(task.allowed_skills)
    incoming_allowed_tools = list(task.allowed_tools)
    incoming_required_paths = list(task.context_manifest.required_read_paths)
    incoming_hint_paths = list(task.context_manifest.hint_read_paths)
    incoming_updated_at = float(task.updated_at or 0.0)
    incoming_extra = getattr(task, "_extra_fields", {})
    existing_extra = getattr(existing, "_extra_fields", {})
    task._extra_fields = (
        {**copy.deepcopy(existing_extra), **copy.deepcopy(incoming_extra)}
        if int(task.state_revision or 0) >= int(existing.state_revision or 0)
        else copy.deepcopy(existing_extra)
    )

    existing_runner_is_newer = int(existing.runner_attempts or 0) > int(
        task.runner_attempts or 0
    )
    incoming_is_control_terminal = (
        str(task.status or "").strip().upper() in SUBAGENT_RECOVERY_CLOSED_STATUSES
    )
    stale_file_start = False
    if service.manager.runtime_db is None:
        from ...file_runner_start import file_runner_start_is_newer, preserve_background_start

        stale_file_start = file_runner_start_is_newer(task, existing)
    replaced = stale_file_start or (existing_runner_is_newer and not incoming_is_control_terminal)
    if replaced:
        for item in fields(task):
            setattr(task, item.name, copy.deepcopy(getattr(existing, item.name)))
        task._extra_fields = copy.deepcopy(existing_extra)
    if service.manager.runtime_db is None:
        preserve_background_start(task, existing)
        task.runner_abandoned_attempt_ids = _unique_strings([
            *existing.runner_abandoned_attempt_ids, *task.runner_abandoned_attempt_ids,
        ])
    # 活动诊断由心跳的窄 mutation 独占写入。旧工具/进度快照保存时不能覆盖或删除它，
    # 否则正常流式进展会抹掉提醒回执，造成重复通知；旧 attempt 的展示仍按 exact ID 隔离。
    diagnostic = (existing.attributes or {}).get("runtime_activity_diagnostic")
    if isinstance(diagnostic, dict):
        task.attributes = {**dict(task.attributes or {}), "runtime_activity_diagnostic": copy.deepcopy(diagnostic)}
    # capability 是独立的 append-only 域。canonical 永远是冲突裁决基线，旧 writer
    # 只能补新 request/grant/gap 或把 OPEN 推进到终态，不能翻转已落盘的终态。
    task.capability_requests = _merge_capability_requests(
        existing.capability_requests,
        incoming_requests,
    )
    task.capability_grants = _merge_capability_grants(
        existing.capability_grants,
        incoming_grants,
    )
    task.capability_gaps = _merge_records_by_id(
        existing.capability_gaps,
        incoming_gaps,
    )
    task.allowed_skills = _unique_strings(
        [*existing.allowed_skills, *incoming_allowed_skills]
    )
    task.allowed_tools = _unique_strings(
        [*existing.allowed_tools, *incoming_allowed_tools]
    )
    task.context_manifest.required_read_paths = _unique_strings(
        [
            *existing.context_manifest.required_read_paths,
            *incoming_required_paths,
        ]
    )
    task.context_manifest.hint_read_paths = _unique_strings(
        [*existing.context_manifest.hint_read_paths, *incoming_hint_paths]
    )
    task.updated_at = max(
        float(task.updated_at or 0.0),
        incoming_updated_at,
        float(existing.updated_at or 0.0),
    )
    _advance_granted_capability_resume_state(task)
    return replaced


# LLM: A settled attempt with fully resolved capability requests is dispatch-ready, not blocked.
# This reducer uses only typed status, attempt, request, gap, and grant facts; legacy BLOCKED plus
# grant remains readable but every new canonical write advances it to the single PENDING state.
# 函数用途: 授权已齐且旧执行轮已安全收口时，把子代理原子推进到待续跑状态。
def _advance_granted_capability_resume_state(task: SubAgentTask) -> bool:
    if str(task.status or "").strip().upper() != TaskStatus.BLOCKED.value:
        return False
    if str(task.failure_type or "").strip() not in CAPABILITY_GRANTED_BLOCKER_FAILURE_TYPES:
        return False
    if str(task.runner_active_attempt_id or "").strip():
        return False
    if int(task.runner_attempts or 0) <= 0 or not task.capability_grants:
        return False
    if any(
        capability_request_requires_parent_resolution(item.status)
        for item in task.capability_requests
    ):
        return False
    if any(str(item.status or "").strip() == "OPEN" for item in task.capability_gaps):
        return False
    task.status = TaskStatus.PENDING.value
    task.failure_type = ""
    task.ended_at = 0.0
    task.progress = min(float(task.progress or 0.0), 0.99)
    return True


# LLM: Capability records are append-only identities. Existing canonical order remains stable and
# an incoming record replaces only the same id so a concurrent grant/gap is retained exactly once.
# 函数用途: 按结构化 id 合并能力授权和缺口记录，避免并发写丢账或重复。
def _merge_records_by_id(existing: list[Any], incoming: list[Any]) -> list[Any]:
    merged = [copy.deepcopy(item) for item in existing]
    positions = {
        str(getattr(item, "id", "") or "").strip(): index
        for index, item in enumerate(merged)
        if str(getattr(item, "id", "") or "").strip()
    }
    for item in incoming:
        record_id = str(getattr(item, "id", "") or "").strip()
        if record_id and record_id in positions:
            merged[positions[record_id]] = copy.deepcopy(item)
            continue
        if record_id:
            positions[record_id] = len(merged)
        merged.append(copy.deepcopy(item))
    return merged


# LLM: One capability request may have only one durable grant identity. Canonical records win;
# a stale or retried writer carrying a different generated grant id for the same request is
# ignored, while grants for distinct requests remain append-only.
# 函数用途: 按 request_id 幂等合并授权，避免并发或重试给同一申请生成两份权限账。
def _merge_capability_grants(
    existing: list[CapabilityGrant],
    incoming: list[CapabilityGrant],
) -> list[CapabilityGrant]:
    merged = [copy.deepcopy(item) for item in existing]
    seen = {
        str(item.request_id or "").strip() or str(item.id or "").strip()
        for item in merged
    }
    for item in incoming:
        identity = str(item.request_id or "").strip() or str(item.id or "").strip()
        if identity and identity in seen:
            continue
        if identity:
            seen.add(identity)
        merged.append(copy.deepcopy(item))
    return merged


# LLM: Request lifecycle is monotonic within the capability domain. Canonical terminal decisions
# win conflicts; an incoming writer may only advance canonical OPEN to one current terminal state.
# 函数用途: 合并同一能力申请的状态，确保已授权/缺口/关闭不会被旧 OPEN 状态覆盖。
def _merge_capability_requests(
    existing: list[CapabilityRequest],
    incoming: list[CapabilityRequest],
) -> list[CapabilityRequest]:
    merged = [copy.deepcopy(item) for item in existing]
    positions = {
        str(item.id or "").strip(): index
        for index, item in enumerate(merged)
        if str(item.id or "").strip()
    }
    terminal = {"GRANTED", "GAP", "CLOSED"}
    for item in incoming:
        request_id = str(item.id or "").strip()
        if not request_id or request_id not in positions:
            if request_id:
                positions[request_id] = len(merged)
            merged.append(copy.deepcopy(item))
            continue
        current = merged[positions[request_id]]
        current_status = str(current.status or "").strip()
        incoming_status = str(item.status or "").strip()
        if current_status in terminal:
            continue
        if incoming_status in terminal:
            merged[positions[request_id]] = copy.deepcopy(item)
    return merged


# LLM: Canonical recovery-closed status is monotonic at the final shared persistence boundary.
# A caller may explicitly reactivate only an exact structured same-run continuation or lifecycle
# repair; ordinary stale progress/heartbeat/result snapshots must be replaced by canonical state.
# 已关闭记录整份拷回时，只有 _closed_record_appends 白名单里的接替关系（新的 superseded_by 及随它按 id 新增的
# TakeoverRecord）可以单调追加并继续落盘；其余字段一律以磁盘上的已关闭状态为准。
# 函数用途: 防止已完成、已取消、已放弃或已接管的子代理被旧线程写回运行中，同时让调用者立刻看到真实终态；返回 True 表示本次不写盘。
# LLM: 较新的终态恢复时同时复制未知扩展，不允许旧执行器覆盖已关闭的canonical事实。
# 函数用途: 恢复较新关闭快照并保留扩展字段。
def _restore_newer_closed_state(
    service: SubAgentPersistenceService,
    task: SubAgentTask,
    *,
    allow_terminal_reactivation: bool,
) -> bool:
    if allow_terminal_reactivation:
        return False
    try:
        existing = service.load(task.id)
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        return False
    existing_status = str(existing.status or "").strip().upper()
    incoming_status = str(task.status or "").strip().upper()
    if (
        existing_status not in SUBAGENT_RECOVERY_CLOSED_STATUSES
        or incoming_status == existing_status
    ):
        return False
    appends = _closed_record_appends(existing, task)
    for item in fields(task):
        setattr(task, item.name, copy.deepcopy(getattr(existing, item.name)))
    task._extra_fields = copy.deepcopy(getattr(existing, "_extra_fields", {}))
    for name, value in appends.items():
        setattr(task, name, value)
    return not appends


# LLM: 已关闭记录上允许单调追加的白名单：superseded_by 只能从空写成非空，TakeoverRecord 只随这条新关系按 id 追加，
#   不能单独追加（输掉竞态的 TAKEN_OVER 写入不留悬空记录）；不改终态和其它字段。纯计算，返回需要叠加到拷回状态上的
#   字段。改白名单须同步 test_subagent_done_supersede。
# 函数用途: 从一次被拒写回的快照里挑出可以保留的新接替关系。
def _closed_record_appends(existing: SubAgentTask, incoming: SubAgentTask) -> dict[str, object]:
    if not incoming.superseded_by or existing.superseded_by:
        return {}
    return {
        "superseded_by": incoming.superseded_by,
        "takeover_records": _union_takeover_records(existing.takeover_records, incoming.takeover_records),
    }


# LLM: TakeoverRecord 按 id 去重合并，first 的顺序在前、second 里的新记录追加在后；返回深拷贝，不改入参。纯计算。
# 函数用途: 合并两份接管记录列表，不丢记录也不重复。
def _union_takeover_records(first: list[TakeoverRecord], second: list[TakeoverRecord]) -> list[TakeoverRecord]:
    known = {record.id for record in first}
    fresh = [record for record in second if record.id not in known]
    return copy.deepcopy([*first, *fresh])


# 函数用途: 读上一份落盘状态的 locked_files(锁变更账本的对比基准;首存返回 None)。
def _existing_locked_files(service: SubAgentPersistenceService, task: SubAgentTask) -> list[str] | None:
    try:
        existing = service.load(task.id)
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        return None
    return list(existing.locked_files or [])


# LLM: 只在保留层级链接的保存路径上调用。同状态的旧快照（例如接替前读到的 DONE）不能冲掉已落盘的接替关系：
#   superseded_by 与 TakeoverRecord 只增不减；已 TAKEN_OVER 的记录不被未接管快照写回。只改内存中的 task。
# 函数用途: 保存前把磁盘上已有的接管/接替关系合并回本次要写的快照。
def _merge_existing_takeover_state(service: SubAgentPersistenceService, task: SubAgentTask) -> None:
    try:
        existing = service.load(task.id)
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        return
    if existing.superseded_by and not task.superseded_by:
        task.superseded_by = existing.superseded_by
    task.takeover_records = _union_takeover_records(existing.takeover_records, task.takeover_records)
    existing_taken = str(existing.status or "").upper() == "TAKEN_OVER" or bool(existing.takeover_by)
    incoming_taken = str(task.status or "").upper() == "TAKEN_OVER" or bool(task.takeover_by)
    if not existing_taken or incoming_taken:
        return
    task.status = existing.status
    task.takeover_by = existing.takeover_by
    task.takeover_reason = existing.takeover_reason
    task.takeover_records = list(existing.takeover_records)
    task.final_owner = existing.final_owner
    task.locked_files = _unique_strings([*existing.locked_files, *task.locked_files])


# LLM: canonical task persistence is the last shared write boundary for every
# runner, cancellation path and narrow lifecycle repair.  A named Audit watch
# already marked closed is a durable control fact; no stale runner snapshot may
# persist RUNNING/PENDING/BLOCKED over it.
# 函数用途：统一保存子代理状态前读取来源 watch 的结构化生命周期；已关闭就单调
# 投影为 CANCELLED，并废弃仍挂在旧快照上的 attempt。
def _project_closed_audit_source_state(task: SubAgentTask) -> None:
    attrs = getattr(task, "attributes", {}) or {}
    from ....common.audit_activation import (
        structured_audit_source_worker_attributes,
    )

    if not structured_audit_source_worker_attributes(attrs):
        return
    try:
        from ....ingestion.source_worker import source_worker_lifecycle_state

        closed = source_worker_lifecycle_state(task) == "closed"
    except Exception:
        closed = False
    if not closed:
        return
    attempt_id = str(getattr(task, "runner_active_attempt_id", "") or "").strip()
    if attempt_id and attempt_id not in task.runner_abandoned_attempt_ids:
        task.runner_abandoned_attempt_ids.append(attempt_id)
    task.runner_active_attempt_id = ""
    task.status = TaskStatus.CANCELLED.value
    task.verification_status = VerificationStatus.UNVERIFIED.value
    task.failure_type = FailureType.CANCELLED.value
    task.blockers = []
    task.ended_at = task.ended_at or time.time()


def _refresh_system_tree_snapshot(task: SubAgentTask) -> None:
    attrs = dict(getattr(task, "attributes", {}) or {})
    attrs["system_tree"] = {
        "schema_version": "subagent_system_tree.v1",
        "updated_by": "system",
        "source": "persistence.save",
        "run_id": task.id,
        "owner_id": task.owner,
        "root_id": task.root_id or task.id,
        "parent_id": task.parent_id,
        "depth": _safe_int(task.depth),
        "status": task.status,
        "verification_status": task.verification_status,
        "failure_type": task.failure_type,
        "progress": _safe_float(task.progress),
        "current_step": task.current_step,
        "latest_summary": task.latest_summary,
        "child_ids": _unique_strings(list(task.child_ids)),
        "artifact_refs": _unique_strings(list(task.artifact_refs)),
        "artifact_registry_refs": _registry_records(attrs.get("artifact_registry_refs")),
        "evidence_refs": _unique_strings(list(task.evidence_refs)),
        "blockers": _unique_strings(list(task.blockers)),
        "updated_at": _safe_float(task.updated_at or task.heartbeat_at or task.created_at),
    }
    task.attributes = attrs


def _safe_int(value: object, default: int = 0) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return default


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return default


def _unique_strings(values: list[str]) -> list[str]:
    return list(dict.fromkeys(item for item in values if item))


def _registry_records(value: object, *, limit: int = 12) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        row = dict(item)
        artifact_id = str(row.get("artifact_id") or "").strip()
        path = str(row.get("path") or "").strip()
        key = artifact_id or path
        if not key or key in seen:
            continue
        seen.add(key)
        rows.append(row)
        if len(rows) >= limit:
            break
    return rows


# LLM: 精确workspace由原runtime路径解析给出；原opaque ID/canonical reader/normalizer共用，不新建路径、Store或manager。
# 函数用途: 给原manager和已认证冷C只读一个已存在子任务。
def load_subagent_task(workspace: Path, run_id: str) -> SubAgentTask:
    run_id = validate_opaque_id(run_id, kind="run_id")
    path = Path(workspace) / run_id / "task.json"
    if not path.exists():
        raise FileNotFoundError(f"子代理记录不存在: {run_id}")
    return task_from_payload(_read_state_payload(path))

# LLM: 原始counter组在首次load即校验；缺失总次数不能被dataclass默认0遮住；纯读不写盘，旧记录迁移留结构化依据。
# 函数用途: 恢复正式SubAgentTask并保留未来代际数据，不让旧schema读写抹字段。
def task_from_payload(data: dict[str, Any]) -> SubAgentTask:
    """Build a normalized task from a canonical state payload."""

    missing_attempts = "runner_attempts" not in data and any(name in data for name in RECOVERY_COUNTER_NAMES)
    legacy_counters = not any(name in data for name in (*RECOVERY_COUNTER_NAMES, "no_progress_unknown_count"))
    extra = {key: value for key, value in data.items() if key not in _field_names(SubAgentTask)}
    extra = copy.deepcopy(extra) if extra else {}
    data = {key: value for key, value in data.items() if key in _field_names(SubAgentTask)}
    data["capability_requests"] = [
        _normalize_nested_model(CapabilityRequest, item)
        for item in data.get("capability_requests", [])
        if isinstance(item, dict)
    ]
    data["capability_grants"] = [
        _normalize_nested_model(CapabilityGrant, item)
        for item in data.get("capability_grants", [])
        if isinstance(item, dict)
    ]
    data["capability_gaps"] = [
        _normalize_nested_model(CapabilityGap, item)
        for item in data.get("capability_gaps", [])
        if isinstance(item, dict)
    ]
    data["evidence"] = [VerificationEvidence(**item) for item in data.get("evidence", []) if isinstance(item, dict)]
    data["evidence_packets"] = [
        _normalize_evidence_packet(item) for item in data.get("evidence_packets", []) if isinstance(item, dict)
    ]
    data["findings"] = [
        _normalize_finding(item) for item in data.get("findings", []) if isinstance(item, dict)
    ]
    data["takeover_records"] = [
        TakeoverRecord(**item) for item in data.get("takeover_records", []) if isinstance(item, dict)
    ]
    data["channel_checks"] = [
        ChannelProbeCheck(**item) for item in data.get("channel_checks", []) if isinstance(item, dict)
    ]
    data["quality_contract"] = _normalize_quality_contract(data.get("quality_contract"))
    data["context_manifest"] = _normalize_context_manifest(data.get("context_manifest"))
    data["context_packs"] = _normalize_context_packs(data.get("context_packs"))
    data["latest_status_report"] = _normalize_status_report(data.get("latest_status_report"))
    data["inheritance_manifest"] = normalize_inheritance_manifest(data.get("inheritance_manifest"))
    data["failure_handoff"] = normalize_failure_handoff(data.get("failure_handoff"))
    data["security_signals"] = [
        normalize_security_signal(item) for item in data.get("security_signals", []) if isinstance(item, dict)
    ]
    data["runtime_identity"] = normalize_runtime_identity(data.get("runtime_identity"))
    task = SubAgentTask(**data)
    task._extra_fields = extra
    counters = recovery_counters(task)
    if missing_attempts:
        counters["recovery_history_unknown_count"] = max(1, counters["recovery_history_unknown_count"])
    if legacy_counters and not counters["recovery_history_unknown_count"] and task.runner_attempts:
        extra["recovery_counter_migration"] = {"source": "legacy_total_attempts", "runner_attempts": task.runner_attempts}
    extra.update(counters)
    return task
