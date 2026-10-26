# LLM: 通道入口及审计组准入检查结构化身份；旧绑定账只读、nofollow、有界，坏源失败关闭。
# 模块用途: 统一普通本地用户纯Web通道和历史IM绑定的入组限制。
from __future__ import annotations

import json
import os
from pathlib import Path

from ..common.nofollow_fs import open_directory_beneath, read_bytes_beneath
from ..common.path_segments import safe_path_segment
from ..plugin_host.errors import HostAPIError

_MAX_BINDING_FILES = 128
_MAX_BINDING_BYTES = 16 * 1024 * 1024


# LLM: identity由宿主resolver或provider记录提供；普通local/user任何非web入口均403，无IO。
# 函数用途: 在绑定和使用旧绑定之前拒绝受管账号进入IM通道。
def require_local_user_channel(identity, channel: str) -> None:
    if isinstance(identity, str):
        managed = identity.startswith("local/") and identity != "local/main"
    else:
        managed = identity.provider == "local" and identity.owner_kind != "main"
    if managed and channel != "web":
        raise HostAPIError("MANAGED_USER_WEB_ONLY_CHANNEL_RESTRICTED", 403,
                           message="受管本地账号仅允许通过Web访问，不能绑定或使用IM通道。")


# LLM: 只列原provider索引目录，不创建目录/锁、不跟随链接；非索引名早退，索引数量和已见文件消失仍硬拒。
# 函数用途: 浅层遍历并有界收集provider账本，缺目录表示没有这类历史记录。
def _provider_binding_paths(root: Path) -> list[tuple[str, ...]]:
    try:
        descriptor = open_directory_beneath(root, ("identity", "provider_identity"))
    except FileNotFoundError:
        return []
    try:
        paths = []
        with os.scandir(descriptor) as entries:
            for entry in entries:
                if not entry.name.endswith(".jsonl"):
                    continue
                paths.append(("identity", "provider_identity", entry.name))
                if len(paths) > _MAX_BINDING_FILES:
                    raise ValueError("identity binding file limit")
        return sorted(paths)
    finally:
        os.close(descriptor)


# LLM: 先校验全部记录再应用末条快照；缺目标/键或不规范ID不得抹掉旧绑定，provider额外status不授权解除绑定。
# 函数用途: 提取原账本的当前绑定，语义损坏与坏JSON同样拒绝。
def _current_bindings(data: bytes, *, provider_index: bool) -> list[dict]:
    latest = {}
    keys = ("provider_subject_id",) if provider_index else ("canonical_user_id", "provider", "provider_subject_id")
    for line in data.decode("utf-8").split("\n"):
        if not line.strip():
            continue
        row = json.loads(line)
        _validate_binding(row, provider_index=provider_index)
        latest[tuple(row[key] for key in keys)] = row
    return list(latest.values())


# LLM: 复用原safe_path_segment但只比对不修复；linked未知状态无法证明解绑则503，provider沿真实模型忽略额外status。
# 函数用途: 防止缺字段、错误类型、空键及会被规范化的身份值把坏账洗成无绑定。
def _validate_binding(row: dict, *, provider_index: bool) -> None:
    required = ("provider", "provider_subject_id", "owner_id")
    if not provider_index:
        required += ("canonical_user_id",)
    if not isinstance(row, dict) or any(not isinstance(row.get(key), str) or not row[key] for key in required):
        raise ValueError("invalid identity binding")
    for key in required:
        parts = row[key].split("/") if key == "owner_id" else (row[key],)
        if any(not part or safe_path_segment(part) != part for part in parts):
            raise ValueError("noncanonical identity binding")
    if provider_index:
        if row.get("owner_kind", "user") not in {"main", "user", "group"}:
            raise ValueError("invalid provider owner kind")
    elif row.get("status", "active") not in {"active", "revoked", "inactive", "disabled"}:
        raise ValueError("unknown identity binding status")


# LLM: root为原LocalUserStore根；入组含重放须复核两类原账，总读取最多16MiB，损坏/链接/超限503且零身份源写入。
# 函数用途: 拒绝已有活跃IM绑定的普通本地用户加入审计组，保留未绑定和已撤销绑定账号的正常准入。
def reject_if_local_user_has_im_binding(root: Path, user_id: str) -> None:
    try:
        paths = _provider_binding_paths(root)
        remaining = _MAX_BINDING_BYTES
        for parts in [*paths, ("identity", "linked_identities.jsonl")]:
            data = read_bytes_beneath(root, parts, max_bytes=remaining)
            if data is None:
                if parts in paths:
                    raise ValueError("identity binding disappeared")
                continue
            remaining -= len(data)
            provider_index = len(parts) == 3
            for row in _current_bindings(data, provider_index=provider_index):
                if not provider_index and row.get("status", "active") != "active":
                    continue
                target = row.get("owner_id")
                local_target = target == f"local/{user_id}" or target == user_id and (
                    not provider_index or row.get("provider") == "local" and row.get("owner_kind", "user") == "user")
                if local_target:
                    raise HostAPIError("MANAGED_USER_WEB_ONLY_CHANNEL_RESTRICTED", 409,
                        message="已有IM身份绑定的普通本地账号不能加入审计组。")
    except HostAPIError:
        raise
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        raise HostAPIError("STORE_UNAVAILABLE", 503) from exc
