# LLM: 原输入快照只来自认证入站；引用与媒体复用同一TaskLink/SessionTask位置，子孙复核当前attempt且仅得其已授权图片子集。
# 模块用途: 持久保存原请求的消息选择和附件引用，恢复不依赖Gateway文件，不从模型attrs新增图片读取权。
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path

from ..adapter.protocol import provider_message_reference
from .host_actor import HostChannelActor

CONTEXT_KEY = 'host_message_context.v1'


# LLM: refs的五个标量不可变；这里只校验结构，写前/用前仍须validate_input_media核owner、文件及上限，禁止base64或任意metadata。
# 类用途: 在原输入快照内保存一份不可被调用方字典原地改写的内容寻址附件引用。
@dataclass(frozen=True)
class HostInputMediaRef:
    path: str
    sha256: str
    media_type: str
    size_bytes: int
    name: str

    # LLM: 路径只是数据，不能单凭它授权；字段严格标量并由原owner校验器二次核验，不执行IO。
    # 函数用途: 拒绝不完整、嵌套或坏类型的媒体快照。
    def __post_init__(self):
        if (not isinstance(self.path, str) or not Path(self.path).is_absolute()
                or not isinstance(self.sha256, str) or re.fullmatch(r'[a-f0-9]{64}', self.sha256) is None
                or Path(self.path).name != self.sha256 or type(self.size_bytes) is not int or self.size_bytes <= 0
                or not isinstance(self.media_type, str) or not self.media_type.startswith(('image/', 'video/'))
                or not isinstance(self.name, str)):
            raise ValueError('原请求媒体引用无效')


# LLM: 只保存原HostActor、精确引用和已校验媒体；无正文/凭据/二进制，模型字典不能充当typed写权。
# 类用途: 冻结一次入站请求的消息选择和附件，原字段不含图时保持原序列化形状。
@dataclass(frozen=True)
class HostMessageContext:
    actor: HostChannelActor
    references: tuple[tuple[str, str], ...]
    input_media: tuple[HostInputMediaRef, ...] = ()

    # LLM: refs必须是宿主不可变二元组且摘要与真实ID一致；此检查不替代官方Get或群授权，无副作用。
    # 函数用途: 拒绝坏快照、重复引用和伪造映射。
    def __post_init__(self):
        if (not isinstance(self.actor, HostChannelActor) or not isinstance(self.references, tuple) or len(self.references) > 256
                or not isinstance(self.input_media, tuple) or any(not isinstance(ref, HostInputMediaRef) for ref in self.input_media)
                or not (self.references or self.input_media)):
            raise ValueError('原请求引用上下文无效')
        for row in self.references:
            if (not isinstance(row, tuple) or len(row) != 2 or not all(isinstance(value, str) and value for value in row)
                    or len(row[1]) > 256 or not row[1].startswith('om_') or row[0] != provider_message_reference(row[1])):
                raise ValueError('原请求引用绑定无效')
        if len({key for key, _value in self.references}) != len(self.references):
            raise ValueError('原请求引用重复')

    # LLM: 只向原受保护根输入序列化；新媒体字段仅非空时出现，旧无媒体记录不改写、不猜附件，调用者负责原锁。
    # 函数用途: 生成可持久化的快照记录，不写文件。
    def to_record(self):
        record = {'schema': CONTEXT_KEY, 'actor': self.actor.to_record(),
            'references': [{'ref': key, 'message_id': value} for key, value in self.references]}
        if self.input_media:
            record['input_media'] = [asdict(ref) for ref in self.input_media]
        return record

    # LLM: 显式兼容已发布的无媒体字段集；新字段严格校验，不从旧attrs/路径文字补图，不携原载荷。
    # 函数用途: 从唯一原根输入恢复经过校验的不可变上下文。
    @classmethod
    def from_record(cls, raw):
        if (not isinstance(raw, dict) or set(raw) not in ({'schema', 'actor', 'references'},
                    {'schema', 'actor', 'references', 'input_media'}) or raw.get('schema') != CONTEXT_KEY
                or not isinstance(raw.get('references'), list) or any(not isinstance(row, dict)
                    or set(row) != {'ref', 'message_id'} for row in raw['references'])
                or not isinstance(raw.get('input_media', []), list) or any(not isinstance(row, dict)
                    or set(row) != {'path', 'sha256', 'media_type', 'size_bytes', 'name'} for row in raw.get('input_media', []))):
            raise ValueError('原请求引用上下文不可读')
        return cls(HostChannelActor.from_record(raw['actor']),
            tuple((row['ref'], row['message_id']) for row in raw['references']),
            tuple(HostInputMediaRef(**row) for row in raw.get('input_media', [])))


# LLM: 仅认证Gateway构造点调用；媒体走原owner和配置上限校验再转不可变字段，不创建文件或第二存储。
# 函数用途: 将原消息引用与顶层已导入附件合并为唯一typed输入快照，没有输入时返回None。
def context_for_input(agent, actor, references=(), input_media=()):
    if not (references or input_media):
        return None
    if not isinstance(actor, HostChannelActor) or actor.owner_id != str(agent.home_paths.owner_id):
        raise ValueError('原请求媒体缺少可信发起人')
    refs = _validated_media(agent, input_media)
    return HostMessageContext(actor, tuple(references), tuple(HostInputMediaRef(**ref) for ref in refs))


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
        context = getattr(current, 'host_message_context', None)
        if context is not None:
            if not isinstance(context, HostMessageContext) or context.actor != actor:
                raise ValueError('原输入快照发起人不一致')
            _validated_media(agent, [asdict(ref) for ref in context.input_media])
            return HostMessageContextBinding(snapshot=context)
        return HostMessageContextBinding(snapshot=HostMessageContext(actor, refs)) if refs else None
    if getattr(current, 'source', '') == 'gateway':
        return None
    identifier = str(getattr(current, 'task_id', '') or '')
    if not identifier or agent.conversation_store.session_tasks.load(identifier) is None:
        return None
    context = _session_context(agent, identifier, actor, thread_id)
    return HostMessageContextBinding(session_task_id=identifier) if context is not None else None


# LLM: 先按attempt核RuntimeDB与canonical谱系；无TaskLink仅准原queued/accepted SessionTask首片准确主根，不能懒建或猜队列。
# 函数用途: 只读验证后台/子孙准确执行，返回原TaskLink；原SessionTask首片尚未晋升时返回None。
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
    if task is None or task['owner_id'] != owner or child_id and child_id != run_id:
        raise ValueError('当前引用原根不一致')
    if link is None:
        root = repo.main_agent_run_for_task(task_id)
        if (child_id or run['parent_agent_run_id'] or root is None or root['agent_run_id'] != run['agent_run_id']
                or getattr(current, 'host_channel_actor', None) != actor
                or _session_context(agent, task_id, actor, task['thread_id']) is None):
            raise ValueError('当前委派首片输入身份不一致')
        return None
    if (link.status != 'active' or task['thread_id'] != link.thread_id
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


# LLM: Gateway只认本请求typed快照/原只读引用投影，后台复核原root和attempt；新请求不得继承上一根输入。
# 函数用途: 读取唯一原输入容器，缺快照返回None，不从模型属性猜测。
def _context_for_run(agent, actor):
    if not isinstance(actor, HostChannelActor) or actor.owner_id != str(agent.home_paths.owner_id):
        raise ValueError('当前请求输入owner不一致')
    current = getattr(agent, '_current_run_params', None)
    if getattr(current, 'source', '') == 'gateway':
        if (getattr(current, 'request_id', '') != actor.entry_request_id
                or getattr(current, 'host_channel_actor', None) != actor):
            raise ValueError('当前请求引用发起人不一致')
        refs = getattr(current, 'host_message_references', ())
        context = getattr(current, 'host_message_context', None)
        if context is not None:
            if not isinstance(context, HostMessageContext) or context.actor != actor:
                raise ValueError('当前请求输入快照发起人不一致')
            return context
        return HostMessageContext(actor, refs) if refs else None
    link = _current_root(agent, current, actor)
    if link is None:
        current_task = agent.subagents.runtime_db.get_task(current.task_id)
        return _session_context(agent, current.task_id, actor, current_task['thread_id'])
    if link.host_message_context is None:
        return None
    binding = HostMessageContextBinding.from_record(link.host_message_context)
    context = binding.snapshot
    if binding.session_task_id:
        if binding.session_task_id != link.task_id:
            raise ValueError('委派引用不是原根输入')
        context = _session_context(agent, binding.session_task_id, actor, link.thread_id)
    if context is None or context.actor != actor:
        raise ValueError('原根引用发起人不一致')
    return context


# LLM: 引用仍是同一原输入的只读投影；新增媒体字段不授予订阅权或扩大工具引用选择。
# 函数用途: 为原来源工具取得消息引用，保持旧无媒体请求合同。
def references_for_run(agent, actor):
    context = _context_for_run(agent, actor)
    return context.references if context is not None else ()


# LLM: 主根读取唯一快照，孩子只读canonical子档已授权子集；同root不等于授权原根全部媒体，模型attrs不得补充。
# 函数用途: 给当前准确执行恢复附件引用，原Gateway被GC仍可读，跨owner/旧attempt失败。
def media_for_run(agent, actor):
    current = getattr(agent, '_current_run_params', None)
    if getattr(current, 'source', '') != 'gateway':
        _current_root(agent, current, actor)
        try:
            child = agent.subagents.load(str(current.run_id or ''))
        except FileNotFoundError:
            child = None
        if child is not None:
            refs = _validated_media(agent, (child.attributes or {}).get('input_media') or ())
            require_subagent_media(agent, refs)
            return refs
    context = _context_for_run(agent, actor)
    return _validated_media(agent, [asdict(ref) for ref in context.input_media]) if context is not None else ()


# LLM: 使用原开关和原SUBAGENT错误协议，关闭时创建前失败，不进入能力等待或删图派纯文字孩子。
# 函数用途: 在宿主自动委派和已授权子代发图前核显式媒体转交开关。
def require_subagent_media(agent, refs):
    from ..agent_core.orchestration.input_media_refs import (
        INPUT_MEDIA_DISABLED_ERROR_CODE,
        SubagentInputMediaError,
    )
    from ..capability.runtime_config_reload import capability_config_for_agent

    if refs and getattr(capability_config_for_agent(agent), 'subagent_input_media_enabled', False) is not True:
        raise SubagentInputMediaError(INPUT_MEDIA_DISABLED_ERROR_CODE,
            {'requested_media_refs': [ref['sha256'] for ref in refs]})


# LLM: 每次恢复沿同owner与当前资源上限重验，不读模型路径/全盘媒体索引，原校验器负责文件及MIME合同。
# 函数用途: 只读原owner媒体校验字节，返回可传原生输入的引用；不编码、不写文件或发网络。
def _validated_media(agent, refs):
    if not refs:
        return ()
    from .input_media import input_media_root, validate_input_media

    return validate_input_media(refs, root=input_media_root(agent), max_bytes=agent.config.input_media_max_bytes,
        max_files=agent.config.input_media_max_files)
