"""Bounded, redacted in-memory diagnostic DTO; not a retry/replay authority.

Classifiers keep their original exceptions and codes. W11 owns wiring this
projection at error boundaries; it never logs or writes to a failing disk.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from ..common.log_redaction import redact_sensitive_text

# 结构化错误投影总量上界，单位UTF-8字节；保留分类和errno，超界明确标记截断。
DIAGNOSTIC_DETAILS_MAX_BYTES = 8192

_PROVIDER_FIELDS = ("provider_code", "request_id")
_MCP_DATA_FIELDS = ("provider_code", "request_id", "errno", "operation", "diagnostic_ref")
_BARE_BEARER_RE = re.compile(r"(?i)\bBearer[ \t]+[^\s\"',;<>]+")


# LLM: 仅消费真实str，不调用插件对象的str/repr；供两类安全诊断共用，不改变进程日志规则。
# 函数用途: 遮蔽普通秘密字段和没有Authorization键名的Bearer凭据。
def redact_diagnostic_text(value: str) -> str:
    if type(value) is not str:
        raise TypeError("diagnostic text must be str")
    return _BARE_BEARER_RE.sub("Bearer <redacted>", redact_sensitive_text(value))


# LLM: limit=None只用于不能静默改写的分类字段；总量由_fit_details显式拒绝，不从诊断推导重试权。
# 函数用途: 返回脱敏的UTF-8文本与是否发生有损处理。
def _safe_text(value: str, limit: int | None) -> tuple[str, bool]:
    safe = redact_diagnostic_text(value)
    encoded = safe.encode("utf-8", errors="replace")
    clipped = encoded[:limit].decode("utf-8", errors="ignore")
    return clipped, clipped != value


def _safe_scalar(value: object) -> tuple[object, bool]:
    if type(value) is str:
        return _safe_text(value, DIAGNOSTIC_DETAILS_MAX_BYTES // 8)
    if value is None or type(value) is bool or (type(value) is int and value.bit_length() <= 64):
        return value, False
    # Never stringify exceptions or opaque plugin objects, or traverse them.
    return None, True


# LLM: deferred字段由调用方另行处理；不把合法容器误算成丢弃，也不遍历未知插件对象。
# 函数用途: 投影白名单标量并准确报告被过滤或裁剪的字段。
def _project_fields(value: dict, keys: tuple[str, ...], *, deferred: tuple[str, ...] = ()) -> tuple[dict, bool]:
    if type(value) is not dict:
        raise TypeError("diagnostic details must be plain dict")
    projected: dict = {}
    changed = False
    for key in keys:
        if key in value:
            safe, altered = _safe_scalar(value[key])
            if not altered or safe is not None:
                projected[key] = safe
            changed = changed or altered
    return projected, changed or len(projected) + sum(key in value for key in deferred) != len(value)


# LLM: code和data都属于MCP白名单；data只沿固定子字段投影，不消费任意扩展对象。
# 函数用途: 保留安全MCP结构并准确标记真实的数据损失。
def _project_mcp(value: dict) -> tuple[dict, bool]:
    projected, changed = _project_fields(value, ("code",), deferred=("data",))
    if "data" in value:
        if type(value["data"]) is dict:
            projected["data"], data_changed = _project_fields(value["data"], _MCP_DATA_FIELDS)
            changed = changed or data_changed
        else:
            changed = True
    return projected, changed


def _fit_details(payload: dict) -> dict:
    """Shrink only optional detail strings; leave classification and errno."""
    candidates: list[tuple[dict, str]] = []
    for key in ("provider_error", "mcp_error"):
        details = payload[key]
        if details is not None:
            candidates.extend((details, name) for name, value in details.items() if type(value) is str)
            if type(details.get("data")) is dict:
                candidates.extend((details["data"], name) for name, value in details["data"].items()
                                  if type(value) is str)
    while len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > DIAGNOSTIC_DETAILS_MAX_BYTES:
        nonempty = [(details, key) for details, key in candidates if details[key]]
        if not nonempty:
            raise ValueError("diagnostic control fields exceed projection budget")
        details, key = max(nonempty, key=lambda item: len(item[0][item[1]].encode("utf-8")))
        encoded = details[key].encode("utf-8")
        details[key] = encoded[:len(encoded) // 2].decode("utf-8", errors="ignore")
        payload["details_truncated"] = True
    return payload


@dataclass(frozen=True)
class DiagnosticEnvelope:
    """Safe transport facts, never permission to repeat an unknown side effect."""

    code: str
    category: str
    retryable: bool
    provider_error: dict | None = None
    mcp_error: dict | None = None
    errno: int | None = None
    operation: str | None = None
    diagnostic_ref: str | None = None
    details_truncated: bool = False

    def __post_init__(self) -> None:
        if type(self.retryable) is not bool or type(self.details_truncated) is not bool:
            raise TypeError("diagnostic flags must be bool")
        if self.errno is not None and type(self.errno) is not int:
            raise TypeError("diagnostic errno must be int or None")
        if self.errno is not None and self.errno.bit_length() > 64:
            raise ValueError("diagnostic errno is out of range")
        for key, value in self.to_dict().items():
            object.__setattr__(self, key, value)

    # LLM: 分类字段保持完整；若控制字段本身超总预算则明确拒绝，不用被改写的code伪造原分类。
    # 函数用途: 返回独立安全投影，限额只缩减可选详情，保留errno。
    def to_dict(self) -> dict:
        """Return a fresh safe JSON projection, even if a detail dict changed."""
        payload: dict = {"retryable": self.retryable, "errno": self.errno,
                         "details_truncated": self.details_truncated}
        for key in ("code", "category", "operation", "diagnostic_ref"):
            value = getattr(self, key)
            if value is None and key in ("operation", "diagnostic_ref"):
                payload[key] = None
                continue
            limit = None if key in ("code", "category") else DIAGNOSTIC_DETAILS_MAX_BYTES // 32
            payload[key], changed = _safe_text(value, limit)
            payload["details_truncated"] = payload["details_truncated"] or changed
        for key in ("provider_error", "mcp_error"):
            value = getattr(self, key)
            if value is None:
                payload[key] = None
                continue
            project = _project_mcp if key == "mcp_error" else lambda item: _project_fields(item, _PROVIDER_FIELDS)
            payload[key], changed = project(value)
            payload["details_truncated"] = payload["details_truncated"] or changed
        return _fit_details(payload)
