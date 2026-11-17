"""my-agent credentials {list,status,revoke}: manage stored task credentials without a task context."""
# 模块用途: 宿主 CLI 冷读规范 owner home，列出、查询或撤销私人凭据记录；只输出公开元数据，从不解密或打印秘密。
from __future__ import annotations

import argparse
import json
from pathlib import Path


# LLM: 只注册参数与处理器，解析阶段不读任何凭据。
# 函数用途: 添加 credentials 子命令组。
def add_credential_subcommands(sub: argparse._SubParsersAction) -> None:
    credentials = sub.add_parser("credentials", help="列出、查询或撤销私人凭据记录（只显示元数据，不显示秘密）")
    actions = credentials.add_subparsers(dest="credentials_action", required=True)
    for name, text in (("list", "列出 owner 的全部凭据记录元数据"), ("status", "查询一条凭据记录"),
                       ("revoke", "撤销一条凭据：删除全部密文并标记 revoked")):
        action = actions.add_parser(name, help=text)
        if name != "list":
            action.add_argument("credential_ref", help="宿主返回的 cred_ 引用")
        action.add_argument("--owner", default="", help="目标 owner（默认 local/main），如 providers/feishu/users/<id>")
        action.add_argument("--json", action="store_true", help="输出 JSON")
        action.set_defaults(func=cmd_credentials)


# LLM: owner 只经原 resolver 解析且必须已存在，不新建目录；错误只输出固定码与异常类型，不回显路径或秘密。
# 函数用途: 执行 credentials list/status/revoke。
def cmd_credentials(args) -> int:
    from ..agent.user_space.home_layout import home_paths
    from ..agent.user_space.owner_resolver import (
        home_paths_with_owner,
        owner_identity_from_owner_id,
        resolve_owner_home,
    )
    from ..agent.user_space.task_credentials import (
        list_credentials_for_owner,
        manage_credential_for_owner,
    )

    action = args.credentials_action
    owner_id = str(getattr(args, "owner", "") or "local/main")
    try:
        root = home_paths()
        try:
            identity = owner_identity_from_owner_id(owner_id)
        except ValueError:
            # --owner 写法不合法同样是找不到这个 owner，不报成凭据管理失败。
            raise LookupError("owner id format invalid") from None
        scoped = home_paths_with_owner(root, resolve_owner_home(root.root, identity))
        if str(scoped.owner_id) != owner_id or not Path(scoped.owner_home_dir).is_dir():
            raise LookupError("owner not found")
        if action == "list":
            result = {"ok": True, "owner_id": owner_id, **list_credentials_for_owner(scoped)}
        else:
            result = {"ok": True, "owner_id": owner_id,
                      **manage_credential_for_owner(scoped, args.credential_ref, operation=action)}
    except LookupError:
        result = {"ok": False, "owner_id": owner_id, "error_code": "OWNER_NOT_FOUND"}
    except (OSError, RuntimeError, ValueError) as error:
        result = {"ok": False, "owner_id": owner_id, "error_code": getattr(error, "code", "") or "CREDENTIAL_MANAGEMENT_FAILED",
                  "error_type": type(error).__name__}
    if getattr(args, "json", False):
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(_render(action, result))
    return 0 if result.get("ok") else 1


# LLM: 纯渲染，只打印公开元数据字段；失败行带异常类型，找不到 owner 时给出 --owner 写法示例。
# 函数用途: 把 credentials 结果渲染为人读文本。
def _render(action: str, result: dict) -> str:
    if not result.get("ok"):
        line = f"credentials {action} owner={result.get('owner_id')} error={result.get('error_code')}"
        if result.get("error_type"):
            line += f" error_type={result['error_type']}"
        if result.get("error_code") == "OWNER_NOT_FOUND":
            line += ("\n找不到这个 owner：--owner 写法如 local/main（本机主用户）、local/<用户ID> 或 "
                     "providers/<平台>/users/<用户ID>，且该 owner 已在本机建立过目录。")
        return line
    rows = result.get("credentials") if action == "list" else [result]
    lines = [f"credentials {action} owner={result.get('owner_id')} count={len(rows)}"
             + (f" unreadable={result['unreadable']}" if result.get("unreadable") else "")]
    for row in rows:
        flags = " revoked=true deleted=true" if row.get("revoked") else ""
        lines.append(f"- {row.get('credential_ref')} scope={row.get('access_scope')} status={row.get('status')} "
                     f"origin={row.get('origin')} task={row.get('origin_task_id') or '-'} expires_at={row.get('expires_at')} "
                     f"secret_present={str(row.get('secret_present')).lower()}{flags}")
    return "\n".join(lines)


__all__ = ["add_credential_subcommands", "cmd_credentials"]
