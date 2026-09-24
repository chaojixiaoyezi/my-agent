# LLM: 收口只消费绑定后的请求、收口响应处理与原因读取操作；不持有Agent、回合参数或执行器，保持原因在生成和响应处理之后读取。
# 模块用途: 统一硬边界后的模型交接及未完成状态，避免重复生成逻辑混配原请求或过早读取副作用事实。
from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from ...backends import ModelResponse


# LLM: 原请求与响应成对返回；先调用原模型运输，再处理剩余工具请求，最后读取结构化原因。异常原样传播，不重试、不执行工具或记账。
# 函数用途: 生成一次停止调用工具后的交接回复，保留原内容和用量，只将运行状态标为未完成。
def generate_tool_loop_closeout(
    *,
    build_prompt: Callable[[], str],
    generate_response: Callable[[str], ModelResponse],
    prepare_handoff_response: Callable[[ModelResponse], ModelResponse],
    read_runtime_reason: Callable[[], str],
) -> tuple[str, ModelResponse]:
    prompt = build_prompt()
    response = generate_response(prompt)
    response = prepare_handoff_response(response)
    reason = read_runtime_reason()
    return prompt, replace(
        response,
        runtime_status="unfinished",
        runtime_reason=reason,
        runtime_source="tool_loop",
    )


# LLM: 只读原halt元组和错误合同；可重试报码不足以证明副作用已知，handler执行过且effect未知必须维持人工核对边界。
# 函数用途: 判断停止后的回复属于已知失败还是副作用未知；合同读取失败保留未知，不解析错误正文或模型话术。
def unknown_outcome_runtime_reason(halt: tuple | None) -> str:
    values = halt or ()
    reported_code = str(values[1] if len(values) > 1 else "").strip().upper()
    effect = str(values[2] if len(values) > 2 else "").strip().lower()
    handler_executed = bool(values[3] if len(values) > 3 else True)
    try:
        from ...contracts.error_taxonomy import error_contract

        contract = error_contract(reported_code) if reported_code else None
        known_retryable_failure = bool(
            contract is not None
            and contract.retryable
            and reported_code
            and (not handler_executed or effect != "unknown")
        )
    except Exception:
        known_retryable_failure = False
    return (
        "REPEATED_TOOL_FAILURE"
        if known_retryable_failure
        else "TOOL_OPERATION_OUTCOME_UNKNOWN"
    )
