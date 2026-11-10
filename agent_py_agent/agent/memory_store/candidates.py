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
import sys
import threading
from collections import OrderedDict
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from itertools import islice
from pathlib import Path
from typing import Any

from ..common.global_write_commit import execute_global_write
from ..common.json_io import (
    locked_json_path,
    read_jsonl_objects_report,
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
from .candidate_ledger import (
    CandidateIdPositions,
    CandidateRows,
    IndexedBindings,
    ObservationPositions,
    iter_candidate_rows,
    manifest_text,
    read_manifest,
    seal_cleanup,
)
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
# 单批候选审核/晋升/终态提交最多处理的条数。
PROMOTION_BATCH_MAX_COUNT = 32


# LLM: 扫描、执行及锁内仓库末门共用；approved及任何提交义务不受年龄覆盖。
# 函数用途: 唯一裁定一个候选是否可按龄清理。
def candidate_retention_eligible(candidate):
    return (candidate.status in RETENTION_ELIGIBLE_CANDIDATE_STATUSES
            and candidate.status != "approved" and not candidate.commit_obligation
            and not _is_binding_tombstone(candidate))

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
    str, tuple[tuple[int, int, int, int], str, tuple[MemoryCandidate, ...], dict[str, MemoryCandidate], int]
] = OrderedDict()
_CANDIDATE_LOAD_CACHE_LOCK = threading.Lock()
# 旧注入点仅在显式替换时接管冷读；正常路径保持M1同FD流式解析。
_DEFAULT_JSONL_READER = read_jsonl_objects_report


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


# LLM: 只读模型直接接管，不复制整账本；锁外计量原行/索引及条目元数据，锁内加容器开销，超额不入驻。
# 函数用途: 缓存严格读验证的同代候选及ID索引，保持最多8路径、128MiB实际对象预算。
def _remember_candidate_load(path, probe, records):
    if probe is None or _candidate_load_stamp(path) != probe[0]:
        return
    snapshot = tuple(records)
    by_id = {row.candidate_id: row for row in snapshot}
    key, stamp = str(path), probe[0][:4]
    retained = _candidate_retained_bytes((key, stamp, probe[1], snapshot, by_id, 0)) + 64
    with _CANDIDATE_LOAD_CACHE_LOCK:
        _CANDIDATE_LOAD_CACHE.pop(str(path), None)
        if retained > _CANDIDATE_LOAD_CACHE_MAX_BYTES:
            return
        _CANDIDATE_LOAD_CACHE[str(path)] = (probe[0][:4], probe[1], snapshot, by_id, retained)
        _CANDIDATE_LOAD_CACHE.move_to_end(str(path))
        while (len(_CANDIDATE_LOAD_CACHE) > _CANDIDATE_LOAD_CACHE_MAX_PATHS
               or sys.getsizeof(_CANDIDATE_LOAD_CACHE) + sum(entry[4] for entry in _CANDIDATE_LOAD_CACHE.values())
               > _CANDIDATE_LOAD_CACHE_MAX_BYTES):
            _CANDIDATE_LOAD_CACHE.popitem(last=False)


# LLM: 迭代计量只跟随候选JSON容器和实例字典，共享对象按身份去重，不计类全局或调用方栈。
# 函数用途: 计算缓存实际保留的对象树大小，避免拿文件长度代替内存用量。
def _candidate_retained_bytes(value):
    total, seen, pending = 0, set(), [value]
    while pending:
        item = pending.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        total += sys.getsizeof(item)
        if isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, (list, tuple, set, frozenset)):
            pending.extend(item)
        elif isinstance(item, MemoryCandidate):
            pending.append(vars(item))
    return total


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
    manifest = read_manifest(service.path)
    if manifest is not None:
        rows = CandidateRows(service.path, manifest)
        try:
            position = rows.index_for(candidate_id, None)
            return _hydrate_candidate(service.path, rows[position]) if position is not None else None
        except (OSError, ValueError, TypeError, KeyError) as error:
            raise CandidateStoreCorruptError('candidate indexed ledger is unreadable') from error
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
    _remember_candidate_load(service.path, probe, snapshot)
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
# LLM: 写前失效、成功后仅发表字节探针，读验证后才发表模型缓存；readonly_rows仅内部借用，公开返回深复制隔离。
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

    # LLM: 批观察全部验证后才在同一锁内替换账本；调用方不得用逐条调用冒充批次原子性。
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
            header = read_manifest(self.path)
            if (header is not None and statuses is not None and '_load_unlocked' not in self.__dict__
                    and getattr(self._load_unlocked, '__func__', None) is CandidateService._load_unlocked):
                selected = CandidateRows(self.path, header).select_statuses(statuses)
                return deepcopy(_candidate_read_slice(selected, statuses, limit))
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
            header = read_manifest(self.path)
            if header is not None and statuses is not None:
                return tuple(_candidate_read_slice(CandidateRows(self.path, header).select_statuses(statuses), statuses, limit))
            return tuple(_candidate_read_slice(_borrow_candidate_readonly_rows(self), statuses, limit))

    # LLM: candidate_id 是唯一定位键；禁止按正文或 subject 模糊选中审核目标。
    #   默认走锁内单条定位，命中缓存时只深拷贝目标候选而不再深拷贝和排序全表；若 list 被外部替身替换则保留原遍历。
    # 函数用途: 读取一个候选，不存在时抛出精确 KeyError。
    def readonly_window(self, cursor=None, *, references=(), max_records=128):
        """后台公平选源窗口；不全读状态组、不迁移或改写候选，旧JSONL用受核字节续点。"""
        from .candidate_ledger import read_candidate_window
        if ("list" in self.__dict__ or getattr(self.list, "__func__", None) is not CandidateService.list
                or "_load_unlocked" in self.__dict__
                or getattr(self._load_unlocked, "__func__", None) is not CandidateService._load_unlocked):
            rows = self.readonly_rows()
            if references:
                wanted = {ref['candidate_id'] for ref in references}
                rows = tuple(row for row in rows if row.candidate_id in wanted)
            return rows[:max_records], tuple({'candidate_id': row.candidate_id} for row in rows[:max_records]), {}, True
        with self._locked_candidates():
            return read_candidate_window(self.path, read_manifest(self.path), cursor,
                references=references, max_records=max_records)

    def anchor_rows(self, scopes):
        """精确前十二锚点；旧投影分批发表，片间释放原锁且只在完整后交付。"""
        from .candidate_anchor_index import read_anchors, ready, source_valid
        reset = False
        while True:
            with self._locked_candidates():
                header = read_manifest(self.path)
                if header is not None:
                    rows = CandidateRows(self.path, header)
                    if ready(rows):
                        return read_anchors(rows, scopes)
                    if not rows:
                        return ()
            with _quota_admission(self.quota_enforcer) as admission, self._locked_candidates():
                rows = self._load_unlocked()
                if not isinstance(rows, CandidateRows):
                    if not rows:
                        return ()
                    # 原JSONL迁移沿唯一写口，失败不发表新头；不另建权威或截断旧数据。
                    _write_candidates_unlocked(self.path, rows, admission=admission, coordinator_io=self.coordinator_io)
                    rows = self._load_unlocked()
                if ready(rows):
                    return read_anchors(rows, scopes)
                valid = source_valid(rows)
                if not valid:
                    if reset:
                        raise RuntimeError('candidate anchor source changed again during rebuild')
                    reset = True
                start = (rows.index.get(rows.root, 'anchor/progress') or 0) if valid else 0
                if type(start) is not int or not 0 <= start <= len(rows):
                    raise CandidateStoreCorruptError('candidate anchor progress is invalid')
                rows.anchor_build_end = min(len(rows), start + 128)
                rows.anchor_build_positions = range(start, rows.anchor_build_end)
                for position in rows.anchor_build_positions:
                    rows[position]
                _write_candidates_unlocked(self.path, rows, admission=admission, coordinator_io=self.coordinator_io)

    def filter_observation_conflicts(self, observations, *, rejected_bindings=()):
        """在原候选锁内按索引预演观察，不复制全部历史或提前写入。"""
        from .curator_rejected_bindings import retain_rejected_target_bindings
        with self._locked_candidates():
            rows = retain_rejected_target_bindings(self._load_unlocked(), rejected_bindings)
            return split_identity_conflicts(rows, observations)

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
                updated = _transition_candidate(current, target, reviewer=actor, review_note=review_note,
                    proposed_action=proposed_action, target_entry_id=target_entry_id, conflicts_with=conflicts_with,
                    promotion_target=promotion_target, snapshot_checked=expected is not None)
                if updated is current:
                    return current
                candidates[index] = updated
                _write_candidates_unlocked(self.path, candidates, admission=admission, **({} if not self.coordinator_io else {"coordinator_io": True}))
                return updated

    # LLM: 批次最多32条，逐项完整快照CAS；合法转换共用单条状态实现，一次准入/一次耐久替换后才返回成功。
    # 函数用途: 批量批准或阻塞候选，坏快照只返回该项原异常，不覆盖同批其他合法状态。
    def transition_batch(self, transitions, *, reviewer: str):
        changes = bounded_candidate_batch(transitions)
        actor = str(reviewer or "").strip()
        if not actor:
            raise ValueError("candidate reviewer is required")
        if not changes:
            return ()
        _unique_candidate_batch([item[0] for item in changes])
        with _quota_admission(self.quota_enforcer) as admission, self._locked_candidates():
            rows = self._load_unlocked()
            indices = CandidateIdPositions(rows) if isinstance(rows, CandidateRows) else {row.candidate_id: i for i, row in enumerate(rows)}
            results, changed = [], False
            for expected, status, options in changes:
                try:
                    index = indices[expected.candidate_id]
                    current = _hydrate_candidate(self.path, rows[index])
                    if current != expected:
                        raise CandidateStateChangedError(current.status)
                    updated = _transition_candidate(current, status, reviewer=actor,
                        snapshot_checked=True, **options)
                    changed |= updated is not current
                    rows[index] = updated
                    results.append(updated)
                except (KeyError, CandidateStateChangedError, CandidateTransitionError) as exc:
                    results.append(exc)
            if changed:
                _write_candidates_unlocked(self.path, rows, admission=admission,
                    **({} if not self.coordinator_io else {"coordinator_io": True}))
            return tuple(results)

    # LLM: quota先于candidate；逐项CAS与单条_promoted_rows共用终态及硬删除规则，不新建批次状态账。
    # 函数用途: 一次耐久提交成功引用及失败退避；写入失败时原approved/commit_obligation保持可恢复。
    def commit_batch(self, expected_candidates, *, reviewer: str, promotion_refs: dict[str, str],
                     deferred_reasons: dict[str, str] | None = None):
        expected_candidates = bounded_candidate_batch(expected_candidates)
        _unique_candidate_batch(expected_candidates)
        if not expected_candidates:
            return ()
        with _quota_admission(self.quota_enforcer) as admission, candidate_redaction_lock(
                self.path, **({} if not self.coordinator_io else {"coordinator_io": True})):
            rows, results, hashes = self._load_unlocked(), [], set()
            indices = CandidateIdPositions(rows) if isinstance(rows, CandidateRows) else {row.candidate_id: i for i, row in enumerate(rows)}
            changed = False
            for expected in expected_candidates:
                try:
                    index = indices[expected.candidate_id]
                    current = _hydrate_candidate(self.path, rows[index])
                    if current != expected:
                        raise CandidateStateChangedError(current.status)
                    ref = str(promotion_refs.get(expected.candidate_id) or "").strip()
                    reason = (deferred_reasons or {}).get(expected.candidate_id, "")
                    if ref:
                        _require_promotable_binding(current, ref)
                        rows[index] = current
                        rows = _promoted_rows(rows, index, (reviewer, ref, ""))
                        if current.proposed_action == "remove":
                            hashes.add(current.content_hash)
                    elif reason and current.status not in TERMINAL_CANDIDATE_STATUSES:
                        rows[index] = _deferred_candidate(current, reason)
                    changed |= rows[index] != current
                    results.append(rows[index])
                except (KeyError, CandidateStateChangedError, CandidateTransitionError) as exc:
                    results.append(exc)
            if changed or hashes:
                cold = {"cold_redaction": {"hashes": hashes, "excluded": set(), "reviewer": reviewer,
                                           "now": utc_now_iso()}} if hashes else {}
                _write_candidates_unlocked(self.path, rows, admission=admission, **cold,
                    **({} if not self.coordinator_io else {"coordinator_io": True}))
            return tuple(results)

    # LLM: 仅合并候选批准与默认落点，不授予正式发布权限；snapshot CAS及原状态机/配额/锁仍由transition执行。
    # 函数用途: 给自动晋升调用者提供一次写盘的准备入口，代替补落点后再次批准。
    def prepare_automatic_promotion(self, candidate_id: str | MemoryCandidate, *, reviewer: str,
                                    reason: str = "") -> MemoryCandidate:
        from .candidate_models import effective_promotion_target

        expected = candidate_id if isinstance(candidate_id, MemoryCandidate) else self.get(candidate_id)
        target = effective_promotion_target(expected.promotion_target, expected.origin, expected.candidate_type)
        return self.transition(expected, "approved", reviewer=reviewer, review_note=reason, promotion_target=target)

    # LLM: 在所有正式锁释放后按完整快照CAS记履约退避；不覆盖并发批准、拒绝或成功终态。
    # 函数用途: 把暂时提交失败留在唯一候选账本，保留证据供后续安全重试。
    def defer_commit(self, expected: MemoryCandidate, reason: str) -> MemoryCandidate:
        with _quota_admission(self.quota_enforcer) as admission, self._locked_candidates():
            rows = self._load_unlocked()
            index = _candidate_index(rows, expected.candidate_id)
            if rows[index] != expected:
                raise CandidateStateChangedError(rows[index].status)
            if expected.status in TERMINAL_CANDIDATE_STATUSES:
                return expected
            rows[index] = _deferred_candidate(expected, reason)
            _write_candidates_unlocked(self.path, rows, admission=admission,
                **({} if not self.coordinator_io else {"coordinator_io": True}))
            return rows[index]

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
            header = read_manifest(self.path)
            if header is not None:
                _finish_candidate_cleanup(self.path, header, coordinator_io=self.coordinator_io, admissions=(admission,))
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

    # LLM: 未终态按最后观察计龄，审核更新时间不冒充新证据；此入口只规划不删除。
    # 函数用途: 规划真正陈旧候选ID，保护最近再次观察到的候选。
    def retention_candidates(self, *, before: datetime) -> list[str]:
        cutoff = before.astimezone(timezone.utc)
        result: list[str] = []
        for item in self.readonly_rows(statuses=RETENTION_ELIGIBLE_CANDIDATE_STATUSES):
            if not candidate_retention_eligible(item):
                continue
            if datetime.fromisoformat(candidate_retention_time(item)) < cutoff:
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
                            and candidate_retention_eligible(current)):
                        continue
                    ids.remove(cid)
                    if skipped_state_changed is not None:
                        skipped_state_changed.add(cid)
            if not ids:
                return 0
            for item in candidates:
                if item.candidate_id in ids and not _is_binding_tombstone(item) and not candidate_retention_eligible(item):
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
                header = read_manifest(self.path)
                if (header is not None and '_load_unlocked' not in self.__dict__
                        and getattr(self._load_unlocked, '__func__', None) is CandidateService._load_unlocked):
                    rows = CandidateRows(self.path, header)
                    counts = rows.index.get(rows.root, 'counts') or {'statuses': {}, 'types': {}}
                    if (set(counts) != {'statuses', 'types'} or any(not isinstance(values, dict)
                            or any(type(key) is not str or type(value) is not int or value < 0 for key, value in values.items())
                            or sum(values.values()) != header['count'] for values in counts.values())):
                        raise CandidateStoreCorruptError('candidate counters are invalid')
                    return {'schema_version': CANDIDATE_SCHEMA_VERSION, 'total': header['count'],
                            'by_status': dict(sorted(counts['statuses'].items())),
                            'by_type': dict(sorted(counts['types'].items()))}
                borrowed = _borrow_candidate_readonly_rows(self)
                candidates = borrowed if borrowed is not None else self._load_unlocked()
        return _candidate_stats_from_rows(candidates)

    # LLM: 只返回原同path锁，C透传准确FD出生，不缓存活来源或缩短锁内业务；默认调用形状不加False。
    # 函数用途: 给候选原读写算法取得相同双层锁，等待与全文件读取都门外。
    def _locked_candidates(self):
        options = {} if not self.coordinator_io else {"birth_commit": _coordinator_candidate_write}
        return locked_json_path(self.path, **options)

    # LLM: 热缓存只复制写方所需的独占模型；冷解析直接归写方，不再为了缓存复制整账本。
    # 函数用途: 在原文件锁内取得可安全修改的候选列表，读侧缓存由只读解析独立建立。
    def _load_unlocked(self) -> list[MemoryCandidate]:
        manifest = read_manifest(self.path)
        if manifest is not None:
            return CandidateRows(self.path, manifest)
        probe = _candidate_load_probe(self.path)
        cached = _cached_candidate_rows(self.path, probe, evict_on_miss=True)
        if cached is not None:
            return list(deepcopy(cached))
        _probe, records = self._parse_candidate_ledger_unlocked()
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


# LLM: 迭代器最多消费33项，超界在任何IO之前拒绝，不把无穷输入整表装入内存。
# 函数用途: 为批准、晋升和终态提交共用固定批次上限。
def bounded_candidate_batch(items):
    rows = tuple(islice(iter(items), PROMOTION_BATCH_MAX_COUNT + 1))
    if len(rows) > PROMOTION_BATCH_MAX_COUNT:
        raise ValueError(f"candidate batch exceeds {PROMOTION_BATCH_MAX_COUNT} items")
    return rows


# LLM: 批次只认完整候选快照；重复ID不能在同一批借前项审核结果推进第二次。
# 函数用途: 拒绝无比较令牌或重复身份的批次。
def _unique_candidate_batch(rows):
    if any(not isinstance(row, MemoryCandidate) for row in rows):
        raise TypeError("candidate batch requires MemoryCandidate snapshots")
    if len({row.candidate_id for row in rows}) != len(rows):
        raise ValueError("candidate batch requires unique candidate IDs")


# LLM: 单条与批次共用原状态图、默认落点修复和义务公式；纯计算，锁/CAS/写盘由调用者承担。
# 函数用途: 构造一次合法的审核状态变更，不把批准只保留在内存中对外回执。
def _transition_candidate(current, target_status, *, reviewer, review_note="", proposed_action=None,
                          target_entry_id=None, conflicts_with=None, promotion_target=None, snapshot_checked=False):
    target = str(target_status or "").strip().lower()
    default_repair = (snapshot_checked and current.status == "approved"
        and current.promotion_target in {"", "none"} and promotion_target == "long_term"
        and current.origin == "user_explicit" and current.candidate_type in {"long_term_fact", "event", "project", "working_agreement"}
        and proposed_action is None and target_entry_id is None and conflicts_with is None)
    if target == current.status and not default_repair:
        return current
    if target != current.status and not candidate_transition_allowed(current.status, target):
        raise CandidateTransitionError(f"candidate transition {current.status} -> {target} is not allowed")
    now = utc_now_iso()
    return replace(current, cold_detail_ref="", target_evidence_bindings=binding_history(current), status=target,
        commit_obligation=(target == "approved" or current.commit_obligation)
            and target not in {"rejected", "withdrawn", "promoted"},
        reviewer=reviewer, review_note=str(review_note or "").strip(), updated_at=now,
        proposed_action=str(proposed_action).strip().lower() if proposed_action is not None else current.proposed_action,
        target_entry_id=str(target_entry_id or "").strip() if target_entry_id is not None else current.target_entry_id,
        target_entry_version=(None if target_entry_id is not None and str(target_entry_id or "").strip() != current.target_entry_id
                              else current.target_entry_version),
        conflicts_with=normalize_string_list(conflicts_with) if conflicts_with is not None else current.conflicts_with,
        promotion_target=str(promotion_target).strip().lower() if promotion_target is not None else current.promotion_target,
        status_history=[*current.status_history, _status_event(target, actor=reviewer, note=review_note, at=now)])


# LLM: 失败退避复用原五分钟规则；不推进语义版本或清除待履约责任。
# 函数用途: 为单条及批次生成同一种持久重试责任。
def _deferred_candidate(candidate, reason):
    return replace(candidate, commit_obligation=True, obligation_blocked_reason=reason,
        obligation_retry_after=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat())


# LLM: 终态沿更新时间；未终态优先宿主最后观察时间，旧行缺字段才逐级回退，不解析正文。
# 函数用途: 为扫描、执行复核和仓库清理提供同一个候选计龄基准。
def candidate_retention_time(candidate):
    if candidate.status in TERMINAL_CANDIDATE_STATUSES:
        return candidate.updated_at or candidate.created_at
    return candidate.last_observed_at or candidate.updated_at or candidate.created_at


# LLM: 两个提交入口共用纯合并；先按规范证据及精确目标校验版本，再按候选/观察身份合并。
#   正文、动作、入口和引用并集不刷新旧绑定；首拒草稿只保留版本责任，不赋予其范围条件/观察键占据合法身份的权力。
#   ID/观察键→位置索引一次构建、逐条更新，不改变typed identity、首拒草稿或目标版本权威，不按正文hash越界去重。
# 函数用途: 以索引合并观察，消除每条观察重建全表字典、扫描观察键和查找列表位置。
def merge_candidate_observations(
    candidates: list[MemoryCandidate],
    observations: list[CandidateObservation] | tuple[CandidateObservation, ...],
) -> tuple[list[MemoryCandidate], list[MemoryCandidate]]:
    prepared = _prepare_observations(observations)
    result = candidates.fork() if isinstance(candidates, CandidateRows) else list(candidates)
    bindings = IndexedBindings(result) if isinstance(result, CandidateRows) else TargetEvidenceVersionBindings(result)
    by_id = CandidateIdPositions(result) if isinstance(result, CandidateRows) else {
        item.candidate_id: index for index, item in enumerate(result)}
    by_observation = _observation_index(result)
    committed: list[MemoryCandidate] = []
    for item in prepared:
        committed.append(_merge_indexed_observation(result, by_id, by_observation, bindings, item))
    return result, committed


# LLM: 预演和正式合并共用一次索引构建，拒绝草稿不占用合法观察身份。
# 函数用途: 将独立观察键映射到所有已有候选位置，不改原模型。
def _observation_index(rows: list[MemoryCandidate]) -> dict[str, set[int]]:
    if isinstance(rows, CandidateRows):
        return ObservationPositions(rows)
    result: dict[str, set[int]] = {}
    for index, item in enumerate(rows):
        if not _rejected_draft(item):
            for key in item.observation_keys:
                result.setdefault(key, set()).add(index)
    return result


# LLM: 冲突检查先于共享索引变更；跨引用别名以历史来源为锚收敛，补run不混淆已知异run，引用及学习权威不改写。
# 函数用途: 按结构化来源合并单条观察，供批提交与可跳冲突预演复用。
def _merge_indexed_observation(result, by_id, by_observation, bindings, prepared):
    normalized, candidate_id, observation_key = prepared
    if normalized.link_candidate_id:
        linked = by_id.get(normalized.link_candidate_id)
        if (linked is None or result[linked].status in TERMINAL_CANDIDATE_STATUSES
                or result[linked].candidate_type not in {"lesson", "hot_rule"}
                or not _same_candidate_identity(result[linked], normalized)):
            raise CandidateIdentityConflictError("candidate link is not in the same typed scope")
        candidate_id = normalized.link_candidate_id
    source_index = by_id.get(candidate_id)
    if (source_index is not None and result[source_index].candidate_type in {"lesson", "hot_rule"}
            and _same_candidate_identity(result[source_index], normalized)):
        existing = result[source_index]
        incoming_sources = _observation_sources(normalized)
        new_sources = [sorted(group) for group in _new_observation_sources(existing, incoming_sources)]
        if incoming_sources or normalized.link_candidate_id:
            # 只为未与历史来源连通的组生成新观察键，不信模型observation_id。
            observation_key = (hashlib.sha256(json.dumps([candidate_id, sorted(new_sources)], sort_keys=True).encode()).hexdigest()
                               if new_sources else existing.observation_keys[0])
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
    merged = candidates.fork() if isinstance(candidates, CandidateRows) else list(candidates)
    bindings = IndexedBindings(merged) if isinstance(merged, CandidateRows) else TargetEvidenceVersionBindings(merged)
    by_id = CandidateIdPositions(merged) if isinstance(merged, CandidateRows) else {
        item.candidate_id: index for index, item in enumerate(merged)}
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
    rows = candidates.fork() if isinstance(candidates, CandidateRows) else list(candidates)
    current = rows[index]
    now = utc_now_iso()
    if current.status != "promoted":
        rows[index] = replace(current, status="promoted", commit_obligation=False,
            obligation_blocked_reason="", obligation_retry_after="", reviewer=str(reviewer or "promotion-service").strip(),
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
# 函数用途: 在owned副本归一化后原子写入，只发表字节探针，下一次读取仍须校验实际磁盘模型。
def _write_candidates_unlocked(
    path: Path,
    candidates: list[MemoryCandidate],
    *,
    admission: Any,
    coordinator_io: bool = False,
    cold_changes: dict[str, str] | None = None,
    cold_redaction: dict | None = None,
) -> None:
    _write_incremental_candidates(path, candidates, admission=admission, coordinator_io=coordinator_io,
                                  cold_changes=cold_changes, cold_redaction=cold_redaction)


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


# LLM: 写成功仅发表大文件摘要，不以写侧模型替代读合同；首次读重核schema/旧读取钩子，后续热读仍复用。
# 函数用途: 从刚落盘文本建立字节探针，候选模型缓存保持失效到下一次严格读取。
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


# LLM: 排除标记只按结构化 candidate_id 命中；已带同原因码的条目跳过，写回由调用方在锁内一次性完成。
# 函数用途: 给命中的候选写排除原因码，返回实际改动条数。
def _mark_excluded_candidates(
    candidates: list[MemoryCandidate],
    ids: set[str],
    code: str,
    now: str,
) -> int:
    changed = 0
    selected = ((candidates.index_for(identifier, None), identifier) for identifier in ids) if isinstance(candidates, CandidateRows) else None
    positions = (position for position, _ in selected if position is not None) if selected is not None else range(len(candidates))
    for index in positions:
        item = candidates[index]
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
        commit_obligation=observation.origin == "user_explicit",
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


# LLM: 首拒草稿仅用新证据重建；未知字段独占保真，拒绝历史与版本约束保留，旧引用不贡献资格。
# 函数用途: 用新观察重新申请同一候选，同时完整保留其未来合同和拒绝审计。
def _retry_rejected_draft(current: MemoryCandidate, observation: CandidateObservation,
                          observation_key: str) -> MemoryCandidate:
    fresh = _new_candidate(observation, current.candidate_id, observation_key)
    return replace(fresh, created_at=current.created_at,
                   _extra_fields=deepcopy(current._extra_fields),
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
        commit_obligation=(current.commit_obligation or resolved_origin == "user_explicit")
            and current.status not in {"promoted", "rejected", "withdrawn"},
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


# LLM: 标签不能自证权威；仅用结构化引用的新来源修复非终态推断，正式回查仍由晋升服务负责。
# 函数用途: 决定补充用户或工具引用后能否恢复正确来源并重新走自动晋升门。
def _resolved_blocked_origin(
    current: MemoryCandidate,
    observation: CandidateObservation,
) -> str:
    incoming = str(observation.origin or "").strip().lower()
    if current.origin == "model_inferred" and current.status not in TERMINAL_CANDIDATE_STATUSES:
        if incoming == "user_explicit" and observation.source_message_refs:
            return incoming
        if incoming == "tool_verified" and observation.source_tool_refs:
            return incoming
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


# LLM: 同条引用的operation/call/artifact_ref/artifact_id为别名组；有稳定ref时禁止退化为run级去重。
# 函数用途: 提取独立来源及计数别名投影，不更改真实证据或其学习覆盖。
def _observation_sources(subject):
    groups = []
    for ref in subject.source_message_refs:
        if ref.get("message_id"):
            groups.append(frozenset({("message", str(ref.get("thread_id") or ""), str(ref["message_id"]))}))
    refs = (*subject.source_tool_refs, *subject.source_artifact_refs)
    contexts = _source_alias_contexts(refs)
    for ref in refs:
        keys = _source_ref_aliases(ref)
        run = str(ref.get("run_id") or "")
        runs = {run} if run else set().union(*(contexts.get(key, set()) for key in keys)) or {""}
        aliases = frozenset((kind, context, value) for kind, value in keys for context in runs)
        if aliases:
            groups.append(aliases)
    if not groups:
        groups.extend(frozenset({("run", "", str(value))}) for value in subject.source_run_ids)
    if not groups:
        groups.extend(frozenset({("task", "", str(value))}) for value in subject.source_task_ids)
    return groups


# LLM: 只提取稳定的同次执行关联键，run单独保留上下文；不同artifact不能凭同run关联。
# 函数用途: 统一工具与产物的别名类别，并兼容call/tool_call同名键。
def _source_ref_aliases(ref):
    return frozenset(("call" if name in {"call_id", "tool_call_id"} else name, str(ref[name]))
                     for name in ("operation_id", "tool_call_id", "call_id", "artifact_ref", "artifact_id")
                     if ref.get(name))


# LLM: 只投影计数等价类；缺run的同组键共享已观察上下文，歧义保守合票，不伪造证据、学习资格或正式权威。
# 函数用途: 让混合/链式历史空run别名收敛，避免单个遗留通配符吞新运行或重放虚增票。
def _source_alias_contexts(refs):
    contexts, pending = {}, []
    for ref in refs:
        aliases = _source_ref_aliases(ref)
        run = str(ref.get("run_id") or "")
        if run:
            for alias in aliases:
                contexts.setdefault(alias, set()).add(run)
        elif aliases:
            pending.append(aliases)
    changed = True
    while changed:
        changed = False
        for aliases in pending:
            runs = set().union(*(contexts.get(alias, set()) for alias in aliases))
            for alias in aliases:
                known = contexts.setdefault(alias, set())
                if not runs.issubset(known):
                    known.update(runs)
                    changed = True
    return contexts


# LLM: 仅计数投影先沿明确历史run收敛，再容历史空run匹配；不按共同run关联、不写回真实引用或学习覆盖。
# 函数用途: 排除同批多跳补全的旧观察，返回真正独立的新来源组。
def _new_observation_sources(existing, incoming):
    seen = set().union(*_observation_sources(existing))
    pending = list(incoming)
    for allow_unqualified in (False, True):
        while pending:
            remaining = []
            for group in pending:
                contexts = _matching_source_contexts(group, seen, allow_unqualified=allow_unqualified)
                if contexts:
                    seen.update((kind, context, value) for kind, run, value in group
                        for context in contexts if not run or run == context)
                else:
                    remaining.append(group)
            if len(remaining) == len(pending):
                break
            pending = remaining
    return pending


# LLM: 相同类别/稳定ID才关联；已知run优先于历史空run，缺run组只传播命中的上下文，不把空通配符传给新别名。
# 函数用途: 为同次执行的别名补全取得已见运行上下文，明确异run不命中。
def _matching_source_contexts(incoming, existing, *, allow_unqualified):
    contexts = {}
    for kind, run, value in existing:
        contexts.setdefault((kind, value), set()).add(run)
    matched = set()
    for kind, run, value in incoming:
        runs = contexts.get((kind, value), set())
        qualified = runs - {""}
        if not qualified and allow_unqualified:
            qualified = runs
        if not run:
            matched.update(qualified)
        elif run in qualified or "" in qualified:
            matched.add(run)
    return matched


# LLM: 仅在精确引用合并后重新开放结构缺证据的候选；密码学、消息及工具证据核验仍由PromotionService独占。
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
    if isinstance(candidates, CandidateRows):
        return candidates.index_for(target)
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
# LLM: cold_cache 只活在调用方的一次扫描内（如保留引用投影），按月缓存已整份校验过的信封；
#   不跨扫描、不跨锁复用，月文件变化仍由调用方的来源复核发现并整轮重试。
def _hydrate_candidate(path: Path, item: MemoryCandidate, cold_cache: dict | None = None) -> MemoryCandidate:
    if not item.cold_detail_ref:
        return item
    match = _COLD_REF.fullmatch(item.cold_detail_ref)
    if match is None or match[2] != item.candidate_id:
        raise CandidateStoreCorruptError("candidate cold reference is invalid")
    by_key = cold_cache.get(match[1]) if cold_cache is not None else None
    if by_key is None:
        by_key = {(row["candidate_id"], row["sha256"]): row for row in _read_cold_records(path, match[1])}
        if cold_cache is not None:
            cold_cache[match[1]] = by_key
    envelope = by_key.get((match[2], match[3]))
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
    if isinstance(rows, CandidateRows):
        positions = set()
        for normalized, identifier, key in _prepare_observations(observations):
            for target in (identifier, normalized.link_candidate_id):
                index = rows.index_for(target, None) if target else None
                if index is not None:
                    positions.add(index)
            positions.update(rows.observation_positions(key))
        for index in positions:
            if rows[index].cold_detail_ref:
                rows[index] = replace(_hydrate_candidate(path, rows[index]), cold_detail_ref="")
        return rows
    if not observations or not any(row.cold_detail_ref for row in rows):
        return rows
    prepared = _prepare_observations(observations)
    ids, keys = {item[1] for item in prepared}, {item[2] for item in prepared}
    return [replace(_hydrate_candidate(path, row), cold_detail_ref="")
            if row.cold_detail_ref and (row.candidate_id in ids or keys.intersection(row.observation_keys)) else row
            for row in rows]


# LLM: 按最后变更时间冷却；只归档现有schema允许cold_detail_ref的三类终态，withdrawn/expired保原热行；负阈值拒绝。
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


# LLM: 默认持原锁同FD流式校验；显式旧读取器覆盖仍只读此FD，重复ID/坏行及schema全部拒绝，不改变热缓存规则。
# 函数用途: 单遍构造模型和稳定磁盘探针，避免整表原文及中间字典列表同时驻留。
def _parse_candidate_ledger(path: Path):
    manifest = read_manifest(path)
    if manifest is not None:
        records = list(iter_candidate_rows(path, manifest))
        return _candidate_load_probe(path), records
    stamp = _candidate_load_stamp(path)
    records = []
    digest, ids = hashlib.blake2b(digest_size=16), set()
    try:
        descriptor = open_readonly_file_beneath(path.parent, (path.name,))
        with os.fdopen(descriptor, "r", encoding="utf-8", newline="") as stream:
            if read_jsonl_objects_report is not _DEFAULT_JSONL_READER:
                from ._candidate_read_compat import read_candidate_override

                records, checksum = read_candidate_override(path, stream, read_jsonl_objects_report, _parse_candidate_line)
                if len({row.candidate_id for row in records}) != len(records):
                    raise CandidateStoreCorruptError("candidate ledger contains duplicate candidate_id")
                probe = ((stamp, checksum) if checksum is not None and stamp is not None
                         and stamp[1] <= _CANDIDATE_LOAD_CACHE_MAX_BYTES and _candidate_load_stamp(path) == stamp else None)
                return probe, records
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


def _candidate_node_family(index, root):
    """显式全表维护回收旧根家族；常规单条提交不调用此遍历。"""
    pending = [root] if root is not None else []
    while pending:
        reference = pending.pop()
        yield reference['ref']['ref']
        node = index._load(reference)
        if 'children' in node:
            pending.extend(node['children'].values())
        else:
            for key, value in node['entries'].items():
                if (key.startswith(('statusroot/', 'anchorroot/')) or key == 'anchor/groups') and value is not None:
                    pending.append(value)
                if key.startswith('row/') and value:
                    yield value['record']['ref']


def _candidate_cleanup_missing(path, parts):
    """只用严格nofollow打开证明缺失；不支持时保留原末门，不猜空或吞路径错误。"""
    from ..common.nofollow_fs import _supports_dir_fd

    if not (_supports_dir_fd() and getattr(os, 'O_NOFOLLOW', 0) and getattr(os, 'O_DIRECTORY', 0)):
        return False
    try:
        descriptor = open_readonly_file_beneath(path.parent, parts)
    except FileNotFoundError:
        return True
    os.close(descriptor)
    return False


def _finish_candidate_cleanup(path, manifest, *, coordinator_io, admissions=()):
    """完成已发表当前态的旧正文删除义务；失败保留头中义务供原锁下重试。"""
    from ..common.nofollow_fs import unlink_file_beneath
    cleanup = manifest.get('cleanup', ())
    cleanup_parts = None
    if isinstance(cleanup, dict):
        from ..common.nofollow_fs import read_bytes_beneath
        digest, name = cleanup.get('sha256'), cleanup.get('ref')
        if (type(digest) is not str or len(digest) != 64 or any(ch not in '0123456789abcdef' for ch in digest)
                or name != digest + '.cleanup' or type(cleanup.get('byte_length')) is not int):
            raise CandidateStoreCorruptError('candidate cleanup reference is invalid')
        cleanup_parts = (path.name + '.records', name)
        raw = read_bytes_beneath(path.parent, cleanup_parts, max_bytes=cleanup['byte_length'])
        if raw is None:
            return  # 成功清理在逐项fsync后最后移除义务文件，缺失表示已完成。
        if len(raw) != cleanup['byte_length'] or hashlib.sha256(raw).hexdigest() != digest:
            raise CandidateStoreCorruptError('candidate cleanup digest mismatches')
        cleanup = json.loads(raw)
        if not isinstance(cleanup, list):
            raise CandidateStoreCorruptError('candidate cleanup shape is invalid')
    for name in cleanup:
        if (type(name) is not str or '/' in name or '\\' in name or name.startswith('.')):
            raise CandidateStoreCorruptError('candidate cleanup reference is invalid')
    # 旧节点直到新头发表后才回收：按零追加登记原大小，不能在峰值准入前预支删除收益。
    tracked = [path.parent / (path.name + '.records') / name for name in cleanup]
    if cleanup_parts is not None:
        tracked.append(path.parent.joinpath(*cleanup_parts))
    if tracked:
        for admission in admissions:
            admission.check([OwnerQuotaChange(target, 0, append=True) for target in tracked])
    for name in cleanup:
        # 规划内被覆盖但从未发表的中间节点也在幂等义务中；不存在时没有写效果，
        # 无需重复G1核验。已存在或缺失不能证明的节点仍逐叶进入原末门。
        if coordinator_io and _candidate_cleanup_missing(path, (path.name + '.records', name)):
            continue
        def operation(name=name):
            return unlink_file_beneath(path.parent, (path.name + '.records', name))
        try:
            _coordinator_candidate_write(operation) if coordinator_io else operation()
        except FileNotFoundError:
            pass
    if cleanup_parts is not None:
        def operation():
            return unlink_file_beneath(path.parent, cleanup_parts)
        _coordinator_candidate_write(operation) if coordinator_io else operation()


def _write_incremental_candidates(path, candidates, *, admission, coordinator_io, cold_changes, cold_redaction):
    from ..common.nofollow_fs import write_bytes_atomic_beneath
    original_stamp = _candidate_load_stamp(path)
    previous = read_manifest(path)
    if previous is not None:
        _finish_candidate_cleanup(path, previous, coordinator_io=coordinator_io, admissions=(admission,))
    if isinstance(candidates, CandidateRows):
        rows = candidates
        redacted = [row for _, row, _ in rows.changed() if row.content_redacted_at and not row._raw_json_line]
    else:
        # 旧账首次迁移或显式保留/硬删除整表规划；普通mutation始终传递惰性工作副本。
        rows = CandidateRows(path, {'root': None, 'count': 0})
        for item in candidates:
            rows.append(item)
        redacted = [row for row in candidates if row.content_redacted_at and not row._raw_json_line]
    options = cold_redaction or {'hashes': {row.content_hash for row in redacted},
        'excluded': {row.candidate_id for _, row, _ in rows.changed() if row.content},
        'reviewer': 'memory-hard-delete', 'now': utc_now_iso()}
    archives = dict(cold_changes or {})
    archives.update(_cold_redaction_plan(path, options, archives))
    if isinstance(candidates, CandidateRows) and not rows.changed() and not archives and not hasattr(rows, "anchor_build_end"):
        return
    root, writes, cleanup = rows.plan()
    if not isinstance(candidates, CandidateRows) and previous is not None:
        old = CandidateRows(path, previous)
        family = set(_candidate_node_family(old.index, previous['root']))
        cleanup = sorted(family - set(writes))
    cleanup = seal_cleanup(writes, cleanup)
    text = manifest_text(root, len(rows), cleanup)
    changes = [OwnerQuotaChange(path, len(text.encode())), *[
        OwnerQuotaChange(path.parent / (path.name + '.records') / name, len(raw)) for name, raw in writes.items()], *[
        OwnerQuotaChange(path.parent / 'candidates_archive' / name, len(content.encode()))
        for name, content in archives.items()]]
    admission.check(changes)
    _drop_candidate_load_cache(path)
    created = []
    try:
        _write_cold_changes(path, archives, coordinator_io=coordinator_io)
        for name, raw in writes.items():
            def operation(name=name, raw=raw):
                from ..common.nofollow_fs import read_bytes_beneath
                ensure_private_dir(path.parent)
                parts = (path.name + '.records', name)
                existing = read_bytes_beneath(path.parent, parts, max_bytes=len(raw))
                if existing is not None:
                    if existing != raw:
                        raise CandidateStoreCorruptError('candidate immutable node changed')
                    return False
                write_bytes_atomic_beneath(path.parent, parts, raw)
                return True
            born = _coordinator_candidate_write(operation) if coordinator_io else operation()
            if born:
                created.append(name)
        def publish():
            write_private_text_file_atomic_unlocked(path, text, **({'replace_once': True} if coordinator_io else {}))
        _coordinator_candidate_write(publish) if coordinator_io else publish()
        _publish_written_candidates(path, text, [])
        _finish_candidate_cleanup(path, {'cleanup': cleanup}, coordinator_io=coordinator_io, admissions=(admission,))
        # 头的cleanup只包含已幂等完成的相对名字；保留它无需第二次权威写入。
    except BaseException:
        _drop_candidate_load_cache(path)
        # 原头身份未变才证明本次没有发表；已发表或结果未知时保留新根及恢复义务。
        if _candidate_load_stamp(path) == original_stamp:
            try:
                _finish_candidate_cleanup(path, {'cleanup': created}, coordinator_io=coordinator_io, admissions=(admission,))
            except BaseException:
                pass  # 保留最初失败；未发表的孤立节点不具有当前态权威。
        raise


def candidate_incremental_plan(path, records):
    """供已有跨文件事务规划候选叶和唯一头，规划阶段不写入或清理旧根。"""
    rows = records
    if not isinstance(rows, CandidateRows):
        rows = CandidateRows(path, {'root': None, 'count': 0})
        for record in records:
            rows.append(record)
    root, writes, cleanup = rows.plan()
    if not isinstance(records, CandidateRows) and (previous := read_manifest(path)) is not None:
        old = CandidateRows(path, previous)
        cleanup = sorted(set(_candidate_node_family(old.index, previous['root'])) - set(writes))
    cleanup = seal_cleanup(writes, cleanup)
    changes = {path.parent / (path.name + '.records') / name: raw.decode() for name, raw in writes.items()}
    changes[path] = manifest_text(root, len(rows), cleanup)
    return changes
