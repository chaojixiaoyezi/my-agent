# LLM: 一次能力包选择的辅助调用失败时，只从异常的结构化属性取诊断事实（类型名、宿主错误码、HTTP 状态、
#   服务商错误的 code/type/param），绝不读异常正文、服务商 message 或提示词；值都按短标记字符集截断检查，不合格的丢弃。
#   结果写进 host_capability_selection.v1 的 failure 字段（capability_selection_state 负责校验）并记一行宿主日志，
#   不进模型上下文。改字段时同步 capability_selection_state._failure_facts 与 test_package_selection_failure.py。
# 模块用途: 把选择失败的原因变成可落盘、可检索、不含正文的小字典，让“为什么没选上”能从结构化事实查清。
from __future__ import annotations

import logging
import re

_LOGGER = logging.getLogger("agent.capability.package_selection")
_TOKEN = re.compile(r"[A-Za-z0-9_.:\-\[\]]{1,80}\Z")
FAILURE_FACT_KEYS = ("error_type", "error_code", "http_status", "provider_error_code", "provider_error_type",
                     "provider_error_param")


# LLM: 只接受短标记（字母数字与 _.:-[]），其余（含空白、正文、超长）一律视为不可用并丢弃，防止把消息文本写进回执。
# 函数用途: 把一个可能的错误码规整成安全短标记，不合格时返回空串。
def _token(value: object) -> str:
    text = str(value or "").strip() if isinstance(value, (str, int)) else ""
    return text if _TOKEN.fullmatch(text) else ""


# LLM: 兼容两种服务商错误形状：{"error": {...}} 和平铺 {...}；只取 code/type/param 三个短字段。
# 函数用途: 从异常 details 里找出服务商错误对象，找不到时返回空字典。
def _provider_error(details: dict) -> dict:
    provider = details.get("provider_error")
    if not isinstance(provider, dict):
        return {}
    nested = provider.get("error")
    return nested if isinstance(nested, dict) else provider


# LLM: 输入是 generate_auxiliary_model_response 抛出的任意异常；status 取 exc.status_code 或 details.status_code，
#   只保留 100–599 的整数。返回值只含 FAILURE_FACT_KEYS 中非空的项，error_type 总是存在。
# 函数用途: 生成选择失败的结构化原因，供回执和日志使用。
def selection_failure_facts(exc: BaseException) -> dict[str, object]:
    details = getattr(exc, "details", None)
    details = details if isinstance(details, dict) else {}
    provider = _provider_error(details)
    status = getattr(exc, "status_code", 0) or details.get("status_code") or 0
    facts: dict[str, object] = {
        "error_type": _token(type(exc).__name__) or "Exception",
        "error_code": _token(getattr(exc, "error_code", "") or getattr(exc, "code", "")),
        "http_status": status if type(status) is int and 100 <= status <= 599 else 0,
        "provider_error_code": _token(provider.get("code")),
        "provider_error_type": _token(provider.get("type")),
        "provider_error_param": _token(provider.get("param")),
    }
    return {key: value for key, value in facts.items() if value}


# LLM: 只写结构化字段，便于运维按 request/run 检索；不写异常正文，失败也不能影响选择收口。
# 函数用途: 记一行宿主日志说明这次选择为什么失败。
def log_selection_failure(facts: dict[str, object], *, request_id: str, run_id: str) -> None:
    _LOGGER.warning("capability selection model call failed(request=%s, run=%s): %s", request_id, run_id,
                    " ".join(f"{key}={facts[key]}" for key in FAILURE_FACT_KEYS if key in facts))


__all__ = ["FAILURE_FACT_KEYS", "log_selection_failure", "selection_failure_facts"]
