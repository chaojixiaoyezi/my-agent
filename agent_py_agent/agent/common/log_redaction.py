# LLM: 日志与模型工具输出共用脱敏；源码模式只保留确定的插值结构，仍遮蔽 URL 明文凭证和已知密钥，不改原文件。
# 模块用途: 在公开输出前隐藏秘密，并避免把源码变量引用和行尾语法误当成密钥删掉。
from __future__ import annotations

"""Process-wide log redaction at the LogRecord creation boundary.

Business modules should log useful structured context without each one having to
remember every credential shape.  This module owns the mandatory last boundary
before a record reaches stderr, journald, a file handler, or a third-party
handler.
"""

import logging
import re
from collections.abc import Mapping
from typing import Any

_REDACTED = "<redacted>"
_SENSITIVE_FIELD_NAMES = frozenset(
    {
        "access_key",
        "access_token",
        "api_key",
        "apikey",
        "app_secret",
        "authorization",
        "client_secret",
        "credential",
        "encrypt_key",
        "id_token",
        "master_key",
        "password",
        "passwd",
        "private_key",
        "pwd",
        "refresh_token",
        "secret",
        "signature",
        "tenant_access_token",
        "ticket",
        "token",
        "verification_token",
        "x-amz-credential",
        "x-amz-signature",
    }
)
# 插值先整体匹配，避免内部括号/引号成为 URL 的结束点；不跨行、不尝试解析整种语言。
_SOURCE_URL_SLOT = r'(?:\$?\{[^{}\r\n]*\}|%(?:\([A-Za-z_]\w*\)|\[[1-9]\d*\])?[-+ #0]*\d*(?:\.\d+)?[sqvdr])'
_SOURCE_URL_SLOT_RE = re.compile(_SOURCE_URL_SLOT)
# 槽和普通字符互斥，避免无 @ 的长模板在 userinfo 匹配中指数回溯；兼容 Python 3.10。
_NOT_SOURCE_URL_SLOT = rf'(?!{_SOURCE_URL_SLOT})'
_URL_VALUE = rf'(?:{_SOURCE_URL_SLOT}|{_NOT_SOURCE_URL_SLOT}[^&#\s\]\)\"\'`,;])+'
_SENSITIVE_QUERY_PREFIX = (
    r"([?&](?:access_key|access_token|api_?key|client_secret|credential|id_token|"
    r"password|refresh_token|secret|signature|tenant_access_token|ticket|token|"
    r"verification_token|x-amz-credential|x-amz-signature)=)"
)
_SENSITIVE_QUERY_RE = re.compile(rf"(?i){_SENSITIVE_QUERY_PREFIX}({_URL_VALUE})")
# 日志不按源码标点分段：逗号、引号或括号可能就是真凭证的一部分，宁可隐藏尾部标点也不能泄漏尾段。
_LOG_SENSITIVE_QUERY_RE = re.compile(rf"(?i){_SENSITIVE_QUERY_PREFIX}([^&#\s]+)")
_AUTHORIZATION_RE = re.compile(r"(?i)(\bAuthorization\s*:\s*Bearer\s+)([^\s,;]+)")
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)(\b(?:access_key|access_token|api_?key|app_secret|client_secret|master_key|"
    r"password|passwd|private_key|pwd|refresh_token|secret|tenant_access_token|ticket|token|"
    r"verification_token)\b\s*[:=]\s*)([\"']?)([^\s,;&\"']{4,})(\2)"
)
_SECRET_JSON_FIELD_RE = re.compile(
    r"(?i)([\"'](?:access_key|access_token|api_?key|app_secret|authorization|client_secret|"
    r"credential|id_token|master_key|password|passwd|private_key|pwd|refresh_token|secret|signature|"
    r"tenant_access_token|ticket|token|verification_token)[\"']\s*:\s*)"
    r"([\"'])([^\"'\r\n]{1,})(\2)"
)
_URL_PASSWORD_RE = re.compile(
    rf'(\b[A-Za-z][A-Za-z0-9+.-]*://(?:{_SOURCE_URL_SLOT}|{_NOT_SOURCE_URL_SLOT}[^:/?\#\s\"\'`@])+:)'
    rf'((?:{_SOURCE_URL_SLOT}|{_NOT_SOURCE_URL_SLOT}[^/@?\#\s\"\'`])+)(@)'
)
_URL_TEMPLATE_USERINFO_RE = re.compile(rf'(\b[A-Za-z][A-Za-z0-9+.-]*://)({_SOURCE_URL_SLOT})(@)')
_LOG_URL_PASSWORD_RE = re.compile(r'(\b[A-Za-z][A-Za-z0-9+.-]*://[^:/?#\s@]+:)([^@\s]+)(@)')
_SOURCE_STRING_RE = re.compile(r'''(["'])((?:\\.|(?!\1)[^\\\r\n])*)\1''')
_KNOWN_SECRET_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(?:sk-[A-Za-z0-9_-]{10,}|github_pat_[A-Za-z0-9_]{10,}|"
    r"gh[pousr]_[A-Za-z0-9_]{10,}|"
    r"xox[baprs]-[A-Za-z0-9-]{10,}|AKIA[A-Z0-9]{16}|AIza[A-Za-z0-9_-]{30,}|"
    r"pypi-[A-Za-z0-9_-]{10,}|npm_[A-Za-z0-9]{10,})(?![A-Za-z0-9_-])"
)
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN[A-Z ]*PRIVATE KEY-----[\s\S]*?-----END[A-Z ]*PRIVATE KEY-----"
)
# LLM: 打码要隐藏的是秘密本身；变量引用不是秘密，只是"值在别处"的写法。整段恰好是一个纯变量引用
#   时保留原文，否则复审会看到 <redacted> 而误判正确的脚本"没带凭据"，模型照抄还可能把字面量
#   <redacted> 写进文件。带默认值、命令替换、拼接、引号包裹等都不算纯引用，照旧打码。
#   注意 Authorization 头那条正则会把结尾引号一起captured 进来（`Bearer $TOKEN"`），所以判定前只脱掉
#   一层引号（成对或只在结尾一个）；引号本身不是值的一部分，脱掉后仍是纯引用才保留。
# 9b 终审必须修：变量名原来收 [A-Za-z_][A-Za-z0-9_]*，于是 "$Summer2024"、"$uper_Secret_1"、
#   "$abcDEF123xyz" 这些以 $ 开头的真密码会被当变量引用放行。改成只认环境变量写法（全大写
#   [A-Z_][A-Z0-9_]*，`$NAME` 与 `${NAME}` 两种），大小写混合与全小写一律打码。两边代价不对等：
#   多打一次码最多让复审误会，少打一次码凭据就进了模型上下文和日志。
#   已知残留边界（有意接受，见 DESIGN_LEDGER）：全大写的 $ 开头字面量（如 $ADMIN123）形状上与变量
#   引用分不开，仍会被保留；不接受任何"只对工具输出例外"的第二套口径。
# 常量用途: 判定一个被打码的值是否恰好只是纯变量引用（全大写环境变量写法，不含任何字面量）。
_PURE_VARIABLE_REFERENCE_RE = re.compile(r"\$(?:[A-Z_][A-Z0-9_]*|\{[A-Z_][A-Z0-9_]*\})\Z")


# LLM: Authorization 正则把结尾引号一起 captured（值形如 `$TOKEN"`），那层引号不是秘密内容；
#   只脱掉一层成对引号或一个结尾引号，其它位置原样保留，避免把 `"$A$B"` 之类放宽。
# 函数用途: 去掉值外层可能被正则带进来的一层引号，用于判定"是否纯变量引用"。
def _strip_wrapping_quote(value: str) -> str:
    if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
        return value[1:-1]
    if value and value[-1] in "\"'":
        return value[:-1]
    return value


# LLM: 调用方决定是否为源码；插值内字面量和实际 URL 凭证仍须遮蔽，已知密钥扫描始终执行。
# 函数用途: 打码一个被赋值/跟随的秘密值；整段只是纯变量引用时原样返回，其余照旧遮蔽。
def _mask_secret_value(value: str, marker: str) -> str:
    return value if _PURE_VARIABLE_REFERENCE_RE.fullmatch(_strip_wrapping_quote(value)) else marker


# LLM: 调用方决定是否为源码；插值内字面量和实际 URL 凭证仍须遮蔽，已知密钥扫描始终执行。
# 函数用途: 返回脱敏副本，不写文件；保持源码结构和日志安全边界各自生效。
def redact_sensitive_text(
    value: object,
    *,
    code_file: bool = False,
    redacted_marker: str = _REDACTED,
    redact_assignment_labels: bool = False,
) -> str:
    """Return a safe string; preserve source-code assignments when requested.

    ``code_file`` follows the file/code boundary: known credential shapes,
    authorization headers, private keys and literal URL credentials are still
    removed. Source interpolation structure is preserved, but literal values
    inside it are masked. Generic ``TOKEN=value`` and JSON-field rules remain
    skipped so ordinary source examples are not corrupted.
    """

    text = str(value)
    if not text:
        return text
    query_pattern = _SENSITIVE_QUERY_RE if code_file else _LOG_SENSITIVE_QUERY_RE
    text = query_pattern.sub(
        lambda match: (
            redacted_marker
            if redact_assignment_labels
            else match.group(1) + _redact_url_value(match.group(2), code_file, redacted_marker)
        ),
        text,
    )
    text = _AUTHORIZATION_RE.sub(
        lambda match: (
            redacted_marker
            if redact_assignment_labels
            else match.group(1) + _mask_secret_value(match.group(2), redacted_marker)
        ),
        text,
    )
    if not code_file:
        text = _SECRET_JSON_FIELD_RE.sub(
            lambda match: (
                redacted_marker
                if redact_assignment_labels
                else match.group(1) + match.group(2) + _mask_secret_value(match.group(3), redacted_marker) + match.group(4)
            ),
            text,
        )
        text = _SECRET_ASSIGNMENT_RE.sub(
            lambda match: (
                redacted_marker
                if redact_assignment_labels
                else match.group(1) + match.group(2) + _mask_secret_value(match.group(3), redacted_marker) + match.group(4)
            ),
            text,
        )
    password_pattern = _URL_PASSWORD_RE if code_file else _LOG_URL_PASSWORD_RE
    text = password_pattern.sub(
        lambda match: match.group(1) + _redact_url_value(match.group(2), code_file, redacted_marker) + match.group(3),
        text,
    )
    text = _URL_TEMPLATE_USERINFO_RE.sub(
        lambda match: match.group(1) + _redact_url_value(match.group(2), code_file, redacted_marker) + match.group(3),
        text,
    )
    text = _KNOWN_SECRET_RE.sub(redacted_marker, text)
    return _PRIVATE_KEY_RE.sub("<redacted-private-key>", text)


# LLM: 只给 code_file 保留插值槽；槽前后真实字面值分别遮蔽，普通日志不因看起来像源码而放行。
# 函数用途: 隐藏一个 URL 凭证值中的明文片段，同时保留模板的括号和格式占位。
def _redact_url_value(value: str, code_file: bool, marker: str) -> str:
    if not code_file:
        return marker
    parts: list[str] = []
    offset = 0
    for match in _SOURCE_URL_SLOT_RE.finditer(value):
        if match.start() > offset:
            parts.append(marker)
        parts.append(_redact_source_slot(match.group(), marker))
        offset = match.end()
    if offset < len(value):
        parts.append(marker)
    return "".join(parts)


# LLM: 变量/属性/调用是表达式而非凭据；表达式内字符串仍遮蔽，只有索引键名保持，最终已知密钥扫描不豁免它。
# 函数用途: 保留 JS/Python 插值外壳和 Go 格式符；字面密钥即使藏在插值里也不能原样输出。
def _redact_source_slot(slot: str, marker: str) -> str:
    if slot.startswith("%"):
        return slot
    start = 2 if slot.startswith("${") else 1
    expression = slot[start:-1]

    # LLM: 字符串只在明确下标位置表示字段名；其它位置不能证明是名称，保守遮蔽其值但保留引号。
    # 函数用途: 替换插值内的秘密字面量，避免连引号和右括号一起吞掉。
    def mask_literal(match: re.Match[str]) -> str:
        index_key = (
            expression[:match.start()].rstrip().endswith("[")
            and expression[match.end():].lstrip().startswith("]")
            and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", match.group(2))
        )
        if index_key or not match.group(2):
            return match.group()
        return match.group(1) + marker + match.group(1)

    sanitized = _SOURCE_STRING_RE.sub(mask_literal, expression)
    # 空间限定在变量引用/调用或已遮蔽字符串，不把任意大括号包裹的值当变量引用。
    if not re.match(r'''\s*(?:[A-Za-z_]|["'])''', expression):
        sanitized = marker
    return slot[:start] + sanitized + "}"


def redact_sensitive_value(
    value: Any,
    *,
    field_name: str = "",
    code_file: bool = False,
) -> Any:
    """Recursively redact strings and values under credential-named fields."""

    if _field_is_sensitive(field_name):
        return _REDACTED
    if isinstance(value, str):
        return redact_sensitive_text(value, code_file=code_file)
    if isinstance(value, BaseException):
        return redact_sensitive_text(value, code_file=code_file)
    if isinstance(value, Mapping):
        return {
            key: redact_sensitive_value(
                item,
                field_name=str(key),
                code_file=code_file,
            )
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        return tuple(
            redact_sensitive_value(item, code_file=code_file)
            for item in value
        )
    if isinstance(value, list):
        return [
            redact_sensitive_value(item, code_file=code_file)
            for item in value
        ]
    return value


def _field_is_sensitive(name: str) -> bool:
    normalized = str(name or "").strip().lower().replace("-", "_")
    return normalized in _SENSITIVE_FIELD_NAMES


def install_log_redaction() -> None:
    """Install the idempotent process-wide LogRecord redaction factory."""

    current_factory = logging.getLogRecordFactory()
    if getattr(current_factory, "_my_agent_secret_redactor", False):
        return

    def _redacting_factory(
        name: str,
        level: int,
        pathname: str,
        lineno: int,
        msg: object,
        args: object,
        exc_info: object,
        func: str | None = None,
        sinfo: str | None = None,
    ) -> logging.LogRecord:
        record = current_factory(name, level, pathname, lineno, msg, args, exc_info, func, sinfo)
        record.msg = redact_sensitive_value(record.msg)
        record.args = redact_sensitive_value(record.args)
        return record

    _redacting_factory._my_agent_secret_redactor = True  # type: ignore[attr-defined]
    logging.setLogRecordFactory(_redacting_factory)


class RedactingFormatter(logging.Formatter):
    """Defense-in-depth formatter for handlers created by my-agent."""

    def format(self, record: logging.LogRecord) -> str:
        return redact_sensitive_text(super().format(record))


__all__ = [
    "RedactingFormatter",
    "install_log_redaction",
    "redact_sensitive_text",
    "redact_sensitive_value",
]
