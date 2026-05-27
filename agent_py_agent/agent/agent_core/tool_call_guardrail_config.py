# LLM: Tool-call guardrail config keeps repeat/no-progress budgets outside the ledger logic.
# 模块用途: 读取重复失败阈值、终止开关，并生成给模型换路的软提示。

from __future__ import annotations

from ..settings.runtime_guard_config import runtime_guard_bool, runtime_guard_int

DEFAULT_REPEAT_FAIL_THRESHOLD = 10
UNLIMITED_REPEAT_FAIL_HINTS = (50, 100)


# LLM: repeat_fail_threshold reads the model-facing repeat warning budget from task attrs or YAML.
# 函数用途: 优先读取单任务覆盖值，否则读取 runtime_guard_config.yaml 的重复失败阈值。
def repeat_fail_threshold(params: object) -> int:
    attrs = getattr(params, "task_attributes", None)
    if isinstance(attrs, dict):
        parsed = _int_value(attrs.get("repeat_fail_threshold"))
        if parsed >= 0:
            return parsed
    return runtime_guard_int("repeat_fail_threshold", DEFAULT_REPEAT_FAIL_THRESHOLD)


# LLM: no_progress_action_block_after derives the action-level intercept point from one user knob.
# 函数用途: 用 repeat_fail_threshold 的 3 倍作为同路无进展动作拦截点，0 表示不拦截。
def no_progress_action_block_after(params: object) -> int:
    threshold = repeat_fail_threshold(params)
    return threshold * 3 if threshold > 0 else 0


# LLM: terminal_block_enabled keeps terminal task blocking opt-in instead of default hard-stop.
# 函数用途: 读取是否允许重复失败门把整个任务置为 blocked，默认只拦截重复动作。
def terminal_block_enabled(params: object) -> bool:
    attrs = getattr(params, "task_attributes", None)
    if isinstance(attrs, dict) and "terminal_block_enabled" in attrs:
        value = attrs.get("terminal_block_enabled")
        if isinstance(value, bool):
            return value
        text = str(value or "").strip().lower()
        return text in {"1", "true", "yes", "on"}
    else:
        return runtime_guard_bool("terminal_block_enabled", False)


# LLM: repeat_failure_hint gives the model a rework hint before intercepting repeated failures.
# 函数用途: 在重复失败达到阈值和二倍阈值时返回中文换路提示。
def repeat_failure_hint(params: object, signature: object, count: int) -> str:
    threshold = repeat_fail_threshold(params)
    if threshold <= 0:
        if count in UNLIMITED_REPEAT_FAIL_HINTS:
            return _hint_text(str(getattr(signature, "tool_name", "") or ""), count, unlimited=True)
        return ""
    if count in {threshold, threshold * 2}:
        return _hint_text(str(getattr(signature, "tool_name", "") or ""), count, unlimited=False)
    return ""


# LLM: no_progress_hint warns the model when repeated read-only results stop changing.
# 函数用途: 在同参数无进展达到阈值和二倍阈值时返回中文换路提示。
def no_progress_hint(params: object, signature: object, count: int) -> str:
    threshold = repeat_fail_threshold(params)
    if threshold <= 0:
        if count in UNLIMITED_REPEAT_FAIL_HINTS:
            return _no_progress_hint_text(str(getattr(signature, "tool_name", "") or ""), count, unlimited=True)
        return ""
    if count in {threshold, threshold * 2}:
        return _no_progress_hint_text(str(getattr(signature, "tool_name", "") or ""), count, unlimited=False)
    return ""


# LLM: _hint_text centralizes repeated-failure wording so config changes do not fork prompts.
# 函数用途: 生成同工具同参数重复失败时给模型看的中文返工提示。
def _hint_text(tool_name: str, count: int, *, unlimited: bool) -> str:
    prefix = "工具循环软提示" if unlimited else "工具循环返工提示"
    limit_text = "当前配置为 0（无限，不会按次数拦截）" if unlimited else "后续仍原样重复到 3 倍阈值时，本次相同调用会被拦截但任务不会被杀掉"
    return (
        f"[{prefix}] {tool_name} 已经以同一参数出现同类失败 {count} 次。"
        "请不要继续原样撞这条路径；先检查最新错误，换关键词、换参数、换工具或换数据来源。"
        f"{limit_text}。"
    )


# LLM: _no_progress_hint_text centralizes no-progress wording for repeated identical results.
# 函数用途: 生成同工具同参数无进展时给模型看的中文返工提示。
def _no_progress_hint_text(tool_name: str, count: int, *, unlimited: bool) -> str:
    prefix = "工具无进展软提示" if unlimited else "工具无进展返工提示"
    limit_text = "当前配置为 0（无限，不会按次数拦截）" if unlimited else "后续仍原样重复到 3 倍阈值时，本次相同调用会被拦截但任务不会被杀掉"
    return (
        f"[{prefix}] {tool_name} 已经以同一参数返回相同结果 {count} 次。"
        "请使用已有结果，或者换查询条件、换工具、换数据来源；如果这是分页/游标任务，请确保工具结果体现 cursor/offset/rows 等真实进展。"
        f"{limit_text}。"
    )


# LLM: _int_value is a tiny tolerant parser for task-level runtime guard overrides.
# 函数用途: 将任务属性里的阈值转换成整数，非法值返回 -1 表示忽略覆盖。
def _int_value(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1
