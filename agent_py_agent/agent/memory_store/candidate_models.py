from __future__ import annotations

"""统一记忆候选的版本化数据合同。

候选是“值得审核的提议”，不是长期事实。这个模块只定义结构、枚举、稳定身份和
严格解析；不负责模型调用、正式晋升或 Persona 写入。
"""

# LLM: 所有候选入口必须先变成这里的结构化对象；禁止在别处另建 status/promotion_status。
# 模块用途: 给后台策展、子代理、迁移和管理员审核提供同一 Candidate Schema。

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..common.text_norm import fold_key, nfc
from .operations import memory_content_hash, normalized_memory_content

CANDIDATE_SCHEMA_VERSION = "my-agent.memory-candidate.v2"

CANDIDATE_TYPES = frozenset(
    {
        "user_profile",
        "user_preference",
        "long_term_fact",
        "event",
        "project",
        "lesson",
        "hot_rule",
        "soul_change",
        "working_agreement",
        "discard",
    }
)
CANDIDATE_ORIGINS = frozenset(
    {
        "user_explicit",
        "tool_verified",
        "model_inferred",
        "subagent_finding",
        "subagent_lesson",
        "reviewed",
        "migrated_legacy",
    }
)
SCOPE_TYPES = frozenset(
    {"global", "personal", "company", "project", "task_class", "session", "temporary"}
)
PROPOSED_ACTIONS = frozenset({"add", "replace", "remove", "merge", "none"})
PROMOTION_TARGETS = frozenset(
    {"user", "long_term", "lesson", "hot", "soul", "agents", "none"}
)
# 晋升权限由宿主入口确定性计算并持久化到候选；模型文本/Schema 不能声明或升权。
# auto_eligible=候选可走自动晋升路径（仍受 Promotion 双重核验）；manual_required=必须
# 人工 approved + automatic=False 才能晋升。缺失/空按 manual_required fail-closed。
PROMOTION_MODES = frozenset({"auto_eligible", "manual_required"})
CANDIDATE_STATUSES = frozenset(
    {
        "observed",
        "pending_review",
        "approved",
        "promoted",
        "rejected",
        "superseded",
        "expired",
        "blocked_missing_evidence",
        "blocked_conflict",
    }
)
TERMINAL_CANDIDATE_STATUSES = frozenset(
    {"promoted", "rejected", "superseded", "expired"}
)

_STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
    "observed": frozenset(
        {
            "pending_review",
            "rejected",
            "superseded",
            "expired",
            "blocked_missing_evidence",
            "blocked_conflict",
        }
    ),
    "pending_review": frozenset(
        {
            "approved",
            "rejected",
            "superseded",
            "expired",
            "blocked_missing_evidence",
            "blocked_conflict",
        }
    ),
    "blocked_missing_evidence": frozenset(
        {"pending_review", "rejected", "superseded", "expired"}
    ),
    "blocked_conflict": frozenset(
        {"pending_review", "rejected", "superseded", "expired"}
    ),
    "approved": frozenset(
        {
            "promoted",
            "rejected",
            "superseded",
            "expired",
            "blocked_missing_evidence",
            "blocked_conflict",
        }
    ),
    "promoted": frozenset({"superseded", "expired"}),
    "rejected": frozenset(),
    "superseded": frozenset(),
    "expired": frozenset(),
}

_SCOPE_KEY_RE = re.compile(r"^[A-Za-z0-9_.:/-]{1,160}$")
_MAX_CONTENT_CHARS = 2_000
_MAX_CONDITION_CHARS = 500
_MAX_REF_BYTES = 4_096


# LLM: Scope 必须是可比较的 typed key；applies/excludes 只供人读，不能替代 scope_type/key。
# 类用途: 表示一条候选适用于全局、个人、公司、项目、任务类型、会话或临时范围。
@dataclass(frozen=True)
class MemoryScope:
    scope_type: str
    scope_key: str
    applies_when: str = ""
    excludes_when: str = ""

    # LLM: 解析失败必须抛出，禁止把任意自然语言整段默认为 global。
    # 函数用途: 从 Candidate JSON 中校验并构造稳定范围。
    @classmethod
    def from_value(cls, value: object) -> MemoryScope:
        if isinstance(value, MemoryScope):
            return value
        if not isinstance(value, dict):
            raise ValueError("candidate scope must be an object")
        scope_type = str(value.get("scope_type") or "").strip().lower()
        scope_key = str(value.get("scope_key") or "").strip()
        applies_when = str(value.get("applies_when") or "").strip()
        excludes_when = str(value.get("excludes_when") or "").strip()
        if scope_type not in SCOPE_TYPES:
            raise ValueError(f"unsupported candidate scope_type: {scope_type or '-'}")
        if not _SCOPE_KEY_RE.fullmatch(scope_key):
            raise ValueError("candidate scope_key must be a stable typed key")
        if scope_type == "global" and scope_key != "global":
            raise ValueError("global scope_key must equal 'global'")
        if len(applies_when) > _MAX_CONDITION_CHARS or len(excludes_when) > _MAX_CONDITION_CHARS:
            raise ValueError("candidate scope condition is too long")
        return cls(scope_type, scope_key, applies_when, excludes_when)

    # LLM: 新 add 候选必须使用可由 runtime 精确召回的规范 typed key；旧账本读取仍走 from_value。
    # 函数用途: 校验新观察的 scope key 与 scope_type 一致，并归一为唯一持久化键
    # （project 的 task:<id> 旧账本兼容输入归一到 project:<id>，阻止双正式身份）。
    @classmethod
    def for_new_observation(cls, value: object) -> MemoryScope:
        scope = cls.from_value(value)
        # 共享合同：已 canonical typed key 原样放行；raw/空/嵌套 typed prefix 拒绝
        # （嵌套双前缀会让同一实体产生两个合法身份，recall 侧不生成、写入侧不收）。
        from .scope_contract import validate_typed_scope_key

        canonical = validate_typed_scope_key(scope.scope_type, scope.scope_key)
        if canonical != scope.scope_key:
            scope = cls(scope.scope_type, canonical, scope.applies_when, scope.excludes_when)
        return scope

    # LLM: 输出字段名是持久化协议，不能为展示方便改名。
    # 函数用途: 把范围写成 JSON 可序列化对象。
    def to_dict(self) -> dict[str, str]:
        return asdict(self)


# LLM: Observation 是入口草稿，不含 host-owned id/status/count；这些字段只能由 CandidateService 决定。
# 类用途: 统一接收后台模型、子代理、remember 候选或迁移器提出的一次独立观察。
@dataclass(frozen=True)
class CandidateObservation:
    candidate_type: str
    content: str
    subject_key: str
    scope: MemoryScope | dict[str, object]
    origin: str
    evidence_refs: tuple[dict[str, object], ...] = ()
    source_message_refs: tuple[dict[str, object], ...] = ()
    source_tool_refs: tuple[dict[str, object], ...] = ()
    source_artifact_refs: tuple[dict[str, object], ...] = ()
    source_task_ids: tuple[str, ...] = ()
    source_run_ids: tuple[str, ...] = ()
    observed_at: str = ""
    valid_from: str = ""
    valid_until: str = ""
    confidence: float = 0.0
    proposed_action: str = "add"
    target_entry_id: str = ""
    conflicts_with: tuple[str, ...] = ()
    promotion_target: str = "long_term"
    promotion_mode: str = ""
    observation_id: str = ""


# LLM: MemoryCandidate 是 candidates.jsonl 的唯一当前态记录；状态历史留在同一记录内。
# 类用途: 保存候选正文、证据、范围、审核、晋升位置和幂等观察次数。
@dataclass
class MemoryCandidate:
    schema_version: str
    candidate_id: str
    candidate_type: str
    content: str
    subject_key: str
    scope: dict[str, str]
    origin: str
    evidence_refs: list[dict[str, object]]
    source_message_refs: list[dict[str, object]]
    source_tool_refs: list[dict[str, object]]
    source_artifact_refs: list[dict[str, object]]
    source_task_ids: list[str]
    source_run_ids: list[str]
    observed_at: str
    last_observed_at: str
    valid_from: str
    valid_until: str
    occurrence_count: int
    confidence: float
    proposed_action: str
    target_entry_id: str
    conflicts_with: list[str]
    promotion_target: str
    status: str
    reviewer: str
    review_note: str
    created_at: str
    updated_at: str
    promoted_at: str
    promotion_ref: str
    content_hash: str = ""
    content_redacted_at: str = ""
    observation_keys: list[str] = field(default_factory=list)
    status_history: list[dict[str, str]] = field(default_factory=list)
    promotion_mode: str = ""

    # LLM: 持久化前完整验证，坏行不能被部分加载后继续覆盖。
    # 函数用途: 将候选转换为 JSONL 记录。
    def to_record(self) -> dict[str, object]:
        validate_candidate(self)
        return asdict(self)

    # LLM: 未知字段可忽略以便增量升级，但已知必填字段缺失或非法必须 fail closed。
    # 函数用途: 从 candidates.jsonl 行恢复当前候选对象。
    @classmethod
    def from_record(cls, payload: dict[str, object]) -> MemoryCandidate:
        known = cls.__dataclass_fields__
        try:
            candidate = cls(**{key: value for key, value in payload.items() if key in known})
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid memory candidate record") from exc
        validate_candidate(candidate)
        return candidate


# LLM: Candidate ID includes semantic content, scope and requested mutation; add/remove of the same text must not collide.
# 函数用途: 为同一候选内容、主题、范围、动作和目标生成稳定编号。
def stable_candidate_id(
    *,
    candidate_type: str,
    content: str,
    subject_key: str,
    scope: MemoryScope,
    proposed_action: str,
    target_entry_id: str,
) -> str:
    material = json.dumps(
        {
            "candidate_type": fold_key(candidate_type),
            "content": normalized_memory_content(content),
            "subject_key": fold_key(subject_key),
            "scope_type": scope.scope_type,
            "scope_key": fold_key(scope.scope_key),
            "proposed_action": fold_key(proposed_action),
            "target_entry_id": str(target_entry_id or "").strip(),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "memory-candidate-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


# LLM: occurrence_count 只按独立结构化来源增加；重复跑同一事件不能提高 HOT 资格。
# 函数用途: 为一次观察生成稳定去重键。
def stable_observation_key(observation: CandidateObservation, candidate_id: str) -> str:
    if str(observation.observation_id or "").strip():
        material = str(observation.observation_id).strip()
    else:
        material = json.dumps(
            {
                "candidate_id": candidate_id,
                "message_refs": normalize_reference_list(observation.source_message_refs),
                "tool_refs": normalize_reference_list(observation.source_tool_refs),
                "artifact_refs": normalize_reference_list(observation.source_artifact_refs),
                "task_ids": normalize_string_list(observation.source_task_ids),
                "run_ids": normalize_string_list(observation.source_run_ids),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    return "observation-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


# LLM: 只有这里定义的有向边才是合法候选状态转换。
# 函数用途: 校验审核、阻塞、晋升和终态转换是否合法。
def candidate_transition_allowed(current: str, target: str) -> bool:
    return target in _STATUS_TRANSITIONS.get(current, frozenset())


# LLM: 引用只能是有界结构化对象；正文、大工具输出或任意嵌套对象不得塞进候选。
# 函数用途: 规范化并去重消息、工具、artifact 和通用证据引用。
def normalize_reference_list(values: object) -> list[dict[str, object]]:
    if values in (None, ""):
        return []
    if not isinstance(values, (list, tuple)):
        raise ValueError("candidate references must be an array of objects")
    normalized: list[dict[str, object]] = []
    seen: set[str] = set()
    for raw in values:
        if not isinstance(raw, dict):
            raise ValueError("candidate reference must be an object")
        item = _normalize_reference(raw)
        key = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if key not in seen:
            seen.add(key)
            normalized.append(item)
    return normalized


# LLM: ref 中只保留 JSON 标量和短字符串列表；content/output/body 等大正文键一律拒绝。
# 函数用途: 校验单条证据引用不会复制原始大内容。
def _normalize_reference(raw: dict[str, object]) -> dict[str, object]:
    forbidden = {"content", "output", "body", "raw", "full_text", "transcript"}
    if forbidden & {str(key).strip().lower() for key in raw}:
        raise ValueError("candidate reference cannot contain raw content")
    item: dict[str, object] = {}
    for key, value in raw.items():
        name = str(key or "").strip()
        if not name:
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            item[name] = nfc(str(value)) if isinstance(value, str) else value
            continue
        if isinstance(value, list) and all(isinstance(part, (str, int, float, bool)) for part in value):
            item[name] = list(value[:32])
            continue
        raise ValueError("candidate reference values must be JSON scalars or short scalar arrays")
    if not item or not any(
        str(item.get(key) or "").strip()
        for key in (
            "ref_id",
            "message_id",
                "call_id",
                "tool_call_id",
            "operation_id",
            "artifact_ref",
            "artifact_id",
            "source_ref",
            "event_id",
            "path",
        )
    ):
        raise ValueError("candidate reference has no stable identifier")
    if len(json.dumps(item, ensure_ascii=False).encode("utf-8")) > _MAX_REF_BYTES:
        raise ValueError("candidate reference is too large")
    return item


# LLM: ID 列表按原顺序去重；空 ID 不获得来源权威。
# 函数用途: 规范化 task_id、run_id、entry_id 和 observation key 列表。
def normalize_string_list(values: object) -> list[str]:
    if values in (None, ""):
        return []
    if not isinstance(values, (list, tuple, set)):
        raise ValueError("candidate id collection must be an array")
    return list(dict.fromkeys(text for item in values if (text := str(item or "").strip())))


# LLM: 时间字段统一成带时区 ISO；不能比较原始时间字符串决定冲突胜负。
# 函数用途: 规范化外部观察时间，空值使用调用方给定默认值。
def normalize_iso_time(value: object, *, default: str = "", allow_empty: bool = True) -> str:
    text = str(value or "").strip()
    if not text:
        if default:
            return normalize_iso_time(default, allow_empty=False)
        if allow_empty:
            return ""
        raise ValueError("timestamp is required")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid ISO timestamp: {text}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


# LLM: Host 时间只用于账本顺序，绝不能自动让新候选覆盖旧事实。
# 函数用途: 返回当前 UTC ISO 时间。
def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# LLM: 每次读写都走同一完整 Schema 校验，禁止某个入口接受另一套弱字段。
# 函数用途: 验证持久候选的所有枚举、引用、范围、时间和状态历史。
def validate_candidate(candidate: MemoryCandidate) -> None:
    if candidate.schema_version != CANDIDATE_SCHEMA_VERSION:
        raise ValueError(f"unsupported candidate schema: {candidate.schema_version}")
    if not str(candidate.candidate_id or "").startswith("memory-candidate-"):
        raise ValueError("candidate_id is invalid")
    if candidate.candidate_type not in CANDIDATE_TYPES:
        raise ValueError(f"unsupported candidate_type: {candidate.candidate_type}")
    if candidate.origin not in CANDIDATE_ORIGINS:
        raise ValueError(f"unsupported candidate origin: {candidate.origin}")
    if candidate.proposed_action not in PROPOSED_ACTIONS:
        raise ValueError(f"unsupported proposed_action: {candidate.proposed_action}")
    if candidate.promotion_target not in PROMOTION_TARGETS:
        raise ValueError(f"unsupported promotion_target: {candidate.promotion_target}")
    if candidate.status not in CANDIDATE_STATUSES:
        raise ValueError(f"unsupported candidate status: {candidate.status}")
    # 晋升权限 fail-closed:legacy/新记录缺失或空一律归一 manual_required,
    # 非空非法值严格拒绝;权限不参与 candidate_id/observation key/身份对比。
    mode = str(candidate.promotion_mode or "").strip().lower()
    if not mode:
        candidate.promotion_mode = "manual_required"
    elif mode not in PROMOTION_MODES:
        raise ValueError(f"unsupported promotion_mode: {mode}")
    content = str(candidate.content or "").strip()
    if not content:
        if candidate.status not in TERMINAL_CANDIDATE_STATUSES:
            raise ValueError("active candidate content is required")
        if not candidate.content_hash or not candidate.content_redacted_at:
            raise ValueError("redacted candidate requires content_hash and content_redacted_at")
    if len(content) > _MAX_CONTENT_CHARS:
        raise ValueError("candidate content is too long; store a ref instead")
    if "\n" in content and len(content.splitlines()) > 8:
        raise ValueError("candidate content must be short and self-contained")
    scope = MemoryScope.from_value(candidate.scope)
    candidate.scope = scope.to_dict()
    candidate.evidence_refs = normalize_reference_list(candidate.evidence_refs)
    candidate.source_message_refs = normalize_reference_list(candidate.source_message_refs)
    candidate.source_tool_refs = normalize_reference_list(candidate.source_tool_refs)
    candidate.source_artifact_refs = normalize_reference_list(candidate.source_artifact_refs)
    candidate.source_task_ids = normalize_string_list(candidate.source_task_ids)
    candidate.source_run_ids = normalize_string_list(candidate.source_run_ids)
    candidate.conflicts_with = normalize_string_list(candidate.conflicts_with)
    candidate.observation_keys = normalize_string_list(candidate.observation_keys)
    if int(candidate.occurrence_count or 0) != len(candidate.observation_keys):
        raise ValueError("candidate occurrence_count does not match independent observations")
    if not 0.0 <= float(candidate.confidence) <= 1.0:
        raise ValueError("candidate confidence must be between 0 and 1")
    for name in (
        "observed_at",
        "last_observed_at",
        "valid_from",
        "valid_until",
        "created_at",
        "updated_at",
        "promoted_at",
        "content_redacted_at",
    ):
        normalized = normalize_iso_time(
            getattr(candidate, name),
            allow_empty=name
            in {"valid_from", "valid_until", "promoted_at", "content_redacted_at"},
        )
        setattr(candidate, name, normalized)
    if candidate.valid_from and candidate.valid_until:
        if datetime.fromisoformat(candidate.valid_until) <= datetime.fromisoformat(candidate.valid_from):
            raise ValueError("candidate valid_until must be later than valid_from")
    if candidate.proposed_action in {"replace", "remove", "merge"} and not candidate.target_entry_id:
        raise ValueError("replace/remove/merge candidate requires target_entry_id")
    if candidate.status == "promoted" and (not candidate.promoted_at or not candidate.promotion_ref):
        raise ValueError("promoted candidate requires promoted_at and promotion_ref")
    if candidate.content and candidate.content_hash != memory_content_hash(candidate.content):
        raise ValueError("candidate content_hash mismatch")
    if not isinstance(candidate.status_history, list) or not candidate.status_history:
        raise ValueError("candidate status_history is required")
    if str(candidate.status_history[-1].get("status") or "") != candidate.status:
        raise ValueError("candidate status_history tail must match current status")


__all__ = [
    "CANDIDATE_ORIGINS",
    "CANDIDATE_SCHEMA_VERSION",
    "CANDIDATE_STATUSES",
    "CANDIDATE_TYPES",
    "CandidateObservation",
    "MemoryCandidate",
    "MemoryScope",
    "PROMOTION_MODES",
    "PROMOTION_TARGETS",
    "PROPOSED_ACTIONS",
    "SCOPE_TYPES",
    "TERMINAL_CANDIDATE_STATUSES",
    "candidate_transition_allowed",
    "normalize_iso_time",
    "normalize_reference_list",
    "normalize_string_list",
    "stable_candidate_id",
    "stable_observation_key",
    "utc_now_iso",
    "validate_candidate",
]
