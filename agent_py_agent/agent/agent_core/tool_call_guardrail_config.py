# LLM: Tool-call guardrail config keeps repeat/no-progress budgets outside the ledger logic.
# 模块用途: 读取重复失败阈值、终止开关，并生成给模型换路的软提示。

from __future__ import annotations

from ..settings.config_io import load_simple_yaml
from .runtime_guard_config import DEFAULT_RUNTIME_GUARD_CONFIG_PATH

DEFAULT_REPEAT_FAIL_THRESHOLD = 10
UNLIMITED_REPEAT_FAIL_HINTS = (50, 100)


def repeat_fail_threshold(params: object) -> int:
    attrs = getattr(params, "task_attributes", None)
    if isinstance(attrs, dict):
        parsed = _int_value(attrs.get("repeat_fail_threshold"))
        if parsed >= 0:
            return parsed
    parsed = _int_value(runtime_guard_data().get("repeat_fail_threshold"))
    if parsed >= 0:
        return parsed
    return DEFAULT_REPEAT_FAIL_THRESHOLD


def no_progress_action_block_after(params: object) -> int:
    threshold = repeat_fail_threshold(params)
    return threshold * 3 if threshold > 0 else 0


def terminal_block_enabled(params: object) -> bool:
    attrs = getattr(params, "task_attributes", None)
    if isinstance(attrs, dict) and "terminal_block_enabled" in attrs:
        value = attrs.get("terminal_block_enabled")
    else:
        value = runtime_guard_data().get("terminal_block_enabled")
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "on"}


def repeat_failure_hint(params: object, signature: object, count: int) -> str:
    threshold = repeat_fail_threshold(params)
    if threshold <= 0:
        if count in UNLIMITED_REPEAT_FAIL_HINTS:
            return _hint_text(str(getattr(signature, "tool_name", "") or ""), count, unlimited=True)
        return ""
    if count in {threshold, threshold * 2}:
        return _hint_text(str(getattr(signature, "tool_name", "") or ""), count, unlimited=False)
    return ""


def no_progress_hint(params: object, signature: object, count: int) -> str:
    threshold = repeat_fail_threshold(params)
    if threshold <= 0:
        if count in UNLIMITED_REPEAT_FAIL_HINTS:
            return _no_progress_hint_text(str(getattr(signature, "tool_name", "") or ""), count, unlimited=True)
        return ""
    if count in {threshold, threshold * 2}:
        return _no_progress_hint_text(str(getattr(signature, "tool_name", "") or ""), count, unlimited=False)
    return ""


def _hint_text(tool_name: str, count: int, *, unlimited: bool) -> str:
    prefix = "工具循环软提示" if unlimited else "工具循环返工提示"
    limit_text = "当前配置为 0（无限，不会按次数拦截）" if unlimited else "后续仍原样重复到 3 倍阈值时，本次相同调用会被拦截但任务不会被杀掉"
    return (
        f"[{prefix}] {tool_name} 已经以同一参数出现同类失败 {count} 次。"
        "请不要继续原样撞这条路径；先检查最新错误，换关键词、换参数、换工具或换数据来源。"
        f"{limit_text}。"
    )


def _no_progress_hint_text(tool_name: str, count: int, *, unlimited: bool) -> str:
    prefix = "工具无进展软提示" if unlimited else "工具无进展返工提示"
    limit_text = "当前配置为 0（无限，不会按次数拦截）" if unlimited else "后续仍原样重复到 3 倍阈值时，本次相同调用会被拦截但任务不会被杀掉"
    return (
        f"[{prefix}] {tool_name} 已经以同一参数返回相同结果 {count} 次。"
        "请使用已有结果，或者换查询条件、换工具、换数据来源；如果这是分页/游标任务，请确保工具结果体现 cursor/offset/rows 等真实进展。"
        f"{limit_text}。"
    )


def runtime_guard_data() -> dict[str, object]:
    path = DEFAULT_RUNTIME_GUARD_CONFIG_PATH
    if not path.exists():
        return {}
    try:
        data = load_simple_yaml(path)
    except OSError:
        return {}
    return data if isinstance(data, dict) else {}


def _int_value(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1
