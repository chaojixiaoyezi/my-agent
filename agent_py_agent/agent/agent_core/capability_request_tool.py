# LLM: Capability request tool gives runners a formal lane to ask parents for missing powers.
# 模块用途: 让子代理在 runner 内通过工具记录能力申请，而不是写假 JSON 文件或改 execution_context。

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..subagents.root_task_policy import is_self_authorized_root_task
from ..subagents.services.lifecycle import RecordCapabilityRequestParams
from ..tools import BaseTool, ToolExecutionResult, ToolSpec
from .parameters import _string_list
from .runner_context import current_subagent_run_id

if TYPE_CHECKING:
    from ..core import SimpleAgent


_TOOL_NAME = "capability_request"


# LLM: CapabilityRequestToolInput carries a normalized self-scoped capability request.
# 类用途: 保存一次能力申请的 run_id 和字段包，工具执行时只写入当前 runner 的任务记录。
@dataclass(frozen=True)
class CapabilityRequestToolInput:
    run_id: str
    params: RecordCapabilityRequestParams


# LLM: CapabilityRequestTool is the model-callable request lane for tools, skills, shell, MCP, and network.
# 类用途: 给子代理正式申请缺失能力；只记录申请，不授权、不执行命令、不跨分支写别的代理状态。
class CapabilityRequestTool(BaseTool):

    # LLM: __init__ stores the agent facade and stable tool metadata.
    # 函数用途: 初始化能力申请工具，不执行任何状态写入。
    def __init__(self, agent: SimpleAgent):
        self.agent = agent
        self.spec = build_capability_request_spec()

    # LLM: execute validates current-run scope and persists one OPEN CapabilityRequest.
    # 函数用途: 把模型的能力申请写入当前 subagent run，返回 request_id 供父级路由。
    def execute(self, params: dict[str, object]) -> ToolExecutionResult:
        request = _capability_request_input(self.agent, params)
        if isinstance(request, ToolExecutionResult):
            return request
        try:
            record = self.agent.subagents.record_capability_request(request.run_id, request.params)
        except FileNotFoundError:
            return _capability_error(f"run_id 不存在: {request.run_id}")
        return _capability_ok({
            "request_id": record.id,
            "run_id": request.run_id,
            "status": record.status,
            "next_action": "route_capability_request",
            "message": "已记录 OPEN capability_request；请停止伪造能力结果，并在最终结果块写 status=PENDING_CAPABILITY_REQUEST 或 BLOCKED。",
        })


# LLM: build_capability_request_spec explains the formal lane and rejects fake file-based requests.
# 函数用途: 构建 capability_request 工具说明，帮助 runner 在缺工具/skill/shell/MCP 时走系统通道。
def build_capability_request_spec() -> ToolSpec:
    return ToolSpec(
        name=_TOOL_NAME,
        category="orchestration",
        effect="mutating",
        requires_idempotency=True,
        description="为当前 subagent run 记录一条待父级路由的能力申请；不授权也不执行工具。",
        use_cases=[
            "runner 需要 controlled_exec、shell 命令、网络、MCP、skill 或新工具才能继续",
            "当前工具集无法产出真实证据，需要父级授权后重跑",
        ],
        avoid_when=[
            "只是推荐父级验收器运行测试命令，可以写 tests/next_actions，不需要申请能力",
            "已经有父级 grant 时应使用被授权的工具，不要重复申请",
        ],
        keywords=["capability", "request", "grant", "tool", "skill", "shell", "MCP", "能力申请", "授权"],
        parameters=_capability_request_parameters(),
        parameter_details=_capability_request_parameter_details(),
        examples=[
            (
                '{"tool":"capability_request","problem":"需要读取当前目录和运行一个小脚本确认环境",'
                '"needed_capability":"controlled_exec","capability_type":"shell",'
                '"requested_tools":["controlled_exec"],"requested_commands":["pwd","python3"],'
                '"path_scope":["/workspace/task"],"output_budget":{"stdout_bytes":65536,"stderr_bytes":32768},'
                '"risk_level":"low"}'
            )
        ],
    )


# LLM: _capability_request_input normalizes bundle-shaped calls and enforces self-run scope.
# 函数用途: 校验 run_id、problem 和能力字段，并转换成生命周期服务参数包。
def _capability_request_input(agent: object, params: dict[str, object]) -> CapabilityRequestToolInput | ToolExecutionResult:
    normalized = _capability_params(params)
    current_run_id = current_subagent_run_id(agent)
    explicit_run_id = str(normalized.get("run_id") or normalized.get("from_run_id") or "").strip()
    if current_run_id and explicit_run_id and explicit_run_id != current_run_id:
        return _capability_error("capability_request 只能为当前 runner 申请能力，不能写入其他 run_id。")
    run_id = explicit_run_id or current_run_id
    if not run_id:
        return _capability_error("缺少 run_id；runner 内会自动使用当前 run id。")
    if _is_root_run(agent, run_id):
        return _capability_error("root run 不走 capability_request；root 当前不应缺能力，请使用现有工具、调度下级或说明暂不支持。")
    problem = str(normalized.get("problem") or "").strip()
    if not problem:
        return _capability_error("缺少 problem；必须说明当前被什么能力缺口阻塞。")
    return CapabilityRequestToolInput(run_id=run_id, params=_record_params(normalized, problem))


# LLM: _is_root_run only blocks self-authorized root/coordinator seeds, not main-agent children.
# 函数用途: 判断当前 run 是否真的是无上级 root；普通一层小傻妞仍能向主代理申请能力。
def _is_root_run(agent: object, run_id: str) -> bool:
    manager = getattr(agent, "subagents", None)
    if manager is None or not hasattr(manager, "load"):
        return False
    try:
        task = manager.load(run_id)
    except FileNotFoundError:
        return False
    return is_self_authorized_root_task(task)


# LLM: _record_params converts a normalized tool payload into the lifecycle request bundle.
# 函数用途: 把能力申请字段集中转成 RecordCapabilityRequestParams，保持业务接口 bundle 化。
def _record_params(params: dict[str, object], problem: str) -> RecordCapabilityRequestParams:
    requested_commands = _string_list(params.get("requested_commands"))
    requested_tools = _string_list(params.get("requested_tools"))
    needed = _needed_capability(params, requested_tools, requested_commands)
    return RecordCapabilityRequestParams(
        problem=problem,
        needed_capability=needed,
        expected_output=str(params.get("expected_output") or ""),
        capability_type=_capability_type(params, requested_tools, requested_commands),
        tried=_string_list(params.get("tried")),
        evidence=_string_list(params.get("evidence")),
        constraints=_string_dict(params.get("constraints")),
        requested_tools=requested_tools,
        requested_skills=_string_list(params.get("requested_skills")),
        requested_mcp_tools=_string_list(params.get("requested_mcp_tools")),
        requested_commands=requested_commands,
        cwd_scope=_string_list(params.get("cwd_scope")),
        path_scope=_string_list(params.get("path_scope")),
        network_scope=_string_list(params.get("network_scope")),
        output_budget=_object_dict(params.get("output_budget")),
        risk_level=str(params.get("risk_level") or ""),
        fallback_attempted=_string_list(params.get("fallback_attempted")),
        escalation_target=str(params.get("escalation_target") or "parent"),
        reserved=_object_dict(params.get("reserved")),
    )


# LLM: _capability_params accepts flat calls plus orchestration/capability_request bundles.
# 函数用途: 兼容模型按 bundle 规范传参；top-level 字段优先，wrapper 字段补齐缺省。
def _capability_params(params: dict[str, object]) -> dict[str, object]:
    normalized: dict[str, object] = {}
    for key in ("orchestration", "capability_request", "request"):
        value = params.get(key)
        if isinstance(value, dict):
            normalized.update(value)
    for key, value in params.items():
        if key not in {"orchestration", "capability_request", "request", "filesystem"}:
            normalized[key] = value
    return normalized


# LLM: _needed_capability infers a conservative name when the model supplies only scoped tool details.
# 函数用途: 优先使用模型显式字段；缺省时从 requested_tools/commands 推断 controlled_exec 或通用能力。
def _needed_capability(params: dict[str, object], tools: list[str], commands: list[str]) -> str:
    explicit = str(params.get("needed_capability") or params.get("capability") or "").strip()
    if explicit:
        return explicit
    if "controlled_exec" in tools:
        return "controlled_exec"
    if commands:
        return "shell"
    return "capability"


# LLM: _capability_type keeps shell/tool/skill/MCP routing hints narrow and predictable.
# 函数用途: 生成 capability_type；显式值优先，否则根据 requested 字段保守推断。
def _capability_type(params: dict[str, object], tools: list[str], commands: list[str]) -> str:
    explicit = str(params.get("capability_type") or "").strip()
    if explicit:
        return explicit
    if commands or "controlled_exec" in tools:
        return "shell"
    return "generic"


# LLM: _string_dict normalizes JSON-object fields without accepting arbitrary scalar text.
# 函数用途: 把 constraints 等字段转成 str->str 字典，避免服务层收到非预期结构。
def _string_dict(value: object) -> dict[str, str]:
    parsed = _object_dict(value)
    return {key: str(item) for key, item in parsed.items() if str(item).strip()}


# LLM: _object_dict preserves JSON-like budgets and reserved values while rejecting scalar blobs.
# 函数用途: 支持 dict 或 JSON object 字符串；其他输入返回空字典。
def _object_dict(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return {str(key): item for key, item in value.items()}
    if isinstance(value, str) and value.strip().startswith("{"):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, dict):
            return {str(key): item for key, item in parsed.items()}
    return {}


# LLM: _capability_request_parameters keeps the spec dict readable under size guards.
# 函数用途: 返回工具参数摘要，供 Tool Catalog 展示。
def _capability_request_parameters() -> dict[str, str]:
    return {
        "problem": "必填；当前被什么能力缺口阻塞",
        "needed_capability": "能力名，例如 controlled_exec、network_api、playwright、skill:xxx",
        "capability_type": "shell/tool/skill/mcp/network/generic",
        "requested_tools": "希望父级授权的工具名列表",
        "requested_commands": "希望受控执行的命令名列表，例如 pwd、python3、pytest",
        "path_scope": "需要访问的目录或文件范围",
        "output_budget": "stdout/stderr/文件输出预算",
        "risk_level": "low/medium/high；高风险必须说明原因和替代方案",
    }


# LLM: _capability_request_parameter_details gives the model exact field semantics without growing the class.
# 函数用途: 返回详细参数说明，减少模型写 capability_request.json 这类假动作。
def _capability_request_parameter_details() -> dict[str, str]:
    return {
        "problem": "写清楚为什么现有工具不能继续；不要只写“需要工具”。",
        "needed_capability": "父级用它做路由检索；能具体就具体。",
        "capability_type": "shell 表示命令执行；tool 表示内置工具；skill 表示知识/流程；mcp/network 分别表示 MCP 或网络能力。",
        "requested_tools": "只列真正需要的工具；申请 shell 命令优先写 controlled_exec，不要申请裸 rm。",
        "requested_commands": "只列命令名，不写完整危险 shell；具体参数留给后续受控 exec grant。",
        "path_scope": "限定在任务目录、产物目录或父级允许的目录；不要写系统根目录。",
        "output_budget": "例如 {\"stdout_bytes\":65536,\"stderr_bytes\":32768}；避免巨大日志撑爆上下文。",
        "risk_level": "涉及删除、网络写入、大量输出或跨目录访问时至少 medium。",
    }


# LLM: _capability_ok formats machine-readable success output for the runner loop.
# 函数用途: 统一成功返回，方便测试和父级日志读取。
def _capability_ok(payload: dict[str, object]) -> ToolExecutionResult:
    return ToolExecutionResult(_TOOL_NAME, True, json.dumps(payload, ensure_ascii=False, sort_keys=True))


# LLM: _capability_error formats concise model-facing validation errors.
# 函数用途: 统一失败返回，让模型能自修参数而不是继续写假文件。
def _capability_error(message: str) -> ToolExecutionResult:
    return ToolExecutionResult(_TOOL_NAME, False, message)
