# LLM: 能力配置现读开关的唯一名单与读取入口；两个自动装回执仍保持原字段、版本标记和命令文字。
#   名单内开关每次按文件现读，不读写 agent 上缓存的预算快照，所以管理员 /settings 改完马上生效；
#   读不到或格式坏按 CapabilityConfig 对应键的默认值处理（两个自动装默认关，不会多装）。改键名或命令文字要同步
#   capability_config.yaml、CapabilityConfig、user_config_capability 的边界登记、parameter_registry 的生效时机和
#   test_self_install_switches、test_capability_selection_scope 与前端参数目录。
# 模块用途: 统一回答四个马上生效的能力开关当前值，并保持自动装回执原样。
from __future__ import annotations

from dataclasses import dataclass

from .config import CapabilityConfig
from .runtime_config_models import CapabilityConfigSnapshot
from .runtime_config_reload import (
    capability_config_path_for,
    load_capability_config_snapshot,
    resolve_capability_config_path,
)

PACK_SELF_INSTALL_KEY = "capability_pack_self_install_enabled"
PLUGIN_SELF_INSTALL_KEY = "plugin_self_install_enabled"
# 自动装回执的两键集合；保留原合同，不包含其它现读开关。
SELF_INSTALL_SWITCH_KEYS = frozenset({PACK_SELF_INSTALL_KEY, PLUGIN_SELF_INSTALL_KEY})
# 用到时现读文件的能力配置键唯一名单；参数中心和前端目录按这份事实报"马上生效"。
CAPABILITY_FRESH_SWITCH_KEYS = SELF_INSTALL_SWITCH_KEYS | frozenset({
    "enable_capability_package_selection", "conversation_method_carry_enabled",
})
# 配置文件读不到或解析失败时的版本标记：开关按关处理，回执如实带上这个标记。
UNREADABLE_CONFIG_VERSION = "unreadable"


# LLM: 只读快照；config_version 是读到的配置文件 sha256（读不到时为 UNREADABLE_CONFIG_VERSION），
#   只用于回执和账本追溯，不参与准入判断。
# 类用途: 一次现读得到的两个开关值。
@dataclass(frozen=True)
class SelfInstallSwitches:
    capability_pack: bool
    plugin: bool
    config_version: str


# LLM: 现读入口共用原路径优先级和正式加载器；不碰 agent 缓存，读取失败只交回缺失事实，由调用方按默认值处理。
# 函数用途: 为现读开关和自动装版本回执读取同一份文件快照，不写文件。
def _read_fresh_capability_snapshot(agent: object) -> CapabilityConfigSnapshot | None:
    try:
        path = resolve_capability_config_path(capability_config_path_for(agent))
        return load_capability_config_snapshot(path)
    except (OSError, TypeError, ValueError):
        return None


# LLM: 仅允许唯一名单内的布尔开关走现读，不能把预算项伪装成马上生效；坏文件用 dataclass 默认值，不读写缓存。
# 函数用途: 主选包和子入口共用此入口，下一次判断读取管理员刚保存的开关值。
def read_fresh_capability_switch(agent: object, key: str) -> bool:
    if key not in CAPABILITY_FRESH_SWITCH_KEYS:
        raise ValueError(f"能力配置键不在现读开关名单内: {key}")
    snapshot = _read_fresh_capability_snapshot(agent)
    config = snapshot.config if snapshot is not None else CapabilityConfig()
    return getattr(config, key) is True


# LLM: 与通用开关共用同一现读入口；两个自动装默认关，坏文件保留 unreadable 标记，原字段和四条命令不变。
# 函数用途: 安装、打包回执前调用，现读两个"自动装"开关并保留真实文件版本。
def read_self_install_switches(agent: object) -> SelfInstallSwitches:
    snapshot = _read_fresh_capability_snapshot(agent)
    config = snapshot.config if snapshot is not None else CapabilityConfig()
    return SelfInstallSwitches(
        capability_pack=getattr(config, PACK_SELF_INSTALL_KEY) is True,
        plugin=getattr(config, PLUGIN_SELF_INSTALL_KEY) is True,
        config_version=snapshot.version if snapshot is not None else UNREADABLE_CONFIG_VERSION,
    )


# LLM: 命令文字必须和 /settings 解析器接受的写法逐字一致（test_self_install_switches 用解析器回放钉住）；
#   这里只给命令原文，不替用户执行。纯函数。
# 函数用途: 给出打开/关闭某个开关时用户要发的那一行命令。
def switch_command(key: str, enabled: bool) -> str:
    return f"/settings set {key} {'true' if enabled else 'false'}"


# LLM: 回执里的开关事实：两个当前值、读到的配置版本、四条开/关命令原文和一句谁能改。模型照这些字段提醒用户，
#   不凭记忆说开关状态。纯函数，不读文件。
# 函数用途: 把一次现读的开关值排成回执用的结构化字段。
def switch_facts(switches: SelfInstallSwitches) -> dict[str, object]:
    return {
        PACK_SELF_INSTALL_KEY: switches.capability_pack,
        PLUGIN_SELF_INSTALL_KEY: switches.plugin,
        "config_version": switches.config_version,
        "switch_commands": {
            "capability_pack_on": switch_command(PACK_SELF_INSTALL_KEY, True),
            "capability_pack_off": switch_command(PACK_SELF_INSTALL_KEY, False),
            "plugin_on": switch_command(PLUGIN_SELF_INSTALL_KEY, True),
            "plugin_off": switch_command(PLUGIN_SELF_INSTALL_KEY, False),
        },
        "switch_note": "这两个开关只有管理员能改（本机 TUI，或已用 /admin 绑定管理员的私聊），模型不能改；改完马上生效。",
    }


__all__ = [
    "CAPABILITY_FRESH_SWITCH_KEYS",
    "PACK_SELF_INSTALL_KEY",
    "PLUGIN_SELF_INSTALL_KEY",
    "SELF_INSTALL_SWITCH_KEYS",
    "SelfInstallSwitches",
    "UNREADABLE_CONFIG_VERSION",
    "read_fresh_capability_switch",
    "read_self_install_switches",
    "switch_command",
    "switch_facts",
]
