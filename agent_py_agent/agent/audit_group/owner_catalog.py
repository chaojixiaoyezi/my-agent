# LLM: 账号只由原local_users和provider索引登记；不按目录扫描创造用户，不公开owner_home。
# 模块用途: 列出可观察owner并为每个来源保留读取错误。
from __future__ import annotations

import json
from pathlib import Path

from ..common.nofollow_fs import read_bytes_beneath
from ..plugin_host.errors import HostAPIError
from ..plugin_host.owner_context import reject_constant, unique_object
from ..user_space.local_user_store import LocalUserStore
from .access_policy import owner_identity
from .readonly_store import ReadonlyAssetStore
from .safe_projection import safe_text


# LLM: 公开错误只包含固定来源名和机器码，不拼底层异常文字或文件路径。
# 函数用途: 为partial结果标记无法读取的owner来源。
def source_error(owner, source):
    return {"owner_id": owner, "source": source, "code": "SOURCE_UNAVAILABLE"}


# LLM: 读现有provider JSONL，最后一条准确主体记录生效；坏行不能悄悄丢弃。
# 函数用途: 仅枚举已登记的飞书原生个人owner。
def feishu_owners(root):
    raw = read_bytes_beneath(root, ("identity", "provider_identity", "feishu.jsonl"),
                             max_bytes=4 * 1024 * 1024, require_dir_fd=True)
    latest = {}
    for line in (raw or b"").decode("utf-8").split("\n"):
        if not line.strip():
            continue
        row = json.loads(line, object_pairs_hook=unique_object, parse_constant=reject_constant)
        if type(row) is not dict or not row.get("provider_subject_id"):
            raise ValueError("invalid provider index")
        latest[(row.get("tenant_or_app_key", ""), row["provider_subject_id"])] = row
    result = {}
    for row in latest.values():
        if row.get("provider") != "feishu" or row.get("owner_kind") != "user":
            continue
        owner = "providers/feishu/users/" + row["owner_id"]
        owner_identity(owner)
        result[owner] = {"owner_id": owner, "owner_kind": "user", "display_name": safe_text(row.get("display_name", "")),
                         "status": "enabled" if row.get("status", "active") == "active" else "disabled",
                         "in_audit_group": False, "updated_at": safe_text(row.get("updated_at", ""))}
    return list(result.values())


# LLM: 枚举、单用户解析、provider解析各自保留故障；read_only无锁无物化。
# 函数用途: 获取当前真实账号列表和安全的来源错误列表。
def registered_owners(root, *, include_local=True, include_feishu=True):
    root, rows, errors = Path(root).resolve(), [], []
    if include_local:
        rows.append({"owner_id": "local/main", "owner_kind": "main", "display_name": "管理员",
                     "status": "enabled", "in_audit_group": True, "updated_at": ""})
        store = LocalUserStore(root)
        try:
            ids = store.list_user_ids()
            if len(ids) > 2000:
                raise ValueError("account listing limit")
            for uid in ids:
                try:
                    user = store.get(uid)
                    rows.append({"owner_id": f"local/{uid}", "owner_kind": "user", "display_name": safe_text(user.display_name),
                                 "status": user.status, "in_audit_group": user.in_audit_group, "updated_at": safe_text(user.updated_at)})
                except (OSError, ValueError, HostAPIError):
                    errors.append(source_error(f"local/{uid}", "account"))
        except (OSError, ValueError, HostAPIError):
            errors.append(source_error("", "local_accounts"))
    if include_feishu:
        try:
            rows.extend(feishu_owners(root))
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            errors.append(source_error("", "feishu_accounts"))
    for row in rows:
        try:
            status = ReadonlyAssetStore(root, row["owner_id"]).object("owner_status.json")
            if status is not None and status.get("status") != "active":
                row["status"] = "disabled"
        except (OSError, ValueError):
            row["status"] = "unknown"
            errors.append(source_error(row["owner_id"], "owner_status"))
    return sorted(rows, key=lambda row: row["owner_id"]), errors
