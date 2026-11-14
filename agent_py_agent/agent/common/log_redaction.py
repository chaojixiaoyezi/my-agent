# LLM: 日志、公开投递与模型工具输出共用脱敏；明确秘密字段和PEM即使终态截断也遮蔽，源码专用模式与纯变量引用由原合同保持。
# 模块用途: 在公开输出前隐藏秘密，并避免把源码变量引用和行尾语法误当成密钥删掉。
from __future__ import annotations

"""Process-wide log redaction at the LogRecord creation boundary.

Business modules should log useful structured context without each one having to
remember every credential shape.  This module owns the mandatory last boundary
before a record reaches stderr, journald, a file handler, or a third-party
handler.
"""

import json
import logging
import re
from collections.abc import Mapping
from contextvars import ContextVar
from functools import wraps
from typing import Any

_REDACTED = "<redacted>"
# 结构化输出与内嵌JSON共用同一条有界遍历；只遮蔽过深/循环的子树，不损伤正常同级字段。
_REDACTION_FRAME = ContextVar('redaction_frame', default=(0, frozenset()))
# 结构遍历最多64层；超过时只遮蔽该子树，防止循环或内嵌JSON耗尽解释器栈。
_MAX_REDACTION_LEVEL_COUNT = 64


def _bounded_structure_redaction(function):
    missing_field = object()

    @wraps(function)
    def guarded(value, *, field_name=missing_field, code_file=False, redacted_marker=_REDACTED,
                redact_assignment_labels=False, redaction_events=None):
        depth, active = _REDACTION_FRAME.get()
        container = isinstance(value, (Mapping, list, tuple))
        marker = redacted_marker
        if depth >= _MAX_REDACTION_LEVEL_COUNT or container and id(value) in active:
            return marker
        token = _REDACTION_FRAME.set((depth + 1, active | {id(value)} if container else active))
        try:
            options = {'code_file': code_file, 'redacted_marker': redacted_marker,
                'redact_assignment_labels': redact_assignment_labels, 'redaction_events': redaction_events}
            # 正文脱敏没有field_name参数；未提供时不补传，错误调用仍由原函数明确拒绝。
            if field_name is not missing_field:
                options['field_name'] = field_name
            return function(value, **options)
        except RecursionError:
            # JSON解码器/异常文本也可能先耗尽解释器栈；边界上不回退原始秘密正文。
            return marker
        finally:
            _REDACTION_FRAME.reset(token)
    return guarded
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
        "pass",
        "passphrase",
        "pw",
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
# LLM: 授权头（Authorization / Proxy-Authorization）只用正则定位“键名 + 引号 + 分隔符 + 值开引号”，值的边界交给
#   _authorization_value_end 线性扫描（rejectdiag4）：旧规则只认 Bearer 且值到空白就停，Basic/Token/Digest 的凭据会残留，
#   `"authorization": {...}` 的 `{` 还会被当值吃掉。分组：1=键前引号、2=键名、3=键后引号、4=分隔符、5=值开引号。
#   authorization 不进 _CREDENTIAL_KEY_NAMES——词表路径会让无引号头被 _SECRET_ASSIGNMENT_RE 二次处理（rejectdiag3 回归）。
#   正则只有单层量词，扫描逐字符前进，整体线性（ReDoS 计时见 TESTS.md rejectdiag4 节）。
_AUTHORIZATION_KEY_RE = re.compile(r"(?i)([\"']?)\b((?:proxy-)?authorization)\b([\"']?)(\s*[:=]\s*)([\"']?)")
_COOKIE_KEY_RE = re.compile(r"(?i)([\"']?)\b((?:set-)?cookie)\b([\"']?)(\s*[:=]\s*)([\"']?)")
# 授权值开头的方案名（RFC 7235：credentials = auth-scheme [ 1*SP ( token68 / #auth-param ) ]）；只保留它，其后整段当凭据。
_AUTH_SCHEME_RE = re.compile(r"([A-Za-z][A-Za-z0-9._~+/-]*)(\s+)(\S.*)", re.S)
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
# 等号赋值）改由授权头专用规则覆盖（rejectdiag4 起是 `_AUTHORIZATION_KEY_RE` + `_redact_authorization_values`，见其注释）。
_CREDENTIAL_KEY_NAMES = (
    r"access_key|access-token|access_token|api-key|api_key|apikey|app_secret|client_secret|cookie|credential|"
    r"encrypt_key|id_token|master_key|password|passwd|pass|passphrase|pw|private_key|pwd|refresh_token|secret|signature|"
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
# 预建固定词表的完整分隔后缀，避免每检查一个普通字段就重建三十余个元组。
_CREDENTIAL_KEY_SUFFIXES = tuple(
    separator + word for word in _CREDENTIAL_KEY_NAME_SET for separator in ("_", "-", ".")
)
# LLM: 词表不含正则元字符的自检——它同时被正则拼装和集合判定使用，混进 `?` 这类符号会让集合判定
#   静默失效（api_?key 与 api_key 不相等），是"功能悄悄变松"而不是报错。这行在 import 时拦住。
assert not any(character in name for name in _CREDENTIAL_KEY_NAME_SET for character in "?*+[](){}|\\"), (
    "凭据词表必须是纯字面量：它同时用于正则拼装与集合判定"
)


# 函数用途: 判断一个键名是不是凭据键——等于凭据词，或以 _ / - / 点连接后以凭据词结尾。
def _is_credential_key_name(name: str) -> bool:
    lowered = name.lower()
    return lowered in _CREDENTIAL_KEY_NAME_SET or lowered.endswith(_CREDENTIAL_KEY_SUFFIXES)


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
    r"([^\s,;&\"']{1,})(\4)"
)
# 只定位独立长参数，值由线性扫描读取；不把路径中的 --token 或普通参数名当作凭据。
_CLI_ARGUMENT_RE = re.compile(r'(?<!\S)--([A-Za-z0-9_][A-Za-z0-9_.-]*)(?:[ \t]|\\\r?\n)+')
# LLM: 整体遮蔽引号中的凭据赋值，避免空格和转义引号后的尾段泄露；普通字段保持原文。
# 常量用途: 只匹配凭据键的完整引号值，跨行或未闭合也整段遮蔽，普通外层字符串不吞掉内部匹配。
_SECRET_QUOTED_ASSIGNMENT_RE = re.compile(
    rf"(?i)(^|[^\w.-])((?:[A-Za-z0-9_.-]*[_.-])?(?:{_CREDENTIAL_KEY_NAMES}))(\s*[:=]\s*)([\"'])"
    r"((?:\\[\s\S]|(?!\4)[^\\])*)(\4|$)"
)
# LLM: 键名先沿同一凭据词表限定完整末尾片段，回调仍复核原判定；普通字段不能消费扩展值体，免得吞掉内部待遮秘密。
#   值体各分支互斥：完整转义、EOF孤立反斜杠、非同种引号字符；缺闭引号时只消费到绝对EOF，不伪造JSON修复。
# 常量用途: 遮住明确凭据JSON字段的非空完整或截断引号值，保留原分组、变量引用与源码模式合同。
_SECRET_JSON_FIELD_RE = re.compile(
    rf"(?i)([\"'])((?:[A-Za-z0-9_.-]*[_.-])?(?:{_CREDENTIAL_KEY_NAMES}))(\1\s*:\s*)([\"'])"
    r"((?:\\[\s\S]|\\\Z|(?!\4)[^\\])+)(\4|\Z)"
)
# 空用户名仍可携带口令；两条入口仅放宽用户名，保留冒号、非空密码及 @ 边界。
_URL_PASSWORD_RE = re.compile(
    rf'(\b[A-Za-z][A-Za-z0-9+.-]*://(?:{_SOURCE_URL_SLOT}|{_NOT_SOURCE_URL_SLOT}[^:/?\#\s\"\'`@])*:)'
    rf'((?:{_SOURCE_URL_SLOT}|{_NOT_SOURCE_URL_SLOT}[^/@?\#\s\"\'`])+)(@)'
)
_URL_TEMPLATE_USERINFO_RE = re.compile(rf'(\b[A-Za-z][A-Za-z0-9+.-]*://)({_SOURCE_URL_SLOT})(@)')
_LOG_URL_PASSWORD_RE = re.compile(r'(\b[A-Za-z][A-Za-z0-9+.-]*://[^:/?#\s@]*:)([^@\s]+)(@)')
_SOURCE_STRING_RE = re.compile(r'''(["'])((?:\\.|(?!\1)[^\\\r\n])*)\1''')
_KNOWN_SECRET_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(?:sk-[A-Za-z0-9_-]{10,}|(?:s|r)k_(?:live|test)_[A-Za-z0-9]{10,}|"
    r"github_pat_[A-Za-z0-9_]{10,}|gh[pousr]_[A-Za-z0-9_]{10,}|"
    r"xox[baprs]-[A-Za-z0-9-]{10,}|(?:AKIA|ASIA)[A-Z0-9]{16}|AIza[A-Za-z0-9_-]{30,}|"
    r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}|"
    r"pypi-[A-Za-z0-9_-]{10,}|npm_[A-Za-z0-9]{10,})(?![A-Za-z0-9_-])"
)
# LLM: 明确的PEM/PGP私钥BEGIN即建立秘密边界；完整块沿原END结束，终态缺END则保守遮到绝对EOF，不推断任意正文。
# 常量用途: 隐藏完整或被截断的PEM/OpenSSH/PGP私钥块，普通文本和原私钥标记不变。
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN[A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----[\s\S]*?(?:-----END[A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----|\Z)"
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
        return match.group(1) + marker
    return match.group(1) + match.group(2) + match.group(3) + match.group(4) + _mask_secret_value(match.group(5), marker) + match.group(6)


# LLM: JSON 字段与文本赋值共用原凭据末尾片段判定；正则先限凭据键，避免普通外层字段吞掉内部秘密。
#   分组为1=键名开引号、2=键名、3=引号+冒号、4=值开引号、5=值、6=闭引号或EOF空串；不补造闭引号。
#   非凭据键原样返回（`{"max_tokens": 4096}` 这类不能被误伤）；值仍是带美元符号的纯变量引用时，
#   _mask_secret_value 会照 rdv2 的口径保留原文。
# 函数用途: 判一条 "键": "值" 要不要打码；要打就把值遮住，不要就原样返回。
def _redact_json_field(match: re.Match[str], marker: str, labels_only: bool) -> str:
    if not _is_credential_key_name(match.group(2)):
        return match.group(0)
    if labels_only:
        return marker
    return (match.group(1) + match.group(2) + match.group(3) + match.group(4)
            + _mask_secret_value(match.group(5), marker) + match.group(6))


# LLM: 逐个定位授权键并从值的结束位置继续找下一个，整体线性。值以 `{`/`[` 开头是结构（如 JSON schema 片段）不是凭据，
#   原样保留并从键后继续找（嵌套里的真凭据照样遮）。labels 模式（拒绝诊断摘要）把“键名到值结束（含闭引号）”整段换成标记；
#   普通模式保留方案名，其后整段过 _mask_secret_value（纯变量引用如 `Bearer $TOKEN` 原样保留）。
# 函数用途: 按同一结构扫描遮住 Authorization 或 Cookie 整个值，Cookie 分号后字段不能泄漏。
def _redact_authorization_values(text: str, marker: str, labels_only: bool, *, key_pattern=_AUTHORIZATION_KEY_RE) -> str:
    parts, position = [], 0
    while (match := key_pattern.search(text, position)) is not None:
        # 值不能跨过下一个授权键：扫描上限定在它前面（下一个键另行处理），每段只扫一次，整体线性。
        following = key_pattern.search(text, match.end())
        start = match.end()
        end = _authorization_value_end(text, match, following.start() if following is not None else len(text))
        value = text[start:end]
        if not value.strip() or value.lstrip()[:1] in "{[":
            parts.append(text[position:match.end()])
            position = match.end()
            continue
        closing = 1 if match.group(5) and text[end:end + 1] == match.group(5) else 0
        if labels_only:
            parts.append(text[position:match.start(2)] + marker)
        else:
            parts.append(text[position:start] + _mask_authorization_value(value, marker) + text[end:end + closing])
        position = end + closing
    parts.append(text[position:])
    return "".join(parts)


# LLM: 值边界只看结构字符，不读语义：值有开引号扫到同种未转义引号；键带引号而值裸露是 JSON 标量，到 `,}]` 或空白；
#   等号赋值到空白或 `&;,` 与引号；其余是请求头形状，到换行或未转义引号为止（嵌在字符串里的 curl -H "...: Bearer $T"
#   在闭引号前停，不吞后面的 URL）；裸露请求头行里 `name="..."` 形式的 Digest 参数值整段跳过。反斜杠转义一律跳过下一个字符。
# 函数用途: 返回授权值在文本里的结束下标（不含闭引号），不超过 limit（下一个授权键的起点）。
def _authorization_value_end(text: str, match: re.Match[str], limit: int) -> int:
    index = match.end()
    stops, param_quotes = _authorization_value_stops(match)
    while index < limit:
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if param_quotes and char == '"' and text[index - 1] == "=":
            closing = _unescaped_quote(text, index + 1, '"', limit)
            index = limit if closing < 0 else closing + 1
            continue
        if char in stops:
            return index
        index += 1
    return limit


# LLM: 按键值形状选值的结束字符与是否跳 Digest 参数引号（见 _authorization_value_end 注释）；只读分组，不扫正文。
# 函数用途: 返回（结束字符集合, 是否按 name="..." 跳引号）。
def _authorization_value_stops(match: re.Match[str]) -> tuple[str, bool]:
    if match.group(5):
        return match.group(5) + "\r\n", False
    if match.group(3):
        return ",}] \t\r\n", False
    if "=" in match.group(4):
        return " \t\r\n&;,\"'", False
    # 只有裸露的请求头行才按 Digest `name="..."` 跳引号；嵌在引号字符串里时参数引号必然已转义，外层闭引号就是边界
    #   （否则 Basic 值末尾的 base64 填充 `=` 紧挨闭引号会被误当参数，越界吞掉后面的键）。
    return "\r\n\"'", not match.group(1)


# LLM: 只服务 _authorization_value_end 的 Digest 参数跳过；遇换行或到 limit 视为未闭合，返回 -1 由调用方按到 limit 处理。
# 函数用途: 从 start 起、limit 之前找下一个未被反斜杠转义的指定引号的位置。
def _unescaped_quote(text: str, start: int, quote: str, limit: int) -> int:
    index = start
    while index < limit:
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == quote:
            return index
        if char in "\r\n":
            return -1
        index += 1
    return -1


# LLM: 方案名之后（Basic/Token/Digest/Bearer… 都一样）整段当凭据；没有“方案名 + 空白 + 内容”形状时整段遮。
# 函数用途: 生成授权值的打码形式：保留方案名，凭据部分交 _mask_secret_value。
def _mask_authorization_value(value: str, marker: str) -> str:
    scheme = _AUTH_SCHEME_RE.fullmatch(value)
    if scheme is None:
        return _mask_secret_value(value, marker)
    return scheme.group(1) + scheme.group(2) + _mask_secret_value(scheme.group(3), marker)


def _redact_cli_arguments(text: str, marker: str, labels_only: bool, *, events=None) -> str:
    """遮蔽敏感长参数的整个Shell词；引号、转义和截断只扫一遍，不执行或解析命令。"""
    parts, position = [], 0
    context_position, host_quote = 0, ''
    markers = tuple(value for value in dict.fromkeys(('<redacted>', '[REDACTED]', marker)) if value)
    marker_forms = tuple(form for value in markers for form in (
        value, "'" + value + "'", '"' + value + '"', "\\'" + value + "\\'", '\\"' + value + '\\"'))
    while (match := _CLI_ARGUMENT_RE.search(text, position)) is not None:
        # 单调扫描原文本前缀，识别repr/JSON等宿主字符串；不能每个参数重新扫整段。
        while context_position < match.start():
            char = text[context_position]
            if char == '\\':
                context_position += 2
                continue
            if host_quote:
                if char == host_quote:
                    host_quote = ''
            elif char in "\"'":
                host_quote = char
            context_position += 1
        start = match.end()
        if not _field_is_sensitive(match.group(1)):
            parts.append(text[position:start])
            position = start
            continue
        end, quote = start, ''
        closed_marker_end = next((start + len(value) for value in marker_forms
                                  if text.startswith(value, start)), -1)
        # 占位符可能来自不可信正文；与其紧邻的字面值仍属同一敏感词，不能借尖括号绕过扫描。
        marker_word = any(text.startswith(value, start) for value in markers)
        if marker_word:
            end += len(next(value for value in markers if text.startswith(value, start)))
        encoded_quote = text[start:start + 2] in ('\\"', "\\'")
        if encoded_quote:
            quote = text[start + 1]
            end += 2
        while end < len(text):
            char = text[end]
            # 仅完整独立占位符可在已知宿主闭引号前结束，保留源码/JSON语法；
            # 紧邻字面值或未闭合秘密不具此资格，仍按原Shell词保守遮蔽。
            if (end == closed_marker_end and host_quote and char == host_quote
                    and (end + 1 == len(text) or text[end + 1] in ' \t\r\n,]})')):
                break
            if encoded_quote and quote and char == '\\':
                closing = end
                while closing < len(text) and text[closing] == '\\':
                    closing += 1
                if closing < len(text) and text[closing] == quote:
                    # JSON中的三个反斜杠表示Shell词内的转义引号，不是词的结束。
                    if (closing - end) % 4 == 1:
                        quote = ''
                    end = closing + 1
                else:
                    end = closing
                continue
            if char == '\\' and quote != "'":
                end = min(len(text), end + 2)
                continue
            if quote:
                if char == quote:
                    quote = ''
            elif char in "\"'":
                quote = char
            elif char.isspace() or char in ';|&' or char in '<>' and not marker_word:
                break
            end += 1
        if end == start:
            parts.append(text[position:start])
            position = start
            continue
        value = text[start:end]
        if quote and end == len(text) and events is not None:
            # 只记录词法事实与范围，不记录参数值；未闭合秘密仍保守遮到当前文本末尾。
            events.append({'code': 'UNCLOSED_CREDENTIAL_ARGUMENT',
                'start': match.start(), 'end': end, 'hidden_through': 'end_of_text'})
        # JSON日志会转义外层引号；只有完整纯环境变量引用仍保留原字节。
        pure_reference = (encoded_quote and value[-2:] == value[:2]
            and _PURE_VARIABLE_REFERENCE_RE.fullmatch(value[2:-2]) is not None)
        parts.append(text[position:match.start() if labels_only else start])
        parts.append(marker if labels_only else value if pure_reference else _mask_secret_value(value, marker))
        position = end
    parts.append(text[position:])
    return ''.join(parts)


# LLM: 调用方决定是否为源码；插值内字面量和实际 URL 凭证仍须遮蔽，已知密钥扫描始终执行。
# 函数用途: 返回脱敏副本，不写文件；保持源码结构和日志安全边界各自生效。
def redact_sensitive_text(
    value: object,
    *,
    code_file: bool = False,
    redacted_marker: str = _REDACTED,
    redact_assignment_labels: bool = False,
    redaction_events: list[dict[str, object]] | None = None,
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
    # 长命令参数先按词边界遮蔽，避免后续通用表达式对同一大段秘密反复扫描。
    if not code_file:
        text = _redact_cli_arguments(text, redacted_marker, redact_assignment_labels,
                                     events=redaction_events)
    query_pattern = _SENSITIVE_QUERY_RE if code_file else _LOG_SENSITIVE_QUERY_RE
    text = query_pattern.sub(
        lambda match: (
            redacted_marker
            if redact_assignment_labels
            else match.group(1) + _redact_url_value(match.group(2), code_file, redacted_marker)
        ),
        text,
    )
    text = _redact_authorization_values(text, redacted_marker, redact_assignment_labels)
    text = _redact_authorization_values(text, redacted_marker, redact_assignment_labels, key_pattern=_COOKIE_KEY_RE)
    if not code_file:
        text = _SECRET_JSON_FIELD_RE.sub(
            lambda match: _redact_json_field(match, redacted_marker, redact_assignment_labels), text)
        text = _SECRET_QUOTED_ASSIGNMENT_RE.sub(
            lambda match: _redact_assignment(match, redacted_marker, redact_assignment_labels), text)
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


@_bounded_structure_redaction
def redact_sensitive_value(
    value: Any,
    *,
    field_name: str = "",
    code_file: bool = False,
    redacted_marker: str = _REDACTED,
    redact_assignment_labels: bool = False,
    redaction_events: list[dict[str, object]] | None = None,
) -> Any:
    """Recursively redact strings and values under credential-named fields."""

    if _field_is_sensitive(field_name):
        return redacted_marker
    if isinstance(value, str):
        return redact_sensitive_output_text(value, code_file=code_file, redacted_marker=redacted_marker,
            redact_assignment_labels=redact_assignment_labels, redaction_events=redaction_events)
    if isinstance(value, BaseException):
        return redact_sensitive_text(value, code_file=code_file, redacted_marker=redacted_marker,
            redact_assignment_labels=redact_assignment_labels, redaction_events=redaction_events)
    if isinstance(value, Mapping):
        return {
            key: redact_sensitive_value(
                item,
                field_name=str(key),
                code_file=code_file,
                redacted_marker=redacted_marker, redact_assignment_labels=redact_assignment_labels,
                redaction_events=redaction_events,
            )
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        return tuple(
            redact_sensitive_value(item, code_file=code_file, redacted_marker=redacted_marker,
                redact_assignment_labels=redact_assignment_labels, redaction_events=redaction_events)
            for item in value
        )
    if isinstance(value, list):
        return [
            redact_sensitive_value(item, code_file=code_file, redacted_marker=redacted_marker,
                redact_assignment_labels=redact_assignment_labels, redaction_events=redaction_events)
            for item in value
        ]
    return value


@_bounded_structure_redaction
def redact_sensitive_output_text(value: object, *, code_file: bool = False,
        redacted_marker: str = _REDACTED, redact_assignment_labels: bool = False,
        redaction_events: list[dict[str, object]] | None = None) -> str:
    """复用原字段脱敏处理完整 JSON；反复投影也不把转义引号当跨字段秘密边界。"""
    text = str(value)
    if not code_file and text.lstrip().startswith(('{', '[')):
        try:
            parsed = json.loads(text)
        except RecursionError:
            return redacted_marker
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, (dict, list)):
            events = [] if redaction_events is not None else None
            safe = redact_sensitive_value(parsed, redacted_marker=redacted_marker,
                redact_assignment_labels=redact_assignment_labels, redaction_events=events)
            if events is not None:
                redaction_events.extend({**event, 'hidden_through': 'end_of_string_field'} for event in events)
            return text if safe == parsed else json.dumps(safe, ensure_ascii=False)
    return redact_sensitive_text(text, code_file=code_file, redacted_marker=redacted_marker,
        redact_assignment_labels=redact_assignment_labels, redaction_events=redaction_events)


# LLM: 结构字段与文本赋值采用同一完整凭据后缀，不按任意子串判断，操作计数与编号不能被误删。
# 函数用途: 判断一个结构字段是否承载凭据，覆盖带业务前缀的密码字段。
def _field_is_sensitive(name: str) -> bool:
    normalized = str(name or "").strip().lower().replace("-", "_")
    return normalized in _SENSITIVE_FIELD_NAMES or _is_credential_key_name(normalized)


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
