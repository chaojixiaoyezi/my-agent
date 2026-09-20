# LLM: 后台工具策略是无副作用的结构化计算；命令与会话工具复用统一工具组，owner/task 策略只收紧目录，不授予执行权限。
# 模块用途: 按唤醒事实和策略计算续跑可见工具，保持终端操作可续接；修改时联合后台运行、快照和工具回归。
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..subagents.role_templates import SHELL_SESSION_TOOLS, active_model_subagent_tools
from .models import SUBAGENT_LIFECYCLE_WAKE_REASONS

# 后台唤醒继续同一 Agent；本表提供默认目录，owner/task 策略在此基础上收紧。
_BACKGROUND_WORK_TOOLS = (
    "skill_search",
    "remember",
    "update_persona",
    "read_file",
    "list_files",
    "search_text",
    "write_file",
    "edit_file",
    *SHELL_SESSION_TOOLS,
    "watch_stream",
    "task_progress",
    "resolve_capability_requests",
    "cancel_subagents",
)

DEFAULT_BACKGROUND_ALLOWED_TOOLS = (
    "send_guidance",
    "create_subagents",
    *_BACKGROUND_WORK_TOOLS,
)

# 发现已经由生产方持久化；上报轮沿既有工具表增加主动投递，仍受 owner/task 和通道路由收紧。
AUDIT_FINDING_ALLOWED_TOOLS = (
    *DEFAULT_BACKGROUND_ALLOWED_TOOLS,
    "send_message",
)

# 唤醒原因选择上下文标签，实际能力仍由 owner/task 策略收紧。
SCHEDULED_BACKGROUND_ALLOWED_TOOLS = DEFAULT_BACKGROUND_ALLOWED_TOOLS

GOAL_BACKGROUND_ALLOWED_TOOLS = (
    *DEFAULT_BACKGROUND_ALLOWED_TOOLS,
    "get_goal",
    "update_goal",
)

SUBAGENT_INTEGRATION_ALLOWED_TOOLS = DEFAULT_BACKGROUND_ALLOWED_TOOLS
GOAL_SUBAGENTS_ACTIVE_ALLOWED_TOOLS = GOAL_BACKGROUND_ALLOWED_TOOLS
GOAL_SUBAGENTS_TERMINAL_ALLOWED_TOOLS = GOAL_BACKGROUND_ALLOWED_TOOLS

CONTROL_ACTION_DESCRIPTIONS = {
    "skill_search": "检索或读取当前轮已经授权的 Skill 正文。",
    "remember": "把当前 owner 的长期事实写入其隔离记忆。",
    "update_persona": "维护当前 owner 的 USER/AGENTS 人格约定；SOUL 仍需用户确认。",
    "send_guidance": "给正在运行的代理追加软提示。",
    "create_subagents": "创建并启动新的下级代理。",
    "read_file": "读取子代理产出的文件/产物,用于整合与验收。",
    "list_files": "查看子代理在工作区写了哪些产物。",
    "search_text": "在子代理产物里检索内容。",
    "write_file": "写最终交付物,或把子代理产物整合成成品。",
    "edit_file": "修订/整合已有交付文件。",
    "run_command": "运行 import/测试做交付前自检。",
    "process_session": "查询、有界等待或停止当前用户会话启动的后台命令。",
    "terminal_session": "在当前权限范围内启动、读取、输入或关闭交互终端。",
    "watch_stream": "读取有持久游标和覆盖账的数据源；Audit 模式按完整记录和 ack/source_ref 对账。",
    "task_progress": "更新任务清单进展。",
    "resolve_capability_requests": "批准或拒绝子代理的能力申请,让它能继续干。",
    "cancel_subagents": "打断并结束一个不应继续运行的直属子代理；它不负责轮询、推动或验收。",
    "send_message": "向当前 owner 的已绑定通道发送一条模型撰写的消息；证据型后台事件必须原样携带其 evidence_refs。",
    "get_goal": "读取当前 /goal 持续目标及其权威状态。",
    "update_goal": "仅在持续目标真正完成或确实阻塞时写入 complete/blocked 终态。",
}


# LLM: 输入只来自宿主配置、持久目标和唤醒事实；调用方须在执行前解析投递能力，不能以自然语言扩大权限。
# 类用途: 显式收集后台一轮工具目录计算所需的信息，不读取文件或运行 Agent。
@dataclass(frozen=True)
class BackgroundToolPolicyRequest:
    """Facts used to choose the background main-agent control tool profile."""

    reason: str = ""
    wake_signal: dict[str, Any] | None = None
    config: object | None = None
    owner_policy: object | None = None
    policy_snapshot: dict[str, Any] | None = None
    # These fields come only from the persisted goal/task/subagent records. They
    # never depend on model prose or natural-language intent classification.
    active_goal: bool = False
    goal_subagent_phase: str = ""
    # Resolved before model execution. False means the current owner route has
    # no provider receipt path, so send_message must not appear in schemas,
    # tool search, or final execution. None preserves standalone policy probes.
    proactive_delivery_available: bool | None = None


# LLM: 决策只描述本次目录及限制来源；工具执行器仍核对权限，结果不能充当额外授权或任务状态。
# 类用途: 保存可见工具、策略标签和剔除原因，供运行时与展示读取。
@dataclass(frozen=True)
class BackgroundToolPolicyDecision:
    """Final background tool list plus where each restriction came from."""

    allowed_tools: tuple[str, ...]
    profile: str
    sources: tuple[str, ...]
    removed_tools: tuple[str, ...] = ()

    # LLM: 展示投影保留版本和限制来源，必须与模型实际目录同源，不产生新的权威状态。
    # 函数用途: 把决策转换为可序列化数据，不写文件或修改原决策。
    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": "background-tool-policy.v1",
            "profile": self.profile,
            "allowed_tools": list(self.allowed_tools),
            "sources": list(self.sources),
            "removed_tools": list(self.removed_tools),
        }


# LLM: 列表调用方复用唯一决策入口，不能在此另算权限；需同步后台 prompt 与执行目录回归。
# 函数用途: 从完整策略结果取得工具名称列表，供只需要目录的调用方使用。
def background_allowed_tools(
    config: object | None = None,
    request: BackgroundToolPolicyRequest | None = None,
) -> list[str]:
    return list(background_tool_policy_decision(config, request=request).allowed_tools)


# LLM: 后台轮工具表必须先淘汰旧子代理控制面，再套 owner/task/delivery 减法；
# 存量配置不能把已注销工具重新带回模型 prompt。
# 函数用途: 根据唤醒类型和结构化策略计算后台主代理本轮可见工具。
def background_tool_policy_decision(
    config: object | None = None,
    request: BackgroundToolPolicyRequest | None = None,
) -> BackgroundToolPolicyDecision:
    if request is None:
        request = BackgroundToolPolicyRequest(config=config)
    config = request.config if request.config is not None else config
    configured = tool_names(getattr(config, "background_main_agent_allowed_tools", None))
    if configured:
        tools = configured
        profile = "configured"
        sources = ["agent_config.background_main_agent_allowed_tools"]
    else:
        profile, default_tools = _default_profile_for_request(request)
        tools = list(default_tools)
        sources = [f"default_profile:{profile}"]
    tools = active_model_subagent_tools(tools)
    tools, removed = _apply_policy_limits(tools, request)
    if removed:
        sources.append("owner_or_task_policy")
    if request.proactive_delivery_available is False and "send_message" in tools:
        tools = [tool for tool in tools if tool != "send_message"]
        removed = [*removed, "send_message"]
        sources.append("delivery_route_capability")
    return BackgroundToolPolicyDecision(
        allowed_tools=tuple(tools),
        profile=profile,
        sources=tuple(sources),
        removed_tools=tuple(removed),
    )


# LLM: 中文描述仅作模型上下文，不参与机器授权；可见名称必须来自同一轮结构化决策。
# 函数用途: 给后台提示词生成已允许工具的用途说明，不加载工具或执行动作。
def background_control_action_lines(
    config: object | None = None,
    request: BackgroundToolPolicyRequest | None = None,
) -> list[str]:
    lines: list[str] = []
    for name in background_allowed_tools(config, request=request):
        description = CONTROL_ACTION_DESCRIPTIONS.get(name)
        if description:
            lines.append(f"- {name}: {description}")
    return lines


# LLM: 这里只解析显式配置中的名称列表，不从自然语言推断工具；保持既有字符串和序列配置格式。
# 函数用途: 去除空名称并按原顺序去重，供任务和 owner 策略合并使用。
def tool_names(value: object) -> list[str]:
    if isinstance(value, str):
        raw_items = value.split(",")
    elif isinstance(value, (list, tuple)):
        raw_items = value
    else:
        return []
    return list(dict.fromkeys(str(item).strip() for item in raw_items if str(item).strip()))


# LLM: 默认 profile 仅由持久 Goal 与宿主 wake reason 选择；标签不强制工作流，需联合各类唤醒回归。
# 函数用途: 选择本轮默认工具目录和可解释标签，显式配置由上层入口优先处理。
def _default_profile_for_request(
    request: BackgroundToolPolicyRequest,
) -> tuple[str, tuple[str, ...]]:
    # Structured state selects context/profile labels, not a required workflow.
    reason = str(request.reason or "").strip().lower()
    if request.active_goal and (
        reason == "thread_goal_continue" or reason in SUBAGENT_LIFECYCLE_WAKE_REASONS
    ):
        if request.goal_subagent_phase == "subagents_active":
            return "thread_goal_subagents_active", GOAL_SUBAGENTS_ACTIVE_ALLOWED_TOOLS
        if request.goal_subagent_phase == "subagents_terminal":
            return "thread_goal_subagents_terminal", GOAL_SUBAGENTS_TERMINAL_ALLOWED_TOOLS
    if reason == "thread_goal_continue":
        return "thread_goal", GOAL_BACKGROUND_ALLOWED_TOOLS
    if reason == "audit_finding":
        return "audit_finding", AUDIT_FINDING_ALLOWED_TOOLS
    if _is_subagent_lifecycle_wake(request):
        return "subagent_integration", SUBAGENT_INTEGRATION_ALLOWED_TOOLS
    if _is_urgent_wake(request):
        return "urgent", DEFAULT_BACKGROUND_ALLOWED_TOOLS
    if _is_scheduled_progress(request):
        return "scheduled_progress", SCHEDULED_BACKGROUND_ALLOWED_TOOLS
    return "default", DEFAULT_BACKGROUND_ALLOWED_TOOLS


# LLM: 任务允许列表与 owner/task 禁用项只能收紧候选目录，不能加入新能力；保持原顺序与限制投影。
# 函数用途: 计算结构化策略过滤后的工具及剔除结果，不修改原配置。
def _apply_policy_limits(
    tools: list[str],
    request: BackgroundToolPolicyRequest,
) -> tuple[list[str], list[str]]:
    allowed = list(dict.fromkeys(tools))
    task_policy = request.policy_snapshot if isinstance(request.policy_snapshot, dict) else {}
    task_allowed = tool_names(task_policy.get("allowed_tools"))
    if task_allowed:
        allowed = [tool for tool in allowed if tool in set(task_allowed)]
    disabled = set(_disabled_tools_from_owner(request.owner_policy))
    disabled.update(tool_names(task_policy.get("disabled_tools")))
    filtered = [tool for tool in allowed if tool not in disabled]
    removed = [tool for tool in allowed if tool not in filtered]
    return filtered, removed


# LLM: owner policy 由调用方提前读取；缺省表示没有额外禁用项，不表示 Full Access 或越权许可。
# 函数用途: 取得并规范化 owner 的工具禁用列表，不再次访问持久配置。
def _disabled_tools_from_owner(owner_policy: object | None) -> list[str]:
    return tool_names(getattr(owner_policy, "disabled_tools", ()))


# LLM: 紧急程度只读取宿主 wake 字段和规范原因码，不解析用户或模型正文；需同步观察路由测试。
# 函数用途: 判断当前结构化唤醒是否使用紧急策略标签。
def _is_urgent_wake(request: BackgroundToolPolicyRequest) -> bool:
    wake = request.wake_signal if isinstance(request.wake_signal, dict) else {}
    urgency = str(wake.get("urgency") or "").strip().lower()
    reason = str(request.reason or wake.get("reason") or "").strip().lower()
    # 紧急标签来自宿主结构化事件，仅选择策略标签，不增加或取消 owner 权限。
    return urgency == "urgent" or reason in {
        "urgent_wake_signal",
        "wake_signal",
        "observation_requires_main_agent",
    }


# 定时类唤醒 reason 的权威名单:工具策略(_is_scheduled_progress)与提示词分支
#   (background_prompt → _scheduled_continuation_prompt)共用,防两处漂移。
SCHEDULED_WAKE_REASONS = frozenset(
    {"scheduled_progress_report", "progress_policy_due", "due_progress_policy", "scheduled_job_due"}
)


# LLM: 定时唤醒原因与运行时提示选择共用同一常量，不创建独立的调度身份或续跑入口。
# 函数用途: 判断本轮是否属于已有定时进度唤醒。
def _is_scheduled_progress(request: BackgroundToolPolicyRequest) -> bool:
    return str(request.reason or "").strip().lower() in SCHEDULED_WAKE_REASONS


# 子代理→主代理的"生命周期"推送:完成/卡住/失败(subagent_runner_finished)、申请能力
# (subagent_capability_request_open)、能力获批可续跑(subagent_capability_granted)。
# 这些唤醒叫回主代理是为了真整合收口/批能力,所以要给整合工具集(见 SUBAGENT_INTEGRATION_ALLOWED_TOOLS)。
# LLM: 子代理生命周期原因来自既有模型合同，不能用报告文字推断结束、失败或能力变更。
# 函数用途: 判断本轮是否由子代理结构化事件唤醒，供目录策略选择使用。
def _is_subagent_lifecycle_wake(request: BackgroundToolPolicyRequest) -> bool:
    wake = request.wake_signal if isinstance(request.wake_signal, dict) else {}
    reason = str(request.reason or wake.get("reason") or "").strip().lower()
    return reason in SUBAGENT_LIFECYCLE_WAKE_REASONS
