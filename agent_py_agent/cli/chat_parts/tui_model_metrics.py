# LLM: 只渲染 model_runtime_metrics.v1，不读取日志、成本文件或模型正文；每帧工作量与固定字段数相关。
# 模块用途: 在 Context 下显示独立统计条，按终端宽度简写，未知缓存不显示成零。
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


# LLM: 本次轮数来自逻辑调用，缓存和速度来自最近一次结算，累计包含本线程当前代理历次调用但不合并子代理。
# 函数用途: 渲染有界一行统计；宽屏多显示速度与输入输出，窄屏只留核心指标。
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
