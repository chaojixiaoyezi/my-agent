# LLM: 模型子卡可显式只读既有key签名；默认首次创建/MAC公式与原payload合同不变，不从引用派生新权限。
# 模块用途: 保持单一交互密钥与结构化签名引用，缺失/坏密钥不在只读签名路径重建。
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


# LLM: 首次C模型菜单仅在原source内显式blocking=False尝试原key锁；默认True与create=False只读语义不变，不能用缺key证明许可。
# LLM: 只在宿主创建密钥，已有坏记录不重置；保护目录由 owner 初始化先建，文件与目录均用原私有权限写入。
# 函数用途: 读取单一 owner 的交互签名密钥，首次明确创建时原子落盘。
def interaction_key(owner_home: Path, *, create: bool = False, blocking: bool = True) -> bytes:
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
    with locked_json_path(path, **({} if blocking else {"blocking": False})):
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


# LLM: 默认创建合同不变；已有原MAC回调派子卡显式create_key=False只读原key，缺失/坏key不得重建。owner 来自宿主规范身份，payload 只是结构化引用；签名不含秘密正文，不允许调用方从 prompt 选择 owner。
# 函数用途: 生成原接收者可在私聊提交的精确交互引用。
def sign_interaction_reference(owner_home: Path, payload: dict[str, object], *, create_key: bool = True) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    signature = hmac.new(interaction_key(owner_home, create=create_key), b"im-interaction-v1\0" + raw, hashlib.sha256).hexdigest()
    return f"ir1.{encoded}.{signature}"


# LLM: 先校验格式/收件人/期限，再按纯 owner 解析读取已有密钥；失败不得创建 owner 或密钥，签名通过也不代替原执行/权限校验。
# 函数用途: 核验原操作者/已签群成员引用；只有cancel允许关闭过期原卡，其余动作拒绝过期。
def verify_interaction_reference(
    home_root: Path, reference: str, *, channel: str, actor_id: str, submitter: dict | None = None, decision: str = "",
) -> dict[str, object]:
    if len(reference) > 8192:
        raise ValueError("交互引用无效")
    prefix, encoded, signature = reference.split(".")
    if prefix != "ir1" or re.fullmatch(r"[0-9a-f]{64}", signature) is None:
        raise ValueError("交互引用版本无效")
    raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
    payload = json.loads(raw)
    expiry = float(payload.get("expires_at") or 0) if isinstance(payload, dict) else 0
    if (not isinstance(payload, dict) or payload.get("channel") != channel
            or not math.isfinite(expiry) or expiry <= 0 or (expiry <= time.time() and decision != "cancel")):
        raise ValueError("交互引用不属于此用户或已过期")
    owner = payload.get("owner")
    if not isinstance(owner, dict) or set(owner) != {"provider", "owner_kind", "owner_id"}:
        raise ValueError("交互引用归属无效")
    identity = OwnerIdentity(**owner)
    resolved = resolve_owner_home(home_root, identity)
    expected = hmac.new(interaction_key(resolved.home_dir), b"im-interaction-v1\0" + raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise ValueError("交互引用签名无效")
    validate_interaction_submitter(payload, actor_id=actor_id, submitter=submitter, decision=decision)
    return payload


# LLM: 群开放只适用于凭据输入及签名模型选择卡cancel；其它模型决定仍锁原actor，成员事实只来自认证适配器。
# 函数用途: 核对签名中的呈现范围与可信实际提交者，不向审批和模型设置扩散群权限。
def validate_interaction_submitter(payload, *, actor_id, submitter=None, decision=""):
    presentation = interaction_presentation(payload,decision)
    if presentation is None:
        if payload.get("actor_id") != actor_id:
            raise ValueError("交互引用不属于此用户")
        return
    owner = payload.get("owner") or {}
    if (payload.get("kind") not in {"task_credential","model_configuration"} or payload.get("channel") != "feishu"
            or owner.get("provider") != "feishu" or owner.get("owner_kind") != "group"
            or presentation != {"kind": "group", "chat_id": owner.get("owner_id")}
            or (payload.get("submitter_policy") if payload.get("kind")=="task_credential" else payload.get("cancellation_submitter_policy")) != "current_group_member"
            or submitter != {"actor_id": actor_id, "chat_id": owner.get("owner_id"), "current_group_member": True}
            or not actor_id):
        raise ValueError("群交互提交者范围无效")


# LLM: 模型群取消资格单独签名，不把cancel授权扩散到select/save/edit；原私人form没有群取消呈现。
# 函数用途: 取当前准确动作能使用的签名群范围，普通模型操作保持原actor限制。
def interaction_presentation(payload,decision=''):
    if payload.get('kind')=='model_configuration':
        if payload.get('presentation') is not None:
            raise ValueError('模型卡不能借用群凭据输入权限')
        return payload.get('cancellation_presentation') if decision=='cancel' and payload.get('mode')=='selection' else None
    return payload.get('presentation')
