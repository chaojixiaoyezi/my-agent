# LLM: TUI activity formatting is isolated so tui.py stays below code-size risk thresholds.
# 模块用途: 格式化 TUI 顶部活动栏的旋转符号和耗时文本。

from __future__ import annotations


# LLM: format_activity_text keeps activity rendering deterministic for tests by accepting now as a value.
# 函数用途: 根据思考文本、开始时间、运行状态和当前时间生成活动栏文本。
def format_activity_text(thinking: str, started_at: float, running: bool, *, now: float) -> str:
    star = _format_activity_star(started_at, running, now=now)
    return f"{star} {_format_thinking_status(thinking, started_at, running, now=now)}"


# LLM: _format_activity_star maps elapsed time to a small deterministic spinner frame.
# 函数用途: 根据运行耗时选择活动栏前缀符号。
def _format_activity_star(started_at: float, running: bool, *, now: float) -> str:
    if not running or not started_at:
        return "✦"
    frames = ("✦", "✧", "✶", "✷")
    elapsed = max(0.0, now - started_at)
    return frames[int(elapsed * 4) % len(frames)]


# LLM: _format_thinking_status appends elapsed seconds only while a task is actively running.
# 函数用途: 生成思考状态文本，空闲时不追加耗时。
def _format_thinking_status(thinking: str, started_at: float, running: bool, *, now: float) -> str:
    if not running or not started_at:
        return thinking
    elapsed = max(0.0, now - started_at)
    return f"{thinking} {elapsed:.1f}s"
