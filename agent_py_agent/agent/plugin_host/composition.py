# LLM: 组合根只接宿主注册的四域对象；没有领域时明确未就绪，不用旧Broker或main invoker代替。
# 模块用途: 提供静态分发、真实可用性发现与返回前撤权复验。
from __future__ import annotations

from .action_specs import action_spec, action_specs
from .authorization import IngressIssuer, revalidate_call
from .contracts import AuthorizedCall, DomainResult, HostDomainPorts
from .cursors import CursorCodec
from .errors import HostAPIError
from .schemas import PAGE_MAX_BYTES, canonical_json, thaw, validate_tree


# LLM: 纯静态前缀分组，不接受用户方法名；动作存在性先由ActionSpec保证。
# 函数用途: 选择一个动作所属的固定领域。
def domain_name(action: str) -> str:
    action_spec(action)
    if action == "host.capabilities.get":
        return "host"
    if action.startswith("conversations."):
        return "conversation"
    if action.startswith(("users.local.", "users.auth.")):
        return "local_users"
    if action.startswith("execute."):
        return "execution"
    return "observations"


# LLM: 没有端口或对应方法就不可用；stream独立于call，不能用同步方法冒充。
# 函数用途: 核验当前组合根是否接入指定动作。
def domain_ready(action: str, ports: HostDomainPorts) -> bool:
    name = domain_name(action)
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


# LLM: 分派没有动态调用插件选择的属性；每个端口只能拿到已授权对象。
# 函数用途: 执行一个固定领域调用。
def _invoke(authorized: AuthorizedCall, ports: HostDomainPorts):
    name = domain_name(authorized.call.action)
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
