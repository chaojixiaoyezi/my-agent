# LLM: 此处仅读取response_decision的现有计数与权威上限；不得改变预算、重置或收口决策。
# 模块用途: 在主子共用工具循环结束时把调用和预算事实带出，不写模型历史。
from __future__ import annotations

from dataclasses import dataclass

from ...backends.reply_diagnostics import public_user_reply_diagnostic
from .response_decision import _MAX_EMPTY_TEXT_REPAIR_COUNT


# LLM: 只携带循环已经裁决的预算对象和工具轮数，不拥有或更新计数。
# 类用途: 将同次循环收口事实打包，避免诊断入口堆叠位置参数。
@dataclass(frozen=True)
class ReplyLoopFacts:
    counters: object
    tool_rounds: int


# LLM: 状态与响应均是本次循环独占对象；最后调用从真实generate接缝读取，不能拿工具schema估算冒充tools数。
# 函数用途: 为最终运行结果附上只读诊断，不改正文、结束原因、用量或执行结果。
def attach_user_reply_diagnostic(params: object, response: object, facts: ReplyLoopFacts) -> None:
    if response is None:
        return
    state = getattr(params, "live_archive_state", None)
    response.user_reply_diagnostic = public_user_reply_diagnostic({
        "last_model_call": state.get("_last_model_call_diagnostic") if isinstance(state, dict) else None,
        "empty_text_repairs": getattr(facts.counters, "empty_text_repairs", None),
        "empty_text_repair_limit": _MAX_EMPTY_TEXT_REPAIR_COUNT,
        "tool_rounds": facts.tool_rounds,
    })
