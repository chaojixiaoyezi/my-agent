# LLM: 本模块只读类型化策略、会话、任务及子代理状态和文件事实，不执行删除；来源或状态未知时必须保守保护并报告错误。
# 模块用途: 为每个 owner 生成可在执行前重验证的留存 dry-run 计划，统一保护引用与恢复材料。
from __future__ import annotations

"""Memory v2 retention 的只读扫描与计划生成。"""

import hashlib
import heapq
import io
import json
import os
import secrets
import sqlite3
import stat
import threading
import time
from collections import Counter, OrderedDict
from collections.abc import MutableSet
from contextlib import closing, contextmanager
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from itertools import chain
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from ..audit.paths import audit_segment_day, audit_segment_paths
from ..common import cache_capacity
from ..common.json_io import read_json_object_report, read_jsonl_objects_report
from ..common.log_redaction import redact_sensitive_text
from ..common.nofollow_fs import (
    open_directory_beneath,
    read_bytes_beneath,
)
from ..conversation.models import ConversationThread
from ..conversation.store_io import safe_file_stem
from ..conversation.thread_ledger_segments import archive_dir_for
from ..conversation.workspace_paths import canonical_task_root, durable_work_root
from ..local_storage.records import derived_terminal_expired
from ..settings.config import AgentConfig
from ..user_space.storage_lanes import STORAGE_LANE_SLICE_BYTES
from . import reference_pins, retention_scan_budget
from .candidate_models import MemoryCandidate
from .candidates import (
    _COLD_REF,
    CandidateService,
    CandidateStoreCorruptError,
    _hydrate_candidate,
    _is_binding_tombstone,
    _validate_cold_envelope,
    candidate_retention_eligible,
)
from .curator_models import MemoryCuratorState
from .curator_run_log import _CURSOR_CHECKPOINT, _CURSOR_DELTA
from .reference_pins import target_pin_read, target_pin_read_enabled, watch_pin_source
from .retention_models import (
    TASK_TERMINAL_STATUSES,
    MemoryRetentionAction,
    MemoryRetentionError,
    MemoryRetentionPolicy,
    MemoryRetentionReport,
    retention_action_id,
)
from .retention_scan_budget import (
    RETENTION_SCAN_SLICE_BYTES,
    RETENTION_SCAN_SLICE_ENTRIES,
    RetentionScanBudget,
)

# 一天按 86400 秒算：保留期换算成秒的统一口径。
_DAY_SECONDS = 86_400
# 过期会话回收前按后台整理游标往后探查可学习消息时，每页读取的会话行数。
_UNCURATED_PROBE_PAGE_ROWS = 16
# 同一会话最多探查的页数；读不完按“未确认读完”处理。
_UNCURATED_PROBE_MAX_PAGES_COUNT = 4
_SUBAGENT_SCRATCH_PATHS = (
    Path("inbox"),
    Path("outbox"),
    Path("compactions"),
    Path("artifacts") / "tool_outputs",
)
# 口径与 tool_output_days_after_terminal / trash_days 一致；用户嫌参数多，所以写代码常量、不新增配置项。
# 快照与工具清单的保留期：30 天（诊断和人工排查够用）。
_CONTEXT_BUNDLE_SNAPSHOT_DAYS = 30
# 日期目录里的 latest 副本永远不动：它们是"最近一份快照"的入口，删了会让排查没有起点。
_CONTEXT_BUNDLE_LATEST_PREFIX = "latest_context_bundle"
# 最后墓碑发布可能成功后才报fsync错误；此保护件在整个事务已确认后才移除。
_TRASH_COMMIT_PENDING = ".retention-pending"
_PIN_CURSOR_KEY = "pins:projection"
_PIN_BUILDS = OrderedDict()
_PIN_BUILDS_LOCK = threading.Lock()
_CURRENT_PIN_BUILDS = OrderedDict()
_EXECUTION_CURSOR_KEY = "execution:pending"
_EXECUTION_PLANS = OrderedDict()


# LLM: plan纯读取；预算覆盖引用投影与会话/快照发现，输入游标深复制，错误及策略指纹门保持。
# 函数用途: 规划临时数据与完整会话的安全回收，可显式分片并返回续点。
def build_retention_plan(
    *,
    home: object,
    candidates: CandidateService,
    conversation_roots: tuple[Path, ...] = (),
    now: datetime | None = None,
    scan_budget: RetentionScanBudget | None = None,
    scan_cursor: dict | None = None,
    config: object | None = None,
) -> MemoryRetentionReport:
    if scan_budget is None:
        scan_budget = RetentionScanBudget.default(config)
        # 普通dry-run也沿同一有界主链路；未提供游标时仅借用同owner活跃投影的私有续点。
        if scan_cursor is None:
            with _PIN_BUILDS_LOCK:
                resident = _PIN_BUILDS.get(Path(home.owner_home_dir))
                execution = _EXECUTION_PLANS.get(Path(home.owner_home_dir))
                scan_cursor = deepcopy(execution[1].scan_cursor if execution is not None else
                                       resident.context.scan_cursor if resident is not None else {})
    remaining = max(0.0, scan_budget.deadline - retention_scan_budget.time.monotonic()) if scan_budget is not None else None
    current = _normalize_now(now)
    policy_path = Path(home.owner_retention_json)
    policy, policy_error = _load_policy(policy_path)
    fingerprint = file_fingerprint(policy_path)
    if not valid_fingerprint(fingerprint) and policy_error is None:
        policy_error = _state_error("MEMORY_RETENTION_POLICY_UNREADABLE", policy_path)
    if policy_error is not None or policy is None:
        return MemoryRetentionReport(
            applied=False,
            actions=(),
            errors=((policy_error,) if policy_error is not None else ()),
            policy_fingerprint=fingerprint,
        )
    if policy.legal_hold:
        return MemoryRetentionReport(
            applied=False,
            actions=(),
            legal_hold=True,
            policy_fingerprint=fingerprint,
        )
    roots = tuple(dict.fromkeys((*(Path(root).expanduser().absolute() for root in conversation_roots),
                                 Path(home.owner_home_dir) / "conversations")))
    if any(root == Path(root.anchor) for root in roots):
        return MemoryRetentionReport(applied=False, actions=(), policy_fingerprint=fingerprint,
                                     errors=(_pin_source_error(ValueError("unsafe conversation root")),))
    context = _RetentionScanContext(
        home=home,
        candidates=candidates,
        policy=policy,
        conversation_roots=roots,
        now=current,
        scan_budget=scan_budget,
        scan_cursor=deepcopy(scan_cursor) if scan_budget is not None and isinstance(scan_cursor, dict) else {},
        config=config,
    )
    context.policy_fingerprint = fingerprint
    context.requested_roots = tuple(dict.fromkeys((*(Path(root).absolute() for root in conversation_roots),
                                                   Path(home.owner_home_dir) / "conversations")))
    # 策略、根发现和游标准备属于固定前奏；只给实际条目扫描保留调用入口剩余的时间，不重置条目/字节账。
    if scan_budget is not None:
        scan_budget.deadline = retention_scan_budget.time.monotonic() + remaining
    actions = _validated_actions(_collect_retention_actions(context), context.errors)
    unique = {action.action_id: action for action in actions}
    ordered = tuple(sorted(unique.values(), key=lambda item: (item.category, str(item.path))))
    return MemoryRetentionReport(
        applied=False,
        actions=ordered,
        errors=tuple(context.errors),
        policy_fingerprint=fingerprint,
        scan_partial=context.scan_partial,
        scan_cursor=context.scan_cursor,
        uncurated_threads=context.uncurated_threads,
    )


# LLM: 同次上下文共享策略、错误及可选预算；游标属于本次私有副本，scanner永不落盘。
# 类用途: 汇总一次只读 retention 计划所需的 owner、策略、会话根和扫描时间。
@dataclass
class _RetentionScanContext:
    home: object
    candidates: CandidateService
    policy: MemoryRetentionPolicy
    conversation_roots: tuple[Path, ...]
    now: datetime
    errors: list[MemoryRetentionError] = field(default_factory=list)
    scan_budget: RetentionScanBudget | None = None
    scan_cursor: dict = field(default_factory=dict)
    scan_partial: bool = False
    config: object = None
    discovery_context: object = None
    discovery_actions: list = field(default_factory=list)
    pin_projection: object = None
    requested_roots: tuple[Path, ...] | None = None
    policy_fingerprint: str = ""
    # 过期会话是否已被后台整理读完的判定材料（提取是否开启、逐会话游标、排除名单），首次用到时读一次，本轮共用。
    curator_facts: tuple | None = None
    # 本轮过期但整理游标之后仍有可学习消息（或读不完/读不出）的会话数，进报告 uncurated_threads。
    uncurated_threads: int = 0
    # 只读打开的会话库，按会话根缓存，供探查整理游标之后的消息。
    conversation_stores: dict = field(default_factory=dict)

    # LLM: 法律保留任务 ID 从已解析策略转成统一集合，供所有扫描器按精确身份复用。
    # 函数用途: 返回禁止清理的任务编号集合。
    @property
    def held_task_ids(self) -> frozenset[str]:
        return frozenset(self.policy.legal_hold_task_ids)


# LLM: 先完成可续引用构建/复核，再执行发现；预算暂停不生成删除动作，执行端仍按原严格来源复核。
# 函数用途: 按任务保护关系组合会话、候选、运行记录、回收站与过期快照/工具清单的清理动作。
def _collect_retention_actions(context: _RetentionScanContext) -> list[MemoryRetentionAction]:
    resumed = _resume_execution_actions(context)
    if resumed is not None:
        return resumed
    protection = None
    pin_errors: list[MemoryRetentionError] = []
    if context.scan_budget is not None:
        # 公开scan_budget仍计量原发现窗口；引用流沿同一配置和截止时间独立续进，不能重置发现计量。
        entries = getattr(context.config, "retention_scan_slice_entries", RETENTION_SCAN_SLICE_ENTRIES)
        size = getattr(context.config, "retention_scan_slice_bytes", RETENTION_SCAN_SLICE_BYTES)
        if type(entries) is not int or entries <= 0 or type(size) is not int or size <= 0:
            raise ValueError("invalid_retention_scan_budget")
        # 显式调用预算也是上界，不能被配置默认128条/8MiB扩大；发现与引用各自保存进度。
        entries = min(entries, context.scan_budget.max_entries)
        size = min(size, context.scan_budget.max_bytes)
        pin_context = replace(context, scan_budget=RetentionScanBudget(entries, size, context.scan_budget.deadline),
                              discovery_context=context, errors=[])
        protection, continuation = _budgeted_pin_projection(pin_context)
        pin_errors = pin_context.errors
        context.scan_partial |= pin_context.scan_partial
        context.pin_projection = pin_context.pin_projection
        if protection is None:
            context.errors.extend(pin_errors)
            return []
        if continuation:
            context.errors.extend(pin_errors)
            discovered = [action for action in context.discovery_actions if _reference_action_allowed(protection, action)]
            return discovered if _finish_pin_cycle(context) else []
    discovered = context.discovery_actions
    task_actions, selected_task_roots = _task_actions(
        home=context.home,
        policy=context.policy,
        held_task_ids=context.held_task_ids,
        now=context.now,
        errors=context.errors,
    )
    if protection is None:
        protection = current_retention_pins(context.home, context.conversation_roots, pin_errors, now=context.now)
    actions = list(task_actions)
    actions.extend(_task_tmp_actions(context, selected_task_roots, protection))
    actions.extend(_run_scratch_actions(context, selected_task_roots, protection))
    actions.extend(_protected_runtime_actions(context, selected_task_roots))
    # 保留原类别错误顺序：任务权威错误在前，统一来源错误只发表一次。
    context.errors.extend(pin_errors)
    actions.extend(_aged_file_actions(context, selected_task_roots, protection))
    actions.extend(_context_bundle_actions(context) if context.scan_budget is None else discovered)
    actions.extend(
        _trash_actions(
            home=context.home,
            days=context.policy.trash_days,
            now=context.now,
            errors=context.errors,
            conversation_roots=context.conversation_roots,
            protection=protection,
        )
    )
    actions = [action for action in actions if _reference_action_allowed(protection, action)]
    return actions if _finish_pin_cycle(context) else []



def _resume_execution_actions(context):
    """执行未开始的原动作先续办；下一片不能用重新规划抹掉其引用核验进度。"""
    owner = Path(context.home.owner_home_dir)
    cursor = context.scan_cursor.get(_EXECUTION_CURSOR_KEY)
    with _PIN_BUILDS_LOCK:
        pending = _EXECUTION_PLANS.get(owner)
        if pending is not None:
            token, plan, roots, chunk = pending
            if (cursor != token or plan.policy_fingerprint != context.policy_fingerprint
                    or roots != context.requested_roots or chunk != getattr(context.config,
                        "memory_reference_pin_chunk_bytes", reference_pins._PIN_SOURCE_CHUNK_BYTES)):
                _EXECUTION_PLANS.pop(owner)
                pending = None
            else:
                _EXECUTION_PLANS.move_to_end(owner)
    if pending is None:
        if cursor is not None:
            # 临时阶段被淘汰/进程重启只允许保守重扫，不能把丢掉的待办宣告完成。
            context.scan_cursor.clear()
        return None
    context.scan_cursor = deepcopy(plan.scan_cursor)
    context.scan_partial = plan.scan_partial
    context.errors.extend(plan.errors)
    return list(plan.actions)


def record_retention_removals(home, action, result):
    """仅登记真实成功且已消失的原路径，后续目录证明仍须精确核对成员差分。"""
    if result.status not in {"trashed", "deleted"}:
        return
    owner = Path(home.owner_home_dir)
    with _PIN_BUILDS_LOCK:
        snapshot = _CURRENT_PIN_BUILDS.get(owner)
    if snapshot is None or not snapshot.lock.acquire(blocking=False):
        return
    try:
        if snapshot.closed:
            return
        paths = action.related_paths if action.category == "conversation" else (action.path,)
        for path in paths:
            old = snapshot.sources.get(path)
            if old is None or old[0] is None or snapshot._stamp(path) is not None:
                continue
            snapshot.removed_roots.add(path)
            parent = path.parent
            if parent in snapshot.sources:
                snapshot.directory_removals.setdefault(parent, set()).add((path.name, old[0][1]))
    finally:
        snapshot.lock.release()


def retain_execution_actions(home, plan, results, *, roots=(), config=None):
    """只保留明确未执行及尚未到达的动作；权限、策略和真实目标仍由执行器逐次复核。"""
    handled = {action.action_id for action in results if action.status != "not_started"}
    pending = tuple(action for action in plan.actions if action.action_id not in handled)
    owner = Path(home.owner_home_dir)
    cursor = deepcopy(plan.scan_cursor)
    with _PIN_BUILDS_LOCK:
        previous = _EXECUTION_PLANS.pop(owner, None)
        if pending:
            token = previous[0] if previous is not None else secrets.token_hex(16)
            cursor[_EXECUTION_CURSOR_KEY] = token
            continuation = replace(plan, actions=pending, scan_cursor=cursor)
            canonical_roots = tuple(dict.fromkeys((*(Path(root).absolute() for root in roots), owner / "conversations")))
            _EXECUTION_PLANS[owner] = (token, continuation, canonical_roots, getattr(config,
                "memory_reference_pin_chunk_bytes", reference_pins._PIN_SOURCE_CHUNK_BYTES))
            while len(_EXECUTION_PLANS) > _pin_capacity():
                _EXECUTION_PLANS.popitem(last=False)
        else:
            cursor.pop(_EXECUTION_CURSOR_KEY, None)
    return cursor, bool(pending)


def _reference_action_allowed(protection, action):
    if action.category not in {"conversation", "completed_task", "tool_output", "subagent_scratch"}:
        return True
    pins, complete = protection
    return (complete and action.record_id not in pins.ids and action.path.name not in pins.ids
            and not any(pins.paths.overlaps(path.resolve(strict=False))
                        for path in (action.path, *action.related_paths)))


# LLM: 正常续点表示未完周期；错误重试不独占周期，健康域完成后可再开新周期，停用域清掉旧续点。
# 函数用途: 在原未预算化扫描前完成会话与快照的共享有界发现。
def _budgeted_discovery_actions(context: _RetentionScanContext) -> list[MemoryRetentionAction]:
    snapshot = Path(context.home.owner_home_dir) / "memory_archive/snapshots/context_bundles"
    snapshot_key = _scan_cursor_key(context.home, "snapshot", snapshot)
    root_keys = {_scan_cursor_key(context.home, "conversation", root): root for root in context.conversation_roots
                 if context.policy.conversation_days > 0}
    known = {*root_keys, snapshot_key}
    for key in tuple(context.scan_cursor):
        if isinstance(key, str) and key.startswith(("conversation:", "snapshot:")) and key not in known:
            context.scan_cursor.pop(key)
    pending = {key for key in known.intersection(context.scan_cursor)
               if not isinstance(context.scan_cursor[key], dict) or context.scan_cursor[key].get("retry") is not True}
    roots = tuple(root for key, root in root_keys.items() if not pending or key in pending)
    actions = _conversation_actions(home=context.home, policy=context.policy,
        held_task_ids=context.held_task_ids, roots=roots,
        now=context.now, errors=context.errors, context=context)
    if not pending or snapshot_key in pending:
        actions.extend(_context_bundle_actions(context))
    return actions


class _PinProjection(reference_pins.ReferencePinSnapshot):
    """同一维护周期的可丢弃投影；游标只保存身份，重启/淘汰后从关闭状态重建。"""

    def __init__(self, context):
        super().__init__(context.home, context.conversation_roots, context.now, current_retention_pins,
                         config=context.config)
        self.thread_directories = {root / "threads" for root in self.roots}
        self.requested_roots = context.conversation_roots if context.requested_roots is None else context.requested_roots
        self.token = secrets.token_hex(16)
        self.lock = threading.Lock()
        self.steps = self._build(context.policy)
        self.budget = context.scan_budget
        self.phase = "build"
        self.progress_steps = 0
        self.paused = False
        self.published = False
        self.index_revisions = []
        self.post_scopes = {}
        self.removed_roots = set()
        self.directory_removals = {}
        self.metadata_checks = {}
        self.verified_leaves = set()
        self.source_children = {}
        self.source_directories = {}
        self.source_leaves = {}
        self.context = context
        self.discovery_done = context.discovery_context is None
        self.queued_actions = {}
        self.candidate_actions = []
        self.candidate_errors = []
        self.closed = False
        # 只用于分类目录成员，不解析链接目标；真正来源及其祖先仍须严格验证。
        from .generation_manifest import MemoryGenerationStore

        store = MemoryGenerationStore(Path(context.home.owner_memory_long_term_jsonl))
        self.pin_roots = {store.path, store.manifest_path, store.directory, store.tombstones.path,
            Path(context.home.owner_retention_json),
            Path(context.home.owner_workspace_dir) / "runtime/workspaces",
            Path(context.home.owner_memory_candidates_jsonl),
            Path(context.home.owner_memory_candidates_jsonl).with_name('candidates.jsonl.records'),
            Path(context.home.owner_memory_candidates_jsonl).parent / "candidates_archive",
            Path(context.home.owner_memory_curator_runs_dir), Path(context.home.owner_memory_curator_runs_dir).parent / "state.json",
            Path(context.home.owner_compact_dir) / "conversations", *_recovery_roots(context.home),
            *(root / "threads" for root in self.roots)}
        self.pin_ancestors = {parent for root in self.pin_roots for parent in (root, *root.parents)}
        self.inventory = {Path(context.home.owner_home_dir),
                          Path(context.home.owner_workspace_dir) / "runtime/workspaces"}

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            try:
                self.steps.close()
            finally:
                try:
                    reference_pins._close_source_descriptors(self.contents["fds"])
                finally:
                    # 关闭表示该周期已退休；断开context回边和临时正文，返回给调用者的pins集合不清空。
                    self.context = None
                    self.value = self.pending = None
                    self.sources.clear()
                    self.contents["files"].clear()
                    self.contents["bytes"] = 0
                    for key in ("digest_bytes", "verified", "recheck"):
                        self.contents[key].clear()
                    self.queued_actions.clear()
                    self.candidate_actions.clear()
                    self.candidate_errors.clear()
                    self.post_scopes.clear()
                    self.removed_roots.clear()
                    self.directory_removals.clear()
                    self.metadata_checks.clear()
                    self.verified_leaves.clear()
                    self.source_children.clear()
                    self.source_directories.clear()
                    self.source_leaves.clear()
                    self.thread_directories.clear()
                    self.index_revisions.clear()

    def _current(self, errors, *, target=None):
        if self.closed:
            raise OSError("pin projection is closed")
        discovery = self.context.discovery_context
        # 继承快照的current作用域和pending发布门；迭代器保留解析位置，绝不把partial当作空pins。
        while self.value is None and not self.budget.exhausted():
            try:
                entries = next(self.steps)
            except StopIteration:
                self.value, self.pending = self.pending[0], None
                self.phase = "discovery"
                self.published = True
                break
            self.progress_steps += 1
            self.budget.charge(entries=1 if entries is None else entries)
        if self.value is None:
            self.paused = True
        elif discovery is not None:
            discovery.conversation_roots = self.roots
            if not self.discovery_done:
                for action in _budgeted_discovery_actions(discovery):
                    self.queued_actions[action.action_id] = action
                self.discovery_done = not _discovery_pending(discovery.scan_cursor)
            discovery.discovery_actions = list(self.queued_actions.values())
            self.queued_actions.clear()
        return self.value

    def _pin_related(self, path):
        return path in self.pin_ancestors or any(parent in self.pin_roots and parent not in self.inventory
                                                 for parent in path.parents)

    def _check_parents(self, path):
        owner = Path(self.home.owner_home_dir)
        anchors = (owner, *self.roots)
        anchor = next((root for root in anchors if path.is_relative_to(root)), None)
        if anchor is None:
            raise OSError(22, "pin source is outside canonical roots", str(path))
        parent = anchor
        for part in ("", *path.relative_to(anchor).parts[:-1]):
            parent = parent / part
            if not stat.S_ISDIR(parent.stat(follow_symlinks=False).st_mode):
                raise OSError(22, "unsafe pin source ancestor", str(parent))

    def _stamp(self, path):
        try:
            stamp = reference_pins._metadata_stamp(path.stat(follow_symlinks=False))
        except FileNotFoundError:
            return None
        if path in self.inventory and stat.S_ISDIR(stamp[2]):
            # 混合目录的mtime/ctime会被无关WAL、锁、维护回执持续改动；引用代次只认目录身份、
            # 类型权限与下方相关成员摘要。独立维护不得反复抹掉已经完成的引用扫描进度。
            return (*stamp[:3], 0, 0, 0)
        return stamp

    def _remember(self, path, stamp, digest):
        observed = (stamp, digest)
        if path in self.sources and self.sources[path] != observed:
            raise _PinReadChanged(11, "pin source changed while reading", str(path))
        if path not in self.sources:
            self.source_children.setdefault(path.parent, set()).add(path)
            index = self.source_directories if stamp is not None and stat.S_ISDIR(stamp[2]) else self.source_leaves
            index[path] = None
        self.sources[path] = observed

    def _forget_source(self, path):
        self.sources.pop(path, None)
        self.source_directories.pop(path, None)
        self.source_leaves.pop(path, None)
        siblings = self.source_children.get(path.parent)
        if siblings is not None:
            siblings.discard(path)
            if not siblings:
                self.source_children.pop(path.parent, None)

    def _chunks(self, path, *, required=False, verifying=False):
        try:
            self._check_parents(path)
        except FileNotFoundError:
            self._remember(path, None, None)
            if required:
                raise
            return
        stamp = self._stamp(path)
        if stamp is None:
            self._remember(path, None, None)
            if required:
                raise FileNotFoundError(2, "pin source missing", str(path))
            return
        if not stat.S_ISREG(stamp[2]):
            raise OSError(22, "pin source is not a regular file", str(path))
        digest = hashlib.sha256()
        scope = None
        offset = 0
        checked = False
        while True:
            current_scope = "settled" if reference_pins._is_settled(stamp) else self.contents["pass_token"]
            if verifying and scope is not None and scope != current_scope:
                offset = 0
                digest = hashlib.sha256()
                checked = False
            scope = current_scope
            if checked and offset == stamp[3]:
                # 最后一个原句柄已证明完整字节；暂停后再核路径，不为查询 EOF 重新打开文件。
                self._check_parents(path)
                leaf = path.stat(follow_symlinks=False)
                if leaf.st_nlink != 1 or reference_pins._metadata_stamp(leaf) != stamp:
                    raise _PinReadChanged(11, "pin source changed before completion", str(path))
                break
            window = min(stamp[3] - offset, self.budget.max_bytes - self.budget.size,
                         reference_pins._MAX_PIN_SOURCE_DIGEST_BYTES)
            if window <= 0 and offset != stamp[3]:
                yield b""
                continue
            self._check_parents(path)
            with closing(_fingerprint_source(path)) as source:
                opened = os.fstat(source.fileno())
                if opened.st_nlink != 1:
                    raise OSError(22, "pin source is not a private regular file", str(path))
                if reference_pins._metadata_stamp(opened) != stamp:
                    raise _PinReadChanged(11, "pin source changed before reading", str(path))
                source.seek(offset)
                # 一次打开读完本片的小窗口；每个底层 read 仍遵守配置，绝不跨暂停持有 FD。
                parts = []
                received = 0
                while received < window:
                    raw = source.read1(min(window - received, self.contents["chunk_bytes"]))
                    self.budget.charge(size=len(raw))
                    if not raw:
                        raise _PinReadChanged(11, "pin source ended before its verified size", str(path))
                    parts.append(raw)
                    received += len(raw)
                    if (received < window and retention_scan_budget.time.monotonic()
                            >= getattr(self.budget, "deadline", float("inf"))):
                        break
                after = os.fstat(source.fileno())
                if (after.st_nlink != 1 or reference_pins._metadata_stamp(after) != stamp
                        or self._stamp(path) != stamp):
                    raise _PinReadChanged(11, "pin source changed while reading", str(path))
                self._check_parents(path)
            checked = True
            if not received:
                break
            offset += received
            block = b"".join(parts)
            del parts
            digest.update(block)
            yield block
        self._remember(path, stamp, digest.digest())
        if verifying:
            self.post_scopes[path] = scope

    def _read(self, path):
        chunks = []
        for raw in self._chunks(path):
            chunks.append(raw)
            yield
        yield
        return b"".join(chunks) if self.sources[path][0] is not None else None

    def _immutable_bytes(self, directory, reference, suffix):
        digest, size, name = reference["sha256"], reference["byte_length"], reference["ref"]
        if (type(digest) is not str or len(digest) != 64
                or any(ch not in "0123456789abcdef" for ch in digest)
                or type(size) is not int or size <= 0 or name != digest + suffix):
            raise ValueError("candidate immutable reference is invalid")
        path = directory / name
        raw = yield from self._read(path)
        if raw is None:
            # 分片读口也保留原不可变索引的精确缺叶原因，不能把缺失洗成无路径摘要错误。
            from ..conversation.store_io import ImmutableIndexNodeMissing

            raise ImmutableIndexNodeMissing(2, "immutable index node is missing", str(path))
        if raw is None or len(raw) != size or hashlib.sha256(raw).hexdigest() != digest:
            raise OSError(22, "candidate immutable source length or digest mismatches", str(path))
        # 内容寻址对象由唯一manifest封印；不把每条历史节点放进逐动作复核集合。
        self._forget_source(path)
        return raw

    def _candidate_lookup(self, directory, reference, key):
        route = hashlib.sha256(key.encode("utf-8", "surrogatepass")).hexdigest()
        for depth in range(65):
            if reference is None:
                return None
            if type(reference) is not dict or set(reference) != {"ref", "maximum"}:
                raise ValueError("candidate immutable index root is invalid")
            raw = yield from self._immutable_bytes(directory, reference["ref"], ".json")
            node = json.loads(raw)
            if (type(node) is not dict or set(node) not in ({"entries"}, {"children"})
                    or type(next(iter(node.values()))) is not dict):
                raise ValueError("candidate immutable index node is invalid")
            if "entries" in node:
                if len(node["entries"]) > 8:
                    raise ValueError("candidate immutable index leaf exceeds fanout")
                return node["entries"].get(key)
            if (depth == 64 or len(node["children"]) > 16
                    or any(type(digit) is not str or len(digit) != 1 or digit not in "0123456789abcdef"
                           for digit in node["children"])):
                raise ValueError("candidate immutable index branch exceeds bounds")
            reference = node["children"].get(route[depth])
        return None

    def _candidate_rows(self, path):
        try:
            yield from self._candidate_rows_unchecked(path)
        except (ValueError, TypeError, KeyError, IndexError, OverflowError) as exc:
            # 候选账的格式/协议损坏仍沿原整份拒绝合同；缺叶等I/O原因保留独立pin诊断。
            raise _CandidatePinSourceUnreadable(22, "candidate ledger is unreadable", str(path)) from exc

    def _candidate_rows_unchecked(self, path):
        from .candidate_ledger import FORMAT, _validate_manifest
        from .candidates import _parse_candidate_line

        with closing(self._rows(path)) as stream:
            first = None
            for row in stream:
                if row is None:
                    yield None
                    continue
                first = row
                break
            if first is None:
                return
            if first.get("candidate_ledger") != FORMAT:
                yield first
                yield from stream
                return
            header = _validate_manifest(first)
            for row in stream:
                if row is not None:
                    raise ValueError("candidate manifest contains extra records")
                yield None
        directory = path.with_name(path.name + ".records")
        for position in range(header["count"]):
            identifier = yield from self._candidate_lookup(directory, header["root"], "pos/" + str(position))
            entry = (yield from self._candidate_lookup(directory, header["root"], "row/" + identifier)) if identifier else None
            if not entry or entry["position"] != position:
                raise ValueError("candidate position index is invalid")
            raw = yield from self._immutable_bytes(directory, entry["record"], ".row")
            candidate = _parse_candidate_line(raw.decode("utf-8"))
            if candidate.candidate_id != identifier:
                raise ValueError("candidate identity index mismatches")
            yield candidate.to_record()

    def _rows(self, path, *, required=False, jsonl=None, strict_lines=False):
        if not (path.suffix == ".jsonl" if jsonl is None else jsonl):
            raw = yield from self._read(path)
            if raw is None:
                if required:
                    raise FileNotFoundError(2, "pin source missing", str(path))
                return
            # 复用唯一对象解析入口；输入是刚经nofollow分片读取并绑定代次的原字节，不再重复读盘。
            self.contents["object_input"] = (path, raw)
            try:
                row = _pin_object(path)
            finally:
                self.contents.pop("object_input", None)
            yield row
            return
        pending = bytearray()
        for raw in self._chunks(path, required=required):
            pending.extend(raw)
            yield None
            start = 0
            while (end := pending.find(b"\n", start)) >= 0:
                line = pending[start:end]
                start = end + 1
                if line and (strict_lines or line.strip()):
                    row = json.loads(line.decode("utf-8"))
                    if not isinstance(row, dict):
                        raise ValueError("pin source contains non-object rows")
                    yield row
            del pending[:start]
        if pending and (strict_lines or pending.strip()):
            row = json.loads(pending.decode("utf-8"))
            if not isinstance(row, dict):
                raise ValueError("pin source contains non-object rows")
            yield row

    def _verify_directory_leaf(self, path, member):
        expected = self.sources.get(path)
        if (not self.published or expected is None or expected[0] is None
                or not stat.S_ISREG(member.st_mode) or self.post_scopes.get(path) != "settled"
                or not reference_pins._is_settled(expected[0])):
            return
        # 与目录页真实stat处于同一调用栈；在交出预算续点前完成此冷叶的比较，不能留旧观测等未来签收。
        if reference_pins._metadata_stamp(member) != expected[0]:
            raise _PinReadChanged(11, "pin source generation changed", str(path))
        self.verified_leaves.add(path)

    def _entries(self, path, *, verifying=False):
        try:
            self._check_parents(path)
        except FileNotFoundError:
            self._remember(path, None, None)
            return
        stamp = self._stamp(path)
        if stamp is None:
            self._remember(path, None, None)
            return
        if not stat.S_ISDIR(stamp[2]):
            raise OSError(22, "pin discovery root is not a directory", str(path))
        # 页先关闭原生目录FD再交出成员；partial快照只保留有界名字和原生续点。
        total = count = 0
        scope = "settled" if reference_pins._is_settled(stamp) else self.contents["pass_token"]
        with closing(self._directory_members(path, stamp)) as members:
            for name, member in members:
                if name is None:
                    yield None, False
                    continue
                scope = "settled" if reference_pins._is_settled(stamp) else self.contents["pass_token"]
                child = path / name
                if verifying:
                    self.metadata_checks[child] = (reference_pins._metadata_stamp(member), self.contents["pass_token"])
                    self._verify_directory_leaf(child, member)
                runtime = Path(self.home.owner_workspace_dir) / "runtime/workspaces"
                selected = (path not in self.inventory or self._pin_related(child)
                            or path == runtime and stat.S_ISDIR(member.st_mode))
                if selected:
                    total = (total + int.from_bytes(hashlib.sha256(
                        os.fsencode(name) + b"\0" + str(member.st_ino).encode()).digest(), "big")) % (1 << 256)
                    count += 1
                if not (stat.S_ISREG(member.st_mode) or stat.S_ISDIR(member.st_mode)):
                    child = path / name
                    if path in self.inventory and not self._pin_related(child):
                        yield child, False
                        continue
                    raise OSError(22, "unsafe pin discovery member", str(path / name))
                yield path / name, stat.S_ISDIR(member.st_mode)
        if self._stamp(path) != stamp:
            raise _PinReadChanged(11, "pin directory changed", str(path))
        removals = self.directory_removals.get(path)
        if verifying and removals:
            previous_stamp, previous_digest = self.sources[path]
            expected_total, expected_count = previous_digest
            for name, inode in removals:
                expected_total = (expected_total - int.from_bytes(hashlib.sha256(
                    os.fsencode(name) + b"\0" + str(inode).encode()).digest(), "big")) % (1 << 256)
                expected_count -= 1
            if previous_stamp[:3] != stamp[:3] or (total, count) != (expected_total, expected_count):
                raise _PinReadChanged(11, "pin directory has changes beyond completed removals", str(path))
            # 只有实际成员摘要准确等于原集合减本次已完成动作，才接受目录的新代次。
            self.sources[path] = (stamp, (total, count))
            self.directory_removals.pop(path)
        else:
            self._remember(path, stamp, (total, count))
        if verifying:
            self.post_scopes[path] = scope or self.contents["pass_token"]

    def _directory_members(self, path, stamp):
        from ..user_space.directory_paging import _native_directory_supported, directory_page

        if _native_directory_supported():
            cursor, complete = {}, False
            while not complete:
                self._check_parents(path)
                if self._stamp(path) != stamp:
                    raise _PinReadChanged(11, "pin directory changed", str(path))
                names, cursor, complete = directory_page(path.parent, (path.name,), cursor,
                    max(1, min(RETENTION_SCAN_SLICE_ENTRIES,
                        self.budget.max_entries - self.budget.entries)))
                if (cursor.get("directory_identity") != [stamp[0], stamp[1]]
                        or self._stamp(path) != stamp):
                    raise _PinReadChanged(11, "pin directory changed", str(path))
                for name in names:
                    if (path in self.inventory
                            and path != Path(self.home.owner_workspace_dir) / "runtime/workspaces"
                            and not self._pin_related(path / name)):
                        continue
                    if path in self.thread_directories and not name.endswith(".json"):
                        continue
                    try:
                        member = (path / name).stat(follow_symlinks=False)
                    except FileNotFoundError as exc:
                        # 成员已被真实写者移走时本代失效；下一片重建，绝不当作空目录发布。
                        raise _PinReadChanged(11, "pin directory member disappeared", str(path / name)) from exc
                    yield name, member
                yield None, None  # 原生页即使只有非权威侧车也计量，不能在单片内无界扫空名。
            return
        # 非原生平台保留原枚举及nofollow成员语义；所有拥有者显式close，不依赖GC。
        with os.scandir(path) as members:
            for index, member in enumerate(members, 1):
                related = (path not in self.inventory or path == Path(self.home.owner_workspace_dir) / "runtime/workspaces"
                           or self._pin_related(path / member.name))
                if related and (path not in self.thread_directories or member.name.endswith(".json")):
                    yield member.name, member.stat(follow_symlinks=False)
                if index % RETENTION_SCAN_SLICE_ENTRIES == 0:
                    yield None, None

    def _walk(self, root, *, recursive=True):
        with closing(self._entries(root)) as stream:
            for path, directory in stream:
                yield None
                if directory and recursive:
                    yield from self._walk(path)
                elif not directory:
                    yield path

    def _verify_source(self, path):
        stamp, _digest = self.sources[path]
        if self._stamp(path) != stamp and path not in self.directory_removals:
            raise _PinReadChanged(11, "pin source generation changed", str(path))
        if stamp is not None:
            if stat.S_ISDIR(stamp[2]):
                with closing(self._entries(path, verifying=True)) as stream:
                    for _ in stream:
                        yield 1
            else:
                for _ in self._chunks(path, verifying=True):
                    yield 1
        yield 1

    def _verify(self):
        hot = []
        self.metadata_checks = {}
        self.verified_leaves = set()
        for path in chain(self.source_directories, self.source_leaves):
            stamp, _digest = self.sources[path]
            if any(parent in self.removed_roots for parent in (path, *path.parents)):
                if self._stamp(path) is not None:
                    raise _PinReadChanged(11, "removed pin source reappeared", str(path))
                self.sources[path] = (None, None)
                yield 1
            elif stamp is not None and not reference_pins._is_settled(stamp):
                hot.append(path)
                yield 1
            elif stamp is not None and stat.S_ISDIR(stamp[2]):
                # 混合inventory的stamp屏蔽mtime，必须核成员；普通冷目录仅在完整metadata漂移/本次删除时重枚举。
                if (not self.published or self.post_scopes.get(path) != "settled"
                        or path in self.inventory or path in self.directory_removals or self._stamp(path) != stamp):
                    yield from self._verify_source(path)
                else:
                    yield 1
            elif path in self.verified_leaves:
                yield 0  # 已在本轮目录页真实stat后立即完成，不再重新计同一份验证工作。
            elif stamp is not None and (not self.published or self.post_scopes.get(path) != "settled"):
                # 首次发布须绑定解析字节与真实复读；热转冷也必须补完一次字节复核，不能只信相同stat。
                yield from self._verify_source(path)
            elif (path in self.metadata_checks
                  and self.metadata_checks[path][1] is self.contents["pass_token"]):
                if self.metadata_checks[path][0] != stamp:
                    raise _PinReadChanged(11, "pin source generation changed", str(path))
                yield 0  # 同一目录页已计量并取得该冷文件的完整stat，不再读一次。
            else:
                if self._stamp(path) != stamp:
                    raise _PinReadChanged(11, "pin source generation changed", str(path))
                yield 1
        # 热组仍须在同一次current完成真实内容复核，不能借冷源的metadata信任放宽。
        while hot:
            for path in hot:
                yield from self._verify_source(path)
            current_pass = self.contents["pass_token"]
            if all(self.post_scopes.get(path) in ("settled", current_pass) for path in hot):
                break
            # 本片装不下完整热组时保持pending；来源冷却后允许逐源有界续验。
            yield 1
        for path in hot:
            if self._stamp(path) != self.sources[path][0]:
                raise _PinReadChanged(11, "pin source generation changed", str(path))
        for root in tuple(self.removed_roots):
            parent = self.sources.get(root.parent)
            if parent is None or parent[0] is None or root.parent in self.directory_removals:
                continue
            # 父目录已准确核验删除差分后，旧叶不再重复作为负向来源驻留；新建成员仍由父集合门发现。
            pending = [root]
            while pending:
                path = pending.pop()
                pending.extend(self.source_children.get(path, ()))
                self._forget_source(path)
                yield 1
            self.removed_roots.discard(root)
        for index, revision in self.index_revisions:
            with index.snapshot(minimum_revision=revision) as snapshot:
                if snapshot.revision != revision:
                    raise _PinReadChanged(11, "tombstone generation changed", str(index.path))
            # 唯一正式库的固定修订末门，不在热组完成与发布之间再暂停。
            self.budget.charge(entries=1)

    def _formal(self, pins):
        from ._jsonl_maintenance import require_maintenance_event
        from .generation_manifest import MemoryGenerationStore
        from .jsonl import (
            _materialize_memory_events,
            _memory_record_from_obj,
            _tombstone_path,
            _with_legacy_identity,
        )

        path = Path(self.home.owner_memory_long_term_jsonl)
        store = MemoryGenerationStore(path)
        manifest = yield from self._read(store.manifest_path)
        marker_body = yield from self._read(store.tombstones.path)
        root = store.manifest(_read=lambda _path: manifest)
        bodies = {store.manifest_path: manifest, store.tombstones.path: marker_body}
        _body, marker = store.tombstones._marker(_read=bodies.__getitem__, _allow_migration=True)
        if root is not None and marker_body is None:
            raise ValueError("MEMORY_TOMBSTONES_UNREADABLE")
        active, generation_ids = {}, set()

        def add(row, *, generation=False):
            record = _memory_record_from_obj(row)
            require_maintenance_event(record)
            if generation:
                if record.action not in {"add", "replace"} or record.entry_id in generation_ids:
                    raise ValueError("MEMORY_GENERATION_UNREADABLE")
                generation_ids.add(record.entry_id)
            record = _with_legacy_identity(record)
            prior = active.get(record.entry_id)
            material = _materialize_memory_events(([prior] if prior is not None else []) + [record], include_expired=True)
            if material:
                active[record.entry_id] = material[0]
            else:
                active.pop(record.entry_id, None)

        if root is None:
            for row in self._rows(path):
                if row is not None:
                    add(row)
                yield int(row is not None)
        else:
            # 根清单仍由唯一schema校验器解析；分片按原hash/count/事件身份校验，逐行可续。
            for segment in root["segments"]:
                source = path.parent / segment["path"]
                count = 0
                for row in self._rows(source, required=True, jsonl=True, strict_lines=True):
                    if row is not None:
                        add(row, generation=True)
                        count += 1
                    yield int(row is not None)
                if self.sources[source][1].hex() != segment["sha256"] or count != segment["count"]:
                    raise ValueError("MEMORY_GENERATION_UNREADABLE")
            # 原正式文件不再是代次权威，但仍属于原快照监视集合。
            for _ in self._chunks(path):
                yield 1
        tombstones = {}
        if marker.get("index_file") is None:
            for entry_id, row in marker["records"].items():
                record = _memory_record_from_obj(row)
                require_maintenance_event(record)
                if record.action != "remove" or record.entry_id != entry_id or row.get("attributes"):
                    raise ValueError("MEMORY_TOMBSTONES_UNREADABLE")
                tombstones[entry_id] = record
                yield 1
        legacy = _tombstone_path(path)
        for row in self._rows(legacy):
            if row is not None:
                record = _memory_record_from_obj(row)
                require_maintenance_event(record)
                if marker.get("index_file") is None:
                    if record.action != "remove":
                        raise ValueError("MEMORY_TOMBSTONES_UNREADABLE")
                    old = tombstones.get(record.entry_id)
                    selected = deepcopy(record if old is None or record.version >= old.version else old)
                    selected.removed_content_hashes = tuple(sorted(set(record.removed_content_hashes)
                        | set(old.removed_content_hashes if old else ())))
                    tombstones[record.entry_id] = selected
            yield int(row is not None)
        if marker.get("index_file") is None:
            if store.tombstones.index.has_committed_authority():
                receipt = store.tombstones.index.migration_receipt()
                identity = hashlib.sha256((marker_body or b"") + b"\0")
                for record in tombstones.values():
                    identity.update(record.to_json().encode())
                    yield 1
                if (receipt is None or receipt.get("migrated_revision") != receipt["revision"]
                        or receipt.get("source_identity") != identity.hexdigest()):
                    raise ValueError("MEMORY_TOMBSTONES_UNREADABLE")
            hashes = set()
            for record in tombstones.values():
                hashes.update(record.removed_content_hashes)
                active.pop(record.entry_id, None)
                yield 1
        else:
            # 复用现有只读SQLite快照，页间只携带修订/主键，不跨片持连接或WAL读锁。
            from .storage_tombstone_index import _validated_tombstone_payload

            revision = None
            after = ""
            minimum = max(marker["index_revision"], (root or {}).get("barrier_revision", 0))
            while True:
                page = max(1, min(RETENTION_SCAN_SLICE_ENTRIES, self.budget.max_entries - self.budget.entries))
                rows = []
                with store.tombstones.index.snapshot(minimum_revision=minimum) as snapshot:
                    if revision is not None and snapshot.revision != revision:
                        raise _PinReadChanged(11, "tombstone generation changed", str(store.tombstones.path))
                    revision = snapshot.revision
                    sizes = snapshot.conn.execute(
                        "SELECT entry_id,length(CAST(payload AS BLOB)) FROM deleted_entries WHERE entry_id>? ORDER BY entry_id LIMIT ?",
                        (after, page)).fetchall()
                    allowance = self.budget.max_bytes - self.budget.size
                    last = None
                    for entry_id, size in sizes:
                        if size > self.budget.max_bytes:
                            raise OSError(22, "pin entry exceeds scan byte budget", str(store.tombstones.path))
                        if size > allowance:
                            break
                        allowance -= size
                        last = entry_id
                    if last is not None:
                        rows = snapshot.conn.execute(
                            "SELECT entry_id,version,payload FROM deleted_entries WHERE entry_id>? AND entry_id<=? ORDER BY entry_id",
                            (after, last)).fetchall()
                if not sizes:
                    break
                if not rows:
                    yield 1
                    continue
                self.budget.charge(size=sum(len(row[2].encode()) for row in rows))
                for row in rows:
                    record = _validated_tombstone_payload(*row)
                    active.pop(record.entry_id, None)
                    after = row[0]
                    yield 1
            self.index_revisions.append((store.tombstones.index, revision))
            items = iter(active.items())
            while True:
                batch = []
                for _ in range(max(1, min(RETENTION_SCAN_SLICE_ENTRIES,
                                         self.budget.max_entries - self.budget.entries))):
                    item = next(items, None)
                    if item is None:
                        break
                    batch.append(item)
                if not batch:
                    break
                with store.tombstones.index.snapshot(minimum_revision=revision) as snapshot:
                    if snapshot.revision != revision:
                        raise _PinReadChanged(11, "tombstone generation changed", str(store.tombstones.path))
                    blocked = snapshot.blocked((entry_id, hashlib.sha256(record.content.encode()).hexdigest())
                                               for entry_id, record in batch) if root is not None else set()
                for entry_id, record in batch:
                    if entry_id not in blocked and (not record.expires_at or record.expires_at > self.now.timestamp()):
                        yield from pins.collect_steps({"source": record.source, "attributes": record.attributes}, path)
                    yield 1
            return
        for record in active.values():
            if ((root is None or hashlib.sha256(record.content.encode()).hexdigest() not in hashes)
                    and (not record.expires_at or record.expires_at > self.now.timestamp())):
                yield from pins.collect_steps({"source": record.source, "attributes": record.attributes}, path)
            yield 1

    def _task_states(self):
        owner = Path(self.home.owner_home_dir)
        for root in _recovery_roots(self.home):
            with closing(self._entries(root)) as stream:
                for child, directory in stream:
                    yield None
                    if not directory:
                        continue
                    with closing(self._task_bases(child)) as stream:
                        for base in stream:
                            if base is None:
                                yield None
                                continue
                            if canonical_task_root(owner, base) is None:
                                continue
                            work = base / "work"
                            # 缺失work/state也监视；不进入tmp/inputs等运行内容树。
                            with closing(self._entries(work)) as stream:
                                for _ in stream:
                                    yield None
                            state = work / "state.json"
                            if self._stamp(state) is None:
                                self._remember(state, None, None)
                            else:
                                yield state

    def _task_bases(self, child):
        yield child
        with closing(self._entries(child)) as stream:
            for nested, directory in stream:
                yield nested if directory else None

    def _build(self, policy):
        pins = _RetentionPins(Path(self.home.owner_home_dir))
        with closing(self._entries(pins.home)) as stream:
            for _ in stream:
                yield 1
        runtime = Path(self.home.owner_workspace_dir) / "runtime/workspaces"
        with closing(self._entries(runtime)) as stream:
            for workspace, directory in stream:
                if directory:
                    self.inventory.add(workspace)
                    conversation = workspace / "conversations"
                    if conversation not in self.roots:
                        self.roots = (*self.roots, conversation)
                        self.thread_directories.add(conversation / "threads")
                        self.pin_roots.add(conversation / "threads")
                        self.pin_ancestors.update((conversation / "threads", *(conversation / "threads").parents))
                    with closing(self._entries(workspace)) as stream:
                        for _ in stream:
                            yield 1
                yield 1
        # 策略读取与后续各源一样绑定字节代次，不能沿用固定前奏中的旧策略。
        raw = yield from self._read(Path(self.home.owner_retention_json))
        if raw is None or MemoryRetentionPolicy.from_payload(json.loads(raw)) != policy:
            raise _PinReadChanged(11, "pin policy generation changed", str(self.home.owner_retention_json))
        yield from self._formal(pins)
        candidate_path = Path(self.home.owner_memory_candidates_jsonl)
        seen, cold_cache = set(), {}
        for row in self._candidate_rows(candidate_path):
            if row is None:
                yield 1
                continue
            try:
                candidate = MemoryCandidate.from_record(row)
            except (ValueError, TypeError, KeyError, IndexError, OverflowError) as exc:
                raise _CandidatePinSourceUnreadable(22, "candidate record is invalid", str(candidate_path)) from exc
            if candidate.candidate_id in seen:
                raise ValueError("duplicate candidate pin source")
            seen.add(candidate.candidate_id)
            self.candidate_actions.extend(_candidate_actions(candidates=self.context.candidates, policy=policy,
                now=self.now, errors=self.candidate_errors, records=(candidate,), authority_fingerprint=""))
            if candidate.cold_detail_ref:
                match = _COLD_REF.fullmatch(candidate.cold_detail_ref)
                if match is None or match[2] != candidate.candidate_id:
                    raise CandidateStoreCorruptError("candidate cold reference is invalid")
                if match[1] not in cold_cache:
                    monthly = {}
                    cold = candidate_path.parent / "candidates_archive" / match[1]
                    for envelope in self._rows(cold, required=True):
                        if envelope is not None:
                            _validate_cold_envelope(candidate_path, envelope)
                            key = envelope["candidate_id"], envelope["sha256"]
                            if key in monthly:
                                raise ValueError("duplicate candidate cold version")
                            monthly[key] = envelope
                        yield int(envelope is not None)
                    cold_cache[match[1]] = monthly
                candidate = _hydrate_candidate(candidate_path, candidate, cold_cache)
                row = candidate.to_record()
            yield from pins.collect_steps(row, candidate_path)
            yield 1
        # 同一次有界来源读取已得到完整摘要；不能为每个删除动作再同步hash整份旧JSONL。
        digest = self.sources[candidate_path][1]
        fingerprint = digest.hex() if digest is not None else "missing"
        for index, action in enumerate(self.candidate_actions):
            self.candidate_actions[index] = replace(action, authority_fingerprint=fingerprint)
            yield 1
        with closing(self._task_states()) as stream:
            for path in stream:
                if path is None:
                    yield 1
                    continue
                for row in self._rows(path, required=True):
                    if row is None:
                        yield 1
                        continue
                    status = str(row.get("status") or "").upper()
                    reconciled = (status == "RUNNING" and path.is_relative_to(pins.home / "runs")
                        and row.get("task_id") not in policy.legal_hold_task_ids
                        and runtime_terminal(self.home, path.parent.parent, row, self.now) is not None)
                    if reconciled or status not in TASK_TERMINAL_STATUSES or row.get("task_id") in policy.legal_hold_task_ids:
                        if not reconciled:
                            pins.paths.add(path.parent.parent.resolve(strict=False))
                        yield from pins.collect_steps(row, path)
                    yield 1
        for root in self.roots:
            # canonical线程仅为直接<id>.json；任务历史等子目录由其原合同读取，不能当成线程正文。
            with closing(self._walk(root / "threads", recursive=False)) as stream:
                for path in stream:
                    if path is None:
                        yield 1
                        continue
                    if path.suffix != ".json":
                        continue
                    for row in self._rows(path, required=True):
                        if row is None:
                            yield 1
                            continue
                        thread = ConversationThread.from_dict(row)
                        if not thread.thread_id or thread.thread_id != path.stem:
                            raise ValueError("pin thread identity mismatches")
                        yield from pins.collect_steps(row, path)
                        yield 1
                        if thread.compact_checkpoint_id:
                            checkpoint = Path(self.home.owner_compact_dir) / "conversations" / f"{thread.thread_id}.jsonl"
                            pins.paths.add(checkpoint.resolve(strict=False))
                            for item in self._rows(checkpoint, required=True):
                                if item is not None:
                                    yield from pins.collect_steps(item, checkpoint)
                                yield int(item is not None)
        curator = Path(self.home.owner_memory_curator_runs_dir)
        with closing(self._walk(curator)) as stream:
            for path in stream:
                if path is None:
                    yield 1
                    continue
                if path.suffix not in {".json", ".jsonl"}:
                    continue
                for row in self._rows(path, required=True):
                    if row is not None:
                        yield from pins.collect_steps(row, path)
                        warnings = row.get("warnings", ())
                        if isinstance(warnings, (list, tuple)) and any(isinstance(value, str)
                            and value.startswith((_CURSOR_CHECKPOINT, _CURSOR_DELTA)) for value in warnings):
                            pins.paths.add(path.resolve(strict=False))
                    yield int(row is not None)
        path = curator.parent / "state.json"
        for row in self._rows(path):
            if row is not None:
                yield from pins.collect_steps(row, path)
            yield 1
        self.phase = "verify"
        self.pending = ((pins, True), self.sources)
        yield 1
        yield from self._verify()


def _budgeted_pin_projection(context):
    owner = Path(context.home.owner_home_dir)
    cursor = context.scan_cursor.get(_PIN_CURSOR_KEY, {})
    token = cursor.get("token") if isinstance(cursor, dict) else None
    snapshot = _admit_pin_projection(context, owner, token)
    if snapshot is None:
        context.scan_partial = True
        return None, False
    before_cursor = deepcopy(context.scan_cursor)
    before_errors = len(context.errors)
    try:
        with snapshot.lock:
            with _PIN_BUILDS_LOCK:
                retained = _PIN_BUILDS.get(owner) is snapshot
            if not retained or snapshot.closed:
                context.scan_partial = True
                return None, False
            continuation = snapshot.phase == "discovery"
            snapshot.budget = context.scan_budget
            snapshot.context = context
            protection = snapshot.current(context.errors)
            with _PIN_BUILDS_LOCK:
                retained = _PIN_BUILDS.get(owner) is snapshot
            if not retained:
                context.scan_cursor.clear()
                context.scan_cursor.update(before_cursor)
                del context.errors[before_errors:]
                context.scan_partial = True
                return None, False
            context.pin_projection = snapshot
            if protection is not None:
                context.errors.extend(snapshot.candidate_errors)
            context.scan_cursor[_PIN_CURSOR_KEY] = {
                "token": snapshot.token, "phase": snapshot.phase, "steps": snapshot.progress_steps,
                "sources": len(snapshot.sources), "slice_entries": snapshot.budget.entries,
                "slice_bytes": snapshot.budget.size,
            }
    except (CandidateStoreCorruptError, OSError, ValueError, TypeError, KeyError, IndexError, OverflowError) as exc:
        with _PIN_BUILDS_LOCK:
            if _PIN_BUILDS.get(owner) is snapshot:
                _PIN_BUILDS.pop(owner)
        snapshot.close()
        context.scan_cursor.pop(_PIN_CURSOR_KEY, None)
        if isinstance(exc, _PinReadChanged):
            for key in tuple(context.scan_cursor):
                if isinstance(key, str) and key.startswith(("conversation:", "snapshot:")):
                    context.scan_cursor.pop(key)
            # 任一代次变化均在后续预算切片重新开始，严禁在暂停前退回同步无预算全扫。
            changed = Path(exc.filename) if exc.filename else None
            source = (str(changed.relative_to(owner)) if changed is not None and changed.is_relative_to(owner)
                      else str(changed or ""))
            context.scan_cursor[_PIN_CURSOR_KEY] = {
                "phase": "restart_required", "completed_steps": snapshot.progress_steps,
                "change": {"code": "MEMORY_RETENTION_PIN_SOURCE_CHANGED", "source": source,
                           "reason": exc.strerror or type(exc).__name__, "side_effects": "not_started"},
            }
            context.scan_partial = True
            return None, False
        context.errors.append(_pin_source_error(exc))
        return (_RetentionPins(owner, complete=False), False), False
    if protection is None:
        context.scan_partial = True
    return protection, continuation


def _admit_pin_projection(context, owner, token):
    candidate = None
    for attempt in range(2):
        retired = []
        probe = None
        with _PIN_BUILDS_LOCK:
            capacity = _pin_capacity()
            # 只有配置缩容可淘汰；优先保留本次要借用的resident。
            while len(_PIN_BUILDS) > capacity:
                victim = next(iter(_PIN_BUILDS))
                if victim == owner:
                    _PIN_BUILDS.move_to_end(victim)
                    victim = next(iter(_PIN_BUILDS))
                retired.append(_PIN_BUILDS.pop(victim))
            snapshot = _PIN_BUILDS.get(owner)
            if snapshot is not None and (snapshot.token != token or snapshot.requested_roots != (
                    context.conversation_roots if context.requested_roots is None else context.requested_roots)
                                         or snapshot.contents["chunk_bytes"] != getattr(context.config,
                                             "memory_reference_pin_chunk_bytes", reference_pins._PIN_SOURCE_CHUNK_BYTES)):
                retired.append(_PIN_BUILDS.pop(owner))
                snapshot = None
            available = len(_PIN_BUILDS) < capacity
            if snapshot is None and not available and attempt == 0:
                probe_owner = next(iter(_PIN_BUILDS))
                probe = (probe_owner, _PIN_BUILDS[probe_owner])
                _PIN_BUILDS.move_to_end(probe_owner)
        for old in retired:
            old.close()
        if snapshot is not None:
            return snapshot
        if available:
            candidate = _PinProjection(context)
            with _PIN_BUILDS_LOCK:
                if owner not in _PIN_BUILDS and len(_PIN_BUILDS) < _pin_capacity():
                    _PIN_BUILDS[owner] = candidate
                    # 进程状态丢失后旧发现动作也丢失，保守重新发现；旧版无pin token的输入续点保持原合同。
                    if token is not None:
                        for key in tuple(context.scan_cursor):
                            if key == _PIN_CURSOR_KEY or key.startswith(("conversation:", "snapshot:")):
                                context.scan_cursor.pop(key)
                    return candidate
            candidate.close()
            return None
        if probe is None:
            return None
        probe_owner, resident = probe
        try:
            stamp = resident._stamp(probe_owner)
            policy_path = Path(resident.home.owner_retention_json)
            policy_stamp = resident._stamp(policy_path)
            policy, error = _load_policy(policy_path)
            retire = (resident.closed or stamp is None or not stat.S_ISDIR(stamp[2]) or error is not None or policy is None
                      or policy_stamp is None or not stat.S_ISREG(policy_stamp[2])
                      or policy.legal_hold or not policy.maintenance_enabled or policy.maintenance_interval_seconds == 0)
        except (OSError, ValueError, TypeError):
            retire = True
        removed = None
        if retire:
            with _PIN_BUILDS_LOCK:
                if _PIN_BUILDS.get(probe_owner) is resident:
                    removed = _PIN_BUILDS.pop(probe_owner)
        if removed is not None:
            removed.close()
    return None


def _pin_capacity():
    # CLI尚未登记全局并发槽位时仍采用唯一配置默认值，显式capacity=1不会被默认下限盖掉。
    return cache_capacity.resident_owners(0) or AgentConfig.__dataclass_fields__["owner_agent_pool_max_agents"].default


def _discovery_pending(cursor):
    return any(isinstance(key, str) and key.startswith(("conversation:", "snapshot:")) and not (
        isinstance(value, dict) and value.get("retry") is True) for key, value in cursor.items())


def _finish_pin_cycle(context):
    if context.scan_budget is None or context.pin_projection is None:
        return True
    pending = _discovery_pending(context.scan_cursor)
    with _PIN_BUILDS_LOCK:
        owner = Path(context.home.owner_home_dir)
        snapshot = _PIN_BUILDS.get(owner)
        if snapshot is not context.pin_projection:
            context.scan_partial = True
            return False
        if not pending:
            _PIN_BUILDS.pop(owner)
    if pending:
        context.scan_partial = True
        return True
    context.scan_cursor.pop(_PIN_CURSOR_KEY, None)
    # 将已完整核验的规范投影交给执行端；执行前仍走有界代次复核，避免重复解析整份候选库。
    retired = []
    with _PIN_BUILDS_LOCK:
        old = _CURRENT_PIN_BUILDS.pop(owner, None)
        if (old is not None and old is not snapshot and not old.closed and old.value is None
                and old.pending is not None and old.phase == "verify"
                and old.requested_roots == snapshot.requested_roots
                and old.context.policy == snapshot.context.policy
                and old.contents["chunk_bytes"] == snapshot.contents["chunk_bytes"]):
            # 执行前的分片复核可能长于一轮规划；新规划不得抹掉其已核进度。
            # 旧投影仍须自行通过完整来源代次门，不能以新规划为其补签complete。
            retired.append(snapshot)
            snapshot = old
        elif old is not None and old is not snapshot:
            retired.append(old)
        while len(_CURRENT_PIN_BUILDS) >= _pin_capacity():
            _, old = _CURRENT_PIN_BUILDS.popitem(last=False)
            retired.append(old)
        _CURRENT_PIN_BUILDS[owner] = snapshot
    for old in retired:
        old.close()
    return True


# LLM: 共用法律保留和终态输入；仅会话扫描消费共享预算，不以mtime预筛线程权威。
# 函数用途: 汇总工具正文、子代理临时目录、候选和完整会话动作。
def _protected_runtime_actions(
    context: _RetentionScanContext,
    selected_task_roots: frozenset[Path],
) -> list[MemoryRetentionAction]:
    common = {
        "home": context.home,
        "policy": context.policy,
        "held_task_ids": context.held_task_ids,
        "selected_task_roots": selected_task_roots,
        "now": context.now,
        "errors": context.errors,
    }
    actions = _tool_output_actions(**common)
    actions.extend(_subagent_scratch_actions(**common))
    if context.pin_projection is not None:
        actions.extend(context.pin_projection.candidate_actions)
    actions.extend(
        _conversation_actions(
            home=context.home,
            policy=context.policy,
            held_task_ids=context.held_task_ids,
            roots=context.conversation_roots,
            now=context.now,
            errors=context.errors,
            context=context,
        ) if context.scan_budget is None else ()
    )
    return actions


# LLM: 仅临时缓存按mtime规划；记忆和Compact不是年龄清理对象，策略兼容字段仍保留。
# 函数用途: 汇总临时文件按龄动作与记录级安全审计整理。
def _aged_file_actions(
    context: _RetentionScanContext,
    selected_task_roots: frozenset[Path],
    protection: tuple[_RetentionPins, bool],
) -> list[MemoryRetentionAction]:
    home = context.home
    policy = context.policy
    # memory/daily和memory/curator/runs是记忆资产，永不按mtime删除；旧策略键仅用于兼容（#178）。
    # O/compact保存活线程可达的CoW节点及检查点副本，mtime不表示存活；由W3按可达性回收。
    categories = (
        # LLM: audit 类别本次一律跳过。类别名与实际数据不符——O/audit/*.jsonl 是主代理每轮的记忆归档
        #   原始事件，Curator 要读，不是审计日志；直接删不可恢复。等存储登记表定了语义再处理
        #   （dev 2026-09-28 裁定）。
        # ("audit", Path(home.owner_audit_dir), policy.audit_days, "*.jsonl"),
        ("cache", Path(home.owner_cache_dir), policy.cache_days, "*"),
        ("tmp", Path(home.owner_tmp_dir), policy.tmp_days, "*"),
    )
    actions: list[MemoryRetentionAction] = []
    for category, root, days, pattern in categories:
        actions.extend(
            _expired_file_actions(
                category=category,
                root=root,
                days=days,
                pattern=pattern,
                now=context.now,
                excluded_roots=selected_task_roots,
                errors=context.errors,
                protection=None,
            )
        )
    actions.extend(_security_audit_actions(home, policy.security_audit_days, context.now, context.errors))
    return actions


# LLM: 仅处理原写入器的两个owner内规范安全审计文件；不扫描raw audit/，不按文件mtime删除整份混龄账本。
# 函数用途: 封存段按日期整段回收，仅小活动文件规划兼容压缩。
def _security_audit_actions(home, days, now, errors):
    if days <= 0:
        return []
    cutoff = now.timestamp() - days * _DAY_SECONDS
    cutoff_day = datetime.fromtimestamp(cutoff, tz=timezone.utc).date().isoformat()
    actions = []
    for path in security_audit_paths(home):
        try:
            for segment in audit_segment_paths(path):
                if audit_segment_day(segment) < cutoff_day:
                    actions.append(_action(category="security_audit", operation="delete_file", path=segment,
                        reason=f"older_than_{days}_days", proof=_ActionProof(
                            source_fingerprint=security_audit_segment_fingerprint(segment), cutoff_timestamp=cutoff)))
            try:
                snapshot = path.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            if snapshot.st_size >= STORAGE_LANE_SLICE_BYTES:
                continue
            digest = security_audit_prefix_fingerprint(path, snapshot=snapshot)
            _content, removed = security_audit_content(path, cutoff, snapshot=snapshot)
            if not removed:
                continue
            actions.append(_action(category="security_audit", operation="security_audit_compact", path=path,
                reason=f"older_than_{days}_days", proof=_ActionProof(
                    source_fingerprint=digest, cutoff_timestamp=cutoff)))
        except (OSError, ValueError):
            errors.append(_state_error("MEMORY_RETENTION_SECURITY_AUDIT_UNREADABLE", path))
    return actions


# LLM: 路径与runtime_paths、interaction_actor_audit现有生产写入器一致；自定义外部audit路径不由retention猜测或扫描。
# 函数用途: 返回默认特权工具与交互事件的安全审计文件，不混入策展原料。
def security_audit_paths(home):
    return (Path(home.owner_logs_dir) / "audit" / "audit.jsonl", Path(home.owner_audit_log_jsonl))


# LLM: 保留未知/坏JSON行及原字节排版；读取上限固定为开始长度，正常追加不使既有前缀失效。
# 函数用途: 计算安全审计保留内容与实际删除行数，扫描和锁内执行共用。
def security_audit_content(path, cutoff, *, snapshot=None):
    kept, removed = [], 0
    with _security_audit_prefix_source(path, snapshot) as (source, before):
        remaining = before.st_size
        while remaining:
            raw = source.readline(remaining)
            if not raw:
                raise OSError("security audit truncated while reading")
            remaining -= len(raw)
            text = raw.decode("utf-8")
            try:
                row = json.loads(text)
            except ValueError:
                row = None
            stamp = _timestamp(row.get("timestamp")) if isinstance(row, dict) else None
            if stamp is not None and stamp < cutoff:
                removed += 1
            else:
                kept.append(text)
    return "".join(kept), removed


def security_audit_segment_fingerprint(path):
    """封存段不再改写，以文件身份摘要复核删除，扫描不打开正文。"""
    try:
        info = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            return "unreadable"
        return hashlib.sha256(repr((path.name, _file_identity(info))).encode("utf-8")).hexdigest()
    except FileNotFoundError:
        return "missing"


def _security_audit_prefix_current(before, current):
    if (not stat.S_ISREG(current.st_mode) or current.st_nlink != 1
            or (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino)
            or current.st_size < before.st_size):
        raise OSError("security audit replaced or truncated while reading")


@contextmanager
def _security_audit_prefix_source(path, snapshot=None):
    """同一 FD 与路径必须仍指向原 inode；增长仅允许在快照长度之外。"""
    with _fingerprint_source(path) as source:
        before = snapshot if snapshot is not None else os.fstat(source.fileno())
        _security_audit_prefix_current(before, os.fstat(source.fileno()))
        yield source, before
        _security_audit_prefix_current(before, os.fstat(source.fileno()))
        _security_audit_prefix_current(before, path.stat(follow_symlinks=False))


def security_audit_prefix_fingerprint(path, *, snapshot=None):
    """仅安全审计采用固定前缀摘要，通用文件指纹语义保持不变。"""
    try:
        with _security_audit_prefix_source(path, snapshot) as (source, before):
            digest = hashlib.sha256()
            remaining = before.st_size
            while remaining:
                chunk = source.read(min(65536, remaining))
                if not chunk:
                    raise OSError("security audit truncated while fingerprinting")
                digest.update(chunk)
                remaining -= len(chunk)
        return digest.hexdigest()
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unreadable"


# LLM: 快照仍走原回收主链；预算片未掌握完整剩余引用时不规划清单，不能把部分视图当全域无人引用。
#   快照与工具清单一起规划：清单"还有没有人用"只由本轮不会被移走的剩余 .json 快照摘要里的
#   manifest_sha256（结构化字段）决定。先删快照、再按剩余引用删清单的次序由类别名保证——计划最后按
#   (category, path) 排序，context_bundle_snapshot 排在 tool_manifest 前面，执行时快照先走。
# 函数用途: 规划过期快照与无人引用的旧工具清单的回收动作（两者都走可恢复回收站）。
def _context_bundle_actions(context: _RetentionScanContext) -> list[MemoryRetentionAction]:
    base = Path(context.home.owner_home_dir) / "memory_archive" / "snapshots"
    snapshot_key = _scan_cursor_key(context.home, "snapshot", base / "context_bundles")
    resumed = bool(context.scan_cursor.get(snapshot_key))
    actions, kept = _snapshot_retention_actions(context, base / "context_bundles")
    # 续扫片无法证明此前保留快照的引用完整；等一次完整单片或非预算扫描再回收清单。
    if not context.scan_partial and not resumed:
        actions.extend(_manifest_retention_actions(context, base / "tool_manifests", kept))
    return actions


# LLM: 游标只用稳定的域前缀及owner相对根；显式外部根保留绝对身份，不把不同目录合并。
# 函数用途: 生成会话或快照扫描域的确定性续点键。
def _scan_cursor_key(home: object, kind: str, root: Path) -> str:
    try:
        identity = str(root.relative_to(Path(home.owner_home_dir)))
    except ValueError:
        identity = str(root)
    return f"{kind}:{identity}"


# LLM: 单次stream枚举且不跟随链接；walk局部失败按真实路径记录，继续健康分支，上层标不完整而非空树。
# 函数用途: 产生线程文件名或快照相对路径，供有界最小堆取窗口。
def _scan_entry_names(root: Path, *, recursive: bool, errors: list[MemoryRetentionError] | None = None):
    if not recursive:
        with os.scandir(root) as entries:
            for entry in entries:
                if entry.name.endswith(".json"):
                    yield entry.name
        return

    # LLM: 局部失败必须保留路径诊断并继续其他分支；没有错误收集方时仍显式抛错，不吞异常。
    # 函数用途: 登记无法枚举的快照分支，留给上层标记不完整并保护引用。
    def unreadable(error):
        if errors is None:
            raise error
        errors.append(_state_error("MEMORY_RETENTION_SCAN_UNREADABLE", Path(error.filename) if error.filename else root))

    for directory, dirs, files in os.walk(root, followlinks=False, onerror=unreadable):
        dirs.sort()
        for name in sorted(files):
            yield str((Path(directory) / name).relative_to(root))
        for name in dirs:
            path = Path(directory) / name
            if path.is_symlink():
                yield str(path.relative_to(root))


# LLM: 单次全序发现用O(limit)堆；超时保留有界窗口到下一片，不延长deadline或推进未读项；字节准入在解析之前。
# 函数用途: 逐片返回候选路径并更新内存中的部分标记及续点，永不保存游标。
def _budgeted_scan_paths(context: _RetentionScanContext, root: Path, key: str, *, recursive: bool = False):
    budget = context.scan_budget
    cursor = context.scan_cursor.get(key)
    after = cursor.get("after", "") if isinstance(cursor, dict) else ""
    if not isinstance(after, str):
        after = ""
    limit = min(RETENTION_SCAN_SLICE_ENTRIES, max(0, budget.max_entries - budget.entries))
    try:
        if not stat.S_ISDIR(root.lstat().st_mode):
            raise OSError("scan root is not a real directory")
    except FileNotFoundError:
        context.scan_cursor.pop(key, None)
        return
    except OSError:
        context.errors.append(_state_error("MEMORY_RETENTION_SCAN_UNREADABLE", root))
        context.scan_partial = True
        context.scan_cursor[key] = {"after": "", "retry": True}
        return
    if budget.exhausted():
        context.scan_partial = True
        context.scan_cursor[key] = cursor if isinstance(cursor, dict) and cursor.get("retry") is not True else {"after": after}
        return
    cached = cursor.get("window") if isinstance(cursor, dict) else None
    valid_cache = (isinstance(cached, list) and 0 < len(cached) <= RETENTION_SCAN_SLICE_ENTRIES + 1
                   and type(cursor.get("window_complete")) is bool
                   and all(isinstance(name, str) and name > after and not Path(name).is_absolute()
                           and ".." not in Path(name).parts and (recursive or Path(name).name == name)
                           for name in cached))
    error_count = len(context.errors)
    try:
        selected = sorted(set(cached)) if valid_cache else heapq.nsmallest(limit + 1,
            (name for name in _scan_entry_names(root, recursive=recursive, errors=context.errors) if name > after))
    except OSError:
        context.errors.append(_state_error("MEMORY_RETENTION_SCAN_UNREADABLE", root))
        context.scan_partial = True
        context.scan_cursor[key] = {"after": "", "retry": True}
        return
    incomplete = len(context.errors) > error_count or bool(valid_cache and cursor.get("window_incomplete"))
    if incomplete:
        context.scan_partial = True
    complete_window = cursor["window_complete"] if valid_cache else len(selected) <= limit
    for index, name in enumerate(selected[:limit]):
        if budget.exhausted():
            context.scan_partial = True
            context.scan_cursor[key] = {"after": after, "window": selected[index:], "window_complete": complete_window}
            if incomplete:
                context.scan_cursor[key]["window_incomplete"] = True
            return
        path = root / name
        try:
            info = path.lstat()
            if info.st_size > budget.max_bytes:
                budget.charge(entries=1)
                after = name
                context.errors.append(_state_error("MEMORY_RETENTION_SCAN_ENTRY_TOO_LARGE", path))
                continue
            if info.st_size > budget.max_bytes - budget.size:
                context.scan_partial = True
                context.scan_cursor[key] = {"after": after, "window": selected[index:], "window_complete": complete_window}
                if incomplete:
                    context.scan_cursor[key]["window_incomplete"] = True
                return
            budget.charge(entries=1, size=info.st_size)
            after = name
            if stat.S_ISLNK(info.st_mode):
                context.errors.append(_symlink_error(path))
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
        except FileNotFoundError:
            budget.charge(entries=1)
            after = name
            continue
        except OSError:
            budget.charge(entries=1)
            after = name
            context.errors.append(_state_error("MEMORY_RETENTION_SCAN_UNREADABLE", path))
            continue
        yield path
    if len(selected) > limit or not complete_window:
        context.scan_partial = True
        context.scan_cursor[key] = {"after": after}
        if len(selected) > limit and valid_cache:
            context.scan_cursor[key].update(window=selected[limit:], window_complete=complete_window)
            if incomplete:
                context.scan_cursor[key]["window_incomplete"] = True
    elif incomplete:
        context.scan_cursor[key] = {"after": "", "retry": True}
    else:
        context.scan_cursor.pop(key, None)


# LLM: 快照范围 = memory_archive/snapshots/context_bundles/<日期>/ 下的 <请求>.json 与 .md；latest 副本
#   永不参与删除（但它的引用照算）。30 天内改过的都保留，进行中回合的快照天然安全；命中 30 天的交给
#   trash_file 移进回收站，不做永久删除。清单引用只从"不会被移走的剩余 .json"里读，被移走的那批不算。
# LLM: 预算模式以相对路径全序选有界窗口；每个文件计条目/字节，未读完只返回部分发现。
# 函数用途: 扫描过期快照文件，返回动作与本片保留的JSON引用来源。
def _snapshot_retention_actions(
    context: _RetentionScanContext,
    root: Path,
) -> tuple[list[MemoryRetentionAction], tuple[Path, ...]]:
    if not root.exists():
        context.scan_cursor.pop(_scan_cursor_key(context.home, "snapshot", root), None)
        return [], ()
    cutoff = context.now.timestamp() - _CONTEXT_BUNDLE_SNAPSHOT_DAYS * _DAY_SECONDS
    actions: list[MemoryRetentionAction] = []
    kept: list[Path] = []
    paths = (sorted(root.rglob("*")) if context.scan_budget is None else
             _budgeted_scan_paths(context, root, _scan_cursor_key(context.home, "snapshot", root), recursive=True))
    for path in paths:
        if path.is_symlink():
            context.errors.append(_symlink_error(path))
            continue
        if path.is_dir() or path.suffix not in {".json", ".md"} or not path.is_file():
            continue
        if _keep_snapshot(context, kept, path, cutoff):
            continue
        actions.append(
            _action(
                category="context_bundle_snapshot",
                operation="trash_file",
                path=path,
                reason=f"older_than_{_CONTEXT_BUNDLE_SNAPSHOT_DAYS}_days",
                destination=_trash_destination(
                    context.home,
                    category="context_bundle_snapshot",
                    path=path,
                    now=context.now,
                ),
                proof=_ActionProof(
                    source_fingerprint=file_fingerprint(path),
                    cutoff_timestamp=cutoff,
                ),
            )
        )
    return actions, tuple(kept)


# LLM: latest、30 天内修改和显式 legal_hold_task_ids 的快照必须保留；Markdown 通过同名 JSON 的 scope 关联。
#   保留的 .json 要作为"剩余快照"参与清单引用判断；.md 不含结构化字段，不参与。
# 函数用途: 判断一个快照文件是否保留；是的话把 .json 记入引用来源并返回 True。
def _keep_snapshot(
    context: _RetentionScanContext, kept: list[Path], path: Path, cutoff: float
) -> bool:
    keep = (
        path.name.startswith(_CONTEXT_BUNDLE_LATEST_PREFIX)
        or _mtime(path) >= cutoff
        or _snapshot_task_held(context, path)
    )
    if keep and path.suffix == ".json":
        kept.append(path)
    return keep


# LLM: 任务保护只读快照 scope.task_id 与 typed policy 的精确交集；Markdown 不解析正文，读取同名 JSON。
#   保护策略存在而快照无法读取时关闭式保留并报错，不让损坏记录绕过 hold。
# 函数用途: 判断某个旧快照是否属于显式禁止清理的任务。
def _snapshot_task_held(context: _RetentionScanContext, path: Path) -> bool:
    if not context.held_task_ids:
        return False
    metadata_path = path.with_suffix(".json")
    report = read_json_object_report(metadata_path, context="memory_retention.context_bundle_hold")
    if report.load_error is not None:
        context.errors.append(MemoryRetentionError(
            "MEMORY_RETENTION_CONTEXT_BUNDLE_INVALID", str(metadata_path),
            "snapshot task scope is unreadable; legal hold cannot be excluded",
        ))
        return True
    scope = report.payload.get("scope")
    task_id = scope.get("task_id") if isinstance(scope, dict) else None
    return isinstance(task_id, str) and task_id in context.held_task_ids


# LLM: 清单同时满足"修改时间超过 30 天"和"没有任何剩余快照引用"才回收；读不懂任何一份剩余快照时
#   算不出完整引用集合，本轮不动任何清单（关闭式失败，只报告结构化错误）。
# 函数用途: 规划无人引用的旧工具清单回收动作。
def _manifest_retention_actions(
    context: _RetentionScanContext,
    root: Path,
    kept: tuple[Path, ...],
) -> list[MemoryRetentionAction]:
    if not root.exists():
        return []
    referenced, complete = _referenced_manifest_shas(kept, context.errors)
    if not complete:
        return []
    cutoff = context.now.timestamp() - _CONTEXT_BUNDLE_SNAPSHOT_DAYS * _DAY_SECONDS
    actions: list[MemoryRetentionAction] = []
    for path in sorted(root.glob("*.json")):
        if path.is_symlink():
            context.errors.append(_symlink_error(path))
            continue
        if not path.is_file() or path.stem in referenced:
            continue
        if _mtime(path) >= cutoff:
            continue
        actions.append(
            _action(
                category="tool_manifest",
                operation="trash_file",
                path=path,
                reason=f"unreferenced_older_than_{_CONTEXT_BUNDLE_SNAPSHOT_DAYS}_days",
                destination=_trash_destination(
                    context.home,
                    category="tool_manifest",
                    path=path,
                    now=context.now,
                ),
                proof=_ActionProof(
                    source_fingerprint=file_fingerprint(path),
                    cutoff_timestamp=cutoff,
                ),
            )
        )
    return actions


# LLM: 引用只认结构化字段：manifest_sha256 与 manifest_ref 都非空（与读取端一致——ref 空表示未持久化，
#   读取端根本不会碰磁盘）；不按文件名猜引用。任一份剩余快照读不懂时返回 complete=False，调用方关闭式跳过。
# 函数用途: 汇总剩余快照引用的清单 sha 集合与"引用集合是否完整"。
def _referenced_manifest_shas(
    kept: tuple[Path, ...],
    errors: list[MemoryRetentionError],
) -> tuple[frozenset[str], bool]:
    shas: set[str] = set()
    complete = True
    for path in kept:
        report = read_json_object_report(path, context="memory_retention.context_bundle_snapshot")
        if report.load_error is not None:
            errors.append(
                MemoryRetentionError(
                    "MEMORY_RETENTION_CONTEXT_BUNDLE_INVALID",
                    str(path),
                    "snapshot summary is unreadable; tool manifests stay untouched this run",
                )
            )
            complete = False
            continue
        manifest = report.payload.get("tool_manifest")
        if not isinstance(manifest, dict):
            continue
        sha = str(manifest.get("manifest_sha256") or "").strip()
        ref = str(manifest.get("manifest_ref") or "").strip()
        if sha and ref:
            shas.add(sha)
    return frozenset(shas), complete


# LLM: 执行端在快照动作结束后重读当前引用；返回值仅限本次 apply 使用，不持久化为第二份权威索引。
# 函数用途: 汇总磁盘上仍存在的快照引用，保护移动失败、规划后变化或新增的快照所用清单。
def current_tool_manifest_references(home: object) -> tuple[frozenset[str], bool]:
    root = Path(home.owner_home_dir) / "memory_archive" / "snapshots" / "context_bundles"
    if not root.exists():
        return frozenset(), True
    paths = tuple(root.rglob("*.json"))
    if root.is_symlink() or any(path.is_symlink() for path in paths):
        return frozenset(), False
    errors: list[MemoryRetentionError] = []
    return _referenced_manifest_shas(paths, errors)


# LLM: 符号链接错误采用统一机器码；不读取链接目标，也不靠错误文案决定后续动作。
# 函数用途: 构造一条"符号链接被跳过"的结构化错误（retention 从不跟随符号链接）。
def _symlink_error(path: Path) -> MemoryRetentionError:
    return MemoryRetentionError(
        "MEMORY_RETENTION_SYMLINK_SKIPPED",
        str(path),
        "retention never follows symbolic links",
    )


# LLM: runtime 只接受当前 schema；旧 v1 key 由 MemoryMigrationService 一次性转换。
# 函数用途: 严格读取 retention.json 并给损坏返回稳定错误。
def _load_policy(
    path: Path,
) -> tuple[MemoryRetentionPolicy | None, MemoryRetentionError | None]:
    # 只读一次原始字节并交引用快照绑定，解析的正是被比对的字节；缺失/坏JSON/非对象分类同read_json_object_report。
    try:
        body = path.read_bytes() if path.exists() else None
    except OSError as exc:
        return None, MemoryRetentionError("MEMORY_RETENTION_POLICY_UNREADABLE", str(path), type(exc).__name__)
    reference_pins.record_parsed_pin_bytes(path, body)
    try:
        payload = json.loads(body.decode("utf-8")) if body is not None else {}
        if not isinstance(payload, dict):
            raise ValueError(f"JSON root is {type(payload).__name__}, expected object")
    except ValueError as exc:
        return None, MemoryRetentionError("MEMORY_RETENTION_POLICY_UNREADABLE", str(path), type(exc).__name__)
    try:
        return MemoryRetentionPolicy.from_payload(payload), None
    except (TypeError, ValueError) as exc:
        return None, MemoryRetentionError(
            "MEMORY_RETENTION_POLICY_INVALID",
            str(path),
            str(exc),
        )


# 输入只来自规范有界引用流；不再自行读取/复制候选库或同步计算全文件摘要。
# 函数用途: 为已读取的一片候选生成严格早于cutoff的删除动作。
def _candidate_actions(
    *,
    candidates: CandidateService,
    policy: MemoryRetentionPolicy,
    now: datetime,
    errors: list[MemoryRetentionError],
    records,
    authority_fingerprint: str,
) -> list[MemoryRetentionAction]:
    if policy.rejected_candidate_days <= 0:
        return []
    cutoff = now.timestamp() - policy.rejected_candidate_days * _DAY_SECONDS
    actions: list[MemoryRetentionAction] = []
    for record in records:
        if not candidate_retention_eligible(record):
            continue
        from .candidates import candidate_retention_time

        updated = _timestamp(candidate_retention_time(record))
        if updated is None:
            errors.append(
                MemoryRetentionError(
                    "MEMORY_RETENTION_CANDIDATE_TIMESTAMP_INVALID",
                    str(candidates.path),
                    f"candidate {record.candidate_id} has invalid updated_at",
                )
            )
            continue
        if updated >= cutoff:
            continue
        actions.append(
            _action(
                category="rejected_candidate",
                operation="candidate_delete",
                path=candidates.path,
                reason=f"terminal_candidate_older_than_{policy.rejected_candidate_days}_days",
                proof=_ActionProof(
                    authority_path=candidates.path,
                    authority_fingerprint=authority_fingerprint,
                    record_id=record.candidate_id,
                    cutoff_timestamp=cutoff,
                ),
            )
        )
    return actions


# LLM: 恢复材料有两个规范根：旧版 O/tasks 与新版运行工作区 O/runs；两者共用同一套 work/state.json 合同。
#   新增运行根不能让旧根失去保护，也不能凭目录名推断类别。
# 函数用途: 返回保存恢复材料的全部规范根目录。
def _recovery_roots(home: object) -> tuple[Path, ...]:
    owner_home = Path(home.owner_home_dir)
    roots = (durable_work_root(owner_home, "task"), owner_home / "tasks", durable_work_root(owner_home, "audit"))
    return tuple(dict.fromkeys(roots))


# LLM: 只枚举本层目录且记录发现代次；仅ENOENT为空，权限/IO/链接/特殊类型必须上抛，不进入scratch正文。
# 函数用途: 严格列出一个发现目录下的普通子目录，避免glob吞错。
def _discovery_directories(root: Path):
    watch_pin_source(root)
    try:
        info = root.stat(follow_symlinks=False)
    except FileNotFoundError:
        return ()
    if not stat.S_ISDIR(info.st_mode):
        raise OSError(22, "unsafe task discovery directory", str(root))
    children = []
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                try:
                    member = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    # 一个成员消失不代表整个发现目录为空，继续检查其余活跃任务。
                    continue
                if stat.S_ISDIR(member.st_mode):
                    children.append(Path(entry.path))
                elif not stat.S_ISREG(member.st_mode):
                    raise OSError(22, "unsafe task discovery member", entry.path)
    except FileNotFoundError:
        return ()
    return tuple(sorted(children))


# LLM: 精确suffix父链逐段lstat且监视缺失路径；work已存在而首次state发表也会使快照失效。
# 函数用途: 检查任务的规范状态/临时目录，不递归枚举其中内容。
def _discovery_suffix(base: Path, suffix: str):
    path = base
    for part in Path(suffix).parts:
        path = path / part
        watch_pin_source(path)
        try:
            info = path.stat(follow_symlinks=False)
        except FileNotFoundError:
            return None
        if not stat.S_ISDIR(info.st_mode) and (path.name != "state.json" or not stat.S_ISREG(info.st_mode)):
            raise OSError(22, "unsafe task discovery suffix", str(path))
    return path


# LLM: 一/二层有界发现共用显式scandir；计划记录错误而pin调用上抛使complete=False，不递归任务内容；不得按合法任务名过滤。
# 函数用途: 枚举扁平和日期分组任务的精确路径并监视work及缺失state。
def _fixed_depth_paths(root: Path, suffix: str, errors=None):
    paths = set()
    try:
        for child in _discovery_directories(root):
            path = _discovery_suffix(child, suffix)
            if path is not None:
                paths.add(path)
            for nested in _discovery_directories(child):
                path = _discovery_suffix(nested, suffix)
                if path is not None:
                    paths.add(path)
    except OSError as exc:
        if errors is None:
            raise
        errors.append(_state_error("MEMORY_RETENTION_TASK_DISCOVERY_UNREADABLE", Path(exc.filename or root)))
        return []
    return sorted(paths)


# LLM: 仅发现owner内两个规范DB位置，读取失败、链接或任何活跃锁均保守关闭，不创建DB或改PRAGMA持久设置。
# 函数用途: 返回可用于只读终态核对的运行库路径。
def _runtime_db_paths(home):
    owner = Path(home.owner_home_dir)
    return tuple(path for path in (owner / "runtime.db", *sorted(
        (Path(home.owner_workspace_dir) / "runtime/workspaces").glob("*/runtime.db"))) if path.exists())


# LLM: query_only+mode=ro，原schema的lease锁先核；不认投影正文、不补猜缺失表的无锁事实。
# 函数用途: 用既有资源锁判断scratch是否可清理；任一库未知则不删。
def _runtime_unlocked(home, now):
    try:
        for path in _runtime_db_paths(home):
            if path.is_symlink() or not path.is_file():
                return False
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.05)) as db:
                db.execute("PRAGMA query_only=ON")
                if db.execute("SELECT 1 FROM resource_locks WHERE lease_expires_at > ? LIMIT 1", (now.timestamp(),)).fetchone():
                    return False
        return True
    except (sqlite3.Error, OSError):
        return False


# LLM: 同一只读快照精确查run/task身份，所有匹配运行都须terminal；不把DB更新时钟当来源权威。
# 函数用途: 为仍写RUNNING的恢复档案取得终态及完成时间，任何冲突/未知返回None。
def runtime_terminal(home, task_root, state, now):
    if not _runtime_unlocked(home, now):
        return None
    run_id = str(state.get("run_id") or task_root.name)
    task_id = str(state.get("task_id") or "")
    matches = []
    try:
        for path in _runtime_db_paths(home):
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.05)) as db:
                db.execute("PRAGMA query_only=ON")
                db.execute("BEGIN")
                rows = db.execute("SELECT status, updated_at FROM agent_runs WHERE run_id=? OR agent_run_id=?", (run_id, run_id)).fetchall()
                if task_id:
                    rows += db.execute("SELECT status, MAX(updated_at, closed_at) FROM task_runs WHERE task_id=?", (task_id,)).fetchall()
                if not rows:
                    continue
                if any(str(status).upper() not in {"DONE", "FAILED", "CANCELLED"} or _timestamp(stamp) is None
                       for status, stamp in rows):
                    return None
                matches.extend(rows)
    except (sqlite3.Error, OSError):
        return None
    if not matches:
        return None
    return max(((str(status).upper(), _timestamp(stamp)) for status, stamp in matches), key=lambda item: item[1])


# LLM: 只遍历已授权scratch；链接/特殊文件/新鲜普通文件均关闭整棵，空子目录不凭新建mtime延长旧文件寿命。
# 函数用途: 为闲置scratch计算安全元数据指纹和清理快照，不读取大文件正文。
def scratch_inventory(root: Path, cutoff: float):
    before = root.stat(follow_symlinks=False)
    if not stat.S_ISDIR(before.st_mode):
        raise OSError("scratch root is not a regular directory")
    members = []
    for path in _walk_files(root, include_dirs=True):
        info = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) and not stat.S_ISDIR(info.st_mode):
            raise OSError("scratch member is unsafe")
        if stat.S_ISREG(info.st_mode) and info.st_mtime >= cutoff:
            raise OSError("scratch is active")
        members.append((str(path.relative_to(root)), _file_identity(info)))
    material = json.dumps([_file_identity(before), sorted(members)], separators=(",", ":"))
    return hashlib.sha256(material.encode()).hexdigest(), members


# LLM: stateless任务也可清tmp；存在状态则法律保留/活跃状态/坏状态保护，DB或文件锁未知同样保护。
# 函数用途: 检查task scratch的当前运行保护，不回写state或DB。
def task_scratch_unlocked(home, task_root, held, now):
    if task_root.name in held or not _runtime_unlocked(home, now):
        return False
    path = task_root / "work/state.json"
    if path.exists():
        row = _read_state(path, [], code="MEMORY_RETENTION_TASK_STATE_INVALID")
        if row is None or row.get("task_id") in held:
            return False
        if str(row.get("status") or "").upper() not in TASK_TERMINAL_STATUSES:
            return False
    # 只检查已有锁，不创建lock文件；跨平台无只读锁探针时保守保留。
    for lock in (*task_root.glob("*.lock"), *(task_root / "work").glob("*.lock"), *(task_root / "tmp").glob("*.lock")):
        try:
            import fcntl
            with _fingerprint_source(lock) as source:
                fcntl.flock(source.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(source.fileno(), fcntl.LOCK_UN)
        except (ImportError, OSError):
            return False
    return True


# LLM: 只允许规范任务临时目录；复用同次规划pin，所属任务或子证据命中即保护整棵，坏源仍关闭。
# 函数用途: 规划三日闲置任务临时文件和空目录回收。
def _task_tmp_actions(context, excluded, protection):
    days = context.policy.task_tmp_idle_days
    if days <= 0:
        return []
    cutoff = context.now.timestamp() - days * _DAY_SECONDS
    actions = []
    pins, complete = protection
    if not complete and not pins.complete_for(Path(context.home.owner_tasks_dir), context.errors):
        return []
    for root in _fixed_depth_paths(Path(context.home.owner_tasks_dir), "tmp", context.errors):
        if len(root.relative_to(Path(context.home.owner_tasks_dir)).parts) != 3 or pins.protects(root, context.errors):
            continue
        if (any(part.is_symlink() for part in (root, root.parent, root.parent.parent))
                or _inside_any(root, excluded)
                or not task_scratch_unlocked(context.home, root.parent, context.held_task_ids, context.now)):
            continue
        try:
            digest, members = scratch_inventory(root, cutoff)
            state_path = root.parent / "work/state.json"
            state = _read_state(state_path, context.errors, code="MEMORY_RETENTION_TASK_STATE_INVALID") if state_path.exists() else {}
            if state is None or scratch_tree_pinned(pins, root, members, context.errors, task_root=root.parent, state=state):
                continue
        except OSError:
            continue
        if members:
            actions.append(_action(category="task_tmp", operation="scratch_clear", path=root,
                reason=f"idle_scratch_older_than_{days}_days", proof=_ActionProof(
                    source_fingerprint=digest, cutoff_timestamp=cutoff)))
    return actions


# LLM: runs仅终态且共享pin完整才规划；所属运行/任务ID或任何子证据引用都保护整棵scratch。
# 函数用途: 将旧RUNNING档案的tmp/中间工具产物接入原留存计划。
def _run_scratch_actions(context, excluded, protection):
    days = context.policy.tmp_days
    if days <= 0:
        return []
    cutoff = context.now.timestamp() - days * _DAY_SECONDS
    actions = []
    runs = Path(context.home.owner_home_dir) / "runs"
    pins, complete = protection
    if not complete and not pins.complete_for(runs, context.errors):
        return []
    for authority in _fixed_depth_paths(runs, "work/state.json", context.errors):
        root = authority.parent.parent
        if _inside_any(root, excluded):
            continue
        row = _read_state(authority, context.errors, code="MEMORY_RETENTION_TASK_STATE_INVALID")
        if row is None or row.get("task_id") in context.held_task_ids:
            continue
        status = str(row.get("status") or "").upper()
        terminal = ((status, _timestamp(row.get("updated_at"))) if status in TASK_TERMINAL_STATUSES
                    else runtime_terminal(context.home, root, row, context.now) if status == "RUNNING" else None)
        if terminal is None or terminal[1] is None or terminal[1] >= cutoff or not _runtime_unlocked(context.home, context.now):
            continue
        for scratch in (root / "tmp", root / "work/tmp", root / "work/blobs/tool_outputs"):
            if pins.protects(scratch, context.errors):
                continue
            if not scratch.exists() or scratch.is_symlink():
                continue
            try:
                digest, members = scratch_inventory(scratch, cutoff)
                if scratch_tree_pinned(pins, scratch, members, context.errors, task_root=root, state=row):
                    continue
            except OSError:
                continue
            if members:
                actions.append(_action(category="run_scratch", operation="scratch_clear", path=scratch,
                    reason=f"terminal_run_scratch_older_than_{days}_days", proof=_ActionProof(
                        authority_path=authority, authority_fingerprint=file_fingerprint(authority),
                        authority_status=terminal[0], record_id=str(row.get("run_id") or root.name),
                        source_fingerprint=digest, cutoff_timestamp=cutoff)))
    return actions


# LLM: 扫描与执行共用同一判据；只认结构化所属ID及原pins保护，不解析正文猜引用，调用者须先核完整性。
# 函数用途: 所属任务或任一子成员受保护时保留整棵scratch及其未引用兄弟文件。
def scratch_tree_pinned(pins, root, members, errors, *, task_root, state):
    identities = {task_root.name, str(state.get("task_id") or ""), str(state.get("run_id") or "")}
    identities.discard("")
    return bool(identities.intersection(pins.ids) or pins.protects(root, errors)
                or any(pins.protects(root / relative, errors) for relative, _identity in members))


# LLM: 只枚举规范根下state，显式发现错误交调用方记录或上抛，不能把不可读当缺失。
# 函数用途: 逐个产出恢复根与其下的任务状态文件路径。
def _iter_task_states(home: object, errors=None):
    owner_home = Path(home.owner_home_dir)
    for root in _recovery_roots(home):
        for state_path in _fixed_depth_paths(root, "work/state.json", errors):
            task_root = state_path.parent.parent
            # 路径必须正好落在 workspace_paths 认定的规范深度上；深一层或浅一层都不算任务根。
            if canonical_task_root(owner_home, task_root) is None:
                continue
            yield root, state_path


# LLM: 完成任务必须有明确 terminal status 和 updated_at；未知/运行中状态永远保护整个恢复目录。
# 函数用途: 规划超过 completed_task_days 的终态任务移入回收站（runs/tasks/audits 三个规范根一视同仁）。
def _task_actions(
    *,
    home: object,
    policy: MemoryRetentionPolicy,
    held_task_ids: frozenset[str],
    now: datetime,
    errors: list[MemoryRetentionError],
) -> tuple[list[MemoryRetentionAction], frozenset[Path]]:
    if policy.completed_task_days <= 0:
        return [], frozenset()
    cutoff = now.timestamp() - policy.completed_task_days * _DAY_SECONDS
    actions: list[MemoryRetentionAction] = []
    selected: set[Path] = set()
    for root, state_path in _iter_task_states(home, errors):
        task_root = state_path.parent.parent
        state = _read_state(state_path, errors, code="MEMORY_RETENTION_TASK_STATE_INVALID")
        if state is None or not _safe_child(task_root, root) or task_root.is_symlink():
            continue
        task_id = str(state.get("task_id") or "").strip()
        status = str(state.get("status") or "").strip().upper()
        if not task_id or not status:
            errors.append(_state_error("MEMORY_RETENTION_TASK_STATE_INVALID", state_path))
            continue
        if task_id in held_task_ids or status not in TASK_TERMINAL_STATUSES:
            continue
        updated = _timestamp(state.get("updated_at"))
        if updated is None:
            errors.append(_state_error("MEMORY_RETENTION_TASK_TIMESTAMP_INVALID", state_path))
            continue
        if updated >= cutoff:
            continue
        destination = _trash_destination(
            home,
            category="completed_task",
            path=task_root,
            now=now,
        )
        actions.append(
            _action(
                category="completed_task",
                operation="trash_tree",
                path=task_root,
                reason=f"terminal_{status}_older_than_{policy.completed_task_days}_days",
                destination=destination,
                proof=_ActionProof(
                    authority_path=state_path,
                    authority_status=status,
                    authority_fingerprint=file_fingerprint(state_path),
                    source_fingerprint=tree_fingerprint(task_root),
                    record_id=task_id,
                    cutoff_timestamp=cutoff,
                ),
            )
        )
        selected.add(task_root.resolve(strict=False))
    return actions, frozenset(selected)


# LLM: 工具正文只有在父任务结构化终态且终态时间超过 cutoff 后可清；运行中任务一律保留恢复材料。
# 函数用途: 规划恢复根下 tool_outputs 大正文目录移入回收站（含旧 tasks 与新版 runs 两个根）。
def _tool_output_actions(
    *,
    home: object,
    policy: MemoryRetentionPolicy,
    held_task_ids: frozenset[str],
    selected_task_roots: frozenset[Path],
    now: datetime,
    errors: list[MemoryRetentionError],
) -> list[MemoryRetentionAction]:
    if policy.tool_output_days_after_terminal <= 0:
        return []
    cutoff = now.timestamp() - policy.tool_output_days_after_terminal * _DAY_SECONDS
    actions: list[MemoryRetentionAction] = []
    # 本片只扩 completed_task；tool output 侧维持原范围（仅旧 O/tasks），不随恢复根一起扩。
    root = Path(home.owner_tasks_dir)
    owner_home = Path(home.owner_home_dir)
    for state_path in _fixed_depth_paths(root, "work/state.json", errors):
        task_root = state_path.parent.parent
        # 只收窄：任务根必须落在规范深度上。运行中任务的 output 里嵌套一份「像任务」的目录
        # （带终态 state.json）时，它下面的 blobs/tool_outputs 不能被当成任务根移走（dev 2026-09-29）。
        if canonical_task_root(owner_home, task_root) is None:
            continue
        if task_root.resolve(strict=False) in selected_task_roots:
            continue
        state = _read_state(state_path, errors, code="MEMORY_RETENTION_TASK_STATE_INVALID")
        if state is None:
            continue
        task_id = str(state.get("task_id") or "").strip()
        status = str(state.get("status") or "").strip().upper()
        updated = _timestamp(state.get("updated_at"))
        if (
            not task_id
            or task_id in held_task_ids
            or status not in TASK_TERMINAL_STATUSES
            or updated is None
            or updated >= cutoff
        ):
            continue
        for output_dir in _task_tool_output_dirs(task_root):
            actions.append(
                _action(
                    category="tool_output",
                    operation="trash_tree",
                    path=output_dir,
                    reason=(
                        f"terminal_{status}_tool_output_older_than_"
                        f"{policy.tool_output_days_after_terminal}_days"
                    ),
                    destination=_trash_destination(
                        home,
                        category="tool_output",
                        path=output_dir,
                        now=now,
                    ),
                    proof=_ActionProof(
                        authority_path=state_path,
                        authority_status=status,
                        authority_fingerprint=file_fingerprint(state_path),
                        source_fingerprint=tree_fingerprint(output_dir),
                        record_id=task_id,
                        cutoff_timestamp=cutoff,
                    ),
                )
            )
    return actions


# LLM: 旧 subagent scratch 清理继续使用同一 v2 plan/apply；只有子代理自身 terminal state 能授权删除。
# 函数用途: 规划终态子代理的 inbox/outbox/compactions/tool_outputs 临时目录。
def _subagent_scratch_actions(
    *,
    home: object,
    policy: MemoryRetentionPolicy,
    held_task_ids: frozenset[str],
    selected_task_roots: frozenset[Path],
    now: datetime,
    errors: list[MemoryRetentionError],
) -> list[MemoryRetentionAction]:
    if policy.subagent_scratch_days <= 0:
        return []
    task_root_dir = Path(home.owner_tasks_dir)
    owner_home = Path(home.owner_home_dir)
    cutoff = now.timestamp() - policy.subagent_scratch_days * _DAY_SECONDS
    actions: list[MemoryRetentionAction] = []
    for agents_dir in _fixed_depth_paths(task_root_dir, "work/agents", errors):
        task_root = agents_dir.parent.parent
        # 只收窄：同上，嵌套的「像任务」目录不算任务根，它下面的子代理 inbox 不能被动。
        if canonical_task_root(owner_home, task_root) is None:
            continue
        if task_root.resolve(strict=False) in selected_task_roots:
            continue
        task_state = _read_state(
            task_root / "work" / "state.json",
            errors,
            code="MEMORY_RETENTION_TASK_STATE_INVALID",
        )
        if task_state is None or str(task_state.get("task_id") or "") in held_task_ids:
            continue
        for run_root in sorted(path for path in agents_dir.iterdir() if path.is_dir()):
            authority = _subagent_state_path(run_root)
            if authority is None:
                continue
            state = _read_state(
                authority,
                errors,
                code="MEMORY_RETENTION_SUBAGENT_STATE_INVALID",
            )
            if state is None:
                continue
            status = str(state.get("status") or "").strip().upper()
            updated = _timestamp(state.get("updated_at"))
            if status not in TASK_TERMINAL_STATUSES or updated is None or updated >= cutoff:
                continue
            for relative in _SUBAGENT_SCRATCH_PATHS:
                scratch = run_root / relative
                if not scratch.exists() or scratch.is_symlink():
                    continue
                actions.append(
                    _action(
                        category="subagent_scratch",
                        operation="trash_tree",
                        path=scratch,
                        reason=(
                            f"terminal_{status}_scratch_older_than_"
                            f"{policy.subagent_scratch_days}_days"
                        ),
                        destination=_trash_destination(
                            home,
                            category="subagent_scratch",
                            path=scratch,
                            now=now,
                        ),
                        proof=_ActionProof(
                            authority_path=authority,
                            authority_status=status,
                            authority_fingerprint=file_fingerprint(authority),
                            source_fingerprint=tree_fingerprint(scratch),
                            cutoff_timestamp=cutoff,
                        ),
                    )
                )
    return actions


# LLM: 原文按thread整体处理；绑定、索引未知及活跃/法律保留均保护，子代理快路须精确投影正面证明。
# 函数用途: 规划超过 conversation_days 的完整会话及其 thread-scoped 附属文件移入回收站。
def _conversation_actions(
    *,
    home: object,
    policy: MemoryRetentionPolicy,
    held_task_ids: frozenset[str],
    roots: tuple[Path, ...],
    now: datetime,
    errors: list[MemoryRetentionError],
    context: _RetentionScanContext | None = None,
) -> list[MemoryRetentionAction]:
    if policy.conversation_days <= 0:
        return []
    cutoff = now.timestamp() - policy.conversation_days * _DAY_SECONDS
    actions: list[MemoryRetentionAction] = []
    for root in roots:
        bound = _bound_thread_ids(root, errors)
        if bound is None:
            if context is not None and context.scan_budget is not None:
                context.scan_partial = True
                context.scan_cursor[_scan_cursor_key(home, "conversation", root)] = {"after": "", "retry": True}
            continue
        paths = (sorted((root / "threads").glob("*.json")) if context is None or context.scan_budget is None else
                 _budgeted_scan_paths(context, root / "threads", _scan_cursor_key(home, "conversation", root)))
        for thread_path in paths:
            report = read_json_object_report(
                thread_path,
                context="memory_retention.conversation_thread",
            )
            if report.load_error is not None:
                errors.append(
                    MemoryRetentionError(
                        "MEMORY_RETENTION_CONVERSATION_STATE_INVALID",
                        str(thread_path),
                        str(report.load_error.get("error_type") or "unreadable thread"),
                    )
                )
                continue
            try:
                thread = ConversationThread.from_dict(report.payload)
            except (TypeError, ValueError) as exc:
                errors.append(
                    MemoryRetentionError(
                        "MEMORY_RETENTION_CONVERSATION_STATE_INVALID",
                        str(thread_path),
                        f"{type(exc).__name__}: {exc}",
                    )
                )
                continue
            if thread.thread_id != thread_path.stem or not thread.thread_id:
                errors.append(
                    MemoryRetentionError(
                        "MEMORY_RETENTION_CONVERSATION_ID_MISMATCH",
                        str(thread_path),
                        "thread_id does not match authority filename",
                    )
                )
                continue
            from ..conversation.task_history import contains
            if thread.active_task_ids or any(contains(thread_path, thread, identifier) for identifier in held_task_ids):
                continue
            if thread.thread_id in bound:
                continue
            if thread.updated_at <= 0:
                errors.append(
                    _state_error("MEMORY_RETENTION_CONVERSATION_TIMESTAMP_INVALID", thread_path)
                )
                continue
            action_cutoff = cutoff
            reason = f"inactive_conversation_older_than_{policy.conversation_days}_days"
            sub_cutoff = now.timestamp() - policy.subagent_thread_days * _DAY_SECONDS
            if (policy.subagent_thread_days > 0 and thread.thread_id.startswith("thread-subagent-")
                    and thread.updated_at < sub_cutoff
                    and _subagent_run_settled(home, thread.thread_id[len("thread-"):], sub_cutoff)):
                action_cutoff = sub_cutoff
                reason = f"terminal_subagent_thread_older_than_{policy.subagent_thread_days}_days"
            if thread.updated_at >= action_cutoff:
                continue
            if thread.metadata.get("thread_kind") == "scheduled":
                from ..conversation.thread_read_index import scheduled_thread_terminal_proof

                proof = scheduled_thread_terminal_proof(home, thread, action_cutoff,
                    scan_budget=context.scan_budget if context is not None else None)
                if proof.get("status") != "settled":
                    continue
            related, related_errors = _conversation_related_paths(root, thread)
            errors.extend(related_errors)
            if related_errors:
                continue
            protect, curator_cursor, curator_enabled = ((False, None, None) if context is None
                                                        else _uncurated_thread_check(context, root, thread))
            if protect:
                continue
            actions.append(
                _action(
                    category="conversation",
                    operation="conversation_trash",
                    path=thread_path,
                    reason=reason,
                    destination=_trash_destination(
                        home,
                        category="conversation",
                        path=thread_path,
                        now=now,
                    ),
                    proof=_ActionProof(
                        authority_path=thread_path,
                        authority_status=thread.status,
                        authority_fingerprint=file_fingerprint(thread_path),
                        source_fingerprint=paths_fingerprint(related),
                        record_id=thread.thread_id,
                        cutoff_timestamp=action_cutoff,
                        related_paths=related,
                        curator_cursor=curator_cursor,
                        curator_enabled=curator_enabled,
                    ),
                )
            )
    return actions


# LLM: 整理状态是原子替换写入的单个 JSON，这里无锁只读（不取整理状态锁，执行端在消息锁内复核时也安全）；
#   缺文件是空表（从未整理），坏文件或坏结构返回 None，调用方按“未确认”处理，不能当作已整理。
# 函数用途: 读出后台整理的逐会话游标表（会话 ID → 不透明消息 ID）。
def curator_thread_cursors(home: object) -> dict[str, str] | None:
    path = getattr(home, "owner_memory_curator_state_json", None)
    if path is None:
        return None
    report = read_json_object_report(Path(path), context="memory_retention.curator_state")
    if report.load_error is not None:
        return None
    try:
        state = MemoryCuratorState.from_dict(report.payload)
    except (TypeError, ValueError):
        return None
    return dict(state.per_thread_cursors)


# LLM: 只看结构化开关：owner 记忆总闸、memory_curator_enabled 与生效的后台辅助模型档案（空=关闭）；冷却与档案暂时
#   不可用仍算开启（整理稍后会继续）。配置已判定关闭时直接返回 False、不读策略文件（config 为空是不带配置的旧构造，
#   按没有设置后台辅助模型处理）；配置为开而记忆策略文件在但读不出时返回 None（未知）：规划按开启保护，执行端放弃移动。
#   规划与执行前复核共用这一判定。
# 函数用途: 读出当前生效的后台记忆提取开关，读不出时返回 None。
def curator_extraction_enabled(home: object, config: object) -> bool | None:
    from ..settings.curator_profile import effective_auxiliary_model_profile
    from ..user_space.owner_policy import effective_memory_enabled

    if not (bool(getattr(config, "memory_curator_enabled", True)) and effective_auxiliary_model_profile(config)):
        return False
    policy_path = getattr(home, "owner_memory_policy_json", None)
    report = (None if policy_path is None else
              read_json_object_report(Path(policy_path), context="memory_retention.memory_policy"))
    if report is not None and report.load_error is not None:
        return None
    return effective_memory_enabled({} if report is None else report.payload)


# LLM: 排除名单读不出时不当作排除，交探查结果决定。
# 函数用途: 读出本轮判断过期会话是否已被后台整理读完所需的材料，同一轮只读一次。
def _curator_retention_facts(context: _RetentionScanContext) -> tuple:
    if context.curator_facts is None:
        from .learning_exclusions import curator_learning_exclusions

        home = context.home
        enabled = curator_extraction_enabled(home, context.config)
        try:
            excluded, unreadable = curator_learning_exclusions(home)
        except (AttributeError, OSError, TypeError, ValueError):
            excluded, unreadable = frozenset(), True
        context.curator_facts = (enabled, curator_thread_cursors(home), frozenset() if unreadable else excluded)
    return context.curator_facts


# LLM: 后台提取开启（或开关读不出）时，整理游标之后还有可学习消息、或读不完/读不出的过期会话都保护（本轮不回收）；
#   关闭时照常回收，只计入 uncurated_threads。宿主标记不学习的会话不需要整理。已读完时把规划所见游标绑进动作，
#   执行端游标变了就放弃；规划时的开关也绑进动作，执行端由关变开或读不出同样放弃。
# 函数用途: 判断一个已满足其它回收条件的过期会话是否要因后台整理未读完而保护，并返回要绑定的整理游标与提取开关。
def _uncurated_thread_check(context: _RetentionScanContext, root: Path,
                            thread: ConversationThread) -> tuple[bool, str | None, bool | None]:
    from .curator_inputs import _thread_learning_excluded
    from .learning_exclusions import thread_is_excluded

    enabled, cursors, excluded = _curator_retention_facts(context)
    if _thread_learning_excluded(thread) or thread_is_excluded(thread, excluded):
        return False, None, None
    enabled = enabled is not False
    cursor = None if cursors is None else cursors.get(thread.thread_id, "")
    verdict = "unknown" if cursor is None else _probe_uncurated_messages(context, root, thread.thread_id, cursor)
    if verdict == "curated":
        return False, (cursor if enabled else None), enabled
    context.uncurated_threads += 1
    return enabled, None, enabled


# LLM: 用与后台整理同一个有界续读口 after_slice_report，从整理游标（不透明消息 ID，只作续读起点，不比大小）往后读，
#   共用本轮扫描预算；第一条未标 learning_excluded 的消息即“未整理”，读到真实末尾才算“已整理”，预算用尽、
#   读错或页数用尽都是“未确认”。
# 函数用途: 探查一个会话在整理游标之后是否还有可学习消息。
def _probe_uncurated_messages(context: _RetentionScanContext, root: Path, thread_id: str, cursor: str) -> str:
    store = context.conversation_stores.get(root)
    if store is None:
        from ..conversation.store import ConversationStore

        store = context.conversation_stores[root] = ConversationStore(root, initialize=False)
    budget = context.scan_budget if context.scan_budget is not None else RetentionScanBudget.default(context.config)
    after = cursor
    for _page_number in range(_UNCURATED_PROBE_MAX_PAGES_COUNT):
        outcome = store.messages.after_slice_report(
            thread_id, after_message_id=after, limit=_UNCURATED_PROBE_PAGE_ROWS, budget=budget)
        if outcome.load_errors:
            return "unknown"
        rows = tuple(outcome.rows)
        if any(not (isinstance(row.metadata, dict) and row.metadata.get("learning_excluded") is True) for row in rows):
            return "uncurated"
        if not rows:
            return "unknown" if outcome.partial else "curated"
        if not outcome.has_more and len(rows) < _UNCURATED_PROBE_PAGE_ROWS:
            return "curated"
        after = str(rows[-1].message_id)
    return "unknown"


# LLM: 精确owner内投影用nofollow读取复核原报告器；缺失、坏解析/时间、链接及交付欠账不能授予快路。
# 函数用途: 确认子代理投影已经终态、交付收口且业务时间早于截止点。
def _subagent_run_settled(home: object, run_id: str, cutoff: float) -> bool:
    if not run_id.startswith("subagent-") or run_id == "subagent-" or Path(run_id).name != run_id:
        return False
    path = Path(home.owner_home_dir) / "agents" / run_id / "state.json"
    parts = ("agents", run_id, "state.json")
    try:
        raw = read_bytes_beneath(home.owner_home_dir, parts, max_bytes=RETENTION_SCAN_SLICE_BYTES, require_dir_fd=True)
        if raw is None:
            return False
        report = read_json_object_report(path, context="memory_retention.subagent_thread_projection")
        if raw != read_bytes_beneath(home.owner_home_dir, parts, max_bytes=RETENTION_SCAN_SLICE_BYTES, require_dir_fd=True):
            return False
        row = json.loads(raw)
        if row != report.payload:
            return False
    except (OSError, ValueError, UnicodeError, RecursionError):
        return False
    return bool(report.load_error is None and row.get("run_id", run_id) == run_id
                and derived_terminal_expired(row, cutoff)
                and not any(row.get(key) for key in ("delivery_pending", "consume_pending", "callback_pending")))


# LLM: 两个共享路由只读一次；缺文件是空表，任何读取错误保护整个会话根并报告原因。
# 函数用途: 收集仍被路由或用户最近会话索引绑定的线程。
def _bound_thread_ids(root: Path, errors: list[MemoryRetentionError]) -> set[str] | None:
    bound: set[str] = set()
    unreadable = False
    for name in ("channel_bindings.json", "user_latest_threads.json"):
        path = root / name
        report = read_json_object_report(path, context="memory_retention.binding_index")
        if report.load_error is not None:
            errors.append(MemoryRetentionError("MEMORY_RETENTION_BINDING_INDEX_UNREADABLE", str(path),
                str(report.load_error.get("error_type") or "unreadable binding index")))
            unreadable = True
        else:
            bound.update(value for value in report.payload.values() if isinstance(value, str))
    return None if unreadable else bound


# LLM: thread-scoped 文件通过结构化 ID 组成；message JSONL 坏行会阻断整个会话删除。
#   转写本按 canonical 布局整体收集：热文件、分卷清单、prepared 暂存段、archive/<thread>/ 下全部冷段与索引节点；
#   清单或冷目录不可解析时整会话保守保留并报结构化错误，不能只搬热文件把冷集合留成无人认领的孤儿。
# LLM: observation/usage 按原 archive_dir_for 精确收集热账与完整冷家族，usage 另含 SQLite 四文件；
#   链接、特殊文件或不可读成员保护整会话，锁侧车不移动，ledger 不套用转写本的冷段序号校验。
# 函数用途: 收集一个会话的完整原文、任务、usage与唤醒家族，不移动Compact树。
def _conversation_related_paths(
    root: Path,
    thread: ConversationThread,
) -> tuple[tuple[Path, ...], list[MemoryRetentionError]]:
    from ..conversation.task_history import iter_all, member_paths

    thread_id = thread.thread_id
    errors: list[MemoryRetentionError] = []
    candidates = [
        root / "threads" / f"{thread_id}.json",
        *_transcript_member_paths(root / "messages", thread_id, errors),
        *_conversation_ledger_member_paths(_conversation_ledger_active_paths(root, thread_id)[0], errors,
                                          "MEMORY_RETENTION_CONVERSATION_OBSERVATIONS_UNREADABLE"),
        root / "goals" / f"{thread_id}.json",
        root / "background_claims" / f"{thread_id}.json",
        root / "guidance" / f"thread.{thread_id}.jsonl",
        *_conversation_usage_paths(root, thread_id, errors),
        *(root / "tasks" / f"{task_id}.json" for task_id in iter_all(root / "threads" / f"{thread_id}.json", thread)),
        *member_paths(root / "threads" / f"{thread_id}.json"),
        *(root / "wake_queue" / "dedupe").glob(f"{thread_id}.*.json"),
    ]
    messages = root / "messages" / f"{thread_id}.jsonl"
    if messages.exists() and not errors:
        report = read_jsonl_objects_report(
            messages,
            context="memory_retention.conversation_messages",
        )
        if report.load_errors or any(
            str(row.get("thread_id") or "") != thread_id for row in report.records
        ):
            errors.append(
                MemoryRetentionError(
                    "MEMORY_RETENTION_CONVERSATION_MESSAGES_INVALID",
                    str(messages),
                    "conversation transcript contains unreadable or cross-thread rows",
                )
            )
    candidates.extend(_thread_wake_files(root / "wake_queue", thread_id, errors))
    paths = tuple(
        sorted(
            {
                path.resolve(strict=False)
                for path in candidates
                if path.exists() and path.is_file() and not path.is_symlink()
            },
            key=str,
        )
    )
    return paths, errors


# LLM: usage路径复用原安全文件名；父目录及叶必须真实目录/普通文件，异常不当作缺失，不跟随链接。
# 函数用途: 返回线程模型用量热/冷家族与SQLite主库、WAL、SHM、journal的精确集合。
def _conversation_usage_paths(root: Path, thread_id: str, errors: list[MemoryRetentionError]) -> list[Path]:
    directory = root / "model_usage"
    paths: list[Path] = []
    try:
        if not stat.S_ISDIR(directory.lstat().st_mode):
            raise OSError("model_usage is not a real directory")
    except FileNotFoundError:
        return paths
    except OSError:
        errors.append(_state_error("MEMORY_RETENTION_CONVERSATION_USAGE_UNREADABLE", directory))
        return paths
    stem = safe_file_stem(thread_id)
    paths.extend(_conversation_ledger_member_paths(_conversation_ledger_active_paths(root, thread_id)[1], errors,
                                                 "MEMORY_RETENTION_CONVERSATION_USAGE_UNREADABLE"))
    candidates = (directory / f".{stem}-usage.sqlite3{suffix}" for suffix in ("", "-wal", "-shm", "-journal"))
    for path in candidates:
        try:
            if not stat.S_ISREG(path.lstat().st_mode):
                raise OSError("usage member is not a regular file")
            paths.append(path)
        except FileNotFoundError:
            continue
        except OSError:
            errors.append(_state_error("MEMORY_RETENTION_CONVERSATION_USAGE_UNREADABLE", path))
    return paths


def _conversation_ledger_active_paths(root: Path, thread_id: str) -> tuple[Path, Path]:
    """与原观察和用量写者保持相同热文件映射，供收集器与原写锁组合共用。"""
    return (root / "observations" / f"{thread_id}.jsonl",
            root / "model_usage" / f"{safe_file_stem(thread_id)}.jsonl")


def _conversation_ledger_member_paths(active: Path, errors: list[MemoryRetentionError], code: str) -> list[Path]:
    """只枚举本线程的普通文件；ledger 段名不套用 transcript 的序号连续性规则。"""
    members = []
    try:
        if not stat.S_ISDIR(active.parent.lstat().st_mode):
            raise OSError("ledger parent is not a real directory")
        if _regular_member_exists(active):
            members.append(active)
        archived, _unused_transcript_sequences = _transcript_archive_files(archive_dir_for(active))
        members.extend(archived)
        for path in members:
            if path.lstat().st_nlink != 1:
                raise OSError("ledger member is not a single regular file")
    except FileNotFoundError:
        # 缺整个家族目录是空集合；枚举中消失的成员由计划/执行指纹重验继续保守处理。
        if active.parent.exists():
            errors.append(_state_error(code, active))
    except OSError:
        errors.append(_state_error(code, active))
    return members


# LLM: 只做 lstat/目录枚举，不跟随链接、不读冷段正文；清单仍经 message_scan 原选择器解析（prepared 按已提交前缀选唯一代次）。
#   选不出唯一代次、清单引用的冷段缺失、冷段序号有空洞、出现链接或特殊文件，一律记结构化错误由调用方整会话保留。
#   prepared 暂存段名由序号决定（.<thread>.jsonl.rotation-<n>.stage，n 不超过已写出的最大冷段序号），逐个精确 lstat，
#   不扫描整个共享 messages 目录；*.lock 侧车永远不进入搬移集合。
# 函数用途: 返回一个会话 canonical 转写本的完整常规文件集合：热文件、清单、暂存段、archive/<thread>/ 下全部冷段与索引节点。
def _transcript_member_paths(messages_dir: Path, thread_id: str, errors: list[MemoryRetentionError]) -> list[Path]:
    from ..conversation.message_scan import transcript_manifest
    from ..runtime_errors import DataCorruptionError

    hot = messages_dir / f"{thread_id}.jsonl"
    members = [hot, messages_dir / f"{thread_id}.manifest.json"]
    try:
        for path in members:
            _regular_member_exists(path)
        manifest = transcript_manifest(hot)
        archive, sequences = _transcript_archive_files(messages_dir / "archive" / thread_id)
        highest = max(sequences, default=0)
        if sorted(sequences) != list(range(1, highest + 1)):
            raise DataCorruptionError("transcript cold segment sequence has gaps")
        if manifest is not None and manifest["segment_count"] > highest:
            raise DataCorruptionError("transcript manifest references missing cold segments")
        for sequence in range(1, highest + 1):
            stage = messages_dir / f".{thread_id}.jsonl.rotation-{sequence}.stage"
            if _regular_member_exists(stage):
                members.append(stage)
    except (DataCorruptionError, OSError, ValueError, KeyError, TypeError) as exc:
        errors.append(
            MemoryRetentionError(
                "MEMORY_RETENTION_CONVERSATION_TRANSCRIPT_INVALID",
                str(hot),
                f"conversation transcript layout is invalid or incomplete: {type(exc).__name__}",
            )
        )
        return members
    return [*members, *archive]


# LLM: 缺失返回 False；链接、目录或特殊文件抛错，调用方据此保守保留，不把它们当作"不存在"静默跳过。
# 函数用途: 判断一个转写本成员是否为存在的普通文件。
def _regular_member_exists(path: Path) -> bool:
    try:
        info = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(info.st_mode):
        raise OSError("conversation transcript member is not a regular file")
    return True


# LLM: archive 与本线程冷目录都须是真实目录；遍历错误显式传播，链接/特殊文件抛错，*.lock 侧车跳过不搬。
#   冷段序号只认 seg-<20 位 ASCII 数字>.jsonl 直接子项；索引节点、原子写残留等同目录普通文件一并收集。
# 函数用途: 枚举一个线程冷归档目录下的全部普通文件，并返回已写出的冷段序号。
def _transcript_archive_files(archive: Path) -> tuple[list[Path], list[int]]:
    for directory in (archive.parent, archive):
        try:
            info = directory.stat(follow_symlinks=False)
        except FileNotFoundError:
            return [], []
        if not stat.S_ISDIR(info.st_mode):
            raise OSError("conversation transcript archive is not a regular directory")
    files: list[Path] = []
    sequences: list[int] = []

    # LLM: 遍历器回调不容忍权限等错误，防止静默漏掉冷段。
    # 函数用途: 将目录遍历错误传回扫描调用方。
    def failed(error):
        raise error

    for directory, dirs, names in os.walk(archive, followlinks=False, onerror=failed):
        base = Path(directory)
        for name in sorted((*dirs, *names)):
            info = (base / name).stat(follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                raise OSError("conversation transcript archive contains a symbolic link")
            if stat.S_ISDIR(info.st_mode) or name.endswith(".lock"):
                continue
            if not stat.S_ISREG(info.st_mode):
                raise OSError("conversation transcript archive member is not a regular file")
            files.append(base / name)
            digits = name[len("seg-"):-len(".jsonl")] if name.startswith("seg-") and name.endswith(".jsonl") else ""
            if base == archive and len(digits) == 20 and digits.isascii() and digits.isdigit():
                sequences.append(int(digits))
    return files, sequences


# LLM: 只按每个 JSON 顶层 thread_id 判定归属；读不出的文件记 MEMORY_RETENTION_CONVERSATION_WAKE_INVALID（写入 errors），
#   不猜归属。目录：待处理的 urgent/normal、毒丸尝试账 attempts、结案 quarantine、quarantine/replayed/<id>/ 留档和
#   quarantine/archive/ 顶层的已归档结案记录（带 thread_id）。archive/ledger、archive/unreadable 与 quarantine/ledger、
#   quarantine/unreadable 一样无法归属会话，不随会话删除，由运维清理。
# 函数用途: 收集一个会话在唤醒队列下的全部文件，供会话删除一起清理；只读文件，不删除。
def _thread_wake_files(wake_root: Path, thread_id: str, errors: list[MemoryRetentionError]) -> list[Path]:
    replayed = wake_root / "quarantine" / "replayed"
    directories = (wake_root / "urgent", wake_root / "normal", wake_root / "attempts", wake_root / "quarantine",
                   wake_root / "quarantine" / "archive",
                   *sorted(path for path in replayed.glob("*") if path.is_dir()))
    paths = [item for directory in directories if directory.exists() for item in directory.glob("*.json")]
    return [path for path in paths if _wake_file_thread(path, errors) == thread_id]


# LLM: 读不出的文件把结构化错误追加进 errors 并返回空串（空串不会等于任何真实 thread_id）。
# 函数用途: 读出一个唤醒相关文件顶层记录的 thread_id。
def _wake_file_thread(path: Path, errors: list[MemoryRetentionError]) -> str:
    report = read_json_object_report(path, context="memory_retention.conversation_wake")
    if report.load_error is None:
        return str(report.payload.get("thread_id") or "")
    errors.append(MemoryRetentionError("MEMORY_RETENTION_CONVERSATION_WAKE_INVALID", str(path), "wake state is unreadable"))
    return ""


# LLM: 单次lstat判类型和mtime；包括链接检查阶段的IO失败均记诊断，不因Python版本把错误吞为空文件。
# 函数用途: 规划过期文件，必要时使用原可恢复回收站。
def _expired_file_actions(
    *,
    category: str,
    root: Path,
    days: int,
    pattern: str,
    now: datetime,
    excluded_roots: frozenset[Path],
    errors: list[MemoryRetentionError],
    protection: tuple[_RetentionPins, object] | None = None,
) -> list[MemoryRetentionAction]:
    if days <= 0 or not root.exists():
        return []
    cutoff = now.timestamp() - days * _DAY_SECONDS
    actions: list[MemoryRetentionAction] = []
    for path in sorted(root.rglob(pattern)):
        try:
            info = path.stat(follow_symlinks=False)
        except FileNotFoundError:
            continue
        except OSError:
            errors.append(_state_error("MEMORY_RETENTION_FINGERPRINT_INVALID", path))
            continue
        if stat.S_ISLNK(info.st_mode):
            errors.append(
                MemoryRetentionError(
                    "MEMORY_RETENTION_SYMLINK_SKIPPED",
                    str(path),
                    "retention never follows symbolic links",
                )
            )
            continue
        if not stat.S_ISREG(info.st_mode) or _inside_any(path, excluded_roots):
            continue
        if info.st_mtime >= cutoff:
            continue
        if protection is not None and protection[0].protects(path, errors):
            continue
        trash_home = protection[1] if protection is not None and category in {"daily", "compact"} else None
        actions.append(
            _action(
                category=category,
                operation="trash_file" if trash_home is not None else "delete_file",
                path=path,
                reason=f"older_than_{days}_days",
                destination=(_trash_destination(trash_home, category=category, path=path, now=now)
                             if trash_home is not None else None),
                proof=_ActionProof(
                    source_fingerprint=file_fingerprint(path),
                    cutoff_timestamp=cutoff,
                ),
            )
        )
    return actions


# LLM: 任意类型的pending保护件或读取失败均拒清理；未确认提交不能只靠trashed墓碑取得删除权。
# 函数用途: 观察回收事务是否仍持有未完成保护，不修改或清理保护件。
def _trash_transaction_pending(container):
    try:
        (container / _TRASH_COMMIT_PENDING).lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


# LLM: purge只接受明确trashed、moved_at及已移除的pending；未知状态或未确认提交先保留，再复核引用。
# 函数用途: 规划回收站最终删除，保留宽限期内重新被引用的材料。
def _trash_actions(
    *,
    home: object,
    days: int,
    now: datetime,
    errors: list[MemoryRetentionError],
    conversation_roots: tuple[Path, ...] = (),
    protection: tuple[_RetentionPins, bool] | None = None,
) -> list[MemoryRetentionAction]:
    root = Path(home.owner_trash_dir)
    if days <= 0 or not root.exists():
        return []
    cutoff = now.timestamp() - days * _DAY_SECONDS
    actions: list[MemoryRetentionAction] = []
    retention_root = root / "retention"
    for tombstone in sorted(retention_root.glob("*/*/tombstone.json")) if retention_root.exists() else ():
        report = read_json_object_report(tombstone, context="memory_retention.trash_tombstone")
        moved = _timestamp(report.payload.get("moved_at")) if report.load_error is None else None
        if moved is None:
            errors.append(_state_error("MEMORY_RETENTION_TOMBSTONE_INVALID", tombstone))
            continue
        if report.payload.get("status") != "trashed" or _trash_transaction_pending(tombstone.parent):
            continue
        # 旧记忆与Compact回收副本仍须可恢复；最终移除留给D-173-RESTORE，不能继续按龄清除。
        if report.payload.get("category") in {"daily", "curator_run", "compact"}:
            continue
        if moved < cutoff:
            container = tombstone.parent
            if protection is None:
                protection = current_retention_pins(home, conversation_roots, errors, now=now)
            pins, complete = protection
            if (not complete and not pins.complete_for(container, errors)) or trash_payload_pinned(pins, container, errors):
                continue
            actions.append(
                _action(
                    category="trash",
                    operation="delete_tree",
                    path=container,
                    reason=f"trashed_older_than_{days}_days",
                    proof=_ActionProof(
                        authority_path=tombstone,
                        authority_fingerprint=file_fingerprint(tombstone),
                        source_fingerprint=tree_fingerprint(container),
                        cutoff_timestamp=cutoff,
                    ),
                )
            )
    return actions


# LLM: 用scandir传播发现错误；目录与缺失threads也登记pin失效源，新建会话根不能漏掉，链接仍拒绝。
# 函数用途: 严格发现标准ConversationStore根（含直连与runtime布局），保证pin发现完整性。
def _conversation_roots(home: object, explicit: tuple[Path, ...]) -> tuple[Path, ...]:
    discovered = list(explicit)
    owner_home = getattr(home, "owner_home_dir", None)
    if owner_home is not None:
        discovered.append(Path(owner_home) / "conversations")
    runtime = Path(home.owner_workspace_dir) / "runtime" / "workspaces"
    watch_pin_source(runtime)
    try:
        runtime.stat()
    except FileNotFoundError:
        pass
    else:
        if runtime.is_symlink():
            raise OSError("retention discovery root is a symlink")
        with os.scandir(runtime) as entries:
            for entry in entries:
                if entry.is_symlink():
                    raise OSError("retention discovery child is a symlink", entry.path)
                if entry.is_dir(follow_symlinks=False):
                    discovered.append(Path(entry.path) / "conversations")
    result: list[Path] = []
    for raw in discovered:
        path = Path(raw).expanduser()
        watch_pin_source(path)
        watch_pin_source(path / "threads")
        try:
            info = (path / "threads").stat(follow_symlinks=False)
        except FileNotFoundError:
            continue
        if path == Path(path.anchor) or path.is_symlink() or not stat.S_ISDIR(info.st_mode):
            raise OSError("retention conversation root is unsafe")
        result.append(path.resolve(strict=False))
    return tuple(dict.fromkeys(result))


# LLM: 证明字段把规划动作绑定到精确权威与来源快照，供执行时重新验证，不能凭动作类别猜证据。
# 类用途: 聚合 retention 动作的状态权威、指纹、记录编号、截止时间和关联路径。
@dataclass(frozen=True)
class _ActionProof:
    authority_path: Path | None = None
    authority_status: str = ""
    authority_fingerprint: str = ""
    source_fingerprint: str = ""
    record_id: str = ""
    cutoff_timestamp: float | None = None
    related_paths: tuple[Path, ...] = ()
    curator_cursor: str | None = None
    curator_enabled: bool | None = None


# LLM: 动作构造器统一生成稳定 ID，防止各扫描器自行定义互相冲突的幂等语义。
# 函数用途: 将基础动作字段与可重验证证据组合成完整 retention 动作。
def _action(
    *,
    category: str,
    operation: str,
    path: Path,
    reason: str,
    destination: Path | None = None,
    proof: _ActionProof | None = None,
) -> MemoryRetentionAction:
    evidence = proof or _ActionProof()
    return MemoryRetentionAction(
        action_id=retention_action_id(
            category=category,
            operation=operation,
            path=path,
            record_id=evidence.record_id,
            cutoff_timestamp=evidence.cutoff_timestamp,
        ),
        category=category,
        operation=operation,
        path=path,
        reason=reason,
        destination=destination,
        authority_path=evidence.authority_path,
        authority_status=evidence.authority_status,
        authority_fingerprint=evidence.authority_fingerprint,
        source_fingerprint=evidence.source_fingerprint,
        record_id=evidence.record_id,
        cutoff_timestamp=evidence.cutoff_timestamp,
        related_paths=evidence.related_paths,
        curator_cursor=evidence.curator_cursor,
        curator_enabled=evidence.curator_enabled,
    )


# LLM: 状态读取不容忍坏 JSON 或空对象；调用方据此保护相应目录。
# 函数用途: 加载 task/subagent state 并记录稳定错误。
def _read_state(
    path: Path,
    errors: list[MemoryRetentionError],
    *,
    code: str,
) -> dict[str, Any] | None:
    report = read_json_object_report(path, context="memory_retention.state")
    if report.load_error is not None or not report.payload:
        errors.append(_state_error(code, path))
        return None
    return report.payload


# LLM: 任务输出目录只认规范 work/blobs 和已有 subagent artifacts 结构，不按正文或文件名猜任务状态。
# 函数用途: 找出一个任务下可按终态清理的工具大输出目录。
def _task_tool_output_dirs(task_root: Path) -> tuple[Path, ...]:
    canonical = task_root / "work" / "blobs" / "tool_outputs"
    found = [canonical] if canonical.is_dir() and not canonical.is_symlink() else []
    agents = task_root / "work" / "agents"
    if agents.exists():
        found.extend(
            path
            for path in agents.glob("*/artifacts/tool_outputs")
            if path.is_dir() and not path.is_symlink()
        )
    return tuple(sorted(dict.fromkeys(found), key=str))


# LLM: canonical_state 优先于 state，二者都不存在时不猜子代理终态。
# 函数用途: 定位子代理唯一可用状态权威。
def _subagent_state_path(run_root: Path) -> Path | None:
    for name in ("canonical_state.json", "state.json"):
        path = run_root / name
        if path.is_file() and not path.is_symlink():
            return path
    return None


# LLM: trash 目标绑定时间、类别和源路径 hash；绝不能落到 owner_trash_dir 之外。
# 函数用途: 为可恢复删除生成唯一目标目录。
def _trash_destination(home: object, *, category: str, path: Path, now: datetime) -> Path:
    digest = hashlib.sha256(str(path.resolve(strict=False)).encode("utf-8")).hexdigest()[:12]
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    return Path(home.owner_trash_dir) / "retention" / category / f"{stamp}-{path.name}-{digest}"


# LLM: 只有严格小写64hex可作删除证据；missing/unreadable及任何非字符串都拒绝。
# 函数用途: 校验指纹是否为完整SHA256摘要。
def valid_fingerprint(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


# LLM: 同一FD读前/读后身份须相同且绑定原路径；非阻塞nofollow打开防FIFO替换挂起，不漂白读错。
# 函数用途: 用64KiB块计算稳定普通文件的SHA256，分别返回missing或unreadable。
def file_fingerprint(path: Path) -> str:
    try:
        with _fingerprint_source(path) as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode):
                return "unreadable"
            digest = hashlib.sha256()
            while chunk := source.read(65536):
                digest.update(chunk)
            after = os.fstat(source.fileno())
            current = path.stat(follow_symlinks=False)
        if _file_identity(before) != _file_identity(after) or _file_identity(after) != _file_identity(current):
            return "unreadable"
        return digest.hexdigest()
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unreadable"


# LLM: 不跟随最终链接并非阻塞打开，再由调用者fstat验证普通文件；fdopen失败也关闭原FD。
# 函数用途: 为流式指纹取得安全二进制句柄，避免FIFO使维护无限阻塞。
def _fingerprint_source(path: Path):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags)
    try:
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


# LLM: ctime补强同大小改写检测；与任务要求的dev/ino/size/mtime_ns同时核对，不授予写权限。
# 函数用途: 提取文件快照身份供流式读取验证。
def _file_identity(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_mode)


# LLM: 任一来源或权威摘要无效即取消动作并记录路径级诊断，目录组合也不能漂白坏成员。
# 函数用途: 为所有扫描类别统一执行指纹准入门。
def _validated_actions(actions, errors):
    accepted = []
    for action in actions:
        checks = ([(action.path, action.source_fingerprint)] if action.operation != "candidate_delete" else [])
        if action.authority_path is not None:
            checks.append((action.authority_path, action.authority_fingerprint))
        bad = [path for path, digest in checks if not valid_fingerprint(digest)]
        if bad:
            errors.extend(_state_error("MEMORY_RETENTION_FINGERPRINT_INVALID", path) for path in bad)
        else:
            accepted.append(action)
    return accepted


# LLM: 目录枚举/任一成员缺失或不可读都返回无效标记，成员检查集中于_tree_member_material，不漂白错误。
# 函数用途: 计算目录快照，失败关闭并且不跟随链接。
def tree_fingerprint(path: Path) -> str:
    try:
        info = path.stat(follow_symlinks=False)
        if stat.S_ISREG(info.st_mode):
            return file_fingerprint(path)
        if not stat.S_ISDIR(info.st_mode):
            return "unreadable"
        material: list[str] = []
        for item in _walk_files(path, include_dirs=True):
            material.append(_tree_member_material(path, item))
        return hashlib.sha256("\n".join(sorted(material)).encode("utf-8")).hexdigest()
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unreadable"


# LLM: 仅普通目录或有效摘要普通文件可进入组合；链接/特殊文件/坏hash抛错令外层无效。
# 函数用途: 为目录指纹生成一个成员的安全材料。
def _tree_member_material(root: Path, item: Path) -> str:
    relative = item.relative_to(root)
    member = item.stat(follow_symlinks=False)
    if stat.S_ISDIR(member.st_mode):
        return f"D:{relative}"
    if stat.S_ISREG(member.st_mode):
        digest = file_fingerprint(item)
        if valid_fingerprint(digest):
            return f"F:{relative}:{digest}"
    raise OSError("retention tree member is unreadable")


# LLM: 多路径摘要仅组合真实有效文件hash；任何坏成员都使外层无效。
# 函数用途: 安全计算一组精确文件的稳定指纹。
def paths_fingerprint(paths: tuple[Path, ...]) -> str:
    material = []
    for path in sorted(paths, key=str):
        digest = file_fingerprint(path)
        if not valid_fingerprint(digest):
            return "unreadable"
        material.append(f"{path}:{digest}")
    return hashlib.sha256("\n".join(material).encode("utf-8")).hexdigest()


# LLM: os.walk错误显式传播；记录发现目录与缺失根，变化使阶段pin视图失效，不跟随链接。
# 函数用途: 严格枚举目录，供指纹和pin来源扫描复用。
def _walk_files(root: Path, *, include_dirs: bool = False, recursive: bool = True):
    watch_pin_source(root)
    try:
        info = root.stat(follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(info.st_mode):
        raise OSError("retention root is not a regular directory")
    # LLM: 遍历器回调不容忍权限等错误，防止静默漏文件。
    # 函数用途: 将目录遍历错误传回扫描调用方。
    def failed(error):
        raise error
    for directory, dirs, files in os.walk(root, followlinks=False, onerror=failed):
        base = Path(directory)
        watch_pin_source(base)
        for name in sorted(dirs):
            child = base / name
            if child.is_symlink():
                raise OSError("retention never follows symbolic links")
            if include_dirs:
                yield child
        for name in sorted(files):
            child = base / name
            if child.is_symlink():
                raise OSError("retention never follows symbolic links")
            yield child
        if not recursive:
            dirs.clear()


# LLM: pin为单次扫描临时投影，不是第二份权威；仅解析结构化引用字段，绝不解析正文。
# 类用途: 保存被引用的路径和记录ID，供扫描与执行阶段统一保护文件。
class _PinPaths(MutableSet):
    """写入时维护路径哈希投影，查询无需补扫历史；共享祖先按引用数移除。"""

    def __init__(self, values=()):
        self.values = set()
        self.exact = set()
        self.ancestors = Counter()
        self.update(values)

    def __contains__(self, value):
        return value in self.values

    def __iter__(self):
        return iter(self.values)

    def __len__(self):
        return len(self.values)

    def add(self, value):
        from .retention import _path_and_ancestor_keys, _path_keys

        if value in self.values:
            return
        self.values.add(value)
        self.exact.update(_path_keys(value))
        self.ancestors.update(_path_and_ancestor_keys((value,)))

    def discard(self, value):
        if value in self.values:
            self.values.remove(value)
            self._forget(value)

    def _forget(self, value):
        from .retention import _path_and_ancestor_keys, _path_keys

        self.exact.difference_update(_path_keys(value))
        for key in _path_and_ancestor_keys((value,)):
            self.ancestors[key] -= 1
            if not self.ancestors[key]:
                del self.ancestors[key]

    def pop(self):
        value = self.values.pop()
        self._forget(value)
        return value

    def update(self, values=(), extra_values=()):
        for items in (values, extra_values):
            for value in items:
                self.add(value)

    def clear(self):
        self.values.clear()
        self.exact.clear()
        self.ancestors.clear()

    def overlaps(self, path):
        from .retention import _path_and_ancestor_keys, _path_keys

        return (any(key in self.ancestors for key in _path_keys(path))
                or bool(_path_and_ancestor_keys((path,)) & self.exact))


@dataclass
class _RetentionPins:
    home: Path
    paths: set[Path] = field(default_factory=set)
    ids: set[str] = field(default_factory=set)
    complete: bool = True
    target_rebuild: Any = field(default=None, repr=False)
    verified_targets: set[Path] = field(default_factory=set, repr=False)

    def __setattr__(self, name, value):
        if name == "paths" and not isinstance(value, _PinPaths):
            value = _PinPaths(value)
        object.__setattr__(self, name, value)

    # LLM: 只为确切目标重核全体来源；仅开读前追加可采用新FD快照，损坏/不可读/读中变化始终False。
    # 函数用途: 将无关的读取起点竞态与目标保护解耦，绝不从源位置猜没有引用。
    def complete_for(self, path: Path, errors: list[MemoryRetentionError]) -> bool:
        target = path.resolve(strict=False)
        if self.complete or target in self.verified_targets:
            return True
        if self.target_rebuild is None:
            return False
        with target_pin_read():
            recovered, complete = self.target_rebuild(errors)
        if not complete:
            return False
        self.paths.update(recovered.paths)
        self.ids.update(recovered.ids)
        self.verified_targets.add(target)
        return True

    # LLM: 只收引用元数据；相对路径同时按owner和来源目录解释，保守多保护、不扩大删除范围。
    # 函数用途: 递归收集引用容器里的路径和ID，忽略摘要等正文。
    def collect(self, value: object, source: Path, *, reference: bool = False) -> None:
        for _ in self.collect_steps(value, source, reference=reference):
            pass

    def collect_steps(self, value: object, source: Path, *, reference: bool = False):
        # 容器遍历每步都检查时间，实际引用叶子另计条目；空元数据不挤占候选记录的条目预算。
        yield 0
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"content", "summary", "quote", "text", "reason", "note"}:
                    continue
                selected = reference or key in {"source", "path", "file_path", "cold_detail_ref"} or any(
                    part in key for part in ("_ref", "_source", "checkpoint", "legal_hold"))
                yield from self.collect_steps(item, source, reference=selected)
        elif isinstance(value, (list, tuple)):
            for item in value:
                yield from self.collect_steps(item, source, reference=reference)
        elif reference and isinstance(value, str) and value:
            yield 1
            self.ids.add(value)
            text = value.removeprefix("file://").removeprefix("file:").split("#", 1)[0]
            if "/" in text or text.endswith((".json", ".jsonl")):
                path = Path(text)
                roots = (self.home, source.parent) if not path.is_absolute() else (Path("/"),)
                self.paths.update((root / path).resolve(strict=False) for root in roots)

    # LLM: 路径重叠双向保护；content_path仅供trash读取真实payload，不读取墓碑声明的外部原路径。
    # 函数用途: 判断文件或目录是否被引用；坏记录不按零引用处理。
    def protects(self, path: Path, errors: list[MemoryRetentionError], *, content_path: Path | None = None) -> bool:
        resolved = path.resolve(strict=False)
        if self.paths.overlaps(resolved):
            return True
        if path.name in self.ids or path.stem in self.ids:
            return True
        if not self.ids or path.suffix not in {".json", ".jsonl"}:
            return False
        try:
            with closing(_pin_rows(content_path or path, jsonl=path.suffix == ".jsonl")) as rows:
                return any(_row_has_pinned_id(row, self.ids) for row in rows)
        except (OSError, ValueError, TypeError):
            errors.append(_state_error("MEMORY_RETENTION_PIN_SOURCE_UNREADABLE", path))
            return True


# LLM: ID只取结构化身份字段；不搜索content/summary字符串，引用不能成为语义授权。
# 函数用途: 检查过期Daily/Compact行身份是否仍被引用。
def _row_has_pinned_id(value: object, ids: set[str]) -> bool:
    if isinstance(value, list):
        return any(_row_has_pinned_id(item, ids) for item in value)
    if not isinstance(value, dict):
        return False
    for key, item in value.items():
        if (key == "id" or key.endswith("_id")) and isinstance(item, str) and item in ids:
            return True
        if isinstance(item, (dict, list)) and _row_has_pinned_id(item, ids):
            return True
    return False


# LLM: 仅源代次变化可重试，权限/坏JSON不靠重试漂白；filename用于只隔离依赖域。
# 类用途: 区分正常并发追加与真正不可读取的pin来源。
class _PinReadChanged(OSError):
    pass


class _CandidatePinSourceUnreadable(OSError):
    """候选权威账格式损坏；维护入口必须保留原整份拒绝边界。"""


# LLM: 借用真实二进制源，仅对解析已读取的字节做摘要；不额外读文件，不把读前完成当作读后完成。
# 类用途: 比对JSON解析流和读前观察（小文件整份、大文件切片），抓住预验后或读中的粗时钟改写。
class _PinDigestReader(io.RawIOBase):
    # LLM: 绑定来自该路径读前观察，post进度由解析后recheck作废，外层拥有原reader的关闭责任。
    # 函数用途: 准备不持有正文副本的流式摘要器。
    def __init__(self, path, raw, binding):
        super().__init__()
        self.path, self.raw, self.binding = path, raw, binding
        self.verified = False

    # LLM: 只读包装器，不改变FD身份。
    # 函数用途: 告知缓冲包装器可以读取。
    def readable(self):
        return True

    # LLM: 返回底层真实FD供_pin_rows首末验证。
    # 函数用途: 保留FD身份检查入口。
    def fileno(self):
        return self.raw.fileno()

    # LLM: 每次只消费包装器请求的实际字节交绑定摘要，EOF必须与读前观察一致。
    # 函数用途: 在正常解析读取中流式校验，不另开来源或重复读取正文。
    def readinto(self, buffer):
        raw = self.raw.read1(min(len(buffer), reference_pins._pin_source_chunk_bytes()))
        if not raw:
            self.verify()
            return 0
        buffer[:len(raw)] = raw
        self.binding.update(raw)
        return len(raw)

    # LLM: 只有真实EOF及长度、整份摘要或全部切片相等才接受解析流，变化清掉切片状态并走原有有界竞态重试。
    # 函数用途: 禁止旧解析结果与新来源摘要配对。
    def verify(self):
        if self.verified:
            return
        if not self.binding.matches():
            raise _PinReadChanged(11, "pin source changed while reading", str(self.path))
        self.verified = True


# LLM: 读前预验partial禁止打开解析器；post阶段已有完整基线时可重解析，由流式切片比对保证正文等于基线。
#   目标复核只容同inode非缩短的打开前追加并重置来源，解析流与读后都须验证。
# 函数用途: 流式严格读取pin来源，防止把旧解析结果与读后新摘要组成缓存。
def _pin_rows(path: Path, *, required: bool = False, jsonl: bool | None = None):
    try:
        before = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        watch_pin_source(path)
        if required:
            raise
        return
    if not stat.S_ISREG(before.st_mode):
        raise OSError("pin source is not a regular file")
    watch_pin_source(path)
    sources = reference_pins._SOURCES.get()
    if (sources is not None and path in sources and not reference_pins._is_complete_stamp(sources[path])
            and reference_pins._RESUMABLE_DIGESTS.get(path, {}).get("phase") != "post"):
        raise reference_pins._PartialPinVerification("MEMORY_RETENTION_PIN_SOURCE_UNREADABLE")
    raw = _fingerprint_source(path)
    try:
        opened = os.fstat(raw.fileno())
        if target_pin_read_enabled() and (before.st_dev, before.st_ino, before.st_mode) == (
                opened.st_dev, opened.st_ino, opened.st_mode) and opened.st_size >= before.st_size:
            if _file_identity(before) != _file_identity(opened):
                before = opened
                reference_pins.rebaseline_watched_pin_source(path)
                if sources is not None and not reference_pins._is_complete_stamp(sources.get(path)):
                    raise reference_pins._PartialPinVerification("MEMORY_RETENTION_PIN_SOURCE_UNREADABLE")
        if _file_identity(before) != _file_identity(opened):
            raise _PinReadChanged(11, "pin source changed before reading", str(path))
        binding = reference_pins.ParsedPinBytes(path)
        verifier = _PinDigestReader(path, raw, binding) if binding.bound else None
        reader = io.BufferedReader(verifier) if verifier is not None else raw
        with io.TextIOWrapper(reader, encoding="utf-8") as source:
            is_jsonl = jsonl if jsonl is not None else path.suffix == ".jsonl"
            rows = (json.loads(line) for line in source if line.strip()) if is_jsonl else (json.load(source),)
            for row in rows:
                if not isinstance(row, dict):
                    raise ValueError("pin source contains non-object rows")
                yield row
            if verifier is not None:
                verifier.verify()
            if _file_identity(before) != _file_identity(os.fstat(source.fileno())):
                raise _PinReadChanged(11, "pin source changed while reading", str(path))
    finally:
        raw.close()
    if _file_identity(before) != _file_identity(path.stat(follow_symlinks=False)):
        raise _PinReadChanged(11, "pin source changed while reading", str(path))
    reference_pins.recheck_pin_sources((path,))
    watch_pin_source(path)


# LLM: JSON对象入口完整耗尽读取器以核对首末FD和路径，不允许未验证首行充当权威状态。
# 函数用途: 读取唯一pin状态对象，缺失或多对象拒绝。
def _pin_object(path: Path) -> dict[str, Any]:
    contents = reference_pins._SOURCE_CONTENTS.get()
    buffered = contents.get("object_input") if contents is not None else None
    rows = ([json.loads(buffered[1].decode("utf-8"))] if buffered is not None and buffered[0] == path
            else list(_pin_rows(path, required=True)))
    if len(rows) != 1 or not isinstance(rows[0], dict):
        raise ValueError("pin source requires exactly one object")
    return rows[0]


# LLM: 墓碑声明只作pin路径映射，正文只读当前容器payload；坏墓碑或坏正文关闭purge，绝不读source外部文件。
# 函数用途: 保护回收站中仍被原路径、实际路径或记录ID引用的材料。
def trash_payload_pinned(pins: _RetentionPins, container: Path, errors: list[MemoryRetentionError]) -> bool:
    try:
        row = _pin_object(container / "tombstone.json")
        original = row.get("source_path")
        if row.get("status") != "trashed" or not isinstance(original, str) or not original:
            return True
        payload = container / "payload"
        return pins.protects(payload, errors) or pins.protects(Path(original), errors, content_path=payload)
    except (OSError, ValueError, TypeError, IndexError):
        errors.append(_state_error("MEMORY_RETENTION_PIN_SOURCE_UNREADABLE", container))
        return True


# 引用证明统一由有界投影构建；未知或代次变化只能续扫，不能退回同步全量重建。
def current_retention_pins(home: object, roots: tuple[Path, ...], errors: list[MemoryRetentionError],
                           *, now: datetime, scan_budget: RetentionScanBudget | None = None,
                           config: object = None) -> tuple[_RetentionPins, bool]:
    """推进一片规范引用投影；未完成只返回保护视图，不允许按目标退回全量扫描。"""
    owner = Path(home.owner_home_dir)
    budget = scan_budget if scan_budget is not None else RetentionScanBudget.default(config)
    policy, error = _load_policy(Path(home.owner_retention_json))
    if error is not None or policy is None or policy.legal_hold:
        if error is not None:
            errors.append(error)
        return _RetentionPins(owner, complete=False), False
    # 工作区目录发现由投影迭代器推进，不能在每动作固定前奏同步枚举所有workspace。
    roots = tuple(dict.fromkeys((*(Path(root).absolute() for root in roots), owner / "conversations")))
    retired = []
    with _PIN_BUILDS_LOCK:
        snapshot = _CURRENT_PIN_BUILDS.get(owner)
        if snapshot is not None and (snapshot.requested_roots != roots or snapshot.context.policy != policy
                or snapshot.contents["chunk_bytes"] != getattr(config, "memory_reference_pin_chunk_bytes",
                                                               reference_pins._PIN_SOURCE_CHUNK_BYTES)):
            retired.append(_CURRENT_PIN_BUILDS.pop(owner))
            snapshot = None
        while len(_CURRENT_PIN_BUILDS) >= _pin_capacity() and snapshot is None:
            _, old = _CURRENT_PIN_BUILDS.popitem(last=False)
            retired.append(old)
        if snapshot is None:
            context = _RetentionScanContext(home, SimpleNamespace(path=Path(home.owner_memory_candidates_jsonl)), policy, roots, now,
                                            scan_budget=budget, config=config)
            snapshot = _PinProjection(context)
            _CURRENT_PIN_BUILDS[owner] = snapshot
        _CURRENT_PIN_BUILDS.move_to_end(owner)
    for old in retired:
        old.close()
    # 同owner并发只做非阻塞借用；维护线程不能排队持锁等待别的扫描切片。
    if not snapshot.lock.acquire(blocking=False):
        return _RetentionPins(owner, complete=False), False
    try:
        if snapshot.closed:
            return _RetentionPins(owner, complete=False), False
        snapshot.budget = budget
        if snapshot.value is not None:
            # 已解析引用只复核来源，不逐候选重建；复核也受同一预算并跨调用续进。
            result = snapshot.value
            snapshot.value = None
            snapshot.pending = (result, snapshot.sources)
            snapshot.steps = snapshot._verify()
            snapshot.phase = "verify"
        result = snapshot.current(errors)
        if result is None:
            return _RetentionPins(owner, complete=False), False
        return result
    except (CandidateStoreCorruptError, OSError, ValueError, TypeError, KeyError, IndexError, OverflowError) as exc:
        with _PIN_BUILDS_LOCK:
            if _CURRENT_PIN_BUILDS.get(owner) is snapshot:
                _CURRENT_PIN_BUILDS.pop(owner)
        if not isinstance(exc, _PinReadChanged):
            errors.append(_pin_source_error(exc))
        return _RetentionPins(owner, complete=False), False
    finally:
        snapshot.lock.release()
        with _PIN_BUILDS_LOCK:
            retained = _CURRENT_PIN_BUILDS.get(owner) is snapshot
        if not retained:
            snapshot.close()


# LLM: 时间只用于保留期 cutoff，不参与长期事实冲突或真伪判定。
# 函数用途: 将 epoch 或 ISO 时间转换为 UTC epoch。
def _timestamp(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if float(value) > 0 else None
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except (OverflowError, ValueError):
        return None


# LLM: naive 测试时间按 UTC 解释，生产默认始终 aware UTC。
# 函数用途: 规范当前 retention 运行时间。
def _normalize_now(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


# LLM: 路径包含检查使用 resolve，不接受简单字符串前缀。
# 函数用途: 判断目标是否位于一个权威根目录内。
def _safe_child(path: Path, root: Path) -> bool:
    try:
        relative = path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except (OSError, RuntimeError, ValueError):
        return False
    return bool(relative.parts)


# LLM: 已整体选中的 task 不再生成内部文件动作，避免双删和状态歧义。
# 函数用途: 判断一个文件是否落在任一排除目录内。
def _inside_any(path: Path, roots: frozenset[Path]) -> bool:
    resolved = path.resolve(strict=False)
    for root in roots:
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


# LLM: mtime 读取失败返回正无穷，保证扫描不会把不可 stat 文件误判为很旧。
# 函数用途: 读取文件修改时间用于按龄类别。
def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return float("inf")


# 引用读取失败只说明引用视图不完整，不能抹去已执行的其它独立清理，也不能将未知引用当空。
def _pin_source_error(exc: Exception) -> MemoryRetentionError:
    """保留精确来源和脱敏异常原因，明确本次拒绝范围及完整性恢复动作。"""
    chain = []
    path = ""
    current = exc
    for _ in range(4):
        if current is None:
            break
        path = path or str(getattr(current, "filename", "") or "")
        chain.append(f"{type(current).__name__}: {redact_sensitive_text(str(current))[:200]}")
        current = current.__cause__
    return MemoryRetentionError(
        ("MEMORY_RETENTION_CANDIDATES_UNREADABLE" if isinstance(exc, _CandidatePinSourceUnreadable)
         else "MEMORY_RETENTION_PIN_SOURCE_UNREADABLE"), path,
        "; ".join(chain) + "；引用视图不完整，依赖该视图的删除未获准，其它动作结果见 actions。"
        "下一步：核实 path 对应来源及其权威根引用，恢复来源完整性后重跑 retention；"
        "不得删除未知引用或将损坏来源当空。",
    )


# LLM: 状态错误保持稳定 code/path，不泄露状态文件正文。
# 函数用途: 构造一条状态损坏诊断。
def _state_error(code: str, path: Path) -> MemoryRetentionError:
    return MemoryRetentionError(code, str(path), "structured retention state is invalid")


__all__ = [
    "build_retention_plan",
    "file_fingerprint",
    "paths_fingerprint",
    "tree_fingerprint",
]
