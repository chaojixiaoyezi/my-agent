# LLM: 本机管理员专用 CLI：只在配置的 base owner 为 local/main 时运行，只读写 home/config 下的管理员密码与 IM 身份绑定。
#   密码只经 getpass 两次无回显读取，不接受命令行参数或环境变量，不打印、不写日志；状态输出只含是否设置与时间。
#   改动时同步 CLI_REFERENCE.md 与 test_admin_identity_store.py。
# 模块用途: 提供 `my-agent admin-password set|status|clear` 与 `my-agent admin-identities list|remove`。

from __future__ import annotations

import argparse
import getpass
import json
import sys
import time
from collections.abc import Callable
from functools import partial
from pathlib import Path

from ..agent.user_space.admin_channel_identity import (
    AdminIdentityStoreError,
    list_admin_channel_identities,
    parse_admin_channel_identity_key,
    remove_admin_channel_identity,
)
from ..agent.user_space.admin_password import (
    AdminPasswordError,
    admin_password_status,
    clear_admin_password,
    set_admin_password,
    validate_admin_password,
)

PasswordPrompt = Callable[[str], str]


# LLM: 两个命令组各自带子命令；缺子命令时打印本组帮助并返回 2，绝不落到根解析器的默认聊天入口。
# 函数用途: 注册管理员密码与管理员 IM 身份两组 CLI 命令。
def add_admin_identity_subcommands(sub: argparse._SubParsersAction) -> None:
    password = sub.add_parser("admin-password", help="本机管理员密码：IM 私聊 /admin 绑定和 /approve 审批时校验")
    password.set_defaults(func=partial(_print_group_help, password))
    password_sub = password.add_subparsers(dest="admin_password_command")
    password_sub.add_parser("set", help="设置或更换管理员密码（输入两次，不回显，至少 8 个字符）").set_defaults(
        func=cmd_admin_password_set
    )
    status = password_sub.add_parser("status", help="查看管理员密码是否已设置")
    status.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    status.set_defaults(func=cmd_admin_password_status)
    password_sub.add_parser("clear", help="清除管理员密码；已绑定的 IM 身份保持不变").set_defaults(
        func=cmd_admin_password_clear
    )

    identities = sub.add_parser("admin-identities", help="查看或解除已绑定为管理员的 IM 私聊身份")
    identities.set_defaults(func=partial(_print_group_help, identities))
    identities_sub = identities.add_subparsers(dest="admin_identities_command")
    listing = identities_sub.add_parser("list", help="列出已绑定为管理员的 IM 私聊身份")
    listing.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    listing.set_defaults(func=cmd_admin_identities_list)
    remove = identities_sub.add_parser("remove", help="解除一个 IM 私聊身份的管理员绑定")
    remove.add_argument("identity", help="渠道:用户ID，例如 feishu:ou_xxx")
    remove.set_defaults(func=cmd_admin_identities_remove)


# LLM: 有副作用：写 home/config/admin-password.json（0600）。prompt 可注入仅供测试；两次输入不一致或不合规时不写任何文件。
# 函数用途: 交互式设置或更换管理员密码。
def cmd_admin_password_set(args, *, prompt: PasswordPrompt = getpass.getpass) -> int:
    home_root = _local_admin_home(args)
    if home_root is None:
        return 2
    first = prompt("新管理员密码（至少 8 个字符，不回显）：")
    try:
        validate_admin_password(first)
    except AdminPasswordError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if prompt("再输入一次：") != first:
        print("两次输入不一致，管理员密码没有更改。", file=sys.stderr)
        return 2
    status = set_admin_password(home_root, first)
    print(f"管理员密码已保存（{_format_time(status.get('updated_at'))}）。")
    print("在飞书等 IM 与机器人的私聊里发送 /admin <密码> 即可绑定管理员身份；发送后建议撤回含密码的消息。")
    return 0


# LLM: 只读；不输出盐、散列或参数。
# 函数用途: 显示管理员密码是否已设置以及已绑定的 IM 身份数量。
def cmd_admin_password_status(args) -> int:
    home_root = _local_admin_home(args)
    if home_root is None:
        return 2
    status = admin_password_status(home_root)
    try:
        bound = len(list_admin_channel_identities(home_root))
    except AdminIdentityStoreError:
        bound = -1
    if getattr(args, "json", False):
        print(json.dumps({**status, "bound_identities": bound}, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    labels = {"configured": "已设置", "missing": "未设置", "invalid": "文件损坏（校验一律不通过）"}
    line = f"管理员密码：{labels.get(str(status.get('state')), '未知')}"
    if status.get("configured"):
        line += f"，更新于 {_format_time(status.get('updated_at'))}"
    print(line)
    print(f"已绑定的 IM 管理员身份：{bound if bound >= 0 else '绑定文件损坏'}")
    return 0


# LLM: 有副作用：删除密码文件；不删除绑定，提示用户如需解除绑定另用 admin-identities remove。
# 函数用途: 清除管理员密码。
def cmd_admin_password_clear(args) -> int:
    home_root = _local_admin_home(args)
    if home_root is None:
        return 2
    removed = clear_admin_password(home_root)
    print("管理员密码已清除。" if removed else "本来就没有设置管理员密码。")
    print("已绑定的 IM 身份保持不变；需要解除时运行 my-agent admin-identities list / remove。")
    return 0


# LLM: 只读；列出渠道、用户 ID 与绑定时间，不含其他信息。
# 函数用途: 列出已绑定为管理员的 IM 私聊身份。
def cmd_admin_identities_list(args) -> int:
    home_root = _local_admin_home(args)
    if home_root is None:
        return 2
    try:
        identities = list_admin_channel_identities(home_root)
    except AdminIdentityStoreError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if getattr(args, "json", False):
        rows = [{"identity": item.key, "bound_at": item.bound_at} for item in identities]
        print(json.dumps({"identities": rows}, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if not identities:
        print("没有已绑定的 IM 管理员身份。")
    for item in identities:
        print(f"{item.key}  绑定于 {_format_time(item.bound_at)}")
    return 0


# LLM: 有副作用：从绑定文件删除一条精确匹配的身份；不存在时返回 1 并说明。
# 函数用途: 解除一个 IM 私聊身份的管理员绑定。
def cmd_admin_identities_remove(args) -> int:
    home_root = _local_admin_home(args)
    if home_root is None:
        return 2
    try:
        channel, user_id = parse_admin_channel_identity_key(getattr(args, "identity", ""))
        removed = remove_admin_channel_identity(home_root, channel, user_id)
    except AdminIdentityStoreError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"已解除 {channel}:{user_id} 的管理员身份。" if removed else f"{channel}:{user_id} 没有绑定管理员身份。")
    return 0 if removed else 1


# LLM: 只接受配置里 base owner 为 local/main 的部署；home 根沿与 Gateway 相同的 configured_home_root 解析。
# 函数用途: 读取配置并返回管理员身份文件所在的 home 根；不是本机主用户配置时打印原因并返回 None。
def _local_admin_home(args) -> Path | None:
    from ..agent.settings import load_config
    from ..agent.user_space.home_layout import home_paths
    from ..agent.user_space.home_root import configured_home_root
    from ..agent.user_space.owner_resolver import OwnerIdentity, owner_identity_from_config

    config = load_config(args.config)
    if owner_identity_from_config(config) != OwnerIdentity.local_main():
        print("管理员密码和 IM 管理员身份只能在本机主用户（local/main）配置下管理。", file=sys.stderr)
        return None
    return home_paths(configured_home_root(config)).root


# LLM: 纯展示；缺失或非法时间显示为“未知”。
# 函数用途: 把时间戳格式化成本地时间文本。
def _format_time(value: object) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(value)))
    except (TypeError, ValueError, OverflowError, OSError):
        return "未知"


# LLM: 只打印帮助，不读写任何文件。
# 函数用途: 命令组缺少子命令时显示该组帮助。
def _print_group_help(parser: argparse.ArgumentParser, args) -> int:
    del args
    parser.print_help()
    return 2


__all__ = [
    "add_admin_identity_subcommands",
    "cmd_admin_identities_list",
    "cmd_admin_identities_remove",
    "cmd_admin_password_clear",
    "cmd_admin_password_set",
    "cmd_admin_password_status",
]
