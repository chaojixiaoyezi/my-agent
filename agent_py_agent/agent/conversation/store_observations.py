# LLM: 观察账本与处理回执保持原路径；唤醒共用事件构造，修改须联测追加顺序、扫描与处理去重。
# 模块用途: 保存观察事件、读取近期或待处理事实，并原子更新处理时间和线程活动投影。
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ..gateway_parts.io import (
    read_json_file,
    update_json_file_atomic,
)
from ..io.jsonl import append_jsonl
from ..runtime_errors import runtime_error_report
from .models import (
    ConversationThread,
    ObservationEvent,
    new_id,
)
from .store_io import (
    now,
    read_jsonl_report,
)
from .store_layout import ConversationStorage


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
    # LLM: 观察与活动投影共用原文件和线程更新回调；构造不创建第二份处理状态。
    # 函数用途: 为观察事件提供同源存储和线程存在校验、原子活动时间更新能力。
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

    # LLM: Observation append owns its ledger row and activity time only; other thread fields are
    # preserved by the atomic latest-record updater.
    # 函数用途: 追加后台观察事件，并安全刷新活动时间而不覆盖任务、压缩或通道状态。
    def append(self, request: dict) -> ObservationEvent:
        thread_id = str(request.get("thread_id") or "")
        thread = self._require_thread(thread_id)
        event, current = observation_from_request(thread.thread_id, request)
        append_jsonl(self.storage.observation_path(thread_id), event.to_dict(), sort_keys=True)
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

    # LLM: 观察事件是全 owner 级台账,轮询路径每轮都要读全部未处理项;这里把"每分片重复
    # 读取 handled 映射"收敛为一次读取,并用增量索引跳过未变化分片的重复解析。返回集合、
    # 顺序(先全序 stable sort 再 limit)、handled/requires_* 过滤语义与旧实现逐字一致。
    # 函数用途: 汇总所有会话里要求主代理处理且尚未处理的观察事件,按观察时间升序取前 limit 条。
    def unhandled_requiring_main(self, *, limit: int = 20) -> list[ObservationEvent]:
        handled = self._read_handled()
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
            parsed, _parse_errors = _observation_events(rows or [], handled)
            events.extend(
                event
                for event in parsed
                if event.handled_at <= 0
                and (event.requires_main_agent or event.requires_llm_report)
            )
        events.sort(key=lambda item: item.observed_at)
        return events if limit <= 0 else events[:limit]

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

    # LLM: 按原规则读取数字确认时间，不从报告或摘要推断消费状态。
    # 函数用途: 加载已有观察处理回执，供读取投影和未处理扫描共用。
    def _read_handled(self) -> dict[str, float]:
        handled: dict[str, float] = {}
        for key, value in read_json_file(self.storage.observation_handled_path).items():
            try:
                handled[str(key)] = float(value or 0.0)
            except (TypeError, ValueError):
                continue
        return handled
