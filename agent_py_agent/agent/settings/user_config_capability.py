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

from .config_io import load_simple_yaml, set_simple_yaml_value

# 生效时机（如实报告，不夸大）
EFFECT_NEXT_SESSION = "next_session"
EFFECT_GATEWAY_RESTART = "restart_gateway"

_EFFECT_TEXT = {
    EFFECT_NEXT_SESSION: "保存后对**下一次新建会话**生效；当前正在跑的会话仍用已加载的值。",
    EFFECT_GATEWAY_RESTART: "保存后需要重启 Gateway 才生效；当前进程不会热加载。",
}


# LLM: 一个可自助修改项＝键名 + 人类说明 + 纯函数校验 + 生效时机。校验只做区间/枚举等客观判断，
#   不解析自然语言，也不接受空值含糊放行。
# 类用途: 描述一个用户可自助修改的配置项及其合法取值。
@dataclass(frozen=True)
class TunableSpec:
    key: str
    describe: str
    validate: Callable[[object], tuple[bool, str, object]]
    effect: str = EFFECT_NEXT_SESSION


def _percent(value: object) -> tuple[bool, str, object]:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return False, "必须是 25~95 之间的整数百分比", 0
    if not 25 <= number <= 95:
        return False, "必须是 25~95 之间的整数百分比", 0
    return True, "", number


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
        describe="上下文用到百分之多少时自动 compact（默认 90）",
        validate=_percent,
    ),
    "memory_compact_recovery_target_percent": TunableSpec(
        key="memory_compact_recovery_target_percent",
        describe="compact 之后的健康目标百分比（默认 60）",
        validate=_percent,
    ),
    "tool_read_max_chars": TunableSpec(
        key="tool_read_max_chars",
        describe="单次读文件返回的最大字符数",
        validate=_positive_int,
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
    "api_key": "供应商凭据，不能由模型改写",
    "api_key_env": "供应商凭据来源，不能由模型改写",
    "my_agent_home": "宿主数据根，属于宿主控制面",
    "my_agent_owner_provider": "owner 身份解析，属于宿主控制面",
    "my_agent_owner_kind": "owner 身份解析，属于宿主控制面",
    "my_agent_owner_id": "owner 身份解析，属于宿主控制面",
}

_SECRET_KEYS = frozenset({"feishu_app_secret", "feishu_verification_token", "feishu_encrypt_key"})


# 函数用途: 回显时对敏感项脱敏，不把明文写回终端、日志或模型上下文。
def mask_value(key: str, value: object) -> str:
    text = str(value or "")
    if key in _SECRET_KEYS and text:
        return f"{text[:3]}***" if len(text) > 3 else "***"
    return text


# LLM: 生效值＝用户配置文件里的值优先；没写才回落到随包默认 YAML。报告必须同时给出两者与来源，
#   否则模型会把随包默认当成"用户配置"（真机：模型去读安装目录里的 agent_config.yaml 当答案）。
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
        "tunable": name in TUNABLE_KEYS,
        "boundary_reason": BOUNDARY_KEYS.get(name, ""),
    }


# LLM: 用户配置＝进程级 MY_AGENT_CONFIG（本机部署指向 ~/.my-agent/config/desktop.yaml）；未设置时
#   回到随包默认 YAML。两者的区别必须如实报告，不能把随包默认当成用户配置。
# 函数用途: 返回用户配置文件路径（可能与随包默认是同一个文件）。
def user_config_path() -> Path | None:
    import os

    configured = str(os.environ.get("MY_AGENT_CONFIG") or "").strip()
    return Path(configured).expanduser() if configured else None


# 函数用途: 返回随包发布的默认配置路径。
def packaged_config_path() -> Path:
    return Path(__file__).resolve().parents[2] / "config" / "agent_config.yaml"


# LLM: 写入只允许白名单键 + 通过校验的值，落盘走保留注释的原子写入；写完必须回读确认，并把
#   "保存到哪里 / 什么时候生效 / 现在生效值是什么"作为结构化事实返回，不允许只说"改好了"。
# 函数用途: 校验并写入一个用户可自助修改的配置项，返回结构化回执。
def set_tunable_value(
    key: object,
    value: object,
    *,
    user_path: Path | None = None,
) -> dict[str, object]:
    name = str(key or "").strip()
    if not name:
        return {"ok": False, "error": "缺少配置键名"}
    if name in BOUNDARY_KEYS:
        return {
            "ok": False,
            "error": f"'{name}' 属于安全边界，不能由模型自行修改：{BOUNDARY_KEYS[name]}",
            "boundary_reason": BOUNDARY_KEYS[name],
        }
    spec = TUNABLE_KEYS.get(name)
    if spec is None:
        return {
            "ok": False,
            "error": f"'{name}' 不在可自助修改白名单内",
            "tunable_keys": sorted(TUNABLE_KEYS),
        }
    path = Path(user_path) if user_path is not None else user_config_path()
    if path is None:
        return {
            "ok": False,
            "error": (
                "当前进程没有用户配置文件位置（环境变量 MY_AGENT_CONFIG 未设置），"
                f"不能安全写入；请人工编辑随包默认配置：{packaged_config_path()}"
            ),
        }
    if not path.exists():
        return {"ok": False, "error": f"用户配置文件不存在：{path}"}
    ok, error, normalized = spec.validate(value)
    if not ok:
        return {"ok": False, "error": f"'{name}' 取值不合法：{error}"}
    try:
        old_value, _line = set_simple_yaml_value(path, name, str(normalized))
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": f"写入失败：{exc}"}
    confirmed = load_simple_yaml(path).get(name)
    return {
        "ok": True,
        "key": name,
        "saved_to": str(path),
        "previous": mask_value(name, old_value),
        "saved": mask_value(name, confirmed),
        "written_value_matches": str(confirmed) == str(normalized),
        "effect_when": spec.effect,
        "effect_text": _EFFECT_TEXT.get(spec.effect, ""),
        "note": "当前进程仍在使用启动时加载的值，请按 effect_text 的时机生效。",
    }


# 函数用途: 生成给模型看的白名单/边界清单，让"能不能改"变成结构化事实而不是模型凭感觉断言。
def capability_summary() -> dict[str, Any]:
    return {
        "tunable": [
            {"key": spec.key, "describe": spec.describe, "effect_when": spec.effect}
            for spec in TUNABLE_KEYS.values()
        ],
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
    "packaged_config_path",
    "user_config_path",
    "mask_value",
    "read_config_fact",
    "set_tunable_value",
]
