# LLM: 原来源拒绝类型唯一定义在common.owner_write_errors；此公开位置只导入同类，producer/首槽/原因和传播不变。
# LLM: 通用管理只消费首次认证HTTP/当前真实Permission与原receipt；冷host纯解析，目录/seed仅同target原短门初始化，不依赖领域issuer，不物化Agent/序列化permit。
# 模块用途: 绑定冷owner宿主与当前HTTP管理来源，在首次合法管理短门内初始化目录和种子，让运输与业务各守唯一权威。
import os
import threading
from contextlib import contextmanager
from types import SimpleNamespace

from ..auth.models import Permission, Role
from ..common.owner_write_errors import (
    OwnerManagementIngressUnavailable as OwnerManagementIngressUnavailable,
)
from ..user_space.owner_resolver import home_paths_with_owner, resolve_owner_home
from .multiprocess_execution_lifetime import ExecutionScopeLifetime

C_OWNER_MANAGEMENT_KINDS = frozenset({'agents', 'memory'})
_SEAL = object()




# LLM: 注册只决定C管理职责；不能从命令正文、JSON角色或冷宿主取得执行许可。
# 函数用途: 判断既有用户确认控制是否留在原C调用栈，避免持久JSON重建短期HTTP授权。
def is_owner_management_control(kind):
    return kind in C_OWNER_MANAGEMENT_KINDS


# LLM: 只用规范owner解析/HomePaths和现有config，零模型/会话/工具初始化；路径投影本身不授权。
# 函数用途: 为管理操作取得正确用户的冷目录与配置宿主。
def cold_owner_host(base, owner):
    resolved = resolve_owner_home(base.home_paths.root, owner)
    return SimpleNamespace(home_paths=home_paths_with_owner(base.home_paths, resolved), config=base.config)


# LLM: 身份来自规范HomePaths且再经原resolver比对；local/user只认owners/local，不接受路径替代命名空间。
# 函数用途: 从已解析的冷home取得原P2身份，兼顾本地普通用户而不授管理权。
def cold_home_identity(home):
    parts = home.owner_id.split('/')
    from ..user_space.owner_resolver import OwnerIdentity
    if parts == ['local', 'main']:
        identity = OwnerIdentity.local_main()
    elif len(parts) == 2 and parts[0] == 'local':
        identity = OwnerIdentity.local_user(parts[1])
        if identity.owner_id != parts[1]:
            raise OwnerManagementIngressUnavailable('owner')
    elif len(parts) == 4 and parts[0] == 'providers' and parts[2] in {'users', 'groups'}:
        constructor = OwnerIdentity.provider_user if parts[2] == 'users' else OwnerIdentity.provider_group
        identity = constructor(parts[1], parts[3])
    else:
        raise OwnerManagementIngressUnavailable('owner')
    resolved = resolve_owner_home(home.root, identity)
    if (home.owner_provider != identity.provider or home.owner_kind != identity.owner_kind
            or home.owner_home_dir != resolved.home_dir):
        raise OwnerManagementIngressUnavailable('owner')
    return identity


# LLM: 认证factory的共享lifetime绑定唯一原interval；exact self/PID/身份/raw/message同时核，复制字段不能重设origin续权。
# 类用途: 保存本次HTTP管理调用的身份来源，首次effect才发行业务入口凭证。
class _ManagementIngress:
    # LLM: 构造保存原HTTP事实，不签nonce、不读写owner文件；seal只由下面原认证入口传入。
    # 函数用途: 固定当前调用与原管理命令的精确关系。
    def __init__(self, command, scope, seal, handler):
        self.origin, self.pid, self.active, self.seal = self, os.getpid(), True, seal
        self.thread = threading.get_ident()
        self.command, self.scope = command, scope
        self.identity = (scope.user_id, scope.channel, scope.conversation_id)
        self.raw, self.kind = command.value, command.kind
        self.request_id = scope.metadata.get('message_id')
        self.used, self.evidence = False, None
        self.handler = handler
        self.middleware = handler._auth_middleware
        self._handler_origin, self._middleware_origin = handler, self.middleware
        self.lifetime = ExecutionScopeLifetime()
        self.lifetime.bind_original(self)
        self._lifetime_origin = self.lifetime

    # LLM: 只能核本interval与原prepared回执恢复出的同一身份；不从fresh scope续许可。
    # 函数用途: 拒绝复制、进程继承、已退出及错命令/会话的入口。
    def require(self, command, scope):
        try:
            if type(self.lifetime) is not ExecutionScopeLifetime or self.lifetime is not self._lifetime_origin:
                raise ValueError("management_lifetime_unavailable")
            self.lifetime.require_original(self)
        except ValueError as error:
            raise OwnerManagementIngressUnavailable("lifetime") from error
        if (type(self) is not _ManagementIngress or self.origin is not self or self.seal is not _SEAL
                or not self.active or self.pid != os.getpid() or self.thread != threading.get_ident()
                or self.command is not command or self.scope is not scope
                or self.identity != (scope.user_id, scope.channel, scope.conversation_id)
                or self.kind != command.kind or self.raw != command.value
                or self.request_id != scope.metadata.get('message_id')):
            raise OwnerManagementIngressUnavailable('binding')

    # LLM: 当前权限只能由同一原HTTP/middleware重新只读核验，不能用入口快照或JSON角色替代。
    # 函数用途: 在实际提交前读取本次认证用户当前的真实权限。
    def current_permission(self, command, scope):
        self.require(command, scope)
        if (self.handler is not self._handler_origin or self.middleware is not self._middleware_origin
                or getattr(self.handler, '_auth_middleware', None) is not self.middleware):
            raise OwnerManagementIngressUnavailable('authentication')
        from .http_handlers import _request_identity
        user, permission = _request_identity(self.handler)
        if (type(permission) is not Permission or user != self.identity[0]
                or permission.user_id != user or self.identity[1] not in permission.channels):
            raise OwnerManagementIngressUnavailable('authentication')
        return permission

    # LLM: 原终态/GET不调用这里；只消费当次身份一次，领域issuer由受信分派入口另行调用。
    # 函数用途: 标记本次首次effect已领用，通用运输不依赖任何管理业务。
    def claim_effect(self, command, scope):
        self.current_permission(command, scope)
        if self.used:
            raise OwnerManagementIngressUnavailable('consumed')
        self.used = True


# LLM: 身份只从原认证middleware/request_identity取得；管理kind缺认证即拒，普通控制不增加issuer或I/O。
# 函数用途: 在原HTTP调用期间提供首次effect凭证，响应/异常后共享interval立即失效。
@contextmanager
def authenticated_management_request(handler, command, scope):
    if not is_owner_management_control(command.kind):
        yield None
        return
    from .http_handlers import _request_identity
    user, permission = _request_identity(handler)
    if (getattr(handler, '_auth_middleware', None) is None or type(permission) is not Permission
            or user != scope.user_id or permission.user_id != user or scope.channel not in permission.channels):
        raise OwnerManagementIngressUnavailable('authentication')
    if type(scope.metadata.get('message_id')) is not str or not scope.metadata['message_id'].strip():
        raise OwnerManagementIngressUnavailable('request_id')
    ingress = _ManagementIngress(command, scope, _SEAL, handler)
    try:
        yield ingress
    finally:
        ingress.active = False
        ingress.lifetime.close()


# LLM: 调用方不得用duck hook、子类或复制interval替代原HTTP来源；不从持久JSON恢复任何许可。
# 函数用途: 在首次业务effect以及业务入口消费时核验同一个活管理凭证。
def require_management_ingress(request, command, scope):
    if type(request) is not _ManagementIngress:
        raise OwnerManagementIngressUnavailable('binding')
    request.require(command, scope)


# LLM: 仅原控制执行锁内first-effect调用，原completed/GET没有此步骤；不会构造新issuer兼容缺来源。
# 函数用途: 原控制分派只领用通用身份一次，由业务consumer自己发行凭证。
def claim_management_effect(request, command, scope):
    require_management_ingress(request, command, scope)
    request.claim_effect(command, scope)


# LLM: 每次末写读当前Permission/policy；冷源显式control/config/home，不造Agent；同HTTP主体与target P2分开，默认真实Agent参数保持。
# 函数用途: 在管理写者末次提交前确认用户仍可操作原资源，不能沿入口Full Access快照续权。
def require_actor_resource_permission(base, actor, home, subject, *, config=None, home_root=None, control=None):
    if type(subject) is not tuple or len(subject) != 3:
        raise OwnerManagementIngressUnavailable('binding')
    request, command, scope = subject
    require_management_ingress(request, command, scope)
    permission = request.current_permission(command, scope)
    actor_identity, target_identity = cold_home_identity(actor.home_paths), cold_home_identity(home)
    if control is not None:
        from .control_plane import GatewayControlPlane
        if type(control) is not GatewayControlPlane or config is not control.config or home_root != control.home_root:
            raise OwnerManagementIngressUnavailable('binding')
        root, configuration = home_root, config
    else:
        root, configuration = base.home_paths.root, base.config
    if actor.home_paths.root != root or home.root != root:
        raise OwnerManagementIngressUnavailable('owner')
    from .control_service import resolve_gateway_scope_owner
    resolved_actor = scope.resolved_owner if control is not None else resolve_gateway_scope_owner(base, scope)
    if resolved_actor is None or actor_identity != resolved_actor:
        raise OwnerManagementIngressUnavailable('binding')
    if actor_identity == target_identity:
        return
    from ..user_space.owner_access import (
        is_complete_local_admin_owner,
        resolve_owner_scope_and_access,
    )
    from ..user_space.owner_policy import resolve_effective_owner_policy
    policy = resolve_effective_owner_policy(actor.home_paths)
    if any(error.get('context') == 'owner_policy.permissions' for error in policy.load_errors):
        raise OwnerManagementIngressUnavailable('cross_owner')
    _, access = resolve_owner_scope_and_access(actor.home_paths, configuration, policy)
    if not is_complete_local_admin_owner(actor.home_paths) or access != 'full-access' or permission.role is not Role.ADMIN:
        raise OwnerManagementIngressUnavailable('cross_owner')


# LLM: 只在首次已claimed/executing的原管理调用绑定规范资源；generic不读取领域issuer/evidence，原source保target P2/C/lease/current actor，Count0同认证末核。
# 函数用途: 给元数据管理返回原短提交入口，并在同一写门内初始化尚不存在的owner；直接入口不新增I/O。
def bind_management_resource(base, actor, home, subject, source):
    if source is not None:
        from .multiprocess_local_control import _LocalControlWriteSource
        if type(source) is not _LocalControlWriteSource:
            raise OwnerManagementIngressUnavailable('binding')
    writer = source.bind_resource_owner(home, actor=actor, subject=subject) if source is not None else None
    if subject is None:
        return writer
    request, command, scope = subject
    require_management_ingress(request, command, scope)
    if not request.used:
        raise OwnerManagementIngressUnavailable('consumed')
    if source is None:
        from ..common.executor_registration import managed_request_execution_required
        if managed_request_execution_required():
            raise OwnerManagementIngressUnavailable('binding')

    # LLM: scope只运输原请求，不签新授权；每leaf仍核同一规范target和当前真实Permission，业务operation异常原样传播。
    # 函数用途: 同原C callback或Count0认证门完整执行一次资源事务。
    def commit(operation, *, owner_home):
        if owner_home != home.owner_home_dir:
            raise OwnerManagementIngressUnavailable('owner')
        if writer is not None:
            return writer(operation, owner_home=owner_home)
        require_actor_resource_permission(base, actor, home, subject)
        return operation()

    commit(lambda: _initialize_missing_owner(home), owner_home=home.owner_home_dir)
    return commit


# LLM: 调用方已在原资源写门核同主体/target；missing检测与mkdir/seed不可拆出该门，已有owner不补种子、不修改正文，链接父链拒绝。
# 函数用途: 在首次合法冷管理时复用原owner初始化，不构造Agent、线程、模型或第二账本。
def _initialize_missing_owner(home):
    if home.owner_home_dir.is_dir():
        return
    from ..common.nofollow_fs import open_directory_beneath
    from ..user_space.owner_resolver import ensure_owner_home
    identity = cold_home_identity(home)
    descriptor = open_directory_beneath(home.root, home.owner_home_dir.relative_to(home.root).parts,
                                        create=True, require_dir_fd=True)
    try:
        result = ensure_owner_home(home.root, identity)
        if result.home_dir != home.owner_home_dir or result.owner_id != home.owner_id:
            raise OwnerManagementIngressUnavailable('owner')
    finally:
        os.close(descriptor)
