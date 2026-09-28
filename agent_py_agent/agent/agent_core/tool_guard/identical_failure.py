# LLM: 主代理同一回合“原样重复同一个失败调用”的硬上限只看宿主结构化事实：工具名、规范化参数摘要
#   （ToolCall.args_hash，即 tool_arguments_hash）和归一化 error_code，不读模型正文或错误文案。
#   连续段只挂在当前回合的易失参数上：任一成功、换参数、换工具或换错误码都从头计数，新回合天然清零。
#   阈值由调用方传入（复用 repeated_failure_halt_threshold，≤0 关闭），本模块不读配置、不写状态文件。
#   改动时联查 _tool_loop_service 的收口分支、turn_end.CONTINUABLE_REASONS（本原因不得加入）和
#   test_identical_tool_failure_halt。
# 模块用途: 计算“同一调用、同一失败”的连续次数，并给出结束当前回合所需的结构化原因与宿主说明。
from __future__ import annotations

from dataclasses import dataclass, replace

# 当前回合因同一调用同一失败连续达上限而结束时写入 runtime_reason 的结构化原因码；不属于可自动续跑原因。
REPEATED_IDENTICAL_TOOL_FAILURE = "REPEATED_IDENTICAL_TOOL_FAILURE"


# LLM: 三个身份字段全部来自宿主 ToolCall/ToolResult；count 是当前回合内的连续次数，不跨回合持久化。
# 类用途: 表示当前回合里“同一个调用拿到同一个失败”的连续段，供硬上限判定和收口说明使用。
@dataclass(frozen=True)
class IdenticalFailureStreak:
    tool_name: str
    args_hash: str
    error_code: str
    count: int = 1

    # LLM: 只投影结构化字段，供测试与诊断读取；不含参数值或工具输出。
    # 函数用途: 把连续段转成普通字典。
    def to_dict(self) -> dict[str, object]:
        return {"tool_name": self.tool_name, "args_hash": self.args_hash,
                "error_code": self.error_code, "count": self.count}


# LLM: 只处理失败结果（成功由调用方直接清零为 None）；三元组与上一段完全相同才累加，否则从 1 重新计数。
#   调用顺序必须是工具结果的原记录顺序，这样“中间夹了别的调用”自然打断连续段。
# 函数用途: 按一次失败的工具结果更新连续段，不修改传入对象。
def next_identical_failure_streak(
    previous: IdenticalFailureStreak | None, tool_name: str, args_hash: str, error_code: str,
) -> IdenticalFailureStreak:
    if (isinstance(previous, IdenticalFailureStreak)
            and (previous.tool_name, previous.args_hash, previous.error_code) == (tool_name, args_hash, error_code)):
        return replace(previous, count=previous.count + 1)
    return IdenticalFailureStreak(tool_name, args_hash, error_code)


# LLM: 阈值 ≤0 表示关闭；达到阈值即命中，与子代理授权阶段收口的比较口径一致。
# 函数用途: 判断连续段是否已达到硬上限。
def identical_failure_limit_reached(streak: IdenticalFailureStreak | None, threshold: int) -> bool:
    return threshold > 0 and streak is not None and streak.count >= threshold


# LLM: 文案只由结构化字段拼出，给用户和下一轮模型看；状态与续跑判定由 runtime_reason 承担，不能反向解析本文案。
# 函数用途: 生成当前回合被硬上限结束时的宿主说明，替代一次额外的收口模型调用。
def identical_failure_closeout_text(streak: IdenticalFailureStreak) -> str:
    return (
        f"系统已结束本轮：工具 {streak.tool_name} 以相同参数连续 {streak.count} 次返回同一错误"
        f"（{streak.error_code or '无错误码'}），原样重试不会有进展。"
        "任务和持续目标都保留，本轮之后不会自动续跑；请调整需求或发送新消息后再继续。"
    )


__all__ = [
    "REPEATED_IDENTICAL_TOOL_FAILURE",
    "IdenticalFailureStreak",
    "identical_failure_closeout_text",
    "identical_failure_limit_reached",
    "next_identical_failure_streak",
]
