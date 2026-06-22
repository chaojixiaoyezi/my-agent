
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

from agent_py_agent.agent.settings.config_io import load_simple_yaml, set_simple_yaml_value

# 可自助设置的字段白名单:聚焦飞书通道接入。其他字段(尤其 path/access 等安全项)请手动编辑配置。
_SETTABLE_KEYS = (
    "feishu_app_id",
    "feishu_app_secret",
    "feishu_verification_token",
    "feishu_encrypt_key",
    "feishu_callback_port",
)
# 回显需脱敏的敏感字段:别把凭证明文打回终端/日志。
_SECRET_KEYS = ("feishu_app_secret", "feishu_verification_token", "feishu_encrypt_key")


def _mask(key: str, value: str) -> str:
    if key in _SECRET_KEYS and value:
        return f"{value[:3]}***" if len(value) > 3 else "***"
    return value


def add_config_subcommands(sub: argparse._SubParsersAction) -> None:
    setter = sub.add_parser("config-set", help="设置一个配置项(白名单内,如飞书凭证);原地保留注释、原子写回")
    setter.add_argument("key", help="配置键名,如 feishu_app_id")
    setter.add_argument("value", help="要设置的值")
    setter.set_defaults(func=cmd_config_set)
    getter = sub.add_parser("config-get", help="读取一个配置项当前值(敏感字段脱敏)")
    getter.add_argument("key", help="配置键名")
    getter.set_defaults(func=cmd_config_get)


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
    try:
        old, _new_line = set_simple_yaml_value(path, key, str(args.value))
    except ValueError as exc:
        print(f"❌ {exc}")
        return 1
    before = _mask(key, (old or "").strip('"')) if old is not None else "(新增)"
    print(f"✅ 已设置 {key}: {before} → {_mask(key, str(args.value))}\n   写入 {path}")
    return 0


def cmd_config_get(args) -> int:
    path = Path(args.config)
    if not path.exists():
        print(f"❌ 配置文件不存在: {path}")
        return 1
    data = load_simple_yaml(path)
    key = str(args.key)
    if key not in data:
        print(f"(未设置) {key}")
        return 0
    print(f"{key}: {_mask(key, str(data[key]))}")
    return 0


__all__ = ["add_config_subcommands", "cmd_config_get", "cmd_config_set"]
