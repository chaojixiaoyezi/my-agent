
# LLM: CLI保留原五键白名单，读写只委托typed参数服务与原全局写门，敏感值不明文回显。
# 模块用途: 提供规范config命令的参数登记及安全读写。
from __future__ import annotations

"""config-set / config-get:让普通用户或 agent 用一条命令可靠地设/读配置项(尤其飞书通道凭证),
不必手撕带注释的简化 yaml。

雏鸟学飞:小白只要在 CLI 跟 my-agent 说"接飞书",my-agent 就能 `config-set feishu_app_id ...` 自己把
凭证写进配置——可靠、原子、保留注释,而不是用通用 edit 工具硬改 yaml。
白名单(_SETTABLE_KEYS)限定可自助设置的字段:当前聚焦飞书接入,挡掉对安全相关配置(path/access 等)的误改;
敏感字段(secret/token)回显一律脱敏,不把明文打回终端或日志。
"""

import argparse
from pathlib import Path

from agent_py_agent.agent.settings import load_config
from agent_py_agent.agent.settings.parameter_changes import ChangeOrigin, WritePaths, set_parameter
from agent_py_agent.agent.settings.user_config_capability import (
    mask_value,
    packaged_config_path,
    read_config_fact,
)

# 可自助设置的字段白名单:聚焦飞书通道接入。其他字段(尤其 path/access 等安全项)请手动编辑配置。
_SETTABLE_KEYS = (
    "feishu_app_id",
    "feishu_app_secret",
    "feishu_verification_token",
    "feishu_encrypt_key",
    "feishu_callback_port",
)


def add_config_subcommands(sub: argparse._SubParsersAction) -> None:
    setter = sub.add_parser("config-set", help="设置一个配置项(白名单内,如飞书凭证);原地保留注释、原子写回")
    setter.add_argument("key", help="配置键名,如 feishu_app_id")
    setter.add_argument("value", help="要设置的值")
    setter.set_defaults(func=cmd_config_set)
    getter = sub.add_parser("config-get", help="读取一个配置项当前值(敏感字段脱敏)")
    getter.add_argument("key", help="配置键名")
    getter.set_defaults(func=cmd_config_get)


# LLM: CLI保留原五项白名单，写入只经现有typed参数服务及其全局写门、正式回读和脱敏账本，不授予Slash身份。
# 函数用途: 校验允许的字段后委托唯一参数写服务，不再直接修改YAML。
def cmd_config_set(args) -> int:
    key = str(args.key)
    if key not in _SETTABLE_KEYS:
        allowed = ", ".join(_SETTABLE_KEYS)
        print(f"⚠️ '{key}' 不在可自助设置白名单({allowed})。\n   为防误改安全相关配置,其他字段请手动编辑配置文件。")
        return 1
    path = Path(args.config)
    if not path.exists():
        print(f"❌ 配置文件不存在: {path}")
        return 1
    report = set_parameter(key, args.value, paths=WritePaths(user_path=path), origin=ChangeOrigin("cli"))
    if not report.get("ok"):
        print(f"❌ {report.get('code', 'CONFIG_WRITE_FAILED')}")
        return 1
    old = report.get("previous")
    before = str(old) if old is not None else "(新增)"
    print(f"✅ 已设置 {key}: {before} → {mask_value(key, str(args.value))}\n   写入 {path}")
    return 0


# LLM: 读取统一参数事实中的生效值和来源，服务端已脱敏；不再维护raw YAML优先级分支。
# 函数用途: 从当前显式配置路径读取正式配置事实，缺失保持原0退出码。
def cmd_config_get(args) -> int:
    path = Path(args.config)
    if not path.exists():
        print(f"❌ 配置文件不存在: {path}")
        return 1
    key = str(args.key)
    fact = read_config_fact(key, user_path=path, default_path=packaged_config_path(), config=load_config(path))
    if fact["source"] == "missing":
        print(f"(未设置) {key}")
        return 0
    print(f"{key}: {fact['effective']}")
    return 0


__all__ = ["add_config_subcommands", "cmd_config_get", "cmd_config_set"]
