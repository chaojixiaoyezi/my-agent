# LLM: learnpack 两个"自动装"开关的唯一读取入口（能力包、插件各一个），打包/安装回执里的开关事实也只从这里出。
#   开关只从能力配置文件读，且每次按文件现读（不走 agent 上缓存的快照），所以管理员 /settings 改完马上生效；
#   读不到或格式坏一律按"关"处理（关着只会多问用户一次，不会多装）。改键名或命令文字要同步
#   capability_config.yaml、CapabilityConfig、user_config_capability 的边界登记、parameter_registry 的生效时机和
#   test_self_install_switches。
# 模块用途: 回答"她现在能不能自己装能力包/插件"，并给出用户要发的开关命令原文，供回执照抄给用户。
from __future__ import annotations

from dataclasses import dataclass

from .runtime_config_reload import (
    capability_config_path_for,
    load_capability_config_snapshot,
    resolve_capability_config_path,
)

PACK_SELF_INSTALL_KEY = "capability_pack_self_install_enabled"
PLUGIN_SELF_INSTALL_KEY = "plugin_self_install_enabled"
# 用到时现读文件的能力配置键：参数中心按这份名单把生效时机报成"马上生效"，不能报成"重启后生效"。
SELF_INSTALL_SWITCH_KEYS = frozenset({PACK_SELF_INSTALL_KEY, PLUGIN_SELF_INSTALL_KEY})
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


# LLM: 路径解析与 capability_config_for_agent 同一规则（用户位置存在用用户位置，否则随包默认），但不读也不写
#   agent 上的缓存快照；文件读不到、格式错一律当两个开关都关。只读。
# 函数用途: 安装、打包回执前调用，现读两个"自动装"开关。
def read_self_install_switches(agent: object) -> SelfInstallSwitches:
    path = resolve_capability_config_path(capability_config_path_for(agent))
    try:
        snapshot = load_capability_config_snapshot(path)
    except (OSError, TypeError, ValueError):
        return SelfInstallSwitches(False, False, UNREADABLE_CONFIG_VERSION)
    config = snapshot.config
    return SelfInstallSwitches(
        capability_pack=getattr(config, PACK_SELF_INSTALL_KEY, False) is True,
        plugin=getattr(config, PLUGIN_SELF_INSTALL_KEY, False) is True,
        config_version=snapshot.version,
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
    "PACK_SELF_INSTALL_KEY",
    "PLUGIN_SELF_INSTALL_KEY",
    "SELF_INSTALL_SWITCH_KEYS",
    "SelfInstallSwitches",
    "UNREADABLE_CONFIG_VERSION",
    "read_self_install_switches",
    "switch_command",
    "switch_facts",
]
