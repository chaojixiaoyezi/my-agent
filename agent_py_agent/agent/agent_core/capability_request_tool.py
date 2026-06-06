
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..common.value_parsing import TOOL_TEXT_LIST_OPTIONS, string_list
from ..subagents.role_templates import is_self_authorized_root_task
from ..subagents.services.lifecycle import RecordCapabilityRequestParams
from ..tooling.models import BaseTool, ToolExecutionResult, ToolSpec
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

    def __init__(self, agent: SimpleAgent):
        self.agent = agent
        self.spec = build_capability_request_spec()

    def execute(self, params: dict[str, object]) -> ToolExecutionResult:
        request = _capability_request_input(self.agent, params)
        if isinstance(request, ToolExecutionResult):
            return request
        try:
            record = self.agent.subagents.lifecycle.record_capability_request(request.run_id, request.params)
        except FileNotFoundError:
            return _capability_error(f"run_id 不存在: {request.run_id}")
        payload = {
            "request_id": record.id,
            "run_id": request.run_id,
            "status": record.status,
            "next_action": "route_capability_request",
            "message": "已记录 OPEN capability_request；请停止伪造能力结果，并在最终结果块写 status=PENDING_CAPABILITY_REQUEST 或 BLOCKED。",
        }
        payload.update(scope_resolution_payload(request.scope_resolution))
        return _capability_ok(payload)


def build_capability_request_spec() -> ToolSpec:
    return ToolSpec(
        name=_TOOL_NAME,
        category="orchestration",
        effect="mutating",
        requires_idempotency=True,
        description="为当前 subagent run 记录一条待父级处理的能力申请；不授权也不执行工具。",
        use_cases=[
            "runner 需要当前工具目录之外的网络、MCP、skill 或新工具才能继续",
            "runner 需要父级扩大运行权限或代为处理当前权限下做不了的动作",
            "当前工具集无法产出真实证据，需要父级授权后重跑",
        ],
        avoid_when=[
            "只是推荐父级运行测试命令，可以写 tests/next_actions，不需要申请能力",
            "当前已有 run_command、write_file、apply_patch 等普通工具能完成任务",
        ],
        keywords=["capability", "request", "grant", "tool", "skill", "shell", "MCP", "能力申请", "授权"],
        parameters=_capability_request_parameters(),
        parameter_details=_capability_request_parameter_details(),
        examples=[
            (
                '{"tool":"capability_request","problem":"当前工具无法访问必要的内部系统查询结果",'
                '"needed_capability":"internal_api_access","capability_type":"network",'
                '"requested_tools":["internal_api_query"],"expected_output":"拿到查询结果或明确失败原因",'
                '"risk_level":"low"}'
            )
        ],
    )


def _capability_request_input(agent: object, params: dict[str, object]) -> CapabilityRequestToolInput | ToolExecutionResult:
    normalized = _capability_params(params)
    resolution = identity_scope_resolution(
        agent,
        normalized,
        explicit_keys=("run_id", "from_run_id", "agent_id"),
    )
    current_run_id = current_subagent_run_id(agent)
    run_id = str(resolution.effective.get("agent_id") or current_run_id).strip()
    if not run_id:
        return _capability_error("缺少 run_id；runner 内会自动使用当前 run id。")
    if _is_root_run(agent, run_id):
        return _capability_error("root run 不走 capability_request；root 当前不应缺能力，请使用现有工具、调度下级或说明暂不支持。")
    problem = str(normalized.get("problem") or "").strip()
    if not problem:
        return _capability_error("缺少 problem；必须说明当前被什么能力缺口阻塞。")
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


def _capability_request_parameters() -> dict[str, str]:
    return {
        "problem": "必填；当前被什么能力缺口阻塞",
        "needed_capability": "能力名，例如 network_api、browser_login、skill:xxx、internal_tool",
        "capability_type": "shell/tool/skill/mcp/network/generic",
        "requested_tools": "希望父级授权的工具名列表",
        "requested_skills": "希望父级授权或加载的 skill 名列表",
        "requested_mcp_tools": "希望父级授权的 MCP 工具名列表",
        "expected_output": "拿到能力后预计能产出的结果或证据",
        "risk_level": "low/medium/high；高风险必须说明原因和替代方案",
    }


def _capability_request_parameter_details() -> dict[str, str]:
    return {
        "problem": "写清楚为什么现有工具不能继续；不要只写“需要工具”。",
        "needed_capability": "父级用它做路由检索；能具体就具体。",
        "capability_type": "shell 表示命令执行；tool 表示内置工具；skill 表示知识/流程；mcp/network 分别表示 MCP 或网络能力。",
        "requested_tools": "只列真正需要的工具；能用已有 run_command/write_file/apply_patch 完成时不要申请。",
        "requested_skills": "只列任务确实需要的 skill；不要把普通任务包装成能力申请。",
        "requested_mcp_tools": "只列需要父级接入的外部工具；不知道名称时在 problem 里说明需要什么能力即可。",
        "expected_output": "说明父级处理后应该返回什么，例如文件路径、查询结果、审批结果或失败原因。",
        "risk_level": "涉及删除、网络写入、大量输出或跨目录访问时至少 medium。",
    }


def _capability_ok(payload: dict[str, object]) -> ToolExecutionResult:
    return ToolExecutionResult(_TOOL_NAME, True, json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _capability_error(message: str) -> ToolExecutionResult:
    return ToolExecutionResult(_TOOL_NAME, False, message)
