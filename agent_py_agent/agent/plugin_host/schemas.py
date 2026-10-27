# LLM: 只做有界纯校验，不读owner或持久化；所有schema来源为静态宿主动作目录。
# 模块用途: 严格解码插件JSON、校验闭合参数并冻结输入。
from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from datetime import datetime
from types import MappingProxyType

from ..tooling.input_schema import validate_tool_input
from .errors import HostAPIError

# 单个公共请求的UTF8字节上限。
REQUEST_MAX_BYTES = 65536
# 单页公开JSON响应的字节上限。
PAGE_MAX_BYTES = 262144
# JSON嵌套层数上限，先迭代检查再做递归投影。
JSON_DEPTH_COUNT = 12


# LLM: 严格JSON钩子不能覆盖重复键，重复内容也拒绝。
# 函数用途: 从键值序列恢复唯一键对象。
def _unique_object(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("重复JSON字段")
        result[key] = value
    return result


# LLM: NaN/Infinity没有跨进程规范语义，不能进入摘要或schema。
# 函数用途: 拒绝非标准JSON常量。
def _nonfinite(value: str):
    raise ValueError("非有限数字")


# LLM: 限制尺寸、字符编码、深度和数值，错误不带原文。
# 函数用途: 把HTTP原始字节转换为安全JSON值。
def strict_json(raw: bytes, maximum: int = REQUEST_MAX_BYTES):
    if not isinstance(raw, bytes):
        raise HostAPIError("INVALID_SCHEMA")
    if len(raw) > maximum:
        raise HostAPIError("PAYLOAD_TOO_LARGE", 413)
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_nonfinite)
        validate_tree(value)
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise HostAPIError("INVALID_SCHEMA") from None


# LLM: bool是合法JSON值但不是数值schema中的整数；深度包含容器边界。
# 函数用途: 有界检查树的类型、字符串编码和非有限数字。
def validate_tree(value) -> None:
    pending = [(value, 0)]
    while pending:
        node, depth = pending.pop()
        if depth > JSON_DEPTH_COUNT:
            raise ValueError("结构过深")
        if isinstance(node, dict):
            if any(not isinstance(key, str) for key in node):
                raise ValueError("非字符串键")
            pending.extend((item, depth + 1) for item in (*node.keys(), *node.values()))
            continue
        if isinstance(node, list):
            pending.extend((item, depth + 1) for item in node)
            continue
        _validate_scalar(node)


# LLM: 独立处理叶子以避免容器遍历和类型分支叠加；行为与JSON严格边界相同。
# 函数用途: 核对单个标量的字符编码与数值合法性。
def _validate_scalar(node) -> None:
    if isinstance(node, str):
        node.encode("utf-8")
        return
    if isinstance(node, float):
        if not math.isfinite(node):
            raise ValueError("非有限数字")
        return
    if node is not None and type(node) not in {int, bool}:
        raise ValueError("非JSON值")


# LLM: 返回规范字节仅用于摘要和持久合同，不作为身份认证。
# 函数用途: 稳定编码JSON且拒绝不可表示值。
def canonical_json(value) -> str:
    return json.dumps(thaw(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


# LLM: MappingProxyType和tuple防止校验后经别名改变参数。
# 函数用途: 深冻结已验证JSON值。
def freeze(value):
    if isinstance(value, Mapping):
        return MappingProxyType({key: freeze(item) for key, item in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(freeze(item) for item in value)
    return value


# LLM: 返回独立可序列化副本，不泄露可变DTO内部引用。
# 函数用途: 把冻结结构投影为普通JSON类型。
def thaw(value):
    if isinstance(value, Mapping):
        return {key: thaw(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [thaw(item) for item in value]
    return value


# LLM: min/max按Unicode字符，字节约束由显式扩展另核。
# 函数用途: 构造有界字符串schema。
def text(maximum: int, minimum: int = 0, pattern: str | None = None, byte_limit: int | None = None) -> dict:
    result = {"type": "string", "minLength": minimum, "maxLength": maximum}
    if pattern is not None:
        result["pattern"] = pattern
    if byte_limit is not None:
        result["x-max-bytes"] = byte_limit
    return result


# LLM: schema整数不接受bool，具体值还受上层宿主配额约束。
# 函数用途: 构造有界整数schema。
def integer(maximum: int, minimum: int = 0) -> dict:
    return {"type": "integer", "minimum": minimum, "maximum": maximum}


# LLM: required与optional显式分开，所有对象默认拒绝额外键。
# 函数用途: 构造闭合对象schema。
def closed(required: dict, optional: dict | None = None) -> dict:
    return {"type": "object", "properties": {**required, **(optional or {})},
            "required": list(required), "additionalProperties": False}


# LLM: defaults只在明确缺字段时注入，显式null不是省略。
# 函数用途: 按静态schema验证输入并应用规范默认值。
def validate_object(value: object, schema: dict) -> dict:
    if not isinstance(value, dict) or not validate_tool_input(value, schema).ok:
        raise HostAPIError("INVALID_SCHEMA")
    _validate_units(value, schema)
    result = json.loads(canonical_json(value))
    for key, field in schema.get("properties", {}).items():
        if key not in result and "default" in field:
            result[key] = json.loads(canonical_json(field["default"]))
    return result


# LLM: nullable数值仍排除bool；事件的可选耗时/计量与旧整数共用校验，不放宽其它字段或身份门。
# 函数用途: 递归复核字节、布尔与含空值数值的边界。
def _validate_units(value, schema: dict) -> None:
    kind = schema.get("type")
    kinds = kind if isinstance(kind, list) else [kind]
    if value is not None and "integer" in kinds and type(value) is not int:
        raise HostAPIError("INVALID_SCHEMA")
    if value is not None and "number" in kinds and type(value) not in (int, float):
        raise HostAPIError("INVALID_SCHEMA")
    if kind == "integer" and type(value) is not int:
        raise HostAPIError("INVALID_SCHEMA")
    if kind == "boolean" and type(value) is not bool:
        raise HostAPIError("INVALID_SCHEMA")
    if isinstance(value, str) and len(value.encode("utf-8")) > schema.get("x-max-bytes", REQUEST_MAX_BYTES):
        raise HostAPIError("INVALID_SCHEMA")
    if isinstance(value, dict):
        for key, item in value.items():
            _validate_units(item, schema.get("properties", {}).get(key, {}))
    if isinstance(value, list):
        if schema.get("uniqueItems") and len({canonical_json(item) for item in value}) != len(value):
            raise HostAPIError("INVALID_SCHEMA")
        for item in value:
            _validate_units(item, schema.get("items", {}))


# LLM: 目标只允许ASCII普通用户ID，绝不清洗非法输入为有效身份。
# 函数用途: 拒绝管理员、目录别名和保留用户名称。
def validate_local_user_id(value: str) -> str:
    if (not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{2,63}", value)
            or value in {"main", "admin", "root", "system", "local"}):
        raise HostAPIError("INVALID_SCHEMA")
    return value


# LLM: 只有users动作payload顶层user_id是合法选择器，其余身份/权限键递归拒绝。
# 函数用途: 阻断工具参数中的宿主身份和沙箱元字段注入。
def reject_identity_injection(payload: dict, users_action: bool = False) -> None:
    forbidden = {"owner_id", "owner_home", "target_owner_home", "admin", "is_admin", "channel", "user_id",
                 "credential_ref", "access_mode", "workspace_root", "override_data_sources", "host_request_owner"}
    pending = [(payload, True)]
    while pending:
        value, top = pending.pop()
        if isinstance(value, dict):
            for key, item in value.items():
                allowed = top and users_action and key == "user_id"
                if not allowed and (key in forbidden or key.startswith("__")):
                    raise HostAPIError("INVALID_SCHEMA")
                pending.append((item, False))
        elif isinstance(value, (list, tuple)):
            pending.extend((item, False) for item in value)


# LLM: 字段名与现有quota分开验证，范围来自v2对外合同。
# 函数用途: 返回普通账号接口的闭合配额schema。
def quota_schema() -> dict:
    return closed({"max_active_agents": integer(1000, 1), "max_subagents": integer(50),
                   "max_depth": integer(4), "max_disk_mb": integer(1048576, 1)})


# LLM: 公共事件仅允许闭合投影；工具原参数、秘密和原始结果均不是合法字段。
# 函数用途: 校验并规范化一条不超过64KiB的RequestEvent。
def validate_request_event(value) -> dict:
    row = strict_json(canonical_json(value).encode())
    timestamp = text(64, 20, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z$")
    schemas = _event_data_schemas(timestamp)
    row = validate_object(row, closed({"event_id": text(128, 1), "sequence": integer(2**63 - 1, 1),
        "kind": {"enum": list(schemas)}, "created_at": timestamp, "data": {"type": "object"}}))
    row["data"] = validate_object(row["data"], schemas[row["kind"]])
    try:
        datetime.fromisoformat(row["created_at"])
        if row["kind"] == "terminal":
            datetime.fromisoformat(row["data"]["completed_at"])
    except ValueError:
        raise HostAPIError("INVALID_SCHEMA") from None
    return row


# LLM: 新计量/耗时仅可选扩展，保留旧四字段工具事件及两字段usage；HTTP泵与缓冲共用唯一合同。
# 函数用途: 返回兼容旧事件的六种闭合数据schema，未知数值仍用null。
def _event_data_schemas(timestamp: dict) -> dict:
    identifier = text(128, 1)
    interaction = closed({"interaction_id": identifier, "summary": text(1024)})
    count = {"type": ["integer", "null"], "minimum": 0, "maximum": 2**63 - 1}
    timing = {"type": ["number", "null"], "minimum": 0}
    usage = closed({"input_tokens": count, "output_tokens": count}, {
        **dict.fromkeys(("cache_read_tokens", "cache_write_tokens", "total_tokens", "cached_tokens"), count),
        "elapsed_seconds": timing, "tokens_per_second": timing})
    terminal = closed({"terminal_id": identifier, "status": {"enum": ["succeeded", "failed", "cancelled"]},
        "completed_at": timestamp, "result_message_ids": {"type": "array", "items": identifier, "maxItems": 100},
        "error_code": {"type": ["string", "null"], "maxLength": 64, "pattern": r"^[A-Z][A-Z0-9_]{0,63}$"},
        "usage": usage,
        "resources_stopped": {"type": ["boolean", "null"]}})
    return {"status": closed({"state": {"enum": ["queued", "admission_wait", "running", "waiting_approval",
        "waiting_input", "cancelling", "succeeded", "failed", "cancelled", "unknown"]}}),
        "assistant_delta": closed({"message_id": identifier, "part": {"enum": ["commentary", "final"]},
                                   "offset_bytes": integer(2**63 - 1), "text": text(REQUEST_MAX_BYTES)}),
        "tool_progress": closed({"call_id": identifier, "tool_name": text(96, 1),
                                 "status": {"enum": ["started", "progress", "ended"]}, "summary": text(1024)},
                                {"elapsed_ms": count}),
        "approval_required": interaction, "input_required": interaction, "terminal": terminal}
