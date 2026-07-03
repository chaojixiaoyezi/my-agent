"""后台连续摄取(harvester):把「脚本级吞吐」和「LLM 级判断」在时间上解耦。

真机实锤(§8-1 加难回归):合计 900 条/秒 + 源端滚动缓冲 ~6 分钟淘汰,而 pull 只在
工具调用块内拉流——模型研判/写报告/compact 的几分钟里无人拉,缓冲淘汰=永久丢,
命中全集中在早期序号。本模块给每路 watch 起一个进程内 daemon 线程,按固定节拍
持续「drain→结构化引擎→候选批落盘 spool」,pull 工具改为消费 spool——模型思考
多久都不丢流,候选积压如实入账。

铁律(与 engine 同):本层零自然语言/关键词定性,只做结构化降维与搬运;
候选真假永远留给模型判。

并发契约:
- 引擎/游标/账目只被 harvester 线程(或无 harvester 时的 inline pull)在 state.lock
  下动;spool 是 append-only ndjson,读者无锁。
- 跨进程单收割者:''<watch_id>.harvester.json'' 租约(心跳新鲜=有人在收),别的进程
  只读 spool 不再起线程;租约过期即接管(从持久化游标续,不重不漏)。
- spool 世代轮转:积压清零且文件超限时换代重写,读者按 generation 对齐偏移,
  支撑数天数月长守不涨盘。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..common.json_io import read_json_object_report
from .puller import DrainBudget, drain_source
from .watch_payloads import build_audit_record, candidate_rows, group_rows
from .watch_state import WatchState, audit_append, persist_state, state_dir

_LOGGER = logging.getLogger(__name__)

_HTTP_TIMEOUT_SECONDS = 15
# 窗口走完后的收尾余量:让最后一批事件被拉完、盯守子代理来得及消费。
_WINDOW_GRACE_SECONDS = 120.0
# 源连续失败时的最大退避(不放弃:源恢复即续,缺口由游标+账目如实体现)。
_MAX_ERROR_BACKOFF_SECONDS = 10.0
# 跨进程租约新鲜窗:超过即视为收割者已死,可接管。
_LEASE_FRESH_SECONDS = 15.0
# spool 轮转阈值:积压清零且文件超过此大小时换代,防数月长守涨盘。
_SPOOL_ROTATE_BYTES = 32 * 1024 * 1024


@dataclass
class HarvesterHandle:
    watch_id: str
    stop_event: threading.Event
    thread: threading.Thread
    started_at: float = field(default_factory=time.time)


class _HarvesterRegistry:
    """进程内收割线程注册表(每 watch 至多一个;死线程自动清位)。"""

    def __init__(self) -> None:
        self._handles: dict[str, HarvesterHandle] = {}
        self._lock = threading.Lock()

    def get_live(self, watch_id: str) -> HarvesterHandle | None:
        with self._lock:
            handle = self._handles.get(watch_id)
            if handle is not None and handle.thread.is_alive():
                return handle
            self._handles.pop(watch_id, None)
            return None

    def put(self, handle: HarvesterHandle) -> None:
        with self._lock:
            self._handles[handle.watch_id] = handle

    def stop(self, watch_id: str) -> None:
        with self._lock:
            handle = self._handles.pop(watch_id, None)
        if handle is not None:
            handle.stop_event.set()


harvesters = _HarvesterRegistry()


def spool_path(state: WatchState) -> Path:
    return state_dir(state.owner_home) / f"{state.watch_id}.spool.ndjson"


def _lease_path(state: WatchState) -> Path:
    return state_dir(state.owner_home) / f"{state.watch_id}.harvester.json"


def _read_sidecar_path(state: WatchState) -> Path:
    return state_dir(state.owner_home) / f"{state.watch_id}.read.json"


def ensure_harvester(state: WatchState, fetch_json: Callable) -> dict[str, Any] | None:
    """确保这路 watch 有收割者:本进程有活线程→复用;别处租约新鲜→remote;否则起线程。

    返回 {"mode": "local"|"remote"} ;线程起不来且无 remote 时返回 None(调用方回落
    inline drain,行为与无 harvester 时完全一致)。
    """
    if harvesters.get_live(state.watch_id) is not None:
        return {"mode": "local"}
    lease = _read_lease(state)
    if lease is not None and not _lease_owned_by_me(lease):
        return {"mode": "remote", "lease": lease}
    stop_event = threading.Event()
    thread = threading.Thread(
        target=_harvest_loop,
        name=f"watch-harvester-{state.watch_id}",
        args=(state, fetch_json, stop_event),
        daemon=True,
    )
    try:
        thread.start()
    except RuntimeError:
        _LOGGER.warning("harvester thread start failed (watch=%s)", state.watch_id, exc_info=True)
        return None
    harvesters.put(HarvesterHandle(state.watch_id, stop_event, thread))
    return {"mode": "local"}


def stop_harvester(watch_id: str) -> None:
    harvesters.stop(watch_id)


def _harvest_loop(state: WatchState, fetch_json: Callable, stop_event: threading.Event) -> None:
    backoff = 0.0
    while not stop_event.is_set():
        outcome = _locked_harvest_step(state, fetch_json)
        if outcome == "stop":
            break
        backoff = min(_MAX_ERROR_BACKOFF_SECONDS, backoff + 1.0) if outcome == "error" else 0.0
        stop_event.wait(state.tuning.poll_interval_seconds + backoff)
    _clear_lease(state)


def _locked_harvest_step(state: WatchState, fetch_json: Callable) -> str:
    """跑一拍并续租约;返回 stop/ok/error。任何异常都吞成 error,线程绝不裸死。"""
    try:
        outcome = _harvest_once(state, fetch_json)
        if outcome != "stop":
            _write_lease(state)
        return outcome
    except Exception:
        _LOGGER.warning("harvest cycle failed (watch=%s)", state.watch_id, exc_info=True)
        return "error"


def _harvest_once(state: WatchState, fetch_json: Callable) -> str:
    """持 state.lock 跑一拍:先判停,再收割。"""
    with state.lock:
        if _should_stop(state):
            return "stop"
        return "ok" if _harvest_cycle(state, fetch_json) else "error"


def _should_stop(state: WatchState) -> bool:
    if state.closed:
        return True
    window = int(state.watch_window_seconds or 0)
    elapsed = time.time() - float(state.opened_at or 0.0)
    if window > 0:
        return elapsed > window + _WINDOW_GRACE_SECONDS
    # 无窗长守:长时间没人 pull 消费(盯守方消失)即自停,防孤儿线程白烧;
    # 下一次 pull 会立即重新拉起并从游标续。消费信号认本进程 last_pull_at 与
    # 跨进程读游标 sidecar 的较新者(消费者可能在别的进程)。
    idle_cap = float(state.tuning.harvester_idle_stop_seconds)
    if idle_cap <= 0:
        return False
    cursor = read_spool_cursor(state)
    last_consume = max(
        float(state.last_pull_at or 0.0),
        float(state.opened_at or 0.0),
        float(cursor.get("updated_at") or 0.0),
    )
    return (time.time() - last_consume) > idle_cap


def _harvest_cycle(state: WatchState, fetch_json: Callable) -> bool:
    """一拍:drain→引擎→候选落 spool→账目/快照。返回源是否健康(False=本拍拉流失败)。"""
    budget = DrainBudget(
        max_events=state.tuning.max_events_per_pull,
        page_limit=state.tuning.page_limit,
        deadline=time.time() + _HTTP_TIMEOUT_SECONDS,
    )
    drain = drain_source(fetch_json, state.source_url, state.cursor, budget)
    state.totals["pulls"] += 1
    if drain.error:
        state.totals["http_errors"] += 1
        state.last_error = drain.error
        persist_state(state)
        return False
    state.last_error = ""
    state.cursor = drain.cursor
    state.last_reached_end = drain.reached_end
    state.totals["gap_events"] += drain.gap_events
    digest = state.engine.process(drain.events, time.time())
    if digest.candidates:
        _spool_append(state, drain, digest)
    persist_state(state)
    audit_append(state, build_audit_record(drain, digest))
    return True


def _spool_append(state: WatchState, drain: Any, digest: Any) -> None:
    # 先写盘、成功才冒泡计数:计数即"记录已可读"(消费方以计数判积压,写失败不留幽灵积压)。
    next_seq = state.spool_seq + 1
    record = {
        "spool_seq": next_seq,
        "generation": state.spool_generation,
        "t": round(time.time(), 3),
        "candidates": candidate_rows(digest),
        "suppressed_groups": group_rows(digest),
        "suppressed_groups_total": digest.groups_total,
        "suppressed_events": digest.suppressed_total,
        "overflow_count": len(digest.overflow),
        "cursor_to": drain.cursor,
    }
    path = spool_path(state)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    state.spool_seq = next_seq
    state.totals["spool_candidates"] = state.totals.get("spool_candidates", 0) + len(digest.candidates)
    if read_spool_cursor(state).get("read_seq", 0) >= next_seq - 1:
        _maybe_rotate_spool(state, path)


def _maybe_rotate_spool(state: WatchState, path: Path) -> None:
    """积压已清(读者追平上一条)且文件超限 → 换代重写,读者按 generation 重置偏移。"""
    try:
        if path.stat().st_size < _SPOOL_ROTATE_BYTES:
            return
    except OSError:
        return
    cursor = read_spool_cursor(state)
    if cursor.get("read_seq", 0) < state.spool_seq - 1:
        return
    try:
        _rewrite_spool_keeping_last(state, path)
    except OSError:
        _LOGGER.warning("spool rotation failed (watch=%s)", state.watch_id, exc_info=True)


def _rewrite_spool_keeping_last(state: WatchState, path: Path) -> None:
    """换代重写:只保留最后一条完整记录(读者可能还没读它),世代号 +1。"""
    last_line = ""
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            last_line = line if line.endswith("\n") else last_line
    state.spool_generation += 1
    tmp = path.with_suffix(".ndjson.tmp")
    tmp.write_text(last_line, encoding="utf-8")
    tmp.replace(path)


def read_spool_records(state: WatchState, *, max_candidates: int) -> tuple[list[dict], dict[str, Any]]:
    """读取未消费的 spool 记录(至少 1 条、候选数够 max_candidates 即停),并推进读游标。

    返回 (records, backlog_info)。无新记录返回 ([], backlog_info)。
    """
    cursor = read_spool_cursor(state)
    try:
        records, offset, taken = _scan_spool(state, cursor, max_candidates)
    except OSError:
        return [], _backlog_info(state, cursor)
    if records:
        consumed = int(cursor.get("candidates_consumed") or 0) + taken
        _write_spool_cursor(
            state,
            {
                "read_seq": int(records[-1].get("spool_seq") or 0),
                "offset": offset,
                "generation": state.spool_generation,
                "candidates_consumed": consumed,
                "updated_at": time.time(),
            },
        )
    return records, _backlog_info(state, read_spool_cursor(state))


def _scan_spool(state: WatchState, cursor: dict, max_candidates: int) -> tuple[list[dict], int, int]:
    """从读游标偏移顺扫 spool,收集未读记录;世代不符则从头扫(轮转后偏移作废)。"""
    offset = int(cursor.get("offset") or 0)
    if int(cursor.get("generation") or 0) != state.spool_generation:
        offset = 0
    read_seq = int(cursor.get("read_seq") or 0)
    with spool_path(state).open("r", encoding="utf-8") as handle:
        handle.seek(offset)
        return _collect_spool_rows(handle, offset, read_seq, max_candidates)


def _collect_spool_rows(handle, offset: int, read_seq: int, max_candidates: int) -> tuple[list[dict], int, int]:
    """顺扫已打开的 spool 句柄:跳过已读/坏行,候选凑够额度即停。"""
    records: list[dict] = []
    taken = 0
    while taken < max_candidates:
        row, offset, complete = _next_spool_row(handle, offset)
        if not complete:
            break
        if row is None or int(row.get("spool_seq") or 0) <= read_seq:
            continue
        records.append(row)
        taken += len(row.get("candidates") or [])
    return records, offset, taken


def _next_spool_row(handle, offset: int) -> tuple[dict | None, int, bool]:
    """读下一条完整记录行;尾部半行(写入中)视为流末,offset 停在半行前。"""
    line = handle.readline()
    if not line or not line.endswith("\n"):
        return None, offset, False
    return _parse_spool_line(line), handle.tell(), True


def _parse_spool_line(line: str) -> dict | None:
    try:
        row = json.loads(line)
    except json.JSONDecodeError:
        return None
    return row if isinstance(row, dict) else None


def _backlog_info(state: WatchState, cursor: dict) -> dict[str, Any]:
    written = state.totals.get("spool_candidates", 0)
    consumed = int(cursor.get("candidates_consumed") or 0)
    return {
        "records_unread": max(0, state.spool_seq - int(cursor.get("read_seq") or 0)),
        "candidates_unread": max(0, written - consumed),
    }


def read_spool_cursor(state: WatchState) -> dict[str, Any]:
    report = read_json_object_report(_read_sidecar_path(state), context="watch_spool.read_cursor")
    return {} if report.load_error is not None else dict(report.payload)


def _write_spool_cursor(state: WatchState, payload: dict[str, Any]) -> None:
    path = _read_sidecar_path(state)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        _LOGGER.warning("spool read-cursor write failed (watch=%s)", state.watch_id, exc_info=True)


def _lease_owner() -> str:
    return f"pid:{os.getpid()}"


def _read_lease(state: WatchState) -> dict[str, Any] | None:
    report = read_json_object_report(_lease_path(state), context="watch_harvester.lease")
    if report.load_error is not None:
        return None
    lease = dict(report.payload)
    try:
        fresh = (time.time() - float(lease.get("heartbeat_at") or 0.0)) <= _LEASE_FRESH_SECONDS
    except (TypeError, ValueError):
        return None
    return lease if fresh else None


def _lease_owned_by_me(lease: dict[str, Any]) -> bool:
    return str(lease.get("owner") or "") == _lease_owner()


def _write_lease(state: WatchState) -> None:
    path = _lease_path(state)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"owner": _lease_owner(), "watch_id": state.watch_id, "heartbeat_at": time.time()}
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        _LOGGER.debug("harvester lease write failed (watch=%s)", state.watch_id, exc_info=True)


def _clear_lease(state: WatchState) -> None:
    try:
        _lease_path(state).unlink(missing_ok=True)
    except OSError:
        pass


def harvester_block(state: WatchState) -> dict[str, Any]:
    """给 pull/status 载荷的收割者健康块(模型据此如实上报源健康/积压)。"""
    handle = harvesters.get_live(state.watch_id)
    lease = _read_lease(state)
    return {
        "running": handle is not None or lease is not None,
        "mode": "local" if handle is not None else ("remote" if lease is not None else "off"),
        "spool_seq": state.spool_seq,
        "last_error": state.last_error or "",
    }


__all__ = [
    "ensure_harvester",
    "harvester_block",
    "harvesters",
    "read_spool_cursor",
    "read_spool_records",
    "spool_path",
    "stop_harvester",
]
