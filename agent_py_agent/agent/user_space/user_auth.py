# LLM: 认证只复用local_users、原provider索引及原IM SQLite密码；不物化owner、不授权插件执行或复制密码库。
# 模块用途: 为本地和飞书普通用户签发有期限的会话，并逐次复核当前身份、密码及禁用状态。
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import stat
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

from ..common.json_io import jsonl_lines
from ..common.nofollow_fs import (
    open_directory_beneath,
    read_bytes_beneath,
    write_bytes_atomic_beneath,
)
from ..common.path_segments import safe_path_segment
from ..plugin_host.errors import HostAPIError
from ..plugin_host.owner_context import reject_constant, unique_object, validate_local_user_id
from ..session_lock.passwords import verify_password
from ..session_lock.service import UnlockService
from ..session_lock.store import SessionLockStore
from .admin_channel_identity import AdminIdentityStoreError, list_admin_channel_identities
from .identity_store import ProviderIdentityRecord
from .local_user_channel_policy import require_local_user_channel
from .local_user_lifecycle import LocalUserLifecycle
from .local_user_store import LocalUserStore
from .owner_admin_controls import read_owner_admin_controls
from .owner_resolver import OwnerIdentity, resolve_owner_home

_SECRET_PARTS = ("local_users", ".auth_session_secret")
_TOKEN_FIELDS = frozenset({"schema", "principal_id", "account_source", "owner_provider", "owner_kind", "owner_id",
                           "revision", "issued_at", "expires_at", "credential_fingerprint", "tenant_or_app_key"})
_FEISHU_SERVICES = OrderedDict()
_SERVICE_LOCK = threading.Lock()


# LLM: 仅可信宿主组合根绑定已运行IM的原UnlockService，注册表只持引用不复制失败账；None撤销，最多保留64根。
# 函数用途: 将新登录入口连接到原飞书锁定窗，缺接线必须失败关闭而不是另造空锁。
def bind_feishu_auth_service(root, service):
    root = Path(root).resolve()
    if service is not None:
        if type(service) is not UnlockService or type(service.store) is not SessionLockStore:
            raise HostAPIError("SOURCE_INVALID", 403)
        if Path(service.store._path).resolve() != root / "session_lock/private_chat_locks.db":
            raise HostAPIError("SOURCE_INVALID", 403)
    with _SERVICE_LOCK:
        _FEISHU_SERVICES.pop(str(root), None)
        if service is not None:
            _FEISHU_SERVICES[str(root)] = service
            while len(_FEISHU_SERVICES) > 64:
                _FEISHU_SERVICES.popitem(last=False)


# LLM: 只认已由宿主绑定的原实例；逐次核原库路径和实时monotonic锁定窗，不创建或清空IM锁。
# 函数用途: 获取当前飞书认证原服务，未接线或仍锁定时拒绝登录和旧会话。
def _feishu_service(root, uid):
    with _SERVICE_LOCK:
        service = _FEISHU_SERVICES.get(str(root))
    if service is None or Path(service.store._path).resolve() != root / "session_lock/private_chat_locks.db":
        raise HostAPIError("HOST_NOT_READY", 503)
    if service.lockout_remaining(uid) > 0:
        raise HostAPIError("ACCOUNT_LOCKED", 403)
    return service


# LLM: DTO不提供管理员标志或执行许可，token不进入repr；公开会话仅供新入口运输，再由原授权链求交。
# 类用途: 返回已认证普通用户的稳定身份和有期限的签名会话。
@dataclass(frozen=True)
class AuthenticatedUserSession:
    principal_id: str
    account_source: str
    effective_owner: OwnerIdentity
    owner_id: str
    display_name: str
    revision: int
    issued_at: int
    expires_at: int
    session_token: str = field(repr=False)

    # LLM: 仅显式会话字段可公开，不含密码散列、HMAC密钥或owner目录。
    # 函数用途: 给插件登录响应提供闭合的安全投影。
    def public_view(self):
        return {"principal_id": self.principal_id, "account_source": self.account_source, "owner_id": self.owner_id,
                "display_name": self.display_name, "revision": self.revision, "issued_at": self.issued_at,
                "expires_at": self.expires_at, "session_token": self.session_token}


# LLM: 私有凭据快照不进入返回或日志；current每次重新读取，不能作为长期授权缓存。
# 类用途: 把同一次账号解析的owner、修订与密码散列交给认证函数。
@dataclass(frozen=True)
class _Credential:
    principal_id: str
    owner: OwnerIdentity
    display_name: str
    revision: int
    password_hash: str = field(repr=False)


# LLM: 仅密码散列的指纹进入签名载荷，用于检测原IM库改密；不是密码明文或快速密码摘要。
# 函数用途: 生成凭据状态的不可逆校验指纹。
def _fingerprint(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# LLM: 输入不做路径清洗或姓名匹配，允许的命名空间前缀必须与来源完全一致。
# 函数用途: 把来源限定的登录编号转换为原账号的准确稳定ID。
def _subject(account_source, user_id):
    if type(account_source) is not str or account_source not in {"local", "feishu"} or type(user_id) is not str:
        raise HostAPIError("INVALID_SCHEMA")
    prefix = "local/" if account_source == "local" else "providers/feishu/users/"
    uid = user_id.removeprefix(prefix)
    if account_source == "local":
        return validate_local_user_id(uid)
    if not uid or safe_path_segment(uid) != uid:
        raise HostAPIError("INVALID_SCHEMA")
    return uid


# LLM: 不创建目录，目录链使用原no-follow打开；原维护状态及当前admin_controls任一拒绝均阻止认证。
# 函数用途: 复核规范owner确实存在且没有被管理员停用。
def _require_live_owner(root, owner):
    home = resolve_owner_home(root, owner)
    try:
        descriptor = open_directory_beneath(root, home.home_dir.relative_to(root).parts, require_dir_fd=True)
        os.close(descriptor)
    except FileNotFoundError:
        raise HostAPIError("IDENTITY_NOT_FOUND", 404, message="Feishu user has not connected via IM yet") from None
    except OSError:
        raise HostAPIError("STORE_UNAVAILABLE", 503) from None
    store = LocalUserStore(root)
    parts = home.home_dir.relative_to(root).parts
    status = store.read_json((*parts, "owner_status.json"))
    if status is not None and status.get("status") != "active":
        raise HostAPIError("USER_DISABLED", 403)
    store.read_json((*parts, "tool_policy.json"))
    controls = read_owner_admin_controls(SimpleNamespace(owner_tool_policy_json=home.tool_policy_json))
    if controls.get("source") == "unreadable":
        raise HostAPIError("STORE_UNAVAILABLE", 503)
    if controls.get("disabled", False) is not False:
        raise HostAPIError("USER_DISABLED", 403)
    return home


# LLM: 原IM密码按uid而非tenant索引；同uid跨tenant映射不同owner一律拒绝，即使显式选tenant，防凭据跨域。
# 函数用途: 读取原飞书索引的最新准确主体，坏行或跨owner歧义失败关闭，不按显示名关联。
def _feishu_record(root, uid, tenant):
    try:
        data = read_bytes_beneath(root, ("identity", "provider_identity", "feishu.jsonl"),
                                  max_bytes=2097152, require_dir_fd=True)
        rows = {}
        for line in jsonl_lines(data.decode("utf-8") if data is not None else ""):
            if not line.strip():
                continue
            row = json.loads(line, object_pairs_hook=unique_object, parse_constant=reject_constant)
            if type(row) is not dict:
                raise ValueError("invalid identity")
            if row.get("provider_subject_id") == uid:
                key = row.get("tenant_or_app_key", "")
                if type(key) is not str:
                    raise ValueError("invalid tenant")
                rows[key] = row
    except (OSError, ValueError, TypeError, RecursionError):
        raise HostAPIError("STORE_UNAVAILABLE", 503) from None
    bindings = {(record.provider, record.owner_kind, record.owner_id)
                for value in rows.values() for record in (ProviderIdentityRecord.from_dict(value),)}
    if len(bindings) > 1:
        raise HostAPIError("IDENTITY_AMBIGUOUS", 409)
    if tenant:
        if tenant not in rows:
            raise HostAPIError("IDENTITY_NOT_FOUND", 404)
        return rows[tenant]
    if len(rows) > 1:
        raise HostAPIError("IDENTITY_AMBIGUOUS", 409)
    return next(iter(rows.values()), None)


# LLM: provider身份为绑定权威，旧local/user绑定也必须先过纯Web门；管理员仍拒普通登录。
# 函数用途: 将飞书准确身份绑定解析成原有普通owner。
def _feishu_owner(root, uid, row):
    try:
        if any(item.channel == "feishu" and item.user_id == uid for item in list_admin_channel_identities(root)):
            raise HostAPIError("ADMIN_OWNER_FORBIDDEN", 403)
    except AdminIdentityStoreError:
        raise HostAPIError("STORE_UNAVAILABLE", 503) from None
    if row is None:
        return OwnerIdentity.provider_user("feishu", uid)
    if row.get("status", "active") != "active":
        raise HostAPIError("USER_DISABLED", 403, message="provider identity is not active")
    record = ProviderIdentityRecord.from_dict(row)
    require_local_user_channel(record, "feishu")
    if record.provider == "local" and record.owner_kind == "main":
        raise HostAPIError("ADMIN_OWNER_FORBIDDEN", 403)
    if record.owner_kind != "user" or not record.owner_id or safe_path_segment(record.owner_id) != record.owner_id:
        raise HostAPIError("STORE_UNAVAILABLE", 503)
    if record.provider == "local":
        if record.owner_id == "main":
            raise HostAPIError("ADMIN_OWNER_FORBIDDEN", 403)
        return OwnerIdentity.local_user(validate_local_user_id(record.owner_id))
    if record.provider != "feishu":
        raise HostAPIError("STORE_UNAVAILABLE", 503)
    return OwnerIdentity.provider_user("feishu", record.owner_id)


# LLM: 原SessionLockStore构造为read_only，不迁移/创建密码库；修订绑定原凭据及身份，不维护第二密码账。
# 函数用途: 读取飞书原IM密码和已绑定owner，形成仅供本次验证的内部快照。
def _feishu_credential(root, uid, tenant):
    row = _feishu_record(root, uid, tenant)
    owner = _feishu_owner(root, uid, row)
    _require_live_owner(root, owner)
    local_revision = 0
    if owner.provider == "local":
        local_revision = LocalUserStore(root).require_enabled(owner.owner_id).revision
    stored = _im_password(root, uid)
    if not stored:
        raise HostAPIError("PASSWORD_NOT_CONFIGURED", 401)
    _feishu_service(root, uid)
    binding = json.dumps([row, local_revision, _fingerprint(stored)], sort_keys=True, separators=(",", ":"))
    revision = int.from_bytes(hashlib.sha256(binding.encode()).digest()[:8], "big") & (2**63 - 1)
    return _Credential("providers/feishu/users/" + uid, owner, str((row or {}).get("display_name") or uid), revision or 1, stored)


# LLM: 原库读取前核父链与普通单链接SQLite头；实际hash仍由原只读Store取得，缺库与坏库分别报告。
# 函数用途: 安全读取原IM密码，不跟随密码库符号链接或创建数据库。
def _im_password(root, uid):
    try:
        parent = open_directory_beneath(root, ("session_lock",), require_dir_fd=True)
        try:
            descriptor = os.open("private_chat_locks.db", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
            with os.fdopen(descriptor, "rb") as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or handle.read(16) != b"SQLite format 3\x00":
                    raise ValueError("invalid password database")
        finally:
            os.close(parent)
        with _SERVICE_LOCK:
            return SessionLockStore(root / "session_lock/private_chat_locks.db", read_only=True).password_hash(uid)
    except FileNotFoundError:
        raise HostAPIError("PASSWORD_NOT_CONFIGURED", 401) from None
    except (OSError, ValueError, sqlite3.Error):
        raise HostAPIError("STORE_UNAVAILABLE", 503) from None


# LLM: 每次从原账号/IM库读取；本地启用复核必须绑定首次观察的revision，不能混用旧hash和新状态；不授插件执行权。
# 函数用途: 为登录与验会话读取同一修订的当前有效账号，遇到并发改密立即拒绝旧快照。
def _current_credential(root, source, uid, tenant):
    if source == "feishu":
        return _feishu_credential(root, uid, tenant)
    store = LocalUserStore(root)
    user = store.read_user(uid)
    if user is None:
        raise HostAPIError("INVALID_CREDENTIALS", 401, message="invalid user_id or password")
    user = store.require_enabled(uid, user.revision)
    _require_live_owner(root, user.owner)
    if not user.password_hash:
        raise HostAPIError("INVALID_CREDENTIALS", 401)
    return _Credential("local/" + uid, user.owner, user.display_name, user.revision, user.password_hash)


# LLM: 只有成功验密才可create；原文件锁序列化并发首建，no-follow私有原子写且绝不覆盖已有有效密钥。
# 函数用途: 读取或一次性创建当前根的32字节HMAC密钥，验证失败不产生文件。
def _session_secret(root, *, create=False):
    try:
        data = read_bytes_beneath(root, _SECRET_PARTS, max_bytes=32, require_dir_fd=True)
        if data is None and create:
            with LocalUserStore(root).locked(_SECRET_PARTS):
                data = read_bytes_beneath(root, _SECRET_PARTS, max_bytes=32, require_dir_fd=True)
                if data is None:
                    data = secrets.token_bytes(32)
                    write_bytes_atomic_beneath(root, _SECRET_PARTS, data)
        if data is None:
            raise HostAPIError("INVALID_SESSION", 401)
        if len(data) != 32:
            raise HostAPIError("STORE_UNAVAILABLE", 503)
        return data
    except (OSError, ValueError) as exc:
        if isinstance(exc, HostAPIError):
            raise
        raise HostAPIError("STORE_UNAVAILABLE", 503) from None


# LLM: 使用无padding的规范URL安全Base64；只编码非秘密签名载荷和MAC。
# 函数用途: 生成紧凑的会话传输片段。
def _encode(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


# LLM: 拒绝宽松Base64别名及非法编码，长度上限由原token读取入口统一约束。
# 函数用途: 将规范会话片段还原为字节用于验签。
def _decode(text):
    data = base64.b64decode(text + "=" * (-len(text) % 4), altchars=b"-_", validate=True)
    if _encode(data) != text:
        raise ValueError("invalid token encoding")
    return data


# LLM: 密码不进token，owner来自当前磁盘认证；仅成功后可创建HMAC密钥，慢验密后再读状态防改密竞态。
# 函数用途: 认证本地或已有飞书普通用户并签发有限期限会话。
def authenticate_user(root: Path, *, account_source: str, user_id: str, password: str,
                      tenant_or_app_key: str = "", ttl_seconds: int = 86400) -> AuthenticatedUserSession:
    root = Path(root).resolve()
    uid = _subject(account_source, user_id)
    if type(tenant_or_app_key) is not str or len(tenant_or_app_key) > 256:
        raise HostAPIError("INVALID_SCHEMA")
    if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 604800:
        raise HostAPIError("INVALID_SCHEMA")
    current = _current_credential(root, account_source, uid, tenant_or_app_key)
    if account_source == "local":
        LocalUserLifecycle(LocalUserStore(root)).verify_user_password(uid, password)
    elif not isinstance(password, str) or not _feishu_service(root, uid).unlock(uid, password):
        raise HostAPIError("INVALID_CREDENTIALS", 401, message="invalid user_id or password")
    if _current_credential(root, account_source, uid, tenant_or_app_key) != current:
        raise HostAPIError("SESSION_REVOKED", 401)
    issued = int(time.time())
    payload = {"schema": "user-auth-session.v1", "principal_id": current.principal_id, "account_source": account_source,
               "owner_provider": current.owner.provider, "owner_kind": current.owner.owner_kind, "owner_id": current.owner.owner_id,
               "revision": current.revision, "credential_fingerprint": _fingerprint(current.password_hash),
               "issued_at": issued, "expires_at": issued + ttl_seconds, "tenant_or_app_key": tenant_or_app_key}
    body = _encode(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    signature = _encode(hmac.digest(_session_secret(root, create=True), body.encode("ascii"), "sha256"))
    return _session(current, account_source, issued, issued + ttl_seconds, body + "." + signature, root)


# LLM: 固定公开DTO与规范resolver产生owner_id；不把内部密码或指纹复制进结果。
# 函数用途: 为签发和验证构造同一种安全会话返回值。
def _session(current, source, issued, expires, token, root):
    return AuthenticatedUserSession(current.principal_id, source, current.owner,
                                    resolve_owner_home(root, current.owner).owner_id, current.display_name,
                                    current.revision, issued, expires, token)


# LLM: HMAC先于解析身份，严格闭合载荷和类型，拒绝重复键/非有限数；失败不生成密钥或owner。
# 函数用途: 还原一个签名正确且结构合法的会话载荷。
def _token_payload(root, token):
    try:
        if type(token) is not str or not 1 <= len(token) <= 8192:
            raise ValueError("invalid token")
        body, signature = token.split(".")
        if not hmac.compare_digest(hmac.digest(_session_secret(root), body.encode("ascii"), "sha256"), _decode(signature)):
            raise ValueError("invalid signature")
        payload = json.loads(_decode(body), object_pairs_hook=unique_object, parse_constant=reject_constant)
        if type(payload) is not dict or set(payload) != _TOKEN_FIELDS or payload["schema"] != "user-auth-session.v1":
            raise ValueError("invalid payload")
        if any(type(payload[key]) is not int or payload[key] < 0 for key in ("revision", "issued_at", "expires_at")):
            raise ValueError("invalid time or revision")
        if any(type(value) is not str for key, value in payload.items() if key not in {"revision", "issued_at", "expires_at"}):
            raise ValueError("invalid identity")
        if not 0 < payload["expires_at"] - payload["issued_at"] <= 604800:
            raise ValueError("invalid lifetime")
        return payload
    except (ValueError, TypeError, UnicodeError, binascii.Error, RecursionError):
        raise HostAPIError("INVALID_SESSION", 401) from None


# LLM: 每次验签后现读账号/IM密码/绑定及禁用，指纹与修订同时匹配；不把token当插件或管理员授权。
# 函数用途: 验证会话期限和当前身份，禁用或改密立即拒绝旧会话。
def verify_user_session(root: Path, session_token: str, *, now: int | None = None) -> AuthenticatedUserSession:
    root = Path(root).resolve()
    payload = _token_payload(root, session_token)
    moment = int(time.time()) if now is None else now
    if type(moment) is not int:
        raise HostAPIError("INVALID_SCHEMA")
    if moment >= payload["expires_at"]:
        raise HostAPIError("SESSION_EXPIRED", 401)
    if moment < payload["issued_at"]:
        raise HostAPIError("INVALID_SESSION", 401)
    source = payload["account_source"]
    uid = _subject(source, payload["principal_id"])
    current = _current_credential(root, source, uid, payload["tenant_or_app_key"])
    if (current.owner != OwnerIdentity(payload["owner_provider"], payload["owner_kind"], payload["owner_id"])
            or current.revision != payload["revision"] or current.principal_id != payload["principal_id"]
            or not hmac.compare_digest(_fingerprint(current.password_hash), payload["credential_fingerprint"])):
        raise HostAPIError("SESSION_REVOKED", 401)
    return _session(current, source, payload["issued_at"], payload["expires_at"], session_token, root)
