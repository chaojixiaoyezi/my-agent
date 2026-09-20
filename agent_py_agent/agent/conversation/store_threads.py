# LLM: 线程元数据与通道绑定；同一文件锁内完成身份校验和 Compact CAS，模型默认仅影响新线程；保持 canonical 文件、锁和错误报告合同。
# 模块用途: 线程元数据与通道绑定；同一文件锁内完成身份校验和 Compact CAS，模型默认仅影响新线程。
from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from dataclasses import replace
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
from .store_io import (
    now,
    read_json_object_report,
)
from .store_layout import ConversationStorage


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


# LLM: 仅由持有同通道绑定事务锁的 store 入口调用；显式 reuse_latest 保留原语义，不按模型名猜会话。
# 函数用途: 在短事务内查找、显式复用或创建会话并提交索引，不持有模型执行锁。
def _get_or_create_bound_thread(store, request: dict) -> ConversationThread:
    existing, binding_error = store.resolve_report(
        channel=request.get("channel", ""),
        channel_conversation_id=request.get("channel_conversation_id", ""),
        channel_user_id=request.get("channel_user_id", ""),
    )
    if binding_error is not None:
        raise DataCorruptionError(str(binding_error))
    if existing is not None:
        return store._bind_existing(existing.thread_id, request)
    latest = None
    if request.get("reuse_latest_for_user"):
        latest, latest_error = store.latest_for_user_report(request.get("canonical_user_id", ""))
        if latest_error is not None:
            raise DataCorruptionError(str(latest_error))
    if latest is not None:
        return store._bind_existing(latest.thread_id, request)
    return store._create(request)


# LLM: 线程元数据与通道绑定；同一文件锁内完成身份校验和 Compact CAS，模型默认仅影响新线程；修改须核对直接调用方与原子存储测试。
# 类用途: 线程元数据与通道绑定；同一文件锁内完成身份校验和 Compact CAS，模型默认仅影响新线程。
class ThreadStore:
    # LLM: 同一 ConversationStorage 与默认解析器由组装入口注入；构造不读写线程或通道索引。
    # 函数用途: 绑定线程文件目录和新会话模型来源，已有会话不会因构造重新选择模型。
    def __init__(
        self, storage: ConversationStorage, *, model_default: Callable[[], str] | None = None
    ) -> None:
        self.storage = storage
        self.model_default = model_default

    # LLM: 通道绑定的查找与创建共用短事务锁；不能与单文件索引锁同路径，不锁模型执行或串行不同会话。
    # 函数用途: 两个窗口首次同时打开同一 session 时只生成一个 thread，避免之后模型选择和消息各走一份。
    def get_or_create(self, request: dict) -> ConversationThread:
        identity = _binding_key(
            request.get("channel", ""),
            request.get("channel_conversation_id", ""),
            request.get("channel_user_id", ""),
        )
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        with locked_file_transition(self.storage.threads_dir / f".channel-{digest}"):
            return _get_or_create_bound_thread(self, request)

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

    # LLM: Channel binding updates must merge into the latest durable thread atomically so a
    # delayed adapter request cannot revert compact, task indexes, or the sticky workspace.
    # initialize_workspace 仅补齐空目录，必须在同一原子更新中判断，不能覆盖并发绑定或既有执行目录。
    # 函数用途: 为同一会话增加或刷新通道绑定，同时保留其他并发更新的会话状态。
    def bind_channel(self, request: dict) -> ConversationThread:
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
        )
        self._write_binding_indexes(binding)
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

    # LLM: One CAS advances transcript/live-tool totals and clears both prior-generation calibration and display usage.
    # 函数用途: 原子提交压缩及累计来源，清除旧代上下文显示和校准，不改写原始消息。
    def update_compact_state(
        self,
        thread_id: str,
        *,
        commit: ConversationCompactCommit,
        expected_generation: int,
        now: float | None = None,
    ) -> ConversationThread:
        """Atomically advance one validated summary/checkpoint without touching raw messages."""
        current = now if now is not None else time.time()

        # LLM: store_threads 的持久化合同：在持锁线程上检查 generation 后提交压缩游标及计数，并清除旧代遥测；修改须同步本领域调用方与存储回归。
        # 函数用途: 在持锁线程上检查 generation 后提交压缩游标及计数，并清除旧代遥测。
        def apply(thread: ConversationThread) -> ConversationThread:
            if thread.compact_generation != expected_generation:
                raise RuntimeError(
                    "conversation compact generation changed while summary was being prepared"
                )
            return replace(
                thread,
                summary=str(commit.summary).strip(),
                compact_operation_evidence=dict(commit.operation_evidence),
                compacted_through_message_id=str(commit.compacted_through_message_id),
                compacted_through_byte_offset=max(
                    0,
                    int(commit.compacted_through_byte_offset),
                ),
                compact_generation=thread.compact_generation + 1,
                compact_updated_at=current,
                compact_source_messages=max(0, int(commit.source_messages)),
                compact_source_tool_pairs=max(
                    0,
                    int(commit.source_tool_pairs),
                ),
                compact_checkpoint_id=str(commit.checkpoint_id),
                compact_consecutive_failures=0,
                compact_failure_updated_at=0.0,
                compact_failure_code="",
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
        if normalized not in {"off", "on", "full"}:
            raise ValueError("verbose level must be one of: off, on, full")
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

    # LLM: store_threads 的持久化合同：原子保存一份已校验线程记录，调用方负责锁和身份边界；修改须同步本领域调用方与存储回归。
    # 函数用途: 原子保存一份已校验线程记录，调用方负责锁和身份边界。
    def write(self, thread: ConversationThread) -> None:
        write_json_file_atomic(self.storage.thread_path(thread.thread_id), thread.to_dict())

    # LLM: Compact CAS checks and writes must share one cross-process file lock; checking a
    # previously loaded dataclass and locking only the final replace permits two generations.
    # 函数用途: 在同一个文件锁里读取、校验和写回 thread，供 compact 成功/失败状态做真实原子迁移。
    def update_atomic(
        self,
        thread_id: str,
        updater: Callable[[ConversationThread], ConversationThread],
    ) -> ConversationThread:
        path = self.storage.thread_path(thread_id)

        # LLM: store_threads 的持久化合同：在文件事务内校验原线程和更新结果身份，拒绝换线程后再序列化；修改须同步本领域调用方与存储回归。
        # 函数用途: 在文件事务内校验原线程和更新结果身份，拒绝换线程后再序列化。
        def apply(payload: dict) -> dict:
            thread = ConversationThread.from_dict(payload)
            if not thread.thread_id:
                raise DataCorruptionError(f"conversation thread is unreadable: {thread_id}")
            if thread.thread_id != thread_id:
                raise DataCorruptionError(f"conversation thread identity is invalid: {thread_id}")
            updated = updater(thread)
            if updated.thread_id != thread_id:
                raise DataCorruptionError(
                    f"conversation thread updater changed identity: {thread_id}"
                )
            return updated.to_dict()

        payload = update_json_file_atomic(path, apply, require_existing=True)
        return ConversationThread.from_dict(payload)

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

    # LLM: store_threads 的持久化合同：把已确认的线程身份带入通道绑定更新，保持原目录更新语义；修改须同步本领域调用方与存储回归。
    # 函数用途: 把已确认的线程身份带入通道绑定更新，保持原目录更新语义。
    def _bind_existing(self, thread_id: str, kwargs: dict) -> ConversationThread:
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
            }
        )

    # LLM: 新 thread 原子保存当前 owner 默认模型引用；已有 thread 的模型绝不在重新绑定通道时更新。
    # 函数用途: 创建持久会话并冻结其初始模型，再登记通道索引，后续默认变化只影响将来新会话。
    def _create(self, kwargs: dict) -> ConversationThread:
        current = now(kwargs.get("now"))
        thread = ConversationThread(
            thread_id=new_id("thread"),
            canonical_user_id=kwargs.get("canonical_user_id", ""),
            owner_id=str(kwargs.get("owner_id") or ""),
            owner_home=str(kwargs.get("owner_home") or ""),
            model_profile_id=self.model_default() if self.model_default is not None else "",
            title=kwargs.get("title", ""),
            created_at=current,
            updated_at=current,
        )
        self.write(thread)
        return self._bind_existing(thread.thread_id, {**kwargs, "now": current})

    # LLM: store_threads 的持久化合同：先原子更新通道索引，再更新用户最近线程索引，保持提交顺序；修改须同步本领域调用方与存储回归。
    # 函数用途: 先原子更新通道索引，再更新用户最近线程索引，保持提交顺序。
    def _write_binding_indexes(self, binding: ChannelBinding) -> None:
        key = _binding_key(
            binding.channel, binding.channel_conversation_id, binding.channel_user_id
        )
        update_json_file_atomic(
            self.storage.bindings_path, lambda data: {**data, key: binding.thread_id}
        )
        update_json_file_atomic(
            self.storage.user_latest_path,
            lambda data: {**data, binding.canonical_user_id: binding.thread_id},
        )

    # LLM: store_threads 的持久化合同：解析单个线程文件并核验身份，将读取或解析失败归入原错误报告；修改须同步本领域调用方与存储回归。
    # 函数用途: 解析单个线程文件并核验身份，将读取或解析失败归入原错误报告。
    def _load_path_report(
        self,
        path: Path,
        *,
        thread_id: str = "",
    ) -> tuple[ConversationThread | None, dict[str, Any] | None]:
        payload, error = read_json_object_report(path, context="conversation.thread.read")
        actual_thread_id = str(thread_id or path.stem)
        if error is not None:
            error["thread_id"] = actual_thread_id
            return None, error
        if not payload:
            return None, None
        try:
            return ConversationThread.from_dict(payload), None
        except Exception as exc:
            report = runtime_error_report(exc, context="conversation.thread.read")
            report["thread_id"] = actual_thread_id
            report["path"] = str(path)
            return None, report
