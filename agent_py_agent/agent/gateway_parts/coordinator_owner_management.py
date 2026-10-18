# LLM: 原cold source首native只在正常出口/固定complete前重抛；actual physical/common异常保持对象/cause/context，不能finally抢抛或回滚耐久completed。
# LLM: 冷管理唯一真实HTTP producer复用原C写源、原operation T及receipt，agents/memory分派不创建业务Agent。
# 模块用途: 在同一次认证管理调用的活栈内运行原领域服务，并沿唯一回执和事件生命周期完成。
import time
from contextlib import ExitStack, contextmanager
from dataclasses import replace

from ..backends.bounded_call import BoundedCallExitUnconfirmedError
from ..backends.request_scope import _DEADLINE
from ..common.global_write_commit import GlobalWriteCommitUnavailable
from ..common.global_write_guard import GlobalWriteInWorkerError
from ..common.request_execution_ports import (
    RequestExecutionContextUnavailable,
    RequestExecutionOutcomeUncertainError,
)
from ..common.tool_write_context import ToolWriteContextUnavailable
from ..conversation.control_commands import ConversationControlResult
from ..runtime_errors import runtime_error_report
from .multiprocess_global_write import GlobalWriteError
from .multiprocess_local_control import LocalControlCoordinatorUnavailable, _LocalControlWriteSource
from .owner_management_control import OwnerManagementIngressUnavailable

MANAGEMENT_SOURCE_ERRORS = (
    RequestExecutionContextUnavailable, RequestExecutionOutcomeUncertainError,
    BoundedCallExitUnconfirmedError, ToolWriteContextUnavailable, GlobalWriteCommitUnavailable,
    GlobalWriteError, GlobalWriteInWorkerError, LocalControlCoordinatorUnavailable, OwnerManagementIngressUnavailable,
)


# LLM: 仅实际cold正count HTTP/work/G1捕获；原parser/owner/identity复用，资源deadline有上游则同值，无则None，不新设业务总时限。
# 函数用途: 冻结本次管理输入所需的原对象，不加载业务Agent、创建owner目录或写回执。
def capture_owner_management_http(handler, server, body):
    from .control_plane import GatewayControlPlane
    from .coordinator_control_transport import _request_input
    from .http_service import GatewayHTTPHandler, GatewayHTTPServer
    from .multiprocess_coordinator import coordinator_for_root
    from .multiprocess_http_transport import capture_http_handler_work
    from .owner_management_control import is_owner_management_control

    if (not isinstance(handler, GatewayHTTPHandler) or type(server) is not GatewayHTTPServer
            or type(server.control_plane) is not GatewayControlPlane or server.agent is not None
            or server.control_plane.worker_count <= 0):
        raise LocalControlCoordinatorUnavailable('control actual HTTP source is required')
    try:
        work = capture_http_handler_work(handler, server)
    except ValueError as error:
        raise LocalControlCoordinatorUnavailable('control actual HTTP source is unavailable') from error
    control, paths = server.control_plane, server.paths
    lease, controller = coordinator_for_root(paths.root, context=control), control.controller()
    command, text, scope = _request_input(handler, server, body)
    middleware = handler._auth_middleware
    if (lease is None or controller is None or command is None or not command.valid
            or not is_owner_management_control(command.kind) or middleware is None
            or not middleware.has_gateway_credential(dict(handler.headers))):
        raise LocalControlCoordinatorUnavailable('control actual HTTP source is unavailable')
    return command, text, scope, work, lease, controller, _DEADLINE.get()


# LLM: 原T在Root外只try，fresh才发行一次source；正常yield出口核首native，当前typed同对象穿出，finally不抢异常或补账；领域/网络仍门外。
# 函数用途: 为后续真实管理服务提供(source, receipt, 原scope)三元组；重放或锁忙时source和scope为空，不再执行。
@contextmanager
def coordinator_owner_management_operation(handler, server, body):
    from . import control_operation_service as ops
    from .owner_management_control import claim_management_effect

    with _LocalControlWriteSource.http_management_scope(handler, server, body) as (source, observed):
        if source is None:
            yield None, observed, None
            return
        command, text, scope = source.http_origin[3:6]
        receipt = source.receipt
        ops._write_control_operation_json(ops.gateway_control_operation_path(source.paths, receipt.operation_id),
                                         receipt.to_dict(), owner_management_source=source)
        executing = replace(receipt, state='executing', updated_at=time.time())
        ops._write_control_operation_json(ops.gateway_control_operation_path(source.paths, receipt.operation_id),
                                         executing.to_dict(), owner_management_source=source)
        claim_management_effect(source.http_origin[7], command, scope)
        try:
            yield source, executing, scope
            source._raise_retained_native_failure()
            if source._receipt_state == 'executing':
                raise RuntimeError('control management domain did not return a result')
        except MANAGEMENT_SOURCE_ERRORS:
            raise
        except Exception as error:
            if source._receipt_state == 'executing':
                uncertain = replace(executing, state='terminal_unknown',
                    error=runtime_error_report(error, context='gateway.control_operation.execute'), updated_at=time.time())
                try:
                    ops._write_control_operation_json(ops.gateway_control_operation_path(source.paths, receipt.operation_id),
                                                     uncertain.to_dict(), owner_management_source=source)
                except MANAGEMENT_SOURCE_ERRORS:
                    raise
                except Exception as cleanup_error:
                    error.add_note('control_management_unknown_write_failed:' + type(cleanup_error).__name__)
            raise


# LLM: exact原source确认后先只读抛首native再验证result；同活栈/executing才收口，已completed不回滚，固定port不接路径/op_id。
# 函数用途: 将真实领域结果保存为本次completed；不向旧command_completed传入伪Agent或为观察事件创建线程。
def complete_owner_management_operation(source, result, *, conversation_thread_id=''):
    from . import control_operation_service as ops

    if type(source) is not _LocalControlWriteSource:
        raise LocalControlCoordinatorUnavailable('control original domain result is unavailable')
    source._raise_retained_native_failure()
    if type(result) is not ConversationControlResult:
        raise LocalControlCoordinatorUnavailable('control original domain result is unavailable')
    if result.kind != source.receipt.command_kind:
        raise LocalControlCoordinatorUnavailable('control original domain result is unavailable')
    source._require_current()
    current = ops.read_gateway_control_operation(source.paths, source.receipt.operation_id, read_only=True)
    completed = replace(current, state='completed', result=result.to_dict(), error={}, updated_at=time.time(),
                        conversation_thread_id=conversation_thread_id or current.conversation_thread_id)
    ops._write_control_operation_json(ops.gateway_control_operation_path(source.paths, current.operation_id),
                                     completed.to_dict(), owner_management_source=source)
    return completed


# LLM: 仅原agents/memory管理分派；同活source和唯一completed/event路径，memory借用及global scope保留至完成，重放不重发seal。
# 函数用途: 让已接妥原写叶的规则与候选审核共用HTTP控制回执，领域拒绝保原码，未知不重投业务。
def execute_coordinator_owner_management(handler, server, body):
    command, _text, _scope, *_facts = capture_owner_management_http(handler, server, body)
    if command.kind not in {'agents', 'memory'}:
        with _LocalControlWriteSource.http_management_scope(handler, server, body) as (_source, observed):
            return observed
    from ..capability._agents_contract_fields import AgentsContractError
    from ..user_space.group_authorization import GroupAuthorizationError
    from .agents_control_service import _failure
    from .coordinator_agents_control import coordinator_agents_thread_id, execute_coordinator_agents
    from .coordinator_memory_candidates import execute_coordinator_memory_candidates
    from .memory_candidate_control import memory_control_failure

    with coordinator_owner_management_operation(handler, server, body) as (source, observed, scope):
        if source is None:
            return observed
        command = source.http_origin[3]
        with ExitStack() as domain_stack:
            if command.kind == 'agents':
                try:
                    result, thread_id = execute_coordinator_agents(source, command, scope)
                except (AgentsContractError, GroupAuthorizationError) as error:
                    result, thread_id = _failure(error.code), coordinator_agents_thread_id(source)
            else:
                try:
                    result, thread_id = domain_stack.enter_context(
                        execute_coordinator_memory_candidates(source, command, scope))
                except GroupAuthorizationError as error:
                    result, thread_id = memory_control_failure(error.code), coordinator_agents_thread_id(source)
            completed = complete_owner_management_operation(source, result, conversation_thread_id=thread_id)
            from .event_points import command_completed_from_resources
            from .owner_resources import resolve_owner_resource_configuration
            home, config, _paths = resolve_owner_resource_configuration(source.context, source.receipt_owner)
            command_completed_from_resources(config, home, completed, server=server)
            return completed
