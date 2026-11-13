# LLM: 独立执行线程仍存于原 ThreadStore；来源引用只用于可信回送，不复制外部入站绑定、历史或任务状态。
# 模块用途: 为 IM 定时和委派准备可恢复的后台会话，让任务与聊天分别使用原执行 claim。
from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

from ..gateway_parts.io import locked_file_transition
from ..runtime_errors import DataCorruptionError
from .models import ConversationThread
from .store_io import now
from .verbose import inherited_verbose_metadata

EXECUTION_ORIGIN_KEY = "host_execution_origin.v1"
IM_TASK_EXECUTION_KEY = "host_im_task_execution.v1"


# LLM: 策略仅存原 SessionTask 的准确引用；来源/owner/目标必须一致，不复制状态，也不从生命周期 prompt 找任务。
# 函数用途: 为任意续跑片解析同一委派任务，避免孩子唤醒换了 request_id 后丢失原接单身份。
def delegated_session_task(store, thread_id):
    thread, error = store.threads.load_report(thread_id)
    if error:
        raise DataCorruptionError("后台线程不可读")
    if thread is None:
        return None
    policy = thread.metadata.get(IM_TASK_EXECUTION_KEY)
    if policy is None:
        return None
    task = store.session_tasks.load(policy.get("session_task_id", ""))
    source = execution_origin_thread(store.threads.require, thread)
    if task is None or task.target_thread_id != thread_id or task.sender_thread_id != source.thread_id:
        raise DataCorruptionError("委派任务引用不可读或归属冲突")
    return task


def lifecycle_session_task_origin(store, thread_id, task_id, wake, *, reason):
    """只读核实原SessionTask子事件来源；正文来自已消费的原guidance，不伪造user消息。"""
    from .models import SUBAGENT_LIFECYCLE_WAKE_REASONS

    if reason not in SUBAGENT_LIFECYCLE_WAKE_REASONS or not task_id or not isinstance(wake, dict):
        return None
    tasks = getattr(store, 'session_tasks', None)
    task = tasks.load(task_id) if tasks is not None else None
    if task is None:
        return None  # 普通用户请求、Goal和scheduler仍沿各自的原始来源。
    receiver = store.threads.require(thread_id)
    sender = store.threads.require(task.sender_thread_id)
    link, error = store.tasks.link_report(thread_id, task_id)
    if (error or link is None or link.status != 'active' or task.status in {'cancelled', 'failed'}
            or task.target_thread_id != thread_id or task.conversation_request_id != task.task_id
            or receiver.owner_id != sender.owner_id or receiver.owner_home != sender.owner_home
            or wake.get('root_task_id') != task.task_id):
        raise DataCorruptionError('SESSION_TASK_LIFECYCLE_SCOPE_INVALID')
    metadata = wake.get('metadata') or {}
    if not isinstance(metadata, dict) or not isinstance(metadata.get('events', []), list):
        raise DataCorruptionError('SESSION_TASK_LIFECYCLE_SOURCE_INVALID')
    envelopes = [wake, *metadata.get('events', [])]
    selected = [item for item in envelopes if isinstance(item, dict)
        and isinstance(item.get('metadata'), dict)
        and item['metadata'].get('conversation_request_id') == task.task_id]
    if not selected:
        raise DataCorruptionError('SESSION_TASK_LIFECYCLE_SOURCE_MISSING')
    import json

    from ..common.nofollow_fs import read_bytes_beneath

    home = Path(receiver.owner_home).absolute()
    for envelope in selected:
        child_id = envelope.get('source_agent_id')
        if (not child_id or envelope.get('parent_agent_id') != task.task_id
                or envelope.get('root_task_id') != task.task_id):
            raise DataCorruptionError('SESSION_TASK_LIFECYCLE_CHILD_MISMATCH')
        child_link, error = store.tasks.link_report(thread_id, child_id)
        if error or child_link is None or not child_link.task_path:
            raise DataCorruptionError('SESSION_TASK_LIFECYCLE_CHILD_MISSING')
        root = Path(child_link.task_path).absolute()
        def read_object(path):
            raw = read_bytes_beneath(home, path.relative_to(home).parts, max_bytes=4 * 1024 * 1024 + 1)
            if raw is None or len(raw) > 4 * 1024 * 1024:
                raise DataCorruptionError('SESSION_TASK_LIFECYCLE_CHILD_UNREADABLE')
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise DataCorruptionError('SESSION_TASK_LIFECYCLE_CHILD_INVALID')
            return value
        state = read_object(root / 'task.json')
        if state.get('canonical_state_ref'):
            canonical = Path(state['canonical_state_ref'])
            canonical.relative_to(root)  # 原locator只能指向这个孩子自己的规范状态。
            state = read_object(canonical)
        attrs = state.get('attributes') or {}
        if not isinstance(attrs, dict):
            raise DataCorruptionError('SESSION_TASK_LIFECYCLE_CHILD_INVALID')
        if (state.get('id') != child_id or state.get('owner') != receiver.owner_id
                or state.get('parent_id') != task.task_id or state.get('root_id') != task.task_id
                or attrs.get('conversation_thread_id') != thread_id
                or attrs.get('conversation_request_id') != task.conversation_request_id):
            raise DataCorruptionError('SESSION_TASK_LIFECYCLE_CHILD_IDENTITY_INVALID')
    receipt = (store.guidance.ledger.read_receipt(store.storage.guidance_dedupe_path(task.body_dedupe_key))
        if task.body_dedupe_key else None)
    entry = receipt.entry if receipt is not None else None
    facts = entry.metadata if entry is not None else {}
    if (receipt is None or receipt.status != 'consumed' or receipt.dedupe_key != task.body_dedupe_key
            or entry.guidance_id != task.body_guidance_id or entry.target_type != 'thread'
            or entry.target_id != thread_id or entry.sender != task.sender_thread_id
            or facts.get('origin_kind') != 'session_task' or facts.get('session_task_id') != task.task_id
            or facts.get('origin_thread_id') != task.sender_thread_id or not entry.message.strip()):
        raise DataCorruptionError('SESSION_TASK_LIFECYCLE_BODY_NOT_CONSUMED')
    return task, entry.message


# LLM: 仅把原委派任务的稳定回合号投影给原 claim 和后台参数；空表示普通线程，不据此创建任务。
# 函数用途: 让子代理生命周期唤醒继续使用同一接单身份和停止目标。
def delegated_turn_id(store, thread_id):
    task = delegated_session_task(store, thread_id)
    return task.task_id if task is not None else ""


# LLM: 原任务创建后、正文和 wake 发布前写准确 ref，避免后台线程抢先读取未绑定策略；已有绑定只可相同。
# 函数用途: 将新会话任务引用固定到自己的独立线程，不存第二份状态。
def bind_delegated_task(store, thread_id, task_id):
    # LLM: 线程锁内仅合并原策略的任务引用；普通目标无策略零变更，不接受改绑。
    # 函数用途: 保存准确会话任务编号供后续生命周期片读取。
    def bind(current):
        policy = current.metadata.get(IM_TASK_EXECUTION_KEY)
        if policy is None:
            return current
        if policy.get("session_task_id") not in {None, task_id}:
            raise DataCorruptionError("委派任务引用冲突")
        return replace(current, metadata={**current.metadata, IM_TASK_EXECUTION_KEY: {**policy, "session_task_id": task_id}})

    store.threads.update_atomic(thread_id, bind)


# LLM: owner 与来源只读当前原 ThreadStore/home；operation_key 是宿主操作身份，不从自然语言猜，幂等写原线程库。
# 函数用途: 创建隔离后台线程，保留来源、模型及显示档位，不注册用户聊天索引或复制正文。
def ensure_execution_thread(agent: object, source_thread_id: str, operation_key: str, *, kind: str, source_binding: dict | None = None) -> ConversationThread:
    store = agent.conversation_store.threads
    source, error = store.load_report(source_thread_id)
    if error is not None or source is None:
        raise DataCorruptionError("后台执行来源会话不可读")
    home = agent.home_paths
    owner_id, owner_home = str(home.owner_id), str(home.owner_home_dir)
    if ((source.owner_id and source.owner_id != owner_id)
            or (source.owner_home and Path(source.owner_home) != Path(owner_home))):
        raise DataCorruptionError("后台执行来源超出当前用户")
    if kind not in {"scheduled", "delegated"} or not operation_key:
        raise ValueError("后台执行身份无效")
    binding = source_binding
    if binding is None:
        recent = max(source.channel_bindings, key=lambda row: row.last_active_at, default=None)
        if recent is None:
            raise DataCorruptionError("后台执行来源缺少可信通道绑定")
        binding = {"channel": recent.channel, "conversation_id": recent.channel_conversation_id, "actor_id": recent.channel_user_id}
    if not _binding_matches(source, binding):
        raise DataCorruptionError("后台执行来源绑定不一致")
    identity = {"source_thread_id": source.thread_id, "kind": kind, "operation_key": operation_key, "binding": dict(binding)}
    digest = hashlib.sha256(f"{owner_id}\x1f{source.thread_id}\x1f{kind}\x1f{operation_key}".encode()).hexdigest()
    thread_id = f"work_{digest}"
    path = store.storage.thread_path(thread_id)
    with locked_file_transition(path.with_name(f".{thread_id}.execution")):
        existing, error = store.load_report(thread_id)
        if error is not None:
            raise DataCorruptionError("后台执行会话损坏")
        if existing is not None:
            if (existing.channel_bindings or existing.owner_id != owner_id or existing.owner_home != owner_home
                    or existing.metadata.get(EXECUTION_ORIGIN_KEY) != identity):
                raise DataCorruptionError("后台执行会话身份冲突")
            return existing
        current = now()
        thread = ConversationThread(
            thread_id=thread_id, canonical_user_id=source.canonical_user_id,
            owner_id=owner_id, owner_home=owner_home, model_profile_id=source.model_profile_id,
            model_selection_revision=1 if source.model_profile_id else 0,
            model_selection_source="inherited" if source.model_profile_id else "unknown",
            reasoning_effort=source.reasoning_effort, verbose_level=source.verbose_level,
            title=source.title, created_at=current, updated_at=current,
            cwd=owner_home, runtime_workspace_roots=(owner_home,),
            metadata={**inherited_verbose_metadata(source), "thread_kind": "background", EXECUTION_ORIGIN_KEY: identity},
        )
        store.write(thread)
        return thread


# LLM: 只在当前 owner 的线程库解析准确来源；缺失/改归属拒绝回送，不返回跨用户地址或启动聊天。
# 函数用途: 找到后台工作对应的原聊天，供原投递服务读取其鲜活绑定。
def execution_origin_thread(load_thread, thread: ConversationThread) -> ConversationThread:
    reference = (thread.metadata or {}).get(EXECUTION_ORIGIN_KEY)
    if reference is None:
        return thread
    if (not isinstance(reference, dict) or set(reference) != {"source_thread_id", "kind", "operation_key", "binding"}
            or reference["kind"] not in {"scheduled", "delegated"} or not reference["operation_key"]):
        raise DataCorruptionError("后台执行来源引用无效")
    source = load_thread(str(reference["source_thread_id"]))
    if (source is None or source.thread_id == thread.thread_id or source.owner_id != thread.owner_id
            or source.owner_home != thread.owner_home or EXECUTION_ORIGIN_KEY in (source.metadata or {})
            or not _binding_matches(source, reference["binding"])):
        raise DataCorruptionError("后台执行来源引用不再有效")
    return source


# LLM: 通道/地址来自受理时可信绑定；聊天地址仍存在即可，不能因群内后来发送者改变而改投另一人的私聊。
# 函数用途: 验证冻结来源仍属当前原聊天，缺聊天地址时必须精确匹配原用户，不提供其它地址兜底。
def _binding_matches(thread, binding):
    if (not isinstance(binding, dict) or set(binding) != {"channel", "conversation_id", "actor_id"}
            or any(not isinstance(value, str) for value in binding.values()) or not binding["channel"]
            or not (binding["conversation_id"] or binding["actor_id"])):
        return False
    return any(row.channel == binding["channel"] and (
        row.channel_conversation_id == binding["conversation_id"] if binding["conversation_id"] else row.channel_user_id == binding["actor_id"]
    ) for row in thread.channel_bindings)
