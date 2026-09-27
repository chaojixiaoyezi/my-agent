# LLM: 本模块是"哪些配置项模型/用户可以自助改"的**唯一权威**，CLI(`config-set`)与模型工具都从这里取名单、
#   校验和生效语义，不得各自维护第二份。安全边界（权限模式、危险根、凭据、模型密钥、宿主控制面路径）
#   永不进白名单：不是"暂时没开放"，而是结构上不允许模型自行改变。
#   生效语义必须如实报告：当前进程持有启动时加载的 AgentConfig，写盘后要等下一轮/下一次会话/网关重启，
#   不能声称"已经生效"。
# 模块用途: 给用户与模型提供受控、可校验、能报告来源与生效时机的配置读写能力。
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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

# 凭据名只按键名最后的完整片段认：input_media_token_reserve 里的 token 是计数单位，max_tokens 是复数，都不是凭据
_CREDENTIAL_SUFFIXES = ("api_key", "secret", "password", "token", "cookie", "cookies", "credential", "credentials", "encrypt_key")


# LLM: 参数中心登记表、聊天 /settings、user_config 与命令行 config-get 共用这一条凭据判定，不再各自维护名单；
#   键名等于凭据名或以 `_凭据名` 结尾才算（完整片段，不按子串）。改规则须同步 test_parameter_registry 的脱敏用例。
# 函数用途: 判断一个配置键是不是凭据（回显、记账都必须脱敏，也永远是安全边界）。
def is_credential_key(key: object) -> bool:
    text = str(key or "")
    return any(text == name or text.endswith("_" + name) for name in _CREDENTIAL_SUFFIXES)


# LLM: 只按 is_credential_key 判定；原先只认 3 个飞书键，api_key、gateway_auth_token 等会在 /settings 与 user_config 里明文回显。
# 函数用途: 回显时对凭据脱敏，不把明文写回终端、日志或模型上下文。
def mask_value(key: str, value: object) -> str:
    text = str(value or "")
    if is_credential_key(key) and text:
        return f"{text[:3]}***" if len(text) > 3 else "***"
    return text


# LLM: 生效值＝用户配置文件里的值优先；没写才回落到随包默认 YAML。报告必须同时给出两者与来源，
#   否则模型会把随包默认当成"用户配置"（真机：模型去读安装目录里的 agent_config.yaml 当答案）。
#   能不能改只由参数中心登记表的 writable 回答；这里不再给旧白名单的 tunable 字段（09-27 模型见 writable=true、
#   tunable=false 两个口径，误以为 max_tokens 改不了）。
# 函数用途: 读取某个键的用户值与随包默认值，并给出当前生效值与来源。
def read_config_fact(
    key: str, *, user_path: Path | None, default_path: Path
) -> dict[str, object]:
    name = str(key or "").strip()
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
#   写账本），这里不再维护第二套白名单写法。actor 固定为 model（只有模型工具调用它）。副作用：写用户配置与账本。
# 函数用途: 校验并写入一个模型可修改的配置项，返回结构化回执。
def set_tunable_value(
    key: object,
    value: object,
    *,
    user_path: Path | None = None,
) -> dict[str, object]:
    from .parameter_changes import ChangeOrigin, set_parameter

    path = user_path if user_path is not None else user_config_path()
    return set_parameter(key, value, user_path=path, origin=ChangeOrigin("model"))


# LLM: 可写范围以参数中心登记表为准（两百多项，不整表塞进模型上下文）：给数量、查找方法、生效时机和永不可写的显式清单。
# 函数用途: 生成给模型看的可改范围与安全边界摘要，让“能不能改”变成结构化事实而不是模型凭感觉断言。
def capability_summary() -> dict[str, Any]:
    from .parameter_registry import parameter_registry

    registry = parameter_registry()
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
