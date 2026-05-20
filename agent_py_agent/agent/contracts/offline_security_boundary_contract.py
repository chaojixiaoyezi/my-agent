# LLM: Offline security boundary contracts validate network, prompt-data, secret, and idempotency facts.
# 模块用途: 用结构化事件校验私网访问、外部文本隔离、敏感字段脱敏和副作用幂等边界。

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

SECRET_FIELD_NAMES = {"api_key", "authorization", "cookie", "password", "secret", "token"}
REDACTED_VALUES = {"[redacted]", "<redacted>", "***", "redacted"}
PRIVATE_HOSTS = {"localhost"}


# LLM: OfflineSecurityBoundaryValidation reports security-boundary findings.
# 类用途: 返回离线安全边界合同是否通过、错误码和逐项结构化 finding。
@dataclass(frozen=True)
class OfflineSecurityBoundaryValidation:
    ok: bool
    error_codes: tuple[str, ...]
    findings: tuple[dict[str, object], ...]


# LLM: validate_security_boundary_events checks structured security events without network access.
# 函数用途: 根据事件里的 url、payload、idempotency_key 等机器字段校验安全边界。
def validate_security_boundary_events(
    events: tuple[dict[str, Any], ...],
    *,
    allowed_private_hosts: tuple[str, ...] = (),
) -> OfflineSecurityBoundaryValidation:
    findings: list[dict[str, object]] = []
    _validate_network_events(events, set(allowed_private_hosts), findings)
    _validate_external_text_events(events, findings)
    _validate_secret_redaction(events, findings)
    _validate_side_effect_idempotency(events, findings)
    return OfflineSecurityBoundaryValidation(
        ok=not findings,
        error_codes=tuple(dict.fromkeys(_text(item.get("code")) for item in findings)),
        findings=tuple(findings),
    )


# LLM: _validate_network_events blocks private hosts unless explicitly allowed.
# 函数用途: 对 network_request.url 做本地字符串/IP 判断，不发起真实网络请求。
def _validate_network_events(
    events: tuple[dict[str, Any], ...],
    allowed_private_hosts: set[str],
    findings: list[dict[str, object]],
) -> None:
    for index, event in enumerate(events):
        if _event_type(event) != "network_request":
            continue
        host = _host(event.get("url"))
        if host and host not in allowed_private_hosts and _is_private_host(host):
            findings.append(_finding("NETWORK_PRIVATE_HOST_BLOCKED", index, event, {"host": host}))


# LLM: _validate_external_text_events prevents untrusted text from becoming machine contract facts.
# 函数用途: 如果外部文本被标记为已应用到机器合同，则返回 prompt-injection 隔离 finding。
def _validate_external_text_events(
    events: tuple[dict[str, Any], ...],
    findings: list[dict[str, object]],
) -> None:
    for index, event in enumerate(events):
        if _event_type(event) != "external_text":
            continue
        if event.get("untrusted_instruction_detected") is True and event.get("applied_to_machine_contract") is True:
            findings.append(_finding("PROMPT_INJECTION_IGNORED_AS_DATA", index, event))


# LLM: _validate_secret_redaction scans LLM-context payloads for unredacted secret fields.
# 函数用途: 递归检查 llm_context.payload 的 token/password/api_key 等字段是否已脱敏。
def _validate_secret_redaction(
    events: tuple[dict[str, Any], ...],
    findings: list[dict[str, object]],
) -> None:
    for index, event in enumerate(events):
        if _event_type(event) != "llm_context":
            continue
        for field_path in _secret_field_paths(event.get("payload"), prefix="payload"):
            findings.append(_finding("SECRET_REDACTION_REQUIRED", index, event, {"field_path": field_path}))


# LLM: _validate_side_effect_idempotency requires stable keys for replayable side effects.
# 函数用途: 发送消息、写产物、执行动作等副作用必须有 idempotency_key，且同 key 不得换 args_hash。
def _validate_side_effect_idempotency(
    events: tuple[dict[str, Any], ...],
    findings: list[dict[str, object]],
) -> None:
    seen: dict[str, str] = {}
    for index, event in enumerate(events):
        if event.get("side_effect") is not True:
            continue
        key = _text(event.get("idempotency_key"))
        if not key:
            findings.append(_finding("IDEMPOTENCY_KEY_REQUIRED", index, event))
            continue
        args_hash = _text(event.get("args_hash"))
        previous = seen.get(key)
        if previous is not None and previous != args_hash:
            findings.append(_finding("SIDE_EFFECT_REPLAY_BLOCKED", index, event, {"idempotency_key": key}))
        seen.setdefault(key, args_hash)


# LLM: _secret_field_paths recursively scans structured secret-like keys.
# 函数用途: 只根据字段名和脱敏占位判断 secret，不分析自然语言正文。
def _secret_field_paths(value: object, *, prefix: str) -> tuple[str, ...]:
    paths: list[str] = []
    stack: list[tuple[str, object]] = [(prefix, value)]
    while stack:
        current_prefix, current = stack.pop()
        if isinstance(current, dict):
            paths.extend(_dict_secret_paths(current_prefix, current, stack))
            continue
        if isinstance(current, (list, tuple)):
            stack.extend((f"{current_prefix}[{index}]", child) for index, child in enumerate(current))
    return tuple(paths)


# LLM: _dict_secret_paths scans one dict level and pushes children for recursive checks.
# 函数用途: 敏感字段值未脱敏时记录字段路径。
def _dict_secret_paths(
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


# LLM: _is_private_host detects private localhost and IP-literal targets.
# 函数用途: 对 host 字符串做本地判断，覆盖 localhost、loopback、private、link-local。
def _is_private_host(host: str) -> bool:
    normalized = host.strip().lower().strip("[]")
    if normalized in PRIVATE_HOSTS:
        return True
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return address.is_private or address.is_loopback or address.is_link_local


# LLM: _host extracts hostname from URL-like values.
# 函数用途: 使用标准 urlparse 读取 hostname，不解析文本描述。
def _host(value: object) -> str:
    parsed = urlparse(_text(value))
    return _text(parsed.hostname)


# LLM: _value_is_redacted recognizes approved redaction sentinels only.
# 函数用途: 判断敏感字段值是否已经替换为脱敏占位。
def _value_is_redacted(value: object) -> bool:
    if value in (None, ""):
        return True
    if not isinstance(value, str):
        return False
    return value.strip().lower() in REDACTED_VALUES


# LLM: _finding creates compact security-boundary findings.
# 函数用途: 生成 code、index、event_type 和可选结构化字段。
def _finding(
    code: str,
    index: int,
    event: dict[str, Any],
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    return {"code": code, "index": index, "event_type": _event_type(event), **(extra or {})}


# LLM: _event_type normalizes event type values for exact dispatch.
# 函数用途: 读取 type 字段并转小写字符串。
def _event_type(event: dict[str, Any]) -> str:
    return _text(event.get("type")).lower()


# LLM: _text normalizes optional scalar values for exact comparisons.
# 函数用途: 把 None 或标量转成去空白字符串；不解析自然语言含义。
def _text(value: object) -> str:
    return str(value or "").strip()


__all__ = ["OfflineSecurityBoundaryValidation", "validate_security_boundary_events"]
