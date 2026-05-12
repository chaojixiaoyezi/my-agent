# LLM: Capability request identity keeps tool-created and model-parsed requests deduped by scope.
# 模块用途: 用稳定签名识别同一个能力申请，避免 runner 工具调用和结构化结果各写一份重复请求。

from __future__ import annotations

import shlex
from collections.abc import Iterable

_ACTIVE_REQUEST_STATUSES = frozenset({"", "OPEN", "GRANTED", "RESOLVED"})


# LLM: find_equivalent_capability_request returns an active matching request instead of creating duplicates.
# 函数用途: 在已有 capability_requests 中按能力、工具、命令、路径和预算查找等价请求；匹配到则复用旧记录。
def find_equivalent_capability_request(requests: Iterable[object], candidate: object):
    target = capability_request_signature(candidate)
    for request in requests or []:
        if str(getattr(request, "status", "") or "").upper() not in _ACTIVE_REQUEST_STATUSES:
            continue
        if capability_request_signature(request) == target:
            return request
    return None


# LLM: capability_request_signature compares business scope, not transient ids or model wording.
# 函数用途: 生成能力申请去重签名；忽略 problem 文案和 request id，保留实际授权边界。
def capability_request_signature(value: object) -> tuple:
    return (
        _text_attr(value, "capability_type", default="generic").lower(),
        _text_attr(value, "needed_capability").lower(),
        _normalized_list_attr(value, "requested_tools"),
        _normalized_list_attr(value, "requested_skills"),
        _normalized_list_attr(value, "requested_mcp_tools"),
        _normalized_command_attr(value, "requested_commands"),
        _normalized_list_attr(value, "cwd_scope"),
        _normalized_list_attr(value, "path_scope"),
        _normalized_list_attr(value, "network_scope"),
        tuple(sorted(_object_dict_attr(value, "output_budget").items())),
        _text_attr(value, "risk_level").lower(),
    )


# LLM: _normalized_command_attr mirrors shell gateway command-name matching without executing anything.
# 函数用途: 将完整命令字符串归一成首个可执行名，保证 python3 -c 与 python3 不会生成重复授权。
def _normalized_command_attr(value: object, name: str) -> tuple[str, ...]:
    commands = [_command_name(item).lower() for item in _list_attr(value, name)]
    return tuple(sorted(item for item in dict.fromkeys(commands) if item))


# LLM: _command_name is syntax-only; it never expands variables or shells out.
# 函数用途: 用 shlex 提取命令名，失败时退回空白切分，服务于签名和去重。
def _command_name(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parts = shlex.split(text)
    except ValueError:
        parts = text.split()
    return str(parts[0]).strip() if parts else ""


# LLM: _normalized_list_attr returns deterministic list fields for signature comparison.
# 函数用途: 字符串化、去空、去重、排序，避免同一 scope 因顺序不同被当成新请求。
def _normalized_list_attr(value: object, name: str) -> tuple[str, ...]:
    items = [str(item).strip() for item in _list_attr(value, name)]
    return tuple(sorted(item for item in dict.fromkeys(items) if item))


# LLM: _list_attr reads dataclass attrs without assuming a concrete request class.
# 函数用途: 兼容 CapabilityRequest 与 RecordCapabilityRequestParams 这两种输入形态。
def _list_attr(value: object, name: str) -> list:
    raw = getattr(value, name, None)
    if raw is None:
        return []
    return list(raw) if isinstance(raw, (list, tuple, set)) else [raw]


# LLM: _text_attr keeps missing optional fields stable across old records.
# 函数用途: 读取字符串字段并给 capability_type 等旧记录缺省值。
def _text_attr(value: object, name: str, *, default: str = "") -> str:
    raw = getattr(value, name, default)
    if raw is None:
        return default
    return str(raw or default).strip()


# LLM: _object_dict_attr keeps output budget comparable without accepting scalar blobs.
# 函数用途: 读取 JSON-like dict 字段；非字典按空对象处理。
def _object_dict_attr(value: object, name: str) -> dict[str, object]:
    raw = getattr(value, name, None)
    if not isinstance(raw, dict):
        return {}
    return {str(key): item for key, item in raw.items()}
