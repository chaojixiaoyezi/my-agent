# LLM: 审计动作只接真实AG1领域；来源、enabled、成员和管理范围每次现查，回执不能成为永久权限。
# 模块用途: 将审计组观察/黑盒调用、账号资格与双Bot会话接入Host v2。
from ..audit_group.access_policy import AuditGroupError, require_audit_group_member
from ..audit_group.blackbox_invoke_service import (
    AuditGroupBlackboxInvokeService,
    normalize_asset_reference,
)
from ..audit_group.observation_service import AuditGroupObservationService, alert_timestamp
from ..audit_group.source_snapshot import SourceAssetSnapshot
from ..conversation.bot_sessions import BotSessionManager
from ..user_space.local_user_lifecycle import LocalUserLifecycle
from ..user_space.local_user_store import LocalUserStore
from ..user_space.owner_resolver import OwnerIdentity
from .action_specs import action_spec, required_capabilities
from .action_specs_audit_bots import AUDIT_BOT_ACTIONS
from .authorization import managed_user_id
from .contracts import DomainResult
from .conversations import BoundCursor, digest, read_context
from .errors import HostAPIError
from .schemas import thaw


# LLM: 原HostAPIError没有details构造参数；仅本域扩展安全诊断，不修改全局错误合同或投递域403。
# 类用途: 保留AG1机器码、准确HTTP状态和结构化详情。
class AuditBotDomainRejection(HostAPIError):
    # LLM: AG1只返回固定公开错误；不复制底层异常链和私有文件内容。
    # 函数用途: 将审计组领域拒绝转换为Host v2错误。
    def __init__(self, error):
        super().__init__(error.code, error.status_code, message=str(error)[:256])
        self.details = dict(error.details)

    # LLM: 运输层仍消费原错误封套，仅增加领域诊断对象。
    # 函数用途: 输出含安全details的Host错误。
    def to_payload(self):
        return {**super().to_payload(), "details": self.details}


# LLM: 原调用者不由安装管理员替代；所有入口与重放先冷核账号/目录，普通provider沿原owner_state处理。
# 函数用途: 复验当前用户没有停用，并取得同一原上下文。
def _check_caller(authorized, expected):
    context = read_context(authorized)
    if context.identity != expected.identity or context.owner_home.home_dir != expected.owner_home.home_dir:
        raise HostAPIError("RESOURCE_NOT_FOUND", 404)
    users = LocalUserStore(context.owner_home.root)
    if context.identity == OwnerIdentity.local_main():
        parts = context.owner_home.home_dir.relative_to(users.root).parts
        status = users.read_json((*parts, "owner_status.json"))
        if status is not None and status.get("status") != "active":
            raise HostAPIError("USER_DISABLED", 403)
    else:
        from .owner_context import owner_state
        owner_state(users, context.identity)
    return context


# LLM: 只认生产请求端口上的同一GatewayPluginRequests实例；插件payload不能注入准备器。
# 函数用途: 取得可将Bot消息绑定到原输入回执的真实宿主服务。
def bot_request_service(ports):
    from .gateway_runtime import GatewayPluginRequests
    prepare = getattr(getattr(ports.conversation, "request_ports", None), "prepare_request", None)
    service = getattr(prepare, "__self__", None)
    return service if isinstance(service, GatewayPluginRequests) else None


# LLM: 静态目录不代表真实端口就绪；只读审计无需ConversationStore，写口须有原事务资源。
# 函数用途: 逐项检查十动作的生产依赖。
def audit_bots_ready(action, ports):
    if action not in AUDIT_BOT_ACTIONS:
        return False
    if action == "users.local.audit_group.set":
        return getattr(ports.local_users, "factory", None) is not None
    if action == "audit_group.assets.invoke":
        services = getattr(ports.observations, "services", None)
        return callable(getattr(getattr(services, "resources", None), "paths_for", None))
    if action.startswith("audit_group."):
        return (action == "audit_group.posture.overview"
                or getattr(ports.conversation, "cursor_codec", None) is not None)
    if action == "conversations.bots.submit":
        return bot_request_service(ports) is not None
    return (callable(getattr(ports.conversation, "store_resolver", None))
            and getattr(ports.conversation, "cursor_codec", None) is not None)


# LLM: 闭合schema与AND能力在直接Python入口同样检查；所有领域返回前重新核caller及当前成员。
# 函数用途: 分发审计组六动作与Bot四动作，AG1错误保持准确状态和详情。
def call_audit_bots(authorized, ports):
    action = authorized.call.action
    if action not in AUDIT_BOT_ACTIONS:
        raise HostAPIError("UNSUPPORTED_ACTION")
    context = read_context(authorized)
    _check_caller(authorized, context)
    if not required_capabilities(action, authorized.call.payload) <= authorized.binding.capabilities:
        raise HostAPIError("CAPABILITY_DENIED", 403)
    if not audit_bots_ready(action, ports):
        raise HostAPIError("HOST_NOT_READY", 503)
    payload = action_spec(action).validate_payload(thaw(authorized.call.payload))
    try:
        if action == "users.local.audit_group.set":
            result = _set_membership(authorized, context, ports, payload)
        elif action.startswith("audit_group."):
            require_audit_group_member(context.owner_home.root, context.identity)
            result = (_invoke_asset(authorized, context, ports, payload) if action.endswith(".invoke")
                      else DomainResult(200, _observe(authorized, context, ports, payload)))
            require_audit_group_member(context.owner_home.root, context.identity)
        else:
            result = _bot(authorized, context, ports, payload)
        _check_caller(authorized, context)
        return result
    except AuditGroupError as error:
        raise AuditBotDomainRejection(error) from None


# LLM: actor取真实caller，不借public安装者；目标必须在批准manageable交集内，生命周期仍持原CAS/幂等锁。
# 函数用途: 经原安装短提交门修改一个受管用户的审计组资格。
def _set_membership(authorized, context, ports, payload):
    caller = authorized.binding.source.caller_owner or authorized.binding.actor
    if (caller != OwnerIdentity.local_main() or context.identity != OwnerIdentity.local_main()
            or authorized.call.as_user_id is not None):
        raise HostAPIError("AUDIT_GROUP_ADMIN_ONLY", 403)
    uid = managed_user_id(payload["user_id"])
    allowed = authorized.binding.grant["manageable_user_ids"]
    if uid not in allowed and "*" not in allowed:
        raise HostAPIError("MANAGED_USER_SCOPE_DENIED", 403)
    factory = ports.local_users.factory
    call = factory.verify(authorized)

    # LLM: 生命周期写入前和重放前重新观察原活来源，不缓存管理员安装资格。
    # 函数用途: 在原账号事务内复核本次管理权。
    def check():
        factory.check_call(authorized, call)
        _check_caller(authorized, context)

    service = LocalUserLifecycle(factory.store, check)
    with factory.commit_call(authorized, call):
        result = service.set_audit_group_membership(uid, in_audit_group=payload["in_audit_group"],
            expected_revision=payload["expected_revision"], actor_owner=caller,
            idempotency_key=digest([authorized.binding.installation_ref, authorized.call.idempotency_key]))
    keys = ("user_id", "in_audit_group", "revision", "audit_group_updated_at", "audit_group_updated_by")
    return DomainResult(200, {**{key: result[key] for key in keys}, "owner_id": "local/" + uid,
                              "idempotent_replay": result["replayed"]})


# LLM: 复用原幂等账，首次/重放先归一规则引用并冻结目标依赖；成员/治理/源启停重核及未执行错误分类不变。
# 函数用途: 调用原AG1黑盒服务，并完整保留执行前拒绝分类。
def _invoke_asset(authorized, context, ports, payload):
    from ..audit_group.caller_controls import caller_policy
    from .capabilities_v2_handlers import _once

    # LLM: 预检与执行共用规则引用归一，选定规则后才冻结；回执不绕过治理禁用，源坏账仍沿AG1安全503映射。
    # 函数用途: 在发表executing之前复核当前执行资格和安全源快照。
    def preflight(_saved):
        _check_caller(authorized, context)
        try:
            kind, identifier, rule_id = normalize_asset_reference(payload["kind"], payload["asset_id"], payload.get("rule_id"))
            caller_policy(context.owner_home.root, context.identity, kind)
            SourceAssetSnapshot.freeze(context.owner_home.root, payload["source_owner_id"], kind, identifier, rule_id=rule_id)
        except AuditGroupError:
            raise
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            raise AuditGroupError("AUDIT_GROUP_EXECUTION_UNAVAILABLE", status_code=503) from None

    # LLM: rule_id与inputs原样送AG1规则选择器；caller、执行及产物仍由原领域绑定。
    # 函数用途: 调用真正的跨Owner黑盒服务而不复制源码。
    def effect():
        try:
            data = AuditGroupBlackboxInvokeService(context.owner_home.root).invoke_asset(context.identity,
                source_owner=payload["source_owner_id"], asset_type=payload["kind"],
                asset_id=payload["asset_id"], inputs=payload["input_params"], rule_id=payload.get("rule_id", ""))
            return DomainResult(200, data)
        except AuditGroupError as error:
            raise AuditBotDomainRejection(error) from None

    return _once(authorized, context, ports.observations.services, effect, preflight)


# LLM: 态势筛选原样交领域共用选择器；分页游标绑定owner/action/完整筛选，until只过滤当前页且不丢原位置/覆盖。
# 函数用途: 将四个只读审计动作的参数翻译为AG1接口，并保留来源错误。
def _observe(authorized, context, ports, payload):
    import time
    service = AuditGroupObservationService(context.owner_home.root)
    action = authorized.call.action
    if action == "audit_group.posture.overview":
        return service.posture_overview(context.identity, owner_ids=payload.get("owner_ids"),
                                        target_owner=payload.get("target_owner"))
    codec = getattr(ports.conversation, "cursor_codec", None)
    if codec is None:
        raise HostAPIError("HOST_NOT_READY", 503)
    cursor = BoundCursor(codec, authorized, context, {k: v for k, v in payload.items() if k != "cursor"})
    saved = cursor.decode(payload.get("cursor"))
    args = {"cursor": saved["anchor"]["domain_cursor"] if saved else "", "limit": payload["limit"]}
    if action == "audit_group.owners.list":
        data = service.list_owners(context.identity, **args)
    elif action == "audit_group.assets.summary":
        data = service.summarize_assets(context.identity, owner_ids=payload.get("owner_ids"),
                                       kinds=[payload["kind"]] if payload.get("kind") else None, **args)
    else:
        until = alert_timestamp(payload["until"]) if payload.get("until") else None
        data = service.list_alerts(context.identity, owner_ids=payload.get("owner_ids"),
            severity=payload.get("severity", ""), since=payload.get("since", ""), **args)
        if until is not None:
            data["items"] = [row for row in data["items"] if alert_timestamp(row["created_at"]) <= until]
    next_cursor = data["next_cursor"]
    data["next_cursor"] = (cursor.encode({"domain_cursor": next_cursor}, 0,
        saved["expires"] if saved else time.time() + 900) if next_cursor else None)
    return data


# LLM: 最多读取10000条公开投影；超限拒绝而非报告部分集合为完整态势，错误列表去重不丢覆盖缺口。
# 函数用途: 为选定owner的态势聚合收集有界公开页。
def _gather(method, caller, **arguments):
    rows, errors, token, as_of = [], {}, "", ""
    for _ in range(50):
        data = method(caller, limit=200, cursor=token, **arguments)
        rows.extend(data["items"])
        errors.update({digest(row): row for row in data["source_errors"]})
        as_of = data["as_of"]
        token = data["next_cursor"]
        if not token:
            return rows, list(errors.values()), as_of
    raise HostAPIError("PAYLOAD_TOO_LARGE", 413)


# LLM: 明确的owner_ids不可静默变成全局查询；所有汇总只消费AG1安全投影，不触及源私有记录。
# 函数用途: 计算页面选中账号的态势子集。
def _selected_posture(service, caller, owner_ids):
    owners, errors, stamp = _gather(service.list_owners, caller)
    requested = set(owner_ids)
    if requested - {row["owner_id"] for row in owners}:
        raise AuditGroupError("OWNER_NOT_FOUND", status_code=404)
    owners = [row for row in owners if row["owner_id"] in requested]
    assets, failures, _ = _gather(service.summarize_assets, caller, owner_ids=owner_ids)
    alerts, alert_errors, _ = _gather(service.list_alerts, caller, owner_ids=owner_ids)
    errors = list({digest(row): row for row in [*errors, *failures, *alert_errors]}.values())
    return {"coverage": "partial" if errors else "complete", "source_errors": errors, "as_of": stamp,
        "owners_total": len(owners), "enabled_owners": sum(row["status"] == "enabled" for row in owners),
        "audit_group_members": sum(row["in_audit_group"] for row in owners),
        "assets_by_kind": {kind: sum(row["kind"] == kind for row in assets)
                           for kind in ("script", "workflow", "skill", "monitor")},
        "active_monitors": sum(row["kind"] == "monitor" and row["enabled"] for row in assets),
        "alerts_summary": {"total": len(alerts), "error": len(alerts)}}


# LLM: manager先持久绑定代次再由真实Gateway服务入队，队列重放不重新决定线程，后台不会被分卷迁移。
# 函数用途: 接通四个Bot动作并返回准确受理回执。
def _bot(authorized, context, ports, payload):
    domain = ports.conversation

    # LLM: 每次存储写与回执重放都重新冷核当前调用，不缓存enabled。
    # 函数用途: 为本次Bot槽位事务注入原来源复验。
    def check():
        _check_caller(authorized, context)

    manager = BotSessionManager(context.owner_home, domain.store_resolver(context), check,
                                installation_ref=authorized.binding.installation_ref)
    action, key = authorized.call.action, authorized.call.idempotency_key
    if action == "conversations.bots.history.list":
        data = manager.history(payload, authorized, context, domain.cursor_codec)
    elif action == "conversations.bots.ensure":
        data = manager.ensure(payload, key)
    elif action == "conversations.bots.rollover":
        data = manager.rollover(payload, key)
    else:
        binding = manager.prepare_submit(payload, key)
        data = bot_request_service(ports).submit_bot_message(authorized, context, binding)
        return DomainResult(202, data)
    return DomainResult(200, data)
