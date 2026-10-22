# LLM: 观察账本与处理回执保持原路径；唤醒共用事件构造，修改须联测追加顺序、扫描与处理去重。
# 模块用途: 保存观察事件、读取近期或待处理事实，并原子更新处理时间和线程活动投影。
from __future__ import annotations

import copy
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ..common.json_io import append_private_jsonl_records
from ..gateway_parts.io import (
    read_json_file_report,
    update_json_file_atomic,
)
from ..runtime_errors import runtime_error_report
from .models import (
    ConversationThread,
    ObservationEvent,
    new_id,
)
from .store_index import _file_fingerprint
from .store_io import (
    now,
    read_jsonl_report,
)
from .store_layout import ConversationStorage

_SHARD_MAIN_EVENTS_CACHE_MAX_ENTRIES_COUNT = 512


# LLM: 事件冻结不代表metadata不可变；只复制可变成员，不重新from_dict，也不暴露索引原始负载。
# 函数用途: 返回独立的观察对象列表，防止调用方污染分片记忆。
def _clone_main_events(events: tuple[ObservationEvent, ...]) -> list[ObservationEvent]:
    return [replace(event, metadata=copy.deepcopy(event.metadata) if event.metadata else {}) for event in events]


# LLM: 原ScanIndex读口之后才可复用；强持rows并用is判同一负载，避免裸id复用；错误/未知handled版本不记忆。
# 函数用途: 复用稳定分片的未处理主代理事件，保持原handled_at和requires过滤。
def _memo_main_events(store, path, rows, handled, handled_fp, *, cacheable):
    key = str(path)
    with store._memo_lock:
        cached = store._shard_main_events_cache.get(key) if cacheable and handled_fp is not None else None
        snapshot = cached[2] if cached is not None and cached[0] is rows and cached[1] == handled_fp else None
        if snapshot is not None:
            store._shard_main_events_cache.move_to_end(key)
    if snapshot is not None:
        return _clone_main_events(snapshot)
    parsed, errors = _observation_events(rows or [], handled)
    snapshot = tuple(event for event in parsed if event.handled_at <= 0 and (event.requires_main_agent or event.requires_llm_report))
    if cacheable and handled_fp is not None and not errors:
        with store._memo_lock:
            store._shard_main_events_cache[key] = (rows, handled_fp, snapshot)
            store._shard_main_events_cache.move_to_end(key)
            while len(store._shard_main_events_cache) > _SHARD_MAIN_EVENTS_CACHE_MAX_ENTRIES_COUNT:
                store._shard_main_events_cache.popitem(last=False)
    return _clone_main_events(snapshot)


# LLM: 处理账是唯一确认源，观察行不被回写或复制。
# 函数用途: 将已有处理时间合并到读取出来的观察对象。
def with_handled_at(event: ObservationEvent, handled: dict[str, float]) -> ObservationEvent:
    handled_at = float(handled.get(event.observation_id) or event.handled_at or 0.0)
    return event if handled_at == event.handled_at else replace(event, handled_at=handled_at)


# LLM: 事件输入来自已校验线程和显式字段，不能由展示摘要推断归属。
# 类用途: 收拢观察构造所需字段，不持有可变运行状态。
@dataclass(frozen=True)
class _ObservationEventInput:
    thread_id: str
    event_type: str
    summary: str
    current: float
    kwargs: dict[str, Any]


# LLM: 沿用宿主指定的观察 ID 或新生成 ID；构造无写盘副作用。
# 函数用途: 将结构化字段变成待保存的观察事件。
def _observation_event(request: _ObservationEventInput) -> ObservationEvent:
    kwargs = request.kwargs
    refs = kwargs.get("evidence_refs") or []
    return ObservationEvent(
        observation_id=str(kwargs.get("_observation_id") or "").strip() or new_id("obs"),
        thread_id=request.thread_id,
        event_type=str(request.event_type or "observation"),
        summary=str(request.summary or ""),
        urgency=str(kwargs.get("urgency") or "normal"),
        severity=str(kwargs.get("severity") or ""),
        source_agent_id=str(kwargs.get("source_agent_id") or ""),
        parent_agent_id=str(kwargs.get("parent_agent_id") or ""),
        root_task_id=str(kwargs.get("root_task_id") or ""),
        evidence_refs=tuple(str(item) for item in refs),
        requires_main_agent=bool(kwargs.get("requires_main_agent")),
        requires_llm_report=bool(kwargs.get("requires_llm_report")),
        observed_at=request.current,
        metadata=kwargs.get("metadata") or {},
    )


# LLM: 观察追加和唤醒联合发布必须复用同一构造规则与时间来源。
# 函数用途: 归一观察请求并返回事件和观察时间，不追加账本。
def observation_from_request(thread_id: str, request: dict) -> tuple[ObservationEvent, float]:
    current = now(request.get("now"))
    kwargs = {
        "urgency": request.get("urgency", "normal"),
        "severity": request.get("severity", ""),
        "source_agent_id": request.get("source_agent_id", ""),
        "parent_agent_id": request.get("parent_agent_id", ""),
        "root_task_id": request.get("root_task_id", ""),
        "evidence_refs": request.get("evidence_refs") or [],
        "requires_main_agent": request.get("requires_main_agent", False),
        "requires_llm_report": request.get("requires_llm_report", False),
        "metadata": request.get("metadata") or {},
        "_observation_id": request.get("_observation_id", ""),
    }
    return (
        _observation_event(
            _ObservationEventInput(
                thread_id,
                str(request.get("event_type") or ""),
                str(request.get("summary") or ""),
                current,
                kwargs,
            )
        ),
        current,
    )


# LLM: 成功行与逐行坏账报告并行返回，不能将坏行默认为空观察。
# 函数用途: 把 JSONL 行转换成带处理回执的观察对象。
def _observation_events(
    rows: list[dict[str, Any]],
    handled: dict[str, float],
) -> tuple[list[ObservationEvent], list[dict[str, Any]]]:
    events: list[ObservationEvent] = []
    errors: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        try:
            events.append(with_handled_at(ObservationEvent.from_dict(row), handled))
        except Exception as exc:
            report = runtime_error_report(exc, context="conversation.observations.parse")
            report["row_index"] = index
            errors.append(report)
    return events, errors


# LLM: 观察分片是追加式 JSONL,索引负载就是 read_jsonl_report 的原始行;脏行会让该分片
# 保持"读权威"路径(不入索引),以免把带解析错误的快照固化下来。
# 函数用途: 读取一个观察分片的权威行,供增量索引与全量扫描共用同一解析口径。
def _read_observation_shard_entry(
    path: Path,
) -> tuple[bool, list[dict[str, Any]] | None, dict[str, Any] | None]:
    report = read_jsonl_report(path, context="conversation.observations.read")
    error = report.load_errors[0] if report.load_errors else None
    return error is None, report.rows, error


# LLM: 只拥有观察账和处理回执；线程活动修改使用注入的原子更新能力。
# 类用途: 提供观察追加、近期读取、待处理扫描和确认接口。
class ObservationStore:
    # LLM: 观察与活动仍沿原文件；处理和分片memo仅为有界派生投影，RLock绑定处理数据与指纹，不产生处理权威。
    # 函数用途: 连接原观察存储与线程回调，初始化只读热拍记忆。
    def __init__(
        self,
        storage: ConversationStorage,
        *,
        require_thread: Callable[[str], ConversationThread],
        update_thread_atomic: Callable[
            [str, Callable[[ConversationThread], ConversationThread]], ConversationThread
        ],
    ) -> None:
        self.storage = storage
        self._require_thread = require_thread
        self._update_thread_atomic = update_thread_atomic
        self._memo_lock = threading.RLock()
        self._handled_cache_fp: tuple[int, int, int, int] | None = None
        self._handled_cache_data: dict[str, float] = {}
        self._shard_main_events_cache: OrderedDict[str, tuple[list, tuple[int, int, int, int], tuple[ObservationEvent, ...]]] = OrderedDict()

    # LLM: Observation append owns its ledger row and activity time only; other thread fields are
    # preserved by the atomic latest-record updater.
    # 函数用途: 追加后台观察事件，并安全刷新活动时间而不覆盖任务、压缩或通道状态。
    def append(self, request: dict) -> ObservationEvent:
        thread_id = str(request.get("thread_id") or "")
        thread = self._require_thread(thread_id)
        event, current = observation_from_request(thread.thread_id, request)
        # 私有追加：0600/0700，存量宽权限观察账本下次写入即收紧；内容逐字节不变。
        append_private_jsonl_records(self.storage.observation_path(thread_id), [event.to_dict()], sort_keys=True)
        self._update_thread_atomic(
            thread.thread_id,
            lambda latest: replace(
                latest,
                updated_at=max(latest.updated_at, current),
            ),
        )
        return event

    # LLM: 保留近期选择的原 include_handled/limit 语义，严格调用方使用 report 接口。
    # 函数用途: 读取指定线程的近期观察，不标记为已处理。
    def recent(
        self, thread_id: str, *, limit: int = 20, include_handled: bool = True
    ) -> list[ObservationEvent]:
        events, _errors = self.recent_report(
            thread_id,
            limit=limit,
            include_handled=include_handled,
        )
        return events

    # LLM: An observation ledger is append-on-first-use; absence is an authoritative empty
    # collection, while unreadable or malformed existing rows remain explicit load errors.
    # 函数用途: 读取会话观察事件；尚未产生任何观察时正常返回空列表，不把未创建文件误报成会话损坏。
    def recent_report(
        self,
        thread_id: str,
        *,
        limit: int = 20,
        include_handled: bool = True,
    ) -> tuple[list[ObservationEvent], list[dict[str, Any]]]:
        handled = self._read_handled()
        path = self.storage.observation_path(thread_id)
        if not path.exists():
            return [], []
        report = read_jsonl_report(
            path,
            context="conversation.observations.read",
        )
        events, parse_errors = _observation_events(report.rows, handled)
        if not include_handled:
            events = [event for event in events if event.handled_at <= 0]
        selected = events if limit <= 0 else events[-limit:]
        return selected, [*report.load_errors, *parse_errors]

    # LLM: 每轮仍完整调用原索引以保持metrics；处理数据与版本同锁取快照，分片memo只省建模，排序/limit不变。
    # 函数用途: 汇总未处理主代理观察，复用未变化分片但即时反映处理标记。
    def unhandled_requiring_main(self, *, limit: int = 20) -> list[ObservationEvent]:
        with self._memo_lock:
            handled = self._read_handled()
            handled_fp = self._handled_cache_fp
        index = self.storage.indexes.get("observations")
        paths = index.ordered_paths(self.storage.observations_dir, "*.jsonl")
        use_index = self.storage.indexes.usable(
            index, key="observations", directory=self.storage.observations_dir, pattern="*.jsonl"
        )
        events: list[ObservationEvent] = []
        for path in paths:
            # 读取目标仍按 path.stem 反推(与旧实现 recent_observations(path.stem) 完全同路),
            # 因此文件名与 thread_id 约定不一致时仍然读不到东西,不会因为换了扫描方式而多读。
            target = self.storage.observation_path(path.stem)
            _ok, rows, _error = self.storage.indexes.read(
                index,
                target,
                _read_observation_shard_entry,
                use_index=use_index,
            )
            events.extend(_memo_main_events(self, target, rows, handled, handled_fp,
                                           cacheable=_ok and use_index and rows is not None))
        events.sort(key=lambda item: item.observed_at)
        return events if limit <= 0 else events[:limit]

    # LLM: 给读不出信封的唤醒结案用：只靠观察记录自带的 wake_signal_id 链接找关联观察，不读唤醒信封；扫全部分片
    #   （唤醒的 thread 未知），只标未处理的。返回本次标掉的观察 ID，空列表表示没有关联观察。副作用同 mark_handled。
    # 函数用途: 把链接到某条唤醒的全部未处理观察标成已处理。
    def mark_handled_for_wake(self, wake_signal_id: str, *, now: float | None = None) -> list[str]:
        selected = str(wake_signal_id or "").strip()
        if not selected:
            return []
        handled = self._read_handled()
        directory = self.storage.observations_dir
        ids: list[str] = []
        for path in sorted(directory.glob("*.jsonl")) if directory.is_dir() else ():
            report = read_jsonl_report(path, context="conversation.observations.read")
            events, _parse_errors = _observation_events(report.rows, handled)
            ids.extend(event.observation_id for event in events
                       if event.wake_signal_id == selected and event.handled_at <= 0)
        self.mark_handled(ids, now=now)
        return ids

    # LLM: 处理 ID 与时间一次原子合并到原回执，不覆盖其它观察的确认事实。
    # 函数用途: 持久确认一批精确观察 ID 已处理。
    def mark_handled(
        self, observation_ids: list[str] | tuple[str, ...], *, now: float | None = None
    ) -> None:
        ids = [str(item) for item in observation_ids if str(item or "").strip()]
        if ids:
            current = now if now is not None else time.time()
            update_json_file_atomic(
                self.storage.observation_handled_path,
                lambda data: {**data, **dict.fromkeys(ids, current)},
            )

    # LLM: 原数字时间规则不变；仅成功且读前后指纹稳定才记忆，异常/缺失先清版本，绝不复用旧处理代次。
    # 函数用途: 读取处理回执或克隆未变映射，保持坏值忽略和读取失败空结果语义。
    def _read_handled(self) -> dict[str, float]:
        with self._memo_lock:
            path = self.storage.observation_handled_path
            fp = _file_fingerprint(path)
            if fp is not None and fp == self._handled_cache_fp:
                return dict(self._handled_cache_data)
            self._handled_cache_fp = None
            report = read_json_file_report(path)
            handled: dict[str, float] = {}
            for key, value in report.payload.items():
                try:
                    handled[str(key)] = float(value or 0.0)
                except (TypeError, ValueError):
                    continue
            if not report.load_error and fp is not None and _file_fingerprint(path) == fp:
                self._handled_cache_fp = fp
                self._handled_cache_data = dict(handled)
            return handled
