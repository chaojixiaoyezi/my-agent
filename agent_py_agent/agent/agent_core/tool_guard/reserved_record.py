
from __future__ import annotations

from ...backends import ModelResponse

_RESERVED_TOOL_RECORD_MARKERS = (
    "[tool-record",
    "[tool-output-record",
    "[/tool-call]",
)


def contains_reserved_tool_record(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(marker in lowered for marker in _RESERVED_TOOL_RECORD_MARKERS)


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


def reserved_tool_record_repair_context() -> str:
    return (
        "[tool-system]\n"
        "上一轮模型回复包含系统保留的 `[tool-record]` / `[tool-output-record]` 标记。"
        "这些标记只能由工具循环在真实工具执行后写入，模型不能自行书写、复制或假装工具成功。"
        "请改用真实 `[TOOL_CALL]...[/TOOL_CALL]` 请求工具，或只基于已经存在的真实工具回执总结。"
    )


def reserved_tool_record_block_response(backend: str) -> ModelResponse:
    return ModelResponse(
        text=(
            "系统已阻止本轮结果：模型输出了系统保留的工具记录标记，"
            "但没有提供可执行的真实工具调用或可信的真实工具回执。"
            "当前不能把这次回复视为完成；请重新发起真实工具调用，"
            "或读取现有 task/subagent 状态后再汇报。"
        ),
        backend=backend,
        runtime_status="blocked",
        runtime_reason="RESERVED_TOOL_RECORD",
    )
