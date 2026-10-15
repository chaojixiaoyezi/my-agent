# LLM: 引用上下文只来自认证入站和原根输入；子孙复核当前RuntimeDB attempt/原HostActor后读取，不从attrs或owner推测队列。
# 模块用途: 在原TaskLink或SessionTask中保存一次不可变消息引用输入，根链接只引用唯一快照，恢复不依赖Gateway请求保留期。
from __future__ import annotations

from dataclasses import dataclass

from ..adapter.protocol import provider_message_reference
from .host_actor import HostChannelActor

CONTEXT_KEY = 'host_message_context.v1'


# LLM: 只保存原HostActor与精确引用，不含正文/凭据；构造和恢复都核形状，不能把模型字典当作typed写权。
# 类用途: 冻结一个入站请求的消息选择上下文，不执行IO或授予订阅权限。
@dataclass(frozen=True)
class HostMessageContext:
    actor: HostChannelActor
    references: tuple[tuple[str, str], ...]

    # LLM: refs必须是宿主不可变二元组且摘要与真实ID一致；此检查不替代官方Get或群授权，无副作用。
    # 函数用途: 拒绝坏快照、重复引用和伪造映射。
    def __post_init__(self):
        if not isinstance(self.actor, HostChannelActor) or not isinstance(self.references, tuple) or not 1 <= len(self.references) <= 256:
            raise ValueError('原请求引用上下文无效')
        for row in self.references:
            if (not isinstance(row, tuple) or len(row) != 2 or not all(isinstance(value, str) and value for value in row)
                    or len(row[1]) > 256 or not row[1].startswith('om_') or row[0] != provider_message_reference(row[1])):
                raise ValueError('原请求引用绑定无效')
        if len({key for key, _value in self.references}) != len(self.references):
            raise ValueError('原请求引用重复')

    # LLM: 只向原受保护根输入序列化，不放进模型attrs、提示词或孩子记录，调用者负责原写锁。
    # 函数用途: 生成可持久化的快照记录，不写文件。
    def to_record(self):
        return {'schema': CONTEXT_KEY, 'actor': self.actor.to_record(),
            'references': [{'ref': key, 'message_id': value} for key, value in self.references]}

    # LLM: 恢复必须准确schema/字段，旧缺失与坏值都不补认，错误不携带原载荷。
    # 函数用途: 从唯一原根输入恢复经过校验的不可变上下文。
    @classmethod
    def from_record(cls, raw):
        if (not isinstance(raw, dict) or set(raw) != {'schema', 'actor', 'references'} or raw.get('schema') != CONTEXT_KEY
                or not isinstance(raw.get('references'), list) or any(not isinstance(row, dict)
                    or set(row) != {'ref', 'message_id'} for row in raw['references'])):
            raise ValueError('原请求引用上下文不可读')
        return cls(HostChannelActor.from_record(raw['actor']),
            tuple((row['ref'], row['message_id']) for row in raw['references']))


# LLM: 根只能inline或精确SessionTask引用二选一；delegate不复制快照到TaskLink/孩子，也不按最新会话恢复。
# 类用途: 描述原根输入的唯一存储位置，不执行IO。
@dataclass(frozen=True)
class HostMessageContextBinding:
    snapshot: HostMessageContext | None = None
    session_task_id: str = ''

    # LLM: typed快照与引用互斥，引用不能带路径；写入仍由原TaskStore typed keyword控制。
    # 函数用途: 拒绝双权威或可改变读取位置的非法引用。
    def __post_init__(self):
        if ((self.snapshot is None) == (not self.session_task_id)
                or self.snapshot is not None and not isinstance(self.snapshot, HostMessageContext)
                or not isinstance(self.session_task_id, str) or '/' in self.session_task_id or '\\' in self.session_task_id):
            raise ValueError('根引用上下文位置无效')

    # LLM: 只输出一种来源，不将SessionTask正文或快照复制进引用记录，无副作用。
    # 函数用途: 序列化原根输入的位置。
    def to_record(self):
        return ({'source': 'inline', 'context': self.snapshot.to_record()} if self.snapshot is not None
            else {'source': 'session_task', 'task_id': self.session_task_id})

    # LLM: 只接受准确结构来源，不猜旧字段/路径/模型标签，无副作用。
    # 函数用途: 严格恢复根输入位置，坏记录不切换另一条来源。
    @classmethod
    def from_record(cls, raw):
        if isinstance(raw, dict) and set(raw) == {'source', 'context'} and raw.get('source') == 'inline':
            return cls(snapshot=HostMessageContext.from_record(raw['context']))
        if isinstance(raw, dict) and set(raw) == {'source', 'task_id'} and raw.get('source') == 'session_task':
            return cls(session_task_id=raw['task_id'])
        raise ValueError('根引用上下文位置不可读')


# LLM: SessionTask必须属于原delegated线程/policy/actor；首片queued可有真实root运行，终态或换来源均拒绝，不读取消息正文。
# 函数用途: 只读恢复一次宿主委派的原输入快照，核对原接单身份。
def _session_context(agent, identifier, actor, target_thread_id):
    from .execution_threads import EXECUTION_ORIGIN_KEY, delegated_session_task

    task = delegated_session_task(agent.conversation_store, target_thread_id)
    thread = agent.conversation_store.threads.require(target_thread_id)
    origin = thread.metadata.get(EXECUTION_ORIGIN_KEY) or {}
    if (task is None or task.task_id != identifier or task.sender_thread_id != actor.source_thread_id
            or thread.owner_id != actor.owner_id or origin.get('operation_key') != actor.entry_request_id
            or origin.get('binding') != actor.binding() or task.status not in {'queued', 'accepted'}
            or task.conversation_request_id not in {'', task.task_id}):
        raise ValueError('委派原输入归属不一致')
    raw = task.metadata.get(CONTEXT_KEY)
    if raw is None:
        return None
    context = HostMessageContext.from_record(raw)
    if context.actor != actor:
        raise ValueError('委派原输入发起人不一致')
    return context


# LLM: 晋升只消费当前专有字段或已核SessionTask，不从模型request字典取同名值；TaskStore负责锁内不可改写。
# 函数用途: 为新TaskLink选择inline快照或委派根的精确引用，不写状态。
def promotion_context_binding(agent, current, actor, thread_id):
    refs = getattr(current, 'host_message_references', ())
    if actor is None:
        return None
    if getattr(current, 'request_id', '') == actor.entry_request_id:
        return HostMessageContextBinding(snapshot=HostMessageContext(actor, refs)) if refs else None
    if getattr(current, 'source', '') == 'gateway':
        return None
    identifier = str(getattr(current, 'task_id', '') or '')
    if not identifier or agent.conversation_store.session_tasks.load(identifier) is None:
        return None
    context = _session_context(agent, identifier, actor, thread_id)
    return HostMessageContextBinding(session_task_id=identifier) if context is not None else None


# LLM: 先用caller attempt join原RuntimeDB，再比较owner/thread及canonical子档谱系；业务role不决定主子，不懒建权威或猜队列。
# 函数用途: 只读验证后台/子孙真实当前执行，并取得唯一原TaskLink。
def _current_root(agent, current, actor):
    from ..runtime_context import current_subagent_run_id
    from ..runtime_db.managed_operation_store import (
        ManagedOperationStore,
        ToolOperationAuthorityRequest,
    )
    from .host_actor import task_host_actor

    owner = str(agent.home_paths.owner_id)
    run_id, task_id, attempt_id = (str(getattr(current, name, '') or '') for name in ('run_id', 'task_id', 'attempt_id'))
    if not all((run_id, task_id, attempt_id)) or actor.owner_id != owner:
        raise ValueError('当前引用执行身份缺失')
    repo = getattr(getattr(agent, 'subagents', None), 'runtime_db', None)
    ManagedOperationStore(repo).require_authority(ToolOperationAuthorityRequest(owner_id=owner,
        run_id=run_id, task_id=task_id, attempt_id=attempt_id, operation_id='', tool_name='host_message_context'))
    task = repo.get_task(task_id)
    attempt = repo.get_attempt(attempt_id)
    run = repo.get_agent_run(attempt['agent_run_id'])
    link = agent.conversation_store.tasks.load(task_id)
    child_id = current_subagent_run_id(agent)
    if (task is None or task['owner_id'] != owner or link is None or link.status != 'active'
            or task['thread_id'] != link.thread_id or child_id and child_id != run_id
            or link.host_channel_actor is None or HostChannelActor.from_record(link.host_channel_actor) != actor):
        raise ValueError('当前引用原根不一致')
    try:
        child = agent.subagents.load(run_id)
    except FileNotFoundError:
        child = None
    if child is not None:
        if child.root_id != task_id or child.id != run_id or task_host_actor(agent, child) != actor:
            raise ValueError('当前引用子孙谱系不一致')
        parent = repo.get_agent_run(run['parent_agent_run_id']) if run['parent_agent_run_id'] else None
        if (run['parent_agent_run_id'] and parent is None
                or parent is not None and parent['run_id'] != child.parent_id
                or parent is None and child.parent_id != task_id):
            raise ValueError('当前引用原父绑定不一致')
    elif child_id or run['parent_agent_run_id']:
        raise ValueError('当前引用子孙原记录缺失')
    else:
        root = repo.main_agent_run_for_task(task_id)
        if root is None or root['agent_run_id'] != run['agent_run_id']:
            raise ValueError('当前引用不是原宿主主根')
    return link


# LLM: actor须经resolve_host_actor核scope；独立Gateway只消费本请求，已登记后台/子孙复核当前attempt，缺失不从attrs/prompt恢复。
# 函数用途: 为来源工具返回准确引用集合；只读原输入，无平台请求、状态变更或新增账本。
def references_for_run(agent, actor):
    current = getattr(agent, '_current_run_params', None)
    if getattr(current, 'source', '') == 'gateway':
        if (getattr(current, 'request_id', '') != actor.entry_request_id
                or getattr(current, 'host_channel_actor', None) != actor):
            raise ValueError('当前请求引用发起人不一致')
        refs = getattr(current, 'host_message_references', ())
        return HostMessageContext(actor, refs).references if refs else ()
    link = _current_root(agent, current, actor)
    binding = HostMessageContextBinding.from_record(link.host_message_context)
    context = binding.snapshot
    if binding.session_task_id:
        if binding.session_task_id != link.task_id:
            raise ValueError('委派引用不是原根输入')
        context = _session_context(agent, binding.session_task_id, actor, link.thread_id)
    if context is None or context.actor != actor:
        raise ValueError('原根引用发起人不一致')
    return context.references
