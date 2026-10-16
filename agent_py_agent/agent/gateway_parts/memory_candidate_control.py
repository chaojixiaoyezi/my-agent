# LLM: 人工记忆控制只认当前认证入口的活封印；沿原owner、HostActor、候选服务和持久control，不向模型注册。
# 模块用途: 让本人在IM或终端查看并准确确认记忆候选，普通/memory搜索仍走原入口。
import re
from contextlib import contextmanager
from dataclasses import dataclass, replace
from types import SimpleNamespace

from ..conversation.control_commands import ConversationControlCommand, ConversationControlResult

USAGE = ('/memory candidates [list [起始序号]|show <候选ID>|preview <候选ID>|'
         'confirm|reject|cancel|receipt <预览编号>]；AGENTS使用/agents，Skill使用/skills。')
_SEAL = object()
_ID = re.compile(r'[A-Za-z0-9_.:-]{1,160}')
_NONCE = re.compile(r'gwctl-msg-[0-9a-f]{32}')


# LLM: 封印只在当前HTTP/direct栈存活且最多传给原control首次执行；持久JSON不能恢复用户当前请求。
# 类用途: 绑定实际用户、通道、会话、消息与精确命令，离开入口立即失效。
@dataclass
class _MemoryIngress:
    identity: tuple
    raw: str
    request_id: str
    seal: object = _SEAL
    active: bool = True
    used: bool = False


# LLM: permission只来自原HTTP认证结果，缺失不签权；不读模型confirmed/source_quote字段。
# 函数用途: 为当前本人控制提供临时封印，结束时使复制对象失效。
@contextmanager
def memory_http_ingress(scope, command, permission):
    evidence = (_MemoryIngress((scope.user_id, scope.channel, scope.conversation_id), command.value,
                str(scope.metadata['message_id'])) if permission is not None and scope.metadata.get('message_id') else None)
    try:
        yield replace(scope, memory_ingress=evidence)
    finally:
        if evidence is not None:
            evidence.active = False


# LLM: 只有control首次副作用调用可以透传封印；重放返回旧回执，不重新授权。
# 函数用途: 复核持久重建的scope与当前入口精确相同，再将封印消费一次。
def bind_memory_effect_scope(original, restored, command):
    evidence = original.memory_ingress
    if (type(evidence) is _MemoryIngress and evidence.seal is _SEAL and evidence.active and not evidence.used
            and evidence.identity == (restored.user_id, restored.channel, restored.conversation_id)
            and evidence.raw == command.value and evidence.request_id == str(restored.metadata.get('message_id') or '')):
        evidence.used = True
        return replace(restored, memory_ingress=evidence)
    return restored


# LLM: 只解析显式candidates子树，编号必须完整；普通查询正文不能变成审核或批准。
# 函数用途: 将候选列表、完整预览和nonce动作解析成共享控制对象。
def parse_memory_candidate_command(value):
    tokens = str(value or '').split()
    operation, argument, valid = 'list', '', False
    if tokens == ['candidates']:
        valid = True
    elif len(tokens) in {2, 3} and tokens[:2] == ['candidates', 'list']:
        argument = tokens[2] if len(tokens) == 3 else ''
        valid = not argument or (argument.isascii() and argument.isdecimal() and len(argument) <= 8)
    elif len(tokens) == 3 and tokens[0] == 'candidates':
        operation, argument = tokens[1:]
        validator = (_ID if operation in {'show', 'preview'} else
                     _NONCE if operation in {'confirm', 'reject', 'cancel', 'receipt'} else None)
        valid = validator is not None and validator.fullmatch(argument) is not None
    return ConversationControlCommand('memory', value=' '.join(tokens), operation=operation,
                                      target_id=argument, valid=valid, usage=USAGE)


# LLM: 普通owner控制不要求全机admin；owner只沿原认证路由，拒绝模型/旧封印及跨owner参数。
# 函数用途: 沿原持久控制执行一次候选操作，错误只返回固定原因码。
def execute_memory_candidate_control(base, paths, command, scope):
    evidence = scope.memory_ingress
    if (type(evidence) is not _MemoryIngress or evidence.seal is not _SEAL or not evidence.active
            or not evidence.used or evidence.identity != (scope.user_id, scope.channel, scope.conversation_id)
            or evidence.raw != command.value):
        return memory_control_failure('MEMORY_CONTROL_NOT_USER_REQUEST')
    try:
        from ..user_space.group_authorization_context import group_capability_guard
        from ..user_space.owner_policy import resolve_effective_owner_policy
        from .control_service import resolve_gateway_scope_owner
        from .memory_candidate_review import run_memory_candidate_control
        from .model_profile_service import _scoped_model_host
        from .request_worker import _resolve_request_agent_for_owner

        policy_host, thread = _scoped_model_host(base, scope)
        owner = resolve_gateway_scope_owner(base, scope)
        loaded = _resolve_request_agent_for_owner(base, owner)
        if loaded.home_paths.owner_id != policy_host.home_paths.owner_id:
            return memory_control_failure('MEMORY_CONTROL_OWNER_MISMATCH')
        policy = resolve_effective_owner_policy(policy_host.home_paths)
        if not policy.memory_enabled or 'remember' in policy.disabled_tools:
            return memory_control_failure('MEMORY_CONTROL_DISABLED')
        # 控制宿主复制原服务引用，绝不把群成员身份写进共享Agent的模型运行上下文。
        host = SimpleNamespace(**vars(loaded))
        host.home_paths = policy_host.home_paths
        with group_capability_guard(policy_host, 'assistant.configure', thread_id=thread.thread_id):
            return run_memory_candidate_control(host, paths, command, scope, thread.thread_id)
    except Exception as error:
        code = getattr(error, 'error_code', '')
        return memory_control_failure(code if isinstance(code, str) and code.startswith('GROUP_AUTHORIZATION_')
                                      else 'MEMORY_CONTROL_UNAVAILABLE')


# LLM: direct只由终端真实命令分支调用，不注册模型工具；原持久control-once仍是唯一重放账。
# 函数用途: 无Gateway终端沿同一预览确认服务处理，所有路径来自当前owner。
def execute_memory_candidate_direct(agent, command, session_id, message_id):
    from ..user_space.owner_resolver import owner_identity_from_config
    from .control_operation_service import execute_gateway_control_operation
    from .control_service import GatewayControlScope
    from .paths import gateway_paths_from_root

    if not session_id or not message_id:
        return memory_control_failure('MEMORY_CONTROL_REQUEST_ID_REQUIRED')
    scope = GatewayControlScope('local-agent', 'chat', session_id, {'message_id': message_id},
                                resolved_owner=owner_identity_from_config(agent.config))
    paths = gateway_paths_from_root(agent.home_paths.owner_home_dir / 'gateway')
    with memory_http_ingress(scope, command, object()) as issued:
        receipt = execute_gateway_control_operation(agent, paths, command, issued,
                                                    command_text='/memory ' + command.value)
    result = receipt.result
    return ConversationControlResult('memory', result.get('ok', False), result.get('message', ''),
        error_code=result.get('error_code', ''), details=result.get('details', {}),
        delivery_status=result.get('delivery_status', 'accepted'))


# LLM: 异常不泄露候选正文、私有路径或原异常信息；拒绝不转普通模型任务。
# 函数用途: 提供稳定错误码和人工重新查看提示，不阻断后续正常聊天。
def memory_control_failure(code):
    return ConversationControlResult('memory', False, '记忆候选操作未完成（' + code
        + '）；请用 /memory candidates 查看。', error_code=code, delivery_status='rejected')
