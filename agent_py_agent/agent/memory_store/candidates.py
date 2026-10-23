# LLM: C显式IO只收紧原目录/锁出生/raw末写，默认False保持原身份/状态/字节；联验原promotion/quota/候选回归。
# 模块用途: 为唯一候选服务保原算法并接当前C同步末门。
from __future__ import annotations

"""Owner 级唯一记忆候选仓库和状态机。"""

# LLM: candidates.jsonl 是候选当前态唯一事实源；ops、task workspace 和 learning draft 不得双写。
# 模块用途: 幂等合并观察、审核状态转换、晋升标记、过期和硬删除正文清理。

import hashlib
import json
import threading
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..common.global_write_commit import execute_global_write
from ..common.json_io import (
    locked_json_path,
    read_jsonl_objects_report,
    write_private_text_file_atomic_unlocked,
)
from ..common.nofollow_fs import ensure_private_dir
from ..common.text_norm import fold_key
from ..user_space.owner_quota import OwnerQuotaAdmission, OwnerQuotaChange, OwnerQuotaEnforcer
from .candidate_commit_lock import candidate_commit_lock, candidate_redaction_lock
from .candidate_models import (
    CANDIDATE_ORIGINS,
    CANDIDATE_SCHEMA_VERSION,
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

# 记忆候选列表缓存最多跟踪的文件路径数。
_CANDIDATE_LOAD_CACHE_MAX_PATHS = 8
# 记忆候选列表缓存允许占用的最大总字节数。
_CANDIDATE_LOAD_CACHE_MAX_BYTES = 32 * 1024 * 1024
_CANDIDATE_LOAD_CACHE: OrderedDict[
    str, tuple[tuple[int, int, int, int], str, tuple[MemoryCandidate, ...]]
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


# LLM: 每次热读仍读取完整字节作blake2b校验，不信任同尺寸同mtime；超原始字节预算或竞态不创建投影。
# 函数用途: 为候选解析缓存取得本轮稳定文件版本和内容摘要。
def _candidate_load_probe(path: Path):
    stamp = _candidate_load_stamp(path)
    if stamp is None or stamp[1] > _CANDIDATE_LOAD_CACHE_MAX_BYTES:
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if len(data) != stamp[1] or _candidate_load_stamp(path) != stamp:
        return None
    return stamp, hashlib.blake2b(data, digest_size=16).hexdigest()


# LLM: 只清进程内派生解析结果，不改变任何候选、配额或业务锁；失败写入也可安全重新读取。
# 函数用途: 使指定候选路径的模型缓存失效。
def _drop_candidate_load_cache(path: Path) -> None:
    with _CANDIDATE_LOAD_CACHE_LOCK:
        _CANDIDATE_LOAD_CACHE.pop(str(path), None)


# LLM: 保存前深复制全部嵌套字段，最多8路径和32MiB原始字节；调用方返回值不能成为缓存权威。
# 函数用途: 记忆已通过原坏行、schema及重复ID验证的一代候选。
def _remember_candidate_load(path, probe, records):
    if probe is None or _candidate_load_stamp(path) != probe[0]:
        return
    snapshot = deepcopy(tuple(records))
    with _CANDIDATE_LOAD_CACHE_LOCK:
        _CANDIDATE_LOAD_CACHE[str(path)] = (probe[0][:4], probe[1], snapshot)
        _CANDIDATE_LOAD_CACHE.move_to_end(str(path))
        while (len(_CANDIDATE_LOAD_CACHE) > _CANDIDATE_LOAD_CACHE_MAX_PATHS
               or sum(entry[0][1] for entry in _CANDIDATE_LOAD_CACHE.values()) > _CANDIDATE_LOAD_CACHE_MAX_BYTES):
            _CANDIDATE_LOAD_CACHE.popitem(last=False)


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
# 类用途: 管理原owner唯一候选源，不创建第二状态或保存活来源。
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
                candidates = self._load_unlocked()
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
            rows, results = assemble_evidence_batch(self._load_unlocked(), observations, failures)
            _write_candidates_unlocked(self.path, rows, admission=admission, **({} if not self.coordinator_io else {"coordinator_io": True}))
            return results

    # LLM: 读取同样严格解析全部行；状态/内容过滤只在完整事实集验证后执行。
    # 函数用途: 列出候选，可按状态过滤并保留确定性创建顺序。
    # LLM: C复用同一原锁/序列化/准入，仅出生与raw末写消费原scope；状态/身份/事务顺序不变。
    # 函数用途: 沿原候选业务取得真实锁并保完整失败事实，不以活来源替换领域裁决。
    def list(
        self,
        *,
        statuses: set[str] | frozenset[str] | None = None,
        limit: int = 0,
    ) -> list[MemoryCandidate]:
        with self._locked_candidates():
            records = self._load_unlocked()
        if statuses is not None:
            records = [item for item in records if item.status in statuses]
        records.sort(key=lambda item: (item.created_at, item.candidate_id))
        return records if limit <= 0 else records[-limit:]

    # LLM: candidate_id 是唯一定位键；禁止按正文或 subject 模糊选中审核目标。
    # 函数用途: 读取一个候选，不存在时抛出精确 KeyError。
    def get(self, candidate_id: str) -> MemoryCandidate:
        target = str(candidate_id or "").strip()
        for item in self.list():
            if item.candidate_id == target:
                return item
        raise KeyError(target)

    # LLM: 调用方已持quota→policy；候选锁覆盖快照核对和正式写入，期间不能合并/审核该候选。
    # 函数用途: 为最终提交锁定已验证的完整候选快照。
    # LLM: C复用同一原锁/序列化/准入，仅出生与raw末写消费原scope；状态/身份/事务顺序不变。
    # 函数用途: 沿原候选业务取得真实锁并保完整失败事实，不以活来源替换领域裁决。
    @contextmanager
    def locked_snapshot(self, expected: MemoryCandidate) -> Iterator[MemoryCandidate]:
        with candidate_commit_lock(self.path, **({} if not self.coordinator_io else {"coordinator_io": True})):
            rows = self._load_unlocked()
            current = next((row for row in rows if row.candidate_id == expected.candidate_id), None)
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

    # LLM: 字符串保持旧审核入口；传完整候选则锁内CAS后转换，改目标ID仍清除旧版本，不新增授权来源。
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
                current = candidates[index]
                if expected is not None and current != expected:
                    raise CandidateStateChangedError(current.status)
                if target == current.status:
                    return current
                if not candidate_transition_allowed(current.status, target):
                    raise CandidateTransitionError(
                        f"candidate transition {current.status} -> {target} is not allowed"
                    )
                now = utc_now_iso()
                updated = replace(
                    current,
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

    # LLM: 晋升提交复用准入/候选锁；remove终态与同hash正文脱敏必须在同一次原子替换中落盘。
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
            _write_candidates_unlocked(self.path, rows, admission=admission, **({} if not self.coordinator_io else {"coordinator_io": True}))
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
            if changed:
                rows = [new if new is not None else old for old, new in zip(candidates, replacements, strict=True)]
                _write_candidates_unlocked(self.path, rows, admission=admission, **({} if not self.coordinator_io else {"coordinator_io": True}))
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

    # LLM: retention 只按结构化 rejected/expired/superseded 状态和 updated_at 删除。
    # 函数用途: 规划超过截止时间的非活跃候选 ID，不执行删除。
    def retention_candidates(self, *, before: datetime) -> list[str]:
        cutoff = before.astimezone(timezone.utc)
        result: list[str] = []
        for item in self.list(statuses={"rejected", "expired", "superseded"}):
            if _is_binding_tombstone(item):
                continue
            if datetime.fromisoformat(item.updated_at) < cutoff:
                result.append(item.candidate_id)
        return result

    # LLM: 精确ID且仅retention终态；带版本绑定的行必须脱敏留约束，不能硬删后重新授权旧证据。
    # 函数用途: 清理终态候选正文与引用，返回本次实际清理条数。
    # LLM: C复用同一原锁/序列化/准入，仅出生与raw末写消费原scope；状态/身份/事务顺序不变。
    # 函数用途: 沿原候选业务取得真实锁并保完整失败事实，不以活来源替换领域裁决。
    def delete_terminal(self, candidate_ids: list[str] | tuple[str, ...]) -> int:
        ids = set(normalize_string_list(candidate_ids))
        if not ids:
            return 0
        with _quota_admission(self.quota_enforcer) as admission:
            with self._locked_candidates():
                candidates = self._load_unlocked()
                for item in candidates:
                    if item.candidate_id in ids and item.status not in {
                        "rejected",
                        "expired",
                        "superseded",
                    }:
                        raise CandidateTransitionError(
                            f"candidate {item.candidate_id} is not retention-terminal"
                        )
                retained, deleted = _retention_records(candidates, ids)
                if deleted:
                    _write_candidates_unlocked(self.path, retained, admission=admission, **({} if not self.coordinator_io else {"coordinator_io": True}))
                return deleted

    # LLM: 健康统计不返回正文、路径或用户引用。
    # 函数用途: 为 doctor/CLI 输出候选数量和状态分布。
    def stats(self) -> dict[str, object]:
        candidates = self.list()
        statuses: dict[str, int] = {}
        types: dict[str, int] = {}
        for item in candidates:
            statuses[item.status] = statuses.get(item.status, 0) + 1
            types[item.candidate_type] = types.get(item.candidate_type, 0) + 1
        return {
            "schema_version": CANDIDATE_SCHEMA_VERSION,
            "total": len(candidates),
            "by_status": dict(sorted(statuses.items())),
            "by_type": dict(sorted(types.items())),
        }

    # LLM: 只返回原同path锁，C透传准确FD出生，不缓存活来源或缩短锁内业务；默认调用形状不加False。
    # 函数用途: 给候选原读写算法取得相同双层锁，等待与全文件读取都门外。
    def _locked_candidates(self):
        options = {} if not self.coordinator_io else {"birth_commit": _coordinator_candidate_write}
        return locked_json_path(self.path, **options)

    # LLM: 原path锁覆盖读取；缓存须匹配本轮完整字节摘要，出入均深复制；坏行/schema/重复ID仍按原错误阻断。
    # 函数用途: 严格加载候选，只在内容未变时跳过重复JSONL解析和模型验证。
    def _load_unlocked(self) -> list[MemoryCandidate]:
        probe = _candidate_load_probe(self.path)
        with _CANDIDATE_LOAD_CACHE_LOCK:
            cached = _CANDIDATE_LOAD_CACHE.get(str(self.path))
            if probe is not None and cached is not None and cached[:2] == (probe[0][:4], probe[1]):
                _CANDIDATE_LOAD_CACHE.move_to_end(str(self.path))
            else:
                _CANDIDATE_LOAD_CACHE.pop(str(self.path), None)
                cached = None
        if cached is not None:
            return list(deepcopy(cached[2]))
        report = read_jsonl_objects_report(
            self.path,
            context="memory_candidates.load",
        )
        if report.load_errors:
            raise CandidateStoreCorruptError(
                f"candidate ledger contains {len(report.load_errors)} unreadable row(s)"
            )
        records: list[MemoryCandidate] = []
        for index, payload in enumerate(report.records, start=1):
            try:
                records.append(MemoryCandidate.from_record(payload))
            except ValueError as exc:
                raise CandidateStoreCorruptError(
                    f"candidate ledger row {index} failed schema validation"
                ) from exc
        ids = [item.candidate_id for item in records]
        if len(ids) != len(set(ids)):
            raise CandidateStoreCorruptError("candidate ledger contains duplicate candidate_id")
        _remember_candidate_load(self.path, probe, records)
        return records


# LLM: 同一宿主观察身份被拿去对应另一个类型化主题时抛出；仍是 ValueError（原调用方的全有或全无语义不变），
#   单列子类只为让 Curator 能精确识别这一种冲突并只丢弃那一条（split_identity_conflicts）。
# 类用途: 表示一条观察撞上了已有观察身份、但类型化主题不同。
class CandidateIdentityConflictError(ValueError):
    pass


# LLM: 两个提交入口共用纯合并；先按规范证据及精确目标校验版本，再按候选/观察身份合并。
#   正文、动作、入口和引用并集不刷新旧绑定；首拒草稿只保留版本责任，不赋予其范围条件/观察键占据合法身份的权力。
#   同批观察依次累积；已晋升或脱敏行不属于首拒草稿，失败仍不写任何账本。
# 函数用途: 将一批 observation 合并进已验证账本，先核对既有观察权威再返回完整新账本。
def merge_candidate_observations(
    candidates: list[MemoryCandidate],
    observations: list[CandidateObservation] | tuple[CandidateObservation, ...],
) -> tuple[list[MemoryCandidate], list[MemoryCandidate]]:
    prepared = _prepare_observations(observations)
    result = list(candidates)
    bindings = TargetEvidenceVersionBindings(result)
    committed: list[MemoryCandidate] = []
    for normalized, candidate_id, observation_key in prepared:
        if bindings.conflicts(normalized):
            raise CandidateIdentityConflictError("candidate evidence cannot refresh target observation version")
        matches = [item for item in result
                   if observation_key in item.observation_keys and not _rejected_draft(item)]
        if any(not _same_candidate_identity(item, normalized) for item in matches):
            raise CandidateIdentityConflictError(
                "candidate observation identity was reused for a different typed subject"
            )
        by_id = {item.candidate_id: item for item in result}
        existing = by_id.get(candidate_id) or next(iter(matches), None)
        if existing is None:
            candidate = _new_candidate(normalized, candidate_id, observation_key)
            result.append(candidate)
        else:
            if not _rejected_draft(existing) and not _same_candidate_identity(existing, normalized):
                raise CandidateIdentityConflictError(
                    "candidate observation identity was reused for a different typed subject"
                )
            candidate = _merge_candidate(existing, normalized, observation_key)
            result[result.index(existing)] = candidate
        bindings.add(normalized)
        committed.append(candidate)
    return result, committed


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


# LLM: 纯函数，不写账本：用与提交完全相同的 merge_candidate_observations 逐条试合并，只把抛
#   CandidateIdentityConflictError 的那几条剔掉；其它校验错误照常上抛（原全有或全无语义不变）。
#   供 Curator 在整批提交前调用，一条冲突不再拖死整批、也不再让同一批反复重放。
# 函数用途: 把一批观察分成可提交的和身份冲突的，返回（可提交的观察, 剔除条数）。
def split_identity_conflicts(
    candidates: list[MemoryCandidate],
    observations: list[CandidateObservation] | tuple[CandidateObservation, ...],
) -> tuple[tuple[CandidateObservation, ...], int]:
    merged = list(candidates)
    kept: list[CandidateObservation] = []
    for observation in observations:
        try:
            merged, _committed = merge_candidate_observations(merged, [observation])
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


# LLM: 硬删除先冻结版本约束，再清除正文、引用quote、来源自由文本和历史审核note；不依赖retention后补。
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
    if not any((item.content, item.evidence_refs, item.source_message_refs, item.source_tool_refs,
                item.source_artifact_refs, item.source_task_ids, item.source_run_ids,
                item.learning_source, item.learning_source_coverage)):
        return None
    target = item.status if item.status in TERMINAL_CANDIDATE_STATUSES else "superseded"
    history = [{**event, "note": ""} for event in item.status_history]
    note = "正文及引用随正式记忆硬删除清除，仅保留身份与版本约束。"
    if target != item.status and candidate_transition_allowed(item.status, target):
        history.append(_status_event(target, actor=reviewer, note=note, at=now))
    return replace(item, content="", status=target, reviewer=reviewer, review_note=note,
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
# 函数用途: 原容量核验成功后使解析缓存失效并替换同一候选文件，C末核失败保原当前态。
def _write_candidates_unlocked(
    path: Path,
    candidates: list[MemoryCandidate],
    *,
    admission: Any,
    coordinator_io: bool = False,
) -> None:
    text = "".join(
        json.dumps(item.to_record(), ensure_ascii=False, sort_keys=True) + "\n"
        for item in candidates
    )
    admission.check([OwnerQuotaChange(path, len(text.encode("utf-8")))])
    _drop_candidate_load_cache(path)
    if coordinator_io:
        _coordinator_candidate_write(lambda: write_private_text_file_atomic_unlocked(path, text, replace_once=True))
    else:
        write_private_text_file_atomic_unlocked(path, text)


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
    "merge_candidate_observations",
    "split_identity_conflicts",
]


# LLM: retention保留版本绑定和硬删除防重放约束；无绑定add被正式删除后也必须留下不含原话的身份墓碑。
# 函数用途: 计算终态保留清理后的候选集合与实际清理条数。
def _retention_records(candidates: list[MemoryCandidate], ids: set[str]) -> tuple[list[MemoryCandidate], int]:
    retained: list[MemoryCandidate] = []
    cleaned = 0
    for item in candidates:
        if item.candidate_id not in ids:
            retained.append(item)
            continue
        history = binding_history(item)
        if not history and not item.content_redacted_at:
            cleaned += 1
            continue
        if _is_binding_tombstone(item):
            retained.append(item)
            continue
        now = utc_now_iso()
        retained.append(replace(item, content="", content_redacted_at=now, updated_at=now,
            evidence_refs=[], source_message_refs=[], source_tool_refs=[], source_artifact_refs=[],
            source_task_ids=[], source_run_ids=[], learning_source={}, learning_source_coverage={},
            target_evidence_bindings=history, reviewer="retention", review_note="retained_version_bindings",
            status_history=[_status_event(item.status, actor="retention", note="retained_version_bindings", at=now)]))
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
