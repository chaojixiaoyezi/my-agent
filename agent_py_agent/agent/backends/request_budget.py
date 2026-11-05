"""请求局部的纯预算合同；不读取宿主、配置或供应商元数据。"""
from dataclasses import dataclass

# 工程余量，不是供应商计数误差或未来工具输出的上界。
REQUEST_INPUT_SAFETY_TOKENS = 2048
# 压缩请求固定尾缀与协议包装的默认预留 token 余量。
COMPACT_SUFFIX_ALLOWANCE_TOKENS = 4096


@dataclass(frozen=True)
class RequestTokenBudget:
    input_tokens: int
    output_tokens: int
    context_window_tokens: int
    safety_tokens: int
    shared_window: bool
    server_managed_output: bool = False


class RequestBudgetExceeded(ValueError):
    error_code = "REQUEST_OUTPUT_BUDGET_UNAVAILABLE"

    # LLM: 仅保存规划失败的标量，不读取输入正文、不发送请求或修改后端。
    # 函数用途: 把无容量与非法配置分开交给宿主原恢复路径。
    def __init__(self, *, reason: str, input_tokens: int, output_ceiling: int, context_window_tokens: int):
        self.reason = reason
        self.input_tokens = input_tokens
        self.output_ceiling = output_ceiling
        self.context_window_tokens = context_window_tokens
        super().__init__(f"{self.error_code}: {reason} (input={input_tokens}, output={output_ceiling}, window={context_window_tokens})")


# LLM: 纯请求规划；只校验冻结的标量输入，不推断供应商计数或触发 I/O。
# 函数用途: 按已知共享窗口分配输出，完整摘要预留不足时明确拒绝。
def resolve_request_token_budget(*, input_tokens: int, context_window_tokens: int,
                                 output_ceiling: int, shared_window: bool,
                                 server_managed_output: bool = False, require_full_output: bool = False,
                                 safety_tokens: int = REQUEST_INPUT_SAFETY_TOKENS) -> RequestTokenBudget:
    facts = dict(input_tokens=input_tokens, output_ceiling=output_ceiling,
                 context_window_tokens=context_window_tokens)
    if (any(type(value) is not int for value in (input_tokens, context_window_tokens, output_ceiling, safety_tokens))
            or input_tokens < 0 or output_ceiling <= 0 or safety_tokens < 0):
        raise RequestBudgetExceeded(reason="invalid_budget", **facts)
    output = output_ceiling
    if shared_window:
        if context_window_tokens <= safety_tokens or (
                require_full_output and output_ceiling + safety_tokens > context_window_tokens):
            raise RequestBudgetExceeded(reason="invalid_geometry", **facts)
        capacity = context_window_tokens - input_tokens - safety_tokens
        if capacity <= 0 or (require_full_output and capacity < output_ceiling):
            raise RequestBudgetExceeded(reason="input_overflow", **facts)
        output = min(output_ceiling, capacity)
    return RequestTokenBudget(input_tokens, output, context_window_tokens,
                              safety_tokens, shared_window, server_managed_output)


# LLM: 纯水位运算；未知窗口保留零，正窗口不可能容纳摘要时不得伪造一 token。
# 函数用途: 把百分比、绝对上限和共享容量约束合成一条触发线。
def compact_trigger_limit(*, window: int, percent: int, absolute_cap: int,
                          compact_reserve: int, shared_window: bool,
                          safety_tokens: int = REQUEST_INPUT_SAFETY_TOKENS,
                          suffix_allowance: int = COMPACT_SUFFIX_ALLOWANCE_TOKENS) -> int:
    if window <= 0:
        return 0
    if (compact_reserve <= 0 or safety_tokens < 0 or suffix_allowance < 0
            or window - compact_reserve - safety_tokens <= 0):
        raise RequestBudgetExceeded(reason="invalid_geometry", input_tokens=0,
                                    output_ceiling=compact_reserve, context_window_tokens=window)
    trigger = window * percent // 100
    if absolute_cap > 0:
        trigger = min(trigger, absolute_cap)
    if shared_window:
        capacity = window - compact_reserve - safety_tokens - suffix_allowance
        if capacity <= 0:
            raise RequestBudgetExceeded(reason="invalid_geometry", input_tokens=suffix_allowance,
                                        output_ceiling=compact_reserve, context_window_tokens=window)
        trigger = min(trigger, capacity)
    return trigger
