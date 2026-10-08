# LLM: 只渲染 model_runtime_metrics.v1，不读取日志、成本文件或模型正文；每帧工作量与固定字段数相关。
# 模块用途: 在 Context 下沿原统计行显示 LLM 与决策约数（决策段与 LLM 段都把已报/估算/缺报分开标注，未发出单列），按宽度简写，缺报保留未知，不显示价格。
from __future__ import annotations

from ...agent.conversation.model_metrics import public_model_metrics, split_unsent_failures
from .tui_markdown import FormattedLine, display_width_text


# LLM: 计数仅做紧凑格式化，不改变 token 口径；完整精度仍保留在事件与用量账本中。
# 函数用途: 把累计消耗缩为 k/m 单位，避免窄终端被数字挤满。
def _tokens(value: int) -> str:
    for scale, suffix in ((1_000_000, "m"), (1_000, "k")):
        if value >= scale:
            return f"{value / scale:.1f}{suffix}"
    return str(value)


# LLM: 仅供显示，不写回账本、不参与预算或调度。token 两种来源分开标注：
#   已报 = 供应商回报的输入，按已报调用的平均值外推到全部成功调用（有没报的成功调用时带 ≈）；
#   估算 = 已发出后超时/失败的调用的发送前本地估算，标“未完成”。
#   次数互不重叠、可直接看：成功 / 失败（只算发出去之后失败或超时的）/ 未发出（一次 HTTP 尝试都没有，不计入失败，
#   口径见 split_unsent_failures）。真的一点数据都没有的只作为括号说明挂在所属次数上（成功却一次都没回报用量、
#   旧账失败分不清是否发出），不另起一个会与成功/失败重复计数的“缺报”数，免得用户把几个数加起来当总次数。
# 函数用途: 生成统计行里的“决策 已报 ≈N token · 估算 M token（未完成） · 成功 X（其中 a 次未回报用量） · 失败 Y（其中 b 次分不清是否发出） · 未发出 Z”片段。
def _decision_part(metrics: dict[str, object]) -> str:
    success, reported = int(metrics["decision_success_count"]), int(metrics["decision_input_reported_calls"])
    failures, not_sent = split_unsent_failures(metrics["decision_failure_count"], metrics["decision_unfinished_calls"],
                                               metrics["decision_unknown_failures"])
    value, estimate = metrics["decision_input_tokens"], int(metrics["decision_estimated_tokens"])
    parts = []
    if value is not None and reported:
        approx = "≈" if success > reported else ""
        parts.append(f"已报 {approx}{_tokens(round(int(value) * max(success, reported) / reported))} token")
    if estimate:
        parts.append(f"估算 {_tokens(estimate)} token（未完成）")
    unreported, unknown = (0 if reported else success), int(metrics["decision_unknown_failures"])
    parts.append(f"成功 {success}" + (f"（其中 {unreported} 次未回报用量）" if unreported else ""))
    parts.append(f"失败 {failures}" + (f"（其中 {unknown} 次分不清是否发出）" if unknown else ""))
    return "决策 " + " · ".join(parts + ([f"未发出 {not_sent}"] if not_sent else []))


# LLM: 非决策调用（主模型、辅助调用）与决策段同一口径（split_unsent_failures）：发出去之后超时/失败的显示发送前估算并标
#   “未完成”；一次 HTTP 尝试都没有的单列“未发出”；旧账分不清是否发出的才算“缺报”。成功但供应商没回报用量的已按本地估算
#   计入会话累计（带 ~），不再算缺报。数字 = 全部用途的原始累计 − 决策分区，同一次决策调用只在决策段讲一次。
# 函数用途: 生成统计行里非决策调用的“LLM 估算 N token（未完成） · 未发出 M · 缺报 K”片段，都没有时返回空串。
def _llm_part(metrics: dict[str, object]) -> str:
    other = {key: max(0, int(metrics.get(key) or 0) - int(metrics.get(decision_key) or 0)) for key, decision_key in (
        ("failure_count", "decision_failure_count"), ("unfinished_calls", "decision_unfinished_calls"),
        ("unknown_failures", "decision_unknown_failures"), ("unfinished_tokens", "decision_estimated_tokens"))}
    _failed, not_sent = split_unsent_failures(other["failure_count"], other["unfinished_calls"], other["unknown_failures"])
    parts = [f"估算 {_tokens(other['unfinished_tokens'])} token（未完成）"] if other["unfinished_tokens"] else []
    parts += ([f"未发出 {not_sent}"] if not_sent else []) + ([f"缺报 {other['unknown_failures']}"] if other["unknown_failures"] else [])
    return "LLM " + " · ".join(parts) if parts else ""


# LLM: 缓存一栏先给会话累计（账本累计 cache_read ÷ 输入，DeepSeek Harness 页脚同一口径），再给最近一次；
#   只有最近一次时沿旧文案"最近缓存"。压缩前后两次调用会把最近值拉到个位数，累计值才反映前缀复用是否生效。
# 函数用途: 生成统计行里的缓存片段：“缓存 会话 97% · 最近 85%”或“最近缓存 85%”。
def _cache_part(metrics: dict[str, object]) -> str:
    recent = f"{metrics['cache_percent']:.0f}%" if metrics["cache_percent"] is not None else "—"
    session = metrics.get("cache_percent_session")
    if session is None:
        return f"最近缓存 {recent}"
    return f"缓存 会话 {session:.0f}% · 最近 {recent}"


# LLM: 决策约数是会话累计子集，输出留白但原账保留；不能把全部缺报显示成完整零值。决策段与 LLM 段按同一口径各讲一次，
#   同一次调用不会在两段重复出现。
# 函数用途: 在同一统计行增加决策约数与成败次数、非决策调用的估算/未发出/缺报；普通轮次/缓存/速度保留原口径，窄屏仍有界裁剪。
def render_model_metrics(value: object, width: int) -> tuple[FormattedLine, ...]:
    metrics = public_model_metrics(value)
    if not metrics:
        return ()
    narrow = width < 110
    rounds = metrics["model_rounds"]
    tools = metrics["tool_count"] if metrics["tool_count"] is not None else "—"
    cache = _cache_part(metrics)
    total = int(metrics["input_tokens"]) + int(metrics["output_tokens"]) + int(metrics["estimated_tokens"])
    total_text = ("~" if metrics["estimated_tokens"] else "") + _tokens(total)
    if not metrics["totals_known"]:
        total_text = "未知"
    parts = [f"本次轮 {rounds}" if narrow else f"本次模型轮 {rounds}", f"当轮工具 {tools}", cache, f"会话累计 {total_text}"]
    if metrics.get("decision_call_count"):
        parts.append(_decision_part(metrics))
    if metrics["retry_count"]:
        parts.append(f"重试 {metrics['retry_count']}")
    if llm := _llm_part(metrics):
        parts.append(llm)
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
