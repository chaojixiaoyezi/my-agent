# LLM: 唤醒沿原文件发布；稳定键由 store_wake_publication 在原锁内冻结完整配对后安装，查询不补账。
# 模块用途: 保存唤醒、可靠交接配对观察并确认处理，显式依赖线程与观察能力，不运行模型。
from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..gateway_parts.io import (
    read_json_file,
    update_json_file_atomic,
    write_json_file_atomic,
)
from ..io.jsonl import append_jsonl
from ..runtime_errors import runtime_error_report
from .models import (
    ConversationThread,
    ObservationEvent,
    WakeSignal,
    new_id,
)
from .store_io import (
    now,
    unlink_quietly,
)
from .store_layout import ConversationStorage, wake_urgency
from .store_observations import observation_from_request
from .store_wake_publication import publication_receipt, publish_deduped


# LLM: 显式 refs 优先于观察 refs，沿原有值归一，不推断证据真实性。
# 函数用途: 从已有结构化请求提取唤醒证据引用，不读文件。
def wake_evidence_refs(
    observation: ObservationEvent | None, explicit_refs: object
) -> tuple[str, ...]:
    if explicit_refs is not None:
        refs = explicit_refs
    elif observation is not None:
        refs = observation.evidence_refs
    else:
        refs = ()
    return (
        tuple(str(item) for item in refs if str(item or "").strip())
        if isinstance(refs, (list, tuple))
        else ()
    )


# LLM: 信号归属来自已校验线程，观察仅补充原字段；构造不发布或授予执行权。
# 函数用途: 构造待落盘的唤醒信号，保留观察链接、去重键和时间。
def _wake_signal(thread_id: str, current: float, kwargs: dict[str, Any]) -> WakeSignal:
    observation = kwargs.get("observation")
    observation = observation if isinstance(observation, ObservationEvent) else None
    return WakeSignal(
        wake_signal_id=new_id("wake"),
        thread_id=thread_id,
        observation_id=observation.observation_id if observation is not None else "",
        urgency=wake_urgency(kwargs.get("urgency", "urgent")),
        severity=kwargs.get("severity")
        or (observation.severity if observation is not None else ""),
        reason=str(kwargs.get("reason") or "agent_event"),
        source_agent_id=kwargs.get("source_agent_id")
        or (observation.source_agent_id if observation is not None else ""),
        parent_agent_id=kwargs.get("parent_agent_id")
        or (observation.parent_agent_id if observation is not None else ""),
        root_task_id=kwargs.get("root_task_id")
        or (observation.root_task_id if observation is not None else ""),
        summary=kwargs.get("summary") or (observation.summary if observation is not None else ""),
        evidence_refs=wake_evidence_refs(observation, kwargs.get("evidence_refs")),
        created_at=current,
        dedupe_key=str(kwargs.get("dedupe_key") or ""),
        metadata=kwargs.get("metadata") or {},
    )


# LLM: 仅选择现有紧急和普通队列，排序与扫描范围沿原开关。
# 函数用途: 为待处理扫描提供队列次序。
def _wake_kinds(include_normal: bool) -> tuple[str, ...]:
    return ("urgent", "normal") if include_normal else ("urgent",)


# LLM: 解析与建模拆成两步,唯一目的是让读取侧增量索引能缓存"磁盘原始负载"而仍然复用
# 同一份错误口径(runtime_error_report + context + path,逐字与旧实现相同,不含 traceback,
# 因此错误字典对同一份字节是确定性的、可缓存)。任何一步失败都返回同一种结构化错误。
# 函数用途: 读取并解析一条唤醒信号文件,返回磁盘原始负载或结构化错误。
def _read_wake_signal_payload(path: Path) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"wake signal file is {type(payload).__name__}, expected object")
        return payload, None
    except Exception as exc:
        report = runtime_error_report(exc, context="conversation.wake_signal.read")
        report["path"] = str(path)
        return None, report


# LLM: 建模失败与解析失败共用同一 context/path 口径,消费方(guidance/运行时)以 load_error
# 是否存在作硬门,因此这里绝不能吞掉任何一条失败事实。
# 函数用途: 把一条已解析的唤醒信号负载建模成 WakeSignal,失败时给出同口径错误报告。
def _wake_signal_from_payload(
    path: Path, payload: dict[str, Any]
) -> tuple[WakeSignal | None, dict[str, Any] | None]:
    try:
        return WakeSignal.from_dict(payload), None
    except Exception as exc:
        report = runtime_error_report(exc, context="conversation.wake_signal.read")
        report["path"] = str(path)
        return None, report


# LLM: 该读取器只解析不改语义:负载就是磁盘字节的 json.loads 结果,错误就是旧
# _read_wake_signal 的同一份结构化报告,因此增量索引缓存的正是权威读取事实。
# 函数用途: 增量索引与全量扫描共用的读取器,统一 (ok, 负载, 错误) 形状;解析失败时 ok=False。
def _read_wake_signal_entry(
    path: Path,
) -> tuple[bool, dict[str, Any] | None, dict[str, Any] | None]:
    payload, error = _read_wake_signal_payload(path)
    return payload is not None, payload, error


# LLM: 显式复用 storage 与线程/观察回调；发布、去重、冻结及确认顺序必须联测。
# 类用途: 保存唤醒和处理回执，提供调度读取及模型投递冻结能力。
class WakeStore:
    # LLM: 唤醒只接收线程校验/活动更新和观察确认；发布顺序保持，不能反向访问整个 Store。
    # 函数用途: 连接原唤醒队列及跨领域回调，不注册调度任务或发起模型请求。
    def __init__(
        self,
        storage: ConversationStorage,
        *,
        require_thread: Callable[[str], ConversationThread],
        update_thread_atomic: Callable[
            [str, Callable[[ConversationThread], ConversationThread]], ConversationThread
        ],
        mark_observations_handled: Callable[..., None],
    ) -> None:
        self.storage = storage
        self._require_thread = require_thread
        self._update_thread_atomic = update_thread_atomic
        self._mark_observations_handled = mark_observations_handled

    # LLM: 回执纯读，prepared 不冒充完整交付，坏账显式报错；重试和保留 handled 由发布锁内裁决。
    # 函数用途: 查询某个键的完整发布是否 pending/handled，不迁移或修复任何记录。
    def delivery_receipt(self, thread_id: str, dedupe_key: str) -> str:
        """Return 'pending' | 'handled' | '' for one exact dedupe key."""

        return publication_receipt(self.storage, thread_id, dedupe_key)

    # LLM: 有键配对先冻结原负载，再安装 wake/观察；部分提交原样报错，不追加随机无链观察。
    #   retain_handled 是显式通用策略；无键调用不承诺重试幂等，线程活动仍原子合并。
    # 函数用途: 可靠发布或恢复同一对观察与唤醒，不把迟到请求内容覆盖到原发布。
    def append_observation(
        self,
        observation_request: dict,
        wake_request: dict,
    ) -> tuple[ObservationEvent, WakeSignal]:
        """Freeze keyed content, then publish the wake before its linked observation."""
        thread_id = str(observation_request.get("thread_id") or "")
        thread = self._require_thread(thread_id)
        wake_thread_id = str(wake_request.get("thread_id") or thread_id)
        if wake_thread_id != thread.thread_id:
            raise ValueError("observation and wake signal must use the same thread")
        observation, _observed_at = observation_from_request(thread.thread_id, observation_request)
        kwargs = {
            "observation": observation,
            "urgency": wake_request.get("urgency", "urgent"),
            "severity": wake_request.get("severity", ""),
            "reason": wake_request.get("reason", "agent_event"),
            "source_agent_id": wake_request.get("source_agent_id", ""),
            "parent_agent_id": wake_request.get("parent_agent_id", ""),
            "root_task_id": wake_request.get("root_task_id", ""),
            "summary": wake_request.get("summary", ""),
            "evidence_refs": wake_request.get("evidence_refs"),
            "dedupe_key": wake_request.get("dedupe_key", ""),
            "metadata": wake_request.get("metadata") or {},
        }
        signal = _wake_signal(thread.thread_id, now(wake_request.get("now")), kwargs)
        if signal.dedupe_key:
            linked, selected = publish_deduped(
                self.storage, signal, observation=observation,
                retain_handled=wake_request.get("retain_handled", False),
            )
            assert linked is not None
        else:
            selected = self._write_new(signal)
            linked = replace(observation, wake_signal_id=selected.wake_signal_id)
            append_jsonl(self.storage.observation_path(thread_id), linked.to_dict(), sort_keys=True)
        self._update_thread_atomic(
            thread.thread_id,
            lambda latest: replace(latest, updated_at=max(latest.updated_at, linked.observed_at)),
        )
        return linked, selected

    # LLM: 写入原 canonical 队列文件，不另写观察或去重状态。
    # 函数用途: 发布一条不需要去重领取的新唤醒。
    def _write_new(self, signal: WakeSignal) -> WakeSignal:
        write_json_file_atomic(self.storage.wake_signal_path(signal), signal.to_dict())
        return signal

    # LLM: 稳定键沿原发布锁冻结信号，显式 retain_handled 控制已消费重发；无键仍直接发布。
    # 函数用途: 创建或恢复一个信号，普通 Goal 在上一代处理后仍能正常继续。
    def raise_signal(self, request: dict) -> WakeSignal:
        thread_id = str(request.get("thread_id") or "")
        thread = self._require_thread(thread_id)
        kwargs = {
            "observation": request.get("observation"),
            "urgency": request.get("urgency", "urgent"),
            "severity": request.get("severity", ""),
            "reason": request.get("reason", "agent_event"),
            "source_agent_id": request.get("source_agent_id", ""),
            "parent_agent_id": request.get("parent_agent_id", ""),
            "root_task_id": request.get("root_task_id", ""),
            "summary": request.get("summary", ""),
            "evidence_refs": request.get("evidence_refs"),
            "dedupe_key": request.get("dedupe_key", ""),
            "metadata": request.get("metadata") or {},
        }
        signal = _wake_signal(thread.thread_id, now(request.get("now")), kwargs)
        if signal.dedupe_key:
            return publish_deduped(
                self.storage, signal, retain_handled=request.get("retain_handled", False),
            )[1]
        write_json_file_atomic(self.storage.wake_signal_path(signal), signal.to_dict())
        return signal

    # LLM: 保留调用方只取结果的原接口，严格错误处理须调用 pending_report。
    # 函数用途: 读取有界待处理信号，不消费队列。
    def pending(self, *, limit: int = 100, include_normal: bool = True) -> list[WakeSignal]:
        signals, _load_errors = self.pending_report(limit=limit, include_normal=include_normal)
        return signals

    # LLM: 精确 ID 查询不能受分页 limit 截断，也不能命中已处理记录。
    # 函数用途: 查找一条尚待消费的唤醒。
    def pending_one(self, wake_signal_id: str) -> WakeSignal | None:
        """Return one exact pending wake without truncating a queue view."""

        return self._pending_by_id(str(wake_signal_id or "").strip())

    # LLM: 首次模型投递负载沿原信号锁冻结，后续重投不能覆盖已冻结内容。
    # 函数用途: 在待处理信号上保存可恢复的 owner 投递快照。
    def cache_delivery(
        self,
        wake_signal_id: str,
        delivery: dict[str, Any],
    ) -> WakeSignal | None:
        """Freeze one model-authored owner payload on its existing durable wake."""

        selected_id = str(wake_signal_id or "").strip()
        path = self._find_path(selected_id)
        if path is None:
            return None
        cached: WakeSignal | None = None

        # LLM: 锁内同时核对信号 ID、pending 状态和既有冻结值。
        # 函数用途: 仅为仍匹配的未冻结信号写入模型投递负载。
        def updater(data: dict[str, Any]) -> dict[str, Any]:
            nonlocal cached
            signal = WakeSignal.from_dict(data)
            if signal.wake_signal_id != selected_id or signal.status != "pending":
                return data
            metadata = dict(signal.metadata or {})
            existing = metadata.get("owner_delivery")
            if isinstance(existing, dict):
                cached = signal
                return data
            metadata["owner_delivery"] = dict(delivery)
            cached = replace(signal, metadata=metadata)
            return cached.to_dict()

        update_json_file_atomic(path, updater, require_existing=True)
        return cached

    # LLM: 紧急级别和创建时间排序、limit 及逐条错误必须保持原语义。
    # 函数用途: 合并所选队列的待处理信号与读取错误。
    def pending_report(
        self, *, limit: int = 100, include_normal: bool = True
    ) -> tuple[list[WakeSignal], list[dict[str, Any]]]:
        signals: list[WakeSignal] = []
        load_errors: list[dict[str, Any]] = []
        for kind in _wake_kinds(include_normal):
            kind_signals, kind_errors = self._pending_kind_report(kind)
            signals.extend(kind_signals)
            load_errors.extend(kind_errors)
        signals.sort(key=lambda item: (0 if item.urgency == "urgent" else 1, item.created_at))
        selected = signals if limit <= 0 else signals[:limit]
        return selected, load_errors

    # LLM: 保持先写 handled 回执、再移除 pending、最后确认观察的原顺序。
    # 函数用途: 持久确认一条唤醒已处理，并更新关联观察回执。
    def mark_handled(self, wake_signal_id: str, *, now: float | None = None) -> WakeSignal | None:
        path = self._find_path(wake_signal_id)
        if path is None or not (data := read_json_file(path)):
            return None
        current = now if now is not None else time.time()
        handled = replace(WakeSignal.from_dict(data), status="handled", handled_at=current)
        write_json_file_atomic(
            self.storage.wake_handled_dir / f"{handled.wake_signal_id}.json", handled.to_dict()
        )
        unlink_quietly(path)
        if handled.observation_id:
            self._mark_observations_handled([handled.observation_id], now=handled.handled_at)
        return handled

    # LLM: 只在两个原队列查找精确文件名，不访问归档或使用文本匹配。
    # 函数用途: 定位一个待处理唤醒的现存路径。
    def _find_path(self, wake_signal_id: str) -> Path | None:
        name = f"{wake_signal_id}.json"
        return next(
            (
                path
                for kind in ("urgent", "normal")
                if (path := self.storage.wake_queue_dir / kind / name).exists()
            ),
            None,
        )

    # LLM: 唤醒队列的每轮全量解析在这里收敛为增量索引:未变化文件复用上次权威读取的负载,
    # 变化/新增文件仍按 _read_wake_signal_payload + _wake_signal_from_payload 同路解析。
    # load_error 集合必须逐条保持原样——runtime/guidance 消费方以"有 load_error 就不消费"
    # 作硬门,因此失败文件的结构化错误同样被索引缓存,不得被跳过。
    # 函数用途: 读取一个队列的 pending 信号和错误，复用同源增量索引。
    def _pending_kind_report(self, kind: str) -> tuple[list[WakeSignal], list[dict[str, Any]]]:
        signals: list[WakeSignal] = []
        load_errors: list[dict[str, Any]] = []
        index = self.storage.indexes.get(f"wake:{kind}")
        paths = index.ordered_paths(self.storage.wake_queue_dir / kind, "*.json")
        use_index = self.storage.indexes.usable(
            index,
            key=f"wake:{kind}",
            directory=self.storage.wake_queue_dir / kind,
            pattern="*.json",
        )
        for path in paths:
            _ok, payload, error = self.storage.indexes.read(
                index,
                path,
                _read_wake_signal_entry,
                identity_key="wake_signal_id",
                use_index=use_index,
            )
            if error is not None:
                load_errors.append(error)
            if payload is None:
                continue
            signal, signal_error = _wake_signal_from_payload(path, payload)
            if signal_error is not None:
                load_errors.append(signal_error)
                continue
            if signal is not None and signal.status == "pending":
                signals.append(signal)
        return signals, load_errors

    # LLM: 精确路径只接受 pending 状态，已处理信号不重新激活。
    # 函数用途: 按 ID 读回待处理唤醒对象。
    def _pending_by_id(self, wake_signal_id: str) -> WakeSignal | None:
        path = self._find_path(wake_signal_id)
        data = read_json_file(path) if path is not None else {}
        signal = WakeSignal.from_dict(data) if data else None
        return signal if signal is not None and signal.status == "pending" else None
