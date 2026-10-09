# LLM: 模型采样与响应采纳只使用绑定后的窄操作；保留重试、计量、输入确认顺序，禁止持有Agent或重建历史。
# 一次尝试的 prompt/response/params 由调用方成组交回，本模块不解释 params 内容，也不 import 业务层。
# 模块用途: 接收一次模型请求的结果，按供应商事实确认补充消息；请求组装和Compact绑定原调用方，执行权仍由外层负责。
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ...backends import ModelResponse
from ...settings.runtime_guard_config import RuntimeGuardPolicy
from ..provider_transient_auto_resume import (
    ProviderTransientRetryCallbacks,
    run_with_provider_transient_auto_resume,
)


# LLM: 三者必须来自同一次请求尝试；params 是调用方的完整运行参数引用（首请求选模采用的那份或原对象），只原样传递。
# 类用途: 把实际发送的 prompt、收到的 response 与产出它的循环参数绑成一个不可变结果，供计量和确认显式使用。
@dataclass(frozen=True)
class ModelTurnRequest:
    prompt: str
    response: ModelResponse
    params: object


# LLM: 流在完成标记前被切断（backend 归一为 MODEL_STREAM_INCOMPLETE）时，只有"还没有对外
#   文本、也没有工具调用痕迹"才允许原样重发这次采样：不完整响应里 backend 已丢弃工具块、
#   整轮零执行，重发不重放副作用；已经有文本或工具痕迹的保持既有失败语义，避免重复内容与
#   重放。判据只读响应上的结构化字段，不解析正文；与 provider_transient_auto_resume 的
#   响应级重试（should_retry_result）配套，改动时同步核对三种协议的 incomplete 投影。
# 函数用途: 判定一条未完整响应是否可安全重试本次模型调用。
def _stream_incomplete_retryable(turn: ModelTurnRequest) -> bool:
    response = turn.response
    if str(getattr(response, "runtime_reason", "") or "") != "MODEL_STREAM_INCOMPLETE":
        return False
    if str(getattr(response, "runtime_source", "") or "") != "model_provider":
        return False
    if str(getattr(response, "text", "") or "").strip():
        return False
    if getattr(response, "truncated_tool_names", None):
        return False
    if getattr(response, "tool_use_blocks", None):
        return False
    return True


# LLM: 只复用原瞬断恢复器；先计量响应，再处理输入。provider_error超限恢复原批次，preflight不消费，其他响应才确认。
#   响应级重试（should_retry_result）只放行"零工具执行、无对外文本的流未完整结束"这一种
#   可原样重发的采样失败；已经有文本或工具痕迹的响应原样返回，走既有失败语义。
# 三个回调只收到最终成功那次尝试交回的 params；瞬断失败的尝试不计量、不恢复、不确认。
# 函数用途: 执行模型采样并采纳结果，防止重试重复投递插话；所有写账由调用方绑定的原操作执行。
def sample_and_accept_model_response(
    request_response: Callable[[], ModelTurnRequest],
    *,
    retry_allowed: Callable[[], bool],
    account_response: Callable[[object, ModelResponse], object],
    restore_rejected_input: Callable[[object], object],
    acknowledge_input: Callable[[object], object],
    on_chunk: Callable[[str], object] | None = None,
    policy: RuntimeGuardPolicy | None = None,
) -> ModelTurnRequest:
    turn = run_with_provider_transient_auto_resume(
        request_response,
        on_chunk=on_chunk,
        policy=policy,
        callbacks=ProviderTransientRetryCallbacks(
            retry_guard=retry_allowed,
            should_retry_result=_stream_incomplete_retryable,
        ),
    )
    account_response(turn.params, turn.response)
    if str(getattr(turn.response, "runtime_status", "") or "") == "context_overflow":
        if str(getattr(turn.response, "runtime_source", "") or "") == "provider_error":
            restore_rejected_input(turn.params)
    else:
        acknowledge_input(turn.params)
    return turn


# LLM: 固定同一组能力完成采样；工具加载集合由线程typed归档及循环持有，只增不减，不在响应边清空。
# 函数用途: 请求模型并按原超限策略压缩重试；保持后续主请求与Compact的工具前缀，最终prompt/response成对。
def request_model_response(
    *,
    build_prompt: Callable[[], str],
    generate_response: Callable[[str], ModelResponse],
    restore_rejected_input: Callable[[], object],
    recover_context: Callable[[str], bool],
    read_overflow_retry_limit: Callable[[], int],
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
    return prompt, response
