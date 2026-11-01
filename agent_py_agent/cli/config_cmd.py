
# LLM: CLI保留原五键白名单，读写只委托typed参数服务与原全局写门，敏感值不明文回显。
# 模块用途: 提供规范config命令的参数登记及安全读写。
from __future__ import annotations

"""config-set / config-get:让普通用户或 agent 用一条命令可靠地设/读配置项(尤其飞书通道凭证),
不必手撕带注释的简化 yaml。

雏鸟学飞:小白只要在 CLI 跟 my-agent 说"接飞书",my-agent 就能 `config-set feishu_app_id ...` 自己把
凭证写进配置——可靠、原子、保留注释,而不是用通用 edit 工具硬改 yaml。
共享白名单限定可自助设置的字段:当前聚焦飞书接入,挡掉对安全相关配置(path/access 等)的误改;
敏感字段(secret/token)回显一律脱敏,不把明文打回终端或日志。
"""

import argparse
import json
import sys
from pathlib import Path

from agent_py_agent.agent.common.global_write_guard import GlobalWriteInWorkerError
from agent_py_agent.agent.settings import load_config
from agent_py_agent.agent.settings.config_io import format_config_parse_diagnostic
from agent_py_agent.agent.settings.parameter_changes import (
    CLI_DEPLOYMENT_WRITABLE_KEYS,
    ChangeOrigin,
    WritePaths,
    cli_deployment_write_scope,
    set_parameter,
)
from agent_py_agent.agent.settings.user_config_capability import (
    mask_value,
    packaged_config_path,
    read_config_fact,
)


def add_config_subcommands(sub: argparse._SubParsersAction) -> None:
    setter = sub.add_parser("config-set", help="设置一个配置项(白名单内,如飞书凭证);原地保留注释、原子写回")
    setter.add_argument("key", help="配置键名,如 feishu_app_id")
    setter.add_argument("value", help="要设置的值")
    setter.set_defaults(func=cmd_config_set)
    getter = sub.add_parser("config-get", help="读取一个配置项当前值(敏感字段脱敏)")
    getter.add_argument("key", help="配置键名")
    getter.set_defaults(func=cmd_config_get)


# LLM: 仅五项白名单可写；旧值只取统一配置事实中已解析、已脱敏的 user_value，不能用默认值替代缺失覆盖；写入仍经参数服务和全局写门，仅捕获明确门拒绝。
# 函数用途: 委托 typed 服务读取写前展示值并写配置和账本，展示安全前后值或具体失败原因；未知故障继续向上传播。
def cmd_config_set(args) -> int:
    key = str(args.key)
    if key not in CLI_DEPLOYMENT_WRITABLE_KEYS:
        allowed = ", ".join(sorted(CLI_DEPLOYMENT_WRITABLE_KEYS))
        print(f"⚠️ '{key}' 不在可自助设置白名单({allowed})。\n   为防误改安全相关配置,其他字段请手动编辑配置文件。")
        return 1
    path = Path(args.config)
    if not path.exists():
        print(f"❌ 配置文件不存在: {path}")
        return 1
    config = load_config(path)
    _print_parse_warnings(config)
    # 配置事实已统一去引号并脱敏；只用用户覆盖，不把随包默认值误报成写前旧值。
    previous = read_config_fact(key, user_path=path, default_path=packaged_config_path(), config=config)["user_value"]
    before = previous if previous is not None else "(新增)"
    try:
        with cli_deployment_write_scope():
            report = set_parameter(key, args.value, paths=WritePaths(user_path=path), origin=ChangeOrigin("cli"))
    except GlobalWriteInWorkerError as exc:
        print(json.dumps({"ok": False, "code": exc.error_code,
                          "error": "当前受限进程没有写入全局配置的权限。", "executed": False}, ensure_ascii=False))
        return 1
    if not report.get("ok"):
        print(f"❌ {report.get('code', 'CONFIG_WRITE_FAILED')}: {report.get('error') or '配置写入失败，原因未知。'}")
        return 1
    print(f"✅ 已设置 {key}: {before} → {mask_value(key, str(args.value))}\n   写入 {path}")
    return 0


def cmd_config_get(args) -> int:
    """原始覆盖来自参数事实，实际生效值来自正式loader；二者均脱敏，结构错误明确展示。"""
    path = Path(args.config)
    if not path.exists():
        print(f"❌ 配置文件不存在: {path}")
        return 1
    key = str(args.key)
    config = load_config(path)
    _print_parse_warnings(config)
    fact = read_config_fact(key, user_path=path, default_path=packaged_config_path(), config=config)
    source = config.config_source_for(key)
    effective = mask_value(key, getattr(config, key)) if source else fact["effective"]
    if (fact["source"] == "missing" and not source) or effective == "":
        print(f"(未设置) {key}")
    else:
        print(f"{key}: {effective}")
    raw = fact["user_value"] if fact["user_value"] is not None else "(未覆盖)"
    print(f"raw: {raw}")
    print(f"effective: {effective} (source: {source.get('source') if source else fact['source']})")
    return 0


def _print_parse_warnings(config) -> None:
    # 只展示W0脱敏结构事实，不把类型校验中的原始值或底层异常字符串拼回终端。
    for diagnostic in config.config_parse_diagnostics:
        print(f"⚠️ 配置读取告警：{format_config_parse_diagnostic(diagnostic)}", file=sys.stderr)


__all__ = ["add_config_subcommands", "cmd_config_get", "cmd_config_set"]
