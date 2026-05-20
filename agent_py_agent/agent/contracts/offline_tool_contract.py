# LLM: Offline tool contracts validate fake-tool and replay outputs before the agent trusts them.
# 模块用途: 校验工具结果形状、大输出外置、敏感字段脱敏和重复无进展调用。

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

SECRET_FIELD_NAMES = {"api_key", "authorization", "cookie", "password", "secret", "token"}
REDACTED_VALUES = {"[redacted]", "<redacted>", "***", "redacted"}


# LLM: OfflineToolValidation reports tool-result contract findings.
# 类用途: 返回工具离线合同是否通过、错误码和逐项结构化 finding。
@dataclass(frozen=True)
class OfflineToolValidation:
    ok: bool
    error_codes: tuple[str, ...]
    findings: tuple[dict[str, object], ...]


# LLM: validate_tool_events checks tool_result rows from fake tools, real traces, or replay fixtures.
# 函数用途: 校验工具输出 shape、大输出处理、敏感字段和重复无进展调用。
def validate_tool_events(
    events: tuple[dict[str, Any], ...],
    *,
    repeated_threshold: int = 3,
) -> OfflineToolValidation:
    findings: list[dict[str, object]] = []
    _validate_tool_result_shapes(events, findings)
    _validate_repeated_no_progress(events, max(2, repeated_threshold), findings)
    return OfflineToolValidation(
        ok=not findings,
        error_codes=tuple(dict.fromkeys(_text(item.get("code")) for item in findings)),
        findings=tuple(findings),
    )


# LLM: _validate_tool_result_shapes verifies each tool_result has a structured result payload.
# 函数用途: 对 None、非对象、大输出未外置、敏感字段泄露分别生成稳定 finding。
def _validate_tool_result_shapes(
    events: tuple[dict[str, Any], ...],
    findings: list[dict[str, object]],
) -> None:
    for index, event in enumerate(events):
        if _event_type(event) != "tool_result":
            continue
        result = event.get("result")
        if result is None:
            findings.append(
                _finding(
                    "TOOL_RESULT_NONE",
                    {"index": index, "operation_id": _text(event.get("operation_id"))},
                )
            )
            continue
        if not isinstance(result, dict):
            findings.append(
                _finding(
                    "TOOL_RESULT_SHAPE_INVALID",
                    {"index": index, "operation_id": _text(event.get("operation_id"))},
                )
            )
            continue
        _validate_large_output(index, event, result, findings)
        _validate_secret_fields(index, result, findings)


# LLM: _validate_large_output requires large inline payloads to be truncated or referenced.
# 函数用途: 如果 result content/text/output 超出 inline_budget_bytes，必须有 artifact_refs 或 truncated=true。
def _validate_large_output(
    index: int,
    event: dict[str, Any],
    result: dict[str, Any],
    findings: list[dict[str, object]],
) -> None:
    budget = _optional_int(event.get("inline_budget_bytes") or result.get("inline_budget_bytes"))
    if budget is None:
        return
    inline_size = max(_byte_len(result.get(key)) for key in ("content", "text", "output"))
    if inline_size <= budget:
        return
    if result.get("truncated") is True or _string_list(result.get("artifact_refs")):
        return
    findings.append(
        _finding(
            "TOOL_RESULT_TOO_LARGE_NOT_EXTERNALIZED",
            {
                "index": index,
                "operation_id": _text(event.get("operation_id")),
                "inline_size_bytes": inline_size,
                "inline_budget_bytes": budget,
            },
        )
    )


# LLM: _validate_secret_fields rejects unredacted secrets in structured tool results.
# 函数用途: 递归扫描 result 内的敏感字段名，字段值未脱敏时返回 TOOL_RESULT_SECRET_LEAK。
def _validate_secret_fields(
    index: int,
    result: dict[str, Any],
    findings: list[dict[str, object]],
) -> None:
    for field_path in _secret_field_paths(result, prefix="result"):
        findings.append(_finding("TOOL_RESULT_SECRET_LEAK", {"index": index, "field_path": field_path}))


# LLM: _validate_repeated_no_progress blocks identical read-only tool/result loops.
# 函数用途: 连续同 tool、args_hash、result_hash 且 read_only=true 达阈值时返回重复无进展 finding。
def _validate_repeated_no_progress(
    events: tuple[dict[str, Any], ...],
    threshold: int,
    findings: list[dict[str, object]],
) -> None:
    last_key: tuple[str, str, str] | None = None
    streak = 0
    reported_keys: set[tuple[str, str, str]] = set()
    for index, event in enumerate(events):
        key = _repeat_key(event)
        if key is None:
            last_key = None
            streak = 0
            continue
        streak = streak + 1 if key == last_key else 1
        last_key = key
        if streak >= threshold and key not in reported_keys:
            reported_keys.add(key)
            findings.append(
                _finding(
                    "TOOL_REPEATED_NO_PROGRESS",
                    {
                        "index": index,
                        "tool": key[0],
                        "args_hash": key[1],
                        "result_hash": key[2],
                    },
                )
            )


# LLM: _repeat_key returns the exact no-progress identity for read-only tool results.
# 函数用途: 只有 tool、args_hash、result_hash 都存在且 read_only=true 时才参与重复判断。
def _repeat_key(event: dict[str, Any]) -> tuple[str, str, str] | None:
    if _event_type(event) != "tool_result" or event.get("read_only") is not True:
        return None
    tool = _text(event.get("tool"))
    args_hash = _text(event.get("args_hash"))
    result_hash = _text(event.get("result_hash"))
    if not (tool and args_hash and result_hash):
        return None
    return (tool, args_hash, result_hash)


# LLM: _secret_field_paths recursively scans structured keys for sensitive fields.
# 函数用途: 递归找出未脱敏的 token/password/api_key 等字段路径。
def _secret_field_paths(value: object, *, prefix: str) -> tuple[str, ...]:
    paths: list[str] = []
    stack: list[tuple[str, object]] = [(prefix, value)]
    while stack:
        current_prefix, current = stack.pop()
        if isinstance(current, dict):
            paths.extend(_dict_secret_field_paths(current_prefix, current, stack))
            continue
        if isinstance(current, (list, tuple)):
            stack.extend(_indexed_children(current_prefix, current))
    return tuple(paths)


# LLM: _dict_secret_field_paths records unredacted secret keys and pushes child values.
# 函数用途: 处理一层 dict，避免 _secret_field_paths 深层嵌套。
def _dict_secret_field_paths(
    prefix: str,
    value: dict[object, object],
    stack: list[tuple[str, object]],
) -> list[str]:
    paths: list[str] = []
    for key, child in value.items():
        key_text = _text(key)
        path = f"{prefix}.{key_text}" if prefix else key_text
        if key_text.lower() in SECRET_FIELD_NAMES and not _value_is_redacted(child):
            paths.append(path)
        stack.append((path, child))
    return paths


# LLM: _indexed_children returns list children with stable path suffixes.
# 函数用途: 将 list/tuple 子项转成扫描栈条目。
def _indexed_children(prefix: str, value: list[object] | tuple[object, ...]) -> list[tuple[str, object]]:
    return [(f"{prefix}[{index}]", child) for index, child in enumerate(value)]


# LLM: _value_is_redacted recognizes approved redaction sentinels only.
# 函数用途: 判断敏感字段值是否已经替换为脱敏占位。
def _value_is_redacted(value: object) -> bool:
    if value in (None, ""):
        return True
    if not isinstance(value, str):
        return False
    return value.strip().lower() in REDACTED_VALUES


# LLM: _byte_len measures inline result fields without serializing whole payloads.
# 函数用途: 返回字符串字段的 UTF-8 字节长度，非字符串按 0 处理。
def _byte_len(value: object) -> int:
    if not isinstance(value, str):
        return 0
    return len(value.encode("utf-8"))


# LLM: _optional_int reads optional numeric budgets without inventing limits.
# 函数用途: 将显式传入的数字转为 int，未传或非法时返回 None。
def _optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# LLM: _finding creates compact machine findings without prose parsing.
# 函数用途: 生成 code 和额外结构字段。
def _finding(code: str, extra: dict[str, object] | None = None) -> dict[str, object]:
    return {"code": code, **(extra or {})}


# LLM: _event_type normalizes event type values for exact dispatch.
# 函数用途: 读取 type 字段并转小写字符串。
def _event_type(event: dict[str, Any]) -> str:
    return _text(event.get("type")).lower()


# LLM: _string_list normalizes list-like fields without parsing embedded prose.
# 函数用途: 把结构化数组规整成去空字符串列表。
def _string_list(value: object) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    return [text for item in value for text in [_text(item)] if text]


# LLM: _text normalizes optional scalar values for exact comparisons.
# 函数用途: 把 None 或标量转成去空白字符串；不解析自然语言含义。
def _text(value: object) -> str:
    return str(value or "").strip()


__all__ = ["OfflineToolValidation", "validate_tool_events"]
