# LLM: 通道身份只来自宿主专有参数、子代理原记录或不可变 execution_origin；模型 attributes、最新群成员和管理员绑定不作替代来源。
# 模块用途: 为监控、审批和子孙继承统一解析原发起人，不新增执行权、外发或身份账本。
from __future__ import annotations

from dataclasses import asdict, dataclass

from ..runtime_errors import DataCorruptionError
from .channels import supports_transcript_delivery
from .execution_threads import EXECUTION_ORIGIN_KEY, IM_TASK_EXECUTION_KEY, execution_origin_thread

_FIELDS = {"owner_id", "source_thread_id", "entry_request_id", "channel", "conversation_id", "actor_id"}
_MESSAGES = {
    "SCHEDULER_MEMBERSHIP_UNAVAILABLE": "创建者成员状态待核验：群成员权限、网络或完整分页不可用，未授权代为清理。",
    "SCHEDULER_CURRENT_MEMBER_REQUIRED": "当前操作者不在原群，已拒绝代为清理。",
    "SCHEDULER_CREATOR_STILL_MEMBER": "创建者仍在原群，只有原创建者可管理该监控。",
    "CHANNEL_ACTOR_IDENTITY_REQUIRED": "当前执行缺少可信发起人身份，请从原会话重新发起。",
    "CHANNEL_ACTOR_IDENTITY_INVALID": "原发起人身份记录不可读或已损坏，已拒绝猜测身份。",
    "CHANNEL_ACTOR_IDENTITY_CONFLICT": "当前发起人与原任务冻结身份冲突，已拒绝操作。",
    "CHANNEL_ACTOR_SCOPE_MISMATCH": "发起人身份与当前用户或会话归属不一致，已拒绝操作。",
}


# LLM: 异常正文固定，不能携带账本路径、任务正文或凭据；调用方按准确 error_code 返回失败，不继续副作用。
# 类用途: 区分身份缺失、坏记录、冲突与越界，便于用户和无正文审计定位。
class HostActorError(ValueError):
    # LLM: code 由宿主有限合同提供，异常不保存输入载荷。
    # 函数用途: 构造稳定身份拒绝码与安全说明。
    def __init__(self, code: str):
        self.error_code = code
        super().__init__(_MESSAGES[code])


# LLM: 对象只由入口 scope 或原记录构造；owner/thread/request 是出处，不含模型可写属性，传递不授予工具权限。
# 类用途: 保存一次宿主受理的不可变通道发起人及准确来源。
@dataclass(frozen=True)
class HostChannelActor:
    owner_id: str
    source_thread_id: str
    entry_request_id: str
    channel: str
    conversation_id: str
    actor_id: str

    # LLM: 即使宿主直接构造也必须满足准确字段合同，空值不能被当作本地或管理员。
    # 函数用途: 校验冻结对象，拒绝类型错误和缺失出处。
    def __post_init__(self) -> None:
        value = asdict(self)
        if (any(not isinstance(item, str) or item != item.strip() for item in value.values())
                or any(not value[key] for key in _FIELDS - {"conversation_id"})):
            raise HostActorError("CHANNEL_ACTOR_IDENTITY_INVALID")

    # LLM: 仅用于原子代理记录的专有字段；不放入 attributes 或模型工具参数。
    # 函数用途: 将宿主身份保存为原账本可恢复的结构。
    def to_record(self) -> dict[str, str]:
        return asdict(self)

    # LLM: 持久记录必须准确六字段；未知形状不从 legacy attrs 或最新绑定修补。
    # 函数用途: 校验并恢复原记录里的宿主身份。
    @classmethod
    def from_record(cls, value: object) -> HostChannelActor:
        if (not isinstance(value, dict) or set(value) != _FIELDS
                or any(not isinstance(item, str) or item != item.strip() for item in value.values())
                or any(not value[key] for key in _FIELDS - {"conversation_id"})):
            raise HostActorError("CHANNEL_ACTOR_IDENTITY_INVALID")
        return cls(**value)

    # LLM: execution_origin 仍使用原三字段协议，此投影只丢出处、不改变身份或写状态。
    # 函数用途: 为原调度执行线程提供冻结通道绑定。
    def binding(self) -> dict[str, str]:
        return {key: getattr(self, key) for key in ("channel", "conversation_id", "actor_id")}


# LLM: scope 是 Gateway 已验证的入口对象，不消费请求额外字段；本地通道不创建 IM 身份。
# 函数用途: 在进入模型前冻结准确 owner/thread/request 与发起人。
def actor_from_scope(agent, scope, thread_id: str, request_id: str) -> HostChannelActor | None:
    if scope is None or supports_transcript_delivery(scope.channel):
        return None
    value = HostChannelActor.from_record({"owner_id": agent.home_paths.owner_id,
        "source_thread_id": thread_id, "entry_request_id": request_id,
        "channel": scope.channel, "conversation_id": scope.channel_conversation_id,
        "actor_id": scope.channel_user_id})
    return _validate_scope(agent, thread_id, value)


# LLM: 原 ThreadStore 的 owner 和不可变来源是验证锚；读取错误统一拒绝，不打印原错误正文。
# 函数用途: 读取本次线程及其原聊天，避免跨用户引用或坏来源被当成本地身份。
def _origin(agent, thread_id: str):
    try:
        thread, error = agent.conversation_store.threads.load_report(thread_id)
        if error or thread is None:
            raise HostActorError("CHANNEL_ACTOR_IDENTITY_INVALID")
        owner = str(getattr(getattr(agent, "home_paths", None), "owner_id", "") or "")
        if owner and thread.owner_id and thread.owner_id != owner:
            raise HostActorError("CHANNEL_ACTOR_SCOPE_MISMATCH")
        source = execution_origin_thread(agent.conversation_store.threads.require, thread)
        return thread, source
    except HostActorError:
        raise
    except (DataCorruptionError, OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise HostActorError("CHANNEL_ACTOR_IDENTITY_INVALID") from exc


# LLM: 当前入口与原工作来源必须一致；immutable 来源已经固定 actor，只复核会话地址，不用最新绑定替换原人。
# 函数用途: 检查专有身份的 owner、线程、通道及冻结来源冲突。
def _validate_scope(agent, thread_id: str, actor: HostChannelActor) -> HostChannelActor:
    thread, source = _origin(agent, thread_id)
    if source.owner_id != actor.owner_id or source.thread_id != actor.source_thread_id:
        raise HostActorError("CHANNEL_ACTOR_SCOPE_MISMATCH")
    reference = thread.metadata.get(EXECUTION_ORIGIN_KEY)
    if reference is not None and reference["binding"] != actor.binding():
        raise HostActorError("CHANNEL_ACTOR_IDENTITY_CONFLICT")
    group = getattr(getattr(agent, "home_paths", None), "owner_kind", "") == "group"
    if not any(row.channel == actor.channel and (group or reference is not None or row.channel_user_id == actor.actor_id) and (
            row.channel_conversation_id == actor.conversation_id if actor.conversation_id
            else row.channel_user_id == actor.actor_id) for row in source.channel_bindings):
        raise HostActorError("CHANNEL_ACTOR_SCOPE_MISMATCH")
    return actor


# LLM: 普通 RunParams 只认冻结对象；缺对象的外部来源先核对准确主根，本机无通道请求在晋升前保持 None，不从 attrs 造人。
# 函数用途: 为当前前台、后台和子代理返回同一准确发起人，本地无 IM 身份返回 None。
def resolve_host_actor(agent, thread_id: str) -> HostChannelActor | None:
    current = getattr(agent, "_current_run_params", None)
    actor = getattr(current, "host_channel_actor", None)
    if actor is not None:
        if not isinstance(actor, HostChannelActor):
            raise HostActorError("CHANNEL_ACTOR_IDENTITY_INVALID")
        return _validate_current_actor_root(agent, current, thread_id, _validate_scope(agent, thread_id, actor))
    _thread, source = _origin(agent, thread_id)
    external = (getattr(getattr(agent, "home_paths", None), "owner_kind", "") == "group"
                or any(not supports_transcript_delivery(row.channel) for row in source.channel_bindings))
    if not external:
        return None
    root_id = str(getattr(current, "task_id", "") or "")
    if root_id:
        actor = background_host_actor(agent, thread_id, root_id)
        if actor is not None:
            return actor
    raise HostActorError("CHANNEL_ACTOR_IDENTITY_REQUIRED")


# LLM: 已存在的准确 TaskLink 是当前逻辑根，不是可由新群成员改写的聊天投影；同本人新入口只换 request，不换原根身份。
# 函数用途: 在所有敏感入口统一拒绝当前 actor 与原根冲突，新请求尚无根时保留宿主已认证身份。
def _validate_current_actor_root(agent, current, thread_id, actor):
    root_id = str(getattr(current, "task_id", "") or "")
    if not root_id:
        return actor
    link, error = agent.conversation_store.tasks.load_report(root_id)
    if error:
        raise HostActorError("CHANNEL_ACTOR_IDENTITY_INVALID")
    if link is None:
        return actor
    original = background_host_actor(agent, thread_id, root_id)
    if original is None:
        raise HostActorError("CHANNEL_ACTOR_IDENTITY_REQUIRED")
    if (original.owner_id != actor.owner_id or original.source_thread_id != actor.source_thread_id
            or original.binding() != actor.binding()):
        raise HostActorError("CHANNEL_ACTOR_IDENTITY_CONFLICT")
    return actor


# LLM: attributes 是模型可写数据，不得携带认证别名；只删除身份键，其它业务属性原样保留。
# 函数用途: 清除子孙工具参数里的伪造发起人，不把旧同名字段升级成权限事实。
def clear_actor_attributes(attributes: dict) -> None:
    for key in ("host_channel_actor", "channel", "channel_user_id", "user_id"):
        attributes.pop(key, None)


# LLM: 创建流程只读取父当前准确线程，子参数不能替换它；本地父 None 不产生外部 actor。
# 函数用途: 给新建子任务的专有字段准备宿主身份，旧不明来源明确拒绝。
def current_actor_for_child(agent) -> HostChannelActor | None:
    if agent is None:
        return None
    current = getattr(agent, "_current_run_params", None)
    attrs = getattr(current, "task_attributes", None) or {}
    thread_id = str(attrs.get("conversation_thread_id") or "")
    if not thread_id:
        if getattr(current, "host_channel_actor", None) is not None:
            raise HostActorError("CHANNEL_ACTOR_IDENTITY_REQUIRED")
        return None
    actor = resolve_host_actor(agent, thread_id)
    root_id = str(getattr(current, "task_id", "") or attrs.get("conversation_task_id") or "")
    if actor is not None and root_id:
        link, error = agent.conversation_store.tasks.load_report(root_id)
        if error:
            raise HostActorError("CHANNEL_ACTOR_IDENTITY_INVALID")
        if link is not None:
            original = background_host_actor(agent, link.thread_id, root_id)
            if original is None or original.owner_id != actor.owner_id or original.binding() != actor.binding():
                raise HostActorError("CHANNEL_ACTOR_IDENTITY_CONFLICT")
            return _validate_scope(agent, thread_id, original)
    return actor


# LLM: 专有字段属于 canonical task；子孙必须与原 parent/root 的专有身份一致，旧 attrs 永远不作恢复依据。
# 函数用途: 恢复子代理宿主身份并校验原谱系，允许旧工作线程按不可变来源恢复。
def task_host_actor(agent, task) -> HostChannelActor | None:
    record = getattr(task, "host_channel_actor", None)
    thread_id = str((task.attributes or {}).get("conversation_thread_id") or "")
    if record is None:
        # 先核对准确原根，不能用模型 attributes 指向另一人的工作线程来升级身份。
        if not task.root_id or task.root_id == task.id:
            return _missing_task_actor(agent, thread_id)
        actor = background_host_actor(agent, "", task.root_id)
        if actor is None:
            return _missing_task_actor(agent, thread_id)
        if task.owner != actor.owner_id:
            raise HostActorError("CHANNEL_ACTOR_SCOPE_MISMATCH")
        for identifier in {task.parent_id, task.root_id} - {"", task.id}:
            validate_actor_ancestor(agent, identifier, actor)
        return _validate_scope(agent, thread_id, actor)
    actor = HostChannelActor.from_record(record)
    if task.owner != actor.owner_id:
        raise HostActorError("CHANNEL_ACTOR_SCOPE_MISMATCH")
    for identifier in {task.parent_id, task.root_id} - {"", task.id}:
        validate_actor_ancestor(agent, identifier, actor)
    if task.parent_id:
        try:
            parent = agent.subagents.load(task.parent_id)
        except FileNotFoundError:
            parent = None
        if parent is not None and (parent.root_id or parent.id) != task.root_id:
            raise HostActorError("CHANNEL_ACTOR_IDENTITY_CONFLICT")
    return _validate_scope(agent, thread_id, actor)


# LLM: 主根可能是原 ConversationTaskLink 而非子代理；只有准确记录及原线程归属证明才能通过，不跳过缺失根。
# 函数用途: 在子代理原 parent/root 或主代理原任务关联上校验同一冻结发起人。
def validate_actor_ancestor(agent, identifier: str, actor: HostChannelActor) -> None:
    manager = getattr(agent, "subagents", agent)
    try:
        ancestor = manager.load(identifier)
    except FileNotFoundError:
        ancestor = None
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise HostActorError("CHANNEL_ACTOR_IDENTITY_INVALID") from exc
    if ancestor is not None:
        if (ancestor.owner != actor.owner_id or not ancestor.host_channel_actor
                or HostChannelActor.from_record(ancestor.host_channel_actor) != actor):
            raise HostActorError("CHANNEL_ACTOR_IDENTITY_CONFLICT")
        return
    store = agent.conversation_store
    link, error = store.tasks.load_report(identifier)
    if error or link is None or link.task_id != identifier:
        raise HostActorError("CHANNEL_ACTOR_IDENTITY_INVALID")
    root_actor = background_host_actor(agent, link.thread_id, identifier)
    if root_actor is None:
        raise HostActorError("CHANNEL_ACTOR_IDENTITY_REQUIRED")
    if root_actor != actor:
        raise HostActorError("CHANNEL_ACTOR_IDENTITY_CONFLICT")


# LLM: task_id 必须来自原后台 claim；读取准确 TaskLink，首片委派读取原 SessionTask 与冻结策略，不按 raw 线程挑人。
# 函数用途: 恢复准确主根身份；旧无证据返回 None 不授予身份，敏感操作及孩子恢复自行拒绝。
def background_host_actor(agent, thread_id: str, task_id: str) -> HostChannelActor | None:
    link, error = agent.conversation_store.tasks.load_report(task_id)
    if error and error.get("error_type") != "FileNotFoundError":
        raise HostActorError("CHANNEL_ACTOR_IDENTITY_INVALID")
    if link is None:
        return _delegated_host_actor(agent, thread_id, task_id)
    if link.task_id != task_id:
        raise HostActorError("CHANNEL_ACTOR_IDENTITY_INVALID")
    if thread_id and link.thread_id != thread_id:
        raise HostActorError("CHANNEL_ACTOR_SCOPE_MISMATCH")
    record = link.host_channel_actor
    if record is not None:
        return _validate_scope(agent, link.thread_id, HostChannelActor.from_record(record))
    thread, source = _origin(agent, link.thread_id)
    reference = thread.metadata.get(EXECUTION_ORIGIN_KEY)
    if reference is not None:
        return _validate_scope(agent, link.thread_id, HostChannelActor.from_record({"owner_id": thread.owner_id,
            "source_thread_id": source.thread_id, "entry_request_id": reference["operation_key"], **reference["binding"]}))
    return None


# LLM: 首个委派唤醒尚未晋升 TaskLink，只按精确 SessionTask 及目标冻结策略恢复；普通派活不能借目标工作线程的发起人。
# 函数用途: 校验原委派发送方、目标及策略同源，再恢复原入口身份，不写账或猜当前群成员。
def _delegated_host_actor(agent, thread_id, task_id):
    try:
        task = agent.conversation_store.session_tasks.load(task_id)
        if task is None or task.task_id != task_id:
            raise HostActorError("CHANNEL_ACTOR_IDENTITY_INVALID")
        if thread_id and task.target_thread_id != thread_id:
            raise HostActorError("CHANNEL_ACTOR_SCOPE_MISMATCH")
        thread, source = _origin(agent, task.target_thread_id)
        reference = thread.metadata.get(EXECUTION_ORIGIN_KEY)
        policy = thread.metadata.get(IM_TASK_EXECUTION_KEY)
        if reference is None and policy is None:
            return None
        if (reference is None or reference["kind"] != "delegated" or not isinstance(policy, dict)
                or policy.get("mode") != "delegated" or policy.get("session_task_id") != task_id
                or task.sender_thread_id != source.thread_id):
            raise HostActorError("CHANNEL_ACTOR_SCOPE_MISMATCH")
        return _validate_scope(agent, thread.thread_id, HostChannelActor.from_record({"owner_id": thread.owner_id,
            "source_thread_id": source.thread_id, "entry_request_id": reference["operation_key"], **reference["binding"]}))
    except HostActorError:
        raise
    except (DataCorruptionError, OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise HostActorError("CHANNEL_ACTOR_IDENTITY_INVALID") from exc


# LLM: 旧 child 没有原根证明时不得用 raw thread 恢复外部 actor，本地任务仍保持无 IM 身份。
# 函数用途: 拒绝无出处的外部子任务，保留本机旧任务的原行为。
def _missing_task_actor(agent, thread_id):
    _thread, source = _origin(agent, thread_id) if thread_id else (None, None)
    if (getattr(getattr(agent, "home_paths", None), "owner_kind", "") == "group"
            or source is not None and any(not supports_transcript_delivery(row.channel) for row in source.channel_bindings)):
        raise HostActorError("CHANNEL_ACTOR_IDENTITY_REQUIRED")
    return None
