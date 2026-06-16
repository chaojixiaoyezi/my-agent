# Collaboration tool values
from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

from ..common.value_parsing import (
    dict_value,
    dict_values,
    float_value,
    non_negative_int,
    string_list,
)
from ..tooling.models import ToolExecutionResult

if TYPE_CHECKING:
    from ..core import SimpleAgent


def ok(tool: str, payload: dict[str, Any]) -> ToolExecutionResult:
    return ToolExecutionResult(tool, True, json.dumps({"ok": True, **payload}, ensure_ascii=False, indent=2))


def error(tool: str, code: str, message: str, details: dict[str, Any] | None = None) -> ToolExecutionResult:
    payload = {"ok": False, "error": code, "message": message}
    if details:
        payload.update(details)
    return ToolExecutionResult(tool, False, json.dumps(payload, ensure_ascii=False, indent=2), error_code=code)


def string_values(value: object) -> list[str]:
    return string_list(value)


def deadline_at(agent: SimpleAgent, params: dict[str, object]) -> float:
    absolute = float_value(params.get("deadline_at"))
    if absolute > 0:
        return absolute
    seconds = float_value(params.get("deadline_seconds")) or default_deadline_seconds(agent)
    return time.time() + seconds if seconds > 0 else 0.0


def default_deadline_seconds(agent: SimpleAgent) -> float:
    try:
        config = getattr(agent, "config", None)
        return max(0.0, float(getattr(config, "collaboration_default_deadline_seconds", 120)))
    except (TypeError, ValueError):
        return 120.0


def limit_param(value: object, *, default: int) -> int:
    return non_negative_int(value, default=default)

# Collaboration tool specs
from ..tooling.models import ToolSpec

_CASE_PARAMETERS = {
    "thread_id": "会话线程 ID；不知道时可传 task_id 让系统反查",
    "task_id": "任务 ID；thread_id 为空时用于反查会话",
    "title": "case 标题，描述这次需要协作处理的事",
    "summary": "结构化事实摘要，不要求业务专项格式",
    "priority": "优先级，urgent 会让 coordinator 更快升级；保留原始值，不做封闭枚举硬拒",
    "created_by": "创建 case 的代理 ID",
    "required_capabilities": "本 case 需要哪些能力，如 query/analyze/notify/act",
    "entities": "相关实体字典；开放世界，不写死字段，建议只放轻量索引和说明",
}
_REQUEST_PARAMETERS = {
    "case_id": "协作 case ID",
    "requester_agent_id": "发起协作请求的代理 ID",
    "target_agent_ids": "明确目标代理列表；省略时按 required_capabilities 匹配",
    "required_capabilities": "希望响应者具备的能力",
    "question": "希望对方补充什么事实或证据",
    "entities": "本次请求携带的轻量实体字典；能用 observed_facts/query_hints 表达时优先用后者",
    "problem_statement": "这次协作要判断的问题，用普通语言描述，不绑定业务类型",
    "observed_facts": "已观察到的线索事实列表；每项可含 fact_id/label/kind/value/source_refs/queryable 等开放世界字段",
    "query_intent": "希望响应者完成的查询意图，例如收集佐证、排除可能性、扩大范围；开放世界对象",
    "query_hints": "LLM 给响应者的软查询提示列表；响应者可以完整查、拆分查、改写查或换来源",
    "routing_requirements": "路由要求对象，例如需要的来源范围、角色、时效或负载偏好；开放世界对象",
    "response_contract": "可选响应建议；不知道怎么写就省略，响应者仍可按任务自然回证据",
    "context_refs": "可选上下文引用列表，指向触发事件、工具结果、产物或外部证据",
    "deadline_at": "可选截止时间戳；到点后 coordinator 可关闭收集窗口并带部分结果通知上级；省略时使用配置默认 deadline",
    "deadline_seconds": "可选相对等待秒数；例如 30 表示从当前调用起 30 秒后仍未响应也继续推进；省略时使用配置默认 deadline，配置 0 才不自动补",
    "priority": "请求优先级；省略时继承 case priority",
}
_RAISE_EVENT_PARAMETERS = {
    "thread_id": "会话线程 ID；不知道时可传 task_id 或留空由当前 runner 反查",
    "task_id": "任务 ID；thread_id 为空时用于反查会话",
    "title": "协作事件标题，描述这次为什么需要多人参与",
    "summary": "已知事实摘要，不要求业务专项格式",
    "priority": "优先级；urgent 可让后台主代理更快处理，开放世界字符串",
    "created_by": "发现事件的代理 ID；省略时工具会尽量用当前 runner 身份",
    "requester_agent_id": "发起协作请求的代理 ID；省略时继承 created_by",
    "target_agent_ids": "明确目标代理列表；省略时按 required_capabilities 匹配",
    "required_capabilities": "希望响应者具备的能力，如 query/analyze/notify/act",
    "entities": "相关实体字典；开放世界，只放轻量索引和说明",
    "question": "希望其他代理补充什么事实或证据",
    "problem_statement": "这次协作要判断的问题，用普通语言描述，不绑定业务类型",
    "observed_facts": "已观察到的线索事实列表；每项可含 fact_id/label/kind/value/source_refs/queryable 等开放世界字段",
    "query_intent": "希望响应者完成的查询意图，例如收集佐证、排除可能性、扩大范围；开放世界对象",
    "query_hints": "LLM 给响应者的软查询提示列表；响应者可以完整查、拆分查、改写查或换来源",
    "routing_requirements": "路由要求对象，例如需要的来源范围、角色、时效或负载偏好；开放世界对象",
    "response_contract": "可选响应建议；不知道怎么写就省略，响应者仍可按任务自然回证据",
    "context_refs": "可选上下文引用列表，指向触发事件、工具结果、产物或外部证据",
    "deadline_at": "可选截止时间戳；到点后 coordinator 可关闭收集窗口并带部分结果通知上级；省略时使用配置默认 deadline",
    "deadline_seconds": "可选相对等待秒数；例如 30 表示从当前调用起 30 秒后仍未响应也继续推进；省略时使用配置默认 deadline，配置 0 才不自动补",
    "metadata": "可选结构化补充信息；开放世界，不写死字段",
}
_LIST_REQUESTS_PARAMETERS = {
    "agent_id": "可选代理 ID；省略时工具会优先使用当前 runner 的 run_id",
    "agent_name": "可选代理名；用于匹配模型可见名字",
    "agent_role": "可选代理角色；用于匹配按角色点名的协作请求",
    "limit": "最多返回多少条；0 表示不限制，默认 10",
}
_EVIDENCE_PARAMETERS = {
    "case_id": "协作 case ID",
    "request_id": "对应的协作请求 ID，可为空",
    "source_agent_id": "提交证据的代理 ID",
    "matched": "是否找到匹配证据",
    "summary": "证据摘要",
    "evidence_refs": "证据引用列表，指向工具结果、产物、数据库快照或外部证据 ID",
    "queried_scopes": "响应者实际查询过的范围；开放世界字符串列表",
    "used_query_hints": "使用过的 query_hints/hint_id 列表；没有使用也可为空",
    "miss_reason": "未命中原因；matched=false 时建议说明，不作为封闭枚举",
    "response_facts": "响应者产生的开放世界事实列表，可记录命中、未命中、派生观察或限制",
    "followup_suggestions": "后续协作建议列表，例如建议其他能力/来源继续查；开放世界对象",
    "query_actions": "实际查询动作摘要列表，轻量记录查了什么，不塞大结果",
    "confidence": "可信度数值，0-1；只记录，不作为通用硬门",
    "limitations": "证据限制说明列表",
}
_UPDATE_STATUS_PARAMETERS = {
    "case_id": "协作 case ID",
    "status": "新状态；机器只识别 open/closed 作为收集窗口状态；其他值只记录展示，不驱动关闭判断；closed 必须带摘要",
    "actor_agent_id": "执行状态推进的代理 ID",
    "summary": "状态推进摘要；closed 必须提供摘要、决策或既有决策",
    "decision_type": "可选决策类型，如 triaged_by_main_agent/closed_by_main_agent",
    "evidence_ids": "可选关联证据包 ID 列表",
}
_UPDATE_REQUEST_PARAMETERS = {
    "case_id": "协作 case ID",
    "request_id": "协作请求 ID",
    "status": "请求状态；机器只识别 completed/blocked/timeout/declined；其他值都按等待中展示，不驱动完成或阻塞判断",
    "actor_agent_id": "更新请求状态的代理 ID",
    "summary": "状态更新摘要，说明已完成、阻塞原因或下一步需要什么",
    "target_agent_ids": "可选；需要换路时写新的目标代理列表，系统会把它落到请求目标上",
    "metadata": "可选结构化补充信息；开放世界，不写死字段",
}
_REROUTE_REQUEST_PARAMETERS = {
    "case_id": "协作 case ID",
    "request_id": "需要换路的协作请求 ID",
    "actor_agent_id": "执行换路判断的代理 ID，通常是 main 或 coordinator",
    "target_agent_ids": "新的目标代理列表；必须是结构化列表，不能只写在自然语言摘要里",
    "status": "换路后的请求状态；默认 pending，表示等待新目标继续处理",
    "summary": "换路原因和下一步，例如原目标不可用、换到其他来源继续查",
    "metadata": "可选结构化补充信息；开放世界，不写死字段",
}

# 精确 JSON Schema 片段（native tool_use 弱推导消歧）；按各 *_PARAMETERS 同样的合并方式拼装。
# 只声明类型明确、execute 会按该类型解析的参数（列表/布尔/对象/数值）；开放世界标量留 string 回退。
_CASE_PARAMETER_SCHEMA = {
    "thread_id": {"type": "string"},
    "task_id": {"type": "string"},
    "title": {"type": "string"},
    "summary": {"type": "string"},
    "priority": {"type": "string"},
    "created_by": {"type": "string"},
    "required_capabilities": {"type": "array", "items": {"type": "string"}},
    "entities": {"type": "object"},
}
_REQUEST_PARAMETER_SCHEMA = {
    "case_id": {"type": "string"},
    "requester_agent_id": {"type": "string"},
    "target_agent_ids": {"type": "array", "items": {"type": "string"}},
    "required_capabilities": {"type": "array", "items": {"type": "string"}},
    "question": {"type": "string"},
    "entities": {"type": "object"},
    "problem_statement": {"type": "string"},
    "observed_facts": {"type": "array", "items": {"type": "object"}},
    "query_intent": {"type": "object"},
    "query_hints": {"type": "array", "items": {"type": "string"}},
    "routing_requirements": {"type": "object"},
    "response_contract": {"type": "object"},
    "context_refs": {"type": "array", "items": {"type": "string"}},
    "deadline_at": {"type": "number"},
    "deadline_seconds": {"type": "number"},
    "priority": {"type": "string"},
}
_RAISE_EVENT_PARAMETER_SCHEMA = {
    "thread_id": {"type": "string"},
    "task_id": {"type": "string"},
    "title": {"type": "string"},
    "summary": {"type": "string"},
    "priority": {"type": "string"},
    "created_by": {"type": "string"},
    "requester_agent_id": {"type": "string"},
    "target_agent_ids": {"type": "array", "items": {"type": "string"}},
    "required_capabilities": {"type": "array", "items": {"type": "string"}},
    "entities": {"type": "object"},
    "question": {"type": "string"},
    "problem_statement": {"type": "string"},
    "observed_facts": {"type": "array", "items": {"type": "object"}},
    "query_intent": {"type": "object"},
    "query_hints": {"type": "array", "items": {"type": "string"}},
    "routing_requirements": {"type": "object"},
    "response_contract": {"type": "object"},
    "context_refs": {"type": "array", "items": {"type": "string"}},
    "deadline_at": {"type": "number"},
    "deadline_seconds": {"type": "number"},
    "metadata": {"type": "object"},
}
_LIST_REQUESTS_PARAMETER_SCHEMA = {
    "agent_id": {"type": "string"},
    "agent_name": {"type": "string"},
    "agent_role": {"type": "string"},
    "limit": {"type": "integer", "minimum": 0},
}
_EVIDENCE_PARAMETER_SCHEMA = {
    "case_id": {"type": "string"},
    "request_id": {"type": "string"},
    "source_agent_id": {"type": "string"},
    "matched": {"type": "boolean"},
    "summary": {"type": "string"},
    "evidence_refs": {"type": "array", "items": {"type": "string"}},
    "queried_scopes": {"type": "array", "items": {"type": "string"}},
    "used_query_hints": {"type": "array", "items": {"type": "string"}},
    "miss_reason": {"type": "string"},
    "response_facts": {"type": "array", "items": {"type": "object"}},
    "followup_suggestions": {"type": "array", "items": {"type": "object"}},
    "query_actions": {"type": "array", "items": {"type": "object"}},
    "confidence": {"type": "number"},
    "limitations": {"type": "array", "items": {"type": "string"}},
}
_UPDATE_STATUS_PARAMETER_SCHEMA = {
    "case_id": {"type": "string"},
    "status": {"type": "string"},
    "actor_agent_id": {"type": "string"},
    "summary": {"type": "string"},
    "decision_type": {"type": "string"},
    "evidence_ids": {"type": "array", "items": {"type": "string"}},
}
_UPDATE_REQUEST_PARAMETER_SCHEMA = {
    "case_id": {"type": "string"},
    "request_id": {"type": "string"},
    "status": {"type": "string"},
    "actor_agent_id": {"type": "string"},
    "summary": {"type": "string"},
    "target_agent_ids": {"type": "array", "items": {"type": "string"}},
    "metadata": {"type": "object"},
}


def build_raise_collaboration_spec() -> ToolSpec:
    return ToolSpec(
        name="raise_collaboration",
        category="orchestration",
        effect="mutating",
        requires_idempotency=True,
        description=(
            "发起通用协作：没有 case_id 时打开新 case；有 question/target/capability 时同时发请求；"
            "已有 case_id 时在该 case 里继续发请求。"
        ),
        use_cases=[],
        avoid_when=[],
        keywords=["协作", "case", "补证据", "联合判断", "collaboration", "coordination"],
        parameters={**_CASE_PARAMETERS, **_REQUEST_PARAMETERS, **_RAISE_EVENT_PARAMETERS},
        parameter_schema={**_CASE_PARAMETER_SCHEMA, **_REQUEST_PARAMETER_SCHEMA, **_RAISE_EVENT_PARAMETER_SCHEMA},
        examples=[],
    )


def build_inspect_collaboration_spec() -> ToolSpec:
    return ToolSpec(
        name="inspect_collaboration",
        category="orchestration",
        effect="read_only",
        description="只读查看协作：传 case_id 看 case；不传 case_id 时列出当前或指定代理的待处理协作请求。",
        use_cases=[],
        avoid_when=["不要用它查看普通 agent/subagent run 的执行状态；run 状态请用 inspect_agent_tree。"],
        keywords=["协作状态", "协作待办", "pending collaboration", "case status", "request discovery"],
        parameters={"case_id": "可选协作 case ID；有则查看 case 状态", **_LIST_REQUESTS_PARAMETERS},
        parameter_schema={"case_id": {"type": "string"}, **_LIST_REQUESTS_PARAMETER_SCHEMA},
        examples=[],
    )


def build_submit_collaboration_result_spec() -> ToolSpec:
    return ToolSpec(
        name="submit_collaboration_result",
        category="orchestration",
        effect="mutating",
        requires_idempotency=True,
        description="向 case 提交协作结果；只交 refs、查询范围、命中/未命中摘要和限制，不把大正文塞进协作账本。",
        use_cases=[],
        avoid_when=[],
        keywords=["证据", "evidence", "refs", "协作响应", "result"],
        parameters=_EVIDENCE_PARAMETERS,
        parameter_schema=_EVIDENCE_PARAMETER_SCHEMA,
        examples=[],
    )


def build_update_collaboration_spec() -> ToolSpec:
    return ToolSpec(
        name="update_collaboration",
        category="orchestration",
        effect="mutating",
        requires_idempotency=True,
        description="更新协作 case 或 request；有 request_id 时更新请求，无 request_id 时更新 case；带 target_agent_ids 可改派请求。",
        use_cases=[],
        avoid_when=[],
        keywords=["case update", "request update", "reroute", "换路", "关闭协作", "状态推进"],
        parameters={**_UPDATE_STATUS_PARAMETERS, **_UPDATE_REQUEST_PARAMETERS},
        parameter_schema={**_UPDATE_STATUS_PARAMETER_SCHEMA, **_UPDATE_REQUEST_PARAMETER_SCHEMA},
        examples=[],
    )


__all__ = [
    "build_inspect_collaboration_spec",
    "build_raise_collaboration_spec",
    "build_submit_collaboration_result_spec",
    "build_update_collaboration_spec",
]

# Collaboration target resolution
from typing import TYPE_CHECKING

from ..agent_core.orchestration.scope_resolution import (
    ScopeResolution,
    identity_scope_resolution,
    scope_resolution_payload,
)
from ..runtime_errors import runtime_error_report
from ..subagents.models import (
    SUBAGENT_DEAD_STATUSES,
    SUBAGENT_HANDLED_TERMINAL_STATUSES,
    task_status_in,
    task_status_reason_code,
)

if TYPE_CHECKING:
    from ..core import SimpleAgent

_UNAVAILABLE_TARGET_STATUSES = SUBAGENT_DEAD_STATUSES | SUBAGENT_HANDLED_TERMINAL_STATUSES


def resolved_target_agent_ids(agent: SimpleAgent, params: dict[str, object]) -> list[str]:
    targets, _errors = resolved_target_agent_ids_report(agent, params)
    return targets


def resolved_target_agent_ids_report(agent: SimpleAgent, params: dict[str, object]) -> tuple[list[str], list[dict[str, object]]]:
    raw = string_values(params.get("target_agent_ids"))
    routing = dict_value(params.get("routing_requirements"))
    raw.extend(string_values(routing.get("target_agent_ids")))
    return _resolve_raw_targets_report(agent, raw)


def target_runtime_summary(
    agent: SimpleAgent,
    target_agent_ids: list[str],
    *,
    resolution_errors: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    task_rows, load_error = subagent_tasks_report(agent)
    tasks = {str(getattr(task, "id", "") or ""): task for task in task_rows}
    rows = [_runtime_row(target, tasks.get(str(target or "").strip())) for target in target_agent_ids]
    payload: dict[str, object] = {
        "available_target_run_ids": [row["agent_id"] for row in rows if not row["unavailable_reason"]],
        "unavailable_targets": [{"agent_id": row["agent_id"], "reason": row["unavailable_reason"]} for row in rows if row["unavailable_reason"]],
        "target_statuses": [_status_payload(row) for row in rows],
    }
    if load_error:
        payload["target_runtime_load_error"] = load_error
    if resolution_errors:
        payload["target_resolution_errors"] = list(resolution_errors)
    return payload


def target_runtime_metadata(summary: dict[str, object]) -> dict[str, object]:
    result: dict[str, object] = {}
    if summary.get("unavailable_targets"):
        result["unavailable_targets"] = summary["unavailable_targets"]
    if summary.get("target_statuses"):
        result["target_statuses"] = summary["target_statuses"]
    if summary.get("target_runtime_load_error"):
        result["target_runtime_load_error"] = summary["target_runtime_load_error"]
    if summary.get("target_resolution_errors"):
        result["target_resolution_errors"] = summary["target_resolution_errors"]
    return result


def target_response_payload(summary: dict[str, object]) -> dict[str, object]:
    available = [str(item) for item in summary.get("available_target_run_ids", []) or [] if str(item or "").strip()]
    payload = {
        "available_target_run_ids": available,
        "unavailable_targets": list(summary.get("unavailable_targets", []) or []),
        "target_statuses": list(summary.get("target_statuses", []) or []),
    }
    if summary.get("target_runtime_load_error"):
        payload["target_runtime_load_error"] = summary["target_runtime_load_error"]
    if summary.get("target_resolution_errors"):
        payload["target_resolution_errors"] = summary["target_resolution_errors"]
    if summary.get("capability_roster_load_error"):
        payload["capability_roster_load_error"] = summary["capability_roster_load_error"]
    if available:
        payload["suggested_dispatch_tool_call"] = _dispatch_suggestion(available)
    return payload


def request_identity(agent: SimpleAgent, params: dict[str, object]) -> dict[str, str]:
    identity, _error = request_identity_report(agent, params)
    return identity


def request_identity_report(agent: SimpleAgent, params: dict[str, object]) -> tuple[dict[str, str], dict[str, object] | None]:
    resolution = collaboration_identity_resolution(
        agent,
        params,
        explicit_keys=("agent_id", "run_id"),
    )
    agent_id = str(resolution.effective.get("agent_id") or "").strip()
    agent_name = str(params.get("agent_name") or "").strip()
    agent_role = str(params.get("agent_role") or "").strip()
    load_error = None
    if agent_id and not (agent_name and agent_role) and _is_known_subagent_run(agent, agent_id):
        agent_name, agent_role, load_error = _loaded_task_identity_report(
            agent,
            agent_id,
            agent_name,
            agent_role,
        )
    current = getattr(agent, "_current_run_params", None)
    agent_id = agent_id or str(getattr(current, "task_id", "") or "").strip()
    return {"agent_id": agent_id, "agent_name": agent_name, "agent_role": agent_role}, load_error


def actor_agent_id(agent: SimpleAgent, params: dict[str, object]) -> str:
    resolution = collaboration_identity_resolution(
        agent,
        params,
        explicit_keys=("created_by", "actor_agent_id", "requester_agent_id", "source_agent_id", "agent_id", "run_id"),
    )
    return str(resolution.effective.get("agent_id") or request_identity(agent, params)["agent_id"]).strip()


def collaboration_identity_resolution(
    agent: SimpleAgent,
    params: dict[str, object],
    *,
    explicit_keys: tuple[str, ...],
) -> ScopeResolution:
    return identity_scope_resolution(agent, params, explicit_keys=explicit_keys)


def collaboration_scope_payload(
    agent: SimpleAgent,
    params: dict[str, object],
    *,
    explicit_keys: tuple[str, ...],
) -> dict[str, object]:
    return scope_resolution_payload(
        collaboration_identity_resolution(agent, params, explicit_keys=explicit_keys)
    )


def subagent_tasks(agent: SimpleAgent) -> list[object]:
    rows, _load_error = subagent_tasks_report(agent)
    return rows


def subagent_tasks_report(agent: SimpleAgent) -> tuple[list[object], dict[str, object] | None]:
    manager = getattr(agent, "subagents", None)
    if manager is None or not hasattr(manager, "list_runs"):
        return [], None
    try:
        return list(manager.list_runs()), None
    except Exception as exc:
        return [], runtime_error_report(exc, context="raise_collaboration.target_runtime")


def _is_known_subagent_run(agent: SimpleAgent, run_id: str) -> bool:
    rows, _load_error = subagent_tasks_report(agent)
    return any(str(getattr(task, "id", "") or "") == run_id for task in rows)


def _resolve_raw_targets_report(agent: SimpleAgent, raw: list[str]) -> tuple[list[str], list[dict[str, object]]]:
    resolved: list[str] = []
    errors: list[dict[str, object]] = []
    for target in raw:
        matches, match_errors = _matching_subagent_run_ids_report(agent, target)
        errors.extend(match_errors)
        _append_resolved_targets(resolved, matches or [target])
    return resolved, errors


def _append_resolved_targets(resolved: list[str], items: list[str]) -> None:
    for item in items:
        if item not in resolved:
            resolved.append(item)


def _matching_subagent_run_ids(agent: SimpleAgent, target: str) -> list[str]:
    matches, _errors = _matching_subagent_run_ids_report(agent, target)
    return matches


def _matching_subagent_run_ids_report(agent: SimpleAgent, target: str) -> tuple[list[str], list[dict[str, object]]]:
    text = str(target or "").strip()
    if not text:
        return [], []
    identity_keys, identity_error = _target_identity_keys_report(agent, text)
    tasks, list_error = subagent_tasks_report(agent)
    errors = [item for item in (identity_error, list_error) if item]
    return [run_id for task in tasks if (run_id := _matching_task_run_id(task, identity_keys))], errors


def _target_identity_keys(agent: SimpleAgent, text: str) -> set[str]:
    identity_keys, _error = _target_identity_keys_report(agent, text)
    return identity_keys


def _target_identity_keys_report(agent: SimpleAgent, text: str) -> tuple[set[str], dict[str, object] | None]:
    try:
        return agent.collaboration_store.agent_identity_keys(text), None
    except Exception as exc:
        return {text}, runtime_error_report(
            exc,
            context="raise_collaboration.agent_identity_keys",
        )


def _matching_task_run_id(task: object, identity_keys: set[str]) -> str:
    run_id = str(getattr(task, "id", "") or "").strip()
    return run_id if run_id and run_id in identity_keys else ""


def _runtime_row(target: str, task: object | None) -> dict[str, str]:
    run_id = str(target or "").strip()
    status = str(getattr(task, "status", "") or "").strip() if task is not None else "UNKNOWN"
    channel = str(getattr(task, "channel_status", "") or "").strip() if task is not None else ""
    return {"agent_id": run_id, "agent_name": str(getattr(task, "agent_name", "") or ""), "status": status, "channel_status": channel, "unavailable_reason": _unavailable_reason(status, channel)}


def _unavailable_reason(status: str, channel_status: str) -> str:
    if status == "UNKNOWN":
        return "unknown_target"
    if channel_status == "BROKEN":
        return "channel_broken"
    return task_status_reason_code(status) if task_status_in(status, _UNAVAILABLE_TARGET_STATUSES) else ""


def _status_payload(row: dict[str, str]) -> dict[str, str]:
    return {key: row[key] for key in ("agent_id", "agent_name", "status", "channel_status")}


def _dispatch_suggestion(available: list[str]) -> dict[str, object]:
    return {"tool": "dispatch_subagents", "dry_run": False, "run_ids": available, "max_runners": len(available), "runner_instruction": "处理点名给自己的 collaboration request；查完后用 submit_collaboration_result 提交命中、未命中、证据引用和限制，然后返回自己的原任务。"}


def _loaded_task_identity(agent: SimpleAgent, agent_id: str, agent_name: str, agent_role: str) -> tuple[str, str]:
    loaded_name, loaded_role, _error = _loaded_task_identity_report(
        agent,
        agent_id,
        agent_name,
        agent_role,
    )
    return loaded_name, loaded_role


def _loaded_task_identity_report(
    agent: SimpleAgent,
    agent_id: str,
    agent_name: str,
    agent_role: str,
) -> tuple[str, str, dict[str, object] | None]:
    try:
        task = agent.subagents.load(agent_id)
    except Exception as exc:
        return agent_name, agent_role, runtime_error_report(
            exc,
            context="collaboration.identity.subagents.load",
        )
    return (
        agent_name or str(getattr(task, "agent_name", "") or ""),
        agent_role or str(getattr(task, "role", "") or ""),
        None,
    )

# Collaboration thread helpers
import logging
from typing import TYPE_CHECKING

from ..agent_core.runner.context import current_subagent_run_id
from ..runtime_errors import runtime_error_report
from ..tooling.models import ToolExecutionResult

if TYPE_CHECKING:
    from ..core import SimpleAgent

_LOGGER = logging.getLogger(__name__)


def resolve_thread(agent: SimpleAgent, params: dict[str, object], *, tool_name: str = "raise_collaboration") -> tuple[str, str] | ToolExecutionResult:
    thread_id = str(params.get("thread_id") or "").strip()
    task_id = str(params.get("task_id") or "").strip()
    if thread_id:
        return _explicit_thread(agent, thread_id, task_id, tool_name=tool_name)
    if task_id:
        resolved = thread_from_task(agent, task_id, materialize=True, tool_name=tool_name)
        if isinstance(resolved, ToolExecutionResult):
            return _current_runner_or_error(agent, resolved)
        if resolved:
            return resolved
    if resolved := thread_from_current_runner(agent):
        return resolved
    return error(tool_name, "thread_required", "thread_id is required unless task_id is bound to a thread")


def thread_from_task(
    agent: SimpleAgent,
    task_id: str,
    *,
    materialize: bool = False,
    tool_name: str = "raise_collaboration",
) -> tuple[str, str] | ToolExecutionResult | None:
    if not task_id:
        return None
    try:
        thread = agent.conversation_store.thread_for_task(task_id)
    except Exception as exc:
        return error(
            tool_name,
            "task_thread_lookup_failed",
            f"conversation task binding lookup failed: {task_id}",
            _load_error(exc, "raise_collaboration.thread_for_task"),
        )
    if thread is not None:
        return thread.thread_id, task_id
    linked = _thread_from_subagent_task(agent, task_id, tool_name=tool_name)
    if isinstance(linked, ToolExecutionResult):
        return linked
    if linked:
        return linked, task_id
    return _materialize_internal_thread_for_task(agent, task_id, tool_name=tool_name) if materialize else None


def thread_from_current_runner(agent: SimpleAgent) -> tuple[str, str] | ToolExecutionResult | None:
    run_id = current_subagent_run_id(agent)
    return thread_from_task(agent, run_id, materialize=True, tool_name="raise_collaboration") if run_id else None


def _explicit_thread(agent: SimpleAgent, thread_id: str, task_id: str, *, tool_name: str) -> tuple[str, str] | ToolExecutionResult:
    thread = _load_conversation_thread(
        agent,
        thread_id,
        context="raise_collaboration.load_thread",
        tool_name=tool_name,
    )
    if isinstance(thread, ToolExecutionResult):
        return thread
    if thread is not None:
        return thread_id, task_id
    return error(tool_name, "unknown_thread", f"unknown conversation thread: {thread_id}")


def _materialize_internal_thread_for_task(
    agent: SimpleAgent,
    task_id: str,
    *,
    tool_name: str,
) -> tuple[str, str] | ToolExecutionResult | None:
    task = _load_subagent_task(agent, task_id, tool_name=tool_name)
    if isinstance(task, ToolExecutionResult):
        return task
    current = getattr(agent, "_current_run_params", None)
    if task is None and str(getattr(current, "task_id", "") or "").strip() != task_id:
        return None
    goal = str(getattr(task, "goal", "") or getattr(current, "prompt", "") or task_id)
    owner = str(getattr(task, "owner", "") or getattr(task, "agent_name", "") or "local-agent")
    try:
        thread = agent.conversation_store.get_or_create_thread(
            {
                "canonical_user_id": "local-agent",
                "channel": "internal",
                "channel_conversation_id": f"task:{task_id}",
                "channel_user_id": owner,
                "title": goal[:80],
            }
        )
        agent.conversation_store.bind_task(
            {
                "thread_id": thread.thread_id,
                "task_id": task_id,
                "goal": goal,
                "status": str(getattr(task, "status", "") or "active"),
            }
        )
    except Exception as exc:
        return error(
            tool_name,
            "internal_thread_materialize_failed",
            f"internal collaboration thread materialization failed: {task_id}",
            _load_error(exc, "raise_collaboration.materialize_internal_thread"),
        )
    _remember_thread_on_task(agent, task, thread.thread_id, task_id)
    return thread.thread_id, task_id


def _thread_from_subagent_task(agent: SimpleAgent, task_id: str, *, tool_name: str) -> str | ToolExecutionResult:
    task = _load_subagent_task(agent, task_id, tool_name=tool_name)
    if isinstance(task, ToolExecutionResult):
        return task
    attrs = getattr(task, "attributes", {}) if task is not None else {}
    thread_id = str(attrs.get("conversation_thread_id") or "").strip() if isinstance(attrs, dict) else ""
    if not thread_id:
        return ""
    thread = _load_conversation_thread(
        agent,
        thread_id,
        context="raise_collaboration.load_linked_thread",
        tool_name=tool_name,
    )
    if isinstance(thread, ToolExecutionResult):
        return thread
    return thread_id if thread is not None else ""


def _load_conversation_thread(
    agent: SimpleAgent,
    thread_id: str,
    *,
    context: str,
    tool_name: str,
):
    try:
        thread, load_error = _load_thread_with_report(agent, thread_id)
    except Exception as exc:
        return error(
            tool_name,
            "thread_lookup_failed",
            f"conversation thread lookup failed: {thread_id}",
            _load_error(exc, context),
        )
    if load_error is not None:
        return error(
            tool_name,
            "thread_lookup_failed",
            f"conversation thread lookup failed: {thread_id}",
            _report_load_error(load_error, context),
        )
    return thread


def _load_thread_with_report(agent: SimpleAgent, thread_id: str):
    if callable(getattr(agent.conversation_store, "load_thread_report", None)):
        return agent.conversation_store.load_thread_report(thread_id)
    return agent.conversation_store.load_thread(thread_id), None


def _load_subagent_task(agent: SimpleAgent, task_id: str, *, tool_name: str):
    try:
        return agent.subagents.load(task_id)
    except Exception as exc:
        return error(
            tool_name,
            "subagent_task_load_failed",
            f"subagent task lookup failed while resolving collaboration thread: {task_id}",
            _load_error(exc, "raise_collaboration.subagents.load"),
        )


def _remember_thread_on_task(agent: SimpleAgent, task, thread_id: str, task_id: str) -> None:
    attrs = getattr(task, "attributes", {}) if task is not None else {}
    if not isinstance(attrs, dict):
        return
    updated = {**attrs, "conversation_thread_id": thread_id, "conversation_task_id": task_id}
    if updated == attrs:
        return
    task.attributes = updated
    try:
        agent.subagents.save(task)
    except Exception as exc:
        report = runtime_error_report(exc, context="raise_collaboration.remember_thread_on_task")
        report["task_id"] = task_id
        report["thread_id"] = thread_id
        _LOGGER.warning("collaboration thread binding could not be saved on task: %s", report)


def _load_error(exc: BaseException, context: str) -> dict[str, object]:
    return {"load_error": runtime_error_report(exc, context=context)}


def _report_load_error(report: dict[str, object], context: str) -> dict[str, object]:
    payload = dict(report)
    payload["read_context"] = payload.get("context", "")
    payload["context"] = context
    return {"load_error": payload}


def _current_runner_or_error(agent: SimpleAgent, original_error: ToolExecutionResult) -> tuple[str, str] | ToolExecutionResult:
    current = thread_from_current_runner(agent)
    return current if current else original_error

# Collaboration evidence tools
from typing import TYPE_CHECKING

from ..tooling.models import BaseTool, ToolExecutionResult

if TYPE_CHECKING:
    from ..core import SimpleAgent


class SubmitCollaborationResultTool(BaseTool):
    def __init__(self, agent: SimpleAgent):
        self.agent = agent
        self.spec = build_submit_collaboration_result_spec()

    def execute(self, params: dict[str, object]) -> ToolExecutionResult:
        case_id = str(params.get("case_id") or "").strip()
        if not case_id:
            return error("submit_collaboration_result", "case_id_required", "case_id is required")
        evidence = self.agent.collaboration_store.submit_evidence({"case_id": case_id, **_evidence_kwargs(self.agent, params)})
        payload = _evidence_payload(case_id, evidence.evidence_id, params)
        payload.update(collaboration_scope_payload(self.agent, params, explicit_keys=("source_agent_id", "actor_agent_id", "agent_id", "run_id")))
        return ok("submit_collaboration_result", payload)


def _evidence_kwargs(agent: SimpleAgent, params: dict[str, object]) -> dict[str, object]:
    return {
        "request_id": str(params.get("request_id") or ""),
        "source_agent_id": actor_agent_id(agent, params),
        "matched": bool(params.get("matched", False)),
        "summary": str(params.get("summary") or ""),
        "evidence_refs": string_values(params.get("evidence_refs")),
        "queried_scopes": string_values(params.get("queried_scopes")),
        "used_query_hints": string_values(params.get("used_query_hints")),
        "miss_reason": str(params.get("miss_reason") or ""),
        "response_facts": dict_values(params.get("response_facts")),
        "followup_suggestions": dict_values(params.get("followup_suggestions")),
        "query_actions": dict_values(params.get("query_actions")),
        "confidence": float_value(params.get("confidence")),
        "limitations": string_values(params.get("limitations")),
        "metadata": dict_value(params.get("metadata")),
    }


def _evidence_payload(case_id: str, evidence_id: str, params: dict[str, object]) -> dict[str, object]:
    request_id = str(params.get("request_id") or "").strip()
    payload = {"case_id": case_id, "request_id": request_id, "evidence_id": evidence_id, "case_ref": f"collaboration://case/{case_id}", "evidence_ref": f"collaboration://evidence/{evidence_id}"}
    if request_id:
        payload["request_ref"] = f"collaboration://request/{request_id}"
    return payload

# Collaboration case tools
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..runtime_errors import runtime_error_report
from ..tooling.models import BaseTool, ToolExecutionResult

if TYPE_CHECKING:
    from ..core import SimpleAgent


@dataclass(frozen=True)
class _CasePayloadRefs:
    thread_id: str
    task_id: str
    params: dict[str, object]


@dataclass(frozen=True)
class _EventCaseInput:
    thread_id: str
    task_id: str
    actor_id: str
    params: dict[str, object]


@dataclass(frozen=True)
class _EventPayloadRefs:
    thread_id: str
    task_id: str
    runtime: dict[str, object]


class RaiseCollaborationTool(BaseTool):
    def __init__(self, agent: SimpleAgent):
        self.agent = agent
        self.spec = build_raise_collaboration_spec()

    def execute(self, params: dict[str, object]) -> ToolExecutionResult:
        actor_id = actor_agent_id(self.agent, params)
        case_id = str(params.get("case_id") or "").strip()
        if case_id:
            case = None
            thread_id = ""
            task_id = ""
        else:
            resolved = resolve_thread(self.agent, params)
            if isinstance(resolved, ToolExecutionResult):
                return resolved
            thread_id, task_id = resolved
            case = self._open_event_case(_EventCaseInput(thread_id, task_id, actor_id, params))
            case_id = case.case_id
        request = self._request_event(case_id, actor_id, params) if _should_request_collaboration(params) else None
        runtime = target_runtime_summary(self.agent, list(request.target_agent_ids) if request is not None else [])
        if request is not None:
            runtime.update(_request_runtime_diagnostics(request))
        payload = _raise_collaboration_payload(case, request, _EventPayloadRefs(thread_id, task_id, runtime))
        payload.update(collaboration_scope_payload(self.agent, params, explicit_keys=("created_by", "requester_agent_id", "actor_agent_id", "agent_id", "run_id")))
        return ok("raise_collaboration", payload)

    def _open_event_case(self, event: _EventCaseInput):
        params = event.params
        return self.agent.collaboration_store.open_case({'thread_id': event.thread_id, 'task_id': event.task_id, 'title': str(params.get("title") or "协作事件"), 'summary': str(params.get("summary") or str(params.get("problem_statement") or "")), 'priority': str(params.get("priority") or "normal"), 'created_by': event.actor_id, 'entities': dict_value(params.get("entities")), 'required_capabilities': string_values(params.get("required_capabilities")), 'metadata': {"created_by_tool": "raise_collaboration", **dict_value(params.get("metadata"))}})

    def _request_event(self, case_id: str, actor_id: str, params: dict[str, object]):
        targets, resolution_errors = resolved_target_agent_ids_report(self.agent, params)
        runtime = target_runtime_summary(
            self.agent,
            targets,
            resolution_errors=resolution_errors,
        )
        return self.agent.collaboration_store.request_collaboration({'case_id': case_id, 'requester_agent_id': actor_id, 'required_capabilities': string_values(params.get("required_capabilities")), 'question': str(params.get("question") or ""), 'entities': dict_value(params.get("entities")), 'problem_statement': str(params.get("problem_statement") or ""), 'observed_facts': dict_values(params.get("observed_facts")), 'query_intent': dict_value(params.get("query_intent")), 'query_hints': dict_values(params.get("query_hints")), 'routing_requirements': dict_value(params.get("routing_requirements")), 'response_contract': dict_value(params.get("response_contract")), 'context_refs': string_values(params.get("context_refs")), 'target_agent_ids': targets, 'deadline_at': deadline_at(self.agent, params), 'priority': str(params.get("priority") or ""), 'metadata': {"created_by_tool": "raise_collaboration", **dict_value(params.get("metadata")), **target_runtime_metadata(runtime)}})


class UpdateCollaborationTool(BaseTool):
    def __init__(self, agent: SimpleAgent):
        self.agent = agent
        self.spec = build_update_collaboration_spec()

    def execute(self, params: dict[str, object]) -> ToolExecutionResult:
        if str(params.get("request_id") or "").strip():
            return update_request(self.agent, "update_collaboration", params, targets=string_values(params.get("target_agent_ids")))
        case_id = str(params.get("case_id") or "").strip()
        if not case_id:
            return error("update_collaboration", "case_id_required", "case_id is required")
        try:
            case = self.agent.collaboration_store.record_case_status(
                {
                    "case_id": case_id,
                    "status": str(params.get("status") or ""),
                    "actor_agent_id": actor_agent_id(self.agent, params),
                    "summary": str(params.get("summary") or ""),
                    "decision_type": str(params.get("decision_type") or ""),
                    "evidence_ids": string_values(params.get("evidence_ids")),
                    "metadata": dict_value(params.get("metadata")),
                }
            )
        except (KeyError, ValueError) as exc:
            return error("update_collaboration", "case_status_update_failed", str(exc))
        except Exception as exc:
            return error(
                "update_collaboration",
                "case_status_update_failed",
                "collaboration case status update failed",
                _load_error(exc, "update_collaboration.record_case_status"),
            )
        decisions, decision_error = _case_decisions(self.agent, case_id)
        payload = {"case": case.to_dict(), "decision": decisions[-1].to_dict() if decisions else {}}
        if decision_error:
            payload["decision_load_error"] = runtime_error_report(
                decision_error,
                context="update_collaboration.case_decisions",
            )
        payload.update(collaboration_scope_payload(self.agent, params, explicit_keys=("actor_agent_id", "agent_id", "run_id")))
        return ok("update_collaboration", payload)


class InspectCollaborationTool(BaseTool):
    def __init__(self, agent: SimpleAgent):
        self.agent = agent
        self.spec = build_inspect_collaboration_spec()

    def execute(self, params: dict[str, object]) -> ToolExecutionResult:
        case_id = str(params.get("case_id") or "").strip()
        if not case_id:
            return list_pending_requests(self.agent, params)
        try:
            return ok("inspect_collaboration", self.agent.collaboration_store.case_status(case_id))
        except Exception as exc:
            return error(
                "inspect_collaboration",
                "case_status_read_failed",
                f"collaboration case status lookup failed: {case_id}",
                _load_error(exc, "inspect_collaboration.case_status"),
            )


def list_pending_requests(agent: SimpleAgent, params: dict[str, object]) -> ToolExecutionResult:
    identity, identity_error = request_identity_report(agent, params)
    if identity_error:
        return error(
            "inspect_collaboration",
            "wrong_status_surface",
            "agent identity could not be loaded; inspect the agent tree status surface",
            {
                **identity,
                "identity_load_error": identity_error,
                "suggested_tool_call": {"tool": "inspect_agent_tree"},
            },
        )
    if not any(identity.values()):
        return error("inspect_collaboration", "agent_identity_required", "agent_id, agent_name, agent_role, or current runner identity is required")
    try:
        requests, load_errors = agent.collaboration_store.pending_requests_for_agent_report(
            agent_id=identity["agent_id"],
            agent_name=identity["agent_name"],
            agent_role=identity["agent_role"],
            limit=limit_param(params.get("limit"), default=10),
        )
    except Exception as exc:
        return error(
            "inspect_collaboration",
            "pending_requests_read_failed",
            "pending collaboration request lookup failed",
            _load_error(exc, "inspect_collaboration.pending_requests"),
        )
    payload = {**identity, "request_count": len(requests), "requests": requests}
    if load_errors:
        payload["load_errors"] = load_errors
    if identity_error:
        payload["identity_load_error"] = identity_error
    payload.update(collaboration_scope_payload(agent, params, explicit_keys=("agent_id", "run_id")))
    if not requests:
        payload.update(_empty_request_projection(agent, params, identity))
    return ok("inspect_collaboration", payload)


def update_request(agent: SimpleAgent, tool: str, params: dict[str, object], *, targets: list[str]) -> ToolExecutionResult:
    return _update_request_tool(agent, tool, params, targets=targets)


def _update_request_tool(agent: SimpleAgent, tool: str, params: dict[str, object], *, targets: list[str]) -> ToolExecutionResult:
    ids = _case_and_request_ids(params)
    if isinstance(ids, ToolExecutionResult):
        return ids
    case_id, request_id = ids
    try:
        request = agent.collaboration_store.update_request_status(
            {
                "case_id": case_id,
                "request_id": request_id,
                "status": str(params.get("status") or "pending"),
                "actor_agent_id": actor_agent_id(agent, params),
                "summary": str(params.get("summary") or ""),
                "target_agent_ids": targets,
                "metadata": dict_value(params.get("metadata")),
            }
        )
    except (KeyError, ValueError) as exc:
        return error(tool, "request_update_failed", str(exc))
    payload = {"request": request.to_dict(), "overview": _request_overview(agent, case_id)}
    payload.update(collaboration_scope_payload(agent, params, explicit_keys=("actor_agent_id", "agent_id", "run_id")))
    return ok(tool, payload)


def _case_and_request_ids(params: dict[str, object]) -> tuple[str, str] | ToolExecutionResult:
    case_id = str(params.get("case_id") or "").strip()
    request_id = str(params.get("request_id") or "").strip()
    if not case_id:
        return error("update_collaboration", "case_id_required", "case_id is required")
    if not request_id:
        return error("update_collaboration", "request_id_required", "request_id is required")
    return case_id, request_id


def _request_overview(agent: SimpleAgent, case_id: str) -> dict[str, object]:
    try:
        status = agent.collaboration_store.case_status(case_id)
    except Exception as exc:
        return {"overview_load_error": runtime_error_report(exc, context="update_collaboration.case_status")}
    return {key: status[key] for key in ("pending_request_count", "blocked_request_count", "timed_out_request_count", "completed_request_count", "ready_for_main_agent")}


def _empty_request_projection(
    agent: SimpleAgent,
    params: dict[str, object],
    identity: dict[str, str],
) -> dict[str, object]:
    projection: dict[str, object] = {
        "status_surface": "collaboration_requests",
        "empty_reason": "no_pending_collaboration_requests",
    }
    suggestion = _agent_tree_suggestion_if_relevant(agent, params, identity)
    if suggestion:
        projection["related_status_surface"] = "agent_tree"
        projection["suggested_tool_call"] = suggestion
    return projection


def _agent_tree_suggestion_if_relevant(
    agent: SimpleAgent,
    params: dict[str, object],
    identity: dict[str, str],
) -> dict[str, object]:
    run_id = _structured_run_id_for_agent_tree(agent, params, identity)
    has_runs = run_id or _has_any_subagent_runs(agent)
    if not has_runs:
        return {}
    suggestion: dict[str, object] = {"tool": "inspect_agent_tree"}
    if run_id:
        suggestion["run_id"] = run_id
    return suggestion


def _structured_run_id_for_agent_tree(
    agent: SimpleAgent,
    params: dict[str, object],
    identity: dict[str, str],
) -> str:
    for key in ("run_id", "task_id", "root_id"):
        value = str(params.get(key) or "").strip()
        if value:
            return value
    agent_id = str(params.get("agent_id") or identity.get("agent_id") or "").strip()
    if agent_id and _is_known_subagent_run(agent, agent_id):
        return agent_id
    return ""


def _is_known_subagent_run(agent: SimpleAgent, run_id: str) -> bool:
    manager = getattr(agent, "subagents", None)
    if manager is None or not hasattr(manager, "list_runs"):
        return False
    try:
        return any(str(getattr(task, "id", "") or "") == run_id for task in manager.list_runs())
    except Exception:
        # list_runs 失败是"系统不可用",回退 False 但不能无声(否则被当成"无此 run")。
        _LOGGER.warning("list_runs failed in _is_known_subagent_run (run_id=%s)", run_id, exc_info=True)
        return False


def _has_any_subagent_runs(agent: SimpleAgent) -> bool:
    manager = getattr(agent, "subagents", None)
    if manager is None or not hasattr(manager, "list_runs"):
        return False
    try:
        return bool(list(manager.list_runs()))
    except Exception:
        # 同上:list_runs 故障回退 False,但记日志避免"系统不可用"被伪装成"无任何 run"。
        _LOGGER.warning("list_runs failed in _has_any_subagent_runs", exc_info=True)
        return False


def _should_request_collaboration(params: dict[str, object]) -> bool:
    return any(
        params.get(key) not in (None, "", [], {})
        for key in (
            "question",
            "target_agent_ids",
            "observed_facts",
            "query_hints",
            "problem_statement",
        )
    )


def _open_case_payload(case, refs: _CasePayloadRefs) -> dict[str, object]:
    return {"case_id": case.case_id, "case_ref": f"collaboration://case/{case.case_id}", "thread_id": refs.thread_id, "task_id": refs.task_id}


def _raise_collaboration_payload(case, request, refs: _EventPayloadRefs) -> dict[str, object]:
    case_id = str(getattr(case, "case_id", "") or getattr(request, "case_id", ""))
    payload: dict[str, object] = {
        "case_id": case_id,
        "case_ref": f"collaboration://case/{case_id}" if case_id else "",
        "thread_id": refs.thread_id,
        "task_id": refs.task_id,
    }
    if request is not None:
        payload.update({
            "request_id": request.request_id,
            "request_ref": f"collaboration://request/{request.request_id}",
            "target_agent_ids": list(request.target_agent_ids),
            "required_capabilities": list(request.required_capabilities),
            **target_response_payload(refs.runtime),
            "next_action": "有 available_target_run_ids 时可 dispatch_subagents 唤醒目标代理；到 deadline 后带已回/未回结果继续推进。",
        })
    return payload


def _request_runtime_diagnostics(request) -> dict[str, object]:
    metadata = getattr(request, "metadata", {}) or {}
    diagnostics: dict[str, object] = {}
    for key in ("target_runtime_load_error", "target_resolution_errors", "capability_roster_load_error"):
        if metadata.get(key):
            diagnostics[key] = metadata[key]
    return diagnostics


def _case_decisions(agent: SimpleAgent, case_id: str) -> tuple[list[object], BaseException | None]:
    try:
        return list(agent.collaboration_store.case_decisions(case_id)), None
    except Exception as exc:
        return [], exc


def _load_error(exc: BaseException, context: str) -> dict[str, object]:
    return {"load_error": runtime_error_report(exc, context=context)}