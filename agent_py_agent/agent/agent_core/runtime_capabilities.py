# LLM: Agent core orchestration module; keep planning, dispatch, tool-loop, and finalization contracts stable.
# 模块用途: 支撑主代理运行循环、计划、工具调用、子代理调度和收尾。

from __future__ import annotations

"""Runtime capability resolution for ordinary SimpleAgent turns.

The registry owns final security-tool authorization. This module only decides
whether a normal run/chat turn carries an explicit logs/security protocol
marker. Natural-language security/log phrases are left to the model and tool
catalog, not hard-coded product logic.
"""

import re
from collections.abc import Iterable

from ..log_analysis.capabilities import SECURITY_TOOL_NAMES, has_security_tool_capability

SECURITY_RUNTIME_CAPABILITY = "logs/security"

_EXPLICIT_SECURITY_MARKERS = {
    SECURITY_RUNTIME_CAPABILITY,
    "log_analysis",
    "security_logs",
    *SECURITY_TOOL_NAMES,
}

# LLM: resolve_runtime_capabilities resolves explicit protocol/tool capability markers.
# 函数用途: 读取 granted_capabilities、inject 和 prompt 中的机器能力标识；不靠“安全日志”等自然语言词表猜能力。
def resolve_runtime_capabilities(
    user_prompt: str,
    *,
    inject: Iterable[str] | None = None,
    granted_capabilities: Iterable[str] | None = None,
) -> list[str]:
    capabilities = _normalize_capabilities(granted_capabilities)
    if has_security_tool_capability(capabilities):
        return capabilities

    text = "\n".join([user_prompt or "", *(str(item) for item in (inject or []))])
    if _looks_like_security_log_task(text):
        capabilities.append(SECURITY_RUNTIME_CAPABILITY)
    return capabilities


# LLM: _normalize_capabilities 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 解析并归一化能力的输入形态，让下游只处理稳定结构；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
def _normalize_capabilities(capabilities: Iterable[str] | None) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in capabilities or []:
        item = str(raw).strip()
        key = item.lower()
        if item and key not in seen:
            normalized.append(item)
            seen.add(key)
    return normalized


# LLM: _looks_like_security_log_task now means explicit capability marker only.
# 函数用途: 判断文本是否包含 logs/security 或 security tool id 等机器标识；普通自然语言不再触发能力。
def _looks_like_security_log_task(text: str) -> bool:
    if not text.strip():
        return False
    lowered = text.lower()
    return _contains_keyword(lowered, _EXPLICIT_SECURITY_MARKERS)


# LLM: _contains_keyword 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 判断keyword条件是否成立，作为后续调度或分支决策的门禁；关键副作用: 主要返回判断或抛出明确异常，调用方依赖布尔语义稳定。
def _contains_keyword(text: str, keywords: Iterable[str]) -> bool:
    for keyword in keywords:
        if _contains_single_keyword(text, keyword):
            return True
    return False


# LLM: _contains_single_keyword 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 判断单个keyword条件是否成立，作为后续调度或分支决策的门禁；关键副作用: 主要返回判断或抛出明确异常，调用方依赖布尔语义稳定。
def _contains_single_keyword(text: str, keyword: str) -> bool:
    if re.fullmatch(r"[a-z0-9_ ]+", keyword):
        return bool(re.search(rf"(?<![a-z0-9_]){re.escape(keyword)}(?![a-z0-9_])", text))
    return keyword in text
