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


# LLM: 结论只来自冻结历史与档案声明；unknown 和未声明不能冒充支持，详情只含模态名和固定原因码。
# 类用途: 表示一个模型候选对本次结构化输入模态是否适用，供主会话和子代理共用。
@dataclass(frozen=True)
class InputModalityDecision:
    status: Literal["applicable", "inapplicable", "unknown"]
    reason_code: str
    required_modalities: tuple[str, ...] = ()
    declared_modalities: tuple[str, ...] = ()
    missing_modalities: tuple[str, ...] = ()

    # LLM: 持久化提示不得包含媒体路径、正文或模型配置，只展开固定的小型事实集合。
    # 函数用途: 生成可写入建议回执的结构化模态判定。
    def as_dict(self) -> dict:
        return {
            "status": self.status, "reason_code": self.reason_code,
            "required_modalities": list(self.required_modalities),
            "declared_modalities": list(self.declared_modalities),
            "missing_modalities": list(self.missing_modalities),
        }


# LLM: 过滤结果保留逐候选不适用原因；没有适用候选只是一条保留提示，绝不授权宿主另挑模型。
# 类用途: 返回一次候选模态过滤的白名单和结构化说明。
@dataclass(frozen=True)
class InputModalityFilter:
    status: Literal["ready", "unknown"]
    reason_code: str
    required_modalities: tuple[str, ...] = ()
    applicable_profile_ids: tuple[str, ...] = ()
    inapplicable: dict[str, dict] = field(default_factory=dict)

    # LLM: 对外只暴露候选编号、固定原因码和模态集合，不复制请求或目录记录。
    # 函数用途: 生成 Decision 材料或保留回执使用的候选过滤提示。
    def as_dict(self) -> dict:
        return {
            "status": self.status, "reason_code": self.reason_code,
            "required_modalities": list(self.required_modalities),
            "applicable_profile_ids": list(self.applicable_profile_ids),
            "inapplicable": deepcopy(self.inapplicable),
        }


# LLM: 参数来自原 ToolLoopExecuteParams；额外三项必须由宿主准备，不能在这里读取 Goal、运行账或执行事实。
#   回合触发类型原样交给 PromptBuilder，只决定当前回合以用户任务还是宿主事件开头。
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
        turn_trigger=getattr(params, "turn_trigger", None),
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
    "InputModalityDecision", "InputModalityFilter", "ToolLoopRequestInput", "ToolLoopRequestProjection",
    "candidate_input_modality_decision", "filter_model_candidates_by_input_modality",
    "project_tool_loop_request", "tool_loop_prompt_request", "text_request_capacity_known",
    "compact_request_source_supported",
]


# LLM: 纯文字仍兼容未声明档案；只有结构化 image/video 才要求显式声明，缺历史事实保持原 unknown 语义。
# 函数用途: 用同一规则判断一个候选是否能接收冻结请求，不读取正文、文件、模型名或 MIME 猜测。
def candidate_input_modality_decision(prepared: object, declared_modalities: object) -> InputModalityDecision:
    required = _request_input_modalities(prepared)
    declared = tuple(dict.fromkeys(value for value in declared_modalities or ()
                                   if isinstance(value, str) and value in {"text", "image", "video"}))
    if required is None:
        return InputModalityDecision("unknown", "history_modality_unknown", declared_modalities=declared)
    missing = tuple(value for value in required if value not in declared)
    if not missing:
        return InputModalityDecision("applicable", "input_modalities_supported", required, declared)
    reason = "candidate_input_modalities_undeclared" if not declared else "candidate_input_modalities_missing"
    return InputModalityDecision("inapplicable", reason, required, declared, missing)


# LLM: 候选过滤只做白名单，不在多个适用模型间排序；全不满足时固定提示保留原模型，禁止静默换到任意项。
# 函数用途: 对一批候选复用单候选判定并返回可选编号及逐项不适用原因。
def filter_model_candidates_by_input_modality(prepared: object, candidate_modalities: dict) -> InputModalityFilter:
    applicable, rejected, required = [], {}, ()
    for profile_id, declared in candidate_modalities.items():
        decision = candidate_input_modality_decision(prepared, declared)
        if decision.status == "unknown":
            return InputModalityFilter("unknown", decision.reason_code)
        required = decision.required_modalities
        if decision.status == "applicable":
            applicable.append(profile_id)
        else:
            rejected[profile_id] = decision.as_dict()
    reason = "no_candidate_supports_input_modalities" if required and not applicable else "input_modalities_filtered"
    return InputModalityFilter("ready", reason, required, tuple(applicable), rejected)


# LLM: None 历史与显式空历史语义不同；两份结构化历史都必须可分类，未知块或跨模型推理信封使整体 unknown。
# 函数用途: 从冻结 provider/IR 历史汇总实际需要的 image/video 集合。
def _request_input_modalities(prepared: object) -> tuple[str, ...] | None:
    provider = getattr(prepared, "provider_history_messages", None)
    native = getattr(prepared, "tool_ir_history", None)
    if provider is None or native is None:
        return None
    provider_modalities = _provider_input_modalities(provider)
    native_modalities = _native_input_modalities(native)
    if provider_modalities is None or native_modalities is None:
        return None
    return tuple(sorted(set(provider_modalities) | set(native_modalities)))


# LLM: provider 历史沿 request_content 的 canonical 判定；只收顶层 user local_file 媒体，assistant/嵌套/未知块不外推。
# 函数用途: 分类 provider 消息中的结构化媒体模态，无法完整分类时返回 None。
def _provider_input_modalities(messages: object) -> tuple[str, ...] | None:
    from ..backends.request_content import classify_nontext_content, is_local_media_block

    classes = classify_nontext_content(messages, allow_reasoning=False)
    if not isinstance(messages, (list, tuple)) or classes.unknown:
        return None
    blocks = (block for row in messages if row.get("role") == "user" and isinstance(row.get("content"), (list, tuple))
              for block in row["content"] if is_local_media_block(block))
    return tuple(sorted({str(block["type"]) for block in blocks}))


# LLM: IR 只承认仓库已有 typed 容器；未知对象、坏媒体引用和跨模型推理都保持 unknown，不经 adapter 丢弃。
# 函数用途: 分类 canonical IR 中的媒体模态。
def _native_input_modalities(items: object) -> tuple[str, ...] | None:
    if not isinstance(items, (list, tuple)):
        return None
    result = []
    for item in items:
        modalities = _ir_item_input_modalities(item)
        if modalities is None:
            return None
        result.extend(modalities)
    return tuple(sorted(set(result)))


# LLM: UserTurn 媒体是唯一新增模态事实；其余 IR 类型沿原跨模型文字可移植规则，不能把 assistant 媒体当用户输入能力。
# 函数用途: 分类一个 IR 项，返回需要的媒体模态或 unknown。
def _ir_item_input_modalities(item: object) -> tuple[str, ...] | None:
    from ..backends.request_content import text_content_supported
    from ..backends.tool_ir import (
        AssistantTurn,
        CompactionSummary,
        RuntimeFactsTurn,
        ToolResult,
        UserTurn,
    )

    if isinstance(item, UserTurn):
        values = tuple(_media_ref_input_modality(ref) for ref in item.media)
        return None if any(value is None for value in values) else tuple(sorted(set(values)))
    if isinstance(item, AssistantTurn):
        return () if text_content_supported(item.content_blocks, allow_reasoning=False) else None
    if isinstance(item, (list, tuple)):
        return () if all(isinstance(result, ToolResult) for result in item) else None
    return () if isinstance(item, (CompactionSummary, RuntimeFactsTurn, ToolResult)) else None


# LLM: 已验证附件引用必须同时有内容哈希和明确 image/video MIME 前缀；路径和文件名不参与机器判断。
# 函数用途: 从一条 canonical input_media 引用取得模态，格式不完整时返回 None。
def _media_ref_input_modality(ref: object) -> str | None:
    if not isinstance(ref, dict) or not isinstance(ref.get("sha256"), str):
        return None
    media_type = ref.get("media_type")
    kind = media_type.split("/", 1)[0] if isinstance(media_type, str) else ""
    return kind if kind in {"image", "video"} else None


# LLM: 适配前检查原IR/历史，未知媒体不假报已量；同模型保留文字推理，跨模型须allow_reasoning=False，不阻止普通生成。
# 函数用途: 判断现有文本计量可覆盖一次请求的全部内容，媒体引用字节数不当作视觉token。
def text_request_capacity_known(prepared: object, *, allow_reasoning: bool = True) -> bool:
    from ..backends.request_content import text_messages_supported

    if not text_messages_supported(getattr(prepared, "provider_history_messages", None) or (), allow_reasoning=allow_reasoning):
        return False
    return all(_text_ir_item_supported(item, allow_reasoning=allow_reasoning)
               for item in getattr(prepared, "tool_ir_history", None) or ())


# LLM: 回答"能否进入压缩链"而不是"能否计量"：unknown 非文本块与 text_request_capacity_known 一样拒绝；
# 已知媒体块（原生历史里的 local_file 引用、UserTurn 的 media refs）在媒体策略不为 off 时放行，由压缩链按策略投影或随图摘要。
# 函数用途: 供 preflight 与恢复宿主判断当前请求的历史能不能压缩，媒体不再让压缩链整体不可用。
def compact_request_source_supported(prepared: object, *, media_policy: str, allow_reasoning: bool = True) -> bool:
    from ..backends.request_content import compact_source_supported

    if not compact_source_supported(getattr(prepared, "provider_history_messages", None) or (), media_policy=media_policy,
                                    allow_reasoning=allow_reasoning):
        return False
    return all(_compact_ir_item_supported(item, media_policy=media_policy, allow_reasoning=allow_reasoning)
               for item in getattr(prepared, "tool_ir_history", None) or ())


# 函数用途: 单个 IR 项的压缩链放行规则；只有带媒体的 UserTurn 与文字计量规则不同。
def _compact_ir_item_supported(item: object, *, media_policy: str, allow_reasoning: bool) -> bool:
    from ..backends.tool_ir import UserTurn

    if isinstance(item, UserTurn):
        return not item.media or media_policy != "off"
    return _text_ir_item_supported(item, allow_reasoning=allow_reasoning)


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
