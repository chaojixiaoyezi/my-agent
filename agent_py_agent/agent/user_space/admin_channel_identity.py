# LLM: 本模块是“哪些 IM 私聊身份已证明是管理员”的唯一权威（config/admin-channel-identities.json，0600，目录 0700）。
#   绑定只认 (channel, user_id) 的结构化精确匹配，不存昵称、正文或密码；只有 /admin 验证成功后的 Gateway 与本机 CLI 能写。
#   查询方遇到损坏文件 fail-closed（视为未绑定），写方遇到损坏文件拒绝覆盖。改动时同步检查 request_worker 的 owner 解析、
#   admin_control_service、cli/admin_identity_commands.py 与 test_admin_identity_store.py。
# 模块用途: 保存、列出、查询和解除管理员的 IM 私聊身份绑定，供 Gateway owner 解析与本机 CLI 共用。

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from pathlib import Path

from ..common.json_io import locked_json_path
from .admin_password import (
    CONFIG_DIR_NAME,
    AdminPasswordError,
    read_private_config_json,
    write_private_config_json,
)

ADMIN_CHANNEL_IDENTITIES_SCHEMA = "admin_channel_identities.v1"
_IDENTITIES_FILE = "admin-channel-identities.json"


# LLM: 文案固定，不含路径以外的秘密；调用方据此拒绝写入，不能把损坏当作“没有绑定”再覆盖。
# 类用途: 表示绑定文件不可读、已损坏或绑定参数无效。
class AdminIdentityStoreError(RuntimeError):
    pass


# LLM: channel 已规范为小写，user_id 保持渠道原样（渠道 ID 区分大小写）；key 形如 feishu:ou_x，也是 CLI 参数格式。
# 类用途: 表示一条已绑定为管理员的 IM 私聊身份及绑定时间。
@dataclass(frozen=True)
class AdminChannelIdentity:
    channel: str
    user_id: str
    bound_at: float = 0.0

    # LLM: key 只由两个结构化字段拼成，用于展示、CLI 定位和失败节流，不参与授权判断以外的推断。
    # 函数用途: 返回 `渠道:用户ID` 形式的身份键。
    @property
    def key(self) -> str:
        return f"{self.channel}:{self.user_id}"


# LLM: 路径只由可信 home 根推导。
# 函数用途: 返回管理员 IM 身份绑定文件位置。
def admin_channel_identities_path(home_root: str | Path) -> Path:
    return Path(home_root) / CONFIG_DIR_NAME / _IDENTITIES_FILE


# LLM: 两个字段都必须非空且不含空白；channel 统一小写，与 owner 解析的 casefold 口径一致。
# 函数用途: 规范并校验一对渠道身份，非法时抛 AdminIdentityStoreError。
def normalize_admin_channel_identity(channel: object, user_id: object) -> tuple[str, str]:
    normalized_channel = str(channel or "").strip().casefold()
    normalized_user = str(user_id or "").strip()
    if not normalized_channel or not normalized_user or any(
        character.isspace() for character in normalized_channel + normalized_user
    ):
        raise AdminIdentityStoreError("渠道身份必须是非空、无空白的渠道名和用户 ID。")
    return normalized_channel, normalized_user


# LLM: 只按第一个冒号切分，冒号前是渠道名；不做模糊匹配。
# 函数用途: 把 CLI 参数 `feishu:ou_x` 解析成渠道身份。
def parse_admin_channel_identity_key(value: object) -> tuple[str, str]:
    channel, separator, user_id = str(value or "").strip().partition(":")
    if not separator:
        raise AdminIdentityStoreError("身份格式应为 渠道:用户ID，例如 feishu:ou_xxx。")
    return normalize_admin_channel_identity(channel, user_id)


# LLM: 只读；缺失返回空列表，损坏抛 AdminIdentityStoreError（写入方和 CLI 需要知道）。
# 函数用途: 列出全部已绑定的管理员 IM 身份。
def list_admin_channel_identities(home_root: str | Path) -> list[AdminChannelIdentity]:
    try:
        payload = read_private_config_json(home_root, _IDENTITIES_FILE)
    except AdminPasswordError as exc:
        raise AdminIdentityStoreError("管理员身份绑定记录无法读取，未做任何更改。") from exc
    if payload is None:
        return []
    rows = payload.get("identities")
    if payload.get("schema") != ADMIN_CHANNEL_IDENTITIES_SCHEMA or not isinstance(rows, list):
        raise AdminIdentityStoreError("管理员身份绑定记录已损坏，未做任何更改。")
    identities: list[AdminChannelIdentity] = []
    for row in rows:
        if not isinstance(row, dict):
            raise AdminIdentityStoreError("管理员身份绑定记录已损坏，未做任何更改。")
        channel, user_id = normalize_admin_channel_identity(row.get("channel"), row.get("user_id"))
        try:
            bound_at = float(row.get("bound_at") or 0.0)
        except (TypeError, ValueError) as exc:
            raise AdminIdentityStoreError("管理员身份绑定记录已损坏，未做任何更改。") from exc
        if not math.isfinite(bound_at):
            raise AdminIdentityStoreError("管理员身份绑定记录已损坏，未做任何更改。")
        identities.append(AdminChannelIdentity(channel, user_id, bound_at))
    return identities


# LLM: 查询是授权入口，任何读取或格式错误都 fail-closed 返回 None；只做 (channel, user_id) 精确匹配。
# 函数用途: 查找某个 IM 身份是否已绑定为管理员。
def find_admin_channel_identity(
    home_root: str | Path,
    channel: object,
    user_id: object,
) -> AdminChannelIdentity | None:
    try:
        wanted = normalize_admin_channel_identity(channel, user_id)
        identities = list_admin_channel_identities(home_root)
    except AdminIdentityStoreError:
        return None
    return next((item for item in identities if (item.channel, item.user_id) == wanted), None)


# LLM: 有副作用：在文件锁内读改写绑定文件（0600）；同一身份重复绑定只刷新 bound_at，不产生重复行。
#   只能在密码校验成功之后调用；损坏文件抛 AdminIdentityStoreError，不覆盖。
# 函数用途: 把一个 IM 私聊身份登记为管理员。
def bind_admin_channel_identity(
    home_root: str | Path,
    channel: object,
    user_id: object,
    *,
    now: float | None = None,
) -> AdminChannelIdentity:
    identity = AdminChannelIdentity(*normalize_admin_channel_identity(channel, user_id), time.time() if now is None else float(now))
    path = admin_channel_identities_path(home_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with locked_json_path(path):
        others = [item for item in list_admin_channel_identities(home_root) if item.key != identity.key]
        _write_identities(home_root, [*others, identity])
    return identity


# LLM: 有副作用：在文件锁内删除一条精确匹配的绑定；返回是否确实删除。损坏文件抛错不覆盖。
# 函数用途: 解除一个 IM 身份的管理员绑定（/admin logout 或本机 CLI）。
def remove_admin_channel_identity(home_root: str | Path, channel: object, user_id: object) -> bool:
    key = AdminChannelIdentity(*normalize_admin_channel_identity(channel, user_id)).key
    path = admin_channel_identities_path(home_root)
    if not path.exists():
        return False
    with locked_json_path(path):
        identities = list_admin_channel_identities(home_root)
        remaining = [item for item in identities if item.key != key]
        if len(remaining) == len(identities):
            return False
        _write_identities(home_root, remaining)
    return True


# LLM: 有副作用：调用方已持文件锁；整表原子替换，行按 key 排序便于人工查看。
# 函数用途: 保存绑定列表。
def _write_identities(home_root: str | Path, identities: list[AdminChannelIdentity]) -> None:
    rows = [
        {"channel": item.channel, "user_id": item.user_id, "bound_at": item.bound_at}
        for item in sorted(identities, key=lambda item: item.key)
    ]
    try:
        write_private_config_json(
            home_root,
            _IDENTITIES_FILE,
            {"schema": ADMIN_CHANNEL_IDENTITIES_SCHEMA, "identities": rows},
        )
    except OSError as exc:
        raise AdminIdentityStoreError("管理员身份绑定记录无法写入，未做任何更改。") from exc


__all__ = [
    "AdminChannelIdentity",
    "AdminIdentityStoreError",
    "admin_channel_identities_path",
    "bind_admin_channel_identity",
    "find_admin_channel_identity",
    "list_admin_channel_identities",
    "normalize_admin_channel_identity",
    "parse_admin_channel_identity_key",
    "remove_admin_channel_identity",
]
