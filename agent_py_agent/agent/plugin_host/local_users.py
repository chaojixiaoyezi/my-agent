# LLM: users.local域只接受宿主核验调用，账号与quota各保留唯一权威，运行用量必须来自宿主观察口。
# 模块用途: 将六个普通用户管理动作接到生命周期、限域分页和配额读取。
from __future__ import annotations

import secrets
from collections import OrderedDict
from dataclasses import dataclass

from ..user_space.local_user_lifecycle import (
    LocalUserLifecycle,
    LocalUserOperation,
    now_utc,
)
from ..user_space.owner_quota import (
    LocalUserQuotaLimits,
    OwnerQuotaUnavailable,
    read_local_user_quota,
)
from ..user_space.owner_resolver import OwnerIdentity
from .errors import HostAPIError
from .owner_context import call_payload, validate_local_user_id


# LLM: 用量来自宿主队列/占用账，不能用目录数量估算；缺失观察口应关闭而非编造零占用。
# 类用途: 运输一次有界运行用量观察。
@dataclass(frozen=True)
class UserRuntimeUsage:
    active_agents: int
    subagents: int
    has_running_work: bool
    disk_bytes: int | None = None
    complete: bool = False

    # LLM: bool不是计数，未知磁盘必须伴随不完整标记。
    # 函数用途: 校验宿主用量投影的基本一致性。
    def __post_init__(self):
        if any(type(value) is not int or value < 0 for value in (self.active_agents, self.subagents)):
            raise ValueError("invalid usage")
        if type(self.has_running_work) is not bool or type(self.complete) is not bool:
            raise ValueError("invalid usage")
        if self.disk_bytes is not None and (type(self.disk_bytes) is not int or self.disk_bytes < 0):
            raise ValueError("invalid usage")
        if self.complete and self.disk_bytes is None:
            raise ValueError("incomplete disk usage")


# LLM: DTO供A统一成DomainResult；不包含token、原请求对象或本机路径。
# 类用途: 返回领域安全正文、HTTP状态和原操作结果。
@dataclass(frozen=True)
class LocalUserDomainResult:
    data: dict
    status: int = 200
    outcome: str = "completed"
    operation_id: str | None = None
    replayed: bool = False
    warnings: tuple[str, ...] = ()


# LLM: 运行用量和取消端口由真实宿主装配；C不编造占用、停止成功或已接通HTTP。
# 类用途: 实现本地普通账号管理的域适配器。
class LocalUsersDomain:
    # LLM: 初始化冷；分页令牌只在当前域实例有效且有界，不是持久执行权威。
    # 函数用途: 绑定权威核验、运行用量和锁外取消端口。
    def __init__(self, factory, usage_reader=None, stop_resources=None):
        self.factory = factory
        self.usage_reader = usage_reader
        self.stop_resources = stop_resources
        self._cursors = OrderedDict()

    # LLM: 完整验证在首次写之前，所有管理动作都必须由获批local/main安装发起。
    # 函数用途: 分派六个users.local动作并返回安全结果。
    def call(self, authorized):
        call = self.factory.verify(authorized)
        if "users.manage" not in call.grant.capabilities or call.grant.source_binding.owner != OwnerIdentity.local_main():
            raise HostAPIError("CAPABILITY_DENIED", 403)
        if call.as_user is not None:
            raise HostAPIError("INVALID_SCHEMA")
        payload = call_payload(call)
        if call.action == "users.local.create" and "quota" not in payload:
            # 未指定配额只能取默认与批准上限交集；显式超限仍按原合同拒绝。
            ceiling = call.grant.quota_ceiling.to_dict()
            payload["quota"] = {key: min(value, ceiling[key])
                                for key, value in LocalUserQuotaLimits().to_dict().items()}
        self.validate(call, payload)
        if self.usage_reader is None:
            raise HostAPIError("HOST_NOT_READY", 503)
        if call.action == "users.local.list":
            return self.list_users(call, payload)
        uid = payload["user_id"]
        if call.action in {"users.local.get", "users.local.quota.get"}:
            return self.read_user(call.action, uid)
        return self.mutate(authorized, call, payload)

    # LLM: 所有schema闭合；create同时要求管理和创建范围，三组许可不能互相补齐。
    # 函数用途: 校验动作、参数、幂等键和配额上限。
    def validate(self, call, payload):
        schemas = {
            "users.local.list": (set(), {"status", "limit", "cursor"}),
            "users.local.get": ({"user_id"}, set()), "users.local.quota.get": ({"user_id"}, set()),
            "users.local.create": ({"user_id"}, {"display_name", "quota"}),
            "users.local.disable": ({"user_id", "expected_revision"}, {"reason"}),
            "users.local.enable": ({"user_id", "expected_revision"}, set()),
        }
        if call.action not in schemas:
            raise HostAPIError("ACTION_UNSUPPORTED", 400)
        required, optional = schemas[call.action]
        if not required <= set(payload) or set(payload) - required - optional:
            raise HostAPIError("INVALID_SCHEMA")
        if "user_id" in payload:
            uid = validate_local_user_id(payload["user_id"])
            if uid not in call.grant.manageable_user_ids:
                raise HostAPIError("TARGET_DENIED", 403)
        writing = call.action in {"users.local.create", "users.local.disable", "users.local.enable"}
        if writing:
            source = call.grant.source_binding
            LocalUserOperation(source.installation_ref, source.activation_id, call.idempotency_key).validate()
        elif call.idempotency_key is not None:
            raise HostAPIError("INVALID_SCHEMA")
        if "expected_revision" in payload:
            if type(payload["expected_revision"]) is not int or payload["expected_revision"] < 1:
                raise HostAPIError("INVALID_SCHEMA")
        for field, limit in (("display_name", 80), ("reason", 256)):
            if field in payload and (type(payload[field]) is not str or len(payload[field]) > limit):
                raise HostAPIError("INVALID_SCHEMA")
        if call.action == "users.local.create":
            self.validate_creation(call, payload)

    # LLM: 默认安全种子也受grant上限约束；超界拒绝，不悄悄缩小显式请求。
    # 函数用途: 检查创建目标和全部配额字段。
    def validate_creation(self, call, payload):
        if payload["user_id"] not in call.grant.creatable_user_ids:
            raise HostAPIError("TARGET_DENIED", 403)
        try:
            limits = LocalUserQuotaLimits.parse(payload.get("quota"))
        except ValueError as exc:
            raise HostAPIError("INVALID_SCHEMA") from exc
        ceilings = call.grant.quota_ceiling.to_dict()
        if any(value > ceilings[key] for key, value in limits.to_dict().items()):
            raise HostAPIError("QUOTA_DENIED", 403)

    # LLM: 仅接受真实宿主读口返回的类型化用量，异常不制造默认零值。
    # 函数用途: 获取一次当前用户运行状态观察。
    def usage(self, uid):
        value = self.usage_reader(uid)
        if type(value) is not UserRuntimeUsage:
            raise HostAPIError("HOST_NOT_READY", 503)
        return value

    # LLM: 公开字段来源于原账号，运行状态来自本次观察；不向调用方暴露home路径。
    # 函数用途: 形成User响应。
    def user_data(self, user):
        data = user.to_dict()
        data["has_running_work"] = self.usage(user.user_id).has_running_work
        return data

    # LLM: 冷读不取得写锁；quota严格读取原文件，磁盘未完整统计明确为null。
    # 函数用途: 返回单用户或当前QuotaView。
    def read_user(self, action, uid):
        store = self.factory.store
        user = store.get(uid)
        if user is None:
            raise HostAPIError("USER_NOT_FOUND", 404)
        if action == "users.local.get":
            return LocalUserDomainResult(self.user_data(user))
        try:
            limits, revision = read_local_user_quota(store.root, store.owner_home(uid).home_dir)
        except OwnerQuotaUnavailable as exc:
            raise HostAPIError("STORE_UNAVAILABLE", 503) from exc
        usage = self.usage(uid)
        return LocalUserDomainResult({"limits": limits.to_dict(), "revision": revision,
                                      "usage": {"active_agents": usage.active_agents, "subagents": usage.subagents,
                                                "disk_bytes": usage.disk_bytes},
                                      "observed_at": now_utc(), "complete": usage.complete})

    # LLM: lifecycle门由A提供，域内再取操作/用户/quota短锁；取消必须在所有锁外执行。
    # 函数用途: 提交一次管理写操作并按实际观察返回取消状态。
    def mutate(self, source, call, payload):
        binding = call.grant.source_binding
        operation = LocalUserOperation(binding.installation_ref, binding.activation_id, call.idempotency_key)
        service = LocalUserLifecycle(self.factory.store, lambda: self.factory.check_call(source, call))
        usage = self.usage(payload["user_id"])
        with self.factory.commit_call(source, call):
            if call.action == "users.local.create":
                result = service.create(payload["user_id"], operation, display_name=payload.get("display_name", ""), quota=payload.get("quota"))
            elif call.action == "users.local.disable":
                result = service.disable(payload["user_id"], payload["expected_revision"], operation, payload.get("reason", ""))
            else:
                result = service.enable(payload["user_id"], payload["expected_revision"], operation)
        data = result.user.to_dict()
        data["has_running_work"] = usage.has_running_work
        warnings = ()
        if call.action == "users.local.disable":
            requested = False
            if self.stop_resources is not None and not result.replayed:
                try:
                    requested = self.stop_resources(result.user.user_id, result.user.revision) is True
                except Exception:
                    warnings = ("RESOURCE_STOP_UNCONFIRMED",)
            data = {"user": data, "cancel_requested": requested, "resources_stopped": None}
        status = 201 if call.action == "users.local.create" else 200
        return LocalUserDomainResult(data, status, operation_id=result.operation_id, replayed=result.replayed, warnings=warnings)

    # LLM: 游标是当前实例随机句柄，绑定原安装/激活/范围/筛选；不解析客户端自报偏移或owner。
    # 函数用途: 对固定管理集合做有界分页，all保留物化失败和未完成账号。
    def list_users(self, call, payload):
        status, limit = payload.get("status", "all"), payload.get("limit", 50)
        if (type(status) is not str or status not in {"all", "enabled", "disabled"}
                or type(limit) is not int or not 1 <= limit <= 100):
            raise HostAPIError("INVALID_SCHEMA")
        allowed = call.grant.manageable_user_ids | call.grant.creatable_user_ids
        scope = (call.grant.source_binding, allowed, status, limit)
        last = ""
        if "cursor" in payload:
            cursor = payload["cursor"]
            if type(cursor) is not str or cursor not in self._cursors or self._cursors[cursor][0] != scope:
                raise HostAPIError("CURSOR_INVALID", 400)
            last = self._cursors[cursor][1]
        users = self.factory.store.list_registered(allowed)
        users = [user for user in users if user.user_id > last and (status == "all" or user.status == status)]
        page, next_cursor = users[:limit], None
        if len(users) > limit:
            next_cursor = secrets.token_urlsafe(32)
            self._cursors[next_cursor] = (scope, page[-1].user_id)
            if len(self._cursors) > 256:
                self._cursors.popitem(last=False)
        return LocalUserDomainResult({"items": [self.user_data(user) for user in page], "next_cursor": next_cursor})
