# LLM: 仅原实际管理HTTP source绑定同owner记忆；不发行第二权限、建Agent或接dispatcher，holder不留source/actor/callback。
# 模块用途: 为冷C候选审核提供固定记忆来源检查及同holder组合，领域业务和锁等待都在Root外。
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from functools import partial

from ..common.global_write_commit import global_write_commit_scope
from ..common.owner_write_errors import LocalControlCoordinatorUnavailable
from ..user_space.owner_policy import resolve_effective_owner_policy
from .coordinator_memory_resources import CoordinatorMemoryResources
from .memory_candidate_control import memory_control_ingress_valid


# LLM: 临时binding只属于原source栈；字段为原已消费scope/holder/home/evidence引用，不进入容器或持久JSON。
# 类用途: 在当前管理来源中固定候选审核的原资源和真人封印。
@dataclass(frozen=True, eq=False, repr=False)
class _ManagementMemoryBinding:
    scope: object
    holder: object
    home: object
    evidence: object


# LLM: 仅由source固定bind方法调用；原当前源先已核验，binding在actor thread/holder构造之前固定，不能从报文传holder或home。
# 函数用途: 绑定当前已领用的真人记忆入口与本人原资源容器。
def _bind_management_memory_ingress(source):
    from .multiprocess_local_control import _LocalControlWriteSource

    if (type(source) is not _LocalControlWriteSource or type(source.http_origin) is not tuple
            or source._management_memory_binding is not None or source._receipt_state != 'executing'
            or source.http_origin[3].kind != 'memory' or not source.http_origin[7].used):
        raise LocalControlCoordinatorUnavailable('control original memory binding is unavailable')
    from .memory_candidate_control import bind_memory_effect_scope, memory_http_ingress

    stack = source._management_memory_stack
    command, original = source.http_origin[3], source.http_origin[5]
    if type(stack) is not ExitStack or stack is not source._management_memory_stack_origin:
        raise LocalControlCoordinatorUnavailable('control original memory lifetime is unavailable')
    permission = source.http_origin[7].current_permission(command, original)
    issued = stack.enter_context(memory_http_ingress(original, command, permission))
    bound_scope = bind_memory_effect_scope(issued, original, command)
    if not memory_control_ingress_valid(bound_scope, command):
        raise LocalControlCoordinatorUnavailable('control original memory ingress is unavailable')
    holder = source.http_origin[1].coordinator_memory_resources
    if type(holder) is not CoordinatorMemoryResources or source._actor_control_resources is None:
        raise LocalControlCoordinatorUnavailable('control original memory resources are unavailable')
    home = source._actor_control_resources[1]
    binding = _ManagementMemoryBinding(bound_scope, holder, home, bound_scope.memory_ingress)
    source._management_memory_binding = source._management_memory_binding_origin = binding
    return bound_scope


# LLM: 固定current叶不反调source/current、不取holder Condition；只核原引用/封印与当前policy，原producer wrapper负责登记首native。
# 函数用途: 在Root末门内拒绝换容器、关闭、失活真人入口或当前记忆禁用后的新写。
def _require_management_memory_current(source):
    binding = source._management_memory_binding
    if binding is None:
        return
    if (type(binding) is not _ManagementMemoryBinding or binding is not source._management_memory_binding_origin
            or type(source._management_memory_stack) is not ExitStack
            or source._management_memory_stack is not source._management_memory_stack_origin
            or binding.scope.memory_ingress is not binding.evidence
            or not memory_control_ingress_valid(binding.scope, source.http_origin[3])):
        raise LocalControlCoordinatorUnavailable('control original memory ingress is unavailable')
    holder, home = binding.holder, binding.home
    control, runtime, paths = source.http_origin[8:11]
    if (type(holder) is not CoordinatorMemoryResources or holder._origin is not holder or holder._closing
            or source.http_origin[1].coordinator_memory_resources is not holder
            or any(a is not b for a, b in zip((holder.control, holder.runtime, holder.paths, holder.config),
                                             holder._references))
            or holder.control is not control or holder.runtime is not runtime or holder.paths is not paths
            or holder.config is not control.config or home is not source._actor_control_resources[1]
            or (source.resource_home is not None and source.resource_home is not home)):
        raise LocalControlCoordinatorUnavailable('control original memory resources are unavailable')
    from .owner_management_control import cold_home_identity

    policy = resolve_effective_owner_policy(home)
    if (cold_home_identity(home) != source.receipt_owner or home.root != control.home_root
            or policy.load_errors or not policy.memory_enabled or 'remember' in policy.disabled_tools):
        raise LocalControlCoordinatorUnavailable('control current memory policy is unavailable')


# LLM: 原HTTP producer先claimed/executing并在factory栈保seal至父unknown写完；这里只借同holder/原group/global scope，返回前不签completed/event或接dispatcher。
# 函数用途: 在真实管理来源内执行原七种候选动作，让将来的原完成写者仍处同一次活领域窗口。
@contextmanager
def execute_coordinator_memory_candidates(source, command, scope):
    from ..user_space.group_authorization_context import (
        bind_group_control_host,
        group_capability_guard,
    )
    from .coordinator_client_memory import (
        _assemble_owner_resources,
        refresh_owner_memory_quota,
    )
    from .owner_management_control import bind_management_resource

    source._require_current()
    if source.http_origin[3] is not command or source.http_origin[5] is not scope:
        raise LocalControlCoordinatorUnavailable('control original memory command is unavailable')
    bound_scope = source.bind_management_memory_ingress()
    host, thread = source.prepare_actor_control_thread()
    bind_group_control_host(host, host, scope, thread.thread_id)
    writer = bind_management_resource(None, host, host.home_paths, (source.http_origin[7], command, scope), source)
    binding = source._management_memory_binding
    with group_capability_guard(host, 'assistant.configure', thread_id=thread.thread_id, blocking=False,
                                birth_commit=partial(writer, owner_home=host.home_paths.owner_home_dir)):
        with global_write_commit_scope(source.perform):
            with binding.holder.borrow_current_owner(binding.home,
                    lambda: _assemble_owner_resources(source.context, source.receipt_owner)) as resources:
                source._require_current()
                refresh_owner_memory_quota(resources, resolve_effective_owner_policy(binding.home))
                result = _run_candidate_action(source, command, bound_scope, resources, thread.thread_id)
                source._require_current()
                yield result, thread.thread_id


# LLM: 原七动作只分派一次，preview端口自己持原第二T；真实domain只运输原引用，Promotion惰性仍在共享审核算法唯一位置。
# 函数用途: 在已借用的本人资源上调用原候选业务，普通预览争用保原业务拒绝码。
def _run_candidate_action(source, command, scope, resources, thread_id):
    from .memory_candidate_control import memory_control_failure
    from .memory_candidate_review import (
        MemoryCandidateServices,
        run_memory_candidate_control_from_services,
    )
    from .memory_preview_transaction import (
        MemoryPreviewTransactionRejected,
        memory_preview_transaction,
    )

    services = MemoryCandidateServices(resources.authorities.candidates, resources.promotion,
                                       source._management_memory_binding.home.owner_id, resources)
    if command.operation in {'list', 'show', 'preview'}:
        return run_memory_candidate_control_from_services(services, source.paths, command, scope, thread_id)
    try:
        with memory_preview_transaction(source, services, thread_id) as transaction:
            return run_memory_candidate_control_from_services(services, source.paths, command, scope,
                thread_id, preview_transaction=transaction)
    except MemoryPreviewTransactionRejected as error:
        return memory_control_failure(error.code)
