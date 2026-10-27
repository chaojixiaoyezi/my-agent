# LLM: 组合根保持四个原端口，WP-4通过固定适配器复用它们；缺领域不借旧Broker或main invoker兜底。
# 模块用途: 提供原动作与V2扩展（含21个投递/治理动作）的静态分发、真实依赖发现与返回前撤权复验。
from __future__ import annotations

from .action_specs import action_spec, action_specs
from .authorization import IngressIssuer, revalidate_call
from .contracts import AuthorizedCall, DomainResult, HostDomainPorts
from .cursors import CursorCodec
from .errors import HostAPIError
from .schemas import PAGE_MAX_BYTES, canonical_json, thaw, validate_tree


# LLM: W7门户优先按静态集合选域，缺兄弟后端保持不可用；不能落入通用observations伪装已接线。
# 函数用途: 选择原四域、门户、V2扩展或双Bot领域。
def domain_name(action: str) -> str:
    action_spec(action)
    from .action_specs_audit_bots import AUDIT_BOT_ACTIONS
    from .action_specs_v2_ext import V2_ACTIONS
    from .action_specs_v2_portal import PORTAL_V2_ACTIONS
    if action in PORTAL_V2_ACTIONS:
        return "portal_v2"
    if action in AUDIT_BOT_ACTIONS:
        return "audit_bots"
    if action in V2_ACTIONS:
        return "capabilities_v2"
    if action == "host.capabilities.get":
        return "host"
    if action.startswith("conversations."):
        return "conversation"
    if action.startswith(("users.local.", "users.auth.")):
        return "local_users"
    if action.startswith("execute."):
        return "execution"
    return "observations"


# LLM: 门户与双Bot逐动作核真实依赖，惰性兄弟模块缺失时不可用，不借main执行器兜底。
# 函数用途: 核验当前组合根是否真正具备指定动作的后端。
def domain_ready(action: str, ports: HostDomainPorts) -> bool:
    name = domain_name(action)
    if name == "portal_v2":
        if action == "conversations.requests.interject":
            return (getattr(ports.conversation, "request_ports", None) is not None
                    and callable(getattr(ports.conversation, "require_thread", None)))
        backend = getattr(ports.conversation, "portal_v2", None)
        return bool(backend is not None and backend.ready(action))
    if name == "audit_bots":
        from .audit_bot_v2_handlers import audit_bots_ready
        return audit_bots_ready(action, ports)
    if name == "capabilities_v2":
        from .capabilities_v2_handlers import capabilities_v2_ready
        return capabilities_v2_ready(action, ports)
    if name == "host":
        return True
    port = {"conversation": ports.conversation, "local_users": ports.local_users,
            "execution": ports.execution, "observations": ports.observations}[name]
    registered = getattr(port, "actions", None)
    if registered is not None and action not in registered:
        return False
    if name == "conversation":
        method = "stream" if action_spec(action).mode == "stream" else "call"
        return callable(getattr(ports.conversation, method, None))
    if name == "local_users":
        return callable(getattr(ports.local_users, "call", None))
    if name == "execution":
        return callable(getattr(ports.execution, "submit", None))
    return callable(getattr(ports.observations, "call", None))


# LLM: available同时受真实端口与批准能力限制，不能把清单申请等同已批准。
# 函数用途: 返回中央动作目录的安全公开视图。
def discover(authorized: AuthorizedCall, ports: HostDomainPorts) -> DomainResult:
    caps = authorized.binding.capabilities
    rows = []
    for spec in action_specs().values():
        permitted = spec.capabilities <= caps and (not spec.any_capabilities or bool(spec.any_capabilities & caps))
        rows.append({"action": spec.name, "mode": spec.mode, "required_capabilities": sorted(spec.capabilities),
                     "any_capabilities": sorted(spec.any_capabilities), "payload_schema": spec.payload_schema,
                     "scope_requirements": list(spec.scope_requirements),
                     "available": permitted and domain_ready(spec.name, ports)})
    return DomainResult(200, {"actions": rows, "capabilities": sorted(caps)})


# LLM: 域调用固定入口，结果必须是公共DTO；异常不带原异常正文，写域未知结果不伪装成未执行。
# 函数用途: 分派调用并在返回前重新检查权限。
def dispatch_call(authorized: AuthorizedCall, ports: HostDomainPorts) -> DomainResult:
    revalidate_call(authorized)
    action = authorized.call.action
    if action_spec(action).mode == "stream":
        raise HostAPIError("INVALID_SCHEMA")
    if not domain_ready(action, ports):
        raise HostAPIError("HOST_NOT_READY", 503)
    try:
        result = _invoke(authorized, ports)
        if not isinstance(result, DomainResult):
            raise ValueError("领域未返回固定DTO")
        validate_tree(thaw(result.data))
        if len(canonical_json(result.data).encode()) > PAGE_MAX_BYTES:
            raise HostAPIError("PAYLOAD_TOO_LARGE", 413, _observed_outcome(action))
    except HostAPIError:
        raise
    except Exception:
        code = "OUTCOME_UNKNOWN" if action_spec(action).requires_idempotency else "STORE_UNAVAILABLE"
        raise HostAPIError(code, 503, "unknown" if code == "OUTCOME_UNKNOWN" else "not_started") from None
    try:
        revalidate_call(authorized)
    except HostAPIError as exc:
        raise HostAPIError(exc.code, exc.status, _observed_outcome(action)) from None
    return result


# LLM: 门户只消费真实AuthorizedCall和生产端口，插话直接进入请求域；缺兄弟模块不构造替身业务。
# 函数用途: 执行原领域、门户、V2扩展或已就绪Bot动作。
def _invoke(authorized: AuthorizedCall, ports: HostDomainPorts):
    name = domain_name(authorized.call.action)
    if name == "portal_v2":
        if authorized.call.action == "conversations.requests.interject":
            from .conversations import read_context
            from .requests import RequestDomain
            return RequestDomain(ports.conversation).call(authorized, read_context(authorized))
        return ports.conversation.portal_v2.call(authorized)
    if name == "audit_bots":
        from .audit_bot_v2_handlers import call_audit_bots
        return call_audit_bots(authorized, ports)
    if name == "capabilities_v2":
        from .capabilities_v2_handlers import call_capabilities_v2
        return call_capabilities_v2(authorized, ports)
    if name == "host":
        return discover(authorized, ports)
    if name == "conversation":
        return ports.conversation.call(authorized)
    if name == "local_users":
        return _c_result(ports.local_users.call(authorized))
    if name == "execution":
        return _c_result(ports.execution.submit(authorized))
    return ports.observations.call(authorized)


# LLM: 只转换C的准确DTO，未知返回仍由dispatch拒绝；警告不得丢失。
# 函数用途: 将本地用户/执行回执规范化成A0领域结果。
def _c_result(result):
    from .local_users import LocalUserDomainResult
    if type(result) is LocalUserDomainResult:
        return DomainResult(result.status, result.data, warnings=result.warnings)
    return result


# LLM: outcome只描述写副作用：合法写成功包括原受理回执提交，不代表202业务完成；纯读取无写入，异常中途不推断已提交。
# 函数用途: 标注已观察的同步结果是否包含写提交。
def _observed_outcome(action: str) -> str:
    return "committed" if action_spec(action).requires_idempotency else "not_started"


# LLM: 迭代器每个事件前后核原授权；stream关闭只结束订阅，不取消底层请求。
# 函数用途: 为独立传输泵提供受验证事件序列。
def stream_events(authorized: AuthorizedCall, ports: HostDomainPorts):
    revalidate_call(authorized)
    if authorized.call.action != "conversations.requests.stream":
        raise HostAPIError("INVALID_SCHEMA")
    if not domain_ready(authorized.call.action, ports):
        raise HostAPIError("HOST_NOT_READY", 503)
    iterator = iter(ports.conversation.stream(authorized))
    try:
        while True:
            revalidate_call(authorized)
            try:
                event = next(iterator)
            except StopIteration:
                return
            revalidate_call(authorized)
            yield event
    finally:
        close = getattr(iterator, "close", None)
        if callable(close):
            close()


# LLM: 只在可信启动组合根构造，不从HTTP正文注册端口；关闭同时撤销来源和游标。
# 类用途: 保存同一次宿主服务的A0依赖，供B/C/D接入。
class HostComposition:
    # LLM: owner_resolver只给issuer，不挤入四个领域端口；默认空端口明确不可用。
    # 函数用途: 连接领域、真实来源及分页服务。
    def __init__(self, ports: HostDomainPorts | None = None, owner_resolver=None, capability_policy=None):
        self.ports = ports or HostDomainPorts()
        self.issuer = IngressIssuer(owner_resolver, capability_policy)
        self.cursors = CursorCodec()

    # LLM: 同步关闭不声称业务任务已停止。
    # 函数用途: 废弃本次服务的来源和分页引用。
    def close(self) -> None:
        self.issuer.close()
        self.cursors.close()
