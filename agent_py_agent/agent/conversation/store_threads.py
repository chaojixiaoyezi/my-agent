# LLM: 原线程文件锁内核对身份及 Compact/设置 CAS；模型选择变化必须逐次前进且保留显式覆盖版本，不增加状态源。 C显式非阻塞模式只复用原锁，默认调用与权限不变。
# 模块用途: 保存会话元数据与通道绑定，原子更新保留并发状态；新默认绑定记录真实初始化事实。
from __future__ import annotations

import copy
import hashlib
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import fields, replace
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

_THREAD_RECORD_CACHE_MAX_ENTRIES_COUNT = 16384
_THREAD_RECORD_CACHE_MAX_RECORD_BYTES = 64 * 1024
_THREAD_RECORD_CACHE_LOCK = threading.Lock()
_THREAD_RECORD_CACHE: OrderedDict[str, tuple[tuple[int, int, int, int], ConversationThread]] = OrderedDict()


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


# LLM: 只接收同次稳定读取且不超过64KiB的指纹，过大/无指纹清旧投影；不持锁深拷贝，不替代磁盘权威。
# 函数用途: 保存独立的线程快照，更新LRU顺序并淘汰最旧记录。
def _store_cached_thread(
    path: Path, fingerprint: tuple[int, int, int, int] | None, thread: ConversationThread,
) -> None:
    if fingerprint is None or fingerprint[1] > _THREAD_RECORD_CACHE_MAX_RECORD_BYTES:
        _drop_cached_thread(path)
        return
    snapshot = _clone_cached_thread(thread)
    key = str(path)
    with _THREAD_RECORD_CACHE_LOCK:
        _THREAD_RECORD_CACHE[key] = (fingerprint, snapshot)
        _THREAD_RECORD_CACHE.move_to_end(key)
        while len(_THREAD_RECORD_CACHE) > _THREAD_RECORD_CACHE_MAX_ENTRIES_COUNT:
            _THREAD_RECORD_CACHE.popitem(last=False)


# LLM: 写入、空记录与读取失败都丢弃旧投影；只触碰内存，不掩盖或修改原错误。
# 函数用途: 在线程缓存锁内幂等移除指定路径。
def _drop_cached_thread(path: Path) -> None:
    with _THREAD_RECORD_CACHE_LOCK:
        _THREAD_RECORD_CACHE.pop(str(path), None)


# LLM: 仅同指纹/同身份且不超过64KiB的记录复用；大记录返回本次解析的独立对象，不重复深拷贝或保留旧缓存。
# 函数用途: 读取并校验独立线程快照，只把身份一致且读前读后稳定的文件放进有界缓存。
def _read_thread_record_report(
    path: Path, *, thread_id: str = "",
) -> tuple[ConversationThread | None, dict[str, Any] | None]:
    actual_thread_id = str(thread_id or path.stem)
    before_fp = _file_fingerprint(path)
    cached_thread = None
    if before_fp is not None and before_fp[1] <= _THREAD_RECORD_CACHE_MAX_RECORD_BYTES:
        with _THREAD_RECORD_CACHE_LOCK:
            cached = _THREAD_RECORD_CACHE.get(str(path))
            if cached is not None and cached[0] == before_fp and cached[1].thread_id == actual_thread_id:
                _THREAD_RECORD_CACHE.move_to_end(str(path))
                cached_thread = cached[1]
    if cached_thread is not None:
        return _clone_cached_thread(cached_thread), None
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
            return _clone_cached_thread(thread), None
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


# LLM: Store commands may receive lists or tuples; normalize them before comparing exact
# thread scope so serialization shape cannot create a false lineage change.
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
# 函数用途: 校验并合并线程更新；模型选择时间只在原选择版本提交时更新，其余更新保留旧时间。
def _apply_thread_update(payload: dict, thread_id: str, updater: Callable) -> dict:
    thread = ConversationThread.from_dict(payload)
    if not thread.thread_id:
        raise DataCorruptionError(f"conversation thread is unreadable: {thread_id}")
    if thread.thread_id != thread_id:
        raise DataCorruptionError(f"conversation thread identity is invalid: {thread_id}")
    updated = updater(thread)
    if updated.thread_id != thread_id:
        raise DataCorruptionError(f"conversation thread updater changed identity: {thread_id}")
    _validate_model_selection_update(thread, updated)
    updated = replace(updated, model_selection_selected_at=(
        now() if updated.model_selection_revision != thread.model_selection_revision else thread.model_selection_selected_at
    ))
    serialized = updated.to_dict()
    omitted = {item.name for item in fields(updated) if item.metadata.get("omit_empty") and item.name not in serialized}
    return {**{key: value for key, value in payload.items() if key not in omitted}, **serialized}


# LLM: 原线程元数据与通道绑定唯一存储；身份校验与扩展合并在类外小函数，仍在同一文件锁内守住 CAS，不建立第二状态源。
# 类用途: 保存线程和通道绑定，防止迟到更新回退模型选择；默认模型只初始化新线程。
class ThreadStore:
    # LLM: 同一 ConversationStorage 与默认解析器由组装入口注入；构造不读写线程或通道索引。
    # 函数用途: 绑定线程文件目录和新会话模型来源，已有会话不会因构造重新选择模型。
    def __init__(
        self, storage: ConversationStorage, *, model_default: Callable[[], str] | None = None
    ) -> None:
        self.storage = storage
        self.model_default = model_default

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

    # LLM: Keep this adapter small; exact-id creation and collision checks live in
    # agent_thread_store so the channel-bound store does not absorb agent runtime policy.
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
    # 函数用途: 通过绑定索引加载精确线程，同时返回索引或线程读取错误。
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
        return self.load_report(thread_id) if thread_id else (None, None)

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
    # LLM: Channel binding updates must merge into the latest durable thread atomically so a
    # delayed adapter request cannot revert compact, task indexes, or the sticky workspace.
    # initialize_workspace 仅补齐空目录，必须在同一原子更新中判断，不能覆盖并发绑定或既有执行目录。
    # 函数用途: 为同一会话增加或刷新通道绑定，同时保留其他并发更新的会话状态。
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
    # 函数用途: 返回按活动时间排序的线程列表，需要坏账信息时调用 list_report。
    def list(self, *, limit: int = 100) -> list[ConversationThread]:
        threads, _load_errors = self.list_report(limit=limit)
        return threads

    # LLM: store_threads 的持久化合同：扫描原线程目录并汇总读取错误，只排序投影，不修复或写盘；修改须同步本领域调用方与存储回归。
    # 函数用途: 扫描原线程目录并汇总读取错误，只排序投影，不修复或写盘。
    def list_report(
        self, *, limit: int = 100
    ) -> tuple[list[ConversationThread], list[dict[str, Any]]]:
        threads: list[ConversationThread] = []
        load_errors: list[dict[str, Any]] = []
        for path in sorted(self.storage.threads_dir.glob("*.json")):
            thread, error = self._load_path_report(path)
            if thread is not None:
                threads.append(thread)
            if error is not None:
                load_errors.append(error)
        threads.sort(key=lambda item: item.updated_at)
        limited = threads if limit <= 0 else threads[-limit:]
        return limited, load_errors

    # LLM: A summary owns only summary and activity time; it must never replace a stale full thread.
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

    # LLM: 唯一CAS推进提交链和计数；局部检查点不覆盖全线程摘要/游标，旧代校准与展示仍统一失效。
    # 函数用途: 原子提交压缩；局部视图只沿同链读取，不另建任务摘要表，也不改原始消息。
    def update_compact_state(
        self,
        thread_id: str,
        *,
        commit: ConversationCompactCommit,
        expected_generation: int,
        now: float | None = None,
    ) -> ConversationThread:
        """Atomically advance one validated summary/checkpoint without touching raw messages."""
        if type(commit.publish_thread_view) is not bool:
            raise ValueError("compact publication scope must be an explicit boolean")
        current = now if now is not None else time.time()

        # LLM: 先检查原generation，再根据结构化publish_thread_view提交全线程投影；局部摘要不成为全局权限。
        # 函数用途: 同锁提交唯一head；局部压缩保留全线程摘要时间和游标，防止其它任务跳过未覆盖原文。
        def apply(thread: ConversationThread) -> ConversationThread:
            if thread.compact_generation != expected_generation:
                raise RuntimeError(
                    "conversation compact generation changed while summary was being prepared"
                )
            return replace(
                thread,
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
                compact_source_tool_pairs=max(
                    0,
                    int(commit.source_tool_pairs),
                ),
                compact_checkpoint_id=str(commit.checkpoint_id),
                compact_consecutive_failures=0,
                compact_failure_updated_at=0.0,
                compact_failure_code="",
                compact_vision_failed_generation=-1,
                provider_context_observation={},
                model_context_usage={},
                updated_at=current,
            )

        return self.update_atomic(thread_id, apply)

    # LLM: Provider usage calibration is small numeric thread telemetry. The compact generation
    # is a CAS fence, and this update must not advance updated_at because model activity is not a
    # user-visible recency edge. Compact commits clear the field in their own atomic transition.
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

    # LLM: A failed summary candidate may update only the compact failure circuit; it must never
    # move the generation, cursor, summary, checkpoint pointer, or raw transcript.
    # 函数用途: 记录一次会话压缩失败，供跨请求熔断使用，但不把失败候选当成已经提交。
    def record_compact_failure(
        self,
        thread_id: str,
        *,
        failure_code: str,
        expected_generation: int,
        now: float | None = None,
    ) -> ConversationThread:
        current = now if now is not None else time.time()

        # LLM: store_threads 的持久化合同：仅为同一 Compact 代增加失败记录，保留原摘要与消息游标；修改须同步本领域调用方与存储回归。
        # 函数用途: 仅为同一 Compact 代增加失败记录，保留原摘要与消息游标。
        def apply(thread: ConversationThread) -> ConversationThread:
            if thread.compact_generation != expected_generation:
                return thread
            return replace(
                thread,
                compact_consecutive_failures=thread.compact_consecutive_failures + 1,
                compact_failure_updated_at=current,
                compact_failure_code=str(failure_code or "COMPACT_FAILED"),
                updated_at=current,
            )

        return self.update_atomic(thread_id, apply)

    # LLM: Verbose is a thread-local system control and updates only its own field atomically.
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
                "runtime_workspace_roots": kwargs.get("runtime_workspace_roots", ()),
                "now": kwargs.get("now"),
            },
            **({} if blocking else {"blocking": False}),
        )

    # LLM: C首次模型元数据仅显式blocking=False透传原锁链；默认True不改调用形状、规范路径、创建/绑定次序或CAS。
    # LLM: 新 thread 同次保存真实默认绑定事件；没有默认引用仍为未知，既有线程不补造初始化或手动事件。
    # 函数用途: 创建会话并记录初始模型版本，再登记原通道索引；后续默认变化只影响新会话。
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

    # LLM: 仅指纹相同的有效线程命中；未命中按原reader/from_dict恢复，读前后不稳定不缓存，错误上下文保持不变。
    # 函数用途: 读取独立线程对象，复用未变化文件；空记录或坏账立即移除旧缓存。
    def _load_path_report(
        self,
        path: Path,
        *,
        thread_id: str = "",
    ) -> tuple[ConversationThread | None, dict[str, Any] | None]:
        return _read_thread_record_report(path, thread_id=thread_id)


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
