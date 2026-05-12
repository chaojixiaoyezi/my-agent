# LLM: Backend error types keep provider failures classifiable across CLI, runners, and recovery.
# 模块用途: 定义模型后端错误边界，让网络超时能被主命令和子代理恢复逻辑稳定识别。

from __future__ import annotations


# LLM: ProviderTimeoutError marks model-provider timeout separately from generic backend crashes.
# 类用途: 表示模型接口在配置的等待时间内没有返回；调用方可据此生成恢复报告或 failure_type。
class ProviderTimeoutError(RuntimeError):
    pass


# LLM: is_provider_timeout_error lets higher layers avoid string-matching backend messages.
# 函数用途: 判断异常是否是模型接口超时；以后新增 provider 子类时只需要改这里。
def is_provider_timeout_error(exc: BaseException) -> bool:
    return isinstance(exc, ProviderTimeoutError)


# LLM: provider_timeout_report renders a compact operator-facing timeout handoff.
# 函数用途: 给 CLI 或父级恢复报告生成统一超时说明，避免用户只看到 Python 堆栈。
def provider_timeout_report(exc: BaseException, *, timeout_seconds: object = "") -> str:
    timeout = _timeout_text(timeout_seconds)
    return (
        "[provider_timeout]\n"
        f"模型接口请求超时{timeout}，本次 run 已停止等待。\n"
        f"error={exc}\n"
        "建议下一步：先查看已写入的 subagent/task 状态和 artifacts；"
        "如果需要恢复本次单轮 run，使用 memory-resume 或针对对应 task/run 做恢复。"
    )


# LLM: _timeout_text keeps optional timeout metadata readable without forcing every caller to pass it.
# 函数用途: 把 request_timeout 配置渲染成短文本；缺失时保持文案简洁。
def _timeout_text(timeout_seconds: object) -> str:
    text = str(timeout_seconds or "").strip()
    return f"（request_timeout={text}s）" if text else ""
