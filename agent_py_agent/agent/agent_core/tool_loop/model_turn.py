# LLM: 模型采样与响应采纳只使用绑定后的窄操作；保留重试、计量、输入确认顺序，禁止持有Agent或重建历史。
# 模块用途: 接收一次模型请求的结果，按供应商事实确认补充消息；请求组装和Compact绑定原调用方，执行权仍由外层负责。
from __future__ import annotations

from collections.abc import Callable

from ...backends import ModelResponse
from ...settings.runtime_guard_config import RuntimeGuardPolicy
from ..provider_transient_auto_resume import run_with_provider_transient_auto_resume


# LLM: 只复用原瞬断恢复器；先计量响应，再处理输入。provider_error超限恢复原批次，preflight不消费，其他响应才确认。
# 函数用途: 执行模型采样并采纳结果，防止重试重复投递插话；所有写账由调用方绑定的原操作执行。
def sample_and_accept_model_response(
    request_response: Callable[[], tuple[str, ModelResponse]],
    *,
    retry_allowed: Callable[[], bool],
    account_response: Callable[[ModelResponse], object],
    restore_rejected_input: Callable[[], object],
    acknowledge_input: Callable[[], object],
    on_chunk: Callable[[str], object] | None = None,
    policy: RuntimeGuardPolicy | None = None,
) -> tuple[str, ModelResponse]:
    prompt, response = run_with_provider_transient_auto_resume(
        request_response,
        on_chunk=on_chunk,
        policy=policy,
        retry_guard=retry_allowed,
    )
    account_response(response)
    if str(getattr(response, "runtime_status", "") or "") == "context_overflow":
        if str(getattr(response, "runtime_source", "") or "") == "provider_error":
            restore_rejected_input()
    else:
        acknowledge_input()
    return prompt, response


# LLM: 固定同一组请求能力完成本次采样；重试上限在首次返回后读，先恢复原输入再回收，最终prompt/response必须配对。
# 函数用途: 组装并请求模型，遇供应商明确超限时按原策略压缩重试；仅有效业务响应消费本轮临时工具声明。
def request_model_response(
    *,
    build_prompt: Callable[[], str],
    generate_response: Callable[[str], ModelResponse],
    restore_rejected_input: Callable[[], object],
    recover_context: Callable[[str], bool],
    read_overflow_retry_limit: Callable[[], int],
    visible_loaded_tools: set[str] | None,
) -> tuple[str, ModelResponse]:
    prompt = build_prompt()
    response = generate_response(prompt)
    retry_max = read_overflow_retry_limit()
    retries = 0
    while (
        retries < retry_max
        and str(getattr(response, "runtime_status", "") or "") == "context_overflow"
        and str(getattr(response, "runtime_source", "") or "") == "provider_error"
    ):
        restore_rejected_input()
        if not recover_context(prompt):
            break
        retries += 1
        prompt = build_prompt()
        response = generate_response(prompt)
    if (
        visible_loaded_tools is not None
        and str(getattr(response, "runtime_status", "") or "") != "context_overflow"
    ):
        visible_loaded_tools.clear()
    return prompt, response
