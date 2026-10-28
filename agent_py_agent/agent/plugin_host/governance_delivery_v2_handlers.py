# LLM: 固定适配WT1/WT2领域，身份只来自Host绑定；确定领域拒绝保留not_started以释放原幂等账，未知副作用仍失败关闭。
# 模块用途: 接通九个跨Owner投递与十二个受管本地用户治理动作。
from __future__ import annotations

from ..assets.delivery_contracts import AssetDeliveryError
from ..assets.topology import AssetClosureError
from ..user_space.local_user_store import LocalUserStore
from ..user_space.owner_resolver import OwnerIdentity
from .action_specs import action_spec
from .authorization import managed_user_id
from .contracts import DomainResult
from .errors import HostAPIError

DELIVERY_METHODS = {
    "assets.delivery.targets.list": "list_targets", "assets.delivery.preview_bundle": "preview_bundle",
    "assets.delivery.send": "send_bundle", "assets.delivery.inbox.list": "list_inbox",
    "assets.delivery.accept": "accept_delivery", "assets.delivery.reject": "reject_delivery",
    "assets.delivery.toggle_asset": "toggle_asset", "assets.delivery.blocklist.list": "get_blocklist",
    "assets.delivery.blocklist.update": "update_blocklist",
}
MANAGED_METHODS = {
    "users.managed.compact_policy.get": "get_user_compact_policy",
    "users.managed.compact_policy.set": "set_user_compact_policy",
    "users.managed.compact_policy.batch_set": "batch_set_users_compact_policy",
    "settings.compact_policy.admin_force.set": "set_global_compact_force_policy",
    "users.managed.overview.get": "get_overview", "users.managed.policy.set": "set_policy",
    "users.managed.persona.get": "get_persona", "users.managed.persona.set": "set_persona",
    "users.managed.memories.list": "list_memories", "users.managed.memories.mutate": "mutate_memories",
    "users.managed.assets.provision": "provision_assets", "users.managed.assets.toggle": "toggle_asset",
    "users.managed.tasks.dispatch": "dispatch_task", "users.managed.tasks.list": "list_tasks",
}
REVIEW_METHODS = {"governance.reviews.list": "list_reviews", "governance.reviews.decide": "decide_review"}
DELIVERY_GOVERNANCE_ACTIONS = frozenset(DELIVERY_METHODS) | frozenset(MANAGED_METHODS) | frozenset(REVIEW_METHODS)
DOMAIN_ERRORS = {
    "MANAGED_USER_SCOPE_DENIED": 403, "ASSET_DELIVERY_SECRET_DETECTED": 422,
    "DELIVERY_BLOCKED_BY_RECIPIENT": 403, "ASSET_DISABLED_BY_OWNER": 403,
    "REVISION_CONFLICT": 409, "DELIVERY_STATE_CONFLICT": 409,
    "DELIVERY_NOT_FOUND": 404, "DELIVERY_TARGET_NOT_FOUND": 404,
    "ASSET_DELIVERY_DEPENDENCY_MISSING": 422, "ASSET_DEPENDENCY_MISSING": 422,
    "ASSET_DELIVERY_QUOTA_EXCEEDED": 429, "ASSET_NOT_FOUND": 404,
    "DELIVERY_SELF_TARGET_FORBIDDEN": 400, "DELIVERY_TARGET_INVALID": 400,
    "ASSET_CLOSURE_INVALID": 422, "ASSET_BUNDLE_CORRUPTED": 422,
    "ASSET_DELIVERY_PAYLOAD_INVALID": 400, "PERSONA_LOCKED_BY_ADMIN": 403,
    "PERSONA_CHANGE_PENDING_ADMIN_REVIEW": 409, "GOVERNANCE_OPERATION_INCOMPLETE": 409,
    "REVIEW_FEEDBACK_REQUIRED": 400, "MEMORY_LOCKED_BY_ADMIN": 403,
}


# LLM: 保持原HostAPIError构造合同；仅附带领域声明的安全details，HTTP仍沿原to_payload，不修改全局错误协议。
# 类用途: 将投递缺失依赖等结构化诊断带到Host错误回执。
class DeliveryDomainRejection(HostAPIError):
    # LLM: 只复制结构化诊断，不复制异常链、原文件或任意异常文字，不产生业务写入。
    # 函数用途: 保存可确定尚未开始的投递领域拒绝及安全细节。
    def __init__(self, code, status, details):
        super().__init__(code, status, outcome="not_started")
        self.details = details

    # LLM: 公共HTTP入口消费本投影；保留原code/status/outcome语义，缺失依赖可被调用方机器读取。
    # 函数用途: 在原错误信封中附加结构化领域诊断。
    def to_payload(self):
        return {**super().to_payload(), "details": self.details}


# LLM: 只按明确类型/机器码分类；未来AssetDeliveryError子码固定422，原HostAPIError及未知异常不降级副作用状态。
# 函数用途: 把已知领域拒绝变成可释放executing回执的Host错误。
def _domain_rejection(error):
    if isinstance(error, HostAPIError):
        return error
    if isinstance(error, AssetClosureError):
        return DeliveryDomainRejection("ASSET_DELIVERY_DEPENDENCY_MISSING", 422, {"missing_refs": list(error.missing_refs)})
    code = getattr(error, "error_code", None) or getattr(error, "code", None)
    if isinstance(error, AssetDeliveryError):
        code = "ASSET_DELIVERY_DEPENDENCY_MISSING" if code == "ASSET_DEPENDENCY_MISSING" else code
        return DeliveryDomainRejection(code, DOMAIN_ERRORS.get(code, 422), dict(error.details))
    if code in DOMAIN_ERRORS:
        return HostAPIError(code, DOMAIN_ERRORS[code], getattr(error, "outcome", "not_started"))
    return None


# LLM: 惰性import只来自固定产品模块，不接受插件地址；准确缺模块明确未就绪，内部依赖故障不洗成成功。
# 函数用途: 找到本动作的领域类和静态公开方法名。
def domain_service(action):
    try:
        if action in DELIVERY_METHODS:
            from ..assets.delivery_service import AssetDeliveryService

            return AssetDeliveryService, DELIVERY_METHODS[action]
        if action in MANAGED_METHODS:
            from ..user_space.managed_user_governance import ManagedUserGovernanceService

            return ManagedUserGovernanceService, MANAGED_METHODS[action]
        if action in REVIEW_METHODS:
            from ..user_space.governance_review_store import GovernanceReviewStore

            return GovernanceReviewStore, REVIEW_METHODS[action]
    except ImportError:
        raise HostAPIError("HOST_NOT_READY", 503) from None
    raise HostAPIError("UNSUPPORTED_ACTION")


# LLM: 无实例化/目录创建副作用；21动作逐项检查实际类方法，目录登记不等于领域就绪。
# 函数用途: 为组合根发现返回真实依赖可用性。
def governance_delivery_ready(action):
    try:
        service, method = domain_service(action)
        return callable(getattr(service, method, None))
    except HostAPIError:
        return False


# LLM: 只读原LocalUserStore；列表必须按当前manageable交集逐目标查询，不能先扫其它用户正文再过滤。
# 函数用途: 取得本次调用确实可管理且已登记的普通本地用户。
def managed_targets(payload, authorized, context):
    if context.identity != OwnerIdentity.local_main():
        raise HostAPIError("MANAGED_USER_SCOPE_DENIED", 403)
    allowed = authorized.binding.grant["manageable_user_ids"]
    requested = [managed_user_id(payload[key]) for key in ("user_id", "target_owner_id") if key in payload]
    if len(set(requested)) > 1:
        raise HostAPIError("MANAGED_USER_SCOPE_DENIED", 403)
    users = LocalUserStore(context.owner_home.root)
    selected = requested[:1] if requested else [uid for uid in users.list_user_ids() if uid in allowed or "*" in allowed]
    if any((uid not in allowed and "*" not in allowed) or users.get(uid) is None for uid in selected):
        raise HostAPIError("MANAGED_USER_SCOPE_DENIED", 403)
    return tuple("local/" + uid for uid in selected)


# LLM: 写入/重放前核注册与名单；批量只用授权交集，global必须local/main且明确通配授权，不能由all选择器扩大权限。
# 函数用途: 在原幂等短门中执行无业务写入的治理预检。
def preflight_governance_delivery(action, payload, authorized, context):
    if action in DELIVERY_METHODS:
        return
    if action == "settings.compact_policy.admin_force.set":
        if context.identity != OwnerIdentity.local_main() or "*" not in authorized.binding.grant["manageable_user_ids"]:
            raise HostAPIError("MANAGED_USER_SCOPE_DENIED", 403)
        return
    if action == "users.managed.compact_policy.batch_set":
        _compact_batch_targets(payload, authorized, context)
        return
    targets = managed_targets(payload, authorized, context)
    if action not in {"users.managed.overview.get", "governance.reviews.list"} and len(targets) != 1:
        raise HostAPIError("INVALID_SCHEMA")
    if action == "governance.reviews.decide":
        service, _ = domain_service(action)
        reviews = service(context.owner_home.root, actor_owner=context.identity).list_reviews(target_owner_id=targets[0])
        if not any(row.get("review_id") == payload["review_id"] for row in reviews.get("items", [])):
            raise HostAPIError("RESOURCE_NOT_FOUND", 404)


# LLM: 确定领域拒绝须在原幂等门内转HostAPIError；未知异常/坏DTO仍保留unknown，不能清账后重复副作用。
# 函数用途: 调用真实领域并投影含缺依赖明细的稳定Host v2错误。
def call_governance_delivery(action, payload, authorized, context):
    preflight_governance_delivery(action, payload, authorized, context)
    try:
        service_type, method_name = domain_service(action)
        if not callable(getattr(service_type, method_name, None)):
            raise HostAPIError("HOST_NOT_READY", 503)
        if action in DELIVERY_METHODS:
            result = _delivery(service_type(context.owner_home.root), method_name, payload, context)
        elif action in MANAGED_METHODS:
            result = _managed(service_type(context.owner_home.root, actor_owner=context.identity), method_name, payload, authorized, context)
        else:
            result = _reviews(service_type(context.owner_home.root, actor_owner=context.identity), method_name, payload, authorized, context)
        if not isinstance(result, dict):
            if action_spec(action).requires_idempotency:
                raise HostAPIError("OUTCOME_UNKNOWN", 503, "unknown")
            raise HostAPIError("HOST_NOT_READY", 503)
        return DomainResult(200, result)
    except Exception as error:
        rejection = _domain_rejection(error)
        if rejection is not None:
            raise rejection from None
        raise


# LLM: sender/recipient/owner主语全部由effective identity生成；payload只可选择对方，不开放auto_accept/admin_provision。
# 函数用途: 将投递动作的页面字段翻译为WT1公开方法参数，普通接受强制初始关闭。
def _delivery(service, method, payload, context):
    identity = context.identity
    owner = ("local/" + identity.owner_id if identity.provider == "local"
             else f"providers/{identity.provider}/{identity.owner_kind}s/{identity.owner_id}")
    args = dict(payload)
    if method == "accept_delivery":
        args.pop("auto_enable", None)
        args["initial_enabled"] = False
    elif method == "update_blocklist":
        args["operation"] = {"block": "add", "unblock": "remove"}[args.pop("mode")]
    return getattr(service, method)(owner, **args)


# LLM: 服务使用真实管理员身份；批量先核完整名单且固定授权交集，global不经单目标分支，旧治理CAS不变。
# 函数用途: 以规范本地Owner调用WT2十个用户治理接口，无目标概览只遍历批准交集。
def _managed(service, method, payload, authorized, context):
    if method == "set_global_compact_force_policy":
        return service.set_global_compact_force_policy(**payload)
    if method == "batch_set_users_compact_policy":
        targets = _compact_batch_targets(payload, authorized, context)
        if not targets:
            return {"atomic": False, "partial": False, "items": []}
        return service.batch_set_users_compact_policy(payload["policy_patch"], user_ids=list(targets),
            expected_revisions=payload.get("expected_revisions"))
    targets = managed_targets(payload, authorized, context)
    args = {key: value for key, value in payload.items() if key not in {"user_id", "target_owner_id"}}
    if method == "get_overview" and not (payload.get("user_id") or payload.get("target_owner_id")):
        return {"items": [service.get_overview(target) for target in targets]}
    if method in {"set_policy", "set_persona"}:
        args["updated_by"] = "local/main"
    return getattr(service, method)(targets[0], **args)


# LLM: all只取注册用户与grant交集；显式列表逐个重核，任何越权或重复别名整批拒绝，不静默删去越权项。
# 函数用途: 固定本次批量压缩政策写入的授权目标。
def _compact_batch_targets(payload, authorized, context):
    if payload.get("scope") == "all_managed_users" and "user_ids" not in payload:
        return managed_targets({}, authorized, context)
    if "scope" in payload or not isinstance(payload.get("user_ids"), list) or not payload["user_ids"]:
        raise HostAPIError("INVALID_SCHEMA")
    targets = tuple(managed_targets({"user_id": uid}, authorized, context)[0] for uid in payload["user_ids"])
    if len(set(targets)) != len(targets):
        raise HostAPIError("INVALID_SCHEMA")
    return targets


# LLM: 读取按批准目标逐个调用，决定同时透传预期Owner给WT2原子门；预检不能替代实际写口归属校验。
# 函数用途: 以WT2提审库接口查询受管队列或提交带CAS的批准/驳回。
def _reviews(service, method, payload, authorized, context):
    targets = managed_targets(payload, authorized, context)
    args = {key: value for key, value in payload.items() if key not in {"user_id", "target_owner_id"}}
    if method == "list_reviews":
        items = []
        for target in targets:
            items.extend(service.list_reviews(target_owner_id=target, **args)["items"])
        return {"items": items}
    return service.decide_review(**args, target_owner_id=targets[0], decided_by="local/main")
