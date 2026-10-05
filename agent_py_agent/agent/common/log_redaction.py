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
        "cookie",
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
# LLM: 授权头三种键值形状（无引号头、JSON 双/单引号键、等号赋值）共用这一条规则：键名后可带
#   一层引号与冒号/等号；值前的方案名（Bearer）留在结构里，值部分过 _mask_secret_value（纯变量
#   引用保留语义不变）；组 3 吃掉值后的闭引号。authorization 不进 _CREDENTIAL_KEY_NAMES——词表
#   路径会让无引号头被 _SECRET_ASSIGNMENT_RE 二次处理、把方案名当值打码（rejectdiag3 实测回归）。
_AUTHORIZATION_RE = re.compile(
    r"(?i)(\bauthorization\b[\"']?\s*[:=]\s*[\"']?(?:Bearer\s+)?)([^\"'\s,;&]{1,})([\"']?)"
)
# 键名按"完整末尾片段"认，与 settings/user_config_capability.is_credential_key 同口径：键名等于凭据词，
# 或以 `_凭据词`/`-凭据词` 结尾才算，不按子串匹配。原来键名前是 \b，而 `_` 是单词字符，\b 断不开，
# 于是 DB_PASSWORD=hunter22、api_token=abcd1234、MY_SECRET: xyz12345 这些带前缀的键名基线里就不打码。
# 反例必须挡住：max_tokens（复数 token 不以它结尾）、token_count、password_hint 都不能被误打码。
# 两份词表各管一处、不共用：common/ 不能反向依赖 settings/（import boundaries 会拦），且这里还要认
# `-` 连接的写法（命令行/HTTP 头风格），settings 那份只管下划线参数键。
# rejectdiag2：cookie 也是 HTTP 凭据头（服务端回显 Cookie/set-cookie 时不能漏遮），补进词表；
# `set-cookie` 以 `-cookie` 结尾，自动命中。
# rejectdiag3：authorization **不进词表**——实测词表路径会让无引号授权头被 `_SECRET_ASSIGNMENT_RE`
# 二次处理，把方案名当值打码，破坏 rdv2 的“纯变量引用保留”合同；三种键值形状（JSON 双/单引号键、
# 等号赋值）改由 `_AUTHORIZATION_RE` 扩形状覆盖（见该常量注释）。
_CREDENTIAL_KEY_NAMES = (
    r"access_key|access-token|access_token|api-key|api_key|apikey|app_secret|client_secret|cookie|credential|"
    r"encrypt_key|id_token|master_key|password|passwd|private_key|pwd|refresh_token|secret|signature|"
    r"tenant_access_token|ticket|token|verification_token|x-amz-credential|x-amz-signature"
)
# 凭据键名判定与 settings/user_config_capability.is_credential_key 同口径：键名等于凭据词，或以
# `_凭据词`/`-凭据词` 结尾才算。这里另加一层小写归一（settings 那份按原样比），并把点也当分隔符
# （`x.y.token` 这种写法在日志里会被点分开，但键名可能整段带点）。
# 注意词表必须是**纯字面量**：早期版本把正则里的 `api_?key` 直接 split("|") 当集合用，集合里就多了
# `api_?key` 而没有 `api_key`，导致 `api_key = ...` 被判成"不是凭据键"而放过（已修，见下面断言）。
# 两份词表各管一处、不共用：common/ 不能反向依赖 settings/（import boundaries 会拦）。
# 常量用途: 凭据词集合，供 _is_credential_key_name 做等于/后缀判定。
_CREDENTIAL_KEY_NAME_SET = frozenset(_CREDENTIAL_KEY_NAMES.split("|"))
# LLM: 词表不含正则元字符的自检——它同时被正则拼装和集合判定使用，混进 `?` 这类符号会让集合判定
#   静默失效（api_?key 与 api_key 不相等），是"功能悄悄变松"而不是报错。这行在 import 时拦住。
assert not any(character in name for name in _CREDENTIAL_KEY_NAME_SET for character in "?*+[](){}|\\"), (
    "凭据词表必须是纯字面量：它同时用于正则拼装与集合判定"
)


# 函数用途: 判断一个键名是不是凭据键——等于凭据词，或以 _ / - / 点连接后以凭据词结尾。
def _is_credential_key_name(name: str) -> bool:
    lowered = name.lower()
    return any(lowered == word or lowered.endswith(("_" + word, "-" + word, "." + word))
               for word in _CREDENTIAL_KEY_NAME_SET)


# LLM: 只用一个不嵌套的字符类整段取出键名，再在回调里判定——键名前缀原来是 (?:[A-Za-z0-9_.]*[_-])*，
#   嵌套量词且下划线既能被内层也能被外层吃，匹配失败时回溯指数增长（9b 实测 21 个下划线 0.15 秒、
#   29 个 37.7 秒，再长就是小时级；打码函数要处理每段工具输出和每一行日志，Markdown 分隔线、
#   ASCII 表格、日志横线这类长串下划线就能卡住回合与 Gateway 线程）。改成单量词后整体线性。
#   代价是正则不再自己筛掉非凭据键，多调用一次 _is_credential_key_name；这不改变判定口径。
#   键名不设长度上限（9b 建议、3a 采纳）：单个量词 + 前面要求非键名字符，回溯是线性的，加上限反而
#   让超长前缀的凭据键（200 字符前缀 + `-password=`）漏打码，相对 rdv2 是回归。
# 常量用途: 匹配"键名[:=]值"，键名与值都由回调按完整末尾片段决定是否打码。
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)(^|[^\w.-])([A-Za-z0-9_.-]+)(\s*[:=]\s*)([\"']?)"
    r"([^\s,;&\"']{4,})(\4)"
)
# LLM: 与文本赋值规则（_SECRET_ASSIGNMENT_RE）同口径：正则只用不嵌套的单量词取出整段带引号键名，
#   是不是凭据键交给回调里的 _is_credential_key_name 判（同一份词表、同一套末尾片段规则）。
#   原来这里是精确字段名枚举，`{"db_password": ...}`、`{"api-token": ...}` 这类带前缀的 JSON 键
#   一律不打码，与文本规则口径不一致（9b 建议 2 记账的那处差异）。
# 常量用途: 匹配"带引号键名: 带引号值"，键名由回调按完整末尾片段决定是否打码。
_SECRET_JSON_FIELD_RE = re.compile(
    r"(?i)([\"'])([A-Za-z0-9_.-]+)(\1\s*:\s*)([\"'])([^\"'\r\n]{1,})(\4)"
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


# LLM: 键名判定从正则搬进回调，是为了让正则保持单量词、不回溯（见 _SECRET_ASSIGNMENT_RE 注释）。
#   分组固定为 1=前导、2=键名、3=分隔符、4=引号、5=值、6=同 4 的闭引号；键名不是凭据键就整段原样返回，
#   不做任何改写——误伤反例（max_tokens、token_count、password_hint、secretary）走的就是这条分支。
# 函数用途: 判一条"键名[:=]值"要不要打码；要打就把值遮住，不要就原样返回。
def _redact_assignment(match: re.Match[str], marker: str, labels_only: bool) -> str:
    if not _is_credential_key_name(match.group(2)):
        return match.group(0)
    if labels_only:
        return marker
    return match.group(1) + match.group(2) + match.group(3) + match.group(4) + _mask_secret_value(match.group(5), marker) + match.group(6)


# LLM: JSON 字段规则与文本赋值规则同口径：正则只取整段带引号键名，凭据判定交给同一个
#   _is_credential_key_name。分组为 1=键名开引号、2=键名、3=引号+冒号、4=值开引号、5=值、6=闭引号。
#   非凭据键原样返回（`{"max_tokens": 4096}` 这类不能被误伤）；值仍是纯全大写变量引用时，
#   _mask_secret_value 会照 rdv2 的口径保留原文。
# 函数用途: 判一条 "键": "值" 要不要打码；要打就把值遮住，不要就原样返回。
def _redact_json_field(match: re.Match[str], marker: str, labels_only: bool) -> str:
    if not _is_credential_key_name(match.group(2)):
        return match.group(0)
    if labels_only:
        return marker
    return (match.group(1) + match.group(2) + match.group(3) + match.group(4)
            + _mask_secret_value(match.group(5), marker) + match.group(6))


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
            else match.group(1) + _mask_secret_value(match.group(2), redacted_marker) + match.group(3)
        ),
        text,
    )
    if not code_file:
        text = _SECRET_JSON_FIELD_RE.sub(
            lambda match: _redact_json_field(match, redacted_marker, redact_assignment_labels), text)
        text = _SECRET_ASSIGNMENT_RE.sub(
            lambda match: _redact_assignment(match, redacted_marker, redact_assignment_labels), text)
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
