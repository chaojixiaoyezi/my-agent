# LLM: 生命周期沿操作→用户→owner quota→JSON短锁发布，未完成物化不得标成enabled。
# 模块用途: 实现普通本地账号创建、启停和与原激活绑定的耐久幂等。
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from ..common.nofollow_fs import ensure_private_dir, open_directory_beneath
from ..plugin_host.errors import HostAPIError
from ..plugin_host.owner_context import validate_local_user_id
from .local_user_store import LocalUser, parse_local_user
from .owner_quota import LocalUserQuotaLimits, OwnerQuotaUnavailable, seed_local_user_quota


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


# LLM: 使用规范JSON而非repr，输入摘要不暴露原键且稳定跨重启。
# 函数用途: 为操作意图及配额种子生成绑定摘要。
def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


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

    # LLM: 所有参数先校验；操作索引先于用户锁；成功回执与enabled状态原子发布。
    # 函数用途: 创建或恢复同一操作的普通本地账号。
    def create(self, user_id, operation, *, display_name="", quota=None):
        uid = validate_local_user_id(user_id)
        operation.validate()
        if type(display_name) is not str or len(display_name) > 80:
            raise HostAPIError("INVALID_SCHEMA")
        try:
            limits = LocalUserQuotaLimits.parse(quota)
        except ValueError as exc:
            raise HostAPIError("INVALID_SCHEMA") from exc
        intent = {"user_id": uid, "display_name": display_name, "quota": limits.to_dict()}
        op_id = operation.identifier("users.local.create")
        self.check_authority()
        with self.store.locked(("local_users", "operations", op_id)):
            self.bind_operation(operation, "users.local.create", intent)
            with self.store.locked(("local_users", f"{uid}.json")):
                record = self.store.read_record(uid)
                replay = self.replay(record, op_id)
                if replay is not None:
                    return replay
                record = self.begin(uid, record, op_id, display_name)
                return self.provision(record, limits, op_id)

    # LLM: 索引只是不可变原意图，不复制可变账号/配额权威；变化payload冲突，换激活拒绝重放。
    # 函数用途: 在任何用户写入前固定本操作的准确来源和输入摘要。
    def bind_operation(self, operation, action, intent):
        op_id = operation.identifier(action)
        parts = ("local_users", "operations", f"{op_id}.json")
        binding = {"installation_ref": operation.installation_ref, "activation_id": operation.activation_id,
                   "action": action, "input_digest": digest(intent), "user_id": intent["user_id"]}
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

    # LLM: 预先存在但无登记的目录一律不接管；未完成账号只允许原操作恢复。
    # 函数用途: 原子保存创建意图和provisioning状态。
    def begin(self, uid, record, op_id, display_name):
        if record is not None:
            if record["provision"].get("operation_id") != op_id:
                raise HostAPIError("USER_EXISTS", 409)
            return record
        homes = (self.store.owner_home(uid).home_dir, self.store.root / "owners" / "providers" / "local" / "users" / uid)
        if any(home.exists() or home.is_symlink() for home in homes):
            raise HostAPIError("USER_UNMANAGED", 409)
        timestamp = now_utc()
        user = LocalUser(uid, "provisioning", 1, display_name, timestamp, timestamp, 1)
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

    # LLM: 原子停用修订先于锁外取消；异常和取消请求不冒充资源已停止。
    # 函数用途: 禁止用户新工作并通知宿主撤销正在执行的资源。
    def disable(self, user_id, expected_revision, operation, reason=""):
        result = self.change_status(user_id, expected_revision, operation, "disabled", reason)
        if not result.replayed and self.stop_resources is not None:
            self.stop_resources(user_id, result.user.revision)
        return result

    # LLM: 仅disabled可启用，修订递增确保历史请求永远不能复活。
    # 函数用途: 允许已停用用户提交新的工作。
    def enable(self, user_id, expected_revision, operation):
        return self.change_status(user_id, expected_revision, operation, "enabled")

    # LLM: 操作回执重放先于CAS，正常更新要求准确修订与合法前态；状态和回执同文件原子提交。
    # 函数用途: 在短锁下执行一次启停转换。
    def change_status(self, user_id, expected_revision, operation, status, reason=""):
        uid = validate_local_user_id(user_id)
        operation.validate()
        if type(expected_revision) is not int or expected_revision < 1:
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
                if user.revision != expected_revision:
                    raise HostAPIError("REVISION_CONFLICT", 409)
                previous = "enabled" if status == "disabled" else "disabled"
                if user.status != previous:
                    raise HostAPIError("USER_STATE_CONFLICT", 409)
                self.check_authority()
                record["user"].update(status=status, revision=user.revision + 1, updated_at=now_utc())
                record["operations"][op_id] = dict(record["user"])
                self.store.save(uid, record)
                return LocalUserResult(parse_local_user(record["user"], uid), op_id)
