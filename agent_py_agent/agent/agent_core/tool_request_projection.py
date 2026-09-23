# LLM: 只投影宿主已冻结的请求材料；不接收 agent，不读取文件或刷新状态，不做授权、探测、计数或发送。
# 模块用途: 让真实工具轮与创建前容量检查共享原 PromptBuilder、原 IR 转换及精确原生 schema。
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Literal

from ..backends.tool_protocol_adapter import tools_for_choice
from ..prompting_parts.builder import (
    PromptBuildRequest,
    PromptRenderInput,
    ToolSections,
    render_prepared_prompt,
)
from ..tooling.runtime_contracts import ToolChoice, ToolProtocolSnapshot
from .native_tool_protocol import native_tool_use_active
from .tool_ir_history import (
    project_native_prompt_history,
    project_native_provider_messages,
)

if TYPE_CHECKING:
    from ._runtime_params import ToolLoopExecuteParams


# LLM: None 表示未知，空元组/空字符串表示宿主明确知道为空；快照内容从原运行事实复制，不生成新身份或能力。
# 类用途: 保存一次请求的全部冻结输入，缺任一原生历史或 schema 字段时不会把不完整输入误报为可容纳。
@dataclass(frozen=True)
class ToolLoopRequestInput:
    prompt_input: PromptRenderInput | None = field(default=None, repr=False)
    system_instruction: str | None = field(default=None, repr=False)
    tool_protocol_snapshot: ToolProtocolSnapshot | None = None
    tool_choice: ToolChoice | None = None
    native_tools: tuple[dict[str, Any], ...] | None = field(default=None, repr=False)
    tool_ir_history: tuple[Any, ...] | None = field(default=None, repr=False)
    provider_history_messages: tuple[dict[str, Any], ...] | None = field(default=None, repr=False)
    tool_context: tuple[str, ...] | None = field(default=None, repr=False)
    forwarded_guidance: frozenset[str] | None = field(default=None, repr=False)
    conversation_state: str | None = field(default=None, repr=False)

    # LLM: 借用的 IR/schema/message 容器必须在边界复制；纯投影和调用方之后的修改不能反写彼此的材料。
    # 函数用途: 固定本次输入容器，保持原 typed IR 和 schema，不建立持久快照或第二注册表。
    def __post_init__(self) -> None:
        for name in ("native_tools", "tool_ir_history", "provider_history_messages", "tool_context"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, tuple(deepcopy(value)))
        if self.forwarded_guidance is not None:
            object.__setattr__(self, "forwarded_guidance", frozenset(self.forwarded_guidance))


# LLM: ready 仅证明冻结输入完整且按原格式渲染成功，不代表窗口足够、协议已探测或请求已被发送。
# 类用途: 返回精确请求材料或结构化缺口；调用方继续使用原计数、容量和授权合同。
@dataclass(frozen=True)
class ToolLoopRequestProjection:
    status: Literal["ready", "unknown"]
    missing_fields: tuple[str, ...] = ()
    prompt: str | None = field(default=None, repr=False)
    provider_prompt: str | None = field(default=None, repr=False)
    system_instruction: str | None = field(default=None, repr=False)
    messages: list[dict[str, Any]] | None = field(default=None, repr=False)
    tools: list[dict[str, Any]] | None = field(default=None, repr=False)
    tool_choice: ToolChoice | None = None


# LLM: 参数来自原 ToolLoopExecuteParams；额外三项必须由宿主准备，不能在这里读取 Goal、运行账或执行事实。
# 函数用途: 统一真实轮次和准备前投影使用的 PromptBuilder 输入，保留原系统、动态段、名卡及上下文作用域。
def tool_loop_prompt_request(
    params: ToolLoopExecuteParams,
    *,
    runtime_injections: list[str] | tuple[str, ...],
    workspace_context: str | None,
    execution_facts: str,
) -> PromptBuildRequest:
    return PromptBuildRequest(
        user_prompt=params.user_prompt,
        memories=params.memories,
        inject=list(runtime_injections),
        prompt_files=params.prompt_files,
        system_prompt_override=params.system_prompt_override,
        context_scope=params.context_scope,
        workspace_context_override=workspace_context,
        tools=ToolSections(
            selected_skill_ids=params.selected_skill_ids,
            required_skill_ids=params.required_skill_ids,
            tool_catalog_section=params.tool_catalog_section,
            tool_recommendations_section=params.tool_recommendations_section,
            tool_context=params.tool_context,
            execution_facts_section=execution_facts,
            native_tool_use=native_tool_use_active(params),
        ),
    )


# LLM: 先验证完整性，unknown 不渲染；只在副本追加状态与引导，复用原转换、清扫与已定 ToolChoice 的目录投影。
# 函数用途: 无读盘、刷新、探测或网络地还原完整模型请求；图片、推理及工具往返仍由原 IR 适配器保真。
def project_tool_loop_request(prepared: ToolLoopRequestInput) -> ToolLoopRequestProjection:
    missing = _missing_request_fields(prepared)
    if missing:
        return ToolLoopRequestProjection("unknown", missing_fields=missing)
    prompt_input = prepared.prompt_input
    assert prompt_input is not None
    prompt = render_prepared_prompt(prompt_input)
    if not prompt_input.native_tool_use:
        return ToolLoopRequestProjection(
            "ready", prompt=prompt, provider_prompt=prompt,
            system_instruction=prepared.system_instruction,
        )
    params = SimpleNamespace(tool_ir_history=list(deepcopy(prepared.tool_ir_history)))
    provider_prompt, history = project_native_prompt_history(
        params, prompt, conversation_state=prepared.conversation_state,
    )
    return ToolLoopRequestProjection(
        "ready", prompt=prompt, provider_prompt=provider_prompt,
        system_instruction=prepared.system_instruction,
        messages=project_native_provider_messages(
            history, prior_messages=prepared.provider_history_messages,
            tool_context=prepared.tool_context, forwarded_guidance=prepared.forwarded_guidance,
        ),
        tools=tools_for_choice(list(deepcopy(prepared.native_tools)), prepared.tool_choice) or None,
        tool_choice=prepared.tool_choice,
    )


# LLM: 不从正文、模型名或默认空集合推断缺失事实；显式空历史与未提供历史必须保持不同结果。
# 函数用途: 返回缺失或互相冲突的冻结字段名，方便宿主保留原模型并记录精确容量缺口。
def _missing_request_fields(prepared: ToolLoopRequestInput) -> tuple[str, ...]:
    fields = ["prompt_input", "system_instruction", "tool_protocol_snapshot"]
    prompt_input = prepared.prompt_input
    native = prompt_input is not None and prompt_input.native_tool_use
    if native:
        fields.extend((
            "native_tools", "tool_choice", "tool_ir_history", "provider_history_messages",
            "tool_context", "forwarded_guidance", "conversation_state",
        ))
    missing = [name for name in fields if getattr(prepared, name) is None]
    protocol = prepared.tool_protocol_snapshot
    if protocol is not None and (
        not isinstance(protocol, ToolProtocolSnapshot)
        or (protocol.source_protocol == "native") != native
    ):
        missing.append("tool_protocol_snapshot")
    return tuple(dict.fromkeys(missing))


__all__ = [
    "ToolLoopRequestInput", "ToolLoopRequestProjection",
    "project_tool_loop_request", "tool_loop_prompt_request", "text_request_capacity_known",
]


# LLM: 适配前检查原IR/历史，未知媒体不假报已量；同模型保留文字推理，跨模型须allow_reasoning=False，不阻止普通生成。
# 函数用途: 判断现有文本计量可覆盖一次请求的全部内容，媒体引用字节数不当作视觉token。
def text_request_capacity_known(prepared: object, *, allow_reasoning: bool = True) -> bool:
    from ..backends.request_content import text_messages_supported

    if not text_messages_supported(getattr(prepared, "provider_history_messages", None) or (), allow_reasoning=allow_reasoning):
        return False
    return all(_text_ir_item_supported(item, allow_reasoning=allow_reasoning)
               for item in getattr(prepared, "tool_ir_history", None) or ())


# LLM: 本层只认可原IR合同，工具结果以原模型投影呈现；未知类型不可先经adapter过滤，UserTurn媒体保留但不可按refs量容量。
# 函数用途: 分类单个原生历史项，隔离同模型思考与跨模型签名的不同要求。
def _text_ir_item_supported(item: object, *, allow_reasoning: bool) -> bool:
    from ..backends.request_content import text_content_supported
    from ..backends.tool_ir import (
        AssistantTurn,
        CompactionSummary,
        RuntimeFactsTurn,
        ToolResult,
        UserTurn,
    )

    if isinstance(item, AssistantTurn):
        return text_content_supported(item.content_blocks, allow_reasoning=allow_reasoning)
    if isinstance(item, UserTurn):
        return not item.media
    if isinstance(item, (list, tuple)):
        return all(isinstance(result, ToolResult) for result in item)
    return isinstance(item, (CompactionSummary, RuntimeFactsTurn, ToolResult))
