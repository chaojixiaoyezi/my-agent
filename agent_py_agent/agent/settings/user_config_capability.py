# LLM: 本模块是"哪些配置项模型/用户可以自助改"的**唯一权威**，CLI(`config-set`)与模型工具都从这里取名单、
#   校验和生效语义，不得各自维护第二份。安全边界（权限模式、危险根、凭据、模型密钥、宿主控制面路径）
#   永不进白名单：不是"暂时没开放"，而是结构上不允许模型自行改变。
#   生效语义必须如实报告：当前进程持有启动时加载的 AgentConfig，写盘后要等下一轮/下一次会话/网关重启，
#   不能声称"已经生效"。
# 模块用途: 给用户与模型提供受控、可校验、能报告来源与生效时机的配置读写能力。
from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from ._memory_coercion import COMPACT_RECOVERY_PERCENT_RANGE, COMPACT_TRIGGER_PERCENT_RANGE
from .config_io import load_simple_yaml

# 生效时机（如实报告，不夸大）
EFFECT_NEXT_SESSION = "next_session"
EFFECT_GATEWAY_RESTART = "restart_gateway"

_EFFECT_TEXT = {
    EFFECT_NEXT_SESSION: "保存后对**下一次新建会话**生效；当前正在跑的会话仍用已加载的值。",
    EFFECT_GATEWAY_RESTART: ("保存后需要重启 Gateway 才生效（命令行会话下次启动时读取）；当前进程不会热加载。"
                             "管理员可以发 /restart 或让 my-agent 安全重启 Gateway。"),
}


# 函数用途: 返回某个生效时机给人看的说明文字。
def effect_text(effect: str) -> str:
    return _EFFECT_TEXT.get(effect, "")


# LLM: 一个可自助修改项＝键名 + 人类说明 + 纯函数校验 + 生效时机。校验只做区间/枚举等客观判断，
#   不解析自然语言，也不接受空值含糊放行。
# 类用途: 描述一个用户可自助修改的配置项及其合法取值。
@dataclass(frozen=True)
class TunableSpec:
    key: str
    describe: str
    validate: Callable[[object], tuple[bool, str, object]]
    effect: str = EFFECT_NEXT_SESSION


# LLM: 每个百分比项按自己的有效范围校验，范围取自配置解析的同一常量；接受的值必须原样生效，不能再被运行时夹取成别的值。
# 函数用途: 生成一个只接受 [low, high] 内整数百分比的校验函数，供自助修改白名单使用。
def _percent_within(bounds: tuple[int, int]) -> Callable[[object], tuple[bool, str, object]]:
    low, high = bounds
    message = f"必须是 {low}~{high} 之间的整数百分比"

    # LLM: 纯函数，只做整数与区间判断，不解析自然语言。
    # 函数用途: 校验一次自助修改提交的百分比取值。
    def validate(value: object) -> tuple[bool, str, object]:
        try:
            number = int(str(value).strip())
        except (TypeError, ValueError):
            return False, message, 0
        if not low <= number <= high:
            return False, message, 0
        return True, "", number

    return validate


def _positive_int(value: object) -> tuple[bool, str, object]:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return False, "必须是正整数", 0
    if number <= 0:
        return False, "必须是正整数", 0
    return True, "", number


# LLM: 与配置解析（_memory_coercion 的 int 字段，下限 0、无上限）和运行时 compact_trigger_max_tokens 同一口径：
#   接受的值必须原样生效，所以只拒非整数和负数，不另设上限；0 表示不封顶。
# 函数用途: 校验一次自助修改提交的非负整数（比如压缩触发线的绝对 token 上限）。
def _non_negative_int(value: object) -> tuple[bool, str, object]:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return False, "必须是 0 或正整数（0 表示不封顶）", 0
    if number < 0:
        return False, "必须是 0 或正整数（0 表示不封顶）", 0
    return True, "", number


def _non_empty_text(value: object) -> tuple[bool, str, object]:
    text = str(value or "").strip()
    if not text:
        return False, "不能为空", ""
    return True, "", text


# LLM: 白名单宁窄勿宽：只放"用户自己的偏好/运行节奏"，不放任何影响权限、路径、凭据、模型路由或宿主控制面的项。
# 常量用途: 声明可自助修改的配置项。
TUNABLE_KEYS: dict[str, TunableSpec] = {
    "memory_compact_auto_trigger_percent": TunableSpec(
        key="memory_compact_auto_trigger_percent",
        describe="上下文用到百分之多少时自动 compact（50~100，默认 90）",
        validate=_percent_within(COMPACT_TRIGGER_PERCENT_RANGE),
        effect=EFFECT_GATEWAY_RESTART,
    ),
    "memory_compact_recovery_target_percent": TunableSpec(
        key="memory_compact_recovery_target_percent",
        describe="compact 之后的健康目标百分比（25~80，默认 60）",
        validate=_percent_within(COMPACT_RECOVERY_PERCENT_RANGE),
        effect=EFFECT_GATEWAY_RESTART,
    ),
    "memory_compact_auto_trigger_max_tokens": TunableSpec(
        key="memory_compact_auto_trigger_max_tokens",
        describe="自动 compact 触发线的绝对 token 上限（0 表示不封顶，默认 0；大于 0 时触发线取「窗口 × 百分比」和它的较小值）",
        validate=_non_negative_int,
        effect=EFFECT_GATEWAY_RESTART,
    ),
    "tool_read_max_chars": TunableSpec(
        key="tool_read_max_chars",
        describe="单次读文件返回的最大字符数",
        validate=_positive_int,
        effect=EFFECT_GATEWAY_RESTART,
    ),
    "feishu_app_id": TunableSpec(
        key="feishu_app_id",
        describe="飞书应用 App ID（接入飞书通道用）",
        validate=_non_empty_text,
        effect=EFFECT_GATEWAY_RESTART,
    ),
    "feishu_app_secret": TunableSpec(
        key="feishu_app_secret",
        describe="飞书应用 App Secret（写入时不回显明文）",
        validate=_non_empty_text,
        effect=EFFECT_GATEWAY_RESTART,
    ),
    "feishu_verification_token": TunableSpec(
        key="feishu_verification_token",
        describe="飞书事件订阅 Verification Token",
        validate=_non_empty_text,
        effect=EFFECT_GATEWAY_RESTART,
    ),
    "feishu_encrypt_key": TunableSpec(
        key="feishu_encrypt_key",
        describe="飞书事件订阅 Encrypt Key",
        validate=_non_empty_text,
        effect=EFFECT_GATEWAY_RESTART,
    ),
    "feishu_callback_port": TunableSpec(
        key="feishu_callback_port",
        describe="飞书回调监听端口",
        validate=_positive_int,
        effect=EFFECT_GATEWAY_RESTART,
    ),
}

# LLM: 拒绝名单是**结构性**的：这些项要么决定谁能读写什么（权限/路径），要么是宿主或供应商凭据，
#   要么属于宿主控制面。模型永远不能自行改；用户要改就走宿主入口或人工编辑。
# 常量用途: 声明模型不可自行修改的配置项与原因。
BOUNDARY_KEYS: dict[str, str] = {
    "access_mode": "决定工具/命令的权限档位，属于安全边界",
    "path_access_mode": "决定路径访问模式（normal/full），属于安全边界",
    "path_dangerous_roots": "决定危险目录清单，属于安全边界",
    "shell_sandbox_hide_user_home": "决定 Shell 沙箱能否读用户家目录，属于安全边界",
    "api_key": "供应商凭据，不能由模型改写",
    "api_key_env": "供应商凭据来源，不能由模型改写",
    "my_agent_home": "宿主数据根，属于宿主控制面",
    "my_agent_owner_provider": "owner 身份解析，属于宿主控制面",
    "my_agent_owner_kind": "owner 身份解析，属于宿主控制面",
    "my_agent_owner_id": "owner 身份解析，属于宿主控制面",
}

# 凭据名只按键名最后的完整片段认：input_media_token_reserve 里的 token 是计数单位，max_tokens 是复数，都不是凭据。
# 常见缩写与组合（DB_PASS、MYSQL_PWD、SSH_PRIVATE_KEY、AWS_SECRET_ACCESS_KEY、BASIC_AUTH）同样按完整末尾片段认；
# 2026-09-27 核对登记表：补上这些写法没有新命中任何参数（auth_enabled、access_mode 不以它们结尾），边界分类不变。
_CREDENTIAL_SUFFIXES = (
    "api_key", "secret", "password", "token", "cookie", "cookies", "credential", "credentials", "encrypt_key",
    "pass", "passwd", "pwd", "private_key", "secret_key", "access_key", "auth",
)


# LLM: 参数中心登记表、聊天 /settings、user_config 与命令行 config-get 共用这一条凭据判定，不再各自维护名单；
#   键名等于凭据名或以 `_凭据名` 结尾才算（完整片段，不按子串）。改规则须同步 test_parameter_registry 的脱敏用例。
# 函数用途: 判断一个配置键是不是凭据（回显、记账都必须脱敏，也永远是安全边界）。
def is_credential_key(key: object) -> bool:
    text = str(key or "")
    return any(text == name or text.endswith("_" + name) for name in _CREDENTIAL_SUFFIXES)


# 映射里“值一律遮住、只留键名”的容器：请求头与环境变量。按容器名的最后一个片段认（model_custom_headers、env），
# 不按请求头名或变量名写死名单——名字是开放的，容器的角色才是结构事实。
_SECRET_CONTAINERS = frozenset({"headers", "header", "env", "environ", "environment"})
_MASK = "***"
# 值本身形如密钥的通用格式前缀（跨服务商：OpenAI/Anthropic/GitHub/Slack/JWT/AWS 等令牌都以这些开头）。
# 只认格式标识，不写死某家服务商；命中即把“名=值”里的值遮住。
_TOKEN_PREFIXES = ("sk-", "sk_", "ghp_", "gho_", "xoxb-", "xoxp-", "xoxa-", "eyj", "akia", "aiza",
                   "ya29.", "glpat-", "github_pat_", "hf_", "rpk_", "nvapi-")
# 列表里一项的“请求头行”或“名字=值”形状：名字由 HTTP token 字符组成，后接 = 或 :（: 后紧跟 // 的是网址，交给网址规则）
_ENTRY_SHAPE = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+(?:=|:(?!//))")
# 文本里空白分隔的“名字=值”：名字在开头、空白或引号之后（网址查询串里的 ?token= 交给网址规则）；
# 值到下一个空白为止，引号开头的值到配对引号为止（libpq 的 password='a b'）
_TEXT_PAIR = re.compile(r"""(?<![^\s"'])(-*[A-Za-z_][\w.-]*)=('[^']*'?|"[^"]*"?|\S+)""")
# 文本里的网址：scheme:// 起，到下一个空白为止（修改记录外层的 YAML 引号已先剥掉）
_URL_IN_TEXT = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://\S+")


# LLM: 小写、连字符与点当下划线、去掉开头的下划线，让 X-Api-Key、--api-key、db.password 与 api_key、password 走同一条凭据判定。
# 函数用途: 把键名、请求头名、命令行参数名规范成凭据判定用的形式。
def _plain_name(value: object) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(".", "_").lstrip("_")


# LLM: 空值不算泄露，原样返回；文本保留前 3 个字符便于认出是哪个凭据（与原顶层凭据回显一致）；映射（如 auth: {type, token}）
#   保留键名、值逐个遮住，结构仍看得见；其它类型整体换成 ***。
# 函数用途: 把一个凭据位置上的值遮住。
def _masked_leaf(value: object) -> object:
    if value is None or value == "":
        return value
    if isinstance(value, Mapping):
        return {key: _masked_leaf(item) for key, item in value.items()}
    if isinstance(value, str):
        return f"{value[:3]}{_MASK}" if len(value) > 3 else _MASK
    return _MASK


# LLM: 只看查询参数名（规范化后按 is_credential_key 认），名字不是凭据或值为空时原样返回；由 _masked_url 逐对调用。
# 函数用途: 查询参数名是凭据时（如 ?token=、&api_key=）遮住它的值。
def _masked_query_pair(pair: str) -> str:
    name, separator, value = pair.partition("=")
    return f"{name}={_masked_leaf(value)}" if separator and value and is_credential_key(_plain_name(name)) else pair


# LLM: 只由 _masked_text 对文本里找出的每个网址调用。只动“scheme://用户:密码@主机”里的密码和名字是凭据的查询参数值；
#   没有要遮的内容时原文返回（不重新拼接网址），解析失败也原文返回，不猜。
# 函数用途: 遮住网址里夹带的密码与令牌（如带密码的代理地址、带 ?token= 的回调地址）。
def _masked_url(text: str) -> str:
    try:
        parts = urlsplit(text)
        password = parts.password
    except ValueError:
        return text
    pairs = parts.query.split("&") if parts.query else []
    masked_pairs = [_masked_query_pair(pair) for pair in pairs]
    if not password and masked_pairs == pairs:
        return text
    netloc = parts.netloc.replace(f":{password}@", f":{_MASK}@", 1) if password else parts.netloc
    return urlunsplit(parts._replace(netloc=netloc, query="&".join(masked_pairs)))


# LLM: 名字是凭据时遮住整个值；否则值本身再按自由文本处理——引号括起的值里还可能有 password=…，值也可能是网址。
#   值自身形如密钥（长随机串、常见 token 前缀）也遮，规则只看结构（长度、字符集、前缀），不写死服务商。
# 函数用途: 处理文本里的一处“名字=值”。
def _masked_text_pair(match: re.Match) -> str:
    name, value = match.group(1), match.group(2)
    if is_credential_key(_plain_name(name)) or _looks_like_secret(value):
        return f"{name}={_masked_leaf(value)}"
    return f"{name}={_masked_text(value)}"


# LLM: 不跟在开关后面、名字也不像凭据的“名=值”里，值如果是密钥形态也要遮：长随机串（>=24 字符、无空白、
#   无路径/网址特征、字符集限于字母数字和常见 token 符号、且混用字母与数字或大小写）或常见 token 前缀。
#   规则只看结构，普通值（主机名、纯数字、路径、网址、带空格句子）不遮。
# 函数用途: 判断一段“名=值”的值是不是形如密钥，需要整体遮住。
def _looks_like_secret(text: object) -> bool:
    if not isinstance(text, str) or not text:
        return False
    low = text.lower()
    if any(low.startswith(prefix) for prefix in _TOKEN_PREFIXES):
        return True
    if len(text) < 24 or any(char.isspace() for char in text) or "://" in text:
        return False
    if "/" in text or "\\" in text or "@" in text:
        return False
    if not re.fullmatch(r"[A-Za-z0-9._~+=\-]+", text):
        return False
    has_alpha = any(char.isalpha() for char in text)
    has_digit = any(char.isdigit() for char in text)
    has_mixed_case = any(char.isupper() for char in text) and any(char.islower() for char in text)
    return has_alpha and (has_digit or has_mixed_case)


# LLM: 顶层文本、映射里的文本和列表项最后都走这里：先按空白拆出“名字=值”，名字是凭据的遮值（libpq 的 password=…），
#   再找出每个网址遮密码与凭据查询参数。修改记录里的文本值带 YAML 引号，先剥掉外层引号再处理。没有要遮的内容时原文返回。
# 函数用途: 对一段自由文本脱敏。
def _masked_text(text: str) -> str:
    if len(text) > 1 and text[0] == text[-1] and text[0] in "\"'":
        return text[0] + _masked_text(text[1:-1]) + text[0]
    paired = _TEXT_PAIR.sub(_masked_text_pair, text)
    return _URL_IN_TEXT.sub(lambda match: _masked_url(match.group(0)), paired)


# LLM: 按规范化名字的最后一段认（headers/header/env/environ/environment），不按请求头名或变量名写死名单；映射与开关共用。
# 函数用途: 判断一个键名或开关名是不是请求头/环境变量容器（model_custom_headers、env、--header、--env）。
def _is_secret_container(name: object) -> bool:
    return _plain_name(name).split("_")[-1] in _SECRET_CONTAINERS


# LLM: 请求头、环境变量容器里的值一律遮住只留键名；其它映射里键名命中 is_credential_key 的值遮住，其余按键名递归。
# 函数用途: 返回脱敏后的映射副本。
def _masked_mapping(name: str, value: Mapping) -> dict:
    hide_all = _is_secret_container(name)
    return {key: _masked_leaf(item) if hide_all or is_credential_key(_plain_name(key)) else _masked_node(str(key), item)
            for key, item in value.items()}


# LLM: 请求头或环境变量写成一项文本（"Authorization: Bearer …"、"GITHUB_TOKEN=…"）时，从最早的 : 或 = 切开只留名字；
#   没有分隔符说明这一项本身就是名字（docker 的 --env NAME 从宿主继承值），原样返回。
# 函数用途: 把一条文本形式的请求头或环境变量遮到只剩名字。
def _masked_entry(text: str) -> str:
    cut = min((index for index in (text.find(":"), text.find("=")) if index > 0), default=-1)
    return text if cut < 0 else f"{text[:cut + 1]}{_masked_leaf(text[cut + 1:])}"


# LLM: 按开关名或“名字=值”里名字的角色遮值：凭据名（--api-key、GITHUB_TOKEN）遮整个值，请求头/环境变量容器（--header、--env）
#   只留值里的名字；没有特殊角色返回 None，由调用方继续按形状和自由文本处理。
# 函数用途: 按名字的角色遮住它带的值。
def _masked_by_role(name: str, value: str) -> str | None:
    if is_credential_key(name):
        return str(_masked_leaf(value))
    return _masked_entry(value) if _is_secret_container(name) else None


# LLM: 带 = 的开关（--token=…）规范化后是 token=…，既不是凭据名也不是容器名，不会误把下一项当值遮住。
# 函数用途: 前一项是开关（以 - 开头）时返回规范化的开关名，否则返回空串。
def _flag_name(value: object) -> str:
    return _plain_name(value) if isinstance(value, str) and value.startswith("-") else ""


# LLM: 一项自身是“名字=值”且名字有角色时整项都是值（--token=…、docker 的 GITHUB_TOKEN=…、--env=NAME=…、--header=名字: 值，
#   值里的空格属于同一个参数）；名字没有角色时整项按自由文本处理（空白分隔的“名字=值”、网址）。
# 函数用途: 按列表里一项文本自身的内容脱敏。
def _masked_argument(item: str) -> str:
    name, separator, rest = item.partition("=")
    by_role = _masked_by_role(_plain_name(name), rest) if separator else None
    return _masked_text(item) if by_role is None else f"{name}={by_role}"


# LLM: 开关后面的一项先看开关：凭据开关（--api-key、--pass）整项遮住，请求头/环境变量开关（--header、--env）只留名字；
#   任意开关（含单字母 -H、-e）后面形状是“名字: 值”或“名字=值”的一项也只留名字——形状是结构事实，不需要知道开关在
#   各个程序里的含义，宁可多遮（-e LOG_LEVEL=debug、--addr host:8080 也会遮值）。其余交给 _masked_argument，非文本项按结构递归。
# 函数用途: 按前一个参数和这一项的形状决定列表里这一项怎么脱敏。
def _masked_item(item: object, previous: object) -> object:
    if not isinstance(item, str):
        return _masked_node("", item)
    flag = "" if item.startswith("-") else _flag_name(previous)
    by_flag = _masked_by_role(flag, item)
    if by_flag is None and flag and _ENTRY_SHAPE.match(item):
        by_flag = _masked_entry(item)
    return _masked_argument(item) if by_flag is None else by_flag


# LLM: 列表按项脱敏（开关后一项、“名字=值”、自由文本、网址、嵌套映射），保持原来是列表还是元组；
#   请求头/环境变量容器开关（--headers/--header/--env）后“名字 值”分两项写时两项都遮（名字可能是 Authorization/x-api-key 这类
#   敏感请求头名）；凭据名开关（--api-key、--pass）仍是单值写法，只遮值，不能把后面的参数误当“值”。
# 函数用途: 返回脱敏后的列表副本（如 mcp_servers 的 args）。
def _masked_sequence(items: Sequence) -> list | tuple:
    masked: list[object] = []
    index = 0
    while index < len(items):
        item = items[index]
        previous = items[index - 1] if index else None
        if (_is_secret_container(previous)
                and isinstance(item, str) and not item.startswith("-")
                and not _ENTRY_SHAPE.match(item)
                and index + 1 < len(items)
                and not (isinstance(items[index + 1], str) and items[index + 1].startswith("-"))):
            masked.append(_masked_leaf(item))
            masked.append(_masked_leaf(items[index + 1]))
            index += 2
        else:
            masked.append(_masked_item(item, previous))
            index += 1
    return tuple(masked) if isinstance(items, tuple) else masked


# LLM: 映射、列表递归，文本按自由文本处理（名字是凭据的“名字=值”、网址），其它标量原样返回；
#   name 是这个值所在的键名，用来认出请求头/环境变量容器。
# 函数用途: 对一个非凭据位置的值做结构脱敏。
def _masked_node(name: str, value: object) -> object:
    if isinstance(value, Mapping):
        return _masked_mapping(name, value)
    if isinstance(value, (list, tuple)):
        return _masked_sequence(value)
    return _masked_text(value) if isinstance(value, str) else value


# LLM: 所有回显与记账出口的唯一脱敏入口：顶层键是凭据就整值遮住；否则按结构递归——请求头/环境变量映射只留键名，
#   嵌套键名是凭据的遮值，命令行里凭据开关的值遮住，--header/--env 以及任意开关后形状是“名字: 值”“名字=值”的一项只留名字，
#   文本里空白分隔的“名字=值”名字是凭据的遮值（libpq 的 password=…），网址里的密码与凭据查询参数遮值。返回同形副本，不改原值。
#   调用方必须传原始值（dict/list），先 str() 再传就看不见结构。改规则须同步 test_structured_masking。
# 函数用途: 返回某个配置值脱敏后的同形副本。
def masked_structure(key: object, value: object) -> object:
    return _masked_leaf(value) if is_credential_key(_plain_name(key)) else _masked_node(str(key or ""), value)


# LLM: 文本回显出口：结构脱敏后再转文本。user_config 的查看/搜索/改参回执、聊天 /settings、config get 共用这一处口径：
#   非凭据的布尔按配置文件写法显示 true/false，数字照实显示（False、0 不是空值）；None、空串、空列表、空映射显示空串；
#   凭据键整值遮住。原先 `str(x or "")` 把 False、0 也给成空串，模型读到的运行值与配置不符。
#   原先只看顶层键名、再把整个值 str()，model_custom_headers、mcp_servers 的 env 会整段明文回显。
#   改口径须同步 test_value_display_parity 与 test_structured_masking。
# 函数用途: 回显时对凭据脱敏，并把配置值排成各出口一致的文字，不把明文写回终端、日志或模型上下文。
def mask_value(key: str, value: object) -> str:
    masked = masked_structure(key, value)
    if isinstance(masked, bool):
        return "true" if masked else "false"
    if isinstance(masked, (int, float)):
        return str(masked)
    return str(masked or "")


# LLM: 非 agent 来源的读取文件路径按 source 解析：capability 的用户文件是运行时实际读取的那份（capability_path，
#   调用方按 capability_config_for_agent 同一路径解析）；runtime_guard 没有用户覆盖层，只有随包默认。
#   早返回压平嵌套：主配置/未知来源直接回原路径。
# 函数用途: 按参数来源给出读取用户值与随包默认值要用的两个文件路径。
def _fact_paths(
    spec: object, *, user_path: Path | None, default_path: Path, capability_path: Path | None
) -> tuple[Path | None, Path]:
    if spec is None or getattr(spec, "source", "agent") == "agent":
        return user_path, default_path
    source = spec.source
    base = Path(default_path).parent
    packaged = base / f"{source}_config.yaml"
    if source == "capability":
        return capability_path, packaged
    if user_path is not None:
        return Path(user_path).with_name(f"{source}_config.yaml"), packaged
    return None, packaged


# LLM: 生效值＝用户配置文件里的值优先；没写才回落到随包默认 YAML。报告必须同时给出两者与来源，
#   否则模型会把随包默认当成"用户配置"（真机：模型去读安装目录里的 agent_config.yaml 当答案）。
#   能不能改只由参数中心登记表的 writable 回答；这里不再给旧白名单的 tunable 字段（09-27 模型见 writable=true、
#   tunable=false 两个口径，误以为 max_tokens 改不了）。P17 起按登记表的 source 选文件：主配置用传入的
#   user_path/default_path；capability 的用户文件是运行时实际读取的那份（capability_path，调用方按
#   capability_config_for_agent 同一路径解析）；runtime_guard 没有用户覆盖层，只有随包默认。
# 函数用途: 读取某个键的用户值与随包默认值，并给出当前生效值与来源。
def read_config_fact(
    key: str, *, user_path: Path | None, default_path: Path, capability_path: Path | None = None
) -> dict[str, object]:
    name = str(key or "").strip()
    source = "agent"
    from .parameter_registry import parameter_registry

    spec = parameter_registry().get(name)
    user_path, default_path = _fact_paths(spec, user_path=user_path, default_path=default_path,
                                          capability_path=capability_path)
    if spec is not None and getattr(spec, "source", "agent") != "agent":
        source = spec.source
    user_values = load_simple_yaml(user_path) if user_path is not None and user_path.exists() else {}
    default_values = load_simple_yaml(default_path) if default_path.exists() else {}
    user_value = user_values.get(name)
    default_value = default_values.get(name)
    if user_value is not None:
        source = "user_config"
        effective = user_value
    elif default_value is not None:
        source = "packaged_default"
        effective = default_value
    else:
        source = "missing"
        effective = ""
    return {
        "key": name,
        "effective": mask_value(name, effective),
        "source": source,
        "user_config_path": str(user_path) if user_path is not None else "",
        "user_value": mask_value(name, user_value) if user_value is not None else None,
        "packaged_default": mask_value(name, default_value) if default_value is not None else None,
        "boundary_reason": BOUNDARY_KEYS.get(name, ""),
    }


# LLM: 用户配置＝当前进程实际加载的配置文件（config.config_path；Gateway 由 --config 指定，本机是
#   ~/.my-agent/config/desktop.yaml）；没有或加载的就是随包默认 YAML 时才看进程级 MY_AGENT_CONFIG。
#   原来只看环境变量，而 Gateway 从不设置它，模型因此误报“没有用户级配置”。两者的区别必须如实报告。
# 函数用途: 返回用户配置文件路径；没有用户配置时返回 None。
def user_config_path(config: object | None = None) -> Path | None:
    import os

    loaded = str(getattr(config, "config_path", "") or "").strip()
    if loaded and Path(loaded).expanduser().resolve() != packaged_config_path().resolve():
        return Path(loaded).expanduser()
    configured = str(os.environ.get("MY_AGENT_CONFIG") or "").strip()
    return Path(configured).expanduser() if configured else None


# 函数用途: 返回随包发布的默认配置路径。
def packaged_config_path() -> Path:
    return Path(__file__).resolve().parents[2] / "config" / "agent_config.yaml"


# LLM: 兼容入口：写入统一走参数中心 parameter_changes.set_parameter（登记表判定可写、按类型渲染、正式 load_config 回读、
#   写账本），这里不再维护第二套白名单写法。actor 固定为 model（只有模型工具调用它）。capability 等非主配置来源
#   需要调用方给 capability 运行时路径（capability_path），本入口没有 agent 就拿不到，只用于主配置键。
#   副作用：写用户配置与账本。
# 函数用途: 校验并写入一个模型可修改的配置项，返回结构化回执。
def set_tunable_value(
    key: object,
    value: object,
    *,
    user_path: Path | None = None,
) -> dict[str, object]:
    from .parameter_changes import ChangeOrigin, WritePaths, set_parameter

    path = user_path if user_path is not None else user_config_path()
    return set_parameter(key, value, paths=WritePaths(user_path=path), origin=ChangeOrigin("model"))


# LLM: 可写范围以参数中心登记表为准（两百多项，不整表塞进模型上下文）：给数量、查找方法、生效时机和永不可写的显式清单。
#   数量与 /settings 同口径，只算 listed_parameters（不含加载器元数据）。
# 函数用途: 生成给模型看的可改范围与安全边界摘要，让“能不能改”变成结构化事实而不是模型凭感觉断言。
def capability_summary() -> dict[str, Any]:
    from .parameter_registry import listed_parameters

    registry = listed_parameters()
    return {
        "writable_count": sum(spec.writable for spec in registry.values()),
        "boundary_count": sum(not spec.writable for spec in registry.values()),
        "how_to_find": "action=search 加 query 按关键词查参数；action=view 加 key 看单个参数的说明、默认值、当前值与能否修改。",
        "effect_when": EFFECT_GATEWAY_RESTART,
        "not_model_writable": [
            {"key": key, "reason": reason} for key, reason in sorted(BOUNDARY_KEYS.items())
        ],
    }


__all__ = [
    "BOUNDARY_KEYS",
    "EFFECT_GATEWAY_RESTART",
    "EFFECT_NEXT_SESSION",
    "TUNABLE_KEYS",
    "TunableSpec",
    "capability_summary",
    "effect_text",
    "packaged_config_path",
    "user_config_path",
    "mask_value",
    "read_config_fact",
    "set_tunable_value",
]
