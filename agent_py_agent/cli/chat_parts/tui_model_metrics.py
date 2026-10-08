# LLM: 只渲染 model_runtime_metrics.v1，不读取日志、成本文件或模型正文；每帧工作量与固定字段数相关。
# 模块用途: 在 Context 下沿显示一行模型统计：本轮模型轮、当轮工具、总缓存（两位小数）、累计会话、决策（开关）及成败个数、输出、速度；决策调用不计入累计，按宽度裁剪，不显示价格。
from __future__ import annotations

from ...agent.conversation.model_metrics import public_model_metrics, split_unsent_failures
from .tui_markdown import FormattedLine, display_width_text

# 决策开关的中文标签；键是 model_metrics.DECISION_DISPLAY_MODES 里的结构化值。
_DECISION_MODE_LABELS = {"off": "关闭", "observe": "观察", "apply": "实际"}


# LLM: 计数仅做紧凑格式化，不改变 token 口径；完整精度仍保留在事件与用量账本中。
# 函数用途: 把累计消耗缩为 k/m 单位，避免窄终端被数字挤满。
def _tokens(value: int) -> str:
    for scale, suffix in ((1_000_000, "m"), (1_000, "k")):
        if value >= scale:
            return f"{value / scale:.1f}{suffix}"
    return str(value)


# LLM: 仅供显示，不写回账本、不参与预算或调度。括号里的开关值来自快照的 decision_mode（settings.decision_settings_projection
#   的总标签），没有该字段（旧快照、本地模式）就不带括号。用户 2026-10-08 定：决策只看个数、不看 token 和缓存——
#   成功 / 失败（只算发出去之后失败或超时的）/ 未发出（一次 HTTP 尝试都没有，不计入失败，口径见 split_unsent_failures）；
#   决策调用的用量也不进总缓存/累计会话/输出（model_metrics._add_usage 已扣除）。
# 函数用途: 生成统计行里的"决策（观察） 成功 X · 失败 Y · 未发出 Z"片段；没有决策调用时只给"决策（关闭）"这样的开关状态。
def _decision_part(metrics: dict[str, object]) -> str:
    mode = metrics.get("decision_mode")
    head = "决策" + (f"（{_DECISION_MODE_LABELS[mode]}）" if mode in _DECISION_MODE_LABELS else "")
    if not metrics.get("decision_call_count"):
        return head
    failures, not_sent = split_unsent_failures(metrics["decision_failure_count"], metrics["decision_unfinished_calls"],
                                               metrics["decision_unknown_failures"])
    parts = [f"成功 {int(metrics['decision_success_count'])}", f"失败 {failures}"] + ([f"未发出 {not_sent}"] if not_sent else [])
    return head + " " + " · ".join(parts)


# LLM: 只显示会话累计命中率（账本累计 cache_read ÷ 累计输入，DeepSeek Harness 页脚同一口径），保留两位小数：用户拿
#   "累计会话 × 总缓存%"就能算出命中/未命中的量。最近一次的命中率压缩前后会掉到个位数，容易误判，不再显示；
#   账本里仍有 cache_percent 给 /status 等消费者。
# 函数用途: 生成统计行里的"总缓存 87.21%"片段；供应商一次都没回报缓存时显示"总缓存 —"。
def _cache_part(metrics: dict[str, object]) -> str:
    session = metrics.get("cache_percent_session")
    return f"总缓存 {session:.2f}%" if session is not None else "总缓存 —"


# LLM: 行的固定顺序（2026-10-08 用户定）：本轮模型轮 · 当轮工具 · 总缓存 · 累计会话 · 决策（开关）及其成败 · [重试] · 输出 · [速度]。
#   累计会话 = 供应商回报的累计输入（含缓存读），有本地估算补位时带 ~；输出单独一栏，不再混进累计里；
#   不再显示最近一次命中率、非决策调用的估算/未发出/缺报段和"本轮待结算"，这些事实仍在快照与账本里。
#   速度口径见 conversation.model_metrics._last_response_metrics（整次调用时长，不会虚高）。
# 函数用途: 在 Context 下沿渲染一行模型统计，窄屏仍有界裁剪。
def render_model_metrics(value: object, width: int) -> tuple[FormattedLine, ...]:
    metrics = public_model_metrics(value)
    if not metrics:
        return ()
    narrow = width < 110
    rounds = metrics["model_rounds"]
    tools = metrics["tool_count"] if metrics["tool_count"] is not None else "—"
    session_input = int(metrics["input_tokens"]) + int(metrics["estimated_tokens"])
    total_text = ("~" if metrics["estimated_tokens"] else "") + _tokens(session_input)
    if not metrics["totals_known"]:
        total_text = "未知"
    parts = [f"本轮 {rounds}" if narrow else f"本轮模型轮 {rounds}", f"当轮工具 {tools}", _cache_part(metrics),
             f"累计会话 {total_text}", _decision_part(metrics)]
    if metrics["retry_count"]:
        parts.append(f"重试 {metrics['retry_count']}")
    parts.append(f"输出 {_tokens(int(metrics['output_tokens']))}")
    text = "  ▤ " + " · ".join(parts)
    if metrics["output_tps"] is not None:
        extra = f"速度 {metrics['output_tps']:.1f} tok/s"
        if display_width_text(text + " · " + extra) <= width:
            text += " · " + extra
    if display_width_text(text) > max(1, width):
        while text and display_width_text(text + "…") > max(1, width):
            text = text[:-1]
        text += "…"
    return ((("class:tui-muted", text),),)
