from __future__ import annotations

"""Memory Curator 的配置、durable state 和严格模型输出合同。"""

# LLM: 后台模型只能生成 CuratorExtraction；run/cursor/lease/status/id 都由宿主结构持有。
# 模块用途: 防止提示词正文或不同 provider 产生第二套记忆字段和游标语义。

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any

from .candidate_models import (
    CANDIDATE_TYPES,
    PROMOTION_TARGETS,
    PROPOSED_ACTIONS,
    CandidateObservation,
    MemoryScope,
    normalize_reference_list,
)
from .curator_schema import (
    build_curator_response_schema,
    validate_curator_response_payload,
)
from .daily import DailyMemoryEvent

CURATOR_STATE_SCHEMA_VERSION = "my-agent.memory-curator-state.v1"
CURATOR_RUN_SCHEMA_VERSION = "my-agent.memory-curator-run.v2"
CURATOR_OUTPUT_SCHEMA_VERSION = "my-agent.memory-curator-output.v3"
CURATOR_MODEL_ORIGINS = frozenset(
    {"user_explicit", "tool_verified", "model_inferred"}
)
CURATOR_TRIGGER_REASONS = frozenset(
    {
        "turn_threshold",
        "interval",
        "pre_compact",
        "session_close",
        "reset",
        "task_complete",
        "daily_finalize",
        "admin",
        "migration",
    }
)


# LLM: 这是 Curator 唯一有效配置快照；provider/model 切换不得改变输出 Schema。
# 类用途: 从 AgentConfig 提取后台策展开关、批量、超时和晋升策略。
@dataclass(frozen=True)
class MemoryCuratorConfig:
    enabled: bool = True
    provider: str = "auto"
    model: str = ""
    interval_seconds: int = 10_800
    turn_threshold: int = 10
    batch_message_limit: int = 80
    max_input_chars: int = 40_000
    timeout_seconds: int = 90
    max_retries: int = 1
    daily_finalize_hour: int = 23
    auto_promotion_policy: str = "conservative_v1"

    # LLM: 配置只从已经 normalize 的 AgentConfig 读取，不再解析配置文件或自然语言。
    # 函数用途: 构造一次运行不可变配置。
    @classmethod
    def from_agent_config(cls, config: object) -> MemoryCuratorConfig:
        return cls(
            enabled=bool(getattr(config, "memory_curator_enabled", True)),
            provider=str(getattr(config, "memory_curator_provider", "auto") or "auto").strip(),
            model=str(getattr(config, "memory_curator_model", "") or "").strip(),
            interval_seconds=int(getattr(config, "memory_curator_interval_seconds", 10_800)),
            turn_threshold=int(getattr(config, "memory_curator_turn_threshold", 10)),
            batch_message_limit=int(
                getattr(config, "memory_curator_batch_message_limit", 80)
            ),
            max_input_chars=int(getattr(config, "memory_curator_max_input_chars", 40_000)),
            timeout_seconds=int(getattr(config, "memory_curator_timeout_seconds", 90)),
            max_retries=int(getattr(config, "memory_curator_max_retries", 1)),
            daily_finalize_hour=int(
                getattr(config, "memory_curator_daily_finalize_hour", 23)
            ),
            auto_promotion_policy=str(
                getattr(
                    config,
                    "memory_curator_auto_promotion_policy",
                    "conservative_v1",
                )
                or "conservative_v1"
            ).strip(),
        )

    # LLM: revision 只反映影响策展语义/预算的字段，供 state 标识配置切换。
    # 函数用途: 生成短配置修订号。
    def revision(self) -> str:
        material = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return "curator-config-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


# owner 没有可用模型是需要人处理的永久配置问题，不是临时故障：退避拉长到一小时，避免每个维护周期都重建
# owner 实例、整批收集后再失败；配好模型后（IM owner 空闲回收后重建会读到新配置）最迟一小时恢复。
CURATOR_MODEL_NOT_CONFIGURED = "CURATOR_MODEL_NOT_CONFIGURED"
CURATOR_NOT_CONFIGURED_RETRY_SECONDS = 3600


# LLM: 唯一的失败退避口径，发现层 owner_wake_discovery 与 curator._run_pending_when_due 都必须调用它；只按
#   结构化失败码判断，不读异常正文。同步 test_curator_model_not_configured.py。
# 函数用途: 按上次失败码给出这次重试前要等待的秒数。
def curator_failure_retry_seconds(failure_code: str, base_seconds: int) -> int:
    return CURATOR_NOT_CONFIGURED_RETRY_SECONDS if failure_code == CURATOR_MODEL_NOT_CONFIGURED else base_seconds


# LLM: state 是每 owner 唯一游标和 lease 权威；不能在 Gateway 内存另存一份成功游标。
# 类用途: 持久化增量消息/audit 位置、运行健康、累计数量和待处理触发。
@dataclass
class MemoryCuratorState:
    schema_version: str = CURATOR_STATE_SCHEMA_VERSION
    last_processed_message_id: str = ""
    last_processed_audit_event_id: str = ""
    per_thread_cursors: dict[str, str] = field(default_factory=dict)
    last_run_at: str = ""
    last_success_at: str = ""
    last_failure_at: str = ""
    last_failure_code: str = ""
    active_lease: dict[str, object] = field(default_factory=dict)
    processed_count: int = 0
    candidate_count: int = 0
    daily_event_count: int = 0
    config_revision: str = ""
    pending_reasons: list[str] = field(default_factory=list)
    pending_reason_generations: dict[str, int] = field(default_factory=dict)
    pending_requested_at: str = ""
    last_daily_finalize_date: str = ""
    last_committed_run_id: str = ""

    # LLM: 空对象=首次初始化(允许); 非空但无任何已知字段=纯未知字段
    # fail-closed(seq1625 P0 遗留: {"not":"a state"} 不得静默复用默认 state);
    # 部分未知(含已知字段)按已知子集恢复, 兼容旧 state 文件历史字段。
    # 函数用途: 从 state.json 严格恢复 durable state。
    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> MemoryCuratorState:
        if not payload:
            return cls()
        known = cls.__dataclass_fields__
        filtered = {key: value for key, value in payload.items() if key in known}
        if not filtered:
            unknown = sorted(str(key) for key in payload)
            raise ValueError(
                f"memory curator state contains only unknown fields: {unknown}"
            )
        try:
            state = cls(**filtered)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid memory curator state") from exc
        validate_curator_state(state)
        return state

    # LLM: state 写入前必须完整验证，不能让坏 cursor 在下一轮被静默重置。
    # 函数用途: 输出 state.json 对象。
    def to_dict(self) -> dict[str, object]:
        validate_curator_state(self)
        return asdict(self)


# LLM: 模型只提出 Daily 语义和最小证据选择；运行 ID、时间和账本顺序字段全部由宿主引用推导或分配。
# 类用途: 解析一条严格策展每日事件草稿并承接宿主补全字段。
@dataclass(frozen=True)
class CuratorDailyDraft:
    event_type: str
    summary: str
    actor: str
    origin: str
    session_id: str = ""
    thread_id: str = ""
    request_id: str = ""
    task_id: str = ""
    run_id: str = ""
    message_refs: tuple[dict[str, object], ...] = ()
    tool_refs: tuple[dict[str, object], ...] = ()
    artifact_refs: tuple[dict[str, object], ...] = ()
    decisions: tuple[str, ...] = ()
    lessons: tuple[str, ...] = ()
    next_actions: tuple[str, ...] = ()
    created_at: str = ""

    # LLM: 所有 identity/time/order 都已由宿主验证或补充，模型不能在此选择运行作用域。
    # 函数用途: 构造待提交 DailyMemoryEvent。
    def to_daily_event(self, *, curator_run_id: str, extracted_at: str) -> DailyMemoryEvent:
        return DailyMemoryEvent(
            event_type=self.event_type,
            summary=self.summary,
            actor=self.actor,
            origin=self.origin,
            session_id=self.session_id,
            thread_id=self.thread_id,
            request_id=self.request_id,
            task_id=self.task_id,
            run_id=self.run_id,
            message_refs=self.message_refs,
            tool_refs=self.tool_refs,
            artifact_refs=self.artifact_refs,
            decisions=self.decisions,
            lessons=self.lessons,
            next_actions=self.next_actions,
            created_at=self.created_at,
            extracted_at=extracted_at,
            curator_run_id=curator_run_id,
        )


# LLM: 模型输出只表达提炼结果和处理声明；cursor 最终值仍由宿主核对输入顺序后计算。
# 类用途: 表示一次通过严格 JSON Schema 的 Curator 响应。
@dataclass(frozen=True)
class CuratorExtraction:
    daily_events: tuple[CuratorDailyDraft, ...]
    candidates: tuple[CandidateObservation, ...]
    processed_message_refs: tuple[dict[str, object], ...]
    processed_audit_refs: tuple[dict[str, object], ...]
    unresolved_refs: tuple[dict[str, object], ...]
    warnings: tuple[str, ...]
    next_cursor: dict[str, object]


# LLM: run audit 不含输入正文和候选正文，只保存数量、游标、模型和稳定失败码。
# 类用途: 返回显式 CLI/Gateway 可观察的一次策展结果。
@dataclass(frozen=True)
class CuratorRunResult:
    run_id: str
    status: str
    reason: str
    provider: str
    model: str
    processed_messages: int = 0
    processed_audit_events: int = 0
    daily_events: int = 0
    candidates: int = 0
    failure_code: str = ""
    warnings: tuple[str, ...] = ()

    # LLM: CLI/Gateway 只消费稳定状态、计数与错误码，不能借此取得候选正文。
    # 函数用途: 将一次 Curator 运行结果转换为可序列化字典。
    def to_dict(self) -> dict[str, object]:
        return asdict(self)


# LLM: state 类型验证独立于 IO，迁移、doctor 和测试都必须复用同一规则。
# 函数用途: 拒绝未知 schema、负计数、坏游标和非法触发原因。
def validate_curator_state(state: MemoryCuratorState) -> None:
    if state.schema_version != CURATOR_STATE_SCHEMA_VERSION:
        raise ValueError("unsupported memory curator state schema_version")
    if not isinstance(state.per_thread_cursors, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in state.per_thread_cursors.items()
    ):
        raise ValueError("memory curator per_thread_cursors must be string map")
    if not isinstance(state.active_lease, dict):
        raise ValueError("memory curator active_lease must be an object")
    if any(
        int(value) < 0
        for value in (state.processed_count, state.candidate_count, state.daily_event_count)
    ):
        raise ValueError("memory curator counters cannot be negative")
    if not isinstance(state.pending_reasons, list) or any(
        reason not in CURATOR_TRIGGER_REASONS for reason in state.pending_reasons
    ):
        raise ValueError("memory curator pending_reasons contains unsupported reason")
    if not isinstance(state.pending_reason_generations, dict) or any(
        reason not in CURATOR_TRIGGER_REASONS
        or isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation < 0
        for reason, generation in state.pending_reason_generations.items()
    ):
        raise ValueError("memory curator pending_reason_generations must be a non-negative reason map")


# LLM: 只接受 response.text 中一个 JSON 对象；不从代码块或前后自然语言猜 JSON。
# 函数用途: 把 provider 输出转成严格 daily/candidate extraction。
# LLM: 缺失可空顶层字段的宿主默认值(warnings=[] 等);只用于模型偶发漏字段的确定性补全,不引入新权威。
_EMPTY_FIELD_DEFAULT = {
    "daily_events": [],
    "candidates": [],
    "processed_message_refs": [],
    "processed_audit_refs": [],
    "unresolved_refs": [],
    "warnings": [],
    "next_cursor": {"per_thread_cursors": [], "last_audit_event_id": None},
}


def parse_curator_extraction(text: str) -> CuratorExtraction:
    try:
        payload = json.loads(str(text or ""))
    except json.JSONDecodeError as exc:
        raise ValueError("curator response is not strict JSON") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != CURATOR_OUTPUT_SCHEMA_VERSION:
        raise ValueError("unsupported curator output schema_version")
    required = {
        "schema_version",
        "daily_events",
        "candidates",
        "processed_message_refs",
        "processed_audit_refs",
        "unresolved_refs",
        "warnings",
        "next_cursor",
    }
    missing = required - set(payload)
    extra = set(payload) - required
    if extra:
        raise ValueError("curator response fields do not match the strict contract")
    # 宿主容错:模型偶发漏掉可空顶层字段(如 warnings)时按空默认补全,不阻断整批。
    # 结构化生成仍强制主字段;缺失补全是纯宿主侧确定性修复,不引入新权威。
    if missing:
        payload = {**payload, **{key: _EMPTY_FIELD_DEFAULT[key] for key in missing}}
    validate_curator_response_payload(payload, curator_response_schema())
    return CuratorExtraction(
        daily_events=tuple(_parse_daily_draft(item) for item in _object_array(payload, "daily_events")),
        candidates=tuple(_parse_candidate_observation(item) for item in _object_array(payload, "candidates")),
        processed_message_refs=tuple(normalize_reference_list(payload["processed_message_refs"])),
        processed_audit_refs=tuple(normalize_reference_list(payload["processed_audit_refs"])),
        unresolved_refs=tuple(normalize_reference_list(payload["unresolved_refs"])),
        warnings=tuple(_short_string_array(payload, "warnings", max_items=32, max_chars=500)),
        next_cursor=_object(payload, "next_cursor"),
    )


# LLM: daily draft 未知字段一律拒绝，防 provider 把原始 content/output 偷塞到旁路字段。
# 函数用途: 解析单条 daily 模型草稿。
def _parse_daily_draft(payload: dict[str, object]) -> CuratorDailyDraft:
    allowed = set(CuratorDailyDraft.__dataclass_fields__)
    if set(payload) - allowed:
        raise ValueError("curator daily event contains unknown fields")
    for field_name in ("event_type", "summary", "actor", "origin"):
        if not str(payload.get(field_name) or "").strip():
            raise ValueError(f"curator daily event requires {field_name}")
    return CuratorDailyDraft(
        event_type=str(payload["event_type"]),
        summary=str(payload["summary"]),
        actor=str(payload["actor"]),
        origin=str(payload["origin"]),
        session_id=str(payload.get("session_id") or ""),
        thread_id=str(payload.get("thread_id") or ""),
        request_id=str(payload.get("request_id") or ""),
        task_id=str(payload.get("task_id") or ""),
        run_id=str(payload.get("run_id") or ""),
        message_refs=tuple(normalize_reference_list(payload.get("message_refs") or [])),
        tool_refs=tuple(normalize_reference_list(payload.get("tool_refs") or [])),
        artifact_refs=tuple(normalize_reference_list(payload.get("artifact_refs") or [])),
        decisions=tuple(_short_string_array(payload, "decisions")),
        lessons=tuple(_short_string_array(payload, "lessons")),
        next_actions=tuple(_short_string_array(payload, "next_actions")),
        created_at=str(payload.get("created_at") or ""),
    )


# LLM: CandidateObservation 解析只允许模型提出字段；host-owned ID/status/reviewer 不可由模型覆盖。
# 函数用途: 将一条策展候选草稿交给唯一 CandidateService。
def _parse_candidate_observation(payload: dict[str, object]) -> CandidateObservation:
    allowed = set(CandidateObservation.__dataclass_fields__)
    if set(payload) - allowed:
        raise ValueError("curator candidate contains host-owned or unknown fields")
    return CandidateObservation(
        candidate_type=str(payload.get("candidate_type") or ""),
        content=str(payload.get("content") or ""),
        subject_key=str(payload.get("subject_key") or ""),
        scope=MemoryScope.from_value(payload.get("scope")),
        origin=str(payload.get("origin") or "model_inferred"),
        evidence_refs=tuple(normalize_reference_list(payload.get("evidence_refs") or [])),
        source_message_refs=tuple(
            normalize_reference_list(payload.get("source_message_refs") or [])
        ),
        source_tool_refs=tuple(normalize_reference_list(payload.get("source_tool_refs") or [])),
        source_artifact_refs=tuple(
            normalize_reference_list(payload.get("source_artifact_refs") or [])
        ),
        source_task_ids=tuple(_short_string_array(payload, "source_task_ids")),
        source_run_ids=tuple(_short_string_array(payload, "source_run_ids")),
        observed_at=str(payload.get("observed_at") or ""),
        valid_from=str(payload.get("valid_from") or ""),
        valid_until=str(payload.get("valid_until") or ""),
        confidence=float(payload.get("confidence") or 0.0),
        proposed_action=str(payload.get("proposed_action") or "add"),
        target_entry_id=str(payload.get("target_entry_id") or ""),
        conflicts_with=tuple(_short_string_array(payload, "conflicts_with")),
        promotion_target=str(payload.get("promotion_target") or "long_term"),
        observation_id=str(payload.get("observation_id") or ""),
    )


# LLM: 模型数组字段必须真的是对象数组，不能接受 JSON 字符串内嵌第二次解析。
# 函数用途: 读取顶层对象数组。
def _object_array(payload: dict[str, object], name: str) -> list[dict[str, object]]:
    value = payload.get(name)
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"curator {name} must be an array of objects")
    return [dict(item) for item in value]


# LLM: next_cursor 只作为模型声明保存/核验，不能接受数组或字符串猜测。
# 函数用途: 读取顶层对象字段。
def _object(payload: dict[str, object], name: str) -> dict[str, object]:
    value = payload.get(name)
    if not isinstance(value, dict):
        raise ValueError(f"curator {name} must be an object")
    return dict(value)


# LLM: warnings/IDs 都必须有界，不能成为规避 max_input/output 的长文本通道。
# 函数用途: 读取短字符串数组。
def _short_string_array(
    payload: dict[str, object],
    name: str,
    *,
    max_items: int = 64,
    max_chars: int = 500,
) -> list[str]:
    value = payload.get(name, [])
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"curator {name} must be a string array")
    items = [str(item).strip() for item in value if str(item).strip()]
    if len(items) > max_items or any(len(item) > max_chars for item in items):
        raise ValueError(f"curator {name} exceeds bounded output limits")
    return list(dict.fromkeys(items))


# LLM: response_schema 是 provider 输出约束，不授权任何工具或副作用。
# 函数用途: 提供各 provider 共用的严格顶层 JSON Schema。
def curator_response_schema() -> dict[str, Any]:
    return build_curator_response_schema(
        output_version=CURATOR_OUTPUT_SCHEMA_VERSION,
        candidate_types=CANDIDATE_TYPES,
        origins=CURATOR_MODEL_ORIGINS,
        actions=PROPOSED_ACTIONS,
        promotion_targets=PROMOTION_TARGETS,
    )


__all__ = [
    "CURATOR_MODEL_NOT_CONFIGURED",
    "CURATOR_NOT_CONFIGURED_RETRY_SECONDS",
    "CURATOR_OUTPUT_SCHEMA_VERSION",
    "CURATOR_MODEL_ORIGINS",
    "CURATOR_RUN_SCHEMA_VERSION",
    "CURATOR_STATE_SCHEMA_VERSION",
    "CURATOR_TRIGGER_REASONS",
    "CuratorDailyDraft",
    "CuratorExtraction",
    "CuratorRunResult",
    "MemoryCuratorConfig",
    "MemoryCuratorState",
    "curator_failure_retry_seconds",
    "curator_response_schema",
    "parse_curator_extraction",
    "validate_curator_state",
]
