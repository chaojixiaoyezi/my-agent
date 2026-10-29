# LLM: 原线程文件锁内核对身份及 Compact/设置 CAS；模型选择变化必须逐次前进且保留显式覆盖版本，不增加状态源。 C显式非阻塞模式只复用原锁，默认调用与权限不变。
# 模块用途: 保存会话元数据、目录与通道绑定；继任以原线程指针原子发表，新线程仅在前后指针成对时可见。
from __future__ import annotations

import copy
import hashlib
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any

from ..gateway_parts.io import (
    locked_file_transition,
    update_json_file_atomic,
    write_json_file_atomic,
)
from ..runtime_errors import DataCorruptionError, runtime_error_report
from .models import (
    ChannelBinding,
    ConversationCompactCommit,
    ConversationThread,
    new_id,
)
from .store_index import _file_fingerprint
from .store_io import (
    now,
    read_json_object_report,
)
from .store_layout import ConversationStorage

# 会话线程元数据进程内缓存的最大条目数。
_THREAD_RECORD_CACHE_MAX_ENTRIES_COUNT = 16384
# 单条会话线程元数据允许进入缓存的最大字节数。
_THREAD_RECORD_CACHE_MAX_RECORD_BYTES = 64 * 1024
# 会话线程元数据进程内缓存允许占用的总字节数上限。
_THREAD_CACHE_MAX_TOTAL_BYTES = 32 * 1024 * 1024
_THREAD_RECORD_CACHE_LOCK = threading.Lock()


# LLM: 仅内存缓存条目，字节来自同次稳定文件指纹，不是精确Python堆占用或新的状态权威。
# 类用途: 保存线程快照、版本指纹及总预算核算用的原始字节数。
@dataclass(frozen=True)
class _CachedThread:
    fingerprint: tuple[int, int, int, int]
    thread: ConversationThread
    size_bytes: int = 0


_THREAD_RECORD_CACHE: OrderedDict[str, _CachedThread] = OrderedDict()
# 线程记录只读缓存当前累计估算字节数，随缓存增删维护。
_THREAD_RECORD_CACHE_TOTAL_BYTES: int = 0


# LLM: frozen线程仍含可变成员；缓存出入均深拷贝字典和字典元组，空字典也新建，不共享调用者对象。
# 函数用途: 克隆线程快照，隔离摘要证据、设置、诊断、宿主通知和会话方法账本。
def _clone_cached_thread(thread: ConversationThread) -> ConversationThread:
    return replace(
        thread,
        compact_operation_evidence=copy.deepcopy(thread.compact_operation_evidence) if thread.compact_operation_evidence else {},
        provider_context_observation=copy.deepcopy(thread.provider_context_observation) if thread.provider_context_observation else {},
        model_context_usage=copy.deepcopy(thread.model_context_usage) if thread.model_context_usage else {},
        learning_source=copy.deepcopy(thread.learning_source) if thread.learning_source else {},
        metadata=copy.deepcopy(thread.metadata) if thread.metadata else {},
        decision_settings=copy.deepcopy(thread.decision_settings) if thread.decision_settings else {},
        model_metrics=copy.deepcopy(thread.model_metrics) if thread.model_metrics else {},
        pending_host_notices=copy.deepcopy(thread.pending_host_notices),
        conversation_methods=copy.deepcopy(thread.conversation_methods),
    )


# LLM: 单条64KiB/总量32MiB和条数限制不变；增减字节只在同一缓存锁内进行，空表时校准归零以兼容外部直接clear，覆盖/淘汰不遍历全表。
# 函数用途: 保存独立线程快照，以O(1)更新预算并按需淘汰最旧记录。
def _store_cached_thread(
    path: Path, fingerprint: tuple[int, int, int, int] | None, thread: ConversationThread,
) -> None:
    global _THREAD_RECORD_CACHE_TOTAL_BYTES
    if fingerprint is None or fingerprint[1] > _THREAD_RECORD_CACHE_MAX_RECORD_BYTES:
        _drop_cached_thread(path)
        return
    snapshot = _clone_cached_thread(thread)
    key = str(path)
    with _THREAD_RECORD_CACHE_LOCK:
        if not _THREAD_RECORD_CACHE:
            _THREAD_RECORD_CACHE_TOTAL_BYTES = 0
        previous = _THREAD_RECORD_CACHE.pop(key, None)
        if previous is not None:
            _THREAD_RECORD_CACHE_TOTAL_BYTES -= previous.size_bytes
        _THREAD_RECORD_CACHE[key] = _CachedThread(fingerprint, snapshot, fingerprint[1])
        _THREAD_RECORD_CACHE_TOTAL_BYTES += fingerprint[1]
        while _THREAD_RECORD_CACHE and (
            len(_THREAD_RECORD_CACHE) > _THREAD_RECORD_CACHE_MAX_ENTRIES_COUNT
            or _THREAD_RECORD_CACHE_TOTAL_BYTES > _THREAD_CACHE_MAX_TOTAL_BYTES
        ):
            _key, removed = _THREAD_RECORD_CACHE.popitem(last=False)
            _THREAD_RECORD_CACHE_TOTAL_BYTES -= removed.size_bytes


# LLM: 写入、超大记录、空记录与读取失败都丢弃旧投影；同锁扣除实际移除的字节，不更改原错误。
# 函数用途: 在线程缓存锁内幂等移除指定路径并维护预算。
def _drop_cached_thread(path: Path) -> None:
    global _THREAD_RECORD_CACHE_TOTAL_BYTES
    with _THREAD_RECORD_CACHE_LOCK:
        previous = _THREAD_RECORD_CACHE.pop(str(path), None)
        if previous is not None:
            _THREAD_RECORD_CACHE_TOTAL_BYTES -= previous.size_bytes


# LLM: 仅清理进程内投影，同锁重置字节总额；不删除任何canonical线程或改变读取错误合同。
# 函数用途: 清空线程缓存时同步归零预算，供缓存生命周期和测试使用。
def _clear_cached_threads() -> None:
    global _THREAD_RECORD_CACHE_TOTAL_BYTES
    with _THREAD_RECORD_CACHE_LOCK:
        _THREAD_RECORD_CACHE.clear()
        _THREAD_RECORD_CACHE_TOTAL_BYTES = 0


# LLM: 默认克隆保持公开隔离；clone=False仅供模块内只读筛选排序，返回前必须克隆最终切片，错误及磁盘校验不变。
# 函数用途: 读取稳定线程快照，内部列表扫描可避免对不会返回的线程深拷贝。
def _read_thread_record_report(
    path: Path, *, thread_id: str = "", clone: bool = True,
) -> tuple[ConversationThread | None, dict[str, Any] | None]:
    actual_thread_id = str(thread_id or path.stem)
    before_fp = _file_fingerprint(path)
    cached_thread = None
    if before_fp is not None and before_fp[1] <= _THREAD_RECORD_CACHE_MAX_RECORD_BYTES:
        with _THREAD_RECORD_CACHE_LOCK:
            cached = _THREAD_RECORD_CACHE.get(str(path))
            if cached is not None and cached.fingerprint == before_fp and cached.thread.thread_id == actual_thread_id:
                _THREAD_RECORD_CACHE.move_to_end(str(path))
                cached_thread = cached.thread
    if cached_thread is not None:
        return (_clone_cached_thread(cached_thread) if clone else cached_thread), None
    payload, error = read_json_object_report(path, context="conversation.thread.read")
    if error is not None:
        _drop_cached_thread(path)
        error["thread_id"] = actual_thread_id
        return None, error
    if not payload:
        _drop_cached_thread(path)
        return None, None
    try:
        thread = ConversationThread.from_dict(payload)
        if not thread.thread_id or thread.thread_id != actual_thread_id:
            raise DataCorruptionError(f"conversation thread identity is invalid: {actual_thread_id}")
        after_fp = _file_fingerprint(path)
        if (before_fp is not None and after_fp == before_fp
                and before_fp[1] <= _THREAD_RECORD_CACHE_MAX_RECORD_BYTES):
            _store_cached_thread(path, after_fp, thread)
            return (_clone_cached_thread(thread) if clone else thread), None
        _drop_cached_thread(path)
        return thread, None
    except Exception as exc:
        _drop_cached_thread(path)
        report = runtime_error_report(exc, context="conversation.thread.read")
        report["thread_id"] = actual_thread_id
        report["path"] = str(path)
        return None, report


# LLM: 只校验当前原子事务的选择变化，不提供授权、采用或重试；元数据/Compact 普通更新保持选择事实原样。
# 函数用途: 阻止旧模型版本覆盖新选择，保证引用、来源和最近显式覆盖事实在同次线程写入中前进。
def _validate_model_selection_update(previous: ConversationThread, updated: ConversationThread) -> None:
    fields = ("model_profile_id", "model_selection_revision", "model_selection_source", "model_selection_last_explicit_revision")
    if all(getattr(previous, name) == getattr(updated, name) for name in fields):
        return
    if updated.model_selection_revision != previous.model_selection_revision + 1:
        raise DataCorruptionError("会话模型选择版本必须从当前版本前进一步")
    expected_explicit = (updated.model_selection_revision if updated.model_selection_source == "explicit"
                         else previous.model_selection_last_explicit_revision)
    if updated.model_selection_last_explicit_revision != expected_explicit:
        raise DataCorruptionError("会话模型选择不能改写已发生的显式覆盖事实")
    if updated.model_selection_source in {"default", "inherited"} and previous.model_profile_id:
        raise DataCorruptionError("已有模型选择不能重新声明为初始绑定")


# LLM: 存储命令可能收到列表或元组，必须先规范化再比较准确线程作用域，防止序列化形状伪造任务谱系变化。
# 函数用途: 将会话运行根目录整理成有序、去空、去重的不可变列表。
def normalized_runtime_workspace_roots(value: object) -> tuple[str, ...]:
    items = value if isinstance(value, (list, tuple)) else ()
    return tuple(dict.fromkeys(str(item).strip() for item in items if str(item or "").strip()))


# LLM: store_threads 的持久化合同：按通道、会话和用户生成原有精确索引键，保持分隔符和顺序；修改须同步本领域调用方与存储回归。
# 函数用途: 按通道、会话和用户生成原有精确索引键，保持分隔符和顺序。
def _binding_key(channel: str, conversation_id: str, user_id: str) -> str:
    return "\x1f".join([str(channel), str(conversation_id), str(user_id)])


# LLM: store_threads 的持久化合同：从请求构造通道绑定记录，不写索引或改变已有会话；修改须同步本领域调用方与存储回归。
# 函数用途: 从请求构造通道绑定记录，不写索引或改变已有会话。
def _channel_binding(thread_id: str, current: float, kwargs: dict) -> ChannelBinding:
    return ChannelBinding(
        channel=kwargs.get("channel", ""),
        channel_conversation_id=kwargs.get("channel_conversation_id", ""),
        channel_user_id=kwargs.get("channel_user_id", ""),
        canonical_user_id=kwargs.get("canonical_user_id", ""),
        thread_id=thread_id,
        last_active_at=current,
    )


# LLM: store_threads 的持久化合同：仅替换身份相同的绑定，保留其它通道连接；修改须同步本领域调用方与存储回归。
# 函数用途: 仅替换身份相同的绑定，保留其它通道连接。
def _replace_binding(
    thread: ConversationThread, binding: ChannelBinding
) -> tuple[ChannelBinding, ...]:
    key = _binding_key(binding.channel, binding.channel_conversation_id, binding.channel_user_id)
    old = (
        item
        for item in thread.channel_bindings
        if _binding_key(item.channel, item.channel_conversation_id, item.channel_user_id) != key
    )
    return (*old, binding)


# LLM: C首次模型元数据仅显式blocking=False透传原锁链；默认True不改调用形状、规范路径、创建/绑定次序或CAS。
# LLM: 仅由持有同通道绑定事务锁的 store 入口调用；显式 reuse_latest 保留原语义，不按模型名猜会话。
# 函数用途: 在短事务内查找、显式复用或创建会话并提交索引，不持有模型执行锁。
def _get_or_create_bound_thread(store, request: dict, *, blocking: bool = True) -> ConversationThread:
    existing, binding_error = store.resolve_report(
        channel=request.get("channel", ""),
        channel_conversation_id=request.get("channel_conversation_id", ""),
        channel_user_id=request.get("channel_user_id", ""),
    )
    if binding_error is not None:
        raise DataCorruptionError(str(binding_error))
    if existing is not None:
        return store._bind_existing(existing.thread_id, request, **({} if blocking else {"blocking": False}))
    latest = None
    if request.get("reuse_latest_for_user"):
        latest, latest_error = store.latest_for_user_report(request.get("canonical_user_id", ""))
        if latest_error is not None:
            raise DataCorruptionError(str(latest_error))
    if latest is not None:
        return store._bind_existing(latest.thread_id, request, **({} if blocking else {"blocking": False}))
    return store._create(request, **({} if blocking else {"blocking": False}))


# LLM: 原文件事务内校验身份与选择版本；omit_empty 的已知字段清空时移除旧键，未知扩展仍保持原样。
#   由 ThreadStore.update_atomic 在原锁内调用；不读写独立状态，不让扩展合并复活已移除的会话方法。联测 store/commands。
# 函数用途: 校验并合并线程更新；拒绝迟到目录版本，模型选择时间仅随原选择版本提交。
# LLM: updater与B1公共修订/原回执合并同在canonical文件锁内；未接入插件的线程不增加元数据或改变旧写入规则。
# 函数用途: 合并原子线程更新并保住既有宿主CAS与插件幂等字段。
def _apply_thread_update(payload: dict, thread_id: str, updater: Callable) -> dict:
    thread = ConversationThread.from_dict(payload)
    if not thread.thread_id:
        raise DataCorruptionError(f"conversation thread is unreadable: {thread_id}")
    if thread.thread_id != thread_id:
        raise DataCorruptionError(f"conversation thread identity is invalid: {thread_id}")
    before = copy.deepcopy(thread.to_dict()) if "plugin_host_v2" in thread.metadata else None
    updated = updater(thread)
    if updated.thread_id != thread_id:
        raise DataCorruptionError(f"conversation thread updater changed identity: {thread_id}")
    _validate_model_selection_update(thread, updated)
    updated = _validate_workspace_update(thread, updated)
    updated = replace(updated, model_selection_selected_at=(
        now() if updated.model_selection_revision != thread.model_selection_revision else thread.model_selection_selected_at
    ))
    updated = _preserve_plugin_thread_state(before, updated)
    serialized = updated.to_dict()
    omitted = {item.name for item in fields(updated) if item.metadata.get("omit_empty") and item.name not in serialized}
    return {**{key: value for key, value in payload.items() if key not in omitted}, **serialized}


# LLM: 在原线程文件锁中统一守住目录CAS，兼容旧入口不显式递增的最新状态更新；不授权路径或新建索引。
# 函数用途: 拒绝回退/跳跃的目录修订，并为实际目录变化递增版本。
def _validate_workspace_update(previous: ConversationThread, updated: ConversationThread) -> ConversationThread:
    if updated.revision < previous.revision or updated.revision > previous.revision + 1:
        raise RuntimeError("WORKSPACE_REVISION_CONFLICT")
    fields = ("cwd", "workspace_root", "runtime_workspace_roots")
    if updated.revision == previous.revision and any(getattr(previous, name) != getattr(updated, name) for name in fields):
        return replace(updated, revision=previous.revision + 1)
    return updated


# LLM: 只对已登记插件状态启用且revision不得缺省；普通metadata替换保留来源/回执，实际变更单步推进原修订。
# 函数用途: 防止原宿主入口清除幂等回执或让旧插件修订继续有效。
def _preserve_plugin_thread_state(before: dict | None, updated: ConversationThread) -> ConversationThread:
    if before is None:
        return updated
    previous = before["metadata"]["plugin_host_v2"]
    current = updated.metadata.get("plugin_host_v2", previous)
    if not isinstance(previous, dict) or not isinstance(current, dict):
        raise DataCorruptionError("invalid plugin conversation state")
    for key in ("installation_ref", "workspace_ref"):
        if key in current and current[key] != previous.get(key):
            raise DataCorruptionError("plugin conversation source changed")
    old_receipts, new_receipts = previous.get("receipts", {}), current.get("receipts", {})
    if (not isinstance(old_receipts, dict) or not isinstance(new_receipts, dict)
            or any(key in new_receipts and new_receipts[key] != value for key, value in old_receipts.items())):
        raise DataCorruptionError("plugin conversation receipt changed")
    revision = previous.get("revision")
    if type(revision) is not int or revision < 1:
        raise DataCorruptionError("invalid plugin conversation revision")
    state = {**previous, **current, "receipts": {**old_receipts, **new_receipts},
             "revision": revision + (updated.to_dict() != before)}
    return replace(updated, metadata={**updated.metadata, "plugin_host_v2": state})


# LLM: 活动冲突仅表示旧闲置候选让位，不计模型失败；供原CAS与闲置维护做类型化处理。
# 类用途: 表示用户发言或同代闲置压缩已经完成。
class IdleCompactActivityChanged(RuntimeError):
    pass


# LLM: 原线程元数据与通道绑定唯一存储；身份校验与扩展合并在类外小函数，仍在同一文件锁内守住 CAS，不建立第二状态源。
# 类用途: 保存线程、目录与通道绑定，防止迟到更新回退模型/目录；默认模型只初始化新线程。
class ThreadStore:
    # LLM: /new 只创建空白线程并发表同owner/同绑定继任；不关闭TaskLink、Goal、子代理或托管进程。
    # 函数用途: 在原通道事务与线程原子写内继任，重复旧ID返回已发表的最新后继。
    def succeed(self, thread_id: str, *, title: str = "", binding: ChannelBinding | None = None, validate=None) -> ConversationThread:
        return _succeed_thread(self, thread_id, title=title, binding=binding, validate=validate)

    # LLM: 同一 ConversationStorage 与默认解析器由组装入口注入；构造不读写线程或通道索引。
    # 函数用途: 绑定线程文件目录和新会话模型来源，已有会话不会因构造重新选择模型。
    def __init__(
        self, storage: ConversationStorage, *, model_default: Callable[[], str] | None = None
    ) -> None:
        self.storage = storage
        self.model_default = model_default

    # LLM: 创建无通道绑定的主线程，沿原原子创建钩子；上层负责授权，索引不会切走源会话。
    # 函数用途: 创建目录独立线程并冻结默认模型，不创建项目文件。
    def create(self, *, canonical_user_id: str = "", title: str = "", workspace_root: str = "",
               cwd: str = "", owner_id: str = "", owner_home: str = "",
               runtime_workspace_roots: tuple[str, ...] = ()) -> ConversationThread:
        return _create_workspace_thread(self, canonical_user_id=canonical_user_id, title=title,
            workspace_root=workspace_root, cwd=cwd, owner_id=owner_id, owner_home=owner_home,
            runtime_workspace_roots=runtime_workspace_roots)

    # LLM: 上层负责owner授权；此入口检查现存目录，在原锁内核目录revision，不修改其它线程或索引。
    # 函数用途: 同时切换cwd、workspace_root与运行根，迟到目录更新明确报冲突。
    def switch_workspace(self, thread_id: str, workspace_dir: str, *,
                         expected_revision: int | None = None) -> ConversationThread:
        return _switch_thread_workspace(self, thread_id, workspace_dir, expected_revision=expected_revision)

    # LLM: C首次模型元数据仅显式blocking=False透传原锁链；默认True不改调用形状、规范路径、创建/绑定次序或CAS。
    # LLM: 通道绑定的查找与创建共用短事务锁；不能与单文件索引锁同路径，不锁模型执行或串行不同会话。
    # 函数用途: 两个窗口首次同时打开同一 session 时只生成一个 thread，避免之后模型选择和消息各走一份。
    def get_or_create(self, request: dict, *, blocking: bool = True) -> ConversationThread:
        identity = _binding_key(
            request.get("channel", ""),
            request.get("channel_conversation_id", ""),
            request.get("channel_user_id", ""),
        )
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        with locked_file_transition(self.storage.threads_dir / f".channel-{digest}", **({} if blocking else {"blocking": False})):
            return _get_or_create_bound_thread(self, request, **({} if blocking else {"blocking": False}))

    # LLM: 此适配器保持精简；准确身份的创建及冲突检查属于 agent_thread_store，通道绑定存储不得吸收代理运行时策略。
    # 函数用途: 为一个确定的子代理运行创建或校验独立会话线程，不把它绑定成用户聊天会话。
    def ensure_agent(self, request: dict) -> ConversationThread:
        from .agent_thread_store import ensure_agent_thread_record

        return ensure_agent_thread_record(self, request)

    # LLM: store_threads 的持久化合同：读取精确通道绑定对应线程，延续忽略读取错误的便利接口；修改须同步本领域调用方与存储回归。
    # 函数用途: 读取精确通道绑定对应线程，延续忽略读取错误的便利接口。
    def resolve(
        self, *, channel: str, channel_conversation_id: str, channel_user_id: str
    ) -> ConversationThread | None:
        thread, _load_error = self.resolve_report(
            channel=channel,
            channel_conversation_id=channel_conversation_id,
            channel_user_id=channel_user_id,
        )
        return thread

    # LLM: store_threads 的持久化合同：通过绑定索引加载精确线程，同时返回索引或线程读取错误；修改须同步本领域调用方与存储回归。
    # 函数用途: 通过绑定索引加载精确线程并解析该绑定的后继；其它通道不跟随，坏链明确返回错误。
    def resolve_report(
        self,
        *,
        channel: str,
        channel_conversation_id: str,
        channel_user_id: str,
    ) -> tuple[ConversationThread | None, dict[str, Any] | None]:
        bindings, error = self._read_bindings_report()
        if error is not None:
            return None, error
        thread_id = str(
            bindings.get(_binding_key(channel, channel_conversation_id, channel_user_id)) or ""
        )
        thread, error = self.load_report(thread_id) if thread_id else (None, None)
        if error is None and thread is not None:
            scope = [channel, channel_user_id, channel_conversation_id]
            try:
                if _succession_target(thread, scope):
                    thread = self.require(resolve_thread_successor_chain(self, thread_id, binding=scope))
            except (ValueError, KeyError) as exc:
                return None, runtime_error_report(exc, context="conversation.thread.successor")
        return thread, error

    # LLM: store_threads 的持久化合同：按用户最近线程索引加载原记录，返回坏账而不猜测替代线程；修改须同步本领域调用方与存储回归。
    # 函数用途: 按用户最近线程索引加载原记录，返回坏账而不猜测替代线程。
    def latest_for_user_report(
        self, canonical_user_id: str
    ) -> tuple[ConversationThread | None, dict[str, Any] | None]:
        payload, error = read_json_object_report(
            self.storage.user_latest_path,
            context="conversation.user_latest.read",
        )
        if error is not None:
            return None, error
        thread_id = str(payload.get(canonical_user_id) or "")
        return self.load_report(thread_id) if thread_id else (None, None)

    # LLM: C首次模型元数据仅显式blocking=False透传原锁链；默认True不改调用形状、规范路径、创建/绑定次序或CAS。
    # LLM: 通道绑定更新必须原子合并到最新持久线程，防止迟到的适配请求回退压缩状态、任务索引或固定工作区。
    # initialize_workspace 仅补齐空目录，必须在同一原子更新中判断，不能覆盖并发绑定或既有执行目录。
    # 函数用途: 刷新通道与目录绑定并保留并发状态，只在实际改目录时前进目录修订。
    def bind_channel(self, request: dict, *, blocking: bool = True) -> ConversationThread:
        thread_id = str(request.get("thread_id") or "")
        self.require(thread_id)
        current = now(request.get("now"))
        binding = _channel_binding(thread_id, current, request)
        requested_cwd = str(request.get("cwd") or "").strip()
        raw_runtime_roots = request.get("runtime_workspace_roots")
        requested_runtime_roots = tuple(
            str(item)
            for item in (raw_runtime_roots if isinstance(raw_runtime_roots, (list, tuple)) else [])
            if str(item or "").strip()
        )
        updated = self.update_atomic(
            thread_id,
            lambda latest: replace(
                latest,
                canonical_user_id=(request.get("canonical_user_id") or latest.canonical_user_id),
                owner_id=str(request.get("owner_id") or latest.owner_id or ""),
                owner_home=str(request.get("owner_home") or latest.owner_home or ""),
                channel_bindings=_replace_binding(latest, binding),
                cwd=(
                    latest.cwd
                    if request.get("initialize_workspace") and latest.cwd
                    else requested_cwd or latest.cwd
                ),
                workspace_root=(latest.workspace_root if request.get("initialize_workspace") and latest.cwd
                                else str(request.get("workspace_root") or requested_cwd or latest.workspace_root)),
                revision=latest.revision + bool(requested_cwd and requested_cwd != latest.cwd
                                               and not (request.get("initialize_workspace") and latest.cwd)),
                runtime_workspace_roots=(
                    latest.runtime_workspace_roots
                    if request.get("initialize_workspace") and latest.cwd
                    else requested_runtime_roots or latest.runtime_workspace_roots
                ),
                updated_at=max(latest.updated_at, current),
            ),
            **({} if blocking else {"blocking": False}),
        )
        self._write_binding_indexes(binding, **({} if blocking else {"blocking": False}))
        return updated

    # LLM: store_threads 的持久化合同：返回按活动时间排序的线程列表，需要坏账信息时调用 list_report；修改须同步本领域调用方与存储回归。
    # 函数用途: 返回活动时间排序的线程，目录过滤先于limit；读取错误仍交list_report。
    def list(self, *, limit: int = 100, workspace_dir: str | None = None) -> list[ConversationThread]:
        threads, _load_errors = self.list_report(limit=limit, workspace_dir=workspace_dir)
        return threads

    # LLM: 公开名称与原list共享只读扫描，不产生另一套索引。
    # 函数用途: 为目录会话调用方按工作区列出线程。
    def list_threads(self, *, limit: int = 100, workspace_dir: str | None = None) -> list[ConversationThread]:
        return self.list(limit=limit, workspace_dir=workspace_dir)

    # LLM: store_threads 的持久化合同：扫描原线程目录并汇总读取错误，只排序投影，不修复或写盘；修改须同步本领域调用方与存储回归。
    # 函数用途: 扫描原线程，在limit前按规范目录筛选并保留全部坏账，不修复或写盘。
    def list_report(
        self, *, limit: int = 100, workspace_dir: str | None = None
    ) -> tuple[list[ConversationThread], list[dict[str, Any]]]:
        return _workspace_thread_report(self, limit=limit, workspace_dir=workspace_dir)

    # LLM: 摘要更新只拥有摘要及活动时间，不能用陈旧完整线程覆盖最新记录。
    # 函数用途: 原子更新会话摘要，不覆盖同时发生的任务目录、压缩游标或通道变化。
    def update_summary(
        self, thread_id: str, summary: str, *, now: float | None = None
    ) -> ConversationThread:
        current = now if now is not None else time.time()
        return self.update_atomic(
            thread_id,
            lambda latest: replace(
                latest,
                summary=summary,
                updated_at=max(latest.updated_at, current),
            ),
        )

    # LLM: 用户活动独立于updated_at；同请求去重，原线程锁内只更新活动字段，不把后台元数据当新输入。
    # 函数用途: 登记真实用户发言时间和单调代次，供闲置压缩提交核验。
    def record_user_activity(self, thread_id: str, *, request_id: str = "", now: float | None = None) -> ConversationThread:
        current = float(time.time() if now is None else now)

        # LLM: 同锁合并最新metadata，保留设置、摘要及并发信息；重复请求不重置计时。
        # 函数用途: 为一次新的用户请求递增活动代次。
        def apply(thread):
            metadata = dict(thread.metadata or {})
            if request_id and metadata.get("last_user_activity_request_id") == request_id:
                return thread
            generation = metadata.get("user_activity_generation", 0)
            generation = generation if type(generation) is int and generation >= 0 else 0
            metadata.update(last_user_activity_at=current, user_activity_generation=generation + 1,
                            last_user_activity_request_id=request_id)
            return replace(thread, metadata=metadata, updated_at=max(thread.updated_at, current))

        return self.update_atomic(thread_id, apply)

    # LLM: 唯一CAS推进提交链；闲置按append锁→线程锁核指纹及活动代；普通提交只保存摘要前消息观察供去重，不加新CAS。
    # 函数用途: 原子提交压缩与可选活跃工具收缩；局部视图只沿同链读取，不另建任务摘要表，也不改原始消息。
    def update_compact_state(
        self, thread_id: str, *, commit: ConversationCompactCommit, expected_generation: int,
        active_loaded_tool_names: tuple[str, ...] | None = None, now: float | None = None,
        baseline_loaded_tool_names: tuple[str, ...] | None = None,
        active_tool_selector: Callable[[ConversationThread], tuple[str, ...]] | None = None,
        expected_user_activity_generation: int | None = None,
        expected_message_tail: dict[str, Any] | None = None,
        idle_compact: bool = False,
    ) -> ConversationThread:
        """Atomically advance one validated summary/checkpoint without touching raw messages."""
        if type(commit.publish_thread_view) is not bool:
            raise ValueError("compact publication scope must be an explicit boolean")
        if idle_compact and (type(expected_user_activity_generation) is not int or expected_user_activity_generation < 0):
            raise ValueError("idle compact requires a user activity generation")
        if idle_compact and not isinstance(expected_message_tail, dict):
            raise ValueError("idle compact requires a canonical message tail marker")
        current = now if now is not None else time.time()

        # LLM: 先检查generation，再以同锁最新线程合并发现差集；局部摘要不成为全局权限。
        # 函数用途: 同锁提交唯一head；局部压缩保留全线程摘要时间和游标，防止其它任务跳过未覆盖原文。
        def apply(thread: ConversationThread) -> ConversationThread:
            if thread.compact_generation != expected_generation:
                if idle_compact:
                    raise IdleCompactActivityChanged("conversation user activity or compact generation superseded idle summary")
                raise RuntimeError("conversation compact generation changed while summary was being prepared")
            metadata = dict(thread.metadata or {})
            if expected_user_activity_generation is not None:
                if (type(expected_user_activity_generation) is not int
                        or type(metadata.get("user_activity_generation", 0)) is not int
                        or metadata.get("user_activity_generation", 0) != expected_user_activity_generation):
                    raise IdleCompactActivityChanged("conversation user activity changed while idle summary was being prepared")
                if idle_compact:
                    previous = metadata.get("idle_auto_compact", {})
                    if (previous.get("last_idle_compacted_activity_gen") == expected_user_activity_generation
                            and previous.get("message_tail") == expected_message_tail):
                        raise IdleCompactActivityChanged("conversation user activity already idle compacted")
            from .tool_discovery import compact_discovered_tools
            # selector仅计算展示名，不允许I/O；旧tuple调用方维持原合同。
            active = active_tool_selector(thread) if active_tool_selector is not None else active_loaded_tool_names
            tool_updates = compact_discovered_tools(thread, active, baseline_loaded_tool_names=baseline_loaded_tool_names)
            metadata.update(tool_updates.pop("metadata", {}))
            metadata["last_compacted_user_activity_gen"] = metadata.get("user_activity_generation", 0)
            metadata.pop("last_compacted_message_tail", None)
            if expected_message_tail is not None:
                metadata["last_compacted_message_tail"] = dict(expected_message_tail)
            if idle_compact:
                metadata["last_idle_compacted_activity_gen"] = expected_user_activity_generation
                metadata["idle_auto_compact"] = {
                    "last_idle_compacted_activity_gen": expected_user_activity_generation,
                    "last_idle_compacted_message_id": expected_message_tail["last_message_id"],
                    "last_idle_compacted_message_count": expected_message_tail["message_count"],
                    "message_tail": dict(expected_message_tail),
                }
            import hashlib

            metadata['compact_commit_boundary'] = {
                'checkpoint_id': str(commit.checkpoint_id), 'generation': thread.compact_generation + 1,
                'boundary_message_id': str(commit.compacted_through_message_id) if commit.publish_thread_view else thread.compacted_through_message_id,
                'summary_sha256': hashlib.sha256((str(commit.summary).strip() if commit.publish_thread_view else thread.summary).encode('utf-8')).hexdigest(),
            }
            return replace(
                thread, **tool_updates,
                summary=str(commit.summary).strip() if commit.publish_thread_view else thread.summary,
                compact_operation_evidence=(dict(commit.operation_evidence) if commit.publish_thread_view
                                            else thread.compact_operation_evidence),
                compacted_through_message_id=(str(commit.compacted_through_message_id) if commit.publish_thread_view
                                             else thread.compacted_through_message_id),
                compacted_through_byte_offset=(max(0, int(commit.compacted_through_byte_offset))
                                              if commit.publish_thread_view else thread.compacted_through_byte_offset),
                compact_generation=thread.compact_generation + 1,
                compact_updated_at=current if commit.publish_thread_view else thread.compact_updated_at,
                compact_source_messages=max(0, int(commit.source_messages)),
                compact_source_tool_pairs=max(0, int(commit.source_tool_pairs)),
                compact_checkpoint_id=str(commit.checkpoint_id),
                compact_consecutive_failures=0,
                compact_failure_updated_at=0.0,
                compact_failure_code="",
                compact_vision_failed_generation=-1,
                provider_context_observation={},
                model_context_usage={},
                metadata=metadata,
                updated_at=current,
            )

        from ..common.json_io import locked_json_path
        from .message_scan import recover_transcript_transition, rotate_committed_transcript
        from .store_messages import canonical_message_tail_marker

        path = self.storage.message_path(thread_id)
        with locked_json_path(path):
            recover_transcript_transition(path)
            if idle_compact:
                try:
                    unchanged = canonical_message_tail_marker(path, thread_id) == expected_message_tail
                except (OSError, ValueError, TypeError, DataCorruptionError) as exc:
                    raise IdleCompactActivityChanged("canonical messages unreadable before idle commit") from exc
                if not unchanged:
                    raise IdleCompactActivityChanged("canonical messages changed before idle commit")
            updated = self.update_atomic(thread_id, apply)
            if commit.publish_thread_view:
                try:
                    rotate_committed_transcript(self.storage, updated)
                except (OSError, ValueError, TypeError, DataCorruptionError) as exc:
                    # 已发布CAS不可伪装失败；热文件或prepared布局仍完整可读，显式留下维护待重试事实。
                    error_type = type(exc).__name__
                    # LLM: 只给同一获胜head记录维护错误，不覆盖后来提交，也不再次推进generation。
                    # 函数用途: 将冷转I/O故障与已成功摘要提交分开报告。
                    def maintenance_pending(latest):
                        if latest.compact_checkpoint_id != updated.compact_checkpoint_id:
                            return latest
                        metadata = dict(latest.metadata)
                        metadata['compact_transcript_maintenance'] = {
                            'status': 'pending', 'error_type': error_type,
                            'checkpoint_id': updated.compact_checkpoint_id,
                        }
                        return replace(latest, metadata=metadata)

                    updated = self.update_atomic(thread_id, maintenance_pending)
            return updated

    # LLM: 供应商用量校准只是少量数值线程遥测，以压缩代为CAS边界；模型活动不是用户可见活跃时间，因此不得推进 updated_at。
    # 压缩提交在自己的原子转换中清除该字段。
    # 函数用途: 保存一次供应商真实输入量校准；若期间已经压缩会话则放弃旧观测，不改变会话最近活跃时间。
    def update_provider_context_observation(
        self,
        thread_id: str,
        observation: dict[str, Any],
        *,
        expected_compact_generation: int,
    ) -> ConversationThread:
        payload = dict(observation) if isinstance(observation, dict) else {}

        # LLM: store_threads 的持久化合同：只在原 Compact 代保存输入量校准，不修改会话活动时间；修改须同步本领域调用方与存储回归。
        # 函数用途: 只在原 Compact 代保存输入量校准，不修改会话活动时间。
        def apply(thread: ConversationThread) -> ConversationThread:
            if thread.compact_generation != max(0, int(expected_compact_generation)):
                return thread
            return replace(thread, provider_context_observation=payload)

        return self.update_atomic(thread_id, apply)

    # LLM: 失败只改原熔断账，不动摘要/游标/检查点；闲置按消息追加锁→线程锁核消息尾、活动及压缩代，竞争原样上抛。
    # 函数用途: 原子记录仍属于本次基线的失败；闲置旧候选让位，普通压缩保留原代数检查合同。
    def record_compact_failure(
        self,
        thread_id: str,
        *,
        failure_code: str,
        expected_generation: int,
        now: float | None = None,
        expected_user_activity_generation: int | None = None,
        expected_message_tail: dict[str, Any] | None = None,
    ) -> ConversationThread:
        current = now if now is not None else time.time()
        guarded = expected_user_activity_generation is not None or expected_message_tail is not None
        if guarded and (type(expected_user_activity_generation) is not int or expected_user_activity_generation < 0
                        or not isinstance(expected_message_tail, dict)):
            raise IdleCompactActivityChanged("idle failure recording requires a valid activity and message baseline")

        # LLM: 消息锁仍持有时进入原线程原子更新，核最新活动及压缩代；既有失败不得因旧闲置候选被增减。
        # 函数用途: 只给当前有效基线增加一次失败，竞争则抛专用让位异常。
        def apply(thread: ConversationThread) -> ConversationThread:
            if thread.compact_generation != expected_generation:
                if guarded:
                    raise IdleCompactActivityChanged("compact generation changed before idle failure recording")
                return thread
            if guarded:
                activity = (thread.metadata or {}).get("user_activity_generation", 0)
                if type(activity) is not int or activity != expected_user_activity_generation:
                    raise IdleCompactActivityChanged("user activity changed before idle failure recording")
            return replace(
                thread,
                compact_consecutive_failures=thread.compact_consecutive_failures + 1,
                compact_failure_updated_at=current,
                compact_failure_code=str(failure_code or "COMPACT_FAILED"),
                updated_at=current,
            )

        if guarded:
            from ..common.json_io import locked_json_path
            from .store_messages import canonical_message_tail_marker

            path = self.storage.message_path(thread_id)
            with locked_json_path(path):
                try:
                    unchanged = canonical_message_tail_marker(path, thread_id) == expected_message_tail
                except (OSError, ValueError, TypeError, DataCorruptionError) as exc:
                    raise IdleCompactActivityChanged("canonical messages unreadable before idle failure recording") from exc
                if not unchanged:
                    raise IdleCompactActivityChanged("canonical messages changed before idle failure recording")
                return self.update_atomic(thread_id, apply)
        return self.update_atomic(thread_id, apply)

    # LLM: 详细输出级别是线程本地控制字段，必须只原子更新它本身，不覆盖其它线程状态。
    # 函数用途: 修改当前会话的详细输出级别，不让旧会话快照覆盖其他状态。
    def update_verbose_level(
        self,
        thread_id: str,
        level: str,
        *,
        now: float | None = None,
    ) -> ConversationThread:
        normalized = str(level or "off").strip().lower()
        if normalized not in {"off", "on", "full", "stream"}:
            raise ValueError("verbose level must be one of: off, on, full, stream")
        current = now if now is not None else time.time()
        return self.update_atomic(
            thread_id,
            lambda latest: replace(
                latest,
                verbose_level=normalized,
                updated_at=max(latest.updated_at, current),
            ),
        )

    # LLM: store_threads 的持久化合同：按线程身份读回记录，需要错误明细时调用 load_report；修改须同步本领域调用方与存储回归。
    # 函数用途: 按线程身份读回记录，需要错误明细时调用 load_report。
    def load(self, thread_id: str) -> ConversationThread | None:
        thread, _load_error = self.load_report(thread_id)
        return thread

    # LLM: store_threads 的持久化合同：读取精确线程文件并返回结构化错误，空身份保持空结果；修改须同步本领域调用方与存储回归。
    # 函数用途: 读取精确线程文件并返回结构化错误，空身份保持空结果。
    def load_report(
        self, thread_id: str
    ) -> tuple[ConversationThread | None, dict[str, Any] | None]:
        if not thread_id:
            return None, None
        return self._load_path_report(self.storage.thread_path(thread_id), thread_id=thread_id)

    # LLM: store_threads 的持久化合同：读取必需线程，不存在时抛出原 KeyError，供跨领域写入校验；修改须同步本领域调用方与存储回归。
    # 函数用途: 读取必需线程，不存在时抛出原 KeyError，供跨领域写入校验。
    def require(self, thread_id: str) -> ConversationThread:
        thread = self.load(thread_id)
        if thread is None:
            raise KeyError(f"unknown conversation thread: {thread_id}")
        return thread

    # LLM: C首次模型元数据仅显式blocking=False透传原锁链；默认True不改调用形状、规范路径、创建/绑定次序或CAS。
    # LLM: 原writer与blocking形状不变；返回时另一写者可能已提交，仅失效缓存，不给旧对象贴新文件指纹。
    # 函数用途: 原子保存线程并丢弃旧投影，下次读取从磁盘建立新缓存。
    def write(self, thread: ConversationThread, *, blocking: bool = True) -> None:
        path = self.storage.thread_path(thread.thread_id)
        try:
            write_json_file_atomic(path, thread.to_dict(), **({} if blocking else {"blocking": False}))
        finally:
            _drop_cached_thread(path)

    # LLM: Compact/设置/选择沿原锁与CAS，C显式非阻塞不变；锁已在writer返回时释放，只失效缓存避免并发旧值配新指纹。
    # 函数用途: 原子更新线程，返回独立对象；下一次读取重新缓存真实文件。
    def update_atomic(
        self,
        thread_id: str,
        updater: Callable[[ConversationThread], ConversationThread],
        *, blocking: bool = True,
    ) -> ConversationThread:
        path = self.storage.thread_path(thread_id)

        try:
            payload = update_json_file_atomic(path, lambda value: _apply_thread_update(value, thread_id, updater),
                                              require_existing=True, **({} if blocking else {"blocking": False}))
        finally:
            _drop_cached_thread(path)
        thread = ConversationThread.from_dict(payload)
        return _clone_cached_thread(thread)

    # LLM: store_threads 的持久化合同：读回通道到线程索引，损坏时返回错误，禁止静默创建替代绑定；修改须同步本领域调用方与存储回归。
    # 函数用途: 读回通道到线程索引，损坏时返回错误，禁止静默创建替代绑定。
    def _read_bindings_report(self) -> tuple[dict[str, str], dict[str, Any] | None]:
        payload, error = read_json_object_report(
            self.storage.bindings_path,
            context="conversation.bindings.read",
        )
        if error is not None:
            return {}, error
        return {str(key): str(value) for key, value in payload.items()}, None

    # LLM: C首次模型元数据仅显式blocking=False透传原锁链；默认True不改调用形状、规范路径、创建/绑定次序或CAS。
    # LLM: store_threads 的持久化合同：把已确认的线程身份带入通道绑定更新，保持原目录更新语义；修改须同步本领域调用方与存储回归。
    # 函数用途: 把已确认的线程身份带入通道绑定更新，保持原目录更新语义。
    def _bind_existing(self, thread_id: str, kwargs: dict, *, blocking: bool = True) -> ConversationThread:
        return self.bind_channel(
            {
                "thread_id": thread_id,
                "canonical_user_id": kwargs.get("canonical_user_id", ""),
                "owner_id": kwargs.get("owner_id", ""),
                "owner_home": kwargs.get("owner_home", ""),
                "channel": kwargs.get("channel", ""),
                "channel_conversation_id": kwargs.get("channel_conversation_id", ""),
                "channel_user_id": kwargs.get("channel_user_id", ""),
                "cwd": kwargs.get("cwd", ""),
                "workspace_root": kwargs.get("workspace_root", ""),
                "runtime_workspace_roots": kwargs.get("runtime_workspace_roots", ()),
                "now": kwargs.get("now"),
            },
            **({} if blocking else {"blocking": False}),
        )

    # LLM: C首次模型元数据仅显式blocking=False透传原锁链；默认True不改调用形状、规范路径、创建/绑定次序或CAS。
    # LLM: 新 thread 同次保存真实默认绑定事件；没有默认引用仍为未知，既有线程不补造初始化或手动事件。
    # 函数用途: 创建会话并冻结初始模型和目录，再登记通道索引；既有线程不重新选择。
    def _create(self, kwargs: dict, *, blocking: bool = True) -> ConversationThread:
        current = now(kwargs.get("now"))
        initial_model = self.model_default() if self.model_default is not None else ""
        thread = ConversationThread(
            thread_id=new_id("thread"),
            canonical_user_id=kwargs.get("canonical_user_id", ""),
            owner_id=str(kwargs.get("owner_id") or ""),
            owner_home=str(kwargs.get("owner_home") or ""),
            learning_source=_thread_learning_source(kwargs.get("learning_source")),
            model_profile_id=initial_model,
            model_selection_revision=1 if initial_model else 0,
            model_selection_source="default" if initial_model else "unknown",
            title=kwargs.get("title", ""),
            cwd=str(kwargs.get("cwd") or kwargs.get("workspace_root") or ""),
            workspace_root=str(kwargs.get("workspace_root") or kwargs.get("cwd") or ""),
            runtime_workspace_roots=normalized_runtime_workspace_roots(kwargs.get("runtime_workspace_roots"))
            or ((str(kwargs.get("workspace_root") or kwargs.get("cwd")),) if kwargs.get("workspace_root") or kwargs.get("cwd") else ()),
            created_at=current,
            updated_at=current,
        )
        self.write(thread, **({} if blocking else {"blocking": False}))
        return self._bind_existing(thread.thread_id, {**kwargs, "now": current}, **({} if blocking else {"blocking": False}))

    # LLM: C首次模型元数据仅显式blocking=False透传原锁链；默认True不改调用形状、规范路径、创建/绑定次序或CAS。
    # LLM: store_threads 的持久化合同：先原子更新通道索引，再更新用户最近线程索引，保持提交顺序；修改须同步本领域调用方与存储回归。
    # 函数用途: 先原子更新通道索引，再更新用户最近线程索引，保持提交顺序。
    def _write_binding_indexes(self, binding: ChannelBinding, *, blocking: bool = True) -> None:
        key = _binding_key(
            binding.channel, binding.channel_conversation_id, binding.channel_user_id
        )
        update_json_file_atomic(
            self.storage.bindings_path, lambda data: {**data, key: binding.thread_id},
            **({} if blocking else {"blocking": False}),
        )
        update_json_file_atomic(
            self.storage.user_latest_path,
            lambda data: {**data, binding.canonical_user_id: binding.thread_id},
            **({} if blocking else {"blocking": False}),
        )

    # LLM: clone=False只供模块内部列表只读投影；公开load默认独立对象，坏账立即失效，错误上下文保持。
    # 函数用途: 复用未变化线程文件；未被原线程指针发表的新候选不可见，不把半次继任当新会话。
    def _load_path_report(
        self,
        path: Path,
        *,
        thread_id: str = "",
        clone: bool = True,
    ) -> tuple[ConversationThread | None, dict[str, Any] | None]:
        thread, error = _read_thread_record_report(path, thread_id=thread_id, clone=clone)
        predecessor = thread.metadata.get("predecessor_thread_id") if thread is not None else None
        if predecessor and thread.metadata.get("succession_published") is not True:
            try:
                prior, prior_error = _read_thread_record_report(self.storage.thread_path(predecessor), thread_id=predecessor)
            except (TypeError, ValueError) as exc:
                return None, runtime_error_report(exc, context="conversation.thread.predecessor")
            if prior_error:
                return None, prior_error
            scope = thread.metadata.get("predecessor_binding", thread.metadata.get("succession_binding"))
            if prior is None or _succession_target(prior, scope) != thread.thread_id:
                return None, None
        return thread, error


# LLM: 显式继任链不提供授权；每跳复核准确owner/home及原(channel, account/actor, chat)三元组，环/缺失/越界拒绝。
# 函数用途: 找到最多16跳内已成对发表的同通道新会话；不读取最近会话、不修改任何计划。
def resolve_thread_successor_chain(store, thread_id: str, *, max_hops: int = 16, binding=None) -> str:
    threads = getattr(store, "threads", store)
    if type(max_hops) is not int or not 0 <= max_hops <= 16:
        raise ValueError("THREAD_SUCCESSOR_HOP_LIMIT")
    current = threads.require(thread_id)
    identity = (current.owner_id, current.owner_home)
    scope = ([binding.channel, binding.channel_user_id, binding.channel_conversation_id] if isinstance(binding, ChannelBinding)
        else list(binding) if binding is not None else current.metadata.get("succession_binding"))
    visited = {current.thread_id}
    for hop in range(max_hops + 1):
        next_id = _succession_target(current, scope)
        if not next_id:
            return current.thread_id
        if hop == max_hops:
            raise ValueError("THREAD_SUCCESSOR_HOP_LIMIT")
        if not isinstance(next_id, str) or next_id in visited:
            raise ValueError("THREAD_SUCCESSOR_CYCLE")
        try:
            following = threads.require(next_id)
        except KeyError as exc:
            raise ValueError("THREAD_SUCCESSOR_MISSING") from exc
        if not all(identity) or (following.owner_id, following.owner_home) != identity:
            raise ValueError("THREAD_SUCCESSOR_OWNER_MISMATCH")
        if (not isinstance(scope, list) or len(scope) != 3
                or following.metadata.get("predecessor_binding", following.metadata.get("succession_binding")) != scope
                or following.metadata.get("predecessor_thread_id") != current.thread_id
                or not _has_succession_binding(current, scope) or not _has_succession_binding(following, scope)):
            raise ValueError("THREAD_SUCCESSOR_CHANNEL_MISMATCH")
        visited.add(next_id)
        current = following
    raise ValueError("THREAD_SUCCESSOR_HOP_LIMIT")


# LLM: 空三元组仅适用于原本无绑定的Web线程；不得用空目标穿越到IM或另一chat。
# 函数用途: 对照原线程绑定验证一条继任链的固定通道范围。
def _has_succession_binding(thread, scope):
    return (not thread.channel_bindings if scope == ["", "", ""] else any(
        [row.channel, row.channel_user_id, row.channel_conversation_id] == scope for row in thread.channel_bindings))


# LLM: 每个准确三元组单独继任；旧字段仅作同范围兼容，不能用另一通道的指针作fallback。
# 函数用途: 从原记录取指定通道的显式后继编号。
def _succession_target(thread, scope):
    if not isinstance(scope, list) or len(scope) != 3 or any(not isinstance(value, str) for value in scope):
        return None
    mapping = thread.metadata.get("succession_by_binding", {})
    if not isinstance(mapping, dict):
        raise ValueError("THREAD_SUCCESSOR_CHANNEL_MISMATCH")
    key = "\x1f".join(scope)
    if key in mapping:
        return mapping[key]
    return thread.metadata.get("successor_thread_id") if thread.metadata.get("succession_binding") == scope else None


# LLM: 原线程update_atomic是唯一发表点；先私有准备带predecessor的新文件，未发表候选对load/list不可见；失败不触后台账。
# 函数用途: 同绑定锁内复核身份、前台空闲及可选CAS，原子发表空白会话；重试修复索引，只清理本次确已创建的候选。
def _succeed_thread(threads, thread_id, *, title, binding, validate=None):
    from contextlib import ExitStack

    from .store import ConversationStore

    original = threads.require(thread_id)
    if not original.owner_id or not original.owner_home:
        raise ValueError("THREAD_SUCCESSOR_OWNER_REQUIRED")
    bindings = original.channel_bindings
    selected = binding or (bindings[0] if len(bindings) == 1 else None)
    if (bindings and selected is None) or selected is not None and selected not in bindings:
        raise ValueError("THREAD_SUCCESSOR_CHANNEL_AMBIGUOUS")
    scope = [selected.channel, selected.channel_user_id, selected.channel_conversation_id] if selected else ["", "", ""]
    digest = hashlib.sha256(_binding_key(scope[0], scope[2], scope[1]).encode()).hexdigest()
    created, created_written = None, False
    with locked_file_transition(threads.storage.threads_dir / f".channel-{digest}"), ExitStack() as activity_locks:
        # LLM: updater只由原文件锁调用；重复提交不分叉，偏好取同锁最新值，旧运行索引完整保留。
        # 函数用途: 准备新记录，并把前后关系与旧视图归档一起写入原线程的单次提交。
        def apply(latest):
            nonlocal created, created_written
            if (latest.owner_id, latest.owner_home) != (original.owner_id, original.owner_home):
                raise ValueError("THREAD_SUCCESSOR_OWNER_MISMATCH")
            if not _has_succession_binding(latest, scope):
                raise ValueError("THREAD_SUCCESSOR_CHANNEL_MISMATCH")
            if validate is not None:
                validate(latest)
            if _succession_target(latest, scope):
                return latest
            activity_locks.enter_context(locked_file_transition(threads.storage.background_claim_path(thread_id)))
            claim, error = ConversationStore(threads.storage.root, initialize=False).claims.load_report(thread_id)
            if error:
                raise ValueError("THREAD_SUCCESSOR_CLAIM_UNAVAILABLE")
            if claim.get("status") in {"claimed", "running"} and claim.get("recover_same_task_only") is True:
                raise ValueError("NEW_SESSION_BUSY")
            stamp = now(None)
            metadata = {key: copy.deepcopy(value) for key, value in latest.metadata.items()
                        if key in {"provider", "model", "permission_mode", "packs"}}
            portal = latest.metadata.get("plugin_host_v2", {})
            if isinstance(portal, dict) and portal:
                metadata["plugin_host_v2"] = {key: copy.deepcopy(value) for key, value in portal.items()
                    if key in {"permission_mode", "preferred_pack_ids", "installation_ref", "workspace_ref"}}
                metadata["plugin_host_v2"].update(revision=1, receipts={}, msg_bytes=0)
            metadata.update(predecessor_thread_id=latest.thread_id, predecessor_binding=scope, succession_binding=scope)
            created = ConversationThread(thread_id=new_id("thread"), canonical_user_id=latest.canonical_user_id,
                owner_id=latest.owner_id, owner_home=latest.owner_home, title=title or "新会话",
                cwd=latest.cwd, workspace_root=latest.workspace_root, runtime_workspace_roots=latest.runtime_workspace_roots,
                model_profile_id=latest.model_profile_id, model_selection_revision=latest.model_selection_revision,
                model_selection_source=latest.model_selection_source,
                model_selection_last_explicit_revision=latest.model_selection_last_explicit_revision,
                model_selection_selected_at=latest.model_selection_selected_at,
                reasoning_effort=latest.reasoning_effort, verbose_level=latest.verbose_level,
                decision_settings=copy.deepcopy(latest.decision_settings), learning_source=copy.deepcopy(latest.learning_source),
                metadata=metadata, created_at=stamp, updated_at=stamp)
            if selected is not None:
                created = replace(created, channel_bindings=(replace(selected, thread_id=created.thread_id, last_active_at=stamp),))
            create_thread_atomic(threads, created)
            created_written = True
            links = {**latest.metadata.get("succession_by_binding", {}), "\x1f".join(scope): created.thread_id}
            compatibility = {} if latest.metadata.get("successor_thread_id") else {
                "successor_thread_id": created.thread_id, "succeeded_at": stamp, "succession_binding": scope}
            return replace(latest, status="archived", updated_at=stamp,
                metadata={**latest.metadata, **compatibility, "succession_by_binding": links})

        try:
            archived = threads.update_atomic(thread_id, apply)
        except BaseException:
            if created is not None and created_written:
                from ..common.nofollow_fs import unlink_file_beneath
                published, _ = _read_thread_record_report(threads.storage.thread_path(thread_id), thread_id=thread_id)
                candidate, _ = _read_thread_record_report(threads.storage.thread_path(created.thread_id), thread_id=created.thread_id)
                if (published is not None and _succession_target(published, scope) != created.thread_id
                        and candidate is not None and candidate.to_dict() == created.to_dict()):
                    unlink_file_beneath(threads.storage.threads_dir, (created.thread_id + ".json",))
            raise
        current = threads.require(resolve_thread_successor_chain(threads, archived.thread_id, binding=scope))
        if current.metadata.get("succession_published") is not True:
            current = threads.update_atomic(current.thread_id, lambda row: replace(row,
                metadata={**row.metadata, "succession_published": True}))
        for row in current.channel_bindings:
            threads._write_binding_indexes(row)
        return current


# LLM: 缓存对象只读筛选排序，最终切片才深拷贝；目录过滤先于limit，全部坏账照旧报告。
# 函数用途: 扫描原目录并筛选同目录线程，只克隆实际返回的记录。
def _workspace_thread_report(threads: ThreadStore, *, limit: int,
                             workspace_dir: str | None) -> tuple[list[ConversationThread], list[dict[str, Any]]]:
    selected: list[ConversationThread] = []
    load_errors: list[dict[str, Any]] = []
    root = str(Path(workspace_dir).expanduser().resolve(strict=False)) if workspace_dir is not None else None
    for path in sorted(threads.storage.threads_dir.glob("*.json")):
        thread, error = threads._load_path_report(path, clone=False)
        if thread is not None and (root is None or (thread.workspace_root
                and str(Path(thread.workspace_root).expanduser().resolve(strict=False)) == root)):
            selected.append(thread)
        if error is not None:
            load_errors.append(error)
    selected.sort(key=lambda item: item.updated_at)
    return [_clone_cached_thread(row) for row in (selected if limit <= 0 else selected[-limit:])], load_errors


# LLM: 无通道创建仍用原原子写钩子；实现移到类外以保持ThreadStore尺寸，不增加状态源。
# 函数用途: 验证目录并保存独立主会话，不更新通道或最近用户索引。
def _create_workspace_thread(threads: ThreadStore, *, canonical_user_id: str, title: str,
                             workspace_root: str, cwd: str, owner_id: str, owner_home: str,
                             runtime_workspace_roots: tuple[str, ...]) -> ConversationThread:
    from .workspace_context import existing_workspace_directory

    root = str(existing_workspace_directory(workspace_root or cwd)) if workspace_root or cwd else ""
    model = threads.model_default() if threads.model_default is not None else ""
    current = now(None)
    return create_thread_atomic(threads, ConversationThread(
        thread_id=new_id("thread"), canonical_user_id=canonical_user_id, title=title,
        owner_id=owner_id, owner_home=owner_home, cwd=root, workspace_root=root,
        runtime_workspace_roots=runtime_workspace_roots or ((root,) if root else ()),
        model_profile_id=model, model_selection_revision=1 if model else 0,
        model_selection_source="default" if model else "unknown", created_at=current, updated_at=current,
    ))


# LLM: 授权归上层workspace_context；原锁内读取最新目录版本，避免两个切换都持旧版本成功。
# 函数用途: 检查目录并用原线程事务提交目录修订，不修改其他线程和运行绑定。
def _switch_thread_workspace(threads: ThreadStore, thread_id: str, workspace_dir: str, *,
                             expected_revision: int | None) -> ConversationThread:
    from .workspace_context import existing_workspace_directory

    root = str(existing_workspace_directory(workspace_dir))

    # LLM: 只更新目录字段与目录修订，其它并发字段沿原记录保留。
    # 函数用途: 在同一文件锁内核对CAS并替换目录。
    def apply(thread: ConversationThread) -> ConversationThread:
        if expected_revision is not None and (type(expected_revision) is not int or thread.revision != expected_revision):
            raise RuntimeError("WORKSPACE_REVISION_CONFLICT")
        return replace(thread, cwd=root, workspace_root=root, runtime_workspace_roots=(root,),
                       revision=thread.revision + 1, updated_at=max(thread.updated_at, now(None)))

    return threads.update_atomic(thread_id, apply)


# LLM: 只在代次未推进时写 compact_vision_failed_generation=当前代次；不碰 consecutive_failures/failure_code，因此不进熔断。
#   放在类外是为了不再撑大 ThreadStore；仍走同一 update_atomic 原子更新。
# 函数用途: 记录本代次随图摘要失败过，让下一次压缩改走归档引用。
def record_thread_compact_vision_failure(
    threads: ThreadStore,
    thread_id: str,
    *,
    expected_generation: int,
    now: float | None = None,
) -> ConversationThread:
    current = now if now is not None else time.time()

    # LLM: 线程存储原子更新回调；仅原锁内消费当前代数，不覆盖并发胜出状态，联测压缩提交。
    # 函数用途: 仅在原压缩代数仍匹配时标记视觉摘要失败，沿原线程原子更新。
    def apply(thread: ConversationThread) -> ConversationThread:
        if thread.compact_generation != expected_generation:
            return thread
        return replace(thread, compact_vision_failed_generation=thread.compact_generation, updated_at=current)

    return threads.update_atomic(thread_id, apply)


# LLM: 线程来源只接受合法投影；调用方传坏值或旧入口不传时归一为空（未知、不可学），
#   辅助事实不能反过来拒绝线程创建，也不能把未知猜成用户。
# 函数用途: 归一线程创建时的宿主来源投影。
def _thread_learning_source(value: object) -> dict[str, Any]:
    from .learning_source import thread_source_projection

    return thread_source_projection(value) or {}


# LLM: 只新增无通道绑定的精确线程；复用原JSON锁/私有原子写，任何既有或损坏文件均不覆盖，调用方负责授权与幂等。
# 函数用途: 给宿主领域适配器提供不伪造channel绑定的线程创建钩子。
def create_thread_atomic(threads: ThreadStore, thread: ConversationThread) -> ConversationThread:
    path = threads.storage.thread_path(thread.thread_id)

    # LLM: 检查与落盘在原per-path锁内，空JSON、坏JSON和链接也不是可覆盖的空槽。
    # 函数用途: 只在路径确实不存在时返回新线程内容。
    def create(payload: dict) -> dict:
        if payload or path.exists() or path.is_symlink():
            raise FileExistsError("conversation thread already exists")
        return thread.to_dict()

    try:
        result = update_json_file_atomic(path, create)
        return ConversationThread.from_dict(result)
    finally:
        _drop_cached_thread(path)
