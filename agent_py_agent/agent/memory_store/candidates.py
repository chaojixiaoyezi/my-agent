from __future__ import annotations

"""Owner 级唯一记忆候选仓库和状态机。"""

# LLM: candidates.jsonl 是候选当前态唯一事实源；ops、task workspace 和 learning draft 不得双写。
# 模块用途: 幂等合并观察、审核状态转换、晋升标记、过期和硬删除正文清理。

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..common.json_io import (
    locked_json_path,
    read_jsonl_objects_report,
    write_text_file_atomic_unlocked,
)
from ..user_space.owner_quota import OwnerQuotaChange, OwnerQuotaEnforcer
from .candidate_models import (
    CANDIDATE_ORIGINS,
    CANDIDATE_SCHEMA_VERSION,
    CANDIDATE_TYPES,
    PROMOTION_TARGETS,
    PROPOSED_ACTIONS,
    TERMINAL_CANDIDATE_STATUSES,
    CandidateObservation,
    MemoryCandidate,
    MemoryScope,
    candidate_transition_allowed,
    normalize_iso_time,
    normalize_reference_list,
    normalize_string_list,
    stable_candidate_id,
    stable_observation_key,
    utc_now_iso,
)
from .operations import memory_content_hash


# LLM: 损坏候选账本必须阻断写入；跳过坏行会在下一次原子写时永久丢数据。
# 类用途: 向 CLI、Curator 和迁移器返回稳定的候选账本损坏错误。
class CandidateStoreCorruptError(RuntimeError):
    pass


# LLM: 状态机错误要和存储损坏、找不到候选区分，调用方不得自然语言猜修复动作。
# 类用途: 表示一次不在统一状态图中的非法审核或晋升转换。
class CandidateTransitionError(RuntimeError):
    pass


# LLM: Service 只接收结构化 observation/review；不解析聊天正文或相似文本。
# 类用途: 管理一个 owner 的 memory/candidates.jsonl。
class CandidateService:
    # LLM: 构造器只绑定 owner 规范路径和可选 quota；创建目录不读取或迁移旧候选源。
    # 函数用途: 初始化当前 owner 的唯一候选仓库。
    def __init__(
        self,
        path: str | Path,
        *,
        quota_enforcer: OwnerQuotaEnforcer | None = None,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.quota_enforcer = quota_enforcer

    # LLM: 重放同一 observation key 只更新时间和 refs，不增加 occurrence_count。
    # 函数用途: 新建或合并一次候选观察，并自动进入 pending/blocked 初审状态。
    def observe(self, observation: CandidateObservation) -> MemoryCandidate:
        return self.observe_many([observation])[0]

    # LLM: Batch observations validate completely before one locked replacement; callers must not emulate atomicity with loops.
    # 函数用途: 原子写入一批候选，任一条非法时整个批次不改变 candidates.jsonl。
    def observe_many(
        self,
        observations: list[CandidateObservation] | tuple[CandidateObservation, ...],
    ) -> list[MemoryCandidate]:
        with _quota_admission(self.quota_enforcer) as admission:
            with locked_json_path(self.path):
                candidates = self._load_unlocked()
                candidates, committed = merge_candidate_observations(
                    candidates,
                    observations,
                )
                _write_candidates_unlocked(self.path, candidates, admission=admission)
                return committed

    # LLM: 读取同样严格解析全部行；状态/内容过滤只在完整事实集验证后执行。
    # 函数用途: 列出候选，可按状态过滤并保留确定性创建顺序。
    def list(
        self,
        *,
        statuses: set[str] | frozenset[str] | None = None,
        limit: int = 0,
    ) -> list[MemoryCandidate]:
        with locked_json_path(self.path):
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

    # LLM: 审核只能走统一有向状态边；reviewer/note 随记录保存，不能另建 decision ledger。
    # 函数用途: 推进候选状态并可补充替换目标、冲突和晋升落点。
    def transition(
        self,
        candidate_id: str,
        target_status: str,
        *,
        reviewer: str,
        review_note: str = "",
        proposed_action: str | None = None,
        target_entry_id: str | None = None,
        conflicts_with: list[str] | tuple[str, ...] | None = None,
        promotion_target: str | None = None,
    ) -> MemoryCandidate:
        target = str(target_status or "").strip().lower()
        actor = str(reviewer or "").strip()
        if not actor:
            raise ValueError("candidate reviewer is required")
        with _quota_admission(self.quota_enforcer) as admission:
            with locked_json_path(self.path):
                candidates = self._load_unlocked()
                index = _candidate_index(candidates, candidate_id)
                current = candidates[index]
                if target == current.status:
                    return current
                if not candidate_transition_allowed(current.status, target):
                    raise CandidateTransitionError(
                        f"candidate transition {current.status} -> {target} is not allowed"
                    )
                now = utc_now_iso()
                updated = replace(
                    current,
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
                _write_candidates_unlocked(self.path, candidates, admission=admission)
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
        ref = str(promotion_ref or "").strip()
        if not ref:
            raise ValueError("promotion_ref is required")
        with _quota_admission(self.quota_enforcer) as admission:
            with locked_json_path(self.path):
                candidates = self._load_unlocked()
                index = _candidate_index(candidates, candidate_id)
                current = candidates[index]
                if current.status == "promoted":
                    if current.promotion_ref != ref:
                        raise CandidateTransitionError(
                            "promoted candidate cannot be rebound to a different ref"
                        )
                    return current
                if not candidate_transition_allowed(current.status, "promoted"):
                    raise CandidateTransitionError(
                        f"candidate transition {current.status} -> promoted is not allowed"
                    )
                now = utc_now_iso()
                updated = replace(
                    current,
                    status="promoted",
                    reviewer=str(reviewer or "promotion-service").strip(),
                    review_note=str(review_note or "").strip(),
                    promoted_at=now,
                    promotion_ref=ref,
                    updated_at=now,
                    status_history=[
                        *current.status_history,
                        _status_event(
                            "promoted",
                            actor=str(reviewer or "promotion-service"),
                            note=review_note,
                            at=now,
                        ),
                    ],
                )
                candidates[index] = updated
                _write_candidates_unlocked(self.path, candidates, admission=admission)
                return updated

    # LLM: Hard delete 清正文但保留 hash/id/status 审计，不得改写 ConversationStore 原话。
    # 函数用途: 清除与已硬删除长期记忆相同的候选正文。
    def redact_content(
        self,
        *,
        content_hashes: set[str],
        exclude_candidate_ids: set[str] | frozenset[str] | None = None,
        reviewer: str = "memory-hard-delete",
    ) -> int:
        hashes = {str(item).strip() for item in content_hashes if str(item).strip()}
        excluded = {
            str(item).strip()
            for item in (exclude_candidate_ids or set())
            if str(item).strip()
        }
        if not hashes:
            return 0
        with _quota_admission(self.quota_enforcer) as admission:
            with locked_json_path(self.path):
                candidates = self._load_unlocked()
                changed = 0
                now = utc_now_iso()
                for index, item in enumerate(candidates):
                    updated = _redacted_candidate(
                        item,
                        hashes=hashes,
                        excluded=excluded,
                        reviewer=reviewer,
                        now=now,
                    )
                    if updated is None:
                        continue
                    candidates[index] = updated
                    changed += 1
                if changed:
                    _write_candidates_unlocked(self.path, candidates, admission=admission)
                return changed

    # LLM: retention 只按结构化 rejected/expired/superseded 状态和 updated_at 删除。
    # 函数用途: 规划超过截止时间的非活跃候选 ID，不执行删除。
    def retention_candidates(self, *, before: datetime) -> list[str]:
        cutoff = before.astimezone(timezone.utc)
        result: list[str] = []
        for item in self.list(statuses={"rejected", "expired", "superseded"}):
            if datetime.fromisoformat(item.updated_at) < cutoff:
                result.append(item.candidate_id)
        return result

    # LLM: 删除候选必须精确 ID 且只允许 retention 终态；不能用正文或自然语言范围。
    # 函数用途: 应用候选 retention 删除并返回实际删除数量。
    def delete_terminal(self, candidate_ids: list[str] | tuple[str, ...]) -> int:
        ids = set(normalize_string_list(candidate_ids))
        if not ids:
            return 0
        with _quota_admission(self.quota_enforcer) as admission:
            with locked_json_path(self.path):
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
                retained = [item for item in candidates if item.candidate_id not in ids]
                deleted = len(candidates) - len(retained)
                if deleted:
                    _write_candidates_unlocked(self.path, retained, admission=admission)
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

    # LLM: 内部读取必须在 path lock 内调用，任一坏行都阻断下一次覆盖。
    # 函数用途: 严格加载当前候选文件。
    def _load_unlocked(self) -> list[MemoryCandidate]:
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
        return records


# LLM: Curator batch commit and the ordinary observe_many path share this exact pure merge;
# neither may invent a second candidate ID or occurrence-count rule.
# 函数用途: 将一批 observation 合并进已验证账本并返回新的完整账本和对应当前态。
def merge_candidate_observations(
    candidates: list[MemoryCandidate],
    observations: list[CandidateObservation] | tuple[CandidateObservation, ...],
) -> tuple[list[MemoryCandidate], list[MemoryCandidate]]:
    prepared = _prepare_observations(observations)
    result = list(candidates)
    committed: list[MemoryCandidate] = []
    for normalized, candidate_id, observation_key in prepared:
        by_id = {item.candidate_id: item for item in result}
        existing = by_id.get(candidate_id)
        if existing is None:
            existing = next(
                (
                    item
                    for item in result
                    if observation_key in item.observation_keys
                ),
                None,
            )
        if existing is None:
            candidate = _new_candidate(normalized, candidate_id, observation_key)
            result.append(candidate)
        else:
            if not _same_candidate_identity(existing, normalized):
                raise ValueError(
                    "candidate observation identity was reused for a different typed subject"
                )
            candidate = _merge_candidate(existing, normalized, observation_key)
            result[result.index(existing)] = candidate
        committed.append(candidate)
    return result, committed


# LLM: An observation replay may paraphrase content, but it cannot change the typed candidate
# subject, scope, action, target, or promotion authority behind the same host identity.
# 函数用途: 判断一个已存在候选是否可安全接收同 observation key 的重放。
def _same_candidate_identity(
    current: MemoryCandidate,
    observation: CandidateObservation,
) -> bool:
    return (
        current.candidate_type == observation.candidate_type
        and current.subject_key == observation.subject_key
        and current.scope == MemoryScope.from_value(observation.scope).to_dict()
        and current.proposed_action == observation.proposed_action
        and current.target_entry_id == observation.target_entry_id
        and current.promotion_target == observation.promotion_target
    )


# LLM: Complete validation happens before the caller writes any ledger, preserving batch
# all-or-nothing behavior for both standalone and Curator transactions.
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
        prepared.append(
            (normalized, candidate_id, stable_observation_key(normalized, candidate_id))
        )
    return prepared


# LLM: Hard-delete redaction is a pure per-row decision so lock/IO nesting stays shallow and
# every retained audit field is explicit.
# 函数用途: 对命中 hash 的候选清正文并在需要时转为 superseded。
def _redacted_candidate(
    item: MemoryCandidate,
    *,
    hashes: set[str],
    excluded: set[str],
    reviewer: str,
    now: str,
) -> MemoryCandidate | None:
    if item.candidate_id in excluded or item.content_hash not in hashes or not item.content:
        return None
    target = item.status
    history = list(item.status_history)
    if item.status not in TERMINAL_CANDIDATE_STATUSES:
        target = "superseded"
        if candidate_transition_allowed(item.status, target):
            history.append(
                _status_event(
                    target,
                    actor=reviewer,
                    note="正文随正式记忆硬删除清除，仅保留 hash。",
                    at=now,
                )
            )
    return replace(
        item,
        content="",
        status=target,
        reviewer=reviewer,
        review_note="正文随正式记忆硬删除清除，仅保留 hash。",
        updated_at=now,
        content_redacted_at=now,
        status_history=history,
    )


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


# LLM: 写入用完整当前态原子 replace；不能 append 第二条相同 candidate_id。
# 函数用途: 通过 quota 后原子替换 candidates.jsonl。
def _write_candidates_unlocked(
    path: Path,
    candidates: list[MemoryCandidate],
    *,
    admission: Any,
) -> None:
    text = "".join(
        json.dumps(item.to_record(), ensure_ascii=False, sort_keys=True) + "\n"
        for item in candidates
    )
    admission.check([OwnerQuotaChange(path, len(text.encode("utf-8")))])
    write_text_file_atomic_unlocked(path, text)


# LLM: 候选初次观察先记录 observed，再以同一状态图进入 pending/blocked，不允许直接 approved。
# 函数用途: 构造一条新候选当前态和完整初始状态历史。
def _new_candidate(
    observation: CandidateObservation,
    candidate_id: str,
    observation_key: str,
) -> MemoryCandidate:
    now = utc_now_iso()
    observed_at = normalize_iso_time(observation.observed_at, default=now, allow_empty=False)
    status = _initial_review_status(observation)
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
        conflicts_with=normalize_string_list(observation.conflicts_with),
        promotion_target=observation.promotion_target,
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


# LLM: 合并只增加独立证据和 occurrence；终态、审核结果和正式落点不能被后来的模型输出重置。
# 函数用途: 将同一语义候选的新观察幂等合并到当前态。
def _merge_candidate(
    current: MemoryCandidate,
    observation: CandidateObservation,
    observation_key: str,
) -> MemoryCandidate:
    observed_at = normalize_iso_time(
        observation.observed_at,
        default=utc_now_iso(),
        allow_empty=False,
    )
    observation_keys = list(current.observation_keys)
    is_new = observation_key not in observation_keys
    if is_new:
        observation_keys.append(observation_key)
    return replace(
        current,
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


# LLM: 外部 observation 只能使用统一枚举并保持简短正文；服务不自动猜 scope/subject。
# 函数用途: 规范化一次候选观察。
def _normalize_observation(observation: CandidateObservation) -> CandidateObservation:
    if not isinstance(observation, CandidateObservation):
        raise TypeError("candidate observation must use CandidateObservation")
    candidate_type = str(observation.candidate_type or "").strip().lower()
    origin = str(observation.origin or "").strip().lower()
    content = str(observation.content or "").strip()
    subject_key = str(observation.subject_key or "").strip()
    scope = MemoryScope.from_value(observation.scope)
    proposed_action = str(observation.proposed_action or "none").strip().lower()
    promotion_target = str(observation.promotion_target or "none").strip().lower()
    if candidate_type not in CANDIDATE_TYPES:
        raise ValueError(f"unsupported candidate_type: {candidate_type}")
    if origin not in CANDIDATE_ORIGINS:
        raise ValueError(f"unsupported candidate origin: {origin}")
    if proposed_action not in PROPOSED_ACTIONS:
        raise ValueError(f"unsupported proposed_action: {proposed_action}")
    if promotion_target not in PROMOTION_TARGETS:
        raise ValueError(f"unsupported promotion_target: {promotion_target}")
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
        confidence=confidence,
        target_entry_id=str(observation.target_entry_id or "").strip(),
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
    "CandidateTransitionError",
    "merge_candidate_observations",
]
