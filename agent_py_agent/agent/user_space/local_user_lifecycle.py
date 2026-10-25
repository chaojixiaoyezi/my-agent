# LLM: 生命周期沿操作→用户→owner quota→JSON短锁发布，未完成物化不得标成enabled。
# 模块用途: 实现普通本地账号创建、密码管理、启停和与原来源绑定的耐久幂等。
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from ..common.nofollow_fs import ensure_private_dir, open_directory_beneath
from ..plugin_host.errors import HostAPIError
from ..plugin_host.owner_context import validate_local_user_id
from ..session_lock.passwords import hash_password, validate_password_policy, verify_password
from .local_user_store import LocalUser, parse_local_user
from .owner_quota import LocalUserQuotaLimits, OwnerQuotaUnavailable, seed_local_user_quota
from .owner_resolver import OwnerIdentity


# LLM: 只绑定领域操作来源，上游adapter另行核验宿主签发的调用；不能把本DTO当作授权。
# 类用途: 固定安装、激活和原幂等键。
@dataclass(frozen=True)
class LocalUserOperation:
    installation_ref: str
    activation_id: str
    idempotency_key: str

    # LLM: 类型和长度先于任何磁盘副作用，activation不得接受空值或宽松强转。
    # 函数用途: 校验内部操作标识的固定格式。
    def validate(self):
        if type(self.installation_ref) is not str or not 1 <= len(self.installation_ref) <= 200:
            raise HostAPIError("INVALID_SCHEMA")
        if type(self.activation_id) is not str or re.fullmatch(r"[0-9a-f]{64}", self.activation_id) is None:
            raise HostAPIError("INVALID_SCHEMA")
        if type(self.idempotency_key) is not str or re.fullmatch(r"[A-Za-z0-9_-]{16,128}", self.idempotency_key) is None:
            raise HostAPIError("INVALID_SCHEMA")

    # LLM: 索引不包含user_id或activation，防止换用户/换激活用同key另跑一次。
    # 函数用途: 计算安装和动作范围内稳定的原操作编号。
    def identifier(self, action):
        return digest([self.installation_ref, action, self.idempotency_key])


# LLM: 回执包含原终态快照，重放不会重新执行；资源状态未观察时为None。
# 类用途: 返回已提交用户状态和幂等来源。
@dataclass(frozen=True)
class LocalUserResult:
    user: LocalUser
    operation_id: str
    replayed: bool = False
    resources_stopped: bool | None = None

    # LLM: 宿主便捷调用可解包为公开状态和重放标记，不把私有LocalUser密码散列交给界面。
    # 函数用途: 提供与命令侧二元返回约定一致的安全解包形式。
    def __iter__(self):
        yield self.user.public_view()
        yield self.replayed


# LLM: 使用规范JSON而非repr，输入摘要不暴露原键且稳定跨重启。
# 函数用途: 为操作意图及配额种子生成绑定摘要。
def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


# LLM: 保留原无密码操作摘要算法；密码先替换成固定摘要，规范JSON与索引从不接触原文。
# 函数用途: 为含密码的生命周期意图计算可重试、可冲突检测的摘要。
def _payload_hash(operation, payload):
    safe = dict(payload)
    if "password" in safe:
        safe["password_sha256"] = hashlib.sha256(str(safe.pop("password")).encode("utf-8")).hexdigest()
    return digest(safe)


# LLM: 复用原scrypt策略，其实现通过异常报告原因；校验及慢散列必须先于文件锁和物化。
# 函数用途: 拒绝非字符串或弱口令并生成私有密码散列。
def _validate_and_hash_password(password):
    if not isinstance(password, str) or not password:
        raise HostAPIError("INVALID_PASSWORD_POLICY", 400, message="密码必须是非空字符串。")
    try:
        reason = validate_password_policy(password)
        if reason is not None:
            raise ValueError(reason)
        return hash_password(password)
    except ValueError as exc:
        raise HostAPIError("INVALID_PASSWORD_POLICY", 400, message=str(exc)[:256]) from None


# LLM: 仅宿主已授权管理入口使用；namespace只绑定幂等来源，不伪装插件授权或替代上游管理员校验。
# 函数用途: 将宿主管理命令的幂等键转换为原事务使用的明确宿主来源。
def _host_operation(idempotency_key, actor_owner):
    owner = actor_owner or OwnerIdentity.local_main()
    if owner != OwnerIdentity.local_main():
        raise HostAPIError("CAPABILITY_DENIED", 403)
    return LocalUserOperation("host:local/main", digest(["host", "local/main"]), idempotency_key)


# LLM: 仅生成带UTC时区时间戳，不以本地时钟推断授权有效性。
# 函数用途: 记录账号发布和修订的审计时间。
def now_utc():
    return datetime.now(timezone.utc).isoformat()


# LLM: 上游提供无长等待的权威复查口；服务只管理本地事务，不代替请求队列或管理员授权。
# 类用途: 在同一原操作和账号修订上执行短生命周期事务。
class LocalUserLifecycle:
    # LLM: 构造冷且无文件副作用；取消回调只在发布停用后、所有锁外调用。
    # 函数用途: 绑定存储、提交复查和资源取消端口。
    def __init__(self, store, revalidate=None, stop_resources=None):
        self.store = store
        self.revalidate = revalidate
        self.stop_resources = stop_resources

    # LLM: 宿主传入的复查口必须检查原激活及grant，不接受插件自带回调或配置。
    # 函数用途: 在写事务入口和发布点重新检查来源仍有效。
    def check_authority(self):
        if self.revalidate is not None:
            self.revalidate()

    # LLM: 原插件operation/复查口保持；宿主命令可传明确幂等键，密码慢散列先于所有文件副作用。
    # 函数用途: 在原事务内创建或恢复普通本地账号，并可一次设置初始密码。
    def create(self, user_id, operation=None, *, display_name="", quota=None, password=None,
               quotas=None, idempotency_key=None, actor_owner=None):
        uid = validate_local_user_id(user_id)
        operation = operation or _host_operation(idempotency_key, actor_owner)
        operation.validate()
        password_hash = _validate_and_hash_password(password) if password is not None and password != "" else None
        if quotas is not None:
            if quota is not None:
                raise HostAPIError("INVALID_SCHEMA")
            quota = quotas
        display_name = "" if display_name is None else display_name
        if type(display_name) is not str or len(display_name) > 80:
            raise HostAPIError("INVALID_SCHEMA")
        try:
            limits = LocalUserQuotaLimits.parse(quota)
        except ValueError as exc:
            raise HostAPIError("INVALID_SCHEMA") from exc
        intent = {"user_id": uid, "display_name": display_name, "quota": limits.to_dict()}
        if password is not None:
            intent["password"] = password
        op_id = operation.identifier("users.local.create")
        self.check_authority()
        with self.store.locked(("local_users", "operations", op_id)):
            self.bind_operation(operation, "users.local.create", intent)
            with self.store.locked(("local_users", f"{uid}.json")):
                record = self.store.read_record(uid)
                replay = self.replay(record, op_id)
                if replay is not None:
                    return replay
                record = self.begin(uid, record, op_id, display_name, password_hash)
                return self.provision(record, limits, op_id)

    # LLM: 密码先按任务合同去明文，再用同根私有密钥HMAC保护落盘摘要；用途前缀与会话签名隔离，非密码旧算法不变。
    # 函数用途: 固定原操作来源和安全输入摘要，既检测换密码重试也避免索引变成快速离线口令验证器。
    def bind_operation(self, operation, action, intent):
        op_id = operation.identifier(action)
        parts = ("local_users", "operations", f"{op_id}.json")
        binding = {"installation_ref": operation.installation_ref, "activation_id": operation.activation_id,
                   "action": action, "input_digest": _payload_hash(action, intent), "user_id": intent["user_id"]}
        if "password" in intent:
            from .user_auth import _session_secret
            self.check_authority()
            body = b"local-user-intent.v1\0" + action.encode("utf-8") + b"\0" + binding["input_digest"].encode("ascii")
            binding["input_digest"] = hmac.digest(_session_secret(self.store.root, create=True), body, "sha256").hex()
            binding["input_digest_scheme"] = "password-hmac-sha256.v1"
        existing = self.store.read_json(parts)
        if existing is None:
            self.check_authority()
            self.store.write_json(parts, binding)
        elif existing.get("activation_id") != operation.activation_id:
            raise HostAPIError("ACTIVATION_REVOKED", 409)
        elif existing != binding:
            raise HostAPIError("IDEMPOTENCY_CONFLICT", 409)

    # LLM: 原结果与账号状态同一文件，重放返回当时结果而不启动资源或重新写配额。
    # 函数用途: 查询已发布操作回执。
    def replay(self, record, op_id):
        if record is None or op_id not in record["operations"]:
            return None
        self.check_authority()
        raw = record["operations"][op_id]
        return LocalUserResult(parse_local_user(raw, record["user"]["user_id"]), op_id, True)

    # LLM: 未登记目录不接管，原操作恢复不覆盖已保存密码；散列与账号一起私有原子存储。
    # 函数用途: 保存含可选密码的创建意图和provisioning状态。
    def begin(self, uid, record, op_id, display_name, password_hash=None):
        if record is not None:
            if record["provision"].get("operation_id") != op_id:
                raise HostAPIError("USER_EXISTS", 409)
            return record
        homes = (self.store.owner_home(uid).home_dir, self.store.root / "owners" / "providers" / "local" / "users" / uid)
        if any(home.exists() or home.is_symlink() for home in homes):
            raise HostAPIError("USER_UNMANAGED", 409)
        timestamp = now_utc()
        user = LocalUser(uid, "provisioning", 1, display_name, timestamp, timestamp, 1,
                         password_hash=password_hash, password_updated_at=timestamp if password_hash else None)
        record = {"schema": "local-user.v1", "user": asdict(user), "operations": {},
                  "provision": {"operation_id": op_id, "phase": "reserved"}}
        self.check_authority()
        self.store.save(uid, record)
        return record

    # LLM: 逐项物化并读回后才发布enabled；失败保留原操作和半成品，不删除用户目录。
    # 函数用途: 完成同一账号的目录、策略、有限配额和成功回执。
    def provision(self, record, limits, op_id):
        uid = record["user"]["user_id"]
        try:
            self.materialize_home(record)
            home = self.store.owner_home(uid).home_dir
            self.check_authority()
            seed_local_user_quota(self.store.root, home, limits)
            self.seed_policies(uid)
            if self.store.directory_identity(("owners", "local", uid)) != record["provision"]["home_identity"]:
                raise HostAPIError("STORE_UNAVAILABLE", 503)
            self.check_authority()
            record["user"].update(status="enabled", updated_at=now_utc())
            record["provision"].update(phase="committed", quota_digest=digest(limits.to_dict()))
            record["operations"][op_id] = dict(record["user"])
            self.store.save(uid, record)
        except (OSError, HostAPIError, OwnerQuotaUnavailable) as exc:
            observed = self.store.read_record(uid)
            if observed is not None and op_id in observed["operations"]:
                return LocalUserResult(parse_local_user(observed["operations"][op_id], uid), op_id)
            record["user"]["status"] = "provision_failed"
            record["provision"]["phase"] = "failed"
            record["operations"].pop(op_id, None)
            self.store.save(uid, record)
            if isinstance(exc, HostAPIError):
                raise
            raise HostAPIError("STORE_UNAVAILABLE", 503) from exc
        return LocalUserResult(parse_local_user(record["user"], uid), op_id)

    # LLM: mkdir排他新建，已记录inode才可恢复；崩溃留下无归属目录时保持失败，不猜所有权。
    # 函数用途: 创建普通用户独立home并记录真实目录身份。
    def materialize_home(self, record):
        uid = record["user"]["user_id"]
        parent = self.store.root / "owners" / "local"
        ensure_private_dir(parent, canonical_root=self.store.root)
        if "home_identity" not in record["provision"]:
            descriptor = open_directory_beneath(self.store.root, ("owners", "local"), require_dir_fd=True)
            try:
                os.mkdir(uid, mode=0o700, dir_fd=descriptor)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            record["provision"]["home_identity"] = self.store.directory_identity(("owners", "local", uid))
            self.store.save(uid, record)
        actual = self.store.directory_identity(("owners", "local", uid))
        if actual != record["provision"]["home_identity"]:
            raise HostAPIError("STORE_UNAVAILABLE", 503)
        home = self.store.owner_home(uid).home_dir
        for name in ("workspace", "tasks", "memory", "data", "config", "tmp"):
            ensure_private_dir(home / name, canonical_root=self.store.root)

    # LLM: 只种当前普通用户策略，已有不同策略不覆盖；权限不继承local/main或全局full-access。
    # 函数用途: 物化并核对受限权限、工具策略和独立维护状态。
    def seed_policies(self, uid):
        policies = {
            "permissions.json": {"schema_version": "permissions.v1", "filesystem": {"access_mode": "workspace-write"},
                                 "network": {"enabled": False}},
            "tool_policy.json": {"schema_version": "tool-policy.v1", "disabled_tools": []},
            "skill_policy.json": {"schema_version": "skill-policy.v1", "disabled_skills": []},
            "owner_status.json": {"schema_version": "owner-status.v1", "status": "active"},
        }
        for name, policy in policies.items():
            parts = ("owners", "local", uid, name)
            existing = self.store.read_json(parts)
            if existing is None:
                self.store.write_json(parts, policy)
            elif existing != policy:
                raise HostAPIError("STORE_UNAVAILABLE", 503)
            if self.store.read_json(parts) != policy:
                raise HostAPIError("STORE_UNAVAILABLE", 503)

    # LLM: 可省CAS仅供已授权宿主管理，原插件传入int不变；停用修订仍先于锁外取消。
    # 函数用途: 禁止用户新工作，省略修订时在同一用户锁内读取当前值。
    def disable(self, user_id, expected_revision=None, operation=None, reason="", *, idempotency_key=None, actor_owner=None):
        operation = operation or _host_operation(idempotency_key, actor_owner)
        result = self.change_status(user_id, expected_revision, operation, "disabled", reason)
        if not result.replayed and self.stop_resources is not None:
            self.stop_resources(user_id, result.user.revision)
        return result

    # LLM: 仅disabled可启用；省略CAS仍在原锁内读取，插件显式修订与撤销fence保持。
    # 函数用途: 允许已停用用户提交新工作，不复活旧修订。
    def enable(self, user_id, expected_revision=None, operation=None, *, idempotency_key=None, actor_owner=None):
        operation = operation or _host_operation(idempotency_key, actor_owner)
        return self.change_status(user_id, expected_revision, operation, "enabled")

    # LLM: 重放先于CAS；None明确表示锁内当前修订，其余仍严格int和原合法前态，状态回执同文件提交。
    # 函数用途: 在原操作和用户短锁下执行一次启停转换。
    def change_status(self, user_id, expected_revision, operation, status, reason=""):
        uid = validate_local_user_id(user_id)
        operation.validate()
        if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 1):
            raise HostAPIError("INVALID_SCHEMA")
        if type(reason) is not str or len(reason) > 256:
            raise HostAPIError("INVALID_SCHEMA")
        action = "users.local.disable" if status == "disabled" else "users.local.enable"
        op_id = operation.identifier(action)
        intent = {"user_id": uid, "expected_revision": expected_revision, "reason": reason}
        self.check_authority()
        with self.store.locked(("local_users", "operations", op_id)):
            self.bind_operation(operation, action, intent)
            with self.store.locked(("local_users", f"{uid}.json")):
                record = self.store.read_record(uid)
                if record is None:
                    raise HostAPIError("USER_NOT_FOUND", 404)
                replay = self.replay(record, op_id)
                if replay is not None:
                    return replay
                user = parse_local_user(record["user"], uid)
                if expected_revision is not None and user.revision != expected_revision:
                    raise HostAPIError("REVISION_CONFLICT", 409)
                previous = "enabled" if status == "disabled" else "disabled"
                if user.status != previous:
                    raise HostAPIError("USER_STATE_CONFLICT", 409)
                self.check_authority()
                record["user"].update(status=status, revision=user.revision + 1, updated_at=now_utc())
                record["operations"][op_id] = dict(record["user"])
                self.store.save(uid, record)
                return LocalUserResult(parse_local_user(record["user"], uid), op_id)

    # LLM: 密码慢散列在锁外；原操作索引和用户原子文件承载CAS、回执、审计，重放不重新改密码。
    # 函数用途: 为已有普通用户设置或重置密码并使历史修订会话失效。
    def set_password(self, uid, *, password, expected_revision=None, idempotency_key,
                     actor_owner=None, operation=None):
        user_id = validate_local_user_id(uid)
        password_hash = _validate_and_hash_password(password)
        operation = operation or _host_operation(idempotency_key, actor_owner)
        operation.validate()
        action = "users.local.password.set"
        op_id = operation.identifier(action)
        intent = {"user_id": user_id, "password": password, "expected_revision": expected_revision}
        self.check_authority()
        with self.store.locked(("local_users", "operations", op_id)):
            self.bind_operation(operation, action, intent)
            with self.store.locked(("local_users", f"{user_id}.json")):
                record = self.store.read_record(user_id)
                if record is None:
                    raise HostAPIError("USER_NOT_FOUND", 404)
                replay = self.replay(record, op_id)
                if replay is not None:
                    return replay.user.public_view(), True
                user = parse_local_user(record["user"], user_id)
                if expected_revision is not None and (type(expected_revision) is not int or user.revision != expected_revision):
                    raise HostAPIError("REVISION_CONFLICT", 409)
                if user.status not in {"enabled", "disabled"}:
                    raise HostAPIError("USER_STATE_CONFLICT", 409)
                timestamp = now_utc()
                record["user"].update(password_hash=password_hash, password_updated_at=timestamp,
                                      revision=user.revision + 1, updated_at=timestamp)
                record["operations"][op_id] = dict(record["user"])
                record.setdefault("audit_events", []).append({"event": action, "user_id": user_id,
                    "actor_owner": asdict(actor_owner or OwnerIdentity.local_main()), "operation_id": op_id,
                    "revision": user.revision + 1, "at": timestamp})
                self.check_authority()
                self.store.save(user_id, record)
                return parse_local_user(record["user"], user_id).public_view(), False

    # LLM: 使用原enabled状态、home身份及维护闸门，不接受未知或弱类型密码；只读且不物化账号。
    # 函数用途: 校验本地用户密码并返回当前有效账号，统一未知用户与密码错误的公开原因。
    def verify_user_password(self, uid, password):
        user_id = validate_local_user_id(uid)
        user = self.store.read_user(user_id)
        if user is None:
            raise HostAPIError("INVALID_CREDENTIALS", 401, message="invalid user_id or password")
        self.store.require_enabled(user_id)
        if not user.password_hash or not isinstance(password, str) or not verify_password(password, user.password_hash):
            raise HostAPIError("INVALID_CREDENTIALS", 401, message="invalid user_id or password")
        self.store.require_enabled(user_id, user.revision)
        return user
