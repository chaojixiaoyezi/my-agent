# LLM: AGENTS控制只认原HTTP认证入口的短期内存封印或direct终端输入；持久控制回执只定位结果，不恢复授权。
# 模块用途: 沿现有公共控制链提供同owner的规则来源、预览与一次确认，保留原HostActor和群配置租约。
import json
from contextlib import contextmanager
from dataclasses import dataclass, replace
from hashlib import sha256
from types import SimpleNamespace

from ..capability._agents_contract_fields import AgentsContractError
from ..capability.agents_source_host import build_agents_source_repository
from ..capability.agents_write_service import (
    USAGE,
    AgentsWriteService,
    _direct_request,
    _raise_agents_runtime_error,
    parse_agents_value,
)
from ..conversation.control_commands import ConversationControlCommand, ConversationControlResult
from ..plugin_host.errors import HostAPIError
from ..plugin_host.owner_context import validate_local_user_id
from ..user_space.local_user_store import LocalUserStore
from ..user_space.owner_access import is_complete_local_admin_owner, resolve_owner_scope_and_access

_INGRESS_SEAL = object()

# LLM: 只在同一次已认证HTTP栈内存活，退出立即失效；身份与命令不能从JSON复制，最多绑定一次副作用。
# 类用途: 将当前用户的实际输入与原持久控制首次执行绑定。
@dataclass
class _AgentsIngress:
    identity: tuple
    raw: str
    request_id: str
    seal: object
    active: bool = True
    used: bool = False

# LLM: 仅由HTTP私有入口调用，permission是原认证结果；缺认证或消息号不签权，原控制仍可返回结构化拒绝。
# 函数用途: 为一次直接用户AGENTS控制提供内存封印，结束后使所有复制对象失效。
@contextmanager
def agents_http_ingress(scope, command, permission):
    evidence = None
    if permission is not None and scope.metadata.get('message_id'):
        evidence = _AgentsIngress((scope.user_id, scope.channel, scope.conversation_id), command.value,
                                  str(scope.metadata['message_id']), _INGRESS_SEAL)
    try:
        yield replace(scope, agents_ingress=evidence)
    finally:
        if evidence is not None:
            evidence.active = False

# LLM: 只有原C锁首次执行可把HTTP封印传给receipt重建的scope；重放和status读口不调用本入口。
# 函数用途: 保留原控制幂等状态机，同时拒绝由持久JSON重建AGENTS授权。
def bind_agents_effect_scope(original, restored, command):
    evidence = original.agents_ingress
    if (type(evidence) is not _AgentsIngress or evidence.seal is not _INGRESS_SEAL
            or not evidence.active or evidence.used
            or evidence.identity != (restored.user_id, restored.channel, restored.conversation_id)
            or evidence.raw != command.value
            or evidence.request_id != str(restored.metadata.get('message_id') or '')):
        return restored
    evidence.used = True
    return replace(restored, agents_ingress=evidence)

# LLM: 冷宿主绑定规范owner，普通local用户沿已有LocalUserStore纯路径投影；不构造模型、创建账号或目录。
# 函数用途: 为明确管理员目标提供现有owner的来源服务依赖。
def cold_owner_host(base, identity):
    from ..user_space.owner_resolver import home_paths_with_owner, resolve_owner_home
    from .owner_conversation_store import owner_conversation_store
    owner = (LocalUserStore(base.home_paths.root).owner_home(identity.owner_id)
             if identity.provider == 'local' and identity.owner_kind == 'user'
             else resolve_owner_home(base.home_paths.root, identity))
    home = home_paths_with_owner(base.home_paths, owner)
    if not home.owner_home_dir.is_dir():
        raise AgentsContractError('AGENTS_SOURCE_OWNER_UNAVAILABLE', 'owner')
    return SimpleNamespace(home_paths=home, config=base.config,
                           conversation_store=owner_conversation_store(base, home),
                           delivery_service=getattr(base, 'delivery_service', None))

# LLM: 默认scope/旧封印不能签发修改权；群Actor沿原冷host绑定；底层原源拒绝/运输未结不得洗成业务失败。
# 函数用途: 只将原认证用户的实际AGENTS控制交给唯一预览确认服务。
def execute_agents_control(base_agent, command, scope):
    ingress = scope.agents_ingress
    if (type(ingress) is not _AgentsIngress or ingress.seal is not _INGRESS_SEAL
            or not ingress.active or not ingress.used
            or ingress.identity != (scope.user_id, scope.channel, scope.conversation_id)
            or ingress.raw != command.value):
        return _failure('AGENTS_WRITE_NOT_USER_REQUEST')
    try:
        from .model_profile_service import _scoped_model_host
        actor, thread = _scoped_model_host(base_agent, scope)
        return _run_for_actor(base_agent, actor, command,
            _conversation_key(scope.user_id, scope.channel, scope.conversation_id), ingress.request_id,
            guard_thread=thread.thread_id)
    except Exception as error:
        _raise_agents_runtime_error(error)
        return _failure(getattr(error, 'code', getattr(error, 'error_code', 'AGENTS_CONTROL_UNAVAILABLE')))

# LLM: actor来自当前真实入口，显式target只选择受授权仓库；群配置保原HostActor租约，结果交唯一纯投影，不借监控grant或模型文本。
# 函数用途: 执行一次查看或预览确认；运输受理与details里的业务未知分别投影，不引发通用IM重放。
def _run_for_actor(base_agent, actor, command, thread, request_id, *, guard_thread=''):
    from ..user_space.group_authorization_context import group_capability_guard
    target, _, _ = parse_agents_value(command.value)
    selected = _target_agent(base_agent, actor, target)
    repo = build_agents_source_repository(selected)
    _, access = resolve_owner_scope_and_access(actor.home_paths, actor.config)
    request = _direct_request(owner=repo.owner, actor=actor.home_paths.owner_id,
        thread_id=thread, request_id=request_id, raw=command.value,
        actor_is_admin=is_complete_local_admin_owner(actor.home_paths), full_access=access == 'full-access',
        admin_target_owner=target if repo.owner != actor.home_paths.owner_id else '')
    with group_capability_guard(actor, 'assistant.configure', thread_id=guard_thread):
        details = AgentsWriteService(repo).execute(request)
    return _agents_control_result(details, request_id)


# LLM: 只投影原AgentsWriteService结构化结果，不签权或重放；所有直接/冷C入口共用相同业务字段、nonce提示和unknown语义。
# 函数用途: 将原规则预览或提交结果整理成一致的用户控制回执，无文件或网络副作用。
def _agents_control_result(details, request_id):
    state = details['state']
    if state == 'preview':
        message = ('预览，尚未写入；旧版本与拟写值已绑定。\n旧CAS：'
            + json.dumps(details['old_cas'], ensure_ascii=False) + '\n旧正文SHA：' + details['old_sha']
            + '\n新值SHA：' + details['intent_sha256'] + '\n'
            + json.dumps(details['value'], ensure_ascii=False) + '\n' + details['confirm_command'])
    elif state == 'completed':
        message = '已提交；来源在既有刷新点激活。'
    elif state == 'loaded':
        message = ('待生效来源：' + details['snapshot']['kind'] + '\n'
            + (details['content'] if details['content'] is not None else '来源不可用')
            + '\n刷新仅重新加载；当前冻结前缀保持至既有刷新点。')
    else:
        message = '结果未确认，禁止重放；请核对实际来源与 ' + details['receipt_command']
    return ConversationControlResult('agents', details['ok'], message, request_id=request_id,
        error_code=details.get('error_code', ''), details=details, control_state=state,
        delivery_status='accepted')

# LLM: 仅公共parser调用，解析不会生成任何授权；无效输入也不能退到模型。
# 函数用途: 将/agents的尾部转换成共享控制对象。
def parse_agents_command(trailing):
    raw = str(trailing or '').strip()
    try:
        _, operation, _ = parse_agents_value(raw)
    except AgentsContractError:
        return ConversationControlCommand('agents', value=raw, valid=False, usage=USAGE)
    return ConversationControlCommand('agents', value=raw, operation=operation, usage=USAGE)


# LLM: 仅终端直接控制，不注册模型工具；编号取UI提交；原执行拒绝/运输未结同对象外抛，不在业务层恢复。
# 函数用途: direct TUI在相同服务执行，无需启动Gateway或读取其凭据。
def execute_agents_direct(agent, command, session_id, message_id):
    try:
        if not session_id or not message_id:
            raise AgentsContractError('AGENTS_WRITE_REQUEST_ID_REQUIRED', 'request_id')
        return _run_for_actor(agent, agent, command,
                              _conversation_key('local-agent', 'chat', session_id), message_id)
    except Exception as error:
        _raise_agents_runtime_error(error)
        return _failure(getattr(error, 'code', 'AGENTS_CONTROL_UNAVAILABLE'))


# LLM: 会话绑定来自宿主已认证通道事实，不读取metadata的thread_id或role。
# 函数用途: 给直接控制创建稳定且不暴露渠道ID的线程身份。
def _conversation_key(user, channel, conversation):
    if not all(type(v) is str and v for v in (user, channel, conversation)):
        raise AgentsContractError('AGENTS_WRITE_BINDING_MISMATCH', 'thread_id')
    return 'agents-thread-' + sha256(json.dumps([user, channel, conversation]).encode()).hexdigest()


# LLM: target只是用户独立选择，不可改变actor；跨owner必须先验证管理员和当前Full Access。
# 函数用途: 安全解析显式目标并取得其已有宿主，不把path文本当权限。
def _target_agent(base_agent, actor, target):
    identity = _target_owner_identity(actor, target)
    return cold_owner_host(base_agent, identity) if identity is not None else actor


# LLM: 旧Agent和冷C共用纯target解析；普通local用户必须经UID校验，权限门不变，不创建账号、目录或签权。
# 函数用途: 在原操作者上下文检查显式管理员目标，返回规范身份供实际资源组合根使用。
def _target_owner_identity(actor, target):
    from ..user_space.owner_resolver import OwnerIdentity
    if not target or target == actor.home_paths.owner_id:
        return None
    admin = is_complete_local_admin_owner(actor.home_paths)
    _, access = resolve_owner_scope_and_access(actor.home_paths, actor.config)
    if not admin or access != 'full-access':
        raise AgentsContractError('AGENTS_WRITE_CROSS_OWNER_FORBIDDEN', 'owner')
    parts = target.split('/')
    if parts == ['local', 'main']:
        identity = OwnerIdentity.local_main()
    elif len(parts) == 2 and parts[0] == 'local' and parts[1] != 'main':
        try:
            uid = validate_local_user_id(parts[1])
        except HostAPIError as error:
            raise AgentsContractError('AGENTS_WRITE_ADMIN_TARGET_REQUIRED', 'owner') from error
        identity = OwnerIdentity('local', 'user', uid)
    elif len(parts) == 4 and parts[0] == 'providers' and parts[2] in {'users', 'groups'}:
        constructor = OwnerIdentity.provider_user if parts[2] == 'users' else OwnerIdentity.provider_group
        identity = constructor(parts[1], parts[3])
    else:
        raise AgentsContractError('AGENTS_WRITE_ADMIN_TARGET_REQUIRED', 'owner')
    # 规范比较发生在构造/安全owner解析之后；目标字符串不能跳出owner命名空间。
    from ..user_space.owner_resolver import resolve_owner_home
    resolved = (LocalUserStore(actor.home_paths.root).owner_home(identity.owner_id)
                if identity.provider == 'local' and identity.owner_kind == 'user'
                else resolve_owner_home(actor.home_paths.root, identity))
    if resolved.owner_id != target:
        raise AgentsContractError('AGENTS_WRITE_BINDING_MISMATCH', 'owner')
    return identity


# LLM: 异常不带私人路径/正文；失败绝不转成成功回执。
# 函数用途: 生成统一固定原因码及重新预览提示。
def _failure(code):
    return ConversationControlResult('agents', False,
                                     'AGENTS操作未完成（' + code + '）；请查看当前状态或重新预览。',
                                     error_code=code, delivery_status='rejected')
