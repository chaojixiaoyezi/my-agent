# LLM: 只渲染 model_runtime_metrics.v1，不读取日志、成本文件或模型正文；每帧工作量与固定字段数相关。
# 模块用途: 在 Context 下沿原统计行显示 LLM 与决策约数（输入 token、成功/失败次数），按宽度简写，缺报保留未知，不显示价格。
from __future__ import annotations

from ...agent.conversation.model_metrics import public_model_metrics
from .tui_markdown import FormattedLine, display_width_text


# LLM: 计数仅做紧凑格式化，不改变 token 口径；完整精度仍保留在事件与用量账本中。
# 函数用途: 把累计消耗缩为 k/m 单位，避免窄终端被数字挤满。
def _tokens(value: int) -> str:
    for scale, suffix in ((1_000_000, "m"), (1_000, "k")):
        if value >= scale:
            return f"{value / scale:.1f}{suffix}"
    return str(value)


# LLM: 仅供显示的约数：已报输入按已报调用的平均值外推到全部决策调用（超时/失败的调用没有回报用量），
#   一次都没报时显示 ≈?，不能显示成 0；约数不写回账本，也不参与任何预算或调度判断。
# 函数用途: 生成统计行里的"决策 ≈N token · 成功 X · 失败 Y"片段。
def _decision_part(metrics: dict[str, object]) -> str:
    calls, reported = int(metrics["decision_call_count"]), int(metrics["decision_input_reported_calls"])
    value = metrics["decision_input_tokens"]
    approx = "?" if value is None or not reported else _tokens(round(int(value) * max(calls, reported) / reported))
    return f"决策 ≈{approx} token · 成功 {metrics['decision_success_count']} · 失败 {metrics['decision_failure_count']}"


# LLM: 决策约数是会话累计子集，输出留白但原账保留；不能把全部缺报显示成完整零值。
# 函数用途: 在同一统计行增加决策约数与成败次数；普通轮次/缓存/速度保留原口径，窄屏仍有界裁剪。
def render_model_metrics(value: object, width: int) -> tuple[FormattedLine, ...]:
    metrics = public_model_metrics(value)
    if not metrics:
        return ()
    narrow = width < 110
    rounds = metrics["model_rounds"]
    tools = metrics["tool_count"] if metrics["tool_count"] is not None else "—"
    cache = f"{metrics['cache_percent']:.0f}%" if metrics["cache_percent"] is not None else "—"
    total = int(metrics["input_tokens"]) + int(metrics["output_tokens"]) + int(metrics["estimated_tokens"])
    total_text = ("~" if metrics["estimated_tokens"] else "") + _tokens(total)
    if not metrics["totals_known"]:
        total_text = "未知"
    parts = [f"本次轮 {rounds}" if narrow else f"本次模型轮 {rounds}", f"当轮工具 {tools}", f"最近缓存 {cache}", f"会话累计 {total_text}"]
    if metrics.get("decision_call_count"):
        parts.append(_decision_part(metrics))
    if metrics["retry_count"]:
        parts.append(f"重试 {metrics['retry_count']}")
    if metrics["unreported_calls"]:
        parts.append(f"缺报 {metrics['unreported_calls']}")
    if metrics["pending"]:
        parts.append("本轮待结算")
    text = "  ▤ " + " · ".join(parts)
    extras = []
    if metrics["output_tps"] is not None:
        extras.append(f"最近输出 {metrics['output_tps']:.1f} tok/s")
    extras.append(f"入 {_tokens(int(metrics['input_tokens']))} / 出 {_tokens(int(metrics['output_tokens']))}")
    for extra in extras:
        if display_width_text(text + " · " + extra) <= width:
            text += " · " + extra
    if display_width_text(text) > max(1, width):
        while text and display_width_text(text + "…") > max(1, width):
            text = text[:-1]
        text += "…"
    return ((("class:tui-muted", text),),)
