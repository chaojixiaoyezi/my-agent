# LLM: 私密交互引用只证明宿主发出的精确 owner/执行/接收者投影；验证方仍须核对当前 actor、执行代次和原账，不将引用当工具批准。
# 模块用途: 为跨群/私聊审批及凭据卡签名，密钥留在原受保护的 owner 托管存储，不进入模型或日志。
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import re
import secrets
import time
from pathlib import Path

from ..common.json_io import (
    locked_json_path,
    read_json_object_report,
    write_private_text_file_atomic_unlocked,
)
from ..common.nofollow_fs import ensure_private_dir
from ..path_access_policy import PRIVATE_INTERACTIONS_OWNER_PARTS
from .owner_resolver import OwnerIdentity, resolve_owner_home


# LLM: 只在宿主创建密钥，已有坏记录不重置；保护目录由 owner 初始化先建，文件与目录均用原私有权限写入。
# 函数用途: 读取单一 owner 的交互签名密钥，首次明确创建时原子落盘。
def interaction_key(owner_home: Path, *, create: bool = False) -> bytes:
    directory = owner_home.joinpath(*PRIVATE_INTERACTIONS_OWNER_PARTS)
    path = directory / "key.json"
    if directory.is_symlink() or path.is_symlink() or not directory.resolve().is_relative_to(owner_home.resolve()):
        raise ValueError("私密交互存储位置无效")
    if create:
        ensure_private_dir(directory)
    if not create:
        if not path.is_file():
            raise ValueError("私密交互密钥不可用")
        return _key_from_payload(read_json_object_report(path, context="interaction.key").payload)
    with locked_json_path(path):
        report = read_json_object_report(path, context="interaction.key")
        if report.load_error:
            raise ValueError("私密交互密钥不可读")
        payload = report.payload
        if not payload and not path.exists():
            payload = {"schema_version": "private_interaction_key.v1", "key": base64.b64encode(secrets.token_bytes(32)).decode()}
            write_private_text_file_atomic_unlocked(path, json.dumps(payload))
        return _key_from_payload(payload)


# LLM: 密钥只从固定版本记录读取；不补造、不打印原字节，坏记录使引用不可用。
# 函数用途: 校验并解码已有的 32 字节签名密钥。
def _key_from_payload(payload: dict[str, object]) -> bytes:
    if payload.get("schema_version") != "private_interaction_key.v1":
        raise ValueError("私密交互密钥不可用")
    value = base64.b64decode(str(payload.get("key") or ""), validate=True)
    if len(value) != 32:
        raise ValueError("私密交互密钥格式无效")
    return value


# LLM: owner 来自宿主规范身份，payload 只是结构化引用；签名不含秘密正文，不允许调用方从 prompt 选择 owner。
# 函数用途: 生成原接收者可在私聊提交的精确交互引用。
def sign_interaction_reference(owner_home: Path, payload: dict[str, object]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    signature = hmac.new(interaction_key(owner_home, create=True), b"im-interaction-v1\0" + raw, hashlib.sha256).hexdigest()
    return f"ir1.{encoded}.{signature}"


# LLM: 先校验格式/收件人/期限，再按纯 owner 解析读取已有密钥；失败不得创建 owner 或密钥，签名通过也不代替原执行/权限校验。
# 函数用途: 核验私聊原操作者提交的引用，拒绝转发、改字段、过期和错误用户。
def verify_interaction_reference(
    home_root: Path, reference: str, *, channel: str, actor_id: str,
) -> dict[str, object]:
    if len(reference) > 8192:
        raise ValueError("交互引用无效")
    prefix, encoded, signature = reference.split(".")
    if prefix != "ir1" or re.fullmatch(r"[0-9a-f]{64}", signature) is None:
        raise ValueError("交互引用版本无效")
    raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
    payload = json.loads(raw)
    expiry = float(payload.get("expires_at") or 0) if isinstance(payload, dict) else 0
    if (not isinstance(payload, dict) or payload.get("channel") != channel or payload.get("actor_id") != actor_id
            or not math.isfinite(expiry) or expiry <= time.time()):
        raise ValueError("交互引用不属于此用户或已过期")
    owner = payload.get("owner")
    if not isinstance(owner, dict) or set(owner) != {"provider", "owner_kind", "owner_id"}:
        raise ValueError("交互引用归属无效")
    identity = OwnerIdentity(**owner)
    resolved = resolve_owner_home(home_root, identity)
    expected = hmac.new(interaction_key(resolved.home_dir), b"im-interaction-v1\0" + raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise ValueError("交互引用签名无效")
    return payload
