# LLM: 观察账本保持原路径；旧回执只读迁移到私有 ack journal，SQLite 损坏不得丢确认；唤醒仍共用事件构造。
# 模块用途: 保存观察事件、读取近期或有界待处理事实，并持久确认处理和原子更新线程活动。
from __future__ import annotations

import copy
import json
import os
import stat
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ..common.json_io import append_private_jsonl_records_unlocked, locked_json_path
from ..common.nofollow_fs import (
    file_descriptor_change_ns,
    file_identity_snapshot,
    open_readonly_file_beneath,
)
from ..gateway_parts.io import read_json_file_report
from ..runtime_errors import DataCorruptionError, runtime_error_report
from ..user_space.storage_lanes import STORAGE_LANE_SLICE_BYTES
from .models import (
    ConversationThread,
    ObservationEvent,
    new_id,
)
from .observation_index import ObservationIndex, ObservationIndexError
from .store_index import _file_fingerprint
from .store_io import (
    json_row,
    now,
    read_jsonl_report,
    read_jsonl_tail_report,
)
from .store_layout import ConversationStorage
from .thread_ledger_segments import (
    archive_dir_for,
    file_stamp,
    read_manifest,
    read_manifest_header,
    recover_pending,
    seal_if_due,
)

_SHARD_MAIN_EVENTS_CACHE_MAX_ENTRIES_COUNT = 512


# LLM: 事件冻结不代表metadata不可变；只复制可变成员，不重新from_dict，也不暴露索引原始负载。
# 函数用途: 返回独立的观察对象列表，防止调用方污染分片记忆。
def _clone_main_events(events: tuple[ObservationEvent, ...]) -> list[ObservationEvent]:
    return [replace(event, metadata=copy.deepcopy(event.metadata) if event.metadata else {}) for event in events]


# LLM: 原ScanIndex读口之后才可复用；强持rows判同一负载；错误/未知handled不记忆，空快照不分配克隆列表。
# 函数用途: 复用未处理主代理事件，保持原过滤与副本隔离，空分片直接返回空 tuple。
def _memo_main_events(store, path, rows, handled, handled_fp, *, cacheable):
    key = str(path)
    with store._memo_lock:
        cached = store._shard_main_events_cache.get(key) if cacheable and handled_fp is not None else None
        snapshot = cached[2] if cached is not None and cached[0] is rows and cached[1] == handled_fp else None
        if snapshot is not None:
            store._shard_main_events_cache.move_to_end(key)
    if snapshot is not None:
        return _clone_main_events(snapshot) if snapshot else ()
    parsed, errors = _observation_events(rows or [], handled)
    snapshot = tuple(event for event in parsed if event.handled_at <= 0 and (event.requires_main_agent or event.requires_llm_report))
    if cacheable and handled_fp is not None and not errors:
        with store._memo_lock:
            store._shard_main_events_cache[key] = (rows, handled_fp, snapshot)
            store._shard_main_events_cache.move_to_end(key)
            while len(store._shard_main_events_cache) > _SHARD_MAIN_EVENTS_CACHE_MAX_ENTRIES_COUNT:
                store._shard_main_events_cache.popitem(last=False)
    return _clone_main_events(snapshot) if snapshot else ()


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
    # LLM: 观察仍沿原 JSONL；ack journal 是持久确认源，SQLite 和 memo 只作派生定位。
    # 函数用途: 连接原观察存储、精确 ack 索引与线程回调，构造本身不执行索引 IO。
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
        self._index = ObservationIndex(storage)
        self._memo_lock = threading.RLock()
        self._handled_cache_fp: tuple | None = None
        self._legacy_cache_fp: tuple | None = None
        self._handled_cache_data: dict[str, float] = {}
        self._shard_main_events_cache: OrderedDict[str, tuple[list, tuple[int, int, int, int], tuple[ObservationEvent, ...]]] = OrderedDict()

    # LLM: Observation append owns its ledger row and activity time only; other thread fields are
    # preserved by the atomic latest-record updater.
    # 函数用途: 追加后台观察事件，并安全刷新活动时间而不覆盖任务、压缩或通道状态。
    def append(self, request: dict, *, max_bytes: int = STORAGE_LANE_SLICE_BYTES) -> ObservationEvent:
        thread_id = str(request.get("thread_id") or "")
        thread = self._require_thread(thread_id)
        event, current = observation_from_request(thread.thread_id, request)
        # 私有追加：0600/0700，存量宽权限观察账本下次写入即收紧；内容逐字节不变。
        path = self.storage.observation_path(thread_id)
        with locked_json_path(path):
            # 锁外预检后线程可能已退休，须在恢复或追加账本前重新核验。
            self._require_thread(thread_id)
            recover_pending(path)
            append_private_jsonl_records_unlocked(path, [event.to_dict()], sort_keys=True)
            if path.stat().st_size >= max_bytes:
                prefix = self._sealable_prefix_end(path)
                seal_if_due(path, max_bytes=max_bytes, sealable_prefix_end=prefix)
        self._update_thread_atomic(
            thread.thread_id,
            lambda latest: replace(
                latest,
                updated_at=max(latest.updated_at, current),
            ),
        )
        return event

    def _sealable_prefix_end(self, path):
        """只查询原 ack 索引；未知或坏行留在活动账中，绝不借封存签发处理确认。"""
        with path.open("rb") as handle:
            while True:
                batch, consumed = [], 0
                for _ in range(128):
                    start = handle.tell()
                    raw = handle.readline(STORAGE_LANE_SLICE_BYTES + 1)
                    if not raw:
                        break
                    if not raw.endswith(b"\n"):
                        return batch[0][0] if batch else start
                    consumed += len(raw)
                    if raw.strip():
                        try:
                            event = ObservationEvent.from_dict(json.loads(raw))
                        except (ValueError, TypeError, KeyError):
                            return batch[0][0] if batch else start
                        batch.append((start, event))
                    if consumed >= 65536:
                        break
                if batch:
                    try:
                        handled = self._index.handled([event.observation_id for _, event in batch])
                    except ObservationIndexError:
                        return batch[0][0]
                    for start, event in batch:
                        if event.handled_at <= 0 and handled.get(event.observation_id, 0) <= 0:
                            return start
                if not raw:
                    return None

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

    # 近期有限窗口优先读尾；全量与未处理筛选仍读全账，ack 仅查询实际读到的精确 ID。
    # 尾窗没有覆盖范围信息，事件不足时保守回退，避免坏行或空白块遮住较早的有效事件。
    def recent_report(
        self,
        thread_id: str,
        *,
        limit: int = 20,
        include_handled: bool = True,
    ) -> tuple[list[ObservationEvent], list[dict[str, Any]]]:
        path = self.storage.observation_path(thread_id)
        directory = archive_dir_for(path)
        if not path.exists() and not directory.exists():
            return [], []
        with locked_json_path(path):
            recover_pending(path)
            events, errors = self._recent_path_report(path, limit=limit, include_handled=include_handled) if path.exists() else ([], [])
            # 未处理观察按合同始终留在活动账；常规待处理查询不能反扫全部已处理归档。
            if not include_handled or (limit > 0 and len(events) >= limit) or not directory.exists():
                return events, errors
            header = read_manifest_header(directory) if limit > 0 else None
            # 有限窗口从首行序号倒读，最多探测固定数量或本次显式请求数量的段；不解析历史段列表。
            if header is not None:
                last = header["last_seq"]
                lower = max(0, last - max(128, limit))
                paths = (directory / f"seg-{sequence:06d}.jsonl" for sequence in range(last, lower, -1))
                manifest = header
            else:
                manifest = read_manifest(directory)
                paths = (directory / entry["file"] for entry in reversed(manifest["segments"]))
                lower = 0
            errors.extend(manifest.get("load_errors", []))
            if manifest["coverage"] == "partial" and not manifest.get("load_errors"):
                errors.append({"coverage": "partial", "path": str(directory / "manifest.json"),
                               "message": "thread ledger manifest was rebuilt"})
            chunks = [events]
            count = len(events)
            for segment in paths:
                if file_stamp(segment) is None:
                    errors.append({"coverage": "partial", "path": str(segment), "message": "thread ledger segment is missing"})
                    continue
                older, load_errors = self._recent_path_report(segment, limit=max(0, limit - count), include_handled=True)
                errors.extend(load_errors)
                chunks.append(older)
                count += len(older)
                if limit > 0 and count >= limit:
                    break
            if lower and count < limit:
                errors.append({"coverage": "partial", "path": str(directory),
                               "message": "thread ledger recent window exceeds bounded archive probe"})
            return [event for chunk in reversed(chunks) for event in chunk], errors

    def recent_window_report(self, thread_id: str, *, limit: int = 128,
                             max_bytes: int = 262144, max_segments: int = 8):
        """只读有界近期展示；不足时明确partial，不恢复、扫描目录或回退全历史。

        complete仅证明所请求近期窗口已覆盖，绝不签发任务归属或全历史完整性。
        字节计数包括最多512字节的归档导航预算；ack索引另守原固定批次合同。
        """
        if (type(limit) is not int or not 1 <= limit <= 128
                or type(max_bytes) is not int or not 1 <= max_bytes <= 1048576
                or type(max_segments) is not int or not 0 <= max_segments <= 128):
            raise ValueError("观察窗口需要1至128条、1至1048576字节及0至128段预算")
        path = self.storage.observation_path(thread_id)
        directory = archive_dir_for(path)
        errors, reasons, chunks = [], [], []
        remaining, segments, count = max_bytes, 0, 0
        def partial(code, target):
            if code not in reasons:
                reasons.append(code)
            errors.append({"coverage": "partial", "reason_code": code, "path": str(target)})
        with locked_json_path(path):
            header = None
            has_archive = directory.exists()
            if has_archive:
                if remaining < 512:
                    partial("byte_budget_exhausted", directory)
                else:
                    remaining -= 512
                    try:
                        navigation_before = file_stamp(directory / "manifest.json")
                        header = read_manifest_header(directory)
                        if navigation_before != file_stamp(directory / "manifest.json"):
                            partial("archive_navigation_changed", directory)
                            header = None
                    except (OSError, ValueError, TypeError, DataCorruptionError) as exc:
                        errors.append(runtime_error_report(exc, context="conversation.observations.window"))
                    if header is None or header.get("pending") is not None:
                        partial("archive_navigation_unknown", directory)
                        header = None
                    elif header["coverage"] != "complete":
                        partial("archive_coverage_partial", directory)
            paths = [(path, False)]
            if header is not None:
                last = header["last_seq"]
                paths.extend((directory / f"seg-{seq:06d}.jsonl", True)
                             for seq in range(last, max(0, last - max_segments), -1))
            truncated = False
            all_ranges_read = True
            for target, archived in paths:
                if count >= limit:
                    all_ranges_read = False
                    break
                if remaining <= 0:
                    partial("byte_budget_exhausted", target)
                    all_ranges_read = False
                    break
                if archived:
                    segments += 1
                rows, load_errors, cut, consumed, missing = _observation_tail_window(target, remaining, limit - count)
                remaining -= consumed
                all_ranges_read = all_ranges_read and not cut
                errors.extend(load_errors)
                if load_errors:
                    partial("unreadable_rows", target)
                if missing and archived:
                    partial("archive_segment_missing", target)
                try:
                    handled = self._read_handled([str(row.get("observation_id") or "") for row in rows]) if rows else {}
                except ObservationIndexError as exc:
                    handled = {}
                    errors.append(runtime_error_report(exc, context="conversation.observations.window.ack"))
                    partial("handled_coverage_unknown", target)
                events, parse_errors = _observation_events(rows, handled)
                errors.extend(parse_errors)
                if parse_errors:
                    partial("unreadable_events", target)
                chunks.append(events)
                count += len(events)
                if cut and count < limit:
                    truncated = True
                    partial("tail_range_incomplete", target)
                    # 当前文件还有未读的较新记录，不能用更旧归档冒充连续尾窗。
                    break
            if count < limit and header is not None and header["last_seq"] > segments and not truncated:
                partial("archive_segment_budget_exhausted", directory)
            # 小首行只授权近期导航；不读取完整manifest时不能证明全部历史封印。
            if has_archive and count < limit and not reasons:
                partial("archive_history_unverified", directory)
        source_exhausted = not reasons and all_ranges_read and not has_archive
        coverage = {"status": "partial" if reasons else "complete", "reason_codes": reasons,
                    "requested_window_complete": not reasons and (count >= limit or source_exhausted),
                    "source_exhausted": source_exhausted,
                    "history_coverage": "complete" if source_exhausted else "unknown",
                    "limit": limit, "bytes_read_upper_bound": max_bytes - remaining,
                    "segments_read": segments}
        return [event for chunk in reversed(chunks) for event in chunk][-limit:], errors, coverage

    def _recent_path_report(self, path, *, limit, include_handled):
        """保留原单文件尾窗和坏行回退语义；跨段只在当前窗口不足时继续。"""
        if include_handled and limit > 0:
            report = read_jsonl_tail_report(
                path, context="conversation.observations.read", limit=limit
            )
            handled = self._read_handled([str(row.get("observation_id") or "") for row in report.rows])
            events, parse_errors = _observation_events(report.rows, handled)
            if len(events) >= limit:
                return events[-limit:], [*report.load_errors, *parse_errors]
        report = read_jsonl_report(
            path,
            context="conversation.observations.read",
        )
        handled = self._read_handled([str(row.get("observation_id") or "") for row in report.rows])
        events, parse_errors = _observation_events(report.rows, handled)
        if not include_handled:
            events = [event for event in events if event.handled_at <= 0]
        selected = events if limit <= 0 else events[-limit:]
        return selected, [*report.load_errors, *parse_errors]

    # LLM: 大历史包括 limit=0 均只查 pending；仅固定 16 分片/64KiB 内的小集合保留旧 ScanIndex 记忆化。
    # 函数用途: 汇总未处理主代理观察，limit<=0 只放开活跃结果数，不放开历史扫描预算。
    def unhandled_requiring_main(self, *, limit: int = 20) -> list[ObservationEvent]:
        if not self._small_legacy_snapshot():
            return self._index.pending(limit)
        index = self.storage.indexes.get("observations")
        paths = index.ordered_paths(self.storage.observations_dir, "*.jsonl")
        use_index = self.storage.indexes.usable(
            index, key="observations", directory=self.storage.observations_dir, pattern="*.jsonl"
        )
        shards = []
        ids = []
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
            shards.append((target, rows, _ok))
            ids.extend(str(row.get("observation_id") or "") for row in rows or [])
        with self._memo_lock:
            handled = self._read_handled(ids)
            handled_fp = self._handled_cache_fp
        events: list[ObservationEvent] = []
        for target, rows, _ok in shards:
            events.extend(_memo_main_events(self, target, rows, handled, handled_fp,
                                           cacheable=_ok and use_index and rows is not None))
        events.sort(key=lambda item: item.observed_at)
        return events if limit <= 0 else events[:limit]

    def unhandled_requiring_main_report(
        self, thread_id: str, *, limit: int = 0,
    ) -> tuple[list[ObservationEvent], list[dict[str, Any]]]:
        """按线程读取全部或指定数量的未处理活动项；恢复未追平时明确返回未知。"""
        try:
            if type(thread_id) is not str or not thread_id or type(limit) is not int or limit < 0:
                raise ValueError("未处理观察需要准确线程身份和非负数量")
            return self._index.pending(limit, thread_id=thread_id), []
        except Exception as exc:  # noqa: BLE001 - report边界保留原异常类型，不以空结果签发完整性
            error = runtime_error_report(exc, context="conversation.observations.thread_pending")
            error["coverage"] = "partial"
            return [], [error]

    # LLM: 旧 ScanIndex 的小集合记忆化保留，但准入先有界探测 16 个分片/合计 64KiB；大历史只能走 pending。
    # 函数用途: 判断当前数据能否在固定预算内沿原读取口完成精确快照。
    def _small_legacy_snapshot(self):
        path = self.storage.observation_handled_path
        if path.exists() and path.stat().st_size > 65536:
            return False
        total, count = 0, 0
        with os.scandir(self.storage.observations_dir) as entries:
            for entry in entries:
                count += 1
                if count > 16:
                    return False
                if entry.name.endswith(".jsonl") and entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
                    if total > 65536:
                        return False
        return True

    # LLM: 只靠 canonical 行的 wake_signal_id 关联；索引保留普通及主代理观察，不读损坏信封或扫描已处理历史。
    # 函数用途: 持久确认链接到某条唤醒的全部未处理观察，恢复未追平时明确报 partial。
    def mark_handled_for_wake(self, wake_signal_id: str, *, now: float | None = None) -> list[str]:
        selected = str(wake_signal_id or "").strip()
        if not selected:
            return []
        ids = [event.observation_id for event in self._index.pending(0, wake=selected)]
        self.mark_handled(ids, now=now)
        return ids

    # LLM: 只按传入 ID 追加首次确认，SQLite 幂等 upsert；不读 legacy、不重写任何历史 map。
    # 函数用途: 持久确认一批精确观察 ID，重复与崩溃重放不改变首次正处理时间。
    def mark_handled(
        self, observation_ids: list[str] | tuple[str, ...], *, now: float | None = None
    ) -> None:
        ids = [str(item) for item in observation_ids if str(item or "").strip()]
        if ids:
            current = now if now is not None else time.time()
            self._index.ack(ids, current)

    # LLM: 此兼容读口只查询本次实际行 ID；journal 与 legacy 双版本使显式全量扫描的 memo 即时失效。
    # 函数用途: 获取候选处理时间，不复制 owner 的整张确认表；未知迁移状态明确报错。
    def _read_handled(self, observation_ids=()) -> dict[str, float]:
        with self._memo_lock:
            ids = list(dict.fromkeys(observation_ids))
            handled = self._index.handled(ids, include_legacy=False)
            path = self.storage.observation_handled_path
            fp = _file_fingerprint(path)
            journal_fp = _file_fingerprint(self._index.journal_path)
            self._handled_cache_fp = None
            if all(handled.get(key, 0) > 0 for key in ids):
                self._handled_cache_fp = (journal_fp, fp) if journal_fp is not None or fp is not None else None
                return handled
            if fp is not None and fp[1] <= 65536:
                stable = fp == self._legacy_cache_fp
                if stable:
                    legacy = self._handled_cache_data
                else:
                    report = read_json_file_report(path)
                    if report.load_error:
                        raise ObservationIndexError("legacy observation handled map is unreadable")
                    legacy = {}
                    for key, value in report.payload.items():
                        try:
                            legacy[str(key)] = float(value or 0)
                        except (ValueError, TypeError):
                            continue
                    stable = _file_fingerprint(path) == fp
                    if stable:
                        self._legacy_cache_fp, self._handled_cache_data = fp, legacy
                # 只合并候选；小图可精确查尾部，同时固定批迁移持久事实，不把 partial 当未处理。
                for key in ids:
                    if handled.get(key, 0) <= 0 and key in legacy:
                        handled[key] = legacy[key]
                if stable:
                    self._index.migrate_legacy()
                    self._handled_cache_fp = (_file_fingerprint(self._index.journal_path), fp)
            elif fp is not None:
                handled = self._index.handled(ids)
                self._handled_cache_fp = (_file_fingerprint(self._index.journal_path), fp)
            else:
                # 缺失也须核对迁移游标；迁移到一半的旧图删除不能把尾部未知 ack 当未处理。
                handled = self._index.handled(ids)
                if journal_fp is not None:
                    self._handled_cache_fp = (journal_fp, None)
            return handled


def _observation_tail_window(path, max_bytes, limit):
    """一个nofollow FD只读固定尾窗；不跨预算找有效行，保留损坏与代次变化事实。"""
    try:
        descriptor = open_readonly_file_beneath(path.parent, (path.name,))
    except FileNotFoundError:
        return [], [], False, 0, True
    except OSError as exc:
        return [], [runtime_error_report(exc, context="conversation.observations.window")], True, 0, False
    errors, rows, consumed = [], [], 0
    try:
        with os.fdopen(descriptor, "rb") as handle:
            before = os.fstat(handle.fileno())
            changed = file_descriptor_change_ns(handle.fileno(), before)
            if not stat.S_ISREG(before.st_mode):
                return [], [{"reason_code": "invalid_ledger_type", "path": str(path)}], True, 0, False
            start = max(0, before.st_size - max_bytes)
            handle.seek(start)
            raw = handle.read(max_bytes)
            consumed = len(raw)
            current, current_changed = file_identity_snapshot(path)
            after = os.fstat(handle.fileno())
            final_changed = file_descriptor_change_ns(handle.fileno(), after)
    except OSError as exc:
        return [], [runtime_error_report(exc, context="conversation.observations.window")], True, max_bytes, False
    def fields(info):
        return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
                info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    if (fields(before) != fields(after) or fields(current) != fields(after)
            or changed is None or changed != current_changed or changed != final_changed):
        errors.append({"reason_code": "ledger_changed_during_read", "path": str(path)})
    lines = raw.split(b"\n")
    if start:
        lines = lines[1:]
    nonempty = [line for line in lines if line.strip()]
    lines = nonempty[-limit:]
    for line in lines:
        try:
            text = line.decode("utf-8")
        except UnicodeError as exc:
            errors.append(runtime_error_report(exc, context="conversation.observations.window"))
            continue
        row, error = json_row(text, context="conversation.observations.window", path=path, line_number=0)
        if error is not None:
            errors.append(error)
        elif row is not None:
            rows.append(row)
    return rows, errors, start > 0 or len(nonempty) > limit, consumed, False
