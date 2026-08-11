from __future__ import annotations

"""Host-owned action obligations derived before the executable tool turn.

The model may propose which real action a request needs, but only this typed
contract can prevent an unevidenced final answer.  Tool prose and tool output
text never mutate action state.
"""

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from ..tooling.runtime_contracts import ToolCall, ToolChoice, ToolResult

_ACTION_STATUSES = frozenset(
    {
        "open",
        "satisfied",
        "unfinished",
        "needs_user_input",
        "approval_required",
        "blocked",
    }
)
_EFFECTS = frozenset({"read_only", "mutating", "dangerous"})
_EFFECT_RANK = {"read_only": 0, "mutating": 1, "dangerous": 2}


def _tool_effects_by_name(runtime_snapshot: object) -> dict[str, str]:
    """工具名 -> 默认效果等级(用于 required action 评估一致性校验)。"""
    effects: dict[str, str] = {}
    for runtime in tuple(getattr(runtime_snapshot, "runtimes", ()) or ()):
        name = str(getattr(getattr(runtime, "model_spec", None), "name", "") or "").strip()
        resolver = getattr(getattr(runtime, "runtime_policy", None), "effect_resolver", None)
        default = str(getattr(resolver, "default_effect", "") or "").strip().lower()
        if name and default in _EFFECTS:
            effects[name] = default
    return effects
_EXITS = frozenset({"unfinished", "needs_user_input", "approval_required", "blocked"})


@dataclass
class RequiredAction:
    action_id: str
    source_turn_id: str
    kind: str
    allowed_tools: tuple[str, ...]
    effect_ceiling: str
    status: str = "open"
    evidence_call_ids: list[str] = field(default_factory=list)
    acceptable_exits: tuple[str, ...] = (
        "unfinished",
        "needs_user_input",
        "approval_required",
        "blocked",
    )
    blocked_reason: str = ""
    description: str = ""
    success_criteria: str = ""
    no_tool_attempts: int = 0

    def __post_init__(self) -> None:
        self.action_id = str(self.action_id or "").strip()
        self.source_turn_id = str(self.source_turn_id or "").strip()
        self.kind = str(self.kind or "execute").strip().lower()
        self.allowed_tools = tuple(
            dict.fromkeys(
                str(item or "").strip() for item in self.allowed_tools if str(item or "").strip()
            )
        )
        self.effect_ceiling = str(self.effect_ceiling or "read_only").strip().lower()
        self.status = str(self.status or "open").strip().lower()
        self.acceptable_exits = tuple(
            dict.fromkeys(str(item or "").strip().lower() for item in self.acceptable_exits)
        )
        self.blocked_reason = str(self.blocked_reason or "").strip()
        self.description = str(self.description or "").strip()
        self.success_criteria = str(self.success_criteria or "").strip()
        self.no_tool_attempts = max(0, int(self.no_tool_attempts or 0))
        if not self.action_id or not self.source_turn_id:
            raise ValueError("required action needs action_id and source_turn_id")
        if self.effect_ceiling not in _EFFECTS:
            raise ValueError(f"invalid required action effect ceiling: {self.effect_ceiling}")
        if self.status not in _ACTION_STATUSES:
            raise ValueError(f"invalid required action status: {self.status}")
        if any(item not in _EXITS for item in self.acceptable_exits):
            raise ValueError("invalid required action acceptable exit")

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "source_turn_id": self.source_turn_id,
            "kind": self.kind,
            "allowed_tools": list(self.allowed_tools),
            "effect_ceiling": self.effect_ceiling,
            "status": self.status,
            "evidence_call_ids": list(self.evidence_call_ids),
            "acceptable_exits": list(self.acceptable_exits),
            "blocked_reason": self.blocked_reason,
            "description": self.description,
            "success_criteria": self.success_criteria,
            "no_tool_attempts": self.no_tool_attempts,
        }


@dataclass(frozen=True)
class RequiredActionAssessment:
    actions: tuple[RequiredAction, ...] = ()
    source: str = "none"
    error: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    requires_action: bool | None = None


def assess_required_actions(
    *,
    backend: object,
    user_prompt: str,
    runtime_snapshot: object,
    run_id: str,
    source_turn_id: str,
    structured_sources: Iterable[object] = (),
) -> RequiredActionAssessment:
    """Prefer explicit host contracts, otherwise ask for one strict semantic assessment."""

    explicit = _actions_from_structured_sources(
        structured_sources,
        runtime_snapshot=runtime_snapshot,
        run_id=run_id,
        source_turn_id=source_turn_id,
    )
    if explicit:
        return RequiredActionAssessment(
            explicit,
            "structured_contract",
            requires_action=True,
        )
    generate = getattr(backend, "generate_structured", None)
    if not callable(generate) or runtime_snapshot is None or not _has_structured_override(backend):
        return RequiredActionAssessment(
            source="unavailable", error="structured assessor unavailable"
        )
    names = tuple(getattr(runtime_snapshot, "available_tool_names", ()) or ())
    schema = _assessment_schema(names)
    prompt = _assessment_prompt(user_prompt, runtime_snapshot)
    last_error = ""
    for attempt in range(2):
        try:
            response = generate(prompt, response_schema=schema)
            raw = json.loads(str(getattr(response, "text", "") or ""))
            actions = _actions_from_assessment(
                raw,
                runtime_snapshot=runtime_snapshot,
                run_id=run_id,
                source_turn_id=source_turn_id,
            )
            return RequiredActionAssessment(
                actions,
                "model_structured",
                raw=raw,
                requires_action=bool(raw["requires_action"]),
            )
        except Exception as exc:  # noqa: BLE001 - assessment is bounded and non-executable
            last_error = f"{type(exc).__name__}: {exc}"
            prompt += (
                "\nThe previous response failed strict validation. Retry through the backend's "
                "required structured-response channel, matching the supplied schema exactly. "
                "Do not add Markdown, prose, XML, or a textual imitation of a tool call."
            )
            if attempt:
                break
    return RequiredActionAssessment(source="model_structured", error=last_error)


def _has_structured_override(backend: object) -> bool:
    from ..backends.base import BaseBackend

    implementation = getattr(type(backend), "generate_structured", None)
    return implementation is not None and implementation is not BaseBackend.generate_structured


def tool_choice_for_required_actions(
    snapshot: object | None,
    provider_tools: list[dict[str, Any]] | None,
) -> ToolChoice:
    actions = tuple(getattr(snapshot, "required_actions", ()) or ())
    open_actions = [item for item in actions if item.status == "open"]
    if open_actions:
        # open action 不强制工具选择:强制 specific/required 会在"先读后改"等前置
        # 依赖链上死锁(真机铁证 2026-08-08:修 main.go 的 action allowed_tools=
        # [edit_file],模型 read_file 被 TOOL_CHOICE_VIOLATION 连拦 3 轮 break)。
        # 工具权限与 effect 上限由执行层 _required_action_decision 硬约束
        # (REQUIRED_ACTION_TOOL_NOT_ALLOWED / REQUIRED_ACTION_EFFECT_CEILING_EXCEEDED),
        # tool_choice 只决定"是否调用",由主循环模型自主(对齐 长期助手/会话运行时)。
        return ToolChoice.auto("open_required_action")
    if actions:
        # 动作全 settled 也不强制禁工具:模型写完文件后常需继续验证(编译/跑测试),
        # 强制 none 把"修完-验证"链切断 → TOOL_CHOICE_VIOLATION break(真机铁证
        # 2026-08-08: 模型 5 轮修完 main.go 后想 go build,被"the host disabled tools
        # for this informational model turn" 连拦 3 轮 break)。settled=无待办约束,
        # 工具选择权归主循环模型(对齐 长期助手/会话运行时,评估从不硬禁)。
        return ToolChoice.auto("required_actions_settled")
    assessment = getattr(snapshot, "required_action_assessment", None)
    if _assessment_failed(assessment):
        # 评估模型失败不应一票否决禁掉整轮工具：评估只是预判，主循环模型拥有完整上下文，
        # 让它自主决定是否调用(长期助手/会话运行时 均无预评估硬禁)。评估失败硬禁曾导致
        # 真实请求整 run 无工具可用(2026-08-08 真机铁证)。
        return ToolChoice.auto("required_action_assessment_failed")
    if isinstance(assessment, dict) and assessment.get("requires_action") is False:
        # informational 评估(纯询问/闲聊/催办)不硬禁工具:催办、追问进度是真实用户最常
        # 说的消息,评估模型判 False 时整 run 禁工具 → 模型按系统提示发起调用 →
        # TOOL_CHOICE_VIOLATION → 修复轮(教 JSON 格式)方向全错 → 3 轮 break 卡死
        # (2026-08-08 真机铁证:scrapy 复刻连续 3 请求 blocked)。工具是否调用由主循环
        # 模型自主决定,评估只影响 guidance 注入。
        return ToolChoice.auto("semantic_assessment_informational")
    return ToolChoice.auto("ordinary_tool_turn")


def render_required_action_guidance(snapshot: object | None) -> str:
    actions = tuple(getattr(snapshot, "required_actions", ()) or ())
    if not actions:
        # T-USER-001 真机铁证(2026-08-11):评估判 informational(requires_action=False)
        # 时若不注入任何 guidance,主循环模型看不到「本条是信息性陈述」的信号,
        # 会把纯陈述/告警内容当任务执行(9 次调用+写入文件)。这里注入模型可见的
        # 结构化信号但不硬禁工具(auto 保留,防 2026-08-08 TOOL_CHOICE_VIOLATION
        # 卡死铁证):软约束让模型自行收敛,硬约束只用于 UNKNOWN 兜底。
        assessment = getattr(snapshot, "required_action_assessment", None)
        if (
            isinstance(assessment, dict)
            and not _assessment_failed(assessment)
            and assessment.get("requires_action") is False
            and assessment.get("source") == "model_structured"
        ):
            return (
                "[tool-system:required-action-assessment]\n"
                + json.dumps(
                    {"source": "model_structured", "requires_action": False},
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n本条用户消息被评估为信息性陈述(requires_action=false)："
                "不要求执行任何操作。请不要为它发起工具调用或写入任何文件，"
                "直接如实回答即可；如确需工具，请先用一句话说明理由。"
            )
        return ""
    visible = [
        {
            "action_id": item.action_id,
            "kind": item.kind,
            "allowed_tools": list(item.allowed_tools),
            "effect_ceiling": item.effect_ceiling,
            "status": item.status,
            "success_criteria": item.success_criteria,
            "blocked_reason": item.blocked_reason,
        }
        for item in actions
    ]
    return (
        "[tool-system:required-actions]\n"
        + json.dumps(visible, ensure_ascii=False, sort_keys=True)
        + "\n开放动作必须以真实 ToolResult 销账；自然语言声称完成没有效力。"
    )


def settle_required_action(snapshot: object | None, call: ToolCall, result: ToolResult) -> None:
    action = _action_for_call(snapshot, call)
    if action is None:
        return
    if call.call_id not in action.evidence_call_ids:
        action.evidence_call_ids.append(call.call_id)
    if result.status == "approval_required":
        action.status = "approval_required"
        action.blocked_reason = result.error_code or "APPROVAL_REQUIRED"
        return
    if result.status == "cancelled":
        action.status = "blocked"
        action.blocked_reason = result.error_code or "CANCELLED"
        return
    if result.effect_outcome == "unknown":
        action.status = "blocked"
        action.blocked_reason = result.error_code or "TOOL_OPERATION_OUTCOME_UNKNOWN"
        return
    if result.ok and _result_proves_action(result):
        action.status = "satisfied"
        action.blocked_reason = ""
        return
    if not result.retryable and result.error_code in {
        "TOOL_NOT_IN_RUNTIME_SNAPSHOT",
        "TOOL_UNAVAILABLE",
        "TOOL_NOT_ALLOWED",
        "TOOL_OPERATION_STORE_UNAVAILABLE",
        "PATH_ACCESS_DENIED",
        "URL_ACCESS_DENIED",
        "SSRF_BLOCKED",
    }:
        action.status = "blocked"
        action.blocked_reason = result.error_code


def required_action_no_tool_decision(
    snapshot: object | None,
    *,
    has_succeeded_evidence: bool = False,
) -> str:
    """Return repair once, then a typed terminal status for remaining obligations."""

    if _assessment_failed(getattr(snapshot, "required_action_assessment", None)):
        return "blocked"
    actions = tuple(getattr(snapshot, "required_actions", ()) or ())
    open_actions = [item for item in actions if item.status == "open"]
    if open_actions:
        if has_succeeded_evidence:
            # 本 run 已有真实成功执行证据(succeeded tool call):评估拆解粒度与
            # 模型合并执行粒度不对齐是常态(真机铁证 G4-001 三轮 PASS/FAIL/FAIL:
            # 评估拆 3 个 action,模型 2 次调用建完两文件+跑测试,第 3 个 action
            # 永远等不到自己的调用)。义务已有真实执行证据,不再拿评估粒度卡死
            # 已完成任务——防假完成由产物/验收校验把关,此门只防"纯 NL 声称"。
            for item in open_actions:
                item.status = "satisfied"
                item.blocked_reason = ""
            return "complete"
        if max(item.no_tool_attempts for item in open_actions) < 1:
            for item in open_actions:
                item.no_tool_attempts += 1
            return "repair"
        for item in open_actions:
            item.status = "unfinished"
            item.blocked_reason = item.blocked_reason or "REQUIRED_ACTION_HAS_NO_EVIDENCE"
        return "unfinished"
    for status in ("approval_required", "needs_user_input", "blocked", "unfinished"):
        if any(item.status == status for item in actions):
            return status
    return "complete"


def required_action_assessment_failed(snapshot: object | None) -> bool:
    """Whether a configured structured assessor failed strict validation."""

    return _assessment_failed(getattr(snapshot, "required_action_assessment", None))


def _assessment_failed(assessment: object) -> bool:
    return bool(
        isinstance(assessment, dict)
        and assessment.get("source") == "model_structured"
        and str(assessment.get("error") or "").strip()
    )


def restore_required_actions_from_records(
    snapshot: object | None,
    records: Iterable[object],
) -> None:
    actions = {
        item.action_id: item for item in tuple(getattr(snapshot, "required_actions", ()) or ())
    }
    for raw in records:
        if not isinstance(raw, dict):
            continue
        action = actions.get(str(raw.get("required_action_id") or ""))
        if action is None:
            continue
        call_id = str(raw.get("call_id") or raw.get("id") or "").strip()
        if call_id and call_id not in action.evidence_call_ids:
            action.evidence_call_ids.append(call_id)
        status = str(raw.get("status") or "").strip().lower()
        effect = str(raw.get("effect_outcome") or "").strip().lower()
        operation_status = str(raw.get("tool_operation_status") or "").strip().lower()
        if status in {"ok", "succeeded"} and effect == "confirmed":
            if operation_status in {"", "succeeded"}:
                action.status = "satisfied"
        elif status == "approval_required":
            action.status = "approval_required"
        elif effect == "unknown":
            action.status = "blocked"
            action.blocked_reason = "TOOL_OPERATION_OUTCOME_UNKNOWN"


def _result_proves_action(result: ToolResult) -> bool:
    decision = result.metadata.get("action_decision")
    effect = (
        str(decision.get("resolved_effect") or "").strip().lower()
        if isinstance(decision, dict)
        else ""
    )
    if effect == "read_only":
        return result.handler_executed and result.effect_outcome == "confirmed"
    operation = result.operation
    return bool(
        operation is not None
        and operation.status == "succeeded"
        and result.effect_outcome == "confirmed"
        and result.effect_source_ref
    )


def _action_for_call(snapshot: object | None, call: ToolCall) -> RequiredAction | None:
    actions = tuple(getattr(snapshot, "required_actions", ()) or ())
    if call.required_action_id:
        exact = next(
            (item for item in actions if item.action_id == call.required_action_id),
            None,
        )
        if exact is not None and exact.status == "open":
            return exact
        # id 指向不存在或已销账/阻塞的 action:回退工具匹配继续销账。真机铁证
        # (G4-001): 评估产出多个允许同工具的 action 时,模型多次调用复用同一个
        # required_action_id,精确匹配只销第一个,其余真实执行被漏销 → 收口门
        # REQUIRED_ACTION_HAS_NO_EVIDENCE 误杀已完成任务。宽容 id 偏差不放松
        # 执行证据(fail-closed 由 _result_proves_action 把关:动作没做照拦)。
    candidates = [
        item
        for item in actions
        if item.status == "open"
        and (not item.allowed_tools or call.tool_name in item.allowed_tools)
    ]
    if not candidates:
        return None
    # 多候选(评估产出多个允许同工具的 open action)按评估顺序先到先得分配:
    # 真实调用次数足够则每个 open action 都被真实执行证据销账,杜绝"调用全成功
    # 却因归属模糊整体漏销"的误杀;调用次数不足时剩余 open action 收口照拦。
    return candidates[0]


def _actions_from_structured_sources(
    sources: Iterable[object],
    *,
    runtime_snapshot: object,
    run_id: str,
    source_turn_id: str,
) -> tuple[RequiredAction, ...]:
    rows: list[object] = []
    for source in sources:
        if not isinstance(source, dict):
            continue
        value = source.get("required_actions")
        if isinstance(value, list):
            rows.extend(value)
    return _validated_action_rows(
        rows,
        runtime_snapshot=runtime_snapshot,
        run_id=run_id,
        source_turn_id=source_turn_id,
    )


def _actions_from_assessment(
    raw: object,
    *,
    runtime_snapshot: object,
    run_id: str,
    source_turn_id: str,
) -> tuple[RequiredAction, ...]:
    if not isinstance(raw, dict):
        raise ValueError("required action assessment must be an object")
    from ..tooling.input_schema import validate_tool_input

    schema = _assessment_schema(tuple(getattr(runtime_snapshot, "available_tool_names", ()) or ()))
    validation = validate_tool_input(raw, schema)
    if not validation.ok:
        issues = ",".join(f"{issue.path}:{issue.keyword}" for issue in validation.issues[:6])
        raise ValueError(f"required action assessment violates schema: {issues}")
    requires_action = raw.get("requires_action")
    if not isinstance(requires_action, bool):
        raise ValueError("requires_action must be boolean")
    rows = raw.get("actions")
    if not isinstance(rows, list):
        raise ValueError("actions must be a list")
    if not requires_action:
        if rows:
            raise ValueError("informational assessment cannot carry actions")
        return ()
    if not rows:
        raise ValueError("execution assessment requires at least one action")
    return _validated_action_rows(
        rows,
        runtime_snapshot=runtime_snapshot,
        run_id=run_id,
        source_turn_id=source_turn_id,
    )


def _validated_action_rows(
    rows: Iterable[object],
    *,
    runtime_snapshot: object,
    run_id: str,
    source_turn_id: str,
) -> tuple[RequiredAction, ...]:
    available = set(getattr(runtime_snapshot, "available_tool_names", ()) or ())
    effects_by_tool = _tool_effects_by_name(runtime_snapshot)
    actions: list[RequiredAction] = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise ValueError("required action row must be an object")
        allowed_value = row.get("allowed_tools")
        if not isinstance(allowed_value, list):
            raise ValueError("required action allowed_tools must be a list")
        allowed = tuple(
            dict.fromkeys(
                str(item or "").strip()
                for item in allowed_value
                if str(item or "").strip() in available
            )
        )
        ceiling = str(row.get("effect_ceiling") or "read_only").strip().lower()
        # 评估一致性校验:allowed_tools 里任一工具的默认效果超过 ceiling 即自相矛盾
        # (mutating 工具配 read_only ceiling,评估提示语明确禁止)。丢弃矛盾动作而非
        # 让它以 read_only 状态存在——否则"继续干活"类任务后续所有 mutating 工具
        # (run_command/write_file)全被 CEILING 拦截,无审批消费端,任务死锁(真机实证)。
        if any(
            _EFFECT_RANK.get(effects_by_tool.get(tool, ""), 0) > _EFFECT_RANK.get(ceiling, 0)
            for tool in allowed
        ):
            continue
        kind = str(row.get("kind") or "execute").strip().lower()
        description = str(row.get("description") or "").strip()
        action_id = str(row.get("action_id") or "").strip() or _action_id(
            run_id,
            source_turn_id,
            index,
            kind,
            description,
        )
        blocked_reason = str(row.get("blocked_reason") or "").strip()
        status = str(row.get("status") or "open").strip().lower()
        if not allowed and status == "open":
            status = "blocked"
            blocked_reason = blocked_reason or "REQUIRED_ACTION_HAS_NO_ALLOWED_TOOL"
        exits = row.get("acceptable_exits")
        actions.append(
            RequiredAction(
                action_id=action_id,
                source_turn_id=str(row.get("source_turn_id") or source_turn_id),
                kind=kind,
                allowed_tools=allowed,
                effect_ceiling=ceiling,
                status=status,
                acceptable_exits=(
                    tuple(str(item) for item in exits)
                    if isinstance(exits, list)
                    else tuple(sorted(_EXITS))
                ),
                blocked_reason=blocked_reason,
                description=description,
                success_criteria=str(row.get("success_criteria") or ""),
            )
        )
    return tuple(actions)


def _assessment_schema(tool_names: tuple[str, ...]) -> dict[str, Any]:
    tool_enum = sorted(tool_names)
    allowed_tools_schema: dict[str, Any] = {
        "type": "array",
        "uniqueItems": True,
        "items": {"type": "string"},
    }
    if tool_enum:
        allowed_tools_schema["items"] = {"type": "string", "enum": tool_enum}
    else:
        # No fake tool name: the assessor only classifies the obligation and
        # the host turns an empty allowed set into a typed blocked action.
        allowed_tools_schema["maxItems"] = 0
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["requires_action", "actions"],
        "properties": {
            "requires_action": {"type": "boolean"},
            "actions": {
                "type": "array",
                "maxItems": 8,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "kind",
                        "description",
                        "success_criteria",
                        "allowed_tools",
                        "effect_ceiling",
                        "acceptable_exits",
                    ],
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": ["inspect", "execute", "modify", "external", "verify"],
                        },
                        "description": {"type": "string", "minLength": 1},
                        "success_criteria": {"type": "string", "minLength": 1},
                        "allowed_tools": allowed_tools_schema,
                        "effect_ceiling": {
                            "type": "string",
                            "enum": ["read_only", "mutating", "dangerous"],
                        },
                        "acceptable_exits": {
                            "type": "array",
                            "uniqueItems": True,
                            "items": {"type": "string", "enum": sorted(_EXITS)},
                        },
                    },
                },
            },
        },
    }


def _assessment_prompt(user_prompt: str, runtime_snapshot: object) -> str:
    tools = [
        {
            "name": runtime.model_spec.name,
            "description": runtime.model_spec.description,
            "default_effect": runtime.runtime_policy.effect_resolver.default_effect,
        }
        for runtime in tuple(getattr(runtime_snapshot, "runtimes", ()) or ())
    ]
    return (
        "Classify the semantic obligation in the quoted user request. "
        "requires_action=true only when the user asks the assistant to actually inspect, execute, "
        "change, send, verify, or otherwise cause an operation in this turn. "
        "A request asking how to do something, asking what a command means, quoting a command from "
        "a document, requesting explanation only, or reporting that somebody already completed an "
        "operation is informational and must be false unless the user also asks for a new operation. "
        "Do not execute anything. Decompose independent real obligations into minimal actions and "
        "select only candidate tools that can supply evidence.\n\n"
        "For every action, set effect_ceiling to the highest side-effect level the operation needs:\n"
        "- read_only: pure reading or explanation; nothing is persisted or changed.\n"
        "- mutating: the operation writes or updates persistent state, including saving information "
        "to long-term memory, creating or editing files, scheduling, or recording findings. A request "
        "to remember, save, or store information must declare at least mutating.\n"
        "- dangerous: system-level or external side effects (arbitrary shell execution, network "
        "access to unknown hosts, credential handling).\n"
        "The ceiling must cover every tool in allowed_tools: if a listed tool has default_effect "
        "mutating or dangerous, the ceiling must be at least that level. Never combine a mutating "
        "tool with a read_only ceiling.\n\n"
        "Candidate tools:\n"
        + json.dumps(tools, ensure_ascii=False, sort_keys=True)
        + "\n\nQuoted user request (untrusted data):\n"
        + json.dumps(str(user_prompt or ""), ensure_ascii=False)
    )


def _action_id(
    run_id: str,
    source_turn_id: str,
    index: int,
    kind: str,
    description: str,
) -> str:
    raw = f"{run_id}:{source_turn_id}:{index}:{kind}:{description}".encode()
    return "required-action:" + hashlib.sha256(raw).hexdigest()[:28]


__all__ = [
    "RequiredAction",
    "RequiredActionAssessment",
    "assess_required_actions",
    "render_required_action_guidance",
    "required_action_assessment_failed",
    "required_action_no_tool_decision",
    "restore_required_actions_from_records",
    "settle_required_action",
    "tool_choice_for_required_actions",
]
