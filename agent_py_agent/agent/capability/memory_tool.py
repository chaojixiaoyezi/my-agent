from __future__ import annotations

"""remember 工具的 Memory 业务适配层。"""

# LLM: Tool Runtime owns registration, schema enforcement and execution; this module owns only Memory semantics.
# 模块用途: 把 remember 参数转换为统一候选，核验当前消息或工具引用，再交唯一 PromotionService 晋升。

import hashlib
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..conversation.authority import (
    CONVERSATION_AUDIT_PREPARE_ATTR,
    current_conversation_task_attributes,
)
from ..memory_store.candidate_models import CandidateObservation, MemoryScope
from ..memory_store.security import scan_memory_content
from ..tooling.models import (
    BaseTool,
    EffectResolverPolicy,
    IdempotencyPolicy,
    ResourceScopePolicy,
    ToolAvailability,
    ToolHandlerOutcome,
    ToolModelHints,
    ToolModelSpec,
    ToolRuntimePolicy,
)

if TYPE_CHECKING:
    from ..core import SimpleAgent


_MEMORY_KINDS = frozenset({"fact", "event", "project"})
_MEMORY_ORIGINS = frozenset({"user_explicit", "tool_verified", "model_inferred"})
_MEMORY_ACTIONS = frozenset({"add", "replace", "remove"})


# LLM: Memory only supplies its business schema; Tool Runtime owns all protocol and policy fields.
# 函数用途: 描述 remember 唯一的模型可见输入结构。
def build_remember_model_spec() -> ToolModelSpec:
    scope_schema = _remember_scope_schema()
    operation_schema = _remember_operation_schema(scope_schema)
    return ToolModelSpec(
        name="remember",
        description=(
            "管理需要跨会话复用的具体事实、事件和项目知识。新增先进入统一候选并核验证据；"
            "无冲突的 user_explicit/tool_verified 新事实可按保守策略晋升。模型推断、替换和删除只形成候选，"
            "等待统一审核。用户画像、称呼和长期沟通偏好使用 update_persona；教训使用候选到 lesson 链。"
        ),
        input_schema=_remember_input_schema(scope_schema, operation_schema),
        hints=ToolModelHints(
            category="capability",
            use_cases=(
                "用户明确要求长期记住一个具体事实、事件或项目知识",
                "成功工具结果证明了适合跨会话复用的事实",
                "列出正式长期记忆，取得稳定 entry_id 后提出替换或删除",
            ),
            avoid_when=(
                "USER、SOUL、AGENTS 人格内容应使用 update_persona",
                "教训和高频规则进入 lesson/HOT 候选链",
                "一次性任务细节、验证码、密钥和临时口令不得长期保存",
            ),
            keywords=("记住", "remember", "长期事实", "项目知识", "事件"),
            examples=(
                '{"tool":"remember","action":"add","content":"moneywise 项目使用 UTC 保存时间",'
                '"kind":"project","origin":"user_explicit","subject_key":"project.moneywise.timezone",'
                '"scope":{"scope_type":"project","scope_key":"project:moneywise"}}',
                '{"tool":"remember","action":"list"}',
            ),
        ),
    )


# LLM: Scope is one reusable typed object for single and batch operations; natural-language
# content cannot add or reinterpret scope keys.
# 函数用途: 构造 remember 的结构化 scope Schema。
def _remember_scope_schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "scope_type": {
                "type": "string",
                "enum": [
                    "global",
                    "personal",
                    "company",
                    "project",
                    "task_class",
                    "session",
                    "temporary",
                ],
                "description": "适用范围类型；不能用正文或时间猜范围。",
            },
            "scope_key": {
                "type": "string",
                "description": "稳定范围键，例如 personal、company、project:moneywise。",
            },
            "applies_when": {
                "type": "string",
                "description": "可选的人类可读适用条件，不参与范围身份判断。",
            },
            "excludes_when": {
                "type": "string",
                "description": "可选排除条件，不参与范围身份判断。",
            },
        },
        "required": ["scope_type", "scope_key"],
        "additionalProperties": False,
    }


# LLM: Batch items use the same domain fields as a single mutation and cannot nest batch/list
# control operations.
# 函数用途: 构造 remember batch 单项 Schema。
def _remember_operation_schema(scope_schema: dict[str, object]) -> dict[str, object]:
    properties = _remember_business_fields(scope_schema)
    properties["action"] = {
        "type": "string",
        "enum": ["add", "replace", "remove"],
    }
    return {
        "type": "object",
        "properties": properties,
        "required": ["action", "origin"],
        "additionalProperties": False,
    }


# LLM: Field construction is shared by top-level and batch schemas, preventing drift in origin,
# kind, evidence, or expiry semantics.
# 函数用途: 构造 remember 的业务字段 Schema。
def _remember_business_fields(scope_schema: dict[str, object]) -> dict[str, object]:
    return {
        "entry_id": {"type": "string"},
        "content": {"type": "string"},
        "kind": {"type": "string", "enum": ["fact", "event", "project"]},
        "tags": {"type": "array", "items": {"type": "string"}},
        "origin": {
            "type": "string",
            "enum": ["user_explicit", "tool_verified", "model_inferred"],
        },
        "evidence_refs": {"type": "array", "items": {"type": "string"}},
        "subject_key": {"type": "string"},
        "scope": scope_schema,
        "valid_from": {"type": "string"},
        "valid_until": {"type": "string"},
    }


# LLM: Top-level action adds list/batch controls while reusing the exact single-operation
# business field definitions.
# 函数用途: 构造 remember ToolModelSpec 的顶层 input_schema。
def _remember_input_schema(
    scope_schema: dict[str, object],
    operation_schema: dict[str, object],
) -> dict[str, object]:
    fields = _remember_business_fields(scope_schema)
    fields["action"] = {
        "type": "string",
        "enum": ["add", "list", "replace", "remove", "batch"],
    }
    fields["operations"] = {"type": "array", "items": operation_schema}
    for name, description in _remember_parameter_descriptions().items():
        fields[name]["description"] = description
    return {
        "type": "object",
        "properties": fields,
        "additionalProperties": False,
    }


# LLM: Chinese descriptions are kept beside the canonical ToolModelSpec and explain every English
# field exposed to the model/admin reader.
# 函数用途: 返回 remember 参数的中文说明。
def _remember_parameter_descriptions() -> dict[str, str]:
    return {
        "action": "add(默认)、list、replace、remove 或 batch。",
        "entry_id": "replace/remove 的正式长期记忆稳定编号；必须来自 list。",
        "content": "add/replace 的简短、自包含正文。",
        "kind": "fact、event 或 project；lesson 不在长期 JSONL 中。",
        "tags": "可选短标签，只辅助检索，不决定事实权威。",
        "origin": "user_explicit、tool_verified 或 model_inferred；不能从正文猜。",
        "evidence_refs": "tool_verified 的当前成功工具 operation/call/artifact 引用。",
        "subject_key": "稳定主题键；同主题同 scope 不允许静默新增冲突事实。",
        "scope": "结构化适用范围，含 scope_type 和 scope_key。",
        "valid_from": "可选带时区 ISO 生效时间。",
        "valid_until": "可选带时区 ISO 失效时间；temporary scope 必须填写。",
        "operations": "batch 的操作数组；整批只原子进入统一候选账本。",
    }


# LLM: The handler must not write long_term, candidates or Persona through any path other than assembled services.
# 类用途: 连接统一工具运行时与 Memory 候选/晋升服务。
class RememberTool(BaseTool):
    model_spec = build_remember_model_spec()
    runtime_policy = ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy(
            "mutating",
            by_parameter=((
                "action",
                (
                    ("list", "read_only"),
                    ("add", "mutating"),
                    ("replace", "mutating"),
                    ("remove", "mutating"),
                    ("batch", "mutating"),
                ),
            ),),
        ),
        idempotency_policy=IdempotencyPolicy("operation"),
        resource_scopes=ResourceScopePolicy(parameter_names=("entry_id", "subject_key")),
    )

    # LLM: 构造器只绑定 composition root 中已接线的 Candidate/Promotion/long-term 服务，不创建 fallback。
    # 函数用途: 初始化当前 owner 的唯一 remember 模型工具适配器。
    def __init__(self, agent: SimpleAgent):
        self.agent = agent

    # LLM: Named Audit preparation cannot mutate owner Memory; availability is a structured runtime decision.
    # 函数用途: Audit 准备轮次禁用 owner 记忆变更，其他轮次保持可用。
    def availability(self) -> ToolAvailability:
        attributes = current_conversation_task_attributes(self.agent)
        if attributes.get(CONVERSATION_AUDIT_PREPARE_ATTR) is True:
            return ToolAvailability.unavailable(
                "named Audit preparation is task-scoped and cannot mutate owner memory"
            )
        return ToolAvailability.ready()

    # LLM: Every mutation becomes CandidateObservation first; only PromotionService may commit a formal target.
    # 函数用途: 列出正式记忆，或原子记录候选并按保守策略尝试晋升新增事实。
    def execute(self, params: dict[str, object]) -> ToolHandlerOutcome:
        action = str(params.get("action") or "add").strip().lower()
        if action not in {"add", "list", "replace", "remove", "batch"}:
            return _memory_error("action 必须是 add/list/replace/remove/batch", "TOOL_INVALID_ARGUMENTS")
        memory = getattr(self.agent, "memory", None)
        if memory is None or not hasattr(memory, "all"):
            return _memory_error("正式长期记忆不可用", "TOOL_UNAVAILABLE")
        if action == "list":
            return _memory_list_result(memory)
        operations = _memory_operations(params, action)
        prepared = _prepare_observations(self.agent, memory, operations)
        if isinstance(prepared, ToolHandlerOutcome):
            return prepared
        candidates = getattr(self.agent, "memory_candidates", None)
        promotion = getattr(self.agent, "memory_promotion", None)
        if candidates is None or not hasattr(candidates, "observe_many") or promotion is None:
            return _memory_error(
                "统一候选或晋升服务不可用，拒绝回退直接写长期记忆。",
                "MEMORY_SERVICE_UNAVAILABLE",
            )
        try:
            observed = candidates.observe_many(prepared)
        except Exception as exc:  # noqa: BLE001 - map repository failures to one stable tool result
            return _memory_error(f"候选写入失败: {exc}", "MEMORY_CANDIDATE_WRITE_FAILED")
        promotion_results: list[dict[str, object]] = []
        active_changed = False
        for operation, candidate in zip(operations, observed, strict=True):
            origin = str(operation.get("origin") or "")
            can_auto = (
                len(operations) == 1
                and str(operation.get("action") or "") == "add"
                and origin in {"user_explicit", "tool_verified"}
            )
            if not can_auto:
                promotion_results.append(
                    {
                        "candidate_id": candidate.candidate_id,
                        "promoted": False,
                        "status": candidate.status,
                        "reason_code": "REVIEW_REQUIRED",
                        "promotion_ref": "",
                    }
                )
                continue
            result = promotion.promote(candidate.candidate_id, automatic=True)
            payload = result.to_dict()
            promotion_results.append(payload)
            active_changed = active_changed or bool(payload.get("promoted"))
        return ToolHandlerOutcome(
            "remember",
            True,
            json.dumps(
                {
                    "ok": True,
                    "action": action,
                    "active_memory_changed": active_changed,
                    "results": promotion_results,
                    "hint": _remember_result_hint(action, active_changed),
                },
                ensure_ascii=False,
            ),
        )


# LLM: Normalization only projects declared fields and never invents origin, scope or subject from content.
# 函数用途: 把单操作或 batch 参数统一成操作数组。
def _memory_operations(
    params: dict[str, object],
    action: str,
) -> list[dict[str, object]]:
    if action == "batch":
        raw = params.get("operations")
        if not isinstance(raw, list):
            return []
        return [dict(item) for item in raw if isinstance(item, dict)]
    allowed = {
        "entry_id",
        "content",
        "kind",
        "tags",
        "origin",
        "evidence_refs",
        "subject_key",
        "scope",
        "valid_from",
        "valid_until",
    }
    operation = {key: value for key, value in params.items() if key in allowed}
    operation["action"] = action
    return [operation]


# LLM: All operations validate before observe_many, so an invalid batch leaves no partial candidates.
# 函数用途: 校验操作并构造统一 CandidateObservation；返回工具错误时调用方不得继续写入。
def _prepare_observations(
    agent: object,
    memory: object,
    operations: list[dict[str, object]],
) -> list[CandidateObservation] | ToolHandlerOutcome:
    if not operations:
        return _memory_error("operations 必须是非空数组", "TOOL_INVALID_ARGUMENTS")
    observations: list[CandidateObservation] = []
    for index, operation in enumerate(operations, start=1):
        prepared = _prepare_observation(agent, memory, operation, index=index)
        if isinstance(prepared, ToolHandlerOutcome):
            return prepared
        observations.append(prepared)
    return observations


# LLM: Exact target records supply replace/remove scope and content; free text never selects an existing entry.
# 函数用途: 把一个 remember 操作转换为候选，并补齐真实消息或工具证据引用。
def _prepare_observation(
    agent: object,
    memory: object,
    operation: dict[str, object],
    *,
    index: int,
) -> CandidateObservation | ToolHandlerOutcome:
    identity = _validated_action_origin(operation, index=index)
    if isinstance(identity, ToolHandlerOutcome):
        return identity
    action, origin = identity
    target_result = _resolved_target(memory, operation, action=action, index=index)
    if isinstance(target_result, ToolHandlerOutcome):
        return target_result
    entry_id, target = target_result
    body = _validated_candidate_body(operation, target, action=action, index=index)
    if isinstance(body, ToolHandlerOutcome):
        return body
    content, _tags = body
    domain = _validated_domain_fields(operation, target, index=index)
    if isinstance(domain, ToolHandlerOutcome):
        return domain
    provenance = _observation_provenance(agent, operation, origin=origin, content=content)
    observation_id = _observation_id(
        request_id=provenance.request_id,
        operation_index=index,
        action=action,
        entry_id=entry_id,
        content=content,
    )
    return CandidateObservation(
        candidate_type={"fact": "long_term_fact", "event": "event", "project": "project"}[
            domain.kind
        ],
        content=content,
        subject_key=domain.subject_key,
        scope=domain.scope,
        origin=origin,
        evidence_refs=provenance.evidence_refs,
        source_message_refs=provenance.message_refs,
        source_tool_refs=provenance.tool_refs,
        source_task_ids=(provenance.task_id,) if provenance.task_id else (),
        source_run_ids=(provenance.run_id,) if provenance.run_id else (),
        valid_from=str(operation.get("valid_from") or "").strip(),
        valid_until=str(operation.get("valid_until") or "").strip(),
        confidence={"user_explicit": 1.0, "tool_verified": 1.0, "model_inferred": 0.5}[origin],
        proposed_action=action,
        target_entry_id=entry_id,
        promotion_target="long_term",
        observation_id=observation_id,
    )


# LLM: Action and origin are explicit enums; no default or content inference can acquire fact
# authority.
# 函数用途: 校验一个 remember 操作的控制字段。
def _validated_action_origin(
    operation: dict[str, object],
    *,
    index: int,
) -> tuple[str, str] | ToolHandlerOutcome:
    action = str(operation.get("action") or "").strip().lower()
    if action not in _MEMORY_ACTIONS:
        return _memory_error(
            f"第 {index} 个操作的 action 必须是 add/replace/remove",
            "TOOL_INVALID_ARGUMENTS",
        )
    origin = str(operation.get("origin") or "").strip().lower()
    if origin not in _MEMORY_ORIGINS:
        return _memory_error(
            f"第 {index} 个操作缺少有效 origin",
            "TOOL_INVALID_ARGUMENTS",
            hint="必须显式使用 user_explicit/tool_verified/model_inferred。",
        )
    return action, origin


# LLM: Replace/remove resolve exactly one active entry_id and never search by subject, text, or
# timestamp.
# 函数用途: 校验目标编号并读取正式记录。
def _resolved_target(
    memory: object,
    operation: dict[str, object],
    *,
    action: str,
    index: int,
) -> tuple[str, object | None] | ToolHandlerOutcome:
    entry_id = str(operation.get("entry_id") or "").strip()
    if action not in {"replace", "remove"}:
        return entry_id, None
    if not entry_id:
        return _memory_error(
            f"第 {index} 个操作缺少 entry_id",
            "TOOL_INVALID_ARGUMENTS",
            hint="先 action=list 取得稳定 entry_id。",
        )
    target = _memory_record(memory, entry_id)
    if target is None:
        return _memory_error(
            f"第 {index} 个操作的 entry_id 不存在",
            "MEMORY_ENTRY_NOT_FOUND",
        )
    return entry_id, target


# LLM: Content is bounded by the shared threat and transient-credential guards before any
# CandidateService mutation occurs.
# 函数用途: 选择 add/replace/remove 正文并验证可进入候选。
def _validated_candidate_body(
    operation: dict[str, object],
    target: object | None,
    *,
    action: str,
    index: int,
) -> tuple[str, list[str]] | ToolHandlerOutcome:
    content = (
        str(getattr(target, "content", "") or "").strip()
        if action == "remove"
        else str(operation.get("content") or "").strip()
    )
    if not content:
        return _memory_error(f"第 {index} 个操作缺少 content", "TOOL_INVALID_ARGUMENTS")
    tags = _normalize_tags(operation.get("tags"))
    scan = scan_memory_content(content)
    if not scan.safe:
        return _memory_error(scan.reason(), "MEMORY_INJECTION_BLOCKED")
    if not classify_memory_retention(content, tags).durable:
        return _memory_error(
            "内容含临时验证码、解锁码或一次性凭据，不能进入长期候选。",
            "MEMORY_TRANSIENT_DATA_BLOCKED",
        )
    return content, tags


# LLM: Subject, kind, and scope are validated as one domain identity; exact targets supply their
# existing identity and additions must declare it.
# 类用途: 保存 remember 候选的主题、类型与适用范围。
@dataclass(frozen=True)
class _MemoryDomainFields:
    subject_key: str
    kind: str
    scope: MemoryScope


# LLM: Target scope/subject cannot be changed by a replace/remove request, and temporary scope
# always carries an explicit expiry.
# 函数用途: 校验候选的 subject_key、kind 和 scope。
def _validated_domain_fields(
    operation: dict[str, object],
    target: object | None,
    *,
    index: int,
) -> _MemoryDomainFields | ToolHandlerOutcome:
    attributes = getattr(target, "attributes", {}) if target is not None else {}
    attributes = attributes if isinstance(attributes, dict) else {}
    subject_key = str(operation.get("subject_key") or "").strip()
    if target is not None:
        resolved = _target_subject(subject_key, attributes)
        if isinstance(resolved, ToolHandlerOutcome):
            return resolved
        subject_key = resolved
    kind = str(operation.get("kind") or getattr(target, "kind", "fact") or "fact").strip()
    if kind not in _MEMORY_KINDS:
        return _memory_error(
            f"第 {index} 个操作的 kind 必须是 fact/event/project；lesson 走 lesson 候选链。",
            "TOOL_INVALID_ARGUMENTS",
        )
    if not subject_key and kind != "event":
        return _memory_error(
            f"第 {index} 个操作缺少稳定 subject_key",
            "TOOL_INVALID_ARGUMENTS",
        )
    try:
        scope = _operation_scope(operation, target_attributes=attributes)
    except ValueError as exc:
        return _memory_error(str(exc), "TOOL_INVALID_ARGUMENTS")
    if scope.scope_type == "temporary" and not str(operation.get("valid_until") or "").strip():
        return _memory_error("temporary scope 必须提供 valid_until。", "TOOL_INVALID_ARGUMENTS")
    return _MemoryDomainFields(subject_key, kind, scope)


# LLM: A target lacking migrated subject identity cannot be safely replaced, and a caller cannot
# relabel it by supplying a different subject.
# 函数用途: 从正式目标解析不可变 subject_key。
def _target_subject(
    requested: str,
    attributes: dict[str, object],
) -> str | ToolHandlerOutcome:
    authoritative = str(attributes.get("subject_key") or "").strip()
    if not authoritative:
        return _memory_error(
            "旧条目缺少 subject_key，必须先运行 memory migrate。",
            "MEMORY_LEGACY_ENTRY_REQUIRES_MIGRATION",
        )
    if requested and requested != authoritative:
        return _memory_error(
            "subject_key 与 entry_id 的正式记录不一致。",
            "MEMORY_TARGET_SCOPE_MISMATCH",
        )
    return authoritative


# LLM: Provenance contains only current typed runtime IDs and canonical message/tool refs; it
# never scans prior text for a plausible source.
# 类用途: 保存一条 remember observation 的来源引用。
@dataclass(frozen=True)
class _ObservationProvenance:
    request_id: str
    task_id: str
    run_id: str
    evidence_refs: tuple[dict[str, object], ...]
    message_refs: tuple[dict[str, object], ...]
    tool_refs: tuple[dict[str, object], ...]


# LLM: Origin selects exactly one evidence adapter; model_inferred receives no fabricated user or
# tool evidence.
# 函数用途: 从当前运行构造 observation provenance。
def _observation_provenance(
    agent: object,
    operation: dict[str, object],
    *,
    origin: str,
    content: str,
) -> _ObservationProvenance:
    message_refs: tuple[dict[str, object], ...] = ()
    tool_refs: tuple[dict[str, object], ...] = ()
    if origin == "user_explicit":
        message_ref = _current_user_message_ref(agent, content)
        message_refs = (message_ref,) if message_ref else ()
    elif origin == "tool_verified":
        tool_refs = tuple(
            _current_tool_evidence_refs(
                agent,
                _normalize_refs(operation.get("evidence_refs")),
            )
        )
    current = getattr(agent, "_current_run_params", None)
    request_id = str(getattr(current, "request_id", "") or "").strip()
    task_id = str(getattr(current, "task_id", "") or "").strip()
    run_id = str(getattr(current, "run_id", "") or "").strip()
    generic_refs = (({"ref_id": request_id, "kind": "request"},) if request_id else ())
    return _ObservationProvenance(
        request_id,
        task_id,
        run_id,
        generic_refs,
        message_refs,
        tool_refs,
    )


# LLM: Replace/remove inherit typed scope only from the exact formal target; add requires explicit structured scope.
# 函数用途: 校验新增范围，或从目标条目的结构化属性恢复范围。
def _operation_scope(
    operation: dict[str, object],
    *,
    target_attributes: dict[str, object],
) -> MemoryScope:
    requested = operation.get("scope")
    if target_attributes:
        authoritative = MemoryScope(
            str(target_attributes.get("scope_type") or ""),
            str(target_attributes.get("scope_key") or ""),
            str(target_attributes.get("applies_when") or ""),
            str(target_attributes.get("excludes_when") or ""),
        )
        authoritative = MemoryScope.from_value(authoritative)
        if requested not in (None, "") and MemoryScope.from_value(requested) != authoritative:
            raise ValueError("scope 与 entry_id 的正式记录不一致。")
        return authoritative
    return MemoryScope.from_value(requested)


# LLM: user_explicit evidence resolves the exact current Gateway request in ConversationStore, never a text search guess.
# 函数用途: 生成可由 PromotionService 回查的当前用户消息引用；缺失或不唯一时返回空。
def _current_user_message_ref(agent: object, candidate_content: str) -> dict[str, object] | None:
    current = getattr(agent, "_current_run_params", None)
    attrs = getattr(current, "task_attributes", None)
    attrs = attrs if isinstance(attrs, dict) else {}
    thread_id = str(attrs.get("conversation_thread_id") or "").strip()
    request_id = str(getattr(current, "request_id", "") or "").strip()
    store = getattr(agent, "conversation_store", None)
    reader = getattr(store, "recent_messages_report", None)
    if not thread_id or not request_id or not callable(reader):
        return None
    try:
        messages, errors = reader(thread_id, limit=0)
    except Exception:
        return None
    matches = [
        item
        for item in messages
        if str(getattr(item, "role", "") or "") == "user"
        and str((getattr(item, "metadata", {}) or {}).get("gateway_request_id") or "")
        == request_id
    ]
    if errors or len(matches) != 1:
        return None
    message = matches[0]
    content = str(getattr(message, "content", "") or "")
    if not content:
        return None
    quote = candidate_content if candidate_content in content else content[:300]
    return {
        "message_id": str(getattr(message, "message_id", "") or ""),
        "thread_id": thread_id,
        "role": "user",
        "quote": quote[:300],
        "content_hash": "sha256:"
        + hashlib.sha256(content.encode("utf-8", "replace")).hexdigest(),
    }


# LLM: This function only canonicalizes current typed tool records; PromotionService rechecks the Tool Agent ledger.
# 函数用途: 把模型提交的当前工具句柄映射成 owner/run/operation 结构化引用。
def _current_tool_evidence_refs(
    agent: object,
    requested_refs: list[str],
) -> list[dict[str, object]]:
    if not requested_refs:
        return []
    current_loop = getattr(agent, "_current_tool_loop_params", None)
    archive = getattr(current_loop, "archive_tool_calls", None)
    if not isinstance(archive, list):
        return []
    current = getattr(agent, "_current_run_params", None)
    default_run_id = str(getattr(current, "run_id", "") or "").strip()
    owner_id = str(getattr(getattr(agent, "home_paths", None), "owner_id", "") or "").strip()
    found: list[dict[str, object]] = []
    for record in archive:
        if not isinstance(record, dict):
            continue
        operation = record.get("operation")
        operation = operation if isinstance(operation, dict) else {}
        identifiers = {
            str(value).strip()
            for value in (
                record.get("call_id"),
                record.get("scoped_call_id"),
                record.get("operation_id"),
                operation.get("operation_id"),
                record.get("artifact_ref"),
                record.get("effect_source_ref"),
            )
            if str(value or "").strip()
        }
        if not identifiers.intersection(requested_refs):
            continue
        operation_id = str(record.get("operation_id") or operation.get("operation_id") or "").strip()
        run_id = str(record.get("run_id") or default_run_id).strip()
        if not owner_id or not run_id or not operation_id:
            continue
        ref: dict[str, object] = {
            "owner_id": owner_id,
            "run_id": run_id,
            "operation_id": operation_id,
            "tool": str(record.get("tool") or record.get("name") or ""),
        }
        for key in ("call_id", "artifact_ref", "effect_source_ref"):
            value = str(record.get(key) or "").strip()
            if value:
                ref[key] = value
        found.append(ref)
    unique: dict[str, dict[str, object]] = {}
    for item in found:
        unique[json.dumps(item, ensure_ascii=False, sort_keys=True)] = item
    return list(unique.values())


# LLM: Stable observation identity uses host request/index and semantic hash; retries cannot inflate occurrence_count.
# 函数用途: 为 remember 本轮某个操作生成幂等观察编号。
def _observation_id(
    *,
    request_id: str,
    operation_index: int,
    action: str,
    entry_id: str,
    content: str,
) -> str:
    material = json.dumps(
        {
            "request_id": request_id,
            "operation_index": operation_index,
            "action": action,
            "entry_id": entry_id,
            "content_hash": hashlib.sha256(content.encode("utf-8", "replace")).hexdigest(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "remember-observation-" + hashlib.sha256(material.encode()).hexdigest()[:24]


# LLM: Exact entry_id lookup is the only selection mechanism for replace/remove.
# 函数用途: 从正式 active 长期记忆中查找稳定编号。
def _memory_record(memory: object, entry_id: str) -> object | None:
    try:
        return next(
            (item for item in memory.all() if str(getattr(item, "entry_id", "")) == entry_id),
            None,
        )
    except Exception:
        return None


# LLM: Listing reads only active long_term authority; candidates, ops and daily never appear here.
# 函数用途: 返回正式长期记忆及其稳定编号、版本和结构化范围。
def _memory_list_result(memory: object) -> ToolHandlerOutcome:
    try:
        entries = [_memory_record_payload(record) for record in memory.all()]
    except Exception as exc:  # noqa: BLE001
        return _memory_error(f"读取失败: {exc}", "TOOL_EXECUTION_FAILED")
    return ToolHandlerOutcome(
        "remember",
        True,
        json.dumps({"ok": True, "action": "list", "entries": entries}, ensure_ascii=False),
    )


# LLM: Public list payload exposes formal content but not internal candidate/status history.
# 函数用途: 将一条正式长期记忆转换成 remember list 的业务结果。
def _memory_record_payload(record: object) -> dict[str, object]:
    attributes = getattr(record, "attributes", None)
    attributes = attributes if isinstance(attributes, dict) else {}
    return {
        "entry_id": str(getattr(record, "entry_id", "") or ""),
        "version": int(getattr(record, "version", 1) or 1),
        "content": str(getattr(record, "content", "") or ""),
        "kind": str(getattr(record, "kind", "fact") or "fact"),
        "tags": list(getattr(record, "tags", None) or []),
        "subject_key": str(attributes.get("subject_key") or ""),
        "scope": {
            "scope_type": str(attributes.get("scope_type") or "legacy"),
            "scope_key": str(attributes.get("scope_key") or "legacy"),
            "applies_when": str(attributes.get("applies_when") or ""),
            "excludes_when": str(attributes.get("excludes_when") or ""),
        },
        "created_at": float(getattr(record, "created_at", 0.0) or 0.0),
        "updated_at": float(getattr(record, "updated_at", 0.0) or 0.0),
        "expires_at": float(getattr(record, "expires_at", 0.0) or 0.0),
    }


# LLM: Tool errors carry stable codes; no caller should parse the Chinese message for control flow.
# 函数用途: 构造 remember 的统一结构化失败结果。
def _memory_error(message: str, code: str, *, hint: str = "") -> ToolHandlerOutcome:
    payload = {"error": message}
    if hint:
        payload["hint"] = hint
    return ToolHandlerOutcome(
        "remember",
        False,
        json.dumps(payload, ensure_ascii=False),
        error_code=code,
    )


# LLM: Result hints explain whether only candidates or formal facts changed; they are not machine state.
# 函数用途: 给模型一条不会误报正式保存的简短说明。
def _remember_result_hint(action: str, active_changed: bool) -> str:
    if active_changed:
        return "精确证据和保守策略已通过，正式长期记忆已更新。"
    if action == "batch":
        return "整批候选已原子写入；batch 不做部分自动晋升，等待统一审核。"
    return "候选已记录，但正式长期记忆未改变；请依据 reason_code 走审核或补证据。"


# LLM: Tag normalization is display/retrieval metadata only; tags never select scope or authorization.
# 函数用途: 清理空标签并保持原顺序去重。
def _normalize_tags(raw: object) -> list[str]:
    values = raw if isinstance(raw, list) else [raw] if isinstance(raw, str) else []
    return list(dict.fromkeys(str(item).strip() for item in values if str(item).strip()))


# LLM: Requested refs are opaque exact handles; this helper never parses paths or prose.
# 函数用途: 清理空工具引用并保持原顺序去重。
def _normalize_refs(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    return list(dict.fromkeys(str(item).strip() for item in raw if str(item).strip()))


_TRANSIENT_CODE_PATTERNS = (
    re.compile(
        r"(?:验证码|校验码|动态码|一次性(?:密码|口令|代码)|临时(?:密码|口令|码)|"
        r"解锁码|卡片密码|登录密码|访问口令|OTP|one[- ]time (?:password|code)|unlock code)"
        r"\s*(?:是|为|[:：=])?\s*[A-Za-z0-9][A-Za-z0-9._-]{3,63}",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:otp|pin|passcode|verification[_ -]?code)\s*[:=]\s*[A-Za-z0-9._-]{4,64}\b",
        re.IGNORECASE,
    ),
)
_TRANSIENT_TAGS = frozenset(
    {"otp", "password", "passcode", "verification-code", "temporary-code", "unlock-code", "secret"}
)


# LLM: Retention classification is a bounded safety guard, not semantic memory importance scoring.
# 类用途: 表示内容是否允许进入长期候选；code 供机器判断，reason 只供说明。
@dataclass(frozen=True)
class MemoryRetentionDecision:
    durable: bool
    code: str
    reason: str

    # LLM: Serialization names are stable tool/test contract fields.
    # 函数用途: 输出可记录的留存裁决。
    def to_dict(self) -> dict[str, object]:
        return {"durable": self.durable, "code": self.code, "reason": self.reason}


# LLM: This guard only rejects explicit credential tags/value patterns; it does not infer long-term value.
# 函数用途: 拒绝一次性凭据进入候选，普通“密码学”讨论不会被误伤。
def classify_memory_retention(
    content: str,
    tags: list[str] | None = None,
) -> MemoryRetentionDecision:
    normalized_tags = {str(item).strip().lower() for item in (tags or []) if str(item).strip()}
    if normalized_tags & _TRANSIENT_TAGS:
        return MemoryRetentionDecision(False, "transient_credential_tag", "标签表明内容是临时凭据")
    if any(pattern.search(str(content or "")) for pattern in _TRANSIENT_CODE_PATTERNS):
        return MemoryRetentionDecision(
            False,
            "transient_credential_pattern",
            "内容包含临时凭据标签和值",
        )
    return MemoryRetentionDecision(True, "durable_candidate", "未命中临时凭据规则")


__all__ = [
    "MemoryRetentionDecision",
    "RememberTool",
    "build_remember_model_spec",
    "classify_memory_retention",
]
