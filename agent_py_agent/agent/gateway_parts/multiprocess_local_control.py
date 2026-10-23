# LLM: client/管理共用同source与首native槽；client只当前owner且无receipt端口，管理memory仅factory栈保原seal至父unknown结束，不借给client/Count0。
# LLM: memory固定binder须同对象保留原来源与原ingress两类实际拒绝，恢复事实不能洗掉首次失败；登记资格仍唯一53谓词。
# LLM: cold原HTTP source仅保首个实际producer native失败对象；后继固定writer/正常出口拒绝，当前physical/common outer优先；Count0不使用该槽。
# LLM: C控制pin只保责任；每次文件RMW消费同原executing/C/实际资源owner的P2短门，管理不借main权限，网络/退役门外。
# LLM: 原ready Agent/lease-only只走固定receipt叶且原T由caller持有；fullcontroller和通用perform不回退，不造P2或关闭证明。
# 模块用途: 同类真实HTTP来源另持原操作T；本机原插话对账另走固定expected/result叶，普通receipt状态图保持；把本机控制绑定到同一原C及原控制回执，撤权后禁止新的配置叶子写，原责任仍精确结清。
import hashlib
import json
import os
import threading
import time
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace

from ..common.global_write_commit import global_write_commit_scope
from ..common.owner_write_errors import (
    LocalControlCoordinatorUnavailable as LocalControlCoordinatorUnavailable,
)
from ..user_space.owner_resolver import OwnerIdentity
from .multiprocess_control_forward import _coordinator_context_paths
from .multiprocess_coordinator import (
    coordinator_for_root,
    coordinator_role_present,
    pin_coordinator_commit,
    retire_coordinator_commit,
)
from .multiprocess_execution_lifetime import ExecutionScopeLifetime
from .multiprocess_route import owner_digest


# LLM: 只精确退休本factory原pin，不释放任何持久账；首个typed异常不能被清理失败替换。
# 函数用途: 保留真实失败对象及未确认的清理诊断。
def _retire_http_management(lease, pin, primary):
    if pin is None:
        return
    try:
        retire_coordinator_commit(lease, pin)
    except BaseException:
        if primary is None:
            raise
        primary.add_note('owner_management_pin_cleanup_unconfirmed')


# LLM: 原prepared身份字段形成只读比较值；执行状态/结果/时间/后补线程不参与输入身份，不新增账或许可。
# 函数用途: 固定原控制回执的完整输入，后续不能用另一个自洽回执替代。
def _control_write_binding(receipt):
    payload = receipt.to_dict()
    for key in ('state', 'result', 'error', 'updated_at', 'conversation_thread_id'):
        payload.pop(key, None)
    return json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


# LLM: 只存本次已认证HTTP和规范owner事实，不持receipt/command/actor代理；frozen绑定仍须每次末写重核原work与权限。
# 类用途: 保存client memory当前请求的实际引用，不对外序列化或缓存到holder。
@dataclass(frozen=True, eq=False, repr=False)
class _ClientMemoryHTTPFacts:
    handler: object
    server: object
    body: dict
    scope: object
    work: object
    control: object
    runtime: object
    paths: object
    lease: object
    controller: object
    middleware: object
    config: object
    home: object
    owner: object
    policy: object
    holder: object
    agent: object
    body_digest: bytes
    c_run_id: str


# LLM: 两种原HTTP工厂共用首native槽；资格只读原对象/Thread/work/lifetime，不要求故障后active成功，不setter或授权。
# 函数用途: 拒绝复制或跨线程故障污染，旧Agent不读HTTP。
def _original_http_failure_producer(source):
    if source.http_origin is None:
        return False
    from .multiprocess_http_transport import current_http_request_work

    facts = source.http_origin
    if type(facts) is _ClientMemoryHTTPFacts:
        work, handler = facts.work, facts.handler
    elif type(facts) is tuple:
        work, handler = facts[6], facts[0]
    else:
        return False
    return (type(source) is _LocalControlWriteSource and source.origin is source
            and source.pid == os.getpid() and source._http_thread_origin is threading.current_thread()
            and source.http_origin is source._http_origin and work is current_http_request_work()
            and work._handler is handler
            and type(source.lifetime) is ExecutionScopeLifetime and source.lifetime is source._lifetime_origin
            and source.lifetime._origin is source.lifetime and source.lifetime._execution is source
            and source._native_source_failure is None)


# LLM: 纯内存共同初始化只接实际引用，不计算receipt/owner或造Agent；两个原工厂分别冻结各自真实authority。
# 函数用途: 记录原来源生命周期、Thread和首native槽，避免client走伪receipt构造。
def _initialize_original_source_refs(source, agent, paths, context, lease, pin, lifetime):
    source.agent, source.paths, source.context = agent, paths, context
    source.lease, source.pin = lease, pin
    source.controller, source.pid, source.thread = lease.controller, os.getpid(), threading.get_ident()
    source.origin = source
    source.resource_home = source._resource_home_origin = None
    source.actor = source.subject = source._actor_origin = source._subject_origin = None
    source.lifetime = source._lifetime_origin = lifetime
    source.http_origin = source._http_origin = None
    source._native_source_failure = None
    source._http_thread_origin = threading.current_thread()


# LLM: 只对真实正count handler解析当前认证owner，不从body target/管理员跨owner字段选资源；无IO构造或管理receipt。
# 函数用途: 在原work内取得当前记忆请求的规范home、配置和权限事实。
def _capture_client_memory_facts(handler, server, body):
    from ..auth.models import Permission
    from ..backends.request_scope import _DEADLINE
    from ..user_space.owner_policy import resolve_effective_owner_policy
    from .control_plane import GatewayControlPlane
    from .control_service import _scope_request_payload
    from .coordinator_memory_resources import (
        CoordinatorMemoryResources,
        CoordinatorMemoryResourcesUnavailable,
    )
    from .http_handlers import _gateway_control_scope, _request_channel, _request_identity
    from .http_service import GatewayHTTPHandler, GatewayHTTPServer
    from .multiprocess_http_transport import capture_http_handler_work
    from .owner_resources import resolve_owner_resource_configuration
    from .request_worker import _resolve_request_owner_from_config

    if (type(server) is not GatewayHTTPServer or not isinstance(handler, GatewayHTTPHandler)
            or type(server.control_plane) is not GatewayControlPlane or server.control_plane.worker_count <= 0
            or str(body.get('operation') or '').strip().lower() not in {'search', 'remember'}):
        raise LocalControlCoordinatorUnavailable('client memory original HTTP source is unavailable')
    try:
        work = capture_http_handler_work(handler, server)
    except ValueError as error:
        raise LocalControlCoordinatorUnavailable('client memory original HTTP source is unavailable') from error
    user, channel = _request_channel(handler)
    identity, permission = _request_identity(handler)
    middleware = handler._auth_middleware
    if (middleware is None or not middleware.has_gateway_credential(dict(handler.headers))
            or type(permission) is not Permission or identity != user or permission.user_id != user or channel not in permission.channels):
        raise LocalControlCoordinatorUnavailable('client memory current identity is unavailable')
    scope = _gateway_control_scope(handler, body, user_id=user, channel=channel)
    if not scope.conversation_id:
        raise LocalControlCoordinatorUnavailable('client memory current conversation is unavailable')
    control = server.control_plane
    owner = _resolve_request_owner_from_config(control.config, _scope_request_payload(scope), control.home_root)
    home, _config, _paths = resolve_owner_resource_configuration(control, owner)
    policy = resolve_effective_owner_policy(home)
    holder = server.coordinator_memory_resources
    if type(holder) is not CoordinatorMemoryResources or policy.load_errors or not policy.memory_enabled:
        raise LocalControlCoordinatorUnavailable('client memory current policy is unavailable')
    try:
        holder.require_open()
    except CoordinatorMemoryResourcesUnavailable as error:
        raise LocalControlCoordinatorUnavailable('client memory resources are closed') from error
    lease, controller = coordinator_for_root(server.paths.root, context=control), control.controller()
    if lease is None or controller is None:
        raise LocalControlCoordinatorUnavailable('client memory coordinator is unavailable')
    return _ClientMemoryHTTPFacts(handler, server, body, scope, work, control, server.drain_runtime,
        server.paths, lease, controller, middleware, control.config, home, owner, policy, holder, server.agent,
        hashlib.sha256(json.dumps(body, sort_keys=True).encode()).digest(), lease.c_run_id), _DEADLINE.get()


# LLM: 固定client mode与管理共享同source/issuer/lifetime/首槽，不造receipt；业务与构造门外，pin只保本HTTP责任。
# 类用途: 把client工厂与末核按职责放在同模块，不扩大管理权限或新增来源类型。
class _ClientMemorySourceMode:
    # LLM: 只有实际HTTP factory生产client事实；finally同原算法关闭lifetime/退pin，不缓存source到holder或提供管理回执端口。
    # 函数用途: 给同一次search/remember发放当前owner的本地末写来源。
    @classmethod
    @contextmanager
    def http_client_memory_scope(cls, handler, server, body):
        facts, deadline = _capture_client_memory_facts(handler, server, body)
        lifetime, pin, primary = ExecutionScopeLifetime(), None, None
        try:
            pins = []

            # LLM: 只在原C/owner出生门登记当前HTTP责任，不执行业务或把pin当许可。
            # 函数用途: 固定同lease的原client工作片责任。
            def begin():
                pins.append(pin_coordinator_commit(facts.lease,
                    (facts.lease.c_run_id, 'client-memory', str(id(facts.work)))))

            decision = facts.controller.launch_owner_writer(owner_digest(facts.owner), begin)
            if decision.get('status') != 'started' or len(pins) != 1:
                raise LocalControlCoordinatorUnavailable('client memory coordinator is unavailable')
            pin = pins[0]
            source = cls.__new__(cls)
            _initialize_original_source_refs(source, facts.agent, facts.paths, facts.control, facts.lease, pin, lifetime)
            source.owner = source._owner_origin = facts.owner
            source._client_references = (facts.agent, facts.paths, facts.control, facts.lease, pin)
            source.http_origin = source._http_origin = facts
            source.deadline = deadline
            lifetime.bind_original(source)
            source._require_http_current()
            yield source
            source._raise_retained_native_failure()
        except BaseException as error:
            primary = error
            raise
        finally:
            lifetime.close()
            _retire_http_management(facts.lease, pin, primary)

    # LLM: 只绑定factory已捕获的同一个canonical当前home，不接受actor/subject/foreign target；管理三元绑定不改。
    # 函数用途: 让原领域writer固定当前owner文件边界。
    def bind_client_memory_owner(self, home):
        self._require_http_current()
        if (type(self.http_origin) is not _ClientMemoryHTTPFacts or home is not self.http_origin.home
                or self.resource_home is not None):
            raise LocalControlCoordinatorUnavailable('client memory original owner is unavailable')
        self.resource_home = self._resource_home_origin = home

    # LLM: 每次末核真实work/上层引用/G1/body/owner/policy/holder和原C；runtime门只try，未知/撤权不会变成普通业务权限。
    # 函数用途: 拒绝晚回调、身份变更、关闭和换代后新增记忆写。
    def _require_client_memory_current(self):
        from ..common.json_io import check_io_deadline
        from .multiprocess_http_transport import require_http_handler_work

        facts = self.http_origin
        if (type(self) is not _LocalControlWriteSource or self.origin is not self or facts is not self._http_origin
                or self.pid != os.getpid() or self._http_thread_origin is not threading.current_thread()
                or self.owner is not self._owner_origin or self.owner is not facts.owner
                or any(a is not b for a, b in zip((self.agent, self.paths, self.context, self.lease, self.pin),
                                                  self._client_references))
                or type(self.lifetime) is not ExecutionScopeLifetime or self.lifetime is not self._lifetime_origin):
            raise LocalControlCoordinatorUnavailable('client memory original writer is unavailable')
        gate = facts.runtime._gate
        if not gate.acquire(blocking=False):
            raise BlockingIOError('client memory HTTP resource gate is busy')
        try:
            try:
                self.lifetime.require_original(self)
                require_http_handler_work(facts.work, facts.handler, facts.server)
            except ValueError as error:
                raise LocalControlCoordinatorUnavailable('client memory original writer is unavailable') from error
            if (self.context is not facts.control or self.paths is not facts.paths or self.agent is not facts.agent
                    or self.lease is not facts.lease or self.controller is not facts.controller
                    or facts.server.agent is not facts.agent or facts.server.control_plane is not facts.control
                    or facts.control.config is not facts.config or facts.handler._auth_middleware is not facts.middleware
                    or facts.server.coordinator_memory_resources is not facts.holder
                    or facts.holder.control is not facts.control or facts.holder.runtime is not facts.runtime
                    or facts.holder.paths is not facts.paths or facts.holder.config is not facts.config
                    or coordinator_for_root(facts.paths.root, context=facts.control) is not facts.lease
                    or facts.control.controller() is not facts.controller or self.pin not in facts.lease._commit_pins
                    or facts.lease.c_run_id != facts.c_run_id):
                raise LocalControlCoordinatorUnavailable('client memory original writer is unavailable')
            current, _deadline = _capture_client_memory_facts(facts.handler, facts.server, facts.body)
            if (current.work is not facts.work or current.scope != facts.scope or current.owner != facts.owner
                    or current.home != facts.home or current.policy != facts.policy or current.body_digest != facts.body_digest):
                raise LocalControlCoordinatorUnavailable('client memory current facts changed')
            check_io_deadline(self.deadline)
        finally:
            gate.release()

    # LLM: 原controller/P2和Root只包固定本地末writer，原领域锁已在外；不要求管理executing回执或claim管理效果。
    # 函数用途: 用本次HTTP的当前owner执行一次原同步文件或数据库提交。
    def _perform_client_memory(self, operation):
        self._require_http_current()
        if self.resource_home is None or self.resource_home is not self._resource_home_origin:
            raise LocalControlCoordinatorUnavailable('client memory original owner is unavailable')
        results = []

        # LLM: 仅末门内核同source，不等待领域锁/网络/scan；callback不被视为来源producer。
        # 函数用途: 在同一原Root/P2窗口提交已准备好的本地操作。
        def commit():
            gate = self.http_origin.runtime._gate
            if not gate.acquire(blocking=False):
                raise BlockingIOError('client memory HTTP resource gate is busy')
            try:
                self._require_http_current()
                results.append(operation())
            finally:
                gate.release()

        decision = self.controller.launch_owner_writer(owner_digest(self.owner), commit)
        if decision.get('status') != 'started' or len(results) != 1:
            error = LocalControlCoordinatorUnavailable('client memory coordinator generation is unavailable')
            if _original_http_failure_producer(self):
                self._native_source_failure = error
            raise error
        return results[0]


# LLM: 只退休本次factory持有的memory issuer栈；正常关闭无IO，已有actual outer不能被清理异常替换，原lifetime/pin仍由factory finally退休。
# 函数用途: 关闭真人记忆封印并把异常清理失败附到原错误，不补写回执。
def _close_management_memory_stack(stack, primary):
    if stack is None:
        return
    try:
        stack.close()
    except BaseException as cleanup_error:
        if primary is None:
            raise
        primary.add_note('control_memory_ingress_close_failed:' + type(cleanup_error).__name__)


# LLM: 两种HTTP模式仍只发行本类原source；client实现mixin仅分组同类职责，不是第二issuer，管理receipt与actor资源不向client开放。
# 类用途: 把已持原control T的同步配置修改接到原最后短写门，不造HostCommand或ToolOperation。
class _LocalControlWriteSource(_ClientMemorySourceMode):
    # LLM: 原receipt构造捕获同controller引用，仅固定ready receipt可识别原lease-only形态；不改变perform或client来源，晚失controller不得降级。
    # 函数用途: 保存原本地主体控制和回执来源，真人memory与cold管理仍各守原入口。
    def __init__(self, agent, paths, receipt, context, lease, pin, *, lifetime=None):
        _initialize_original_source_refs(self, agent, paths, context, lease, pin, lifetime)
        self._receipt_controller_origin = self.controller
        self.receipt = receipt
        self.binding = _control_write_binding(receipt)
        self.owner = OwnerIdentity(receipt.owner_provider, receipt.owner_kind, receipt.owner_id)
        self._owner_origin = self.owner
        self._references = (agent, paths, receipt, context, lease, pin)
        self.receipt_owner = self._receipt_owner_origin = self.owner
        self._receipt_raw, self._receipt_state = None, None
        self._actor_control_resources = self._actor_control_resources_origin = None
        self._management_memory_binding = self._management_memory_binding_origin = None
        self._management_memory_stack = self._management_memory_stack_origin = None

    # LLM: 仅同原已claimed/executing memory源可绑定一次；原固定producer按53资格保来源/ingress两类首native，工厂stack持issuer至父未知收口，不接参数/callback或发行新权限。
    # 函数用途: 在actor线程与holder初始化前固定真人封印，每次原current末核都复查它。
    def bind_management_memory_ingress(self):
        from ..common.owner_write_errors import OwnerManagementIngressUnavailable
        from .coordinator_memory_candidates import _bind_management_memory_ingress

        self._require_current()
        try:
            scope = _bind_management_memory_ingress(self)
        except (LocalControlCoordinatorUnavailable, OwnerManagementIngressUnavailable) as error:
            if _original_http_failure_producer(self):
                self._native_source_failure = error
            raise
        self._require_current()
        return scope

    # LLM: 原HTTP工厂只为memory保本次ExitStack至父producer未知收口结束；原issuer关闭后再退休lifetime/pin，非memory无新IO/issuer，正常首native与outer优先不变。
    # 函数用途: 给冷管理创建原C写源，不构造Agent，不从回执重建授权，退出精确退pin并关闭所有副本。
    @classmethod
    @contextmanager
    def http_management_scope(cls, handler, server, body):
        from . import control_operation_service as ops
        from .coordinator_owner_management import capture_owner_management_http
        from .owner_management_control import authenticated_management_request

        facts = capture_owner_management_http(handler, server, body)
        command, text, scope, work, lease, controller, deadline = facts
        operation_id, digest = ops.gateway_control_operation_identity(scope, text)
        with ops.try_gateway_control_operation_transition(server.paths, operation_id) as acquired:
            existing = ops.read_gateway_control_operation(server.paths, operation_id, read_only=True)
            if existing is not None:
                if existing.input_digest != digest:
                    raise ops.GatewayControlOperationConflict('gateway control message id was reused with different input')
                yield None, existing
                return
            if not acquired:
                yield None, None
                return
            with authenticated_management_request(handler, command, scope) as ingress:
                receipt = ops._new_control_operation_receipt(scope, command.kind, text, operation_id, digest)
                ops._validate_control_operation_receipt(receipt)
                lifetime, pin, primary = ExecutionScopeLifetime(), None, None
                memory_stack = ExitStack() if command.kind == 'memory' else None
                try:
                    pin = pin_coordinator_commit(lease, (lease.c_run_id, 'owner-management', operation_id))
                    source = cls(None, server.paths, receipt, server.control_plane, lease, pin, lifetime=lifetime)
                    source._management_memory_stack = source._management_memory_stack_origin = memory_stack
                    lifetime.bind_original(source)
                    source.http_origin = (handler, server, body, command, text, scope, work, ingress,
                                          server.control_plane, server.drain_runtime, server.paths,
                                          lease, controller, handler._auth_middleware, server.control_plane.config)
                    source._http_origin = source.http_origin
                    source._http_body = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).digest()
                    source._http_home = server.control_plane.home_root
                    source._http_generation = lease.c_run_id
                    source.deadline = deadline
                    source._require_http_current()
                    from .coordinator_actor_resources import _bind_actor_control_resources
                    _bind_actor_control_resources(source)
                    yield source, None
                    source._raise_retained_native_failure()
                except BaseException as error:
                    primary = error
                    raise
                finally:
                    try:
                        _close_management_memory_stack(memory_stack, primary)
                    finally:
                        lifetime.close()
                        _retire_http_management(lease, pin, primary)




    # LLM: 只包本入口原真实检查producer；记录资格核原对象/HTTP pointer/Thread/lifetime绑定，故障后的活性不再要求成功；不接受业务错误setter。
    # 函数用途: 保存首个真实来源拒绝，恢复条件也不能洗掉本次失败；旧Agent不记槽。
    def _require_http_current(self):
        from ..common.global_write_guard import GlobalWriteInWorkerError
        from .owner_management_control import OwnerManagementIngressUnavailable

        self._raise_retained_native_failure()
        try:
            return self._check_http_current()
        except (LocalControlCoordinatorUnavailable, OwnerManagementIngressUnavailable, GlobalWriteInWorkerError) as error:
            if _original_http_failure_producer(self):
                self._native_source_failure = error
            raise

    # LLM: client原事实末核不变；管理可追加已绑定memory固定检查叶，仍在原producer catch内，不反调current或以pin/JSON授信。
    # 函数用途: 每次末写重新确认本次真实冷管理来源没有被替换或撤销。
    def _check_http_current(self):
        if type(self.http_origin) is _ClientMemoryHTTPFacts:
            return self._require_client_memory_current()
        from ..common.json_io import check_io_deadline
        from .coordinator_control_transport import _request_input
        from .multiprocess_http_transport import require_http_handler_work

        if (type(self) is not _LocalControlWriteSource or self.origin is not self
                or self.pid != os.getpid() or self.thread != threading.get_ident()
                or type(self.lifetime) is not ExecutionScopeLifetime or self.lifetime is not self._lifetime_origin
                or type(self.http_origin) is not tuple or self.http_origin is not self._http_origin
                or self.owner is not self._owner_origin
                or self.receipt_owner is not self._receipt_owner_origin
                or any(a is not b for a, b in zip((self.agent, self.paths, self.receipt, self.context,
                                                  self.lease, self.pin), self._references))):
            raise LocalControlCoordinatorUnavailable('control original HTTP writer is unavailable')
        handler, server, body, command, text, scope, work, ingress, control, runtime, paths, lease, controller, middleware, config = self.http_origin
        gate = runtime._gate
        if not gate.acquire(blocking=False):
            raise BlockingIOError('control HTTP resource gate is busy')
        try:
            try:
                self.lifetime.require_original(self)
                require_http_handler_work(work, handler, server)
            except ValueError as error:
                raise LocalControlCoordinatorUnavailable('control original HTTP writer is unavailable') from error
            if (server.agent is not None or server.control_plane is not control or server.drain_runtime is not runtime
                    or server.paths is not paths or control.config is not config or self.context is not control
                    or coordinator_for_root(paths.root, context=control) is not lease
                    or control.controller() is not controller or self.controller is not controller
                    or self.pin not in lease._commit_pins or lease.c_run_id != self._http_generation
                    or control.home_root != self._http_home or handler._auth_middleware is not middleware
                    or not middleware.has_gateway_credential(dict(handler.headers))
                    or hashlib.sha256(json.dumps(body, sort_keys=True).encode()).digest() != self._http_body
                    or _request_input(handler, server, body) != (command, text, scope)):
                raise LocalControlCoordinatorUnavailable('control original HTTP writer is unavailable')
            ingress.current_permission(command, scope)
            from .coordinator_actor_resources import _check_actor_control_resources
            _check_actor_control_resources(self)
            if self._management_memory_binding is not None:
                from .coordinator_memory_candidates import _require_management_memory_current
                _require_management_memory_current(self)
            check_io_deadline(self.deadline)

        finally:
            gate.release()



    # LLM: client明确拒绝管理actor资源； 同模块固定actor资源端口承接真实原对象；不接owner/path/callback，目标P2仍独立。
    # 函数用途: 在原操作者范围建立会话，具体原存储事务由资源组合叶处理。
    def prepare_actor_control_thread(self):
        if type(self.http_origin) is _ClientMemoryHTTPFacts:
            raise LocalControlCoordinatorUnavailable('client memory has no management actor resources')
        from .coordinator_actor_resources import _prepare_actor_control_thread
        return _prepare_actor_control_thread(self)

    # LLM: 只读抛本cold首native，不校验恢复后的current、不写新状态；无setter/IO/callback，finally不抢当前physical/common outer。
    # 函数用途: 在固定writer及正常出口前拒绝本次已失败来源；旧Agent不使用该槽。
    def _raise_retained_native_failure(self):
        if self.http_origin is not None and self._native_source_failure is not None:
            raise self._native_source_failure

    # LLM: client拒receipt；HTTP管理与实际local各核原source，固定叶同锁codec/raw CAS/readback后仅登记该项，Root/JSON只try。
    # 函数用途: 按prepared、executing、completed或unknown的固定顺序保存本次回执，任何夹写或锁忙均不覆盖旧事实。
    def commit_control_receipt(self, payload):
        return _commit_original_control_receipt(self, payload)

    # LLM: 固定writer先拒首native；client走当前owner实际HTTP/P2，管理原claim/资源末门保持；callback错误不入来源槽，网络在Root外。
    # 函数用途: 在原C仍获准时执行一次完整配置RMW，closing或回执变化均不进入实际writer。
    def perform(self, operation):
        self._raise_retained_native_failure()
        if type(self.http_origin) is _ClientMemoryHTTPFacts:
            return self._perform_client_memory(operation)
        try:
            if (type(self) is not _LocalControlWriteSource or type(self.lifetime) is not ExecutionScopeLifetime
                    or self.lifetime is not self._lifetime_origin):
                raise ValueError("control_lifetime_unavailable")
            self.lifetime.require_original(self)
        except ValueError as error:
            raise LocalControlCoordinatorUnavailable("control original writer is unavailable") from error
        if (self.origin is not self or self.pid != os.getpid() or self.thread != threading.get_ident()
                or self.controller is None or self.lease.controller is not self.controller
                or self.owner is not self._owner_origin
                or any(current is not original for current, original in zip(
                    (self.agent, self.paths, self.receipt, self.context, self.lease, self.pin), self._references))):
            raise LocalControlCoordinatorUnavailable('control original writer is unavailable')
        if self.http_origin is not None and (self.resource_home is None or not self.http_origin[7].used):
            raise LocalControlCoordinatorUnavailable('control claimed resource is unavailable')
        results = []

        # LLM: launch_owner_writer在原生命周期短门内调用；这里核原来源和executing行，pin存在不替代这些当次权限。
        # 函数用途: 完成最后复读后执行原同步配置事务。
        def commit():
            if self.http_origin is None:
                self._require_current()
                self._require_actor_current()
                results.append(operation())
                return
            gate = self.http_origin[9]._gate
            if not gate.acquire(blocking=False):
                raise BlockingIOError('control HTTP resource gate is busy')
            try:
                self._require_current()
                self._require_actor_current()
                results.append(operation())
            finally:
                gate.release()

        from .owner_management_control import cold_home_identity
        target = self.owner if self.http_origin is None or self.resource_home is None else cold_home_identity(self.resource_home)
        decision = self.controller.launch_owner_writer(owner_digest(target), commit)
        if decision.get('status') != 'started' or len(results) != 1:
            error = LocalControlCoordinatorUnavailable('control coordinator generation is unavailable')
            if _original_http_failure_producer(self):
                self._native_source_failure = error
            raise error
        return results[0]

    # LLM: client明确拒此管理三元绑定；管理仍绑定原actor/subject与target冷home，后继每写重核原actor/P2，不重选owner。
    # 函数用途: 将实际资源owner固定到原控制写源，管理员不能借main门写远端。
    def bind_resource_owner(self, home, *, actor, subject):
        if type(self.http_origin) is _ClientMemoryHTTPFacts:
            raise LocalControlCoordinatorUnavailable('client memory has no management resource binding')
        from ..user_space.owner_resolver import resolve_owner_home
        from .owner_management_control import is_owner_management_control
        if not is_owner_management_control(self.receipt.command_kind):
            raise LocalControlCoordinatorUnavailable('control resource owner is unavailable')
        from .owner_management_control import cold_home_identity
        identity = cold_home_identity(home)
        root = self.agent.home_paths.root if self.http_origin is None else self.context.home_root
        resolved = resolve_owner_home(root, identity)
        if (home.root != root or home.owner_home_dir != resolved.home_dir
                or home.owner_id != resolved.owner_id
                or (self.resource_home is not None and self.resource_home is not home)):
            raise LocalControlCoordinatorUnavailable('control resource owner is unavailable')
        from .owner_management_control import require_actor_resource_permission
        if self.http_origin is not None:
            self._require_current()
            if self.resource_home is not None:
                raise LocalControlCoordinatorUnavailable('control resource owner is already bound')
            if subject != (self.http_origin[7], self.http_origin[3], self.http_origin[5]) or not self.http_origin[7].used:
                raise LocalControlCoordinatorUnavailable('control original actor is unavailable')
        require_actor_resource_permission(self.agent, actor, home, subject,
            **({} if self.http_origin is None else {'config': self.context.config, 'home_root': root, 'control': self.context}))
        self.actor, self.subject = actor, subject
        self._actor_origin, self._subject_origin = actor, subject
        self.resource_home = self._resource_home_origin = home
        if self.http_origin is None:
            self.owner = self._owner_origin = identity
        return self.commit_resource_file

    # LLM: 文件writer先拒本cold首native，再核原owner home；绑定不可后替，非法资源参数只拒绝不入槽，不接新owner补许可。
    # 函数用途: 核对文件写者仍使用本次管理选定的资源，再进入原短门。
    def commit_resource_file(self, operation, *, owner_home):
        self._raise_retained_native_failure()
        if (self.resource_home is None or self.resource_home is not self._resource_home_origin
                or owner_home != self.resource_home.owner_home_dir):
            raise LocalControlCoordinatorUnavailable('control resource owner is unavailable')
        return self.perform(operation)


    # LLM: 只包本入口原真实检查producer；记录资格核原对象/HTTP pointer/Thread/lifetime绑定，故障后的活性不再要求成功；不接受业务错误setter。
    # 函数用途: 保存首个真实来源拒绝，恢复条件也不能洗掉本次失败；旧Agent不记槽。
    def _require_actor_current(self):
        from .owner_management_control import OwnerManagementIngressUnavailable

        self._raise_retained_native_failure()
        try:
            return self._check_actor_current()
        except (LocalControlCoordinatorUnavailable, OwnerManagementIngressUnavailable) as error:
            if _original_http_failure_producer(self):
                self._native_source_failure = error
            raise

    # LLM: client原末核保持；管理actor核后追加本栈memory固定检查，原HostActor/P2与当前policy分别成立，不从pin/target重造权限。
    # 函数用途: 撤销跨用户权限或更换主体后阻止新的nonce、缓存、配置及正文写。
    def _check_actor_current(self):
        if type(self.http_origin) is _ClientMemoryHTTPFacts:
            return self._require_http_current()
        if self._management_memory_binding is not None:
            from .coordinator_memory_candidates import _require_management_memory_current
            _require_management_memory_current(self)
        if self.resource_home is None:
            return
        if self.actor is not self._actor_origin or self.subject is not self._subject_origin:
            raise LocalControlCoordinatorUnavailable('control original actor is unavailable')
        from .owner_management_control import require_actor_resource_permission
        require_actor_resource_permission(self.agent, self.actor, self.resource_home, self.subject,
            **({} if self.http_origin is None else {'config': self.context.config, 'home_root': self.context.home_root, 'control': self.context}))


    # LLM: 只包本入口原真实检查producer；记录资格核原对象/HTTP pointer/Thread/lifetime绑定，故障后的活性不再要求成功；不接受业务错误setter。
    # 函数用途: 保存首个真实来源拒绝，恢复条件也不能洗掉本次失败；旧Agent不记槽。
    def _require_current(self):

        self._raise_retained_native_failure()
        try:
            return self._check_current()
        except LocalControlCoordinatorUnavailable as error:
            if _original_http_failure_producer(self):
                self._native_source_failure = error
            raise

    # LLM: client无receipt只核原HTTP；管理同原T/read_only codec/executing/binding/C/pin末核保留，读账不签权或修复。
    # 函数用途: 在实际文件修改之前复核本次控制仍属同一原来源。
    def _check_current(self):
        if type(self.http_origin) is _ClientMemoryHTTPFacts:
            return self._require_http_current()
        from .control_operation_service import read_gateway_control_operation

        if (coordinator_for_root(self.paths.root, context=self.context) is not self.lease
                or self.pin not in self.lease._commit_pins):
            raise LocalControlCoordinatorUnavailable('control coordinator generation is unavailable')
        if self.http_origin is not None:
            self._require_http_current()
        current = read_gateway_control_operation(self.paths, self.receipt.operation_id,
            **({} if self.http_origin is None else {'read_only': True}))
        if current is None or current.state != 'executing' or _control_write_binding(current) != self.binding:
            raise LocalControlCoordinatorUnavailable('control original executing receipt is unavailable')


# LLM: 原操作T内准入同receipt owner pin；C管理可冷处理远端，实际target另核P2；业务门外、每file提交核原源，退出副本失效。
# 函数用途: 将本机控制责任及配置叶子绑定到同一个原C，不让尚活pin授权closing后的新写。
@contextmanager
def local_control_operation_scope(agent, paths, receipt):
    context = _coordinator_context_paths(agent, paths)
    lease = coordinator_for_root(paths.root, context=context)
    if lease is None:
        if coordinator_role_present(paths.root, context=context):
            raise LocalControlCoordinatorUnavailable('control coordinator generation is unavailable')
        yield
        return
    from .owner_management_control import is_owner_management_control
    if ((receipt.owner_provider, receipt.owner_kind, receipt.owner_id) != ('local', 'main', 'main')
            and not is_owner_management_control(receipt.command_kind)):
        raise LocalControlCoordinatorUnavailable('control coordinator generation is unavailable')
    pins, lifetime = [], None

    # LLM: 只在原owner P2/C准入内登记同receipt责任，不执行业务或等待资源。
    # 函数用途: 保存本次管理或本机控制的原pin。
    def begin():
        with lease._lifecycle_gate:
            if coordinator_for_root(paths.root, context=context) is not lease:
                raise LocalControlCoordinatorUnavailable('control coordinator generation is unavailable')
            pins.append(pin_coordinator_commit(lease, (lease.c_run_id, 'local-control', receipt.operation_id)))

    try:
        controller = lease.controller
        if controller is None:
            begin()
        else:
            # blocked也是非空字典；结构化成功和实际原pin必须同时成立。
            owner = OwnerIdentity(receipt.owner_provider, receipt.owner_kind, receipt.owner_id)
            decision = controller.launch_owner_writer(owner_digest(owner), begin)
            if decision.get('status') != 'started' or len(pins) != 1:
                raise LocalControlCoordinatorUnavailable('control coordinator generation is unavailable')
        lifetime = ExecutionScopeLifetime()
        source = _LocalControlWriteSource(agent, paths, receipt, context, lease, pins[0], lifetime=lifetime)
        lifetime.bind_original(source)
        with global_write_commit_scope(source.perform):
            yield source
    finally:
        if lifetime is not None:
            lifetime.close()
        for pin in pins:
            retire_coordinator_commit(lease, pin)


# LLM: 原HTTP/model/local及真实本机私卡各核既有固定source；签卡源不借model，receipt内容不是来源或权限。
# 函数用途: 在回执写前后核同op/owner/input与原Thread/lifetime/C，不把HTTP检查套给local。
def _require_receipt_origin(source, receipt):
    from . import control_operation_service as ops
    from .private_interaction_transaction import _PrivateInteractionTransaction

    if type(source) is _LocalControlWriteSource:
        if (source.origin is not source or source.pid != os.getpid() or source.thread != threading.get_ident()
                or source.lifetime is not source._lifetime_origin
                or any(a is not b for a, b in zip((source.agent, source.paths, source.receipt, source.context,
                                                  source.lease, source.pin), source._references))
                or source.owner is not source._owner_origin or source.receipt_owner is not source._receipt_owner_origin
                or coordinator_for_root(source.paths.root, context=source.context) is not source.lease
                or source.pin not in source.lease._commit_pins
                or receipt.operation_id != source.receipt.operation_id
                or _control_write_binding(receipt) != source.binding):
            raise LocalControlCoordinatorUnavailable('control original receipt source is unavailable')
        source.lifetime.require_original(source)
        if source.http_origin is not None:
            source._require_http_current()
        else:
            source._require_actor_current()
        return
    if type(source) is not _PrivateInteractionTransaction:
        raise ValueError('control_fixed_model_source_required')
    source.require_current()
    if source._model_scope is None:
        return _require_local_private_approval_receipt(source, receipt)
    from dataclasses import replace
    scope = replace(source._model_scope, resolved_owner=source.owner)
    text = str(source.body.get('command', source.body.get('prompt', '')) or '')
    operation, digest = ops.gateway_control_operation_identity(scope, text)
    # 同时支持 model 与 effort set 短事务回执来源校验。
    command = ops.parse_conversation_control(text, reject_unknown_slash=True)
    if (command is None or receipt.command_kind != command.kind or receipt.command_kind not in {'model', 'effort'}
            or command.kind == 'effort' and command.operation != 'set'
            or receipt.operation_id != operation or receipt.input_digest != digest
            or receipt.command_text != text.strip()
            or (receipt.owner_provider, receipt.owner_kind, receipt.owner_id) !=
               (source.owner.provider, source.owner.owner_kind, source.owner.owner_id)
            or (receipt.user_id, receipt.channel, receipt.conversation_id) !=
               (scope.user_id, scope.channel, scope.conversation_id)):
        raise ValueError('control_fixed_model_receipt_changed')


# LLM: 调用方已核原PrivateInteractionTransaction.current；只原tool_approval/local-main及原actualAgent可写同签卡receipt，不借model或构造Agent。
# 函数用途: 由原签卡输入精确核op/digest/action/owner和会话，冷C无actualAgent仍拒绝。
def _require_local_private_approval_receipt(source, receipt):
    from .private_approval_control import _KIND, _actual_local_agent, _approval_receipt_input

    if (source.payload['kind'] != 'tool_approval' or source.owner != OwnerIdentity.local_main()
            or _actual_local_agent(source) is None):
        raise ValueError('control_fixed_local_approval_source_required')
    scope, action, operation, digest = _approval_receipt_input(source)
    if (receipt.command_kind != _KIND or receipt.command_text != _KIND or receipt.dispatch is not None
            or receipt.operation_id != operation or receipt.input_digest != digest or receipt.private_approval != action
            or (receipt.owner_provider, receipt.owner_kind, receipt.owner_id) != ('local', 'main', 'main')
            or (receipt.user_id, receipt.channel, receipt.conversation_id, receipt.conversation_thread_id) !=
               (scope.user_id, scope.channel, scope.conversation_id, source.payload['scope']['thread_id'])):
        raise ValueError('control_fixed_local_approval_receipt_changed')


# LLM: 只接原private reconcile callee给出的expected及actual updated；原local source或已在perform内的签卡source均须原current/raw门，不重投input。
# 函数用途: 保存既有决定或未知错误的原对账结果，不让普通commit泛开unknown状态。
def _commit_original_private_approval_reconciliation(source, expected_receipt, updated):
    from .control_operation_service import gateway_control_operation_path
    _require_private_approval_reconciliation(source, expected_receipt, updated)
    if type(source) is _LocalControlWriteSource:
        return _commit_original_control_receipt(source, expected_receipt.to_dict(),
            reconciliation=(expected_receipt, updated))
    return _write_original_control_receipt(source,
        gateway_control_operation_path(source.paths, expected_receipt.operation_id), expected_receipt.to_dict(),
        reconciliation=(expected_receipt, updated))


# LLM: 资料只有类型/状态/完整输入绑定意义；固定callee是唯一调用方，当前原source/T/fullraw才守权限，不能从updated JSON授信。
# 函数用途: 拒绝私卡错op/改输入/非原类型结果，保原未知错误更新和实际决定后的完成投影。
def _require_private_approval_reconciliation(source, expected_receipt, updated):
    from .control_operation_service import GatewayControlOperationReceipt
    from .private_approval_control import _KIND
    if (type(expected_receipt) is not GatewayControlOperationReceipt
            or type(updated) is not GatewayControlOperationReceipt
            or expected_receipt.command_kind != _KIND or expected_receipt.private_approval is None
            or expected_receipt.dispatch is not None or expected_receipt.state not in {'executing', 'terminal_unknown'}
            or updated.state not in {'completed', 'terminal_unknown'}
            or _control_write_binding(expected_receipt) != _control_write_binding(updated)
            or expected_receipt.conversation_thread_id != updated.conversation_thread_id
            or type(source) is _LocalControlWriteSource and source.http_origin is not None):
        raise LocalControlCoordinatorUnavailable('control original private reconciliation is unavailable')
    _require_receipt_origin(source, expected_receipt)


# LLM: 只有唯一原local steer对账callee非None结果从该固定入口提交；expected与typed结果只是原资料，同source/T/lease/raw门才是写边界。
# 函数用途: 将已有插话投递事实写回同一回执，不重新执行插话或泛开普通unknown终态。
def _commit_original_steer_reconciliation(source, expected_receipt, result):
    _require_local_steer_reconciliation(source, expected_receipt, result)
    return _commit_original_control_receipt(source, expected_receipt.to_dict(),
        reconciliation=(expected_receipt, result))


# LLM: 只接受原无HTTP本机source及唯一原插话对账资料；不借model/私卡来源，结果三态不把wrapper completed当投递成功。
# 函数用途: 拒绝错类型、错op/owner、非插话及非本机对账，复制或退休来源仍由原检查拒绝。
def _require_local_steer_reconciliation(source, expected_receipt, result):
    from ..conversation.control_commands import ConversationControlResult
    from .control_operation_service import GatewayControlOperationReceipt

    if (type(source) is not _LocalControlWriteSource or source.http_origin is not None
            or type(expected_receipt) is not GatewayControlOperationReceipt
            or expected_receipt.command_kind != 'steer' or expected_receipt.dispatch is not None
            or type(result) is not ConversationControlResult or result.kind != 'steer'
            or result.delivery_status not in {'accepted', 'rejected', 'unknown'}):
        raise LocalControlCoordinatorUnavailable('control original steer reconciliation is unavailable')
    _require_receipt_origin(source, expected_receipt)


# LLM: 完整controller原launch/raw增量不变；只有原factory捕获的ready lease-only走固定receipt叶，不把缺controller开放给perform/cold/client或任意callback。
# 函数用途: 在各自原生命周期短门保存唯一回执，缺源和等待期间换引用仍拒绝。
def _commit_original_control_receipt(source, payload, *, reconciliation=None):
    if type(source.http_origin) is _ClientMemoryHTTPFacts:
        raise LocalControlCoordinatorUnavailable('client memory has no control receipt writer')
    source._raise_retained_native_failure()
    from . import control_operation_service as ops

    receipt = ops.GatewayControlOperationReceipt.from_dict(payload)
    lease = source.lease
    _require_receipt_origin(source, receipt)
    if _ready_receipt_source(source):
        with lease._lifecycle_gate:
            _require_ready_receipt_window(source, receipt)
            return _write_original_control_receipt(source,
                ops.gateway_control_operation_path(source.paths, receipt.operation_id), payload,
                **({} if reconciliation is None else {'reconciliation': reconciliation}))
    if source.controller is None:
        raise LocalControlCoordinatorUnavailable('control original receipt controller is unavailable')
    result = []

    # LLM: 只在原owner/controller/lease短门同步运行固定回执叶；JSON锁/完整raw与来源末核均在叶内。
    # 函数用途: 提交本次原回执，不执行领域或任意路径操作。
    def commit():
        gate = source.http_origin[9]._gate if source.http_origin is not None else source.controller._mutex
        if not gate.acquire(blocking=False):
            raise BlockingIOError('control HTTP resource gate is busy')
        try:
            result.append(_write_original_control_receipt(source,
                ops.gateway_control_operation_path(source.paths, receipt.operation_id), payload,
                **({} if reconciliation is None else {'reconciliation': reconciliation})))
        finally:
            gate.release()

    decision = source.controller.launch_owner_writer(owner_digest(source.receipt_owner), commit)
    if decision.get('status') != 'started' or len(result) != 1:
        error = LocalControlCoordinatorUnavailable('control coordinator generation is unavailable')
        if _original_http_failure_producer(source):
            source._native_source_failure = error
        raise error
    return result[0]


# LLM: 原JSON锁/codec/fullraw状态图与首异常顺序保持；完整controller仍原三增量门，固定ready来源只在原lease短门读回，不形成P2/v2或关闭证明。
# 函数用途: 沿固定回执路径保存完整状态，lease-only来源不能变成泛writer或恢复许可。
def _write_original_control_receipt(source, path, payload, *, reconciliation=None):
    from ..common.json_io import (
        locked_json_path,
        write_private_json_file_atomic_no_newline_unlocked,
    )
    from . import control_operation_service as ops
    from .multiprocess_control_forward import read_control_operation_raw
    from .multiprocess_takeover_runtime import (
        remember_current_receipt_write,
        require_current_receipt_window,
        require_current_receipt_write,
    )

    receipt = ops.GatewayControlOperationReceipt.from_dict(payload)
    deadline = getattr(source, 'deadline', None)
    ready = _ready_receipt_source(source)
    if ready:
        _require_ready_receipt_window(source, receipt)
        if path != ops.gateway_control_operation_path(source.paths, receipt.operation_id):
            raise LocalControlCoordinatorUnavailable('control original receipt path is unavailable')
    else:
        require_current_receipt_window(source, path, payload)
    with locked_json_path(path, blocking=False):
        _require_receipt_origin(source, receipt)
        before = read_control_operation_raw(source.paths, receipt.operation_id, deadline=deadline)
        if (type(source) is _LocalControlWriteSource and source.http_origin is not None
                and source._receipt_raw != before):
            error = LocalControlCoordinatorUnavailable('control receipt original raw changed')
            if _original_http_failure_producer(source):
                source._native_source_failure = error
            raise error
        if not ready:
            require_current_receipt_write(source, path, payload, before)
        current = ops.GatewayControlOperationReceipt.from_dict(json.loads(before)) if before is not None else None
        previous = current.state if current is not None else None
        if reconciliation is not None:
            expected_receipt, result = reconciliation
            if type(result) is ops.GatewayControlOperationReceipt:
                _require_private_approval_reconciliation(source, expected_receipt, result)
            else:
                _require_local_steer_reconciliation(source, expected_receipt, result)
            if current != expected_receipt:
                raise LocalControlCoordinatorUnavailable('control original reconciliation receipt changed')
            receipt = (result if type(result) is ops.GatewayControlOperationReceipt else
                replace(expected_receipt, state='completed', result=result.to_dict(), error={}, updated_at=time.time()))
            payload = receipt.to_dict()
        if (current is not None and _control_write_binding(current) != _control_write_binding(receipt)
                or reconciliation is None and (previous, receipt.state) not in {(None, 'prepared'), ('prepared', 'executing'),
                    ('executing', 'completed'), ('executing', 'terminal_unknown')}
                and not (previous == receipt.state == 'completed' and receipt.command_kind == 'steer')
                or type(source) is _LocalControlWriteSource and source.http_origin is not None
                and (source._receipt_raw != before or source._receipt_state != previous)):
            error = LocalControlCoordinatorUnavailable('control receipt original raw changed')
            if type(source) is _LocalControlWriteSource and _original_http_failure_producer(source):
                source._native_source_failure = error
            raise error
        _require_receipt_origin(source, receipt)
        if ready:
            _require_ready_receipt_window(source, receipt)
        write_private_json_file_atomic_no_newline_unlocked(path, payload, sort_keys=True, deadline=deadline)
        after = read_control_operation_raw(source.paths, receipt.operation_id, deadline=deadline)
        if ready:
            _confirm_ready_receipt_write(source, path, payload, after)
        else:
            remember_current_receipt_write(source, path, payload, before, after)
        if type(source) is _LocalControlWriteSource:
            source._receipt_raw, source._receipt_state = after, receipt.state
        return receipt


# LLM: 只分类原构造捕获的None形态，不授写权；实际Agent/原lease/owner等全部由固定窗口重核，full来源晚None不降级。
# 函数用途: 给固定回执入口识别已有ready组合，不作为通用perform的兜底。
def _ready_receipt_source(source):
    return (type(source) is _LocalControlWriteSource and source.http_origin is None
            and getattr(source, '_receipt_controller_origin', source) is None)


# LLM: 原factory/T活栈的ready Agent只可在同lease门保存自身receipt；不创建controller/增量，安装协议并未被此门串行化，每末叶只核可见原引用。
# 函数用途: 拒绝假Agent、跨owner、失效pin/租约或等待期间换成control-plane的迟到回执写。
def _require_ready_receipt_window(source, receipt):
    from ..core import SimpleAgent
    from ..user_space.owner_resolver import owner_identity_from_config, resolve_owner_home

    if (not _ready_receipt_source(source) or not isinstance(source.agent, SimpleAgent)
            or source.controller is not None or source.lease.controller is not None
            or source.context is not source.paths or source.lease.control_plane is not None
            or _coordinator_context_paths(source.agent, source.paths) is not source.paths
            or source.paths.root != source.lease.root or not source.lease.c_run_id
            or not source.lease._lifecycle_gate._is_owned()
            or source.pin.scope_key != (source.lease.c_run_id, 'local-control', receipt.operation_id)
            or owner_identity_from_config(source.agent.config) != source.receipt_owner):
        raise LocalControlCoordinatorUnavailable('control ready receipt source is unavailable')
    home = source.agent.home_paths
    resolved = resolve_owner_home(home.root, source.receipt_owner)
    if (home.owner_provider != source.receipt_owner.provider or home.owner_kind != source.receipt_owner.owner_kind
            or home.owner_id != resolved.owner_id or home.owner_home_dir != resolved.home_dir):
        raise LocalControlCoordinatorUnavailable('control ready receipt owner is unavailable')
    _require_receipt_origin(source, receipt)


# LLM: 仅固定ready写叶成功后核同source/规范path/完整序列化及nofollow读回，不记bootstrap增量或赋予P2；异常保原partial，不补终态。
# 函数用途: 确认ready组合自己的回执字节已实际发表，不能用JSON自称成功。
def _confirm_ready_receipt_write(source, path, payload, after):
    from ..common.json_io import serialize_private_json_no_newline
    from ..common.nofollow_fs import read_bytes_beneath
    from .control_operation_service import GatewayControlOperationReceipt

    _require_ready_receipt_window(source, GatewayControlOperationReceipt.from_dict(payload))
    if (after != serialize_private_json_no_newline(payload, sort_keys=True).encode('utf-8')
            or read_bytes_beneath(source.paths.root, tuple(path.relative_to(source.paths.root).parts),
                                  max_bytes=4 * 1024 * 1024, require_dir_fd=True) != after):
        raise LocalControlCoordinatorUnavailable('control ready receipt readback changed')
