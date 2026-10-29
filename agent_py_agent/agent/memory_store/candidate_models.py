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
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..common.text_norm import fold_key, nfc
from .operations import memory_content_hash, normalized_memory_content

CANDIDATE_SCHEMA_VERSION = "my-agent.memory-candidate.v3"

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
# 晋升权限由宿主按结构化 target/type/origin/action 确定性计算，模型文本/Schema 不能声明。
# auto_eligible=可由当前 owner 的 Agent 自主尝试（仍受证据、CAS、冲突、门槛和注入扫描）；
# AGENTS 目标例外：资格记录仍是 auto_eligible，但自动晋升在 Promotion 边界被硬拦
# （AUTO_AGENTS_WRITE_FORBIDDEN，见 promotion._automatic_policy_block），只能由用户当前回合
# 直接 update_persona 或 owner 本人人工复核写入。
# manual_required 只保留给 SOUL 或根本不具备正式落点权威的候选。
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
        "withdrawn",
        "blocked_missing_evidence",
        "blocked_conflict",
    }
)
TERMINAL_CANDIDATE_STATUSES = frozenset(
    {"promoted", "rejected", "superseded", "expired", "withdrawn"}
)

_STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
    "observed": frozenset(
        {
            "pending_review",
            "rejected",
            "withdrawn",
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
            "withdrawn",
            "superseded",
            "expired",
            "blocked_missing_evidence",
            "blocked_conflict",
        }
    ),
    "blocked_missing_evidence": frozenset(
        {"pending_review", "rejected", "withdrawn", "superseded", "expired"}
    ),
    "blocked_conflict": frozenset(
        {"pending_review", "rejected", "withdrawn", "superseded", "expired"}
    ),
    "approved": frozenset(
        {
            "promoted",
            "rejected",
            "withdrawn",
            "superseded",
            "expired",
            "blocked_missing_evidence",
            "blocked_conflict",
        }
    ),
    "promoted": frozenset({"superseded", "expired"}),
    "rejected": frozenset(),
    "superseded": frozenset({"withdrawn"}),
    "expired": frozenset({"withdrawn"}),
    "withdrawn": frozenset(),
}

_SCOPE_KEY_RE = re.compile(r"^[A-Za-z0-9_.:/-]{1,160}$")
# 候选内容最多 2000 字符：超长正文截断，防记忆候选撑爆输入。
_MAX_CONTENT_CHARS = 2_000
# 候选条件最多 500 字符：条件说明保持简短。
_MAX_CONDITION_CHARS = 500
# 候选证据引用最多 4096 字节：ref 过长视为异常，防止坏快照入库。
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
    # LLM: 宿主判定的学习来源投影；构造入口（curator/子代理/remember）填写，模型不能提供。
    # 字段用途: 让候选携带可追溯来源，晋升门在重试时仍能复核学习资格。
    learning_source: dict[str, object] = field(default_factory=dict)
    # LLM: 仅宿主输入快照或工具解析目标时填写，模型不得提出；None 是没有版本证明，不能推测为当前版本。
    # 字段用途: 固定本次观察所依据的正式目标版本。
    target_entry_version: int | None = None
    # 宿主在当前输入锚点集合内核对后才能使用；不是正式目标ID或晋升权限。
    link_candidate_id: str = ""


# LLM: 候选当前态唯一事实源；raw缓存仅实例属性，不属于dataclass字段/asdict，构造、比较、持久协议不含缓存。
# 类用途: 保存候选正式字段；每次构造隔离未知字段，原行和摘要仅供内部复用。
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
    # LLM: 学习来源排除的持久标记（如 LEARNING_SOURCE_EXCLUDED）：非空表示该候选不得再走任何
    #   自动晋升路径，由排除名单同步写回；人工路径不读取它，状态机也不因此改变。
    # 字段用途: 记录候选被排除自动晋升的结构化原因码。
    promotion_exclusion: str = ""
    # LLM: 宿主判定的学习来源投影；空表示未知来源（自动路径不可学），人工路径不读取它。
    # 字段用途: 记录候选来源与学习资格，供晋升门复核与正式记录溯源。
    learning_source: dict[str, object] = field(default_factory=dict)
    # LLM: 缺字段旧候选按 None 读取；合并、重试与审核不得从当前正式记录补猜版本。
    # 字段用途: 持久保存观察时的正式目标版本，供最终CAS使用。
    target_entry_version: int | None = None

    # LLM: 仅候选仓库构造，模型和观察入口均不能提供；缺字段旧记录沿显式来源保守重建。
    # 字段用途: 区分尚待补齐的来源引用与已经明确不可学的来源。
    learning_source_coverage: dict[str, object] = field(default_factory=dict)
    # Host-only immutable historical bindings; no body/quote, retained inside this authority only.
    target_evidence_bindings: list[dict[str, object]] = field(default_factory=list)
    cold_detail_ref: str = ""
    commit_obligation: bool = False
    obligation_blocked_reason: str = ""
    obligation_retry_after: str = ""
    _extra_fields: dict[str, object] = field(default_factory=dict, repr=False)
    _raw_json_line = ""
    _raw_state_digest = ""

    # LLM: 所有构造及dataclass替换都独占未知字段树；重建路径仍须显式带回旧字段，不把缓存当事实源。
    # 函数用途: 统一隔离候选重建后的未来字段，避免冷热转换或调用者修改污染旧快照。
    def __post_init__(self) -> None:
        self._extra_fields = deepcopy(self._extra_fields)

    # LLM: 字段赋值即时失效原行；嵌套变更由序列化器的完整状态摘要再次校验，不能依赖调用方自觉。
    # 函数用途: 避免状态、版本绑定或隐私字段改写后复用旧JSON。
    def __setattr__(self, name: str, value: object) -> None:
        if not name.startswith("_") and name in self.__dict__ and self.__dict__[name] != value:
            object.__setattr__(self, "_raw_json_line", "")
            object.__setattr__(self, "_raw_state_digest", "")
        object.__setattr__(self, name, value)

    # LLM: 未知字段深复制保真，已知字段始终优先，私有存储与原行缓存不进入协议。
    # 函数用途: 将候选转换为 JSONL 记录。
    def to_record(self) -> dict[str, object]:
        validate_candidate(self)
        payload = deepcopy(self._extra_fields)
        payload.update({name: deepcopy(getattr(self, name)) for name, descriptor in self.__dataclass_fields__.items()
                        if descriptor.init and not name.startswith("_")})
        if not self.cold_detail_ref:
            payload.pop("cold_detail_ref", None)
        if not self.learning_source:
            payload.pop("learning_source", None)
        if self.target_entry_version is None:
            payload.pop("target_entry_version", None)
        if not self.learning_source_coverage:
            payload.pop("learning_source_coverage", None)
        if not self.target_evidence_bindings:
            payload.pop("target_evidence_bindings", None)
        return payload

    # LLM: v2显式升级并保留未知顶层字段；批准/用户提交义务保守恢复，坏已知字段仍关闭式拒绝。
    # 函数用途: 从 candidates.jsonl 行恢复当前候选对象。
    @classmethod
    def from_record(cls, payload: dict[str, object]) -> MemoryCandidate:
        known = {key: value for key, value in cls.__dataclass_fields__.items() if not key.startswith("_")}
        values = deepcopy({key: value for key, value in payload.items() if key in known and known[key].init})
        if values.get("schema_version") == "my-agent.memory-candidate.v2":
            values["schema_version"] = CANDIDATE_SCHEMA_VERSION
        if "commit_obligation" not in values:
            values["commit_obligation"] = values.get("status") == "approved" or (
                (values.get("origin") == "user_explicit" or payload.get("source_kind") == "user_explicit")
                and values.get("status") not in {"promoted", "rejected", "withdrawn"})
        try:
            candidate = cls(**values, _extra_fields=deepcopy({key: value for key, value in payload.items() if key not in known}))
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid memory candidate record") from exc
        validate_candidate(candidate)
        return candidate


# LLM: 基础候选身份包含语义、范围、动作和精确目标；用于保持旧ID和来源观察去重语义。
# 函数用途: 为同一候选内容、主题、范围、动作和目标生成稳定基础编号。
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


# LLM: 只有既有目标变更在基础ID之上绑定宿主版本；缺版本保留旧身份，不能猜成当前版本。
# 函数用途: 为版本化变更生成独立候选ID，允许新证据基于新版本重新提案。
def versioned_candidate_id(candidate_id: str, target_entry_version: int | None) -> str:
    version = normalize_target_entry_version(target_entry_version)
    if version is None:
        return candidate_id
    material = json.dumps([candidate_id, version], separators=(",", ":"))
    return "memory-candidate-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


# LLM: occurrence_count只按独立结构化来源增加；同一来源跨目标版本重放仍是同一观察，不能刷新覆盖权。
# 函数用途: 为一次观察生成稳定去重键。
def stable_observation_key(observation: CandidateObservation, candidate_id: str) -> str:
    if str(observation.observation_id or "").strip():
        material = str(observation.observation_id).strip()
    else:
        # 候选身份含版本，来源观察身份故意不含版本：版本变化本身不是一份新证据。
        if observation.target_entry_version is not None:
            candidate_id = stable_candidate_id(
                candidate_type=observation.candidate_type, content=observation.content,
                subject_key=observation.subject_key, scope=MemoryScope.from_value(observation.scope),
                proposed_action=observation.proposed_action, target_entry_id=observation.target_entry_id,
            )
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


# LLM: 只收真实正整数版本；None保留旧记录的未知语义，不把bool、数字文本或浮点数猜成版本。
# 函数用途: 校验宿主观察目标版本。
def normalize_target_entry_version(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 1:
        raise ValueError("candidate target_entry_version must be a positive integer or null")
    return value


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


# LLM: 默认落点只适用于用户明确的长期类型；用于比较旧none与已补全记录，不赋予证据或自动写权限。
# 函数用途: 统一解析同一候选在观察和晋升阶段的实际目标。
def effective_promotion_target(target: str, origin: str, candidate_type: str) -> str:
    normalized = str(target or "").strip().lower()
    if normalized in {"", "none"} and origin == "user_explicit" and candidate_type in {
        "long_term_fact", "event", "project", "working_agreement",
    }:
        return "long_term"
    return normalized


# LLM: 资格仅由结构化候选字段及宿主来源计算；auto_eligible只是候选资格，正式自动发布仍由末端确认门拒绝。
# manual; formal user facts still require user_explicit authority; lessons/HOT remain protected
# by their target repositories. AGENTS keeps its auto_eligible eligibility record, but every
# automatic promotion path is blocked at the promotion boundary (AUTO_AGENTS_WRITE_FORBIDDEN):
# only the user's own update_persona turn or an owner-run manual review may write AGENTS.
# 函数用途: 统一判断一个候选能否进入模型自主晋升链，避免 remember、curator 和子代理各写一套规则。
def host_promotion_mode(candidate: object) -> str:
    values = candidate if isinstance(candidate, dict) else None
    candidate_type = str(
        (values.get("candidate_type") if values is not None else getattr(candidate, "candidate_type", ""))
        or ""
    ).strip().lower()
    origin = str(
        (values.get("origin") if values is not None else getattr(candidate, "origin", ""))
        or ""
    ).strip().lower()
    target = str(
        (
            values.get("promotion_target")
            if values is not None
            else getattr(candidate, "promotion_target", "")
        )
        or ""
    ).strip().lower()
    action = str(
        (
            values.get("proposed_action")
            if values is not None
            else getattr(candidate, "proposed_action", "")
        )
        or ""
    ).strip().lower()
    if target in {"", "none"} and (
        origin == "user_explicit"
        and candidate_type in {"long_term_fact", "event", "project"}
    ):
        target = "long_term"
    if action not in {"add", "replace", "remove", "merge"}:
        return "manual_required"
    if target == "long_term":
        if candidate_type == "working_agreement":
            # 工作约定：学习资格只由宿主来源投影决定（can_learn），不看正文、置信或模型声明；
            # 来源未知/不可学（派活、定时、后台、缺字段旧候选）一律转人工复核。
            from ..conversation.learning_source import projection_is_learnable

            source = (
                values.get("learning_source")
                if values is not None
                else getattr(candidate, "learning_source", None)
            )
            return "auto_eligible" if projection_is_learnable(source) else "manual_required"
        return (
            "auto_eligible"
            if origin in {"user_explicit", "tool_verified"}
            and candidate_type in {"long_term_fact", "event", "project"}
            else "manual_required"
        )
    if target == "user":
        return (
            "auto_eligible"
            if origin == "user_explicit"
            and candidate_type in {"user_profile", "user_preference"}
            else "manual_required"
        )
    if target == "agents":
        return (
            "auto_eligible"
            if candidate_type == "working_agreement"
            and origin in {"user_explicit", "tool_verified", "model_inferred", "reviewed"}
            else "manual_required"
        )
    if target == "lesson":
        return (
            "auto_eligible"
            if action == "add"
            and candidate_type == "lesson"
            and origin
            in {
                "user_explicit",
                "tool_verified",
                "model_inferred",
                "subagent_lesson",
                "reviewed",
            }
            else "manual_required"
        )
    if target == "hot":
        return (
            "auto_eligible"
            if action == "add"
            and candidate_type == "hot_rule"
            and origin in {"user_explicit", "tool_verified", "model_inferred", "reviewed"}
            else "manual_required"
        )
    return "manual_required"


# LLM: 每次读写校验Schema、观察版本、冷ref固定路径及来源覆盖；旧缺字段可读但不补猜权威。
# 函数用途: 验证持久候选的所有枚举、引用、范围、时间和状态历史。
def validate_candidate(candidate: MemoryCandidate) -> None:
    if type(candidate.commit_obligation) is not bool:
        raise ValueError("candidate commit_obligation must be boolean")
    if candidate.status == "approved" or (candidate.origin == "user_explicit"
            and candidate.status not in {"promoted", "rejected", "withdrawn"}):
        candidate.commit_obligation = True
    if candidate.status in {"promoted", "rejected", "withdrawn"}:
        candidate.commit_obligation = False
        candidate.obligation_blocked_reason = ""
        candidate.obligation_retry_after = ""
    candidate.obligation_retry_after = normalize_iso_time(candidate.obligation_retry_after)
    if not isinstance(candidate.obligation_blocked_reason, str) or len(candidate.obligation_blocked_reason) > 160:
        raise ValueError("candidate obligation_blocked_reason is invalid")
    if not isinstance(candidate.cold_detail_ref, str) or (candidate.cold_detail_ref and (
        candidate.status not in {"promoted", "superseded", "rejected"} or not re.fullmatch(
            r"candidates_archive/[0-9]{4}-(?:0[1-9]|1[0-2])\.jsonl#memory-candidate-[A-Za-z0-9_-]+:[0-9a-f]{64}",
            candidate.cold_detail_ref))):
        raise ValueError("invalid candidate cold_detail_ref")
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
    # 非空非法值严格拒绝；合法旧权限按当前宿主策略重算。这样升级前被一刀切为
    # manual_required 的非 SOUL 候选不会永久等用户，安全/证据门仍在 Promotion 层。
    mode = str(candidate.promotion_mode or "").strip().lower()
    if mode and mode not in PROMOTION_MODES:
        raise ValueError(f"unsupported promotion_mode: {mode}")
    candidate.promotion_mode = host_promotion_mode(candidate)
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
    if len(str(candidate.promotion_exclusion or "")) > 120:
        raise ValueError("candidate promotion_exclusion is too long")
    if not isinstance(candidate.learning_source, dict):
        raise ValueError("candidate learning_source must be an object")
    normalize_target_entry_version(candidate.target_entry_version)
    from .candidate_source_coverage import validate_source_coverage
    from .candidate_target_binding import validate_binding_history

    validate_source_coverage(candidate)
    validate_binding_history(candidate.target_evidence_bindings)


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
    "host_promotion_mode",
    "normalize_iso_time",
    "normalize_reference_list",
    "normalize_string_list",
    "normalize_target_entry_version",
    "stable_candidate_id",
    "stable_observation_key",
    "utc_now_iso",
    "validate_candidate",
    "versioned_candidate_id",
]
