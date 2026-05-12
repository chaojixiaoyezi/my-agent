# LLM: Reserved tool-record guard keeps model-written fake tool transcripts out of control flow.
# 模块用途: 识别模型伪造的系统工具回执，避免把 `[tool-record]` 当成真实工具执行证据。

from __future__ import annotations

from ..backends import ModelResponse

_RESERVED_TOOL_RECORD_MARKERS = (
    "[tool-record",
    "[tool-output-record",
    "[/tool-call]",
)


# LLM: contains_reserved_tool_record detects system-only transcript markers in assistant text.
# 函数用途: 判断模型回复是否包含只能由系统写入的工具记录标记，用于拒绝伪造完成状态。
def contains_reserved_tool_record(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(marker in lowered for marker in _RESERVED_TOOL_RECORD_MARKERS)


# LLM: sanitize_reserved_tool_record_response prevents fake transcripts from entering the next prompt.
# 函数用途: 当回复里有系统保留标记时，用短说明替代原文，避免伪造 run id 或工具输出被下一轮模型继续引用。
def sanitize_reserved_tool_record_response(response: ModelResponse) -> ModelResponse:
    if not contains_reserved_tool_record(response.text):
        return response
    return ModelResponse(
        text=(
            "[assistant-response-omitted]\n"
            "模型回复包含系统保留的 tool-record/tool-output-record 标记，"
            "该回复正文不进入后续 live prompt；系统只会执行真实 [TOOL_CALL] 块。"
        ),
        backend=response.backend,
    )


# LLM: reserved_tool_record_repair_context gives the next model turn a concrete correction contract.
# 函数用途: 写入下一轮工具上下文，要求模型不要伪造工具结果，只能调用工具或基于真实回执收口。
def reserved_tool_record_repair_context() -> str:
    return (
        "[tool-system]\n"
        "上一轮模型回复包含系统保留的 `[tool-record]` / `[tool-output-record]` 标记。"
        "这些标记只能由工具循环在真实工具执行后写入，模型不能自行书写、复制或假装工具成功。"
        "请改用真实 `[TOOL_CALL]...[/TOOL_CALL]` 请求工具，或只基于已经存在的真实工具回执总结。"
    )


# LLM: reserved_tool_record_block_response is the deterministic closeout after repeated spoofing.
# 函数用途: 模型连续伪造工具记录且没有真实工具调用时，返回确定性阻断消息，避免假完成。
def reserved_tool_record_block_response(backend: str) -> ModelResponse:
    return ModelResponse(
        text=(
            "系统已阻止本轮结果：模型输出了系统保留的工具记录标记，"
            "但没有提供可执行的真实工具调用或可信的真实工具回执。"
            "当前不能把这次回复视为完成；请重新发起真实工具调用，"
            "或读取现有 task/subagent 状态后再汇报。"
        ),
        backend=backend,
    )
