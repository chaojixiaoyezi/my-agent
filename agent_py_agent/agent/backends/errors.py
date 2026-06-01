# LLM: Provider error module owns recoverable model-service failure taxonomy.
# 模块用途: 给 CLI、runner、恢复逻辑提供统一的 timeout/transient/recoverable 异常和报告文本。
"""Typed provider errors shared by CLI, runners, and recovery code."""

from __future__ import annotations


# LLM: ProviderRecoverableError is the single parent for resume-safe provider failures.
# 类用途: 标记模型服务临时失败；上层可以重试或恢复，不能误当业务代码失败。
class ProviderRecoverableError(RuntimeError):
    """Base class for model-provider failures that can be retried or resumed."""


# LLM: ProviderTimeoutError preserves request-timeout failures without string matching.
# 类用途: 表示模型服务超时，用于 CLI 报告、子代理失败分类和恢复策略。
class ProviderTimeoutError(ProviderRecoverableError):
    """The provider did not return before the configured request timeout."""


# LLM: ProviderTransientError preserves rate-limit/5xx/disconnect failures without string matching.
# 类用途: 表示模型服务限流、5xx 或临时断连，用于当前模型回合自动续跑。
class ProviderTransientError(ProviderRecoverableError):
    """The provider returned a temporary overload/rate-limit/disconnect error."""


# LLM: is_provider_recoverable_error is the broad predicate callers should prefer.
# 函数用途: 判断异常是否属于模型服务可恢复错误，避免上层分别匹配 timeout/transient。
def is_provider_recoverable_error(exc: BaseException) -> bool:
    """Return True for typed provider failures that should not be treated as task bugs."""
    return isinstance(exc, ProviderRecoverableError)


# LLM: is_provider_timeout_error keeps old timeout-specific callers stable.
# 函数用途: 判断异常是否为模型服务超时；保留给需要区分超时文案的边界。
def is_provider_timeout_error(exc: BaseException) -> bool:
    """Return True when the model-provider request timed out."""
    return isinstance(exc, ProviderTimeoutError)


# LLM: is_provider_transient_error keeps retry loops limited to transient provider flakes.
# 函数用途: 判断异常是否为限流、5xx 或断连；自动重试只使用这个窄判断。
def is_provider_transient_error(exc: BaseException) -> bool:
    """Return True when the model-provider failure is temporary or rate-limited."""
    return isinstance(exc, ProviderTransientError)


# LLM: provider_recoverable_report is the canonical report entry for all provider flakes.
# 函数用途: 将可恢复模型错误渲染成用户/父代理可读的恢复提示，避免重复报告函数分叉。
def provider_recoverable_report(exc: BaseException, *, timeout_seconds: object = "") -> str:
    """Render the canonical operator-facing report for recoverable provider failures."""
    if is_provider_timeout_error(exc):
        return provider_timeout_report(exc, timeout_seconds=timeout_seconds)
    if is_provider_transient_error(exc):
        return provider_transient_report(exc)
    return (
        "[provider_recoverable]\n"
        "模型接口出现可恢复异常，本次 run 已停止当前请求。\n"
        f"error={exc}\n"
        "建议下一步：查看已写入的任务状态和产物，然后从未完成部分继续。"
    )


# LLM: provider_timeout_report renders timeout-specific recovery advice.
# 函数用途: 输出超时恢复建议，并保留 request_timeout 元数据方便排查。
def provider_timeout_report(exc: BaseException, *, timeout_seconds: object = "") -> str:
    """Render a compact timeout handoff for CLI and parent-agent recovery."""
    timeout = _timeout_text(timeout_seconds)
    return (
        "[provider_timeout]\n"
        f"模型接口请求超时{timeout}，本次 run 已停止等待。\n"
        f"error={exc}\n"
        "建议下一步：先查看已写入的 subagent/task 状态和 artifacts；"
        "如果需要恢复本次单轮 run，使用 memory-resume 或针对对应 task/run 做恢复。"
    )


# LLM: provider_transient_report renders rate-limit/5xx/disconnect recovery advice.
# 函数用途: 输出临时 provider 失败恢复建议，提醒继续未完成部分而不是重跑全部。
def provider_transient_report(exc: BaseException) -> str:
    """Render a compact transient-provider handoff for CLI and parent recovery."""
    return (
        "[provider_transient]\n"
        "模型接口临时不可用或被限流，本次 run 已停止当前请求。\n"
        f"error={exc}\n"
        "建议下一步：稍后重试，或先查看已写入的子代理状态、产物和 memory archive；"
        "已完成的工作不要重跑，继续未完成部分即可。"
    )


# LLM: _timeout_text keeps optional timeout metadata readable.
# 函数用途: 把可选 request_timeout 值渲染到超时文案；缺失时不制造噪音。
def _timeout_text(timeout_seconds: object) -> str:
    """Format optional timeout metadata without forcing every caller to pass it."""
    text = str(timeout_seconds or "").strip()
    return f"（request_timeout={text}s）" if text else ""
