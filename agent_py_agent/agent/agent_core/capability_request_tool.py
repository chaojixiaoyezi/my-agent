
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..common.value_parsing import TOOL_TEXT_LIST_OPTIONS, string_list
from ..subagents.role_templates import is_self_authorized_root_task
from ..subagents.services.lifecycle import RecordCapabilityRequestParams
from ..tooling.models import (
    BaseTool,
    EffectResolverPolicy,
    IdempotencyPolicy,
    ResourceScopePolicy,
    ToolHandlerOutcome,
    ToolInputPolicy,
    ToolModelHints,
    ToolModelSpec,
    ToolRuntimePolicy,
)
from .orchestration.scope_resolution import (
    ScopeResolution,
    identity_scope_resolution,
    scope_resolution_payload,
)
from .runner.context import current_subagent_run_id

if TYPE_CHECKING:
    from ..core import SimpleAgent


_TOOL_NAME = "capability_request"


@dataclass(frozen=True)
class CapabilityRequestToolInput:
    run_id: str
    params: RecordCapabilityRequestParams
    scope_resolution: ScopeResolution


class CapabilityRequestTool(BaseTool):

    runtime_policy = ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy("mutating"),
        idempotency_policy=IdempotencyPolicy("operation"),
        resource_scopes=ResourceScopePolicy(mode="declared", static_scopes=("capability_requests",)),
        input_policy=ToolInputPolicy(
            internal_parameters=("run_id", "from_run_id", "agent_id"),
        ),
    )

    def __init__(self, agent: SimpleAgent):
        self.agent = agent
        self.model_spec = build_capability_request_model_spec()

    def execute(self, params: dict[str, object]) -> ToolHandlerOutcome:
        request = _capability_request_input(self.agent, params)
        if isinstance(request, ToolHandlerOutcome):
            return request
        try:
            record = self.agent.subagents.lifecycle.record_capability_request(request.run_id, request.params)
        except FileNotFoundError:
            return _capability_error(
                f"run_id 不存在: {request.run_id}",
                error_code="TOOL_INVALID_ARGUMENTS",
            )
        # 常规能力(shell/写自己任务沙箱)机制层自动批,不再等主代理模型手动 resolve——
        # 真机 3 例(A1-u2/B-u1/B-u3)主代理不批导致子代理卡 BLOCKED 到收口失败。
        from ..subagents.capability_auto_grant import auto_grant_routine_request

        auto_grant = auto_grant_routine_request(self.agent.subagents, request.run_id, record.id)
        if auto_grant is None:
            # R4 子项②：提交即推送父级（observation + wake），主代理不再对未决请求失明。
            from ..subagents.runner_completion_wake import notify_parent_on_capability_request

            notify_parent_on_capability_request(
                self.agent.subagents,
                self.agent.subagents.load(request.run_id),
                record,
            )
        payload = self._request_payload(request.run_id, record, auto_grant)
        payload.update(scope_resolution_payload(request.scope_resolution))
        return _capability_ok(payload)

    # 函数用途: 按"自动批成/待父级裁决"两种结局构造工具响应(子代理模型照 message 行动)。
    def _request_payload(self, run_id: str, record: object, auto_grant: object) -> dict[str, object]:
        if auto_grant is None:
            return {
                "request_id": record.id,
                "run_id": run_id,
                "status": record.status,
                "next_action": "route_capability_request",
                "message": "已记录 OPEN capability_request；请停止伪造能力结果，并在最终结果块写 status=PENDING_CAPABILITY_REQUEST 或 BLOCKED。",
            }
        return {
            "request_id": record.id,
            "run_id": run_id,
            "status": "GRANTED",
            "auto_granted": True,
            "granted_tools": list(getattr(auto_grant, "tools", []) or []),
            "granted_path_scope": list(getattr(auto_grant, "path_scope", []) or []),
            "next_action": "finish_run_as_blocked_for_auto_redispatch",
            "message": (
                "常规能力已机制层自动授权（范围=你自己的任务沙箱）。授权在续跑时生效："
                "请把当前 run 以 status=BLOCKED 正常收尾，写清已完成部分和下一步，"
                "系统会自动带新权限续派你继续同一任务；不要伪造能力结果、不要原地重试。"
            ),
        }


def build_capability_request_model_spec() -> ToolModelSpec:
    return ToolModelSpec(
        name=_TOOL_NAME,
        description=(
            "为当前 subagent run 提交能力申请。常规能力（shell/读写自己任务沙箱）"
            "会被机制层立即自动授权并安排续跑；外部能力（网络/MCP/skill/越界路径/高风险）"
            "记录为待父级处理，本工具自身不执行任何工具。"
        ),
        input_schema=_capability_request_input_schema(),
        hints=ToolModelHints(
            category="orchestration",
            use_cases=(
                "runner 需要当前工具目录之外的网络、MCP、skill 或新工具才能继续",
                "runner 需要父级扩大运行权限或代为处理当前权限下做不了的动作",
                "当前工具集无法产出真实证据，需要父级授权后重跑",
            ),
            avoid_when=(
                "只是推荐父级运行测试命令，可以写 tests/next_actions，不需要申请能力",
                "当前已有 run_command、write_file、apply_patch 等普通工具能完成任务",
            ),
            keywords=("capability", "request", "grant", "tool", "skill", "shell", "MCP", "能力申请", "授权"),
            examples=((
                '{"tool":"capability_request","problem":"当前工具无法访问必要的内部系统查询结果",'
                '"needed_capability":"internal_api_access","capability_type":"network",'
                '"requested_tools":["internal_api_query"],"expected_output":"拿到查询结果或明确失败原因",'
                '"risk_level":"low"}'
            ),),
        ),
    )


def _capability_request_input(agent: object, params: dict[str, object]) -> CapabilityRequestToolInput | ToolHandlerOutcome:
    normalized = _capability_params(params)
    resolution = identity_scope_resolution(
        agent,
        normalized,
        explicit_keys=("run_id", "from_run_id", "agent_id"),
    )
    current_run_id = current_subagent_run_id(agent)
    run_id = str(resolution.effective.get("agent_id") or current_run_id).strip()
    if not run_id:
        return _capability_error(
            "缺少 run_id；runner 内会自动使用当前 run id。",
            error_code="TOOL_PARAMETER_REQUIRED",
        )
    if _is_root_run(agent, run_id):
        return _capability_error(
            "root run 不走 capability_request；root 当前不应缺能力，请使用现有工具、调度下级或说明暂不支持。",
            error_code="TOOL_NOT_ALLOWED",
        )
    problem = str(normalized.get("problem") or "").strip()
    if not problem:
        return _capability_error(
            "缺少 problem；必须说明当前被什么能力缺口阻塞。",
            error_code="TOOL_PARAMETER_REQUIRED",
        )
    return CapabilityRequestToolInput(
        run_id=run_id,
        params=_record_params(normalized, problem),
        scope_resolution=resolution,
    )


def _is_root_run(agent: object, run_id: str) -> bool:
    manager = getattr(agent, "subagents", None)
    if manager is None or not hasattr(manager, "load"):
        return False
    try:
        task = manager.load(run_id)
    except FileNotFoundError:
        return False
    return is_self_authorized_root_task(task)


def _record_params(params: dict[str, object], problem: str) -> RecordCapabilityRequestParams:
    requested_commands = string_list(params.get("requested_commands"), TOOL_TEXT_LIST_OPTIONS)
    requested_tools = string_list(params.get("requested_tools"), TOOL_TEXT_LIST_OPTIONS)
    needed = _needed_capability(params, requested_tools, requested_commands)
    return RecordCapabilityRequestParams(
        problem=problem,
        needed_capability=needed,
        expected_output=str(params.get("expected_output") or ""),
        capability_type=_capability_type(params, requested_tools, requested_commands),
        tried=string_list(params.get("tried"), TOOL_TEXT_LIST_OPTIONS),
        evidence=string_list(params.get("evidence"), TOOL_TEXT_LIST_OPTIONS),
        constraints=_string_dict(params.get("constraints")),
        requested_tools=requested_tools,
        requested_skills=string_list(params.get("requested_skills"), TOOL_TEXT_LIST_OPTIONS),
        requested_mcp_tools=string_list(params.get("requested_mcp_tools"), TOOL_TEXT_LIST_OPTIONS),
        requested_commands=requested_commands,
        cwd_scope=string_list(params.get("cwd_scope"), TOOL_TEXT_LIST_OPTIONS),
        path_scope=string_list(params.get("path_scope"), TOOL_TEXT_LIST_OPTIONS),
        network_scope=string_list(params.get("network_scope"), TOOL_TEXT_LIST_OPTIONS),
        output_budget=_object_dict(params.get("output_budget")),
        risk_level=str(params.get("risk_level") or ""),
        alternatives_attempted=string_list(params.get("alternatives_attempted"), TOOL_TEXT_LIST_OPTIONS),
        escalation_target=str(params.get("escalation_target") or "parent"),
    )


def _capability_params(params: dict[str, object]) -> dict[str, object]:
    return dict(params)


def _needed_capability(params: dict[str, object], tools: list[str], commands: list[str]) -> str:
    explicit = str(params.get("needed_capability") or "").strip()
    if explicit:
        return explicit
    if "controlled_exec" in tools:
        return "controlled_exec"
    if commands:
        return "shell"
    return "capability"


def _capability_type(params: dict[str, object], tools: list[str], commands: list[str]) -> str:
    explicit = str(params.get("capability_type") or "").strip()
    if explicit:
        return explicit
    if commands or "controlled_exec" in tools:
        return "shell"
    return "generic"


def _string_dict(value: object) -> dict[str, str]:
    parsed = _object_dict(value)
    return {key: str(item) for key, item in parsed.items() if str(item).strip()}


def _object_dict(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return {str(key): item for key, item in value.items()}
    return {}


def _capability_request_input_schema() -> dict[str, object]:
    string_list_schema = {"type": "array", "items": {"type": "string"}}
    properties: dict[str, object] = {
        "problem": {"type": "string", "description": "写清楚为什么现有工具不能继续；不要只写需要工具。"},
        "needed_capability": {"type": "string", "description": "具体能力名，如 network_api、browser_login、skill:xxx 或 internal_tool。"},
        "capability_type": {
            "type": "string",
            "enum": ["shell", "tool", "skill", "mcp", "network", "generic"],
            "description": "能力分类，用于父级路由。",
        },
        "requested_tools": {**string_list_schema, "description": "真正需要父级授权的工具名。"},
        "requested_skills": {**string_list_schema, "description": "任务确实需要的 skill 名。"},
        "requested_mcp_tools": {**string_list_schema, "description": "需要父级接入的 MCP 工具名。"},
        "requested_commands": {**string_list_schema, "description": "需要授权的命令或命令前缀。"},
        "cwd_scope": {**string_list_schema, "description": "命令工作目录范围。"},
        "path_scope": {**string_list_schema, "description": "需要读写的路径范围。"},
        "network_scope": {**string_list_schema, "description": "需要访问的网络主机或域范围。"},
        "expected_output": {"type": "string", "description": "父级处理后应返回的结果、证据、审批或失败原因。"},
        "tried": {**string_list_schema, "description": "已经尝试过的安全方案。"},
        "evidence": {**string_list_schema, "description": "证明能力缺口的证据引用。"},
        "constraints": {"type": "object", "additionalProperties": {"type": "string"}, "description": "授权需要遵守的约束。"},
        "output_budget": {"type": "object", "description": "输出预算的结构化要求。"},
        "risk_level": {"type": "string", "enum": ["low", "medium", "high"], "description": "风险等级；删除、网络写入或越界访问至少 medium。"},
        "alternatives_attempted": {**string_list_schema, "description": "已经尝试的替代路径。"},
        "escalation_target": {"type": "string", "description": "升级目标，默认 parent。"},
    }
    return {
        "type": "object",
        "properties": properties,
        "required": ["problem"],
        "additionalProperties": False,
    }


def _capability_ok(payload: dict[str, object]) -> ToolHandlerOutcome:
    return ToolHandlerOutcome(_TOOL_NAME, True, json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _capability_error(message: str, *, error_code: str) -> ToolHandlerOutcome:
    # A missing classification falls back to UNKNOWN_ERROR and tells the model to
    # give up.  Capability failures are all typed parameter, scope, or lookup facts.
    return ToolHandlerOutcome(_TOOL_NAME, False, message, error_code=error_code)
