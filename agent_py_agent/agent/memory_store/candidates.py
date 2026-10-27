# LLM: C显式IO保持原身份/状态/字节；大文件摘要仅按五元stat复用的有界内存投影，小文件仍逐次强摘要，业务锁与CAS不变。
# 模块用途: 管理唯一候选源及可失效的解析/摘要缓存，避免后台热查询重复读取多MiB账本。
from __future__ import annotations

"""Owner 级唯一记忆候选仓库和状态机。"""

# LLM: candidates.jsonl 是候选当前态唯一事实源；ops、task workspace 和 learning draft 不得双写。
# 模块用途: 幂等合并观察、审核状态转换、晋升标记、过期和硬删除正文清理。

import hashlib
import json
import os
import pickle
import re
import threading
from collections import OrderedDict
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..common.global_write_commit import execute_global_write
from ..common.json_io import (
    locked_json_path,
    write_private_text_file_atomic_unlocked,
)
from ..common.nofollow_fs import (
    ensure_private_dir,
    open_directory_beneath,
    open_readonly_file_beneath,
    write_text_atomic_beneath,
)
from ..common.text_norm import fold_key
from ..user_space.owner_quota import OwnerQuotaAdmission, OwnerQuotaChange, OwnerQuotaEnforcer
from .candidate_commit_lock import candidate_commit_lock, candidate_redaction_lock
from .candidate_models import (
    CANDIDATE_ORIGINS,
    CANDIDATE_SCHEMA_VERSION,
    CANDIDATE_STATUSES,
    CANDIDATE_TYPES,
    PROMOTION_MODES,
    PROMOTION_TARGETS,
    PROPOSED_ACTIONS,
    TERMINAL_CANDIDATE_STATUSES,
    CandidateObservation,
    MemoryCandidate,
    MemoryScope,
    candidate_transition_allowed,
    host_promotion_mode,
    normalize_iso_time,
    normalize_reference_list,
    normalize_string_list,
    normalize_target_entry_version,
    stable_candidate_id,
    stable_observation_key,
    utc_now_iso,
    versioned_candidate_id,
)
from .candidate_source_coverage import (
    initial_source_coverage,
    merge_source_coverage,
    source_coverage_projection,
)
from .candidate_target_binding import TargetEvidenceVersionBindings, binding_history
from .operations import memory_content_hash
from .scope_contract import canonical_scope_key

# 保留清理只认合法的未晋升状态；扫描、执行与仓库删除共用同一集合，promoted 永不参与。
RETENTION_ELIGIBLE_CANDIDATE_STATUSES = CANDIDATE_STATUSES - {"promoted"}

# 记忆候选列表缓存最多跟踪的文件路径数。
_CANDIDATE_LOAD_CACHE_MAX_PATHS = 8
# 记忆候选列表缓存允许占用的最大总字节数。
_CANDIDATE_LOAD_CACHE_MAX_BYTES = 128 * 1024 * 1024
# 达到1MiB才允许热读复用相同五元stat的摘要，小文件维持每次完整摘要校验。
_CANDIDATE_FAST_STAT_PROBE_MIN_BYTES = 1024 * 1024
_CANDIDATE_STAMP_DIGEST_CACHE: OrderedDict[
    str, tuple[tuple[int, int, int, int, int], str]
] = OrderedDict()
_CandidateStatStamp = tuple[int, int, int, int, int]
_CANDIDATE_LOAD_CACHE: OrderedDict[
    str, tuple[tuple[int, int, int, int], str, tuple[MemoryCandidate, ...], dict[str, MemoryCandidate]]
] = OrderedDict()
_CANDIDATE_LOAD_CACHE_LOCK = threading.Lock()


# LLM: 四元内容身份之外以ctime检测探测/解析窗口内改写；错误不在这里吞成健康空候选。
# 函数用途: 取得候选文件mtime、尺寸、inode、设备和ctime，不可stat则禁用缓存。
def _candidate_load_stamp(path: Path) -> tuple[int, int, int, int, int] | None:
    try:
        info = path.stat()
    except OSError:
        return None
    return (info.st_mtime_ns, info.st_size, info.st_ino, info.st_dev, info.st_ctime_ns)


# LLM: 大文件五元stat相同时复用有界摘要LRU，小文件始终读全；冷读核尺寸及读后stat，竞态/超预算不发表摘要。
# 函数用途: 为候选解析缓存取得稳定版本，减少多MiB热账本的重复整文件读取。
def _candidate_load_probe(path: Path):
    stamp = _candidate_load_stamp(path)
    if stamp is None or stamp[1] > _CANDIDATE_LOAD_CACHE_MAX_BYTES:
        return None
    key = str(path)
    if stamp[1] >= _CANDIDATE_FAST_STAT_PROBE_MIN_BYTES:
        with _CANDIDATE_LOAD_CACHE_LOCK:
            cached = _CANDIDATE_STAMP_DIGEST_CACHE.get(key)
            if cached is not None and cached[0] == stamp:
                _CANDIDATE_STAMP_DIGEST_CACHE.move_to_end(key)
                return stamp, cached[1]
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if len(data) != stamp[1] or _candidate_load_stamp(path) != stamp:
        return None
    digest = hashlib.blake2b(data, digest_size=16).hexdigest()
    with _CANDIDATE_LOAD_CACHE_LOCK:
        _CANDIDATE_STAMP_DIGEST_CACHE[key] = (stamp, digest)
        _CANDIDATE_STAMP_DIGEST_CACHE.move_to_end(key)
        while len(_CANDIDATE_STAMP_DIGEST_CACHE) > _CANDIDATE_LOAD_CACHE_MAX_PATHS:
            _CANDIDATE_STAMP_DIGEST_CACHE.popitem(last=False)
    return stamp, digest


# LLM: 同一内存锁内清解析投影及stat摘要，不改变候选、配额或业务锁；失败写入也强制下次重读。
# 函数用途: 使指定候选路径的两层缓存同时失效。
def _drop_candidate_load_cache(path: Path) -> None:
    with _CANDIDATE_LOAD_CACHE_LOCK:
        _CANDIDATE_LOAD_CACHE.pop(str(path), None)
        _CANDIDATE_STAMP_DIGEST_CACHE.pop(str(path), None)


# LLM: 写侧先归一化owned副本，只读解析独占模型可接管；同代ID索引与元组原子发表/驱逐，最多8路径128MiB。
# 函数用途: 缓存严格验证且磁盘代次稳定的候选及其ID索引，含成功写后的独占快照。
def _remember_candidate_load(path, probe, records, *, copy_records: bool = True):
    if probe is None or _candidate_load_stamp(path) != probe[0]:
        return
    snapshot = deepcopy(tuple(records)) if copy_records else tuple(records)
    by_id = {row.candidate_id: row for row in snapshot}
    with _CANDIDATE_LOAD_CACHE_LOCK:
        _CANDIDATE_LOAD_CACHE[str(path)] = (probe[0][:4], probe[1], snapshot, by_id)
        _CANDIDATE_LOAD_CACHE.move_to_end(str(path))
        while (len(_CANDIDATE_LOAD_CACHE) > _CANDIDATE_LOAD_CACHE_MAX_PATHS
               or sum(entry[0][1] for entry in _CANDIDATE_LOAD_CACHE.values()) > _CANDIDATE_LOAD_CACHE_MAX_BYTES):
            _CANDIDATE_LOAD_CACHE.popitem(last=False)


# LLM: 校验磁盘探针与缓存摘要完全一致才返回只读元组；不一致时可驱逐旧缓存，调用方负责深复制输出。
# 函数用途: 探测并返回当前候选路径已验证的缓存快照元组。
def _cached_candidate_rows(path: Path, probe, *, evict_on_miss: bool) -> tuple[MemoryCandidate, ...] | None:
    with _CANDIDATE_LOAD_CACHE_LOCK:
        cached = _CANDIDATE_LOAD_CACHE.get(str(path))
        if probe is not None and cached is not None and cached[:2] == (probe[0][:4], probe[1]):
            _CANDIDATE_LOAD_CACHE.move_to_end(str(path))
            return cached[2]
        if evict_on_miss:
            _CANDIDATE_LOAD_CACHE.pop(str(path), None)
        return None


# LLM: 借用入口保持全表校验和加载覆盖；仅复用同一元组代次的ID索引，返回值深复制，不向外泄漏缓存模型。
# 函数用途: 持候选文件锁按ID常数时间查询，缓存超预算时保留完整加载后的遍历。
def _load_one_candidate_unlocked(service: CandidateService, candidate_id: str) -> MemoryCandidate | None:
    rows = _borrow_candidate_readonly_rows(service)
    if rows is None:
        return next((row for row in service._load_unlocked() if row.candidate_id == candidate_id), None)
    with _CANDIDATE_LOAD_CACHE_LOCK:
        cached = _CANDIDATE_LOAD_CACHE.get(str(service.path))
        indexed = cached is not None and cached[2] is rows
        matched = cached[3].get(candidate_id) if indexed else None
    if not indexed:
        matched = next((row for row in rows if row.candidate_id == candidate_id), None)
    return _hydrate_candidate(service.path, deepcopy(matched)) if matched is not None else None


# LLM: 调用方持原文件锁且只读模型；加载覆盖返回None，冷读严格解析后独占交缓存，不复制全账本。
# 函数用途: 为列表、统计、单条与内部扫描借用当前已验证候选元组。
def _borrow_candidate_readonly_rows(service: CandidateService) -> tuple[MemoryCandidate, ...] | None:
    if ('_load_unlocked' in service.__dict__
            or getattr(service._load_unlocked, '__func__', None) is not CandidateService._load_unlocked):
        return None
    cached = _cached_candidate_rows(service.path, _candidate_load_probe(service.path), evict_on_miss=True)
    if cached is not None:
        return cached
    probe, records = service._parse_candidate_ledger_unlocked()
    snapshot = tuple(records)
    _remember_candidate_load(service.path, probe, snapshot, copy_records=False)
    return snapshot


# LLM: 只复制容器不复制模型；状态过滤、稳定排序与正limit取末尾的规则供公开列表和内部借用共用。
# 函数用途: 计算候选扫描的实际返回切片，不修改原候选元组。
def _candidate_read_slice(rows, statuses, limit):
    records = [row for row in rows if statuses is None or row.status in statuses]
    records.sort(key=lambda row: (row.created_at, row.candidate_id))
    return records[-limit:] if limit > 0 else records


# LLM: 仅遍历已验证候选的标量字段，既不复制、排序、修改候选，也不泄露任何记录引用；兼容一次性Iterable。
# 函数用途: 纯计算候选数量、状态和类型分布。
def _candidate_stats_from_rows(candidates: Iterable[MemoryCandidate]) -> dict[str, object]:
    statuses: dict[str, int] = {}
    types: dict[str, int] = {}
    total = 0
    for item in candidates:
        total += 1
        statuses[item.status] = statuses.get(item.status, 0) + 1
        types[item.candidate_type] = types.get(item.candidate_type, 0) + 1
    return {'schema_version': CANDIDATE_SCHEMA_VERSION, 'total': total,
            'by_status': dict(sorted(statuses.items())), 'by_type': dict(sorted(types.items()))}


# LLM: 损坏候选账本必须阻断写入；跳过坏行会在下一次原子写时永久丢数据。
# 类用途: 向 CLI、Curator 和迁移器返回稳定的候选账本损坏错误。
class CandidateStoreCorruptError(RuntimeError):
    pass


# LLM: 状态机错误要和存储损坏、找不到候选区分，调用方不得自然语言猜修复动作。
# 类用途: 表示一次不在统一状态图中的非法审核或晋升转换。
class CandidateTransitionError(RuntimeError):
    pass


# LLM: 表示候选快照已变，不自动重试或把新内容继承为已审核；调用方应重新核对。
# 类用途: 区分候选并发变更与目标库版本冲突。
class CandidateStateChangedError(RuntimeError):
    # LLM: 只携带当前状态用于稳定拒绝回执，不修改候选。
    # 函数用途: 保存并发变更后的候选状态。
    def __init__(self, status: str):
        super().__init__("candidate changed after verification")
        self.status = status


# LLM: Service 只接收结构化 observation/review；不解析聊天正文或相似文本。
# LLM: C只给原目录/锁FD/raw末写接当前scope；所有状态机/merge/CAS/配额原算法保持，同步锁不缩短。
# LLM: 写前失效、成功后发表owned缓存；readonly_rows仅内部借用，公开list/get与写侧模型深复制隔离。
# 类用途: 管理owner唯一候选源及同代只读索引，不创建第二事实源或保存活来源。
class CandidateService:
    # LLM: 构造器只绑定 owner 规范路径和可选 quota；创建目录不读取或迁移旧候选源。
    # LLM: coordinator_io只存策略，目录出生消费原scope；quota与唯一候选身份不变，不存source/callback。
    # 函数用途: 绑定唯一候选仓库，C按当前原门准备原父目录。
    def __init__(
        self,
        path: str | Path,
        *,
        quota_enforcer: OwnerQuotaEnforcer | None = None,
        coordinator_io: bool = False,
    ) -> None:
        self.path = Path(path)
        # 目录缺失时逐级按 0700 新建（pbfix 2026-10-04：统一走 nofollow_fs.ensure_private_dir；已存在的目录一律不动）。
        self.coordinator_io = coordinator_io
        if self.coordinator_io:
            _coordinator_candidate_write(lambda: ensure_private_dir(self.path.parent))
        else:
            ensure_private_dir(self.path.parent)
        self.quota_enforcer = quota_enforcer

    # LLM: 重放同一 observation key 只更新时间和 refs，不增加 occurrence_count。
    # 函数用途: 新建或合并一次候选观察，并自动进入 pending/blocked 初审状态。
    def observe(self, observation: CandidateObservation) -> MemoryCandidate:
        return self.observe_many([observation])[0]

    # LLM: Batch observations validate completely before one locked replacement; callers must not emulate atomicity with loops.
    # 函数用途: 原子写入一批候选，任一条非法时整个批次不改变 candidates.jsonl。
    # LLM: C复用同一原锁/序列化/准入，仅出生与raw末写消费原scope；状态/身份/事务顺序不变。
    # 函数用途: 沿原候选业务取得真实锁并保完整失败事实，不以活来源替换领域裁决。
    def observe_many(
        self,
        observations: list[CandidateObservation] | tuple[CandidateObservation, ...],
    ) -> list[MemoryCandidate]:
        with _quota_admission(self.quota_enforcer) as admission:
            with self._locked_candidates():
                candidates = _hydrate_observation_targets(self.path, self._load_unlocked(), observations)
                candidates, committed = merge_candidate_observations(
                    candidates,
                    observations,
                )
                _write_candidates_unlocked(self.path, candidates, admission=admission, **({} if not self.coordinator_io else {"coordinator_io": True}))
                return committed

    # LLM: 结构已校验；好观察先原子合并，坏观察只可新建阻塞行而绝不修改同ID旧行，全批只落盘一次。
    # 函数用途: 保留首次缺证据候选的可修复回执，同时禁止借证据与坏操作污染。
    # LLM: C复用同一原锁/序列化/准入，仅出生与raw末写消费原scope；状态/身份/事务顺序不变。
    # 函数用途: 沿原候选业务取得真实锁并保完整失败事实，不以活来源替换领域裁决。
    def observe_evidence_batch(self, observations: list, failures: list[str]) -> list:
        from .evidence_admission import assemble_evidence_batch

        with _quota_admission(self.quota_enforcer) as admission, self._locked_candidates():
            rows = _hydrate_observation_targets(self.path, self._load_unlocked(), observations)
            rows, results = assemble_evidence_batch(rows, observations, failures)
            _write_candidates_unlocked(self.path, rows, admission=admission, **({} if not self.coordinator_io else {"coordinator_io": True}))
            return results

    # LLM: 冷热均先严格验证/借用、过滤排序切片，再且仅深复制实际结果；覆盖加载仍由原方法提供。
    # 函数用途: 返回可由调用方安全修改的独立候选列表，保持原筛选与排序规则。
    def list(
        self,
        *,
        statuses: set[str] | frozenset[str] | None = None,
        limit: int = 0,
    ) -> list[MemoryCandidate]:
        with self._locked_candidates():
            borrowed = _borrow_candidate_readonly_rows(self)
            if borrowed is None:
                loaded = self._load_unlocked()
        source_rows = borrowed if borrowed is not None else loaded
        records = _candidate_read_slice(source_rows, statuses, limit)
        return deepcopy(records)

    # LLM: 仅内部消费者只读提取字段，不得修改候选或嵌套字段；实例/子类覆盖list或load时调用原覆盖入口。
    # 函数用途: 在原文件锁内生成只读扫描元组，避免保留规划和学习排除复制全账本。
    def readonly_rows(self, *, statuses: set[str] | frozenset[str] | None = None,
                      limit: int = 0) -> tuple[MemoryCandidate, ...]:
        if ("list" in self.__dict__ or getattr(self.list, "__func__", None) is not CandidateService.list
                or "_load_unlocked" in self.__dict__
                or getattr(self._load_unlocked, "__func__", None) is not CandidateService._load_unlocked):
            return tuple(self.list(statuses=statuses, limit=limit))
        with self._locked_candidates():
            return tuple(_candidate_read_slice(_borrow_candidate_readonly_rows(self), statuses, limit))

    # LLM: candidate_id 是唯一定位键；禁止按正文或 subject 模糊选中审核目标。
    #   默认走锁内单条定位，命中缓存时只深拷贝目标候选而不再深拷贝和排序全表；若 list 被外部替身替换则保留原遍历。
    # 函数用途: 读取一个候选，不存在时抛出精确 KeyError。
    def get(self, candidate_id: str) -> MemoryCandidate:
        target = str(candidate_id or "").strip()
        if "list" in self.__dict__ or getattr(self.list, "__func__", None) is not CandidateService.list:
            for item in self.list():
                if item.candidate_id == target:
                    return item
            raise KeyError(target)
        with self._locked_candidates():
            current = _load_one_candidate_unlocked(self, target)
        if current is not None:
            return current
        raise KeyError(target)

    # LLM: 调用方已持quota→policy；候选锁覆盖快照核对和正式写入，期间不能合并/审核该候选。
    # 函数用途: 为最终提交锁定已验证的完整候选快照。
    # LLM: C复用同一原锁/序列化/准入，仅出生与raw末写消费原scope；状态/身份/事务顺序不变。
    # 函数用途: 沿原候选业务取得真实锁并保完整失败事实，不以活来源替换领域裁决。
    @contextmanager
    def locked_snapshot(self, expected: MemoryCandidate) -> Iterator[MemoryCandidate]:
        with candidate_commit_lock(self.path, **({} if not self.coordinator_io else {"coordinator_io": True})):
            current = _load_one_candidate_unlocked(self, expected.candidate_id)
            if current != expected:
                raise CandidateStateChangedError(current.status if current is not None else "missing")
            yield current

    # LLM: 只补尚未指定的长期落点；调用方已用统一目标政策判断，完整快照CAS先于任何字段写入。
    # 函数用途: 同状态下持久化默认目标，不放开通用状态机或改写终态候选。
    # LLM: C复用同一原锁/序列化/准入，仅出生与raw末写消费原scope；状态/身份/事务顺序不变。
    # 函数用途: 沿原候选业务取得真实锁并保完整失败事实，不以活来源替换领域裁决。
    def fill_long_term_target(self, expected: MemoryCandidate, reviewer: str) -> MemoryCandidate:
        actor = str(reviewer or "").strip()
        if not actor:
            raise ValueError("candidate reviewer is required")
        with _quota_admission(self.quota_enforcer) as admission, self._locked_candidates():
            rows = self._load_unlocked()
            index = _candidate_index(rows, expected.candidate_id)
            current = rows[index]
            if current != expected:
                raise CandidateStateChangedError(current.status)
            if (current.promotion_target not in {"", "none"}
                    or current.status in TERMINAL_CANDIDATE_STATUSES):
                return current
            now = utc_now_iso()
            rows[index] = replace(current, promotion_target="long_term", updated_at=now,
                status_history=[*current.status_history,
                    _status_event(current.status, actor=actor, note="default_long_term_target", at=now)])
            _write_candidates_unlocked(self.path, rows, admission=admission, **({} if not self.coordinator_io else {"coordinator_io": True}))
            return rows[index]

    # LLM: 字符串保持旧审核入口；快照锁内CAS，仅放行已批准用户长期候选补默认目标，其它同状态仍幂等。
    # 函数用途: 推进候选状态并可补充替换目标、冲突和晋升落点。
    # LLM: C复用同一原锁/序列化/准入，仅出生与raw末写消费原scope；状态/身份/事务顺序不变。
    # 函数用途: 沿原候选业务取得真实锁并保完整失败事实，不以活来源替换领域裁决。
    def transition(
        self,
        candidate_id: str | MemoryCandidate,
        target_status: str,
        *,
        reviewer: str,
        review_note: str = "",
        proposed_action: str | None = None,
        target_entry_id: str | None = None,
        conflicts_with: list[str] | tuple[str, ...] | None = None,
        promotion_target: str | None = None,
    ) -> MemoryCandidate:
        expected = candidate_id if isinstance(candidate_id, MemoryCandidate) else None
        candidate_id = expected.candidate_id if expected is not None else candidate_id
        target = str(target_status or "").strip().lower()
        actor = str(reviewer or "").strip()
        if not actor:
            raise ValueError("candidate reviewer is required")
        with _quota_admission(self.quota_enforcer) as admission:
            with self._locked_candidates():
                candidates = self._load_unlocked()
                index = _candidate_index(candidates, candidate_id)
                current = _hydrate_candidate(self.path, candidates[index])
                if expected is not None and current != expected:
                    raise CandidateStateChangedError(current.status)
                default_repair = (expected is not None and current.status == "approved"
                    and current.promotion_target in {"", "none"} and promotion_target == "long_term"
                    and current.origin == "user_explicit" and current.candidate_type in {"long_term_fact", "event", "project", "working_agreement"}
                    and proposed_action is None and target_entry_id is None and conflicts_with is None)
                if target == current.status and not default_repair:
                    return current
                if target != current.status and not candidate_transition_allowed(current.status, target):
                    raise CandidateTransitionError(
                        f"candidate transition {current.status} -> {target} is not allowed"
                    )
                now = utc_now_iso()
                updated = replace(
                    current,
                    cold_detail_ref="",
                    target_evidence_bindings=binding_history(current),
                    status=target,
                    reviewer=actor,
                    review_note=str(review_note or "").strip(),
                    updated_at=now,
                    proposed_action=(
                        str(proposed_action).strip().lower()
                        if proposed_action is not None
                        else current.proposed_action
                    ),
                    target_entry_id=(
                        str(target_entry_id or "").strip()
                        if target_entry_id is not None
                        else current.target_entry_id
                    ),
                    target_entry_version=(
                        None if target_entry_id is not None and
                        str(target_entry_id or "").strip() != current.target_entry_id
                        else current.target_entry_version
                    ),
                    conflicts_with=(
                        normalize_string_list(conflicts_with)
                        if conflicts_with is not None
                        else current.conflicts_with
                    ),
                    promotion_target=(
                        str(promotion_target).strip().lower()
                        if promotion_target is not None
                        else current.promotion_target
                    ),
                    status_history=[
                        *current.status_history,
                        _status_event(target, actor=actor, note=review_note, at=now),
                    ],
                )
                candidates[index] = updated
                _write_candidates_unlocked(self.path, candidates, admission=admission, **({} if not self.coordinator_io else {"coordinator_io": True}))
                return updated

    # LLM: 仅合并候选批准与默认落点，不授予正式发布权限；snapshot CAS及原状态机/配额/锁仍由transition执行。
    # 函数用途: 给自动晋升调用者提供一次写盘的准备入口，代替补落点后再次批准。
    def prepare_automatic_promotion(self, candidate_id: str | MemoryCandidate, *, reviewer: str,
                                    reason: str = "") -> MemoryCandidate:
        from .candidate_models import effective_promotion_target

        expected = candidate_id if isinstance(candidate_id, MemoryCandidate) else self.get(candidate_id)
        target = effective_promotion_target(expected.promotion_target, expected.origin, expected.candidate_type)
        return self.transition(expected, "approved", reviewer=reviewer, review_note=reason, promotion_target=target)

    # LLM: 正式落点提交成功后才能标 promoted；失败时保留 approved 供安全重试。
    # 函数用途: 记录唯一 PromotionService 已完成的正式引用。
    def mark_promoted(
        self,
        candidate_id: str,
        *,
        reviewer: str,
        promotion_ref: str,
        review_note: str = "",
    ) -> MemoryCandidate:
        with _quota_admission(self.quota_enforcer) as admission:
            return self.mark_promoted_admitted(candidate_id, admission,
                (reviewer, promotion_ref, review_note))

    # LLM: 晋升复用原锁；remove显式携带hash清理意图，终态幂等重试也必须清冷孤儿，不依赖热行变化。
    # 函数用途: 提交不会留下“已晋升但未脱敏”中间状态的候选终态。
    # LLM: C复用同一原锁/序列化/准入，仅出生与raw末写消费原scope；状态/身份/事务顺序不变。
    # 函数用途: 沿原候选业务取得真实锁并保完整失败事实，不以活来源替换领域裁决。
    def mark_promoted_admitted(self, candidate_id, admission, review: tuple[str, str, str]):
        ref = str(review[1] or "").strip()
        if not ref:
            raise ValueError("promotion_ref is required")
        with candidate_redaction_lock(self.path, **({} if not self.coordinator_io else {"coordinator_io": True})):
            candidates = self._load_unlocked()
            index = _candidate_index(candidates, candidate_id)
            current = candidates[index]
            _require_promotable_binding(current, ref)
            if current.status == "promoted" and current.proposed_action != "remove":
                return current
            rows = _promoted_rows(candidates, index, review)
            cold = {"cold_redaction": {"hashes": {current.content_hash}, "excluded": set(),
                "reviewer": review[0], "now": utc_now_iso()}} if current.proposed_action == "remove" else {}
            _write_candidates_unlocked(self.path, rows, admission=admission, **cold,
                **({} if not self.coordinator_io else {"coordinator_io": True}))
            return rows[index]

    # LLM: Hard delete 清正文但保留 hash/id/status 审计，不得改写 ConversationStore 原话。
    # 函数用途: 清除与已硬删除长期记忆相同的候选正文。
    def redact_content(
        self,
        *,
        content_hashes: set[str],
        exclude_candidate_ids: set[str] | frozenset[str] | None = None,
        reviewer: str = "memory-hard-delete",
    ) -> int:
        return self.redact_content_admitted(None, {
            "content_hashes": content_hashes,
            "exclude_candidate_ids": exclude_candidate_ids,
            "reviewer": reviewer,
        })

    # LLM: 晋升硬删除传入外层准入，普通入口自行准入；同一候选写入协议不再 policy→quota 重入。
    # 函数用途: 按已有准入或普通配额通道清除候选正文，保留身份与审计。
    # LLM: C复用同一原锁/序列化/准入，仅出生与raw末写消费原scope；状态/身份/事务顺序不变。
    # 函数用途: 沿原候选业务取得真实锁并保完整失败事实，不以活来源替换领域裁决。
    def redact_content_admitted(self, admission: OwnerQuotaAdmission | None, options: dict) -> int:
        if admission is None:
            with _quota_admission(self.quota_enforcer) as acquired:
                return self.redact_content_admitted(acquired, options)
        hashes = {str(item).strip() for item in options["content_hashes"] if str(item).strip()}
        excluded = {str(item).strip() for item in (options.get("exclude_candidate_ids") or ()) if str(item).strip()}
        if not hashes:
            return 0
        now = utc_now_iso()
        with candidate_redaction_lock(self.path, **({} if not self.coordinator_io else {"coordinator_io": True})):
            candidates = self._load_unlocked()
            replacements = [
                _redacted_candidate(item, hashes=hashes, excluded=excluded,
                    reviewer=str(options.get("reviewer") or "memory-hard-delete"), now=now)
                for item in candidates
            ]
            changed = sum(item is not None for item in replacements)
            if changed or _cold_archive_names(self.path):
                rows = [new if new is not None else old for old, new in zip(candidates, replacements, strict=True)]
                _write_candidates_unlocked(self.path, rows, admission=admission,
                    cold_redaction={"hashes": hashes, "excluded": excluded, "reviewer": options.get("reviewer"), "now": now},
                    **({} if not self.coordinator_io else {"coordinator_io": True}))
            return changed

    # LLM: 排除标记只按精确 candidate_id 写结构化原因码，不改状态、不写状态历史、不看正文；
    #   幂等：同值重复标记不产生写入，状态机与人工审核路径都不受影响。
    # 函数用途: 给一批候选写排除自动晋升的持久原因码，返回实际变化条数。
    # LLM: C复用同一原锁/序列化/准入，仅出生与raw末写消费原scope；状态/身份/事务顺序不变。
    # 函数用途: 沿原候选业务取得真实锁并保完整失败事实，不以活来源替换领域裁决。
    def mark_promotion_excluded(
        self,
        candidate_ids: list[str] | tuple[str, ...],
        *,
        reason: str,
    ) -> int:
        ids = set(normalize_string_list(candidate_ids))
        code = str(reason or "").strip()
        if not ids or not code:
            return 0
        with _quota_admission(self.quota_enforcer) as admission, self._locked_candidates():
            candidates = self._load_unlocked()
            changed = _mark_excluded_candidates(candidates, ids, code, utc_now_iso())
            if changed:
                _write_candidates_unlocked(self.path, candidates, admission=admission, **({} if not self.coordinator_io else {"coordinator_io": True}))
            return changed

    # LLM: 此入口仅规划合法未晋升候选，禁止为归档暗中写盘；终态维护由执行delete_terminal负责。
    # 函数用途: 不复制全账本地规划过期候选ID，不执行删除。
    def retention_candidates(self, *, before: datetime) -> list[str]:
        cutoff = before.astimezone(timezone.utc)
        result: list[str] = []
        for item in self.readonly_rows(statuses=RETENTION_ELIGIBLE_CANDIDATE_STATUSES):
            if _is_binding_tombstone(item):
                continue
            if datetime.fromisoformat(item.updated_at) < cutoff:
                result.append(item.candidate_id)
        return result

    # LLM: 原quota→候选锁下先耐久归档完整详情再换热行；至少一天冷却，失败无成功回执且缓存失效。
    # 函数用途: 维护老终态的冷热分离，默认30天或超过1000条且已冷却的终态进入冷归档。
    def compact_terminal_candidates(self, *, before: datetime | None = None, max_hot_terminal: int = 1000,
                                    now: datetime | None = None) -> int:
        return _compact_terminal_candidates(self, before, max_hot_terminal, now)

    # LLM: 精确ID且仅合法未晋升状态；同锁CAS后删除并仅归档其余陈旧未晋升终态，传递调用方now而非重取墙钟。
    #   无快照调用保留原状态检查；版本绑定仍脱敏保留，全跳过或账本未变时不重写。
    # 函数用途: 按本次保留运行时间清理未晋升候选，保留已晋升行，并准确报告锁前变化。
    # LLM: C复用同一原锁/序列化/准入，仅出生与raw末写消费原scope；状态/身份/事务顺序不变。
    # 函数用途: 沿原候选业务取得真实锁并保完整失败事实，不以活来源替换领域裁决。
    def delete_terminal(
        self,
        candidate_ids: Iterable[str],
        *,
        expected_snapshots: dict[str, MemoryCandidate] | None = None,
        skipped_state_changed: set[str] | None = None,
        now: datetime | None = None,
    ) -> int:
        ids = set(normalize_string_list(tuple(candidate_ids)))
        if not ids:
            return 0
        with _quota_admission(self.quota_enforcer) as admission, self._locked_candidates():
            candidates = self._load_unlocked()
            if expected_snapshots is not None:
                current_by_id = {item.candidate_id: item for item in candidates}
                for cid in list(ids):
                    current = current_by_id.get(cid)
                    expected = expected_snapshots.get(cid)
                    if expected is None:
                        continue
                    if (current is not None and _candidate_snapshot_matches(self.path, current, expected)
                            and current.status in RETENTION_ELIGIBLE_CANDIDATE_STATUSES
                            and not _is_binding_tombstone(current)):
                        continue
                    ids.remove(cid)
                    if skipped_state_changed is not None:
                        skipped_state_changed.add(cid)
            if not ids:
                return 0
            for item in candidates:
                if item.candidate_id in ids and item.status not in RETENTION_ELIGIBLE_CANDIDATE_STATUSES:
                    raise CandidateTransitionError(
                        f"candidate {item.candidate_id} is not retention-eligible"
                    )
            return _commit_retention_rows(self, candidates, ids, admission, now=now)

    # LLM: 原锁内借用已验证快照只读计数，热缓存零深拷贝；保留list及_load_unlocked覆盖入口，冷读仍完整验证账本。
    # 函数用途: 为doctor/CLI统计数量、状态和类型，不复制候选或暴露正文、路径及缓存引用。
    def stats(self) -> dict[str, object]:
        if 'list' in self.__dict__ or getattr(self.list, '__func__', None) is not CandidateService.list:
            candidates: Iterable[MemoryCandidate] = self.list()
        else:
            with self._locked_candidates():
                borrowed = _borrow_candidate_readonly_rows(self)
                candidates = borrowed if borrowed is not None else self._load_unlocked()
        return _candidate_stats_from_rows(candidates)

    # LLM: 只返回原同path锁，C透传准确FD出生，不缓存活来源或缩短锁内业务；默认调用形状不加False。
    # 函数用途: 给候选原读写算法取得相同双层锁，等待与全文件读取都门外。
    def _locked_candidates(self):
        options = {} if not self.coordinator_io else {"birth_commit": _coordinator_candidate_write}
        return locked_json_path(self.path, **options)

    # LLM: 写侧始终取得可修改的独立模型；热快照深复制，冷解析以copy_records=True隔离缓存，不向写方借缓存对象。
    # 函数用途: 在原文件锁内加载可安全修改的候选列表。
    def _load_unlocked(self) -> list[MemoryCandidate]:
        probe = _candidate_load_probe(self.path)
        cached = _cached_candidate_rows(self.path, probe, evict_on_miss=True)
        if cached is not None:
            return list(deepcopy(cached))
        probe, records = self._parse_candidate_ledger_unlocked()
        _remember_candidate_load(self.path, probe, records, copy_records=True)
        return records

    # LLM: 持原锁逐行严格解析；同FD流式摘要不保留整表原文/字典，归一化改变时不复用旧原行。
    # 函数用途: 流式构造候选并保留未改行，坏行、schema或重复ID仍阻断写入。
    def _parse_candidate_ledger_unlocked(self) -> tuple[tuple[_CandidateStatStamp, str] | None, list[MemoryCandidate]]:
        return _parse_candidate_ledger(self.path)


# LLM: 同一宿主观察身份被拿去对应另一个类型化主题时抛出；仍是 ValueError（原调用方的全有或全无语义不变），
#   单列子类只为让 Curator 能精确识别这一种冲突并只丢弃那一条（split_identity_conflicts）。
# 类用途: 表示一条观察撞上了已有观察身份、但类型化主题不同。
class CandidateIdentityConflictError(ValueError):
    pass


# LLM: 两个提交入口共用纯合并；先按规范证据及精确目标校验版本，再按候选/观察身份合并。
#   正文、动作、入口和引用并集不刷新旧绑定；首拒草稿只保留版本责任，不赋予其范围条件/观察键占据合法身份的权力。
#   ID/观察键→位置索引一次构建、逐条更新，不改变typed identity、首拒草稿或目标版本权威，不按正文hash越界去重。
# 函数用途: 以索引合并观察，消除每条观察重建全表字典、扫描观察键和查找列表位置。
def merge_candidate_observations(
    candidates: list[MemoryCandidate],
    observations: list[CandidateObservation] | tuple[CandidateObservation, ...],
) -> tuple[list[MemoryCandidate], list[MemoryCandidate]]:
    prepared = _prepare_observations(observations)
    result = list(candidates)
    bindings = TargetEvidenceVersionBindings(result)
    by_id = {item.candidate_id: index for index, item in enumerate(result)}
    by_observation = _observation_index(result)
    committed: list[MemoryCandidate] = []
    for item in prepared:
        committed.append(_merge_indexed_observation(result, by_id, by_observation, bindings, item))
    return result, committed


# LLM: 预演和正式合并共用一次索引构建，拒绝草稿不占用合法观察身份。
# 函数用途: 将独立观察键映射到所有已有候选位置，不改原模型。
def _observation_index(rows: list[MemoryCandidate]) -> dict[str, set[int]]:
    result: dict[str, set[int]] = {}
    for index, item in enumerate(rows):
        if not _rejected_draft(item):
            for key in item.observation_keys:
                result.setdefault(key, set()).add(index)
    return result


# LLM: 全部冲突检查及新模型构造先于共享索引变更；拒绝项不留下观察键、候选行或目标绑定。
# 函数用途: 将单条已规范观察成功合入现有索引，供批提交与可跳冲突预演复用。
def _merge_indexed_observation(result, by_id, by_observation, bindings, prepared):
    normalized, candidate_id, observation_key = prepared
    if bindings.conflicts(normalized):
        raise CandidateIdentityConflictError("candidate evidence cannot refresh target observation version")
    positions = sorted(by_observation.get(observation_key, ()))
    if any(not _same_candidate_identity(result[index], normalized) for index in positions):
        raise CandidateIdentityConflictError("candidate observation identity was reused for a different typed subject")
    index = by_id.get(candidate_id, positions[0] if positions else None)
    if index is None:
        candidate = _new_candidate(normalized, candidate_id, observation_key)
    else:
        existing = result[index]
        if not _rejected_draft(existing) and not _same_candidate_identity(existing, normalized):
            raise CandidateIdentityConflictError("candidate observation identity was reused for a different typed subject")
        candidate = _merge_candidate(existing, normalized, observation_key)
    # 验证绑定历史可能抛错，必须在任何共享状态变更之前完成。
    binding_history(normalized)
    if index is None:
        index = len(result)
        result.append(candidate)
    else:
        result[index] = candidate
    by_id[candidate.candidate_id] = index
    if not _rejected_draft(candidate) and observation_key in candidate.observation_keys:
        by_observation.setdefault(observation_key, set()).add(index)
    bindings.add(normalized)
    return candidate


# LLM: 同一观察可改述但不能换主题、范围、动作或目标版本；独立证据才能基于新目标版本创建观察。
# subject, scope, action, target, or promotion authority behind the same host identity.
#   主题键与范围必须按与身份计算（stable_candidate_id 的 fold_key、Curator 观察身份的规范范围键）同一口径比较：
#   只差大小写/书写形式的主题不能被当成“另一个主题”而让整批提交失败（J13，2026-10-02）。
# LLM: 同一观察也不得改绑正式目标版本；需要基于新版本的独立观察，不能重放旧证据刷新覆盖权。
# 函数用途: 判断一个已存在候选是否可安全接收同 observation key 的重放。
def _same_candidate_identity(
    current: MemoryCandidate,
    observation: CandidateObservation,
) -> bool:
    from .candidate_models import effective_promotion_target

    return (
        current.candidate_type == observation.candidate_type
        and fold_key(current.subject_key) == fold_key(observation.subject_key)
        and _identity_scope(current.scope) == _identity_scope(observation.scope)
        and current.proposed_action == observation.proposed_action
        and current.target_entry_id == observation.target_entry_id
        and current.target_entry_version == observation.target_entry_version
        and effective_promotion_target(current.promotion_target, current.origin, current.candidate_type)
        == effective_promotion_target(observation.promotion_target, observation.origin, observation.candidate_type)
    )


# LLM: 与 Curator 观察身份（curator_validation._canonical_observation_id）同一口径：范围键取规范键，坏值保持原样。
# 函数用途: 生成同身份比对用的范围投影。
def _identity_scope(value: object) -> dict[str, object]:
    scope = MemoryScope.from_value(value)
    try:
        key = canonical_scope_key(scope.scope_type, scope.scope_key)
    except (ValueError, TypeError):
        key = scope.scope_key
    return {**scope.to_dict(), "scope_key": key}


# LLM: 纯函数、不写账本；与正式提交共用一次建表后的增量合并，只把抛
#   CandidateIdentityConflictError 的那几条剔掉；其它校验错误照常上抛（原全有或全无语义不变）。
#   供 Curator 在整批提交前调用，一条冲突不再拖死整批、也不再让同一批反复重放。
# 函数用途: 把一批观察分成可提交的和身份冲突的，返回（可提交的观察, 剔除条数）。
def split_identity_conflicts(
    candidates: list[MemoryCandidate],
    observations: list[CandidateObservation] | tuple[CandidateObservation, ...],
) -> tuple[tuple[CandidateObservation, ...], int]:
    merged = list(candidates)
    bindings = TargetEvidenceVersionBindings(merged)
    by_id = {item.candidate_id: index for index, item in enumerate(merged)}
    by_observation = _observation_index(merged)
    kept: list[CandidateObservation] = []
    for observation in observations:
        try:
            prepared = _prepare_observations([observation])[0]
            _merge_indexed_observation(merged, by_id, by_observation, bindings, prepared)
        except CandidateIdentityConflictError:
            continue
        kept.append(observation)
    return tuple(kept), len(observations) - len(kept)


# LLM: 整批输入在任何候选写入前完成校验；目标版本属于观察身份，坏批次不得部分落盘。
# all-or-nothing behavior for both standalone and Curator transactions; target versions are part of mutation identity.
# 函数用途: 规范化 observation 并预先计算稳定 candidate/observation 身份。
def _prepare_observations(
    observations: list[CandidateObservation] | tuple[CandidateObservation, ...],
) -> list[tuple[CandidateObservation, str, str]]:
    if not observations:
        raise ValueError("candidate observations must be non-empty")
    prepared: list[tuple[CandidateObservation, str, str]] = []
    for observation in observations:
        normalized = _normalize_observation(observation)
        candidate_id = stable_candidate_id(
            candidate_type=normalized.candidate_type,
            content=normalized.content,
            subject_key=normalized.subject_key,
            scope=MemoryScope.from_value(normalized.scope),
            proposed_action=normalized.proposed_action,
            target_entry_id=normalized.target_entry_id,
        )
        if normalized.proposed_action in {"replace", "remove", "merge"}:
            candidate_id = versioned_candidate_id(candidate_id, normalized.target_entry_version)
        prepared.append(
            (normalized, candidate_id, stable_observation_key(normalized, candidate_id))
        )
    return prepared


# LLM: 调用方已持候选锁；已晋升引用不可改绑，其余状态只按唯一状态机裁决。
# 函数用途: 拒绝非法终态提交并保持幂等绑定。
def _require_promotable_binding(current: MemoryCandidate, ref: str) -> None:
    if current.status == "promoted":
        if current.promotion_ref != ref:
            raise CandidateTransitionError("promoted candidate cannot be rebound to a different ref")
        return
    if not candidate_transition_allowed(current.status, "promoted"):
        raise CandidateTransitionError(f"candidate transition {current.status} -> promoted is not allowed")


# LLM: 调用方已核验状态转换并持候选锁；新promoted和remove脱敏生成同一待写快照，未写前不改正式事实。
# 函数用途: 一次组装晋升终态及其必须清除的全部同hash隐私。
def _promoted_rows(candidates, index, review):
    reviewer, promotion_ref, review_note = review
    rows = list(candidates)
    current = rows[index]
    now = utc_now_iso()
    if current.status != "promoted":
        rows[index] = replace(current, status="promoted", reviewer=str(reviewer or "promotion-service").strip(),
            review_note=str(review_note or "").strip(), promoted_at=now,
            promotion_ref=str(promotion_ref).strip(), updated_at=now,
            status_history=[*current.status_history,
                _status_event("promoted", actor=str(reviewer or "promotion-service"), note=review_note, at=now)])
    if current.proposed_action == "remove":
        rows = [_redacted_candidate(row, hashes={current.content_hash}, excluded=set(),
                    reviewer=reviewer, now=now) or row for row in rows]
    return rows


# LLM: 硬删除先冻结约束，正文/引用已空也检查自由备注；固定宿主脱敏说明保留幂等，不依赖retention后补。
# 函数用途: 将命中hash的候选脱敏，活跃候选按原状态机转为superseded。
def _redacted_candidate(
    item: MemoryCandidate,
    *,
    hashes: set[str],
    excluded: set[str],
    reviewer: str,
    now: str,
) -> MemoryCandidate | None:
    if item.candidate_id in excluded or item.content_hash not in hashes:
        return None
    note = "正文及引用随正式记忆硬删除清除，仅保留身份与版本约束。"
    notes = [item.review_note, *(event.get("note", "") for event in item.status_history)]
    safe_notes = {"", note, "retained_version_bindings", "CURATOR_REJECTED_VERSION_BINDING"}
    if not any((item.content, item.evidence_refs, item.source_message_refs, item.source_tool_refs,
                item.source_artifact_refs, item.source_task_ids, item.source_run_ids,
                item.learning_source, item.learning_source_coverage)) and all(value in safe_notes for value in notes):
        return None
    target = item.status if item.status in TERMINAL_CANDIDATE_STATUSES else "superseded"
    history = [{**event, "note": ""} for event in item.status_history]
    if target != item.status and candidate_transition_allowed(item.status, target):
        history.append(_status_event(target, actor=reviewer, note=note, at=now))
    return replace(item, content="", status=target, reviewer=reviewer, review_note=note, cold_detail_ref="",
        updated_at=now, content_redacted_at=now, status_history=history,
        target_evidence_bindings=binding_history(item), evidence_refs=[], source_message_refs=[],
        source_tool_refs=[], source_artifact_refs=[], source_task_ids=[], source_run_ids=[],
        learning_source={}, learning_source_coverage={})


# LLM: quota 锁必须先于候选文件锁，和其他 owner 仓库保持统一锁序。
# 类用途: 为无 quota 的测试和有 quota 的正式运行提供同一上下文接口。
class _NullAdmission:
    # LLM: 无 quota 模式仍保留 check 调用点，不能在这里实现另一套容量策略。
    # 函数用途: 接受一次候选整文件容量检查并保持空操作。
    def check(self, _changes: list[OwnerQuotaChange]) -> None:
        return None


# LLM: 上下文管理只包 owner quota admission，不吞掉任何配额异常。
# 函数用途: 获取候选整文件 mutation 的 quota admission。
def _quota_admission(enforcer: OwnerQuotaEnforcer | None):
    if enforcer is not None:
        return enforcer.admission()

    # LLM: 本地上下文只模拟 admission 生命周期，不吞异常或更改候选结果。
    # 类用途: 在未配置 owner quota 时提供统一 with 接口。
    class _Context:
        # LLM: 进入时返回无状态 admission；不得写任何配额事实。
        # 函数用途: 开始无 quota 候选写入临界区。
        def __enter__(self) -> _NullAdmission:
            return _NullAdmission()

        # LLM: 退出时不拦截调用方异常，保证存储失败原样上抛。
        # 函数用途: 结束无 quota 候选写入临界区。
        def __exit__(
            self,
            exc_type: object,
            exc: object,
            traceback: object,
        ) -> None:
            return None

    return _Context()


# LLM: 写入用完整当前态原子 replace；不能 append 第二条相同 candidate_id。候选装记忆正文，走私有原子写（0600，目录 0700），
#   已有 0644 的旧文件下次写入即收紧；改动同步 test_memory_file_permissions。
# LLM: serialize/admission.check仍持原领域锁在Root外；C只把准备好的raw replace_once交原scope，默认字节/调用形状不变。
# 函数用途: 在owned副本归一化后原子写入，成功才发表新代缓存，失败/未知结果不保留缓存。
def _write_candidates_unlocked(
    path: Path,
    candidates: list[MemoryCandidate],
    *,
    admission: Any,
    coordinator_io: bool = False,
    cold_changes: dict[str, str] | None = None,
    cold_redaction: dict | None = None,
) -> None:
    owned = deepcopy(candidates)
    redacted = [row for row in owned if row.content_redacted_at and not row._raw_json_line]
    options = cold_redaction or {"hashes": {row.content_hash for row in redacted},
        "excluded": {row.candidate_id for row in owned if row.content}, "reviewer": "memory-hard-delete", "now": utc_now_iso()}
    archives = dict(cold_changes or {})
    archives.update(_cold_redaction_plan(path, options, archives))
    text = _serialize_candidates(owned)
    admission.check([OwnerQuotaChange(path, len(text.encode("utf-8"))), *[
        OwnerQuotaChange(path.parent / "candidates_archive" / name, len(content.encode("utf-8")))
        for name, content in archives.items()]])
    _drop_candidate_load_cache(path)
    try:
        _write_cold_changes(path, archives, coordinator_io=coordinator_io)
        if coordinator_io:
            _coordinator_candidate_write(lambda: write_private_text_file_atomic_unlocked(path, text, replace_once=True))
        else:
            write_private_text_file_atomic_unlocked(path, text)
        _publish_written_candidates(path, text, owned)
    except BaseException:
        _drop_candidate_load_cache(path)
        raise


# LLM: 仅散列正式字段的完整逻辑状态；pickle只作进程内指纹编码，从不反序列化或落盘。
# 函数用途: 捕捉嵌套引用/绑定改写，防止原行缓存隐藏没有经过字段赋值的变更。
def _candidate_state_digest(item: MemoryCandidate) -> str:
    values = tuple(getattr(item, name) for name, descriptor in item.__dataclass_fields__.items() if descriptor.init)
    return hashlib.blake2b(pickle.dumps(values, protocol=5), digest_size=16).hexdigest()


# LLM: 每条仅保留当前原行和小摘要；不缓存归一化前的行，坏schema必须在进入缓存前报错。
# 函数用途: 从单行JSON构造合法模型，并在完全无需改写时保留原行。
def _parse_candidate_line(raw: str, line_no: int = 0) -> MemoryCandidate:
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("candidate row must be an object")
    try:
        item = MemoryCandidate.from_record(payload)
        normalized = item.to_record()
    except (ValueError, TypeError) as error:
        raise CandidateStoreCorruptError(f"candidate ledger row {line_no} failed schema validation") from error
    if normalized == payload:
        item._raw_json_line = raw.strip()
        item._raw_state_digest = _candidate_state_digest(item)
    return item


# LLM: 未改行跳过to_record/asdict/JSON编码；完整状态摘要防嵌套变更，replace的新行重走严格归一化。
# 函数用途: 复用当前合法原行生成候选账本文本，不把旧隐私字段或审核状态写回。
def _serialize_candidates(candidates: Iterable[MemoryCandidate]) -> str:
    lines = []
    for item in candidates:
        if not item._raw_json_line or item._raw_state_digest != _candidate_state_digest(item):
            line = json.dumps(item.to_record(), ensure_ascii=False, sort_keys=True)
            item._raw_json_line = line
            item._raw_state_digest = _candidate_state_digest(item)
        lines.append(item._raw_json_line)
    return "\n".join(lines) + ("\n" if lines else "")


# LLM: 调用方已持候选锁且原子写成功，owned模型不能再交外部；stat不稳或超预算时只失效不发表。
# 函数用途: 从刚落盘文本建立解析及大文件摘要缓存，让后续读取不自砸缓存重读。
def _publish_written_candidates(path: Path, text: str, owned: list[MemoryCandidate]) -> None:
    stamp = _candidate_load_stamp(path)
    if stamp is None or stamp[1] > _CANDIDATE_LOAD_CACHE_MAX_BYTES or stamp[1] != len(text.encode("utf-8")):
        _drop_candidate_load_cache(path)
        return
    digest = hashlib.blake2b(text.encode("utf-8"), digest_size=16).hexdigest()
    if stamp[1] >= _CANDIDATE_FAST_STAT_PROBE_MIN_BYTES:
        with _CANDIDATE_LOAD_CACHE_LOCK:
            _CANDIDATE_STAMP_DIGEST_CACHE[str(path)] = (stamp, digest)
            _CANDIDATE_STAMP_DIGEST_CACHE.move_to_end(str(path))
            while len(_CANDIDATE_STAMP_DIGEST_CACHE) > _CANDIDATE_LOAD_CACHE_MAX_PATHS:
                _CANDIDATE_STAMP_DIGEST_CACHE.popitem(last=False)
    _remember_candidate_load(path, (stamp, digest), owned, copy_records=False)


# LLM: 排除标记只按结构化 candidate_id 命中；已带同原因码的条目跳过，写回由调用方在锁内一次性完成。
# 函数用途: 给命中的候选写排除原因码，返回实际改动条数。
def _mark_excluded_candidates(
    candidates: list[MemoryCandidate],
    ids: set[str],
    code: str,
    now: str,
) -> int:
    changed = 0
    for index, item in enumerate(candidates):
        if item.candidate_id not in ids:
            continue
        if str(item.promotion_exclusion or "").strip() == code:
            continue
        candidates[index] = replace(item, promotion_exclusion=code, updated_at=now)
        changed += 1
    return changed


# LLM: 初次观察保存来源覆盖并走统一初审；观察版本原样持久化，不在入库或晋升时重读当前版本。
# 函数用途: 构造一条新候选当前态和完整初始状态历史。
def _new_candidate(
    observation: CandidateObservation,
    candidate_id: str,
    observation_key: str,
) -> MemoryCandidate:
    now = utc_now_iso()
    observed_at = normalize_iso_time(observation.observed_at, default=now, allow_empty=False)
    status = _initial_review_status(observation)
    coverage = initial_source_coverage(observation)
    history = [_status_event("observed", actor="candidate-service", note="", at=now)]
    if status != "observed":
        history.append(
            _status_event(
                status,
                actor="candidate-service",
                note=_initial_review_note(status),
                at=now,
            )
        )
    return MemoryCandidate(
        schema_version=CANDIDATE_SCHEMA_VERSION,
        candidate_id=candidate_id,
        candidate_type=observation.candidate_type,
        content=observation.content,
        subject_key=observation.subject_key,
        scope=MemoryScope.from_value(observation.scope).to_dict(),
        origin=observation.origin,
        evidence_refs=normalize_reference_list(observation.evidence_refs),
        source_message_refs=normalize_reference_list(observation.source_message_refs),
        source_tool_refs=normalize_reference_list(observation.source_tool_refs),
        source_artifact_refs=normalize_reference_list(observation.source_artifact_refs),
        source_task_ids=normalize_string_list(observation.source_task_ids),
        source_run_ids=normalize_string_list(observation.source_run_ids),
        observed_at=observed_at,
        last_observed_at=observed_at,
        valid_from=normalize_iso_time(observation.valid_from),
        valid_until=normalize_iso_time(observation.valid_until),
        occurrence_count=1,
        confidence=float(observation.confidence),
        proposed_action=observation.proposed_action,
        target_entry_id=observation.target_entry_id,
        target_entry_version=observation.target_entry_version,
        conflicts_with=normalize_string_list(observation.conflicts_with),
        promotion_target=observation.promotion_target,
        promotion_mode=observation.promotion_mode,
        learning_source=source_coverage_projection(coverage),
        learning_source_coverage=coverage,
        status=status,
        reviewer="candidate-service" if status.startswith("blocked_") else "",
        review_note=_initial_review_note(status),
        created_at=now,
        updated_at=now,
        promoted_at="",
        promotion_ref="",
        content_hash=memory_content_hash(observation.content),
        content_redacted_at="",
        observation_keys=[observation_key],
        status_history=history,
    )


# LLM: 只有宿主证据门从未接受且未脱敏/晋升的草稿可重建；它仍贡献目标版本约束，但不占用合法业务身份。
# 函数用途: 区分可以重新申请的拒绝草稿与已有正式历史的候选。
def _rejected_draft(current: MemoryCandidate) -> bool:
    return (current.status == "blocked_missing_evidence" and current.reviewer == "evidence_gate"
            and not current.content_redacted_at and not current.promotion_ref)


# LLM: 首拒草稿仅用新证据重建；拒绝历史及旧来源—目标版本约束保留，旧引用/覆盖不贡献晋升资格。
# 函数用途: 用独立新观察重新申请同一候选，避免一次失败证据永久污染后续合法重试。
def _retry_rejected_draft(current: MemoryCandidate, observation: CandidateObservation,
                          observation_key: str) -> MemoryCandidate:
    fresh = _new_candidate(observation, current.candidate_id, observation_key)
    return replace(fresh, created_at=current.created_at,
                   target_evidence_bindings=binding_history(current),
                   promotion_exclusion=current.promotion_exclusion,
                   status_history=[*current.status_history, *fresh.status_history])


# LLM: 准入首拒草稿仅保留拒绝历史后以新观察重建；其余候选按来源覆盖合并，终态、审核与脱敏状态不变。
# 函数用途: 合并已接受的候选来源，并让从未接受的拒绝草稿可以合法重试。
def _merge_candidate(
    current: MemoryCandidate,
    observation: CandidateObservation,
    observation_key: str,
) -> MemoryCandidate:
    if not current.content and current.content_redacted_at:
        return current
    if current.status == "blocked_missing_evidence" and current.reviewer == "evidence_gate":
        return _retry_rejected_draft(current, observation, observation_key)
    observed_at = normalize_iso_time(
        observation.observed_at,
        default=utc_now_iso(),
        allow_empty=False,
    )
    observation_keys = list(current.observation_keys)
    is_new = observation_key not in observation_keys
    if is_new:
        observation_keys.append(observation_key)
    resolved_origin = _resolved_blocked_origin(current, observation)
    origin_repaired = resolved_origin != current.origin
    coverage = merge_source_coverage(current, observation)
    merged = replace(
        current,
        origin=resolved_origin,
        # 未知引用不能继承另一条可学消息的资格；人工入口无投影语义单独保留。
        learning_source=source_coverage_projection(coverage),
        learning_source_coverage=coverage,
        evidence_refs=_merge_refs(current.evidence_refs, observation.evidence_refs),
        source_message_refs=_merge_refs(
            current.source_message_refs, observation.source_message_refs
        ),
        source_tool_refs=_merge_refs(current.source_tool_refs, observation.source_tool_refs),
        source_artifact_refs=_merge_refs(
            current.source_artifact_refs, observation.source_artifact_refs
        ),
        source_task_ids=_merge_strings(current.source_task_ids, observation.source_task_ids),
        source_run_ids=_merge_strings(current.source_run_ids, observation.source_run_ids),
        last_observed_at=max(current.last_observed_at, observed_at),
        occurrence_count=len(observation_keys),
        confidence=max(float(current.confidence), float(observation.confidence)),
        conflicts_with=_merge_strings(current.conflicts_with, observation.conflicts_with),
        observation_keys=observation_keys,
        updated_at=utc_now_iso() if is_new else current.updated_at,
    )
    merged = replace(merged, promotion_mode=host_promotion_mode(merged))
    if (
        merged.status == "blocked_missing_evidence"
        and _candidate_has_structural_evidence(merged)
    ):
        reopened_at = utc_now_iso()
        repair_note = (
            f"structured_origin_repaired:{current.origin}->{resolved_origin}"
            if origin_repaired
            else "structured_evidence_restored"
        )
        merged = replace(
            merged,
            status="pending_review",
            reviewer="candidate-service",
            review_note="结构化来源证据已补齐，重新进入自主晋升链。",
            status_history=[
                *merged.status_history,
                _status_event(
                    "pending_review",
                    actor="candidate-service",
                    note=repair_note,
                    at=reopened_at,
                ),
            ],
            updated_at=reopened_at,
        )
    return merged


# LLM: A model label alone cannot rewrite provenance; correction requires the current candidate
# to be blocked with no matching refs and the incoming origin to carry its own canonical refs.
# 函数用途: 在缺证据终态下裁决能否把误标的 user_explicit/tool_verified 来源纠正为有真实引用的新来源。
def _resolved_blocked_origin(
    current: MemoryCandidate,
    observation: CandidateObservation,
) -> str:
    incoming = str(observation.origin or "").strip().lower()
    if (
        current.status != "blocked_missing_evidence"
        or incoming == current.origin
        or incoming not in {"user_explicit", "tool_verified"}
        or _candidate_has_structural_evidence(current)
    ):
        return current.origin
    if incoming == "user_explicit" and observation.source_message_refs:
        return incoming
    if incoming == "tool_verified" and observation.source_tool_refs:
        return incoming
    return current.origin


# LLM: This check only reopens a structurally missing-evidence candidate after exact references
# have been merged. Cryptographic/message/tool verification remains exclusively in PromotionService.
# 函数用途: 判断候选是否已补齐与来源类型匹配的最小结构化引用，以便自动重试而不是永久卡住。
def _candidate_has_structural_evidence(candidate: object) -> bool:
    origin = str(getattr(candidate, "origin", "") or "").strip().lower()
    if origin == "user_explicit":
        return bool(getattr(candidate, "source_message_refs", ()))
    if origin == "tool_verified":
        return bool(getattr(candidate, "source_tool_refs", ()))
    if origin in {"subagent_finding", "subagent_lesson"}:
        return bool(
            getattr(candidate, "source_task_ids", ())
            or getattr(candidate, "source_run_ids", ())
            or getattr(candidate, "evidence_refs", ())
        )
    return True


# LLM: 初始阻塞只依赖结构化来源/refs/action，不把 confidence 当事实证据。
# 函数用途: 判断新候选进入 pending_review 还是缺证据/冲突阻塞。
def _initial_review_status(observation: CandidateObservation) -> str:
    if observation.conflicts_with:
        return "blocked_conflict"
    if observation.proposed_action in {"replace", "remove", "merge"} and not observation.target_entry_id:
        return "blocked_conflict"
    if observation.origin == "user_explicit" and not observation.source_message_refs:
        return "blocked_missing_evidence"
    if observation.origin == "tool_verified" and not observation.source_tool_refs:
        return "blocked_missing_evidence"
    if observation.origin in {"subagent_finding", "subagent_lesson"} and not (
        observation.source_task_ids or observation.source_run_ids or observation.evidence_refs
    ):
        return "blocked_missing_evidence"
    return "pending_review"


# LLM: 阻塞说明是稳定诊断，不参与后续业务判断。
# 函数用途: 为初始状态保存简短审核说明。
def _initial_review_note(status: str) -> str:
    if status == "blocked_missing_evidence":
        return "缺少与 origin 对应的结构化证据引用。"
    if status == "blocked_conflict":
        return "存在冲突或替换目标不完整，需要人工审核。"
    return ""


# LLM: 新add须用规范typed scope；变更保留旧目标键供迁移修复，宿主观察版本必须是真实正整数或未知。
# 函数用途: 规范化一次候选观察，并在写候选账本前拒绝不可召回的新增 scope。
def _normalize_observation(observation: CandidateObservation) -> CandidateObservation:
    if not isinstance(observation, CandidateObservation):
        raise TypeError("candidate observation must use CandidateObservation")
    candidate_type = str(observation.candidate_type or "").strip().lower()
    origin = str(observation.origin or "").strip().lower()
    content = str(observation.content or "").strip()
    subject_key = str(observation.subject_key or "").strip()
    proposed_action = str(observation.proposed_action or "none").strip().lower()
    scope = (
        MemoryScope.for_new_observation(observation.scope)
        if proposed_action == "add"
        else MemoryScope.from_value(observation.scope)
    )
    promotion_target = str(observation.promotion_target or "none").strip().lower()
    supplied_promotion_mode = str(observation.promotion_mode or "").strip().lower()
    if candidate_type not in CANDIDATE_TYPES:
        raise ValueError(f"unsupported candidate_type: {candidate_type}")
    if origin not in CANDIDATE_ORIGINS:
        raise ValueError(f"unsupported candidate origin: {origin}")
    if proposed_action not in PROPOSED_ACTIONS:
        raise ValueError(f"unsupported proposed_action: {proposed_action}")
    if promotion_target not in PROMOTION_TARGETS:
        raise ValueError(f"unsupported promotion_target: {promotion_target}")
    # 非空非法值严格拒绝；合法旧值也由当前宿主合同重算，模型不能靠字段给自己升权。
    if supplied_promotion_mode and supplied_promotion_mode not in PROMOTION_MODES:
        raise ValueError(f"unsupported promotion_mode: {supplied_promotion_mode}")
    if not content or len(content) > 2_000:
        raise ValueError("candidate content must contain 1..2000 characters")
    if not subject_key and candidate_type not in {"event", "discard"}:
        raise ValueError("candidate subject_key is required for durable candidate types")
    confidence = float(observation.confidence)
    if not 0.0 <= confidence <= 1.0:
        raise ValueError("candidate confidence must be between 0 and 1")
    return replace(
        observation,
        candidate_type=candidate_type,
        content=content,
        subject_key=subject_key,
        scope=scope,
        origin=origin,
        proposed_action=proposed_action,
        promotion_target=promotion_target,
        promotion_mode=host_promotion_mode(observation),
        confidence=confidence,
        target_entry_id=str(observation.target_entry_id or "").strip(),
        target_entry_version=normalize_target_entry_version(observation.target_entry_version),
    )


# LLM: 合并 refs 复用 Candidate Schema 规范化与精确 JSON 身份，不做语义相似去重。
# 函数用途: 保留原顺序合并两组结构化引用。
def _merge_refs(
    current: list[dict[str, object]],
    incoming: object,
) -> list[dict[str, object]]:
    return normalize_reference_list([*current, *normalize_reference_list(incoming)])


# LLM: ID 合并只做精确去重，不根据前缀或文本近似合并任务。
# 函数用途: 合并来源 task/run/entry ID。
def _merge_strings(current: list[str], incoming: object) -> list[str]:
    return normalize_string_list([*current, *normalize_string_list(incoming)])


# LLM: 状态历史只记录状态、actor、note 和时间，不复制候选正文。
# 函数用途: 构造一条内嵌审核/晋升状态事件。
def _status_event(status: str, *, actor: str, note: object, at: str) -> dict[str, str]:
    return {
        "status": str(status or "").strip(),
        "actor": str(actor or "").strip(),
        "note": str(note or "").strip(),
        "at": normalize_iso_time(at, allow_empty=False),
    }


# LLM: 精确 ID 查找失败必须保留 KeyError，调用方不能退回第一条候选。
# 函数用途: 返回候选在当前列表中的索引。
def _candidate_index(candidates: list[MemoryCandidate], candidate_id: str) -> int:
    target = str(candidate_id or "").strip()
    for index, item in enumerate(candidates):
        if item.candidate_id == target:
            return index
    raise KeyError(target)


__all__ = [
    "CandidateService",
    "CandidateStoreCorruptError",
    "CandidateIdentityConflictError",
    "CandidateTransitionError",
    "RETENTION_ELIGIBLE_CANDIDATE_STATUSES",
    "merge_candidate_observations",
    "split_identity_conflicts",
]


# LLM: retention 保留版本绑定和硬删除防重放约束；草稿脱敏必须转 superseded，旧终态及版本绑定不变。
# 函数用途: 计算未晋升候选清理后的合法账本及实际清理条数。
def _retention_records(candidates: list[MemoryCandidate], ids: set[str]) -> tuple[list[MemoryCandidate], int]:
    retained: list[MemoryCandidate] = []
    cleaned = 0
    for item in candidates:
        if item.candidate_id not in ids:
            retained.append(item)
            continue
        history = binding_history(item)
        if not history and not item.content_redacted_at and not item.cold_detail_ref:
            cleaned += 1
            continue
        if _is_binding_tombstone(item):
            retained.append(item)
            continue
        now = utc_now_iso()
        status = item.status if item.status in TERMINAL_CANDIDATE_STATUSES else "superseded"
        retained.append(replace(item, status=status, content="", content_redacted_at=now, updated_at=now, cold_detail_ref="",
            evidence_refs=[], source_message_refs=[], source_tool_refs=[], source_artifact_refs=[],
            source_task_ids=[], source_run_ids=[], learning_source={}, learning_source_coverage={},
            target_evidence_bindings=history, reviewer="retention", review_note="retained_version_bindings",
            status_history=[_status_event(status, actor="retention", note="retained_version_bindings", at=now)]))
        cleaned += 1
    return retained, cleaned


# LLM: 空正文不等于清理完成；所有带引用/来源内容字段都必须为空，才能跳过retention。
# 函数用途: 判断当前记录是否已是没有原话的版本绑定或硬删除防重放墓碑。
def _is_binding_tombstone(item: MemoryCandidate) -> bool:
    return bool(item.content_redacted_at
        and item.reviewer == "retention" and item.review_note == "retained_version_bindings"
        and not any((item.content, item.evidence_refs, item.source_message_refs,
            item.source_tool_refs, item.source_artifact_refs, item.source_task_ids,
            item.source_run_ids, item.learning_source, item.learning_source_coverage)))


# LLM: 本函数不持source，仅消费原global scope；原candidate锁已由调用方持有，不能包merge/scan/embedding。
# 函数用途: 在C的当前真实末门执行一次准确目录/FD出生或已准备raw写。
def _coordinator_candidate_write(operation):
    return execute_global_write(operation, require_scope=True)


_COLD_MONTH = re.compile(r"[0-9]{4}-(?:0[1-9]|1[0-2])\.jsonl")
_COLD_REF = re.compile(r"candidates_archive/([0-9]{4}-(?:0[1-9]|1[0-2])\.jsonl)#(memory-candidate-[A-Za-z0-9_-]+):([0-9a-f]{64})")
_COLD_DETAIL_FIELDS = ("evidence_refs", "source_message_refs", "source_tool_refs", "source_artifact_refs",
                       "source_task_ids", "source_run_ids", "status_history", "learning_source", "learning_source_coverage")


# LLM: 固定候选根下逐段no-follow枚举，链接/坏目录不能当成不存在；锁文件等非归档文件不参与清理。
# 函数用途: 安全列出当前owner所有冷归档月份，包含热写失败留下的孤儿月份。
def _cold_archive_names(path: Path) -> list[str]:
    try:
        descriptor = open_directory_beneath(path.parent, ("candidates_archive",), require_dir_fd=True)
    except FileNotFoundError:
        return []
    except OSError as error:
        raise CandidateStoreCorruptError("candidate cold archive directory is unsafe") from error
    try:
        with os.scandir(descriptor) as entries:
            return sorted(entry.name for entry in entries if _COLD_MONTH.fullmatch(entry.name))
    finally:
        os.close(descriptor)


# LLM: hash包含受信owner规范根及完整正式记录；不把传入的ref当作owner授权或任意文件路径。
# 函数用途: 创建同owner归档详情的内容校验信封。
def _cold_envelope(path: Path, item: MemoryCandidate) -> dict:
    record = replace(item, cold_detail_ref="").to_record()
    material = {"owner_root": str(path.parent.resolve()), "candidate_id": item.candidate_id, "record": record}
    digest = hashlib.sha256(json.dumps(material, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return {**material, "sha256": digest}


# LLM: 先核owner/hash再解析模型；信封、记录及引用ID须一致，归档不得递归引用别的详情。
# 函数用途: 验证单条归档详情的完整性、身份和来源覆盖。
def _validate_cold_envelope(path: Path, envelope: object) -> None:
    if not isinstance(envelope, dict) or set(envelope) != {"owner_root", "candidate_id", "record", "sha256"}:
        raise ValueError("invalid candidate cold envelope")
    material = {key: envelope[key] for key in ("owner_root", "candidate_id", "record")}
    digest = hashlib.sha256(json.dumps(material, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    if envelope["owner_root"] != str(path.parent.resolve()) or envelope["sha256"] != digest:
        raise ValueError("candidate cold owner/hash mismatch")
    item = MemoryCandidate.from_record(envelope["record"])
    if item.candidate_id != envelope["candidate_id"] or item.cold_detail_ref:
        raise ValueError("candidate cold identity mismatch")


# LLM: 同根no-follow FD逐行校验，包括单链接普通文件约束；坏行/重复版本不能跳过或当空详情。
# 函数用途: 读取完整月份用于维护或精确详情检索，缺失仅在创建新月份时显式允许。
def _read_cold_records(path: Path, name: str, *, missing_ok: bool = False) -> list[dict]:
    if not _COLD_MONTH.fullmatch(name):
        raise CandidateStoreCorruptError("candidate cold archive filename is invalid")
    try:
        descriptor = open_readonly_file_beneath(path.parent, ("candidates_archive", name))
        records, seen = [], set()
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            for raw in stream:
                if not raw.strip():
                    continue
                envelope = json.loads(raw)
                _validate_cold_envelope(path, envelope)
                key = envelope["candidate_id"], envelope["sha256"]
                if key in seen:
                    raise ValueError("duplicate candidate cold version")
                seen.add(key)
                records.append(envelope)
        return records
    except FileNotFoundError as error:
        if missing_ok:
            return []
        raise CandidateStoreCorruptError("candidate cold detail is missing") from error
    except (OSError, UnicodeError, ValueError, TypeError, KeyError) as error:
        raise CandidateStoreCorruptError("candidate cold detail is corrupt") from error


# LLM: 精确ref版本且核心身份/版本绑定一致才补白名单详情；热状态/计数/排除标记不被旧冷快照覆盖。
# 函数用途: 为get/详情和指定写入目标透明回填独立的完整候选，缺失或篡改明确报损坏。
def _hydrate_candidate(path: Path, item: MemoryCandidate) -> MemoryCandidate:
    if not item.cold_detail_ref:
        return item
    match = _COLD_REF.fullmatch(item.cold_detail_ref)
    if match is None or match[2] != item.candidate_id:
        raise CandidateStoreCorruptError("candidate cold reference is invalid")
    records = _read_cold_records(path, match[1])
    envelope = next((row for row in records if row["candidate_id"] == match[2] and row["sha256"] == match[3]), None)
    if envelope is None:
        raise CandidateStoreCorruptError("candidate cold version is missing")
    detail = MemoryCandidate.from_record(envelope["record"])
    metadata = {*_COLD_DETAIL_FIELDS, "cold_detail_ref", "target_evidence_bindings", "promotion_exclusion", "updated_at"}
    if any(getattr(item, name) != getattr(detail, name) for name, field in item.__dataclass_fields__.items()
           if field.init and name not in metadata) or item.target_evidence_bindings != binding_history(detail):
        raise CandidateStoreCorruptError("candidate hot/cold identity mismatch")
    result = replace(item, **{name: deepcopy(getattr(detail, name)) for name in _COLD_DETAIL_FIELDS})
    result.to_record()
    return result


# LLM: 只对可能命中的冷行回填并解除ref，不让每次写操作全表读取冷详情；pure merge仍不承担I/O。
# 函数用途: 在观察合并前恢复其涉及的候选详情，避免重放丢历史或旧ref遮住新证据。
def _hydrate_observation_targets(path: Path, rows: list[MemoryCandidate], observations) -> list[MemoryCandidate]:
    if not observations or not any(row.cold_detail_ref for row in rows):
        return rows
    prepared = _prepare_observations(observations)
    ids, keys = {item[1] for item in prepared}, {item[2] for item in prepared}
    return [replace(_hydrate_candidate(path, row), cold_detail_ref="")
            if row.cold_detail_ref and (row.candidate_id in ids or keys.intersection(row.observation_keys)) else row
            for row in rows]


# LLM: 按最后变更时间冷却；默认覆盖全部终态，delete_terminal显式只选未晋升终态；负数量阈值拒绝。
# 函数用途: 在调用方允许的终态中，选择超过年龄或容量阈值且至少冷却一天的热行。
def _terminal_compaction_indices(rows, now, cutoff, maximum, *, statuses=frozenset({"promoted", "superseded", "rejected"})):
    if type(maximum) is not int or maximum < 0:
        raise ValueError("max_hot_terminal must be a nonnegative integer")
    eligible = [index for index, row in enumerate(rows) if row.status in statuses
                and not row.cold_detail_ref and not row.content_redacted_at]
    eligible.sort(key=lambda index: (rows[index].updated_at, rows[index].candidate_id))
    overflow = max(0, len(eligible) - maximum)
    return [index for position, index in enumerate(eligible)
            if datetime.fromisoformat(rows[index].updated_at) < min(cutoff, now - timedelta(days=1))
            or position < overflow and datetime.fromisoformat(rows[index].updated_at) < now - timedelta(days=1)]


# LLM: 每月保持全版本详情且按ID/hash幂等，归档先于热替换；冻结绑定后清引用，保持去重与schema不变量。
# 函数用途: 生成私有月归档文本和合法热stub，尚不执行任何写入。
def _prepare_cold_compaction(path, rows, indices):
    result, months = list(rows), {}
    for index in indices:
        item = rows[index]
        name = item.updated_at[:7] + ".jsonl"
        if name not in months:
            months[name] = _read_cold_records(path, name, missing_ok=True)
        envelope = _cold_envelope(path, item)
        if envelope not in months[name]:
            months[name].append(envelope)
        reference = f"candidates_archive/{name}#{item.candidate_id}:{envelope['sha256']}"
        cleared = {field: [] for field in _COLD_DETAIL_FIELDS if field not in {"status_history", "learning_source", "learning_source_coverage"}}
        result[index] = replace(item, **cleared, cold_detail_ref=reference, target_evidence_bindings=binding_history(item),
            status_history=[item.status_history[-1]], learning_source_coverage={})
        result[index].to_record()
    return result, {name: _cold_records_text(records) for name, records in months.items()}


# LLM: 所有归档版本均用固定信封编码；不得把私有raw缓存或任意对象写进冷文件。
# 函数用途: 生成一个月的完整候选详情JSONL文本。
def _cold_records_text(records: list[dict]) -> str:
    return "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in records)


# LLM: 显式删除意图独立于热行存在；同月新增归档先合入再脱敏，不能用旧磁盘计划覆盖新ref。
# 函数用途: 为正式删除或retention生成不再残留原正文与quote的冷归档替换计划。
def _cold_redaction_plan(path: Path, options: dict, staged: dict[str, str] | None = None) -> dict[str, str]:
    if not options.get("hashes") and not options.get("candidate_ids"):
        return {}
    changes = {}
    staged = staged or {}
    for name in sorted(set(_cold_archive_names(path)) | staged.keys()):
        records = ([json.loads(raw) for raw in staged[name].splitlines() if raw.strip()]
                   if name in staged else _read_cold_records(path, name))
        replacements, changed = {}, False
        for envelope in records:
            _validate_cold_envelope(path, envelope)
            item = MemoryCandidate.from_record(envelope["record"])
            hashes = ({item.content_hash} if item.candidate_id in options.get("candidate_ids", ()) else options.get("hashes", set()))
            redacted = _redacted_candidate(item, hashes=hashes, excluded=options.get("excluded", set()),
                reviewer=str(options.get("reviewer") or "memory-hard-delete"), now=options["now"])
            updated = _cold_envelope(path, redacted) if redacted is not None else envelope
            changed |= redacted is not None
            replacements[(updated["candidate_id"], updated["sha256"])] = updated
        if changed:
            changes[name] = _cold_records_text(list(replacements.values()))
    return changes


# LLM: 私有no-follow原子写/fsync后以64KiB块完整SHA256读回；兼容Python3.10，验证失败不继续提交热行。
# 函数用途: 沿原quota和coordinator末门写冷归档，读回核对字节后才能替换热记录。
def _write_cold_changes(path: Path, changes: dict[str, str], *, coordinator_io: bool) -> None:
    for name, text in sorted(changes.items()):
        if not _COLD_MONTH.fullmatch(name):
            raise CandidateStoreCorruptError("candidate cold archive filename is invalid")
        # LLM: 同步消费本轮已准备的固定路径/文本，不保存活来源，也不把规划放入权限末门。
        # 函数用途: 在原scope中执行当前月份的私有原子替换。
        def operation():
            write_text_atomic_beneath(path.parent, ("candidates_archive", name), text)
        if coordinator_io:
            _coordinator_candidate_write(operation)
        else:
            operation()
        descriptor = open_readonly_file_beneath(path.parent, ("candidates_archive", name))
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "rb") as stream:
            while chunk := stream.read(65536):
                digest.update(chunk)
        if digest.hexdigest() != hashlib.sha256(text.encode()).hexdigest():
            raise CandidateStoreCorruptError("candidate cold archive write verification failed")


# LLM: 持原候选锁逐行校验并累积同FD摘要；只持当前字典，重复ID或任一坏行均拒绝完整加载。
# 函数用途: 单遍构造模型和稳定磁盘探针，避免整表原文及中间字典列表同时驻留。
def _parse_candidate_ledger(path: Path):
    stamp = _candidate_load_stamp(path)
    records = []
    digest, ids = hashlib.blake2b(digest_size=16), set()
    try:
        descriptor = open_readonly_file_beneath(path.parent, (path.name,))
        with os.fdopen(descriptor, "r", encoding="utf-8", newline="") as stream:
            for line_no, raw in enumerate(stream, start=1):
                digest.update(raw.encode("utf-8"))
                if not raw.strip():
                    continue
                candidate = _parse_candidate_line(raw, line_no)
                if candidate.candidate_id in ids:
                    raise CandidateStoreCorruptError("candidate ledger contains duplicate candidate_id")
                ids.add(candidate.candidate_id)
                records.append(candidate)
    except FileNotFoundError:
        if stamp is not None:
            raise CandidateStoreCorruptError("candidate ledger disappeared during read") from None
    except (OSError, UnicodeError, ValueError, TypeError) as error:
        raise CandidateStoreCorruptError("candidate ledger contains unreadable row(s)") from error
    probe = ((stamp, digest.hexdigest()) if stamp is not None and stamp[1] <= _CANDIDATE_LOAD_CACHE_MAX_BYTES
             and _candidate_load_stamp(path) == stamp else None)
    return probe, records


# LLM: quota先于原候选锁；所有冷计划在热写前统一准入，失败保留可重试的完整热行且失效缓存。
# 函数用途: 编排终态归档准备与archive-first提交，不扩大服务类职责。
def _compact_terminal_candidates(service, before, maximum, now):
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cutoff = (before or current - timedelta(days=30)).astimezone(timezone.utc)
    with _quota_admission(service.quota_enforcer) as admission, service._locked_candidates():
        rows = service._load_unlocked()
        selected = _terminal_compaction_indices(rows, current, cutoff, maximum)
        if not selected:
            return 0
        rows, archives = _prepare_cold_compaction(service.path, rows, selected)
        _write_candidates_unlocked(service.path, rows, admission=admission, cold_changes=archives,
            **({} if not service.coordinator_io else {"coordinator_io": True}))
        return len(selected)


# LLM: 原quota/锁内提交精确删除与冷清理；统一使用传入now，只顺手冷归档未晋升终态，promoted不变。
# 函数用途: 按本次时钟完成候选清理和有限冷热维护，不误把边界行变老或改写已晋升记录。
def _commit_retention_rows(service, candidates, ids, admission, now=None):
    retained, deleted = _retention_records(candidates, ids)
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    selected = _terminal_compaction_indices(retained, current, current - timedelta(days=30), 1000,
                                            statuses={"rejected", "superseded"})
    retained, archives = _prepare_cold_compaction(service.path, retained, selected)
    options = {"candidate_ids": ids, "hashes": set(), "excluded": set(), "reviewer": "retention", "now": current.isoformat()}
    archives.update(_cold_redaction_plan(service.path, options, archives))
    if retained != candidates or archives:
        _write_candidates_unlocked(service.path, retained, admission=admission, cold_changes=archives, cold_redaction=options,
            **({} if not service.coordinator_io else {"coordinator_io": True}))
    return deleted


# LLM: 热stub与完整get快照都做精确逻辑比较；不剥去预期详情猜相等，冷校验失败原样拒绝。
# 函数用途: 保留冷热分离后的retention快照CAS，拒绝被改写的预期证据或历史。
def _candidate_snapshot_matches(path: Path, current: MemoryCandidate, expected: MemoryCandidate) -> bool:
    if current == expected:
        return True
    return bool(current.cold_detail_ref and _hydrate_candidate(path, current) == expected)
