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
import math
import os
import re
import shutil
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..common.json_io import (
    locked_json_path,
    read_json_object_report,
    read_jsonl_objects_report,
    write_json_file_atomic_unlocked,
)
from .puller import DrainBudget, DrainResult
from .watch_payloads import (
    build_audit_record,
    candidate_model_view,
    candidate_rows,
    frequent_hit_rows,
    group_rows,
)
from .watch_state import (
    WatchState,
    _unique_tmp,
    audit_append,
    load_state,
    persist_state,
    state_dir,
)

_LOGGER = logging.getLogger(__name__)

_HTTP_TIMEOUT_SECONDS = 15
# 源连续失败时的最大退避(不放弃:源恢复即续,缺口由游标+账目如实体现)。
_MAX_ERROR_BACKOFF_SECONDS = 10.0
# 跨进程租约新鲜窗:超过即视为收割者已死,可接管。
_LEASE_FRESH_SECONDS = 15.0
# spool 轮转阈值:积压清零且文件超过此大小时换代,防数月长守涨盘。
_SPOOL_ROTATE_BYTES = 32 * 1024 * 1024
_HARVEST_TRANSACTION_SCHEMA = "watch-harvest-transaction.v1"
# A physical spool row is only an implementation unit.  Keep normal Audit rows
# small enough that the consumer can combine them against the active model's
# byte budget without ever splitting one source record.
_AUDIT_SOURCE_REF_RE = re.compile(
    r"^audit://(?P<watch_id>ws-[0-9a-f]{10})/candidate/"
    r"(?P<ack_id>[0-9]+:[0-9]+)$"
)
_AUDIT_DELIVERY_REF_SCHEMA = "audit-delivery.v1"
_AUDIT_VERDICT_TOKEN_SCHEMA = "audit-verdict-token.v1"
_AUDIT_BATCH_RECOVERY_SCHEMA = "audit-batch-recovery.v1"
_INGEST_RATE_WINDOW_SECONDS = 10.0
_INGEST_RATE_STALE_SECONDS = 30.0


@dataclass
class HarvesterHandle:
    watch_id: str
    lease_id: str
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
    """权威交付游标 sidecar；消费身份可接管，但不会按 Agent 数量拆成竞争游标。"""
    return state_dir(state.owner_home) / f"{state.watch_id}.read.json"


class _HarvestTransactionUnavailable(OSError):
    """A pending harvest cannot be proved committed or safely rolled back."""


# LLM: One small owner-scoped intent file closes the crash window between source
# fetch, durable spool/audit writes, and publication of the canonical cursor.
# 函数用途: 同一收割拍只使用现有 spool 和状态快照，意图文件仅标记原子提交边界。
def _harvest_transaction_path(state: WatchState) -> Path:
    return state_dir(state.owner_home) / f"{state.watch_id}.harvest-intent.json"


def _harvest_fragment_backup_path(state: WatchState) -> Path:
    return state_dir(state.owner_home) / f"{state.watch_id}.harvest-fragment.bak"


def _audit_ledger_path(state: WatchState) -> Path:
    return state_dir(state.owner_home) / f"{state.watch_id}.audit.ndjson"


def _path_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def _fsync_directory(path: Path) -> None:
    """Best-effort directory sync after replacing or removing transaction files."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _write_harvest_transaction(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _unique_tmp(path)
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(path)
        _fsync_directory(path.parent)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _read_harvest_transaction(state: WatchState) -> dict[str, Any] | None:
    path = _harvest_transaction_path(state)
    if not path.exists():
        return None
    report = read_json_object_report(path, context="watch_harvest.transaction")
    payload = report.payload
    start = payload.get("start")
    if (
        report.load_error is not None
        or payload.get("schema_version") != _HARVEST_TRANSACTION_SCHEMA
        or str(payload.get("watch_id") or "") != state.watch_id
        or not str(payload.get("transaction_id") or "")
        or not isinstance(start, dict)
    ):
        raise _HarvestTransactionUnavailable(
            f"harvest transaction marker is corrupt for {state.watch_id}"
        )
    return payload


def _state_snapshot(state: WatchState) -> WatchState:
    restored = load_state(state.owner_home, state.watch_id)
    if restored is None:
        raise _HarvestTransactionUnavailable(
            f"watch state snapshot is unavailable for {state.watch_id}"
        )
    return restored


def _copy_watch_state(target: WatchState, source: WatchState) -> None:
    """Restore the canonical snapshot into the registered object without replacing its lock."""
    lock = target.lock
    target.__dict__.clear()
    target.__dict__.update(source.__dict__)
    target.lock = lock


def _transaction_is_committed(state: WatchState, transaction: dict[str, Any]) -> bool:
    try:
        restored = _state_snapshot(state)
    except _HarvestTransactionUnavailable:
        return False
    return str(transaction.get("status") or "") == "ready" and restored.harvest_commit_id == str(
        transaction["transaction_id"]
    )


def _backup_fragment_before_harvest(state: WatchState) -> dict[str, Any]:
    from .sources import file_fragment_path

    fragment = file_fragment_path(state)
    backup = _harvest_fragment_backup_path(state)
    try:
        backup.unlink()
    except FileNotFoundError:
        pass
    if not fragment.exists():
        return {"exists": False, "bytes": 0, "sha256": ""}
    backup.parent.mkdir(parents=True, exist_ok=True)
    tmp = _unique_tmp(backup)
    try:
        with fragment.open("rb") as source, tmp.open("wb") as destination:
            shutil.copyfileobj(source, destination)
            destination.flush()
            os.fsync(destination.fileno())
        tmp.replace(backup)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
    payload = backup.read_bytes()
    return {
        "exists": True,
        "bytes": len(payload),
        "sha256": sha256(payload).hexdigest(),
    }


def _begin_harvest_transaction(state: WatchState) -> dict[str, Any]:
    marker = _harvest_transaction_path(state)
    with locked_json_path(marker):
        _recover_pending_harvest_unlocked(state)
        fragment = _backup_fragment_before_harvest(state)
        transaction = {
            "schema_version": _HARVEST_TRANSACTION_SCHEMA,
            "transaction_id": uuid4().hex,
            "watch_id": state.watch_id,
            "owner_id": state.owner_id,
            "audit_id": state.audit_root_task_id,
            "status": "preparing",
            "started_at": time.time(),
            "start": {
                "source_checkpoint": deepcopy(state.source_checkpoint),
                "cursor": state.cursor,
                "line_cursor": state.line_cursor,
                "spool_seq": state.spool_seq,
                "spool_generation": state.spool_generation,
                "spool_bytes": _path_size(spool_path(state)),
                "audit_bytes": _path_size(_audit_ledger_path(state)),
                "harvest_commit_id": state.harvest_commit_id,
                "fragment": fragment,
            },
        }
        _write_harvest_transaction(marker, transaction)
    return transaction


def _commit_harvest_transaction(
    state: WatchState,
    transaction: dict[str, Any],
) -> None:
    marker = _harvest_transaction_path(state)
    with locked_json_path(marker):
        _assert_active_harvest_transaction(state, transaction)
        transaction["status"] = "ready"
        transaction["target"] = {
            "source_checkpoint": deepcopy(state.source_checkpoint),
            "cursor": state.cursor,
            "line_cursor": state.line_cursor,
            "spool_seq": state.spool_seq,
            "spool_generation": state.spool_generation,
        }
        state.harvest_commit_id = str(transaction["transaction_id"])
        _write_harvest_transaction(marker, transaction)
        persist_state(state)
        _commit_transaction_source_record_keys(state, transaction)
        _clear_harvest_transaction_unlocked(
            state,
            expected_transaction_id=str(transaction["transaction_id"]),
        )


def _truncate_to_committed_offset(path: Path, offset: int) -> None:
    if offset < 0:
        raise _HarvestTransactionUnavailable(f"negative transaction offset for {path.name}")
    if not path.exists():
        if offset == 0:
            return
        raise _HarvestTransactionUnavailable(
            f"transaction rollback target disappeared: {path.name}"
        )
    if path.stat().st_size < offset:
        raise _HarvestTransactionUnavailable(
            f"transaction rollback target is shorter than committed offset: {path.name}"
        )
    with path.open("r+b") as handle:
        handle.truncate(offset)
        handle.flush()
        os.fsync(handle.fileno())


def _restore_fragment_after_failed_harvest(
    state: WatchState,
    fragment_facts: dict[str, Any],
) -> None:
    from .sources import file_fragment_path

    fragment = file_fragment_path(state)
    backup = _harvest_fragment_backup_path(state)
    if not bool(fragment_facts.get("exists")):
        try:
            fragment.unlink()
        except FileNotFoundError:
            pass
        return
    try:
        payload = backup.read_bytes()
    except OSError as exc:
        raise _HarvestTransactionUnavailable("harvest fragment backup is unavailable") from exc
    expected_bytes = int(fragment_facts.get("bytes") or 0)
    expected_hash = str(fragment_facts.get("sha256") or "")
    if len(payload) != expected_bytes or sha256(payload).hexdigest() != expected_hash:
        raise _HarvestTransactionUnavailable("harvest fragment backup failed validation")
    tmp = _unique_tmp(fragment)
    try:
        with tmp.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(fragment)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _clear_harvest_transaction_unlocked(
    state: WatchState,
    *,
    expected_transaction_id: str,
) -> bool:
    marker = _harvest_transaction_path(state)
    backup = _harvest_fragment_backup_path(state)
    current = _read_harvest_transaction(state)
    if current is None:
        return False
    if str(current.get("transaction_id") or "") != expected_transaction_id:
        return False
    try:
        marker.unlink()
    except FileNotFoundError:
        pass
    _fsync_directory(marker.parent)
    try:
        backup.unlink()
    except FileNotFoundError:
        pass
    return True


def _recover_pending_harvest(
    state: WatchState,
    *,
    expected_transaction_id: str = "",
) -> bool:
    """Finish a committed harvest or roll an uncommitted append back to its exact offsets."""
    marker = _harvest_transaction_path(state)
    with locked_json_path(marker):
        return _recover_pending_harvest_unlocked(
            state,
            expected_transaction_id=expected_transaction_id,
        )


def _recover_pending_harvest_unlocked(
    state: WatchState,
    *,
    expected_transaction_id: str = "",
) -> bool:
    transaction = _read_harvest_transaction(state)
    if transaction is None:
        try:
            _harvest_fragment_backup_path(state).unlink()
        except FileNotFoundError:
            pass
        return False
    if (
        expected_transaction_id
        and str(transaction.get("transaction_id") or "") != expected_transaction_id
    ):
        return False
    if _transaction_is_committed(state, transaction):
        _copy_watch_state(state, _state_snapshot(state))
        _commit_transaction_source_record_keys(state, transaction)
        _clear_harvest_transaction_unlocked(
            state,
            expected_transaction_id=str(transaction["transaction_id"]),
        )
        return True
    start = dict(transaction["start"])
    restored = _state_snapshot(state)
    if (
        restored.harvest_commit_id != str(start.get("harvest_commit_id") or "")
        or restored.source_checkpoint != dict(start.get("source_checkpoint") or {})
        or restored.cursor != int(start.get("cursor") or 0)
        or restored.line_cursor != int(start.get("line_cursor") or 0)
        or restored.spool_seq != int(start.get("spool_seq") or 0)
        or restored.spool_generation != int(start.get("spool_generation") or 0)
    ):
        raise _HarvestTransactionUnavailable(
            f"watch snapshot moved outside harvest transaction for {state.watch_id}"
        )
    _truncate_to_committed_offset(
        spool_path(state),
        int(start.get("spool_bytes") or 0),
    )
    _truncate_to_committed_offset(
        _audit_ledger_path(state),
        int(start.get("audit_bytes") or 0),
    )
    fragment_facts = start.get("fragment")
    if not isinstance(fragment_facts, dict):
        raise _HarvestTransactionUnavailable("harvest fragment facts are missing")
    _restore_fragment_after_failed_harvest(state, fragment_facts)
    _copy_watch_state(state, restored)
    _clear_harvest_transaction_unlocked(
        state,
        expected_transaction_id=str(transaction["transaction_id"]),
    )
    return True


def _stage_harvest_source_record_keys(
    state: WatchState,
    transaction: dict[str, Any],
    keys: list[str],
) -> None:
    """Persist accepted keys before any matching spool append can occur."""

    transaction["source_record_keys"] = list(keys)
    marker = _harvest_transaction_path(state)
    with locked_json_path(marker):
        _assert_active_harvest_transaction(state, transaction)
        _write_harvest_transaction(marker, transaction)


def _commit_transaction_source_record_keys(
    state: WatchState,
    transaction: dict[str, Any],
) -> None:
    raw = transaction.get("source_record_keys")
    if not isinstance(raw, list) or not raw:
        return
    from .source_record_index import commit_source_record_keys

    commit_source_record_keys(
        state,
        [str(item) for item in raw],
        transaction_id=str(transaction.get("transaction_id") or ""),
    )


def _assert_active_harvest_transaction(
    state: WatchState,
    transaction: dict[str, Any],
) -> None:
    current = _read_harvest_transaction(state)
    if current is None or str(current.get("transaction_id") or "") != str(
        transaction.get("transaction_id") or ""
    ):
        raise _HarvestTransactionUnavailable(
            f"harvest transaction lease was superseded for {state.watch_id}"
        )


def _visible_spool_seq(state: WatchState) -> int | None:
    """Return the last committed sequence while an uncommitted append is present."""
    transaction = _read_harvest_transaction(state)
    if transaction is None or _transaction_is_committed(state, transaction):
        return None
    return int(dict(transaction["start"]).get("spool_seq") or 0)


def consumed_and_acked_on_disk(owner_home: Path, watch_id: str) -> tuple[int, int]:
    """一路 watch 的已交付 / 已确认判完候选数(纯盘上结构信号,单消费者)。
    背压/过载/唤醒兜底的未判积压都按这把尺算。"""
    base = state_dir(owner_home) / f"{watch_id}.read.json"
    report = read_json_object_report(base, context="watch_spool.base_cursor")
    if report.load_error is not None:
        return 0, 0
    return int(report.payload.get("candidates_consumed") or 0), acked_candidates(report.payload)


def ensure_harvester(
    state: WatchState,
    fetch_json: Callable,
    *,
    on_records_ready: Callable[[WatchState], bool] | None = None,
    on_window_finalized: Callable[[WatchState], bool] | None = None,
) -> dict[str, Any] | None:
    """确保这路 watch 有收割者:本进程有活线程→复用;别处租约新鲜→remote;否则起线程。

    返回 {"mode": "local"|"remote"}；租约不可证明可独占时也按 remote fail-closed，
    不回落成第二个 inline 读者。只有已成功占租但线程资源本身启动失败时返回 None。
    """
    if harvesters.get_live(state.watch_id) is not None:
        return {"mode": "local"}
    claimed, lease = _claim_harvester_lease(state)
    if not claimed:
        return {"mode": "remote", "lease": lease}
    lease_id = str(lease["lease_id"])
    stop_event = threading.Event()
    thread = threading.Thread(
        target=_harvest_loop,
        name=f"watch-harvester-{state.watch_id}",
        args=(
            state,
            fetch_json,
            stop_event,
            lease_id,
            on_records_ready,
            on_window_finalized,
        ),
        daemon=True,
    )
    try:
        thread.start()
    except RuntimeError:
        _clear_lease(state, lease_id)
        _LOGGER.warning("harvester thread start failed (watch=%s)", state.watch_id, exc_info=True)
        return None
    harvesters.put(HarvesterHandle(state.watch_id, lease_id, stop_event, thread))
    return {"mode": "local"}


def stop_harvester(watch_id: str) -> None:
    harvesters.stop(watch_id)


def _harvest_loop(
    state: WatchState,
    fetch_json: Callable,
    stop_event: threading.Event,
    lease_id: str,
    on_records_ready: Callable[[WatchState], bool] | None = None,
    on_window_finalized: Callable[[WatchState], bool] | None = None,
) -> None:
    backoff = 0.0
    notified_through = 0
    finalization_notified = False
    try:
        while not stop_event.is_set():
            outcome = _locked_harvest_step(state, fetch_json, lease_id=lease_id)
            notified_through = _records_ready_notification_watermark(
                state,
                on_records_ready,
                notified_through=notified_through,
            )
            if outcome == "stop":
                finalization_notified = _window_finalized_notification_state(
                    state,
                    on_window_finalized,
                    already_notified=finalization_notified,
                )
                break
            backoff = _next_harvest_backoff(outcome, backoff)
            stop_event.wait(_next_harvest_wait(state, backoff))
    finally:
        _clear_lease(state, lease_id)


def _records_ready_notification_watermark(
    state: WatchState,
    callback: Callable[[WatchState], bool] | None,
    *,
    notified_through: int,
) -> int:
    """Wake once per durable tranche, even when an empty instant is not sampled.

    ``written`` and ``acked`` are cumulative across spool generations.  Using
    their watermark avoids a race in the former boolean latch: a consumer can
    acknowledge the last notified row and a collector can append a new row
    before this loop observes an empty queue.  The new tranche must still wake
    the source worker immediately instead of waiting for periodic recovery.
    """
    written = int(state.totals.get("spool_candidates", 0) or 0)
    _consumed, acked = consumed_and_acked_on_disk(state.owner_home, state.watch_id)
    watermark = max(0, int(notified_through or 0))
    if written <= acked:
        return acked
    if callback is None or acked < watermark:
        return watermark
    try:
        return written if callback(state) else acked
    except Exception:
        _LOGGER.warning(
            "harvest ready notification failed (watch=%s)",
            state.watch_id,
            exc_info=True,
        )
        return acked


def _window_finalized_notification_state(
    state: WatchState,
    callback: Callable[[WatchState], bool] | None,
    *,
    already_notified: bool,
) -> bool:
    """Notify after the durable final-boundary transaction has committed."""
    if float(state.window_finalized_at or 0.0) <= 0:
        return already_notified
    if callback is None or already_notified:
        return already_notified
    try:
        callback(state)
        return True
    except Exception:
        _LOGGER.warning(
            "harvest window-finalized notification failed (watch=%s)",
            state.watch_id,
            exc_info=True,
        )
        return False


def _next_harvest_backoff(outcome: str, current: float) -> float:
    if outcome != "error":
        return 0.0
    return min(_MAX_ERROR_BACKOFF_SECONDS, current + 1.0)


def _next_harvest_wait(state: WatchState, backoff: float) -> float:
    """Sleep no later than the collection deadline so the final read is timely."""
    regular = max(0.0, float(state.tuning.poll_interval_seconds) + float(backoff))
    window = int(state.watch_window_seconds or 0)
    if window <= 0 or float(state.window_finalized_at or 0.0) > 0:
        return regular
    until_deadline = float(state.opened_at or 0.0) + window - time.time()
    return min(regular, max(0.0, until_deadline))


def _locked_harvest_step(
    state: WatchState,
    fetch_json: Callable,
    *,
    lease_id: str,
) -> str:
    """跑一拍并续租约;返回 stop/ok/error。任何异常都吞成 error,线程绝不裸死。"""
    try:
        # Claim is durable before the thread starts.  Renew before touching the
        # source so a superseded or unreadable lease fails closed without a
        # second writer entering the network/transaction path.
        if not _write_lease(state, lease_id):
            return "stop"
        outcome = _harvest_once(state, fetch_json)
        if outcome != "stop" and not _write_lease(state, lease_id):
            return "stop"
        return outcome
    except Exception:
        _LOGGER.warning("harvest cycle failed (watch=%s)", state.watch_id, exc_info=True)
        return "error"


def _harvest_once(state: WatchState, fetch_json: Callable) -> str:
    """持 state.lock 跑一拍:先判停,再背压闸,再收割。"""
    with state.lock:
        _recover_pending_harvest(state)
        if _should_stop(state):
            return "stop"
        finalizing_window = _window_deadline_reached(state)
        if _backpressured(state):
            # 抬取快于判读:本拍不 drain(游标不动),只回 ok 让循环续租约心跳、等消费者
            # 推进读游标积压回落。绝不静默丢——被钳住的事件仍在源端,消费追上即续抬。
            # 保证档只会因磁盘水位走到这里,单独记账(回执要能区分"判读慢"与"盘满")。
            key = "disk_backpressure_skips" if state.audit_guarantee else "backpressure_skips"
            state.totals[key] = int(state.totals.get(key, 0) or 0) + 1
            return "ok"
        healthy = _harvest_cycle(
            state,
            fetch_json,
            finalize_window=finalizing_window,
        )
        if not healthy:
            return "error"
        return "stop" if finalizing_window else "ok"


def _effective_ceiling(state: WatchState) -> int:
    """抬取背压上限；一份权威交付账对应一份前瞻缓冲。0=关闭背压。"""
    return backpressure_ceiling(state.tuning)


def _backpressured(state: WatchState) -> bool:
    """未读积压是否已到背压上限(纯计数)。只对内容型全量直通(易洪泛)生效——结构化源
    候选稀疏不洪泛,不背压;上限 0=关闭背压,行为回到旧的无界堆积。

    /audit 保证档不走这条:按判读积压停抬会让滚动缓冲源在源端淘汰(=真丢),违反
    "一条不漏"。保证档判读慢只体现为 spool(磁盘队列)涨,唯一停抬边界是磁盘水位。"""
    if state.audit_guarantee:
        return _disk_backpressured(state)
    if not is_content_mode(state):
        return False
    ceiling = _effective_ceiling(state)
    return ceiling > 0 and spool_unread(state) >= ceiling


def _disk_backpressured(state: WatchState) -> bool:
    """保证档的磁盘水位闸(丢弃恒 0 的最后边界):spool 文件超上限或盘上剩余空间不足
    → 本拍停抬,积压留在【源端】而不是丢已入队的;恢复(判完归档/腾出空间)即续抬。
    纯字节计数;0=不设限。触发即记 disk_backpressure_skips(回执/健康块可见)。"""
    max_bytes = int(getattr(state.tuning, "guarantee_spool_max_mb", 0) or 0) * 1024 * 1024
    if max_bytes > 0:
        try:
            if spool_path(state).stat().st_size >= max_bytes:
                return True
        except OSError:
            pass
    min_free = int(getattr(state.tuning, "guarantee_min_disk_free_mb", 0) or 0) * 1024 * 1024
    if min_free > 0:
        import shutil

        try:
            if shutil.disk_usage(state_dir(state.owner_home)).free <= min_free:
                return True
        except OSError:
            pass
    return False


def _should_stop(state: WatchState) -> bool:
    if state.closed:
        return True
    if _window_deadline_reached(state):
        # LLM: A window is complete only after its final boundary transaction
        # commits. A crashed or failed final read remains recoverable and retryable.
        # 函数用途: 到点不是立刻停；只有最后补拉已和游标、spool 同事务提交才真正停采。
        return float(state.window_finalized_at or 0.0) > 0
    # 无窗长守:长时间没人 pull 消费(盯守方消失)即自停,防孤儿线程白烧;
    # 下一次 pull 会立即重新拉起并从游标续。消费信号认本进程 last_pull_at 与
    # 跨进程读游标 sidecar 的较新者(消费者可能在别的进程)。
    idle_cap = float(state.tuning.harvester_idle_stop_seconds)
    if idle_cap <= 0:
        return False
    last_consume = max(
        float(state.last_pull_at or 0.0),
        float(state.opened_at or 0.0),
        _latest_cursor_update(state),
    )
    return (time.time() - last_consume) > idle_cap


def _window_deadline_reached(state: WatchState) -> bool:
    window = int(state.watch_window_seconds or 0)
    if window <= 0:
        return False
    return time.time() >= float(state.opened_at or 0.0) + window


def _latest_cursor_update(state: WatchState) -> float:
    """读游标最近一次推进时刻(单消费者:一路一个基座游标)——消费活跃度信号,供 idle 自停判定。"""
    return float(read_spool_cursor(state).get("updated_at") or 0.0)


def _harvest_cycle(
    state: WatchState,
    fetch_json: Callable,
    *,
    finalize_window: bool = False,
) -> bool:
    """Run one fetch-to-cursor transaction and recover its own failed attempt."""
    transaction = _begin_harvest_transaction(state)
    try:
        return _harvest_cycle_transaction(
            state,
            fetch_json,
            transaction,
            finalize_window=finalize_window,
        )
    except Exception:
        # LLM: An exception can recover only the transaction id created by this
        # call. A late, superseded worker must never roll back its replacement.
        # 函数用途: 活进程失败即时恢复；真正崩溃则由接替者读取同一标记恢复。
        try:
            _recover_pending_harvest(
                state,
                expected_transaction_id=str(transaction["transaction_id"]),
            )
        except Exception:
            _LOGGER.error(
                "harvest transaction recovery failed (watch=%s transaction=%s)",
                state.watch_id,
                transaction.get("transaction_id"),
                exc_info=True,
            )
        raise


def _harvest_cycle_transaction(
    state: WatchState,
    fetch_json: Callable,
    transaction: dict[str, Any],
    *,
    finalize_window: bool = False,
) -> bool:
    """一拍:drain→分片喂引擎→候选落 spool→账目/快照。返回源是否健康(False=本拍拉流失败)。

    分片喂:冷启动/断点追赶一次 drain 可达上万条,整批一次 process 会让稀有候选挤爆
    "每批候选上限"落 overflow(模型看不见);按 harvest_chunk_events 切片,候选位随
    积压量线性扩。稳态每拍只有几百条=单片,行为不变。
    """
    from .sources import (
        apply_cursor_page_feedback,
        drain_watch_source,
        persist_file_fragment,
    )
    from .watch_feedback import consume_feedback_inbox

    # 反馈收件箱先消费(B3):模型上一批确认的真目标特征即刻入库,本拍就能抬同类。
    consume_feedback_inbox(state, time.time())
    content_mode = is_content_mode(state)
    budget = DrainBudget(
        max_events=_cycle_drain_events(state, content_mode),
        page_limit=state.tuning.page_limit,
        deadline=time.time() + _HTTP_TIMEOUT_SECONDS,
    )
    drain = drain_watch_source(
        state,
        fetch_json,
        budget,
        force_poll=finalize_window,
    )
    _assert_active_harvest_transaction(state, transaction)
    apply_cursor_page_feedback(state, drain)
    if not drain.error:
        _deduplicate_source_records(state, drain, transaction)
    if not drain.error and not persist_file_fragment(state, drain):
        drain.error = "未完成记录片段无法持久化，未推进游标"
        drain.error_code = "SOURCE_FRAGMENT_PERSIST_FAILED"
    state.totals["pulls"] += 1
    if drain.error:
        state.totals["http_errors"] += 1
        state.last_error = drain.error
        state.last_error_code = drain.error_code or "NETWORK_REQUEST_FAILED"
        _commit_harvest_transaction(state, transaction)
        return False
    state.last_error = ""
    state.last_error_code = ""
    # content_mode(冷启动/passthrough 全量直通)记录尺寸钳到一批可精读量(反 rubber-stamp);
    # 结构化源沿用 harvest_chunk_events(稀有候选挤出保护)。
    chunk_size = (
        content_batch_size(state.tuning)
        if content_mode
        else int(state.tuning.harvest_chunk_events or 0)
    )
    chunks = _event_chunks(drain.events, chunk_size)
    headroom = judge_headroom(state)
    # 冷启动:本源还没 configure 出 spec 时,判据没学出来,存量不能靠结构规则筛
    # (根因2)——整批 full_read 无条件生效(宁滥勿漏)。configure 后转 spec 驱动
    # (passthrough spec 自带无条件直通,普通 spec 走降维分诊)。
    cold_start = state.source_spec is None
    for index, chunk in enumerate(chunks):
        chunk_view = _chunk_drain_view(drain, chunk, first=(index == 0))
        digest = state.engine.process(
            chunk,
            time.time(),
            judge_headroom=headroom,
            cold_start=cold_start,
            guarantee=state.audit_guarantee,
        )
        # 本片实抬的候选(真车道+抽检)即时扣减余量:同拍后续片共享同一份判读余量。
        headroom = max(0, headroom - len(digest.candidates))
        if digest.candidates:
            _assert_active_harvest_transaction(state, transaction)
            _spool_append(state, chunk_view, digest)
        _assert_active_harvest_transaction(state, transaction)
        if not audit_append(state, build_audit_record(chunk_view, digest)):
            raise OSError("audit ledger append failed")
    # cursor 是本批已完整提交的结构化水位，不是“网络已经读到哪里”的预告。
    # 必须在 engine/spool/audit 全部成功后发布；否则并发读者会提前判断追平，
    # 后续步骤一旦抛错，下一拍还会从新 cursor 续读并永久跳过未提交事件。
    state.cursor = drain.cursor
    if isinstance(getattr(drain, "source_checkpoint", None), dict):
        state.source_checkpoint = deepcopy(drain.source_checkpoint)
    state.line_cursor = int(getattr(drain, "aux_cursor", 0) or 0)
    if float(getattr(drain, "fetched_at", 0.0) or 0.0) > 0:
        state.last_poll_at = float(drain.fetched_at)
    state.last_reached_end = drain.reached_end
    state.totals["gap_events"] += drain.gap_events
    if finalize_window:
        # LLM: Persist this marker inside the cursor/spool commit transaction.
        # A takeover therefore sees either the whole boundary read or no marker.
        # 函数用途: 最终补拉与游标、原始队列一起原子落盘，消除崩溃后的重复或漏拉判断。
        state.window_finalized_at = time.time()
    _update_ingest_throughput(state, now=time.time())
    _commit_harvest_transaction(state, transaction)
    # Rotation is a post-commit storage optimization.  It never participates in
    # deciding whether the source cursor is committed.
    generation = state.spool_generation
    _maybe_rotate_spool(state, spool_path(state))
    if state.spool_generation != generation:
        persist_state(state)
    return True


def _deduplicate_source_records(
    state: WatchState,
    drain: DrainResult,
    transaction: dict[str, Any],
) -> None:
    """Filter adapter overlaps by their durable record keys before the engine."""

    if not drain.source_record_keys:
        return
    positions = [int(position) for position, _event in drain.events]
    if any(position not in drain.source_record_keys for position in positions):
        drain.error = "来源适配器记录与唯一键未能一一对应"
        drain.error_code = "SOURCE_RECORD_KEY_MISMATCH"
        return
    keys = [str(drain.source_record_keys[position]) for position in positions]
    from .source_record_index import (
        SourceRecordIndexError,
        unseen_source_record_keys,
    )

    try:
        unseen = unseen_source_record_keys(state, keys)
    except SourceRecordIndexError as exc:
        drain.error = str(exc)[:500]
        drain.error_code = "SOURCE_RECORD_INDEX_UNAVAILABLE"
        return
    accepted_events: list[tuple[int, dict]] = []
    accepted_keys: dict[int, str] = {}
    for keep, (_old_position, event), key in zip(unseen, drain.events, keys, strict=True):
        if not keep:
            continue
        position = int(state.cursor) + len(accepted_events)
        accepted_events.append((position, event))
        accepted_keys[position] = key
    duplicates = len(drain.events) - len(accepted_events)
    if duplicates:
        state.totals["source_duplicates"] = int(
            state.totals.get("source_duplicates", 0) or 0
        ) + duplicates
    drain.events = accepted_events
    drain.source_record_keys = accepted_keys
    drain.cursor = int(state.cursor) + len(accepted_events)
    _stage_harvest_source_record_keys(
        state,
        transaction,
        list(accepted_keys.values()),
    )


def judge_headroom(state: WatchState) -> int:
    """判读吞吐反压余量(真机实锤:audit 抽检 4000+/用户淹没主代理判力,逐条报出
    156→18):余量 = 每 pull 判读口粮 - spool 未读积压。
    消费者提交结构化结果后推进读游标 → 积压回落 → 余量自动回升;
    判得慢积压高 → 余量归零 → 抽检自动停抬。纯结构计数,自适应任意模型判读速度,
    不需要估算速率、没有新参数。真信号车道不受此限(见 engine.process)。
    口粮尺与消费口粮同源(judge_quota):直通开着时口粮=直通批量级,否则一批直通
    落 spool 就把余量吃穿、直通永久自锁在关闭态。
    积压按权威读游标已消费数计算。"""
    written = int(state.totals.get("spool_candidates", 0))
    consumed, _acked = consumed_and_acked_on_disk(state.owner_home, state.watch_id)
    backlog = max(0, written - consumed)
    return max(0, judge_quota(state.tuning) - backlog)


def judge_quota(tuning) -> int:
    """每轮 pull 的判读口粮(消费侧取数上限与反压余量共用同一把尺):
    max(每批候选上限, 正常量直通上限)。直通关闭(=0)时与旧口径一致。"""
    return max(int(tuning.max_candidates_per_pull), int(tuning.full_read_per_pull or 0))


def spool_unread(state: WatchState) -> int:
    """spool 里已写入而消费者【尚未取走】的候选数(纯计数:引擎累计写入 − 读游标已交付)。
    背压看这把尺:未读堆着=消费者没跟上,抬取该等一等。"""
    written = int(state.totals.get("spool_candidates", 0) or 0)
    if written <= 0:
        return 0
    consumed, _acked = consumed_and_acked_on_disk(state.owner_home, state.watch_id)
    return max(0, written - consumed)


# LLM: This byte estimate uses the canonical spool and cursor offset only; it is a cheap
# batching trigger, not a semantic/token accounting authority.
# 函数用途: 粗算还没交给研判器的数据体积，让少量但很长的日志不用等到条数满或超时。
def spool_unread_bytes(state: WatchState) -> int:
    cursor = read_spool_cursor(state)
    offset = (
        max(0, int(cursor.get("offset") or 0))
        if int(cursor.get("generation") or 0) == state.spool_generation
        else 0
    )
    try:
        return max(0, spool_path(state).stat().st_size - offset)
    except OSError:
        return 0


def backpressure_ceiling(tuning) -> int:
    """抬取背压上限:未读积压达到 factor×judge_quota 即本拍停抬(0=关闭背压)。
    下限至少一个 quota——ceiling 比一批口粮还小会把正常一批直通也误判成过载。"""
    factor = int(getattr(tuning, "spool_backpressure_factor", 0) or 0)
    if factor <= 0:
        return 0
    return max(judge_quota(tuning), factor * judge_quota(tuning))


def overload_threshold(tuning) -> int:
    """过载线:未读积压 ≥ 一个 judge_quota(明显落后一整批)即视为判读跟不上抬取——
    触发如实标注(overload 块)。纯计数,不决定候选真假。"""
    return max(1, judge_quota(tuning))


def content_batch_size(tuning) -> int:
    """content_mode(passthrough/冷启动全量直通)下每条 spool 记录 = 模型每 pull 批量的
    候选上限:一批正常量直通(full_read_per_pull),回退每批候选上限。真机实锤:passthrough
    洪泛时一次 drain 500 条正常流全落进【一条】spool 记录,而记录整条读取——模型每 pull 被
    怼 500 条正常流当命中整车 rubber-stamp(60 条同微秒批量乱报)。把 content_mode 的记录
    钳到"一批可精读"的量,过载时模型至多面对一批而非一片。结构化源(已配非 passthrough
    判据)不走此路:记录本就稀疏,沿用 harvest_chunk_events 的稀有挤出保护。"""
    return max(1, int(tuning.full_read_per_pull or tuning.max_candidates_per_pull))


def is_content_mode(state: WatchState) -> bool:
    """本源当前是否内容型全量直通(冷启动未学 spec / spec.passthrough)且直通确实开着:
    此时候选≈事件,抬取无稀有筛、易洪泛,记录尺寸与抬取速率都要按背压钳住。
    直通关闭(full_read_per_pull=0)时即便冷启动也走结构化降维分诊(候选稀疏不洪泛),
    不套用背压(否则 judge_quota 掉到 max_candidates 级、ceiling 过小误钳正常盯守)。
    已配非 passthrough 判据的结构化源同样不算。"""
    if int(state.tuning.full_read_per_pull or 0) <= 0:
        return False
    spec = state.engine.spec
    return spec is None or bool(getattr(spec, "passthrough", False))


def _cycle_drain_events(state: WatchState, content_mode: bool) -> int:
    """本拍抓取事件上限:结构化源/背压关时照旧(大预算追赶);content_mode + 背压开时
    钳到"距未读上限还差多少(room)",一拍不把整条存量从起点倒进 spool(真机一次
    5000 条埋掉真事)。至少抓一批口粮,room 再小也有进度。
    保证档不按判读积压钳抓取(全速收进 durable 队列,源端滚动缓冲淘汰才是真丢);
    盘满边界由 _disk_backpressured 在拍首整拍拦。"""
    base = int(state.tuning.max_events_per_pull)
    ceiling = _effective_ceiling(state)
    if state.audit_guarantee or ceiling <= 0 or not content_mode:
        return base
    room = max(0, ceiling - spool_unread(state))
    return max(content_batch_size(state.tuning), min(base, room))


def _event_chunks(events: list, chunk_size: int) -> list[list]:
    if not events:
        return []
    if chunk_size <= 0 or len(events) <= chunk_size:
        return [events]
    return [events[i : i + chunk_size] for i in range(0, len(events), chunk_size)]


def _chunk_drain_view(drain: Any, chunk: list, *, first: bool) -> DrainResult:
    """片级账目视图:seen/cursor_to 记本片,gap 只记在首片(缺口发生在片切分之前)。"""
    view = DrainResult(events=chunk)
    positions = {int(position) for position, _event in chunk}
    view.source_record_keys = {
        int(position): str(key)
        for position, key in dict(getattr(drain, "source_record_keys", {}) or {}).items()
        if int(position) in positions
    }
    view.cursor = (chunk[-1][0] + 1) if chunk else drain.cursor
    view.pages = drain.pages if first else 0
    view.reached_end = drain.reached_end and view.cursor >= drain.cursor
    view.gap_events = drain.gap_events if first else 0
    return view


def candidate_ack_id(spool_seq: int, index: int) -> str:
    """候选的签收令牌(ack-on-judge 的对账键):spool 记录序号:记录内下标。全由盘上事实
    构成——spool_seq 跨轮转单调、下标落盘即定,重投/重启/换人重建出的令牌逐字节相同。"""
    return f"{spool_seq}:{index}"


# LLM: Audit source refs are logical owner-scoped identifiers, never physical paths.
# They remain stable when spool rows move into the archive during rotation.
# 函数用途: 给每条待研判日志生成稳定引用，轮转归档后仍可用同一个引用反查原文。
def audit_source_ref(state: WatchState, ack_id: str) -> str:
    return f"audit://{state.watch_id}/candidate/{ack_id}"


def parse_audit_source_ref(value: object) -> tuple[str, str] | None:
    """Parse one logical Audit record reference through the canonical grammar."""
    match = _AUDIT_SOURCE_REF_RE.fullmatch(str(value or "").strip())
    if match is None:
        return None
    return match.group("watch_id"), match.group("ack_id")


def ensure_ack_ids(record: dict[str, Any]) -> list[dict[str, Any]]:
    """取记录的候选行并保证每行带 ack_id(升级前落盘的旧记录现场按同一规则补齐)。"""
    seq = int(record.get("spool_seq") or 0)
    rows = list(record.get("candidates") or [])
    for index, row in enumerate(rows):
        if isinstance(row, dict) and not row.get("ack_id"):
            row["ack_id"] = candidate_ack_id(seq, index)
    return rows


def _spool_append(state: WatchState, drain: Any, digest: Any) -> None:
    # 先写盘、成功才冒泡计数:计数即"记录已可读"(消费方以计数判积压,写失败不留幽灵积压)。
    rendered = candidate_rows(digest)
    seeds: list[dict[str, Any]] = []
    for index, row in enumerate(rendered):
        stored = dict(row)
        if state.audit_guarantee:
            raw_event = dict(digest.candidates[index].event)
            raw_json = json.dumps(
                raw_event,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            raw_bytes = raw_json.encode("utf-8")
            stored.update(
                {
                    "event": raw_event,
                    "event_sha256": sha256(raw_bytes).hexdigest(),
                    "event_bytes": len(raw_bytes),
                }
            )
            source_record_key = str(
                dict(getattr(drain, "source_record_keys", {}) or {}).get(
                    int(digest.candidates[index].seq_hint),
                    "",
                )
                or ""
            )
            if source_record_key:
                stored["source_record_key"] = source_record_key
        seeds.append(stored)
    # Audit 的调度单位是一条完整原始记录，不是一次网络页或一次 engine digest。
    # 一条 candidate 一行，消费侧才能严格按“时间/条数/累计字节任一触发”组批；
    # 每行仍保存整条 event，绝不为了批次大小从记录中间切开。普通 watch 保持
    # 既有聚合行，避免改变其宽筛/摘要吞吐语义。
    chunks = [[seed] for seed in seeds] if state.audit_guarantee else [seeds]
    timestamp = round(time.time(), 3)
    records: list[dict[str, Any]] = []
    for chunk_index, chunk in enumerate(chunks):
        next_seq = state.spool_seq + len(records) + 1
        candidates: list[dict[str, Any]] = []
        for index, seed in enumerate(chunk):
            ack_id = candidate_ack_id(next_seq, index)
            stored = {**seed, "ack_id": ack_id}
            if state.audit_guarantee:
                stored["source_ref"] = audit_source_ref(state, ack_id)
            candidates.append(stored)
        first = chunk_index == 0
        record = {
            "spool_seq": next_seq,
            "generation": state.spool_generation,
            "t": timestamp,
            "fetched_at": timestamp,
            "owner_id": state.owner_id,
            "audit_id": state.audit_root_task_id,
            "audit_run_epoch": max(0, int(state.audit_run_epoch or 0)),
            "source_id": state.source_id,
            "watch_id": state.watch_id,
            "candidates": candidates,
            "suppressed_groups": group_rows(digest) if first else [],
            "suppressed_groups_total": digest.groups_total if first else 0,
            "suppressed_events": digest.suppressed_total if first else 0,
            "overflow_count": len(digest.overflow) if first else 0,
            "cursor_to": drain.cursor,
        }
        # Digest-level facts belong to one physical row only; repeating them on
        # each storage chunk would double-count a single ingest cycle.
        if first and digest.frequent_hits:
            record["frequent_hits"] = frequent_hit_rows(digest)
        if first and digest.normal_rule_hits:
            record["normal_rule_hits"] = digest.normal_rule_hits
        records.append(record)
    path = spool_path(state)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.writelines(
            json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            for record in records
        )
        handle.flush()
        os.fsync(handle.fileno())
    state.spool_seq += len(records)
    state.totals["spool_candidates"] = state.totals.get("spool_candidates", 0) + len(
        digest.candidates
    )


def _maybe_rotate_spool(state: WatchState, path: Path) -> None:
    """积压已清(读游标追平倒数第二条,最后一条照旧留给读者)且无在途批、文件超限 →
    换代重写,读者按 generation 重置偏移。有在途不轮转:轮转会吃掉接管重投的依据
    (在途在消费者确认后清空,轮转窗口照常出现,长守不涨盘)。"""
    try:
        if path.stat().st_size < _SPOOL_ROTATE_BYTES:
            return
    except OSError:
        return
    cursor = read_spool_cursor(state)
    if isinstance(cursor.get("inflight"), dict):
        return
    if int(cursor.get("read_seq") or 0) < state.spool_seq - 1:
        return
    try:
        _rewrite_spool_keeping_last(state, path)
    except OSError:
        _LOGGER.warning("spool rotation failed (watch=%s)", state.watch_id, exc_info=True)


def _rewrite_spool_keeping_last(state: WatchState, path: Path) -> None:
    """换代重写:只保留最后一条完整记录(读者可能还没读它),世代号 +1。
    保证档(/audit)契约是【判完归档、永不删】:被切走的行(此刻全部已判完签收)
    先追加进 {watch_id}.archive.ndjson 留痕,再重写——活跃工作集有界,月级长跑
    不涨盘也不销毁任何已判记录;非保证档沿用旧行为(切走即弃,省盘)。"""
    lines: list[str] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.endswith("\n"):
                lines.append(line)
    last_line = lines[-1] if lines else ""
    if state.audit_guarantee and len(lines) > 1:
        archive = state_dir(state.owner_home) / f"{state.watch_id}.archive.ndjson"
        with archive.open("a", encoding="utf-8") as handle:
            handle.writelines(lines[:-1])
    state.spool_generation += 1
    tmp = path.with_suffix(".ndjson.tmp")
    tmp.write_text(last_line, encoding="utf-8")
    tmp.replace(path)


def read_spool_records(
    state: WatchState,
    *,
    max_candidates: int,
    consumer: str = "",
    max_bytes: int = 0,
    source_authority: dict[str, object] | None = None,
    effective_audit_objective: str = "",
) -> tuple[list[dict], dict[str, Any]]:
    """读取未消费的 spool 记录(至少 1 条、候选数够 max_candidates 即停),并推进读游标。

    整路 spool 使用一份权威读游标，不按调用者或 Agent 数量切成竞争分片。
    交付是 at-least-once:交付出去的一批先挂"在途"(inflight),**同一消费者下一次来取
    才算确认判完(ack)**——契约与 PULL_GUIDANCE 一致(逐批判完才继续 pull)。消费者
    消费者身份变化时，前任没 ack 的在途批原样重投，不推进交付游标——
    真机实锤:接管只续游标时,前任拉走还没判完就死的候选成了永久孤儿(已抬升的真事
    躺在 spool 里没人判、没上报)。consumer 传拉取方 run_id(solo 主代理恒为空串,
    同样构成稳定身份;身份变化才触发重投)。

    返回 (records, backlog_info);重投批的 backlog_info 带 redelivered_candidates>0。
    """
    with _cursor_mutation_fence(
        state,
        source_authority=source_authority,
        require_current_config=state.audit_guarantee,
    ) as gate:
        if not bool(gate.get("ok")):
            return [], {
                **_backlog_info(state, read_spool_cursor(state)),
                "authorization_error": gate,
            }
        return _read_spool_records_unlocked(
            state,
            max_candidates=max_candidates,
            consumer=consumer,
            max_bytes=max_bytes,
            effective_audit_objective=effective_audit_objective,
        )


def _read_spool_records_unlocked(
    state: WatchState,
    *,
    max_candidates: int,
    consumer: str,
    max_bytes: int,
    effective_audit_objective: str = "",
) -> tuple[list[dict], dict[str, Any]]:
    """Read/claim one batch while the caller holds the cursor mutation fence."""
    cursor = read_spool_cursor(state)
    resolution = _resolve_existing_inflight(
        state,
        cursor,
        consumer,
        max_candidates=max_candidates,
        max_bytes=max_bytes,
    )
    if resolution.response is not None:
        return resolution.response
    cursor = resolution.cursor
    _apply_effective_audit_objective(state, effective_audit_objective)
    records, offset, taken, start_offset = _scan_spool_or_empty(
        state,
        cursor,
        max_candidates=max_candidates,
        max_bytes=max_bytes,
    )
    if records:
        _write_claimed_spool_batch(
            state,
            cursor,
            records,
            consumer=consumer,
            offset=offset,
            taken=taken,
            start_offset=start_offset,
        )
    elif resolution.acked:
        # 没有新记录但发生了 ack(在途被确认/按缺口清掉):确认必须落盘,否则下次
        # 还会把已判完的批当在途重投。空轮询(无 ack 无新批)不写盘,别刷 IO。
        _write_spool_cursor(state, {**cursor, "updated_at": time.time()})
    return records, _backlog_info(state, read_spool_cursor(state))


@dataclass(frozen=True)
class _InflightResolution:
    cursor: dict[str, Any]
    acked: bool = False
    response: tuple[list[dict], dict[str, Any]] | None = None


def _resolve_existing_inflight(
    state: WatchState,
    cursor: dict[str, Any],
    consumer: str,
    *,
    max_candidates: int,
    max_bytes: int,
) -> _InflightResolution:
    inflight = cursor.get("inflight") if isinstance(cursor.get("inflight"), dict) else None
    if inflight is None:
        return _InflightResolution(cursor)
    if state.audit_guarantee and isinstance(inflight.get("pending_acks"), list):
        outcome = _redeliver_unacked_audit(
            state,
            (cursor, inflight),
            consumer,
            max_candidates=max_candidates,
            max_bytes=max_bytes,
        )
        if isinstance(outcome, tuple):
            return _InflightResolution(cursor, response=outcome)
        return _InflightResolution(outcome, acked=True)
    if str(inflight.get("consumer") or "") != str(consumer or ""):
        redelivered = _redeliver_inflight(state, cursor, inflight, consumer)
        if redelivered:
            info = _backlog_info(state, read_spool_cursor(state))
            info["redelivered_candidates"] = int(inflight.get("count") or 0)
            return _InflightResolution(cursor, response=(redelivered, info))
        return _InflightResolution(_acked_cursor(cursor, inflight, gap=True), acked=True)
    return _InflightResolution(_acked_cursor(cursor, inflight), acked=True)


def _apply_effective_audit_objective(state: WatchState, objective: str) -> None:
    selected = str(objective or "").strip()
    if state.audit_guarantee and selected and selected != state.audit_objective:
        state.audit_objective = selected
        persist_state(state)


def _scan_spool_or_empty(
    state: WatchState,
    cursor: dict[str, Any],
    *,
    max_candidates: int,
    max_bytes: int,
) -> tuple[list[dict], int, int, int]:
    try:
        return _scan_spool(
            state,
            cursor,
            max_candidates,
            max_bytes=max_bytes,
        )
    except OSError:
        return [], 0, 0, 0


def _write_claimed_spool_batch(
    state: WatchState,
    cursor: dict[str, Any],
    records: list[dict],
    *,
    consumer: str,
    offset: int,
    taken: int,
    start_offset: int,
) -> None:
    inflight = _new_inflight_claim(
        state,
        cursor,
        records,
        consumer=consumer,
        taken=taken,
        start_offset=start_offset,
    )
    if state.audit_guarantee:
        inflight = _audit_inflight_claim(state, inflight, records)
    payload = {
        **cursor,
        "inflight": inflight,
        "read_seq": int(records[-1].get("spool_seq") or 0),
        "offset": offset,
        "generation": state.spool_generation,
        "candidates_consumed": int(cursor.get("candidates_consumed") or 0) + taken,
        "candidates_acked": acked_candidates(cursor),
        "updated_at": time.time(),
    }
    _write_spool_cursor(state, payload)


def _new_inflight_claim(
    state: WatchState,
    cursor: dict[str, Any],
    records: list[dict],
    *,
    consumer: str,
    taken: int,
    start_offset: int,
) -> dict[str, Any]:
    current_generation = int(cursor.get("generation") or 0) == state.spool_generation
    fetched_at = [float(record.get("fetched_at") or record.get("t") or 0.0) for record in records]
    return {
        "from_seq": int(cursor.get("read_seq") or 0) if current_generation else 0,
        "to_seq": int(records[-1].get("spool_seq") or 0),
        "from_offset": start_offset,
        "count": taken,
        "consumer": str(consumer or ""),
        "generation": state.spool_generation,
        "delivered_at": time.time(),
        "delivery_attempt": 1,
        "oldest_fetched_at": min((value for value in fetched_at if value > 0), default=0.0),
    }


def _audit_inflight_claim(
    state: WatchState,
    inflight: dict[str, Any],
    records: list[dict],
) -> dict[str, Any]:
    ack_ids = [str(row.get("ack_id") or "") for record in records for row in ensure_ack_ids(record)]
    context_rows = [
        row for record in records for row in record.get("candidates") or [] if isinstance(row, dict)
    ]
    from ..memory_archive import estimate_tokens

    payload = {
        **inflight,
        "pending_acks": ack_ids,
        "delivered_ack_ids": list(ack_ids),
        "audit_root_task_id": state.audit_root_task_id,
        "estimated_input_tokens": max(1, estimate_tokens(context_rows)),
        "rendered_bytes": len(
            json.dumps(context_rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ),
    }
    return _bind_audit_delivery_ref(state, payload)


# LLM: Cursor mutation and source-attempt takeover share one lock order:
# source lease first, cursor second. A stale process therefore cannot pass an
# old entry check, wake later and overwrite the successor's in-flight batch.
# 函数用途: 为 Audit 的领取、签收和批次账更新提供同一提交瞬间的租约+游标互斥。
@contextmanager
def _cursor_mutation_fence(
    state: WatchState,
    *,
    source_authority: dict[str, object] | None,
    require_current_config: bool = False,
) -> Iterator[dict[str, object]]:
    stack = ExitStack()
    try:
        if state.audit_guarantee and source_authority is not None:
            from .source_worker import source_worker_lease_fence

            lease_result = stack.enter_context(
                source_worker_lease_fence(
                    state,
                    source_authority,
                    require_current_config=require_current_config,
                )
            )
            if not bool(lease_result.get("ok")):
                stack.close()
                yield lease_result
                return
        stack.enter_context(locked_json_path(_read_sidecar_path(state)))
    except OSError:
        stack.close()
        yield {
            "ok": False,
            "error_code": "AUDIT_CURSOR_LOCK_UNAVAILABLE",
            "error": "Audit 游标无法锁定，已按 fail-closed 拒绝写账。",
        }
        return
    try:
        yield {"ok": True, "error_code": ""}
    finally:
        stack.close()


def _redeliver_unacked_audit(
    state: WatchState,
    cursor_inflight: tuple[dict, dict],
    consumer: str,
    *,
    max_candidates: int,
    max_bytes: int,
) -> tuple[list[dict], dict] | dict:
    """/audit 保证档 ack-on-judge(上一轮崩的直接根因:判读工空转,再 pull 一次就把没判
    的在途批"确认"掉=静默吃):在途批还有候选没交逐条结论(submit_verdicts)时,【同人
    换人一律重投同批、绝不 ack、绝不发新批】——空 pull 推不动游标,领了活不判的工只会
    反复拿到同一批和欠账清单;结论交齐时在途已被 verdict 动作当场清掉,走不到这里。
    返回 (records, info) 即重投;返回 dict = 调整后的游标(在途不可恢复按缺口如实入账
    ——这笔账让覆盖回执 dropped>0 亮红,丢弃恒 0 是硬约束,亮红=有 bug,绝不粉饰;
    或防御摘除欠账已空的在途——不再推 acked 计数,逐条结论入账时已逐条 +1,批级再加双计)。"""
    cursor, inflight = cursor_inflight
    pending = [str(x) for x in inflight.get("pending_acks") or []]
    if not pending:
        return {k: v for k, v in cursor.items() if k != "inflight"}
    try:
        records = _scan_spool_range(state, inflight)
    except OSError:
        records = []
    if not records:
        return _acked_cursor(cursor, inflight, gap=True)
    redelivered, delivered_ids = _pending_record_projection(
        records,
        pending,
        max_candidates=max_candidates,
        max_bytes=max_bytes,
    )
    if not redelivered:
        return _acked_cursor(cursor, inflight, gap=True)
    now = time.time()
    updated_cursor = _finalize_delivery_throughput(
        cursor,
        inflight,
        now=now,
        recovery_max_tokens=max(
            1,
            int(state.tuning.guarantee_batch_max_tokens or 45_000),
        ),
    )
    updated_inflight = _bind_audit_delivery_ref(
        state,
        {
            **inflight,
            "consumer": str(consumer or ""),
            "delivered_at": now,
            "delivered_ack_ids": delivered_ids,
            "delivery_attempt": max(
                1,
                int(inflight.get("delivery_attempt") or 1) + 1,
            ),
        },
    )
    _write_spool_cursor(
        state,
        {
            **updated_cursor,
            "inflight": updated_inflight,
            "updated_at": now,
        },
    )
    info = _backlog_info(state, read_spool_cursor(state))
    info["redelivered_candidates"] = len(delivered_ids)
    info["pending_verdicts"] = len(delivered_ids)
    info["pending_verdicts_total"] = len(pending)
    info["pending_ack_ids"] = delivered_ids
    return redelivered, info


def _pending_record_projection(
    records: list[dict],
    pending_ack_ids: list[str],
    *,
    max_candidates: int,
    max_bytes: int,
) -> tuple[list[dict], list[str]]:
    """Project only unsettled complete rows from an at-least-once delivery.

    A partially judged batch may be much larger than one runner slice.  On
    recovery, replaying rows that already have durable verdicts can consume the
    entire context forever.  The canonical spool and inflight range stay
    unchanged; this function only narrows the next model-facing view at whole
    candidate boundaries.
    """
    pending = set(pending_ack_ids)
    limit = max(1, int(max_candidates or 1))
    selected: list[dict] = []
    selected_ids: list[str] = []
    rendered_bytes = 0
    for record in records:
        projected_rows: list[dict] = []
        for row in ensure_ack_ids(record):
            ack_id = str(row.get("ack_id") or "")
            if ack_id not in pending:
                continue
            row_bytes = len(
                json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            )
            if selected_ids and (
                len(selected_ids) >= limit
                or (max_bytes > 0 and rendered_bytes + row_bytes > max_bytes)
            ):
                return selected, selected_ids
            projected_rows.append(row)
            selected_ids.append(ack_id)
            rendered_bytes += row_bytes
            if len(selected_ids) >= limit:
                break
        if projected_rows:
            selected.append({**record, "candidates": projected_rows})
        if len(selected_ids) >= limit:
            break
    return selected, selected_ids


def _bind_audit_delivery_ref(
    state: WatchState,
    inflight: dict[str, Any],
) -> dict[str, Any]:
    """Bind one opaque token to the exact model-facing Audit delivery.

    The token carries no business meaning.  It only prevents a compact batch
    response from being replayed against another source, worker, takeover
    generation, or redelivered subset.
    """
    payload = dict(inflight)
    delivered = [
        str(value) for value in payload.get("delivered_ack_ids") or [] if str(value or "").strip()
    ]
    if not state.audit_guarantee or not delivered:
        payload.pop("delivery_ref", None)
        return payload
    material = json.dumps(
        {
            "schema": _AUDIT_DELIVERY_REF_SCHEMA,
            "audit_id": state.audit_root_task_id,
            "watch_id": state.watch_id,
            "generation": int(payload.get("generation") or 0),
            "from_seq": int(payload.get("from_seq") or 0),
            "to_seq": int(payload.get("to_seq") or 0),
            "consumer": str(payload.get("consumer") or ""),
            "delivery_attempt": max(
                1,
                int(payload.get("delivery_attempt") or 1),
            ),
            "ack_ids": delivered,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    payload["delivery_ref"] = f"ad-{sha256(material.encode()).hexdigest()[:24]}"
    return payload


def audit_verdict_token(delivery_ref: str, ack_id: str) -> str:
    """Return the opaque row handle for one exact Audit delivery.

    This handle is deliberately derived only from mechanical delivery identity.
    It carries no event sequence or business meaning, and it is never stored as
    the durable record identity.  The model only has to copy the handle adjacent
    to its verdict; the host resolves it back to the canonical ``ack_id``.
    """

    material = "\0".join(
        (
            _AUDIT_VERDICT_TOKEN_SCHEMA,
            str(delivery_ref or "").strip(),
            str(ack_id or "").strip(),
        )
    )
    return f"vt-{sha256(material.encode()).hexdigest()[:24]}"


def _finalize_delivery_throughput(
    cursor: dict[str, Any],
    inflight: dict[str, Any],
    *,
    now: float,
    allow_recovery_growth: bool = True,
    recovery_max_tokens: int | None = None,
) -> dict[str, Any]:
    """Fold one delivered view into durable, content-agnostic speed facts."""
    delivered_at = float(inflight.get("delivered_at") or 0.0)
    if delivered_at <= 0 or now <= delivered_at:
        return dict(cursor)
    pending = {str(value) for value in inflight.get("pending_acks") or []}
    delivered_ids = [str(value) for value in inflight.get("delivered_ack_ids") or [] if str(value)]
    # A successful or partial verdict clears the opaque delivery reference and
    # its model-facing id list before the next replay view is bound.  Such an
    # empty marker is not a zero-throughput sample; counting its idle interval
    # would make an already-settled view look slower every time it is replayed.
    if not delivered_ids:
        return dict(cursor)
    settled = sum(1 for ack_id in delivered_ids if ack_id not in pending)
    elapsed = max(0.001, now - delivered_at)
    updated = {
        **cursor,
        "processing_throughput": _updated_processing_throughput(
            cursor,
            settled=settled,
            elapsed=elapsed,
            now=now,
        ),
    }
    updated = _with_verdict_output_profile(updated, inflight, settled=settled, now=now)
    return _with_batch_recovery_growth(
        updated,
        inflight,
        delivered_ids=delivered_ids,
        settled=settled,
        now=now,
        allowed=allow_recovery_growth,
        max_tokens=recovery_max_tokens,
    )


def _updated_processing_throughput(
    cursor: dict[str, Any],
    *,
    settled: int,
    elapsed: float,
    now: float,
) -> dict[str, Any]:
    previous = (
        dict(cursor.get("processing_throughput") or {})
        if isinstance(cursor.get("processing_throughput"), dict)
        else {}
    )
    total_records = max(0, int(previous.get("settled_records") or 0)) + settled
    total_seconds = max(0.0, float(previous.get("active_seconds") or 0.0)) + elapsed
    return {
        "settled_records": total_records,
        "active_seconds": round(total_seconds, 6),
        "completed_delivery_samples": max(0, int(previous.get("completed_delivery_samples") or 0))
        + 1,
        "last_delivery_records": settled,
        "last_delivery_seconds": round(elapsed, 6),
        "records_per_second": (
            round(total_records / total_seconds, 6) if total_seconds > 0 else 0.0
        ),
        "updated_at": now,
    }


def _with_verdict_output_profile(
    cursor: dict[str, Any],
    inflight: dict[str, Any],
    *,
    settled: int,
    now: float,
) -> dict[str, Any]:
    observed_output_records = max(
        0,
        int(inflight.get("model_output_records") or 0),
    )
    observed_output_bytes = max(
        0,
        int(inflight.get("model_output_bytes") or 0),
    )
    observed_max_row_bytes = max(
        0,
        int(inflight.get("model_output_max_row_bytes") or 0),
    )
    if min(settled, observed_output_records, observed_output_bytes, observed_max_row_bytes) <= 0:
        return cursor
    previous = (
        dict(cursor.get("verdict_output_profile") or {})
        if isinstance(cursor.get("verdict_output_profile"), dict)
        else {}
    )
    total_records = max(0, int(previous.get("observed_records") or 0)) + observed_output_records
    total_bytes = max(0, int(previous.get("serialized_bytes") or 0)) + observed_output_bytes
    return {
        **cursor,
        "verdict_output_profile": {
            "schema": "audit-verdict-output-profile.v1",
            "observed_records": total_records,
            "serialized_bytes": total_bytes,
            "average_bytes_per_record": round(total_bytes / total_records, 3),
            "max_row_bytes": max(
                max(0, int(previous.get("max_row_bytes") or 0)),
                observed_max_row_bytes,
            ),
            "completed_delivery_samples": max(
                0,
                int(previous.get("completed_delivery_samples") or 0),
            )
            + 1,
            "updated_at": now,
        },
    }


def _with_batch_recovery_growth(
    cursor: dict[str, Any],
    inflight: dict[str, Any],
    *,
    delivered_ids: list[str],
    settled: int,
    now: float,
    allowed: bool,
    max_tokens: int | None,
) -> dict[str, Any]:
    recovery = (
        dict(cursor.get("batch_recovery") or {})
        if isinstance(cursor.get("batch_recovery"), dict)
        else {}
    )
    if not recovery or not allowed or settled <= 0 or settled != len(delivered_ids):
        return cursor
    # A fully settled replay proves that this whole-record delivery size is safe.
    current_ceiling = max(1, int(recovery.get("max_batch_tokens") or 1))
    delivered_tokens = max(
        1,
        int(inflight.get("estimated_input_tokens") or current_ceiling),
    )
    grown_ceiling = max(current_ceiling * 2, delivered_tokens * 2)
    if max_tokens is not None:
        grown_ceiling = min(grown_ceiling, max(1, int(max_tokens)))
    recovery.update(
        {
            "max_batch_tokens": grown_ceiling,
            "successful_delivery_count": max(
                0,
                int(recovery.get("successful_delivery_count") or 0),
            )
            + 1,
            "last_success_input_tokens": delivered_tokens,
            "last_success_at": now,
        }
    )
    return {**cursor, "batch_recovery": recovery}


_RECENT_PROCESSING_LATENCY_SAMPLE_LIMIT = 512


def _with_recent_processing_latency(
    cursor: dict[str, Any],
    ledger_rows: list[dict[str, Any]],
    *,
    now: float,
) -> dict[str, Any]:
    """Persist bounded source-to-verdict latency samples for cheap status reads."""
    previous = cursor.get("processing_latency")
    previous = dict(previous) if isinstance(previous, dict) else {}
    samples = [
        max(0.0, float(value))
        for value in previous.get("recent_seconds", [])
        if isinstance(value, int | float) and float(value) >= 0
    ]
    for row in ledger_rows:
        observed_at = float(row.get("observed_at") or 0.0)
        if observed_at > 0 and now >= observed_at:
            samples.append(round(now - observed_at, 3))
    samples = samples[-_RECENT_PROCESSING_LATENCY_SAMPLE_LIMIT:]
    if not samples:
        return dict(cursor)
    ordered = sorted(samples)

    def percentile(ratio: float) -> float:
        index = max(0, min(len(ordered) - 1, int((len(ordered) * ratio) + 0.999999) - 1))
        return round(ordered[index], 3)

    return {
        **cursor,
        "processing_latency": {
            "scope": "recent_source_to_verdict",
            "sample_count": len(samples),
            "recent_seconds": samples,
            "p50_seconds": percentile(0.50),
            "p95_seconds": percentile(0.95),
            "p99_seconds": percentile(0.99),
            "max_seconds": round(ordered[-1], 3),
            "updated_at": now,
        },
    }


def _acked_cursor(cursor: dict, inflight: dict, *, gap: bool = False) -> dict:
    """确认在途批:acked 计数推进、在途清空;gap=True 记不可恢复缺口(结构化计数)。"""
    payload = dict(cursor)
    pending = inflight.get("pending_acks")
    amount = len(pending) if isinstance(pending, list) else int(inflight.get("count") or 0)
    payload["candidates_acked"] = acked_candidates(cursor) + amount
    payload.pop("inflight", None)
    if gap:
        payload["redelivery_gap_candidates"] = (
            int(payload.get("redelivery_gap_candidates") or 0) + amount
        )
    return payload


def acked_candidates(cursor: dict[str, Any]) -> int:
    """已确认判完的候选累计数。旧 sidecar 没有 acked 字段:按已交付数起底
    (历史批无法追认,如实沿用旧口径,不追溯重投)。"""
    value = cursor.get("candidates_acked", cursor.get("candidates_consumed"))
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


# Default verdict values are a storage contract only. They neither route work
# nor decide whether, how, or when the Agent communicates with the user.
_VERDICT_KINDS = ("hit", "clear", "unsure")
_FINDING_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


def _verdict_ledger_path(state: WatchState) -> Path:
    return state_dir(state.owner_home) / f"{state.watch_id}.verdicts.ndjson"


def _verdict_ledger_append(
    state: WatchState,
    rows: list[dict[str, Any]],
) -> bool:
    """逐条结论台账(append-only,.ndjson 非上报面):每条候选的判断和复核都留痕。

    调用方只有在本函数成功后才能推进 ACK 游标；写失败必须 fail-closed，不能出现
    "已经签收但没有判断凭证"。
    """
    if not rows:
        return True
    path = _verdict_ledger_path(state)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = "".join(
            json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows
        )
        with path.open("a", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        _LOGGER.warning("verdict ledger append failed (watch=%s)", state.watch_id, exc_info=True)
        return False
    return True


def submit_verdicts(
    state: WatchState,
    *,
    consumer: str,
    verdicts: list[dict[str, Any]] | None = None,
    delivery_ref: str | None = None,
    source_authority: dict[str, object] | None = None,
    model_output_bytes: int = 0,
    model_output_records: int = 0,
    model_output_max_row_bytes: int = 0,
) -> dict[str, Any]:
    """/audit 保证档的逐条结论签收(ack-on-judge 的推进入口,与 pull 同线程序列化):
    把判读工交来的 [{ack_id, verdict, score, note?}] 与在途欠账(inflight.pending_acks)逐条对账
    ——对上的:结论进台账、acked 逐条 +1、欠账销一条;全销完才摘在途(下一次 pull 才发
    新批)。对不上的(ack_id 不在任何在途欠账里)原样退回 unknown,绝不凭空入账。
    游标/ACK 只在这里因"真判完+记了结论"推进——空转的判读工推不动任何账。"""
    parsed, shape_errors, malformed_ack_ids = _parsed_verdict_submission(
        state,
        verdicts,
    )
    if not parsed:
        return _invalid_verdict_submission(
            "verdicts 结构或数值范围无效；没有可安全写账的逐条结论",
            malformed=len(shape_errors),
            errors=shape_errors,
            error_code="AUDIT_VERDICT_SHAPE_INVALID",
        )
    review_modes = {bool(row.get("review")) for row in parsed.values()}
    # Historical review and explicit-id repair remain one atomic submission:
    # without a current delivery_ref there is no durable pending slice to
    # redeliver only malformed rows.  The normal Audit worker path always binds
    # the current delivery and can therefore settle valid rows independently.
    if shape_errors and not delivery_ref:
        return _invalid_verdict_submission(
            "verdicts 结构或数值范围无效；本次调用没有写账或签收",
            malformed=len(shape_errors),
            errors=shape_errors,
            error_code="AUDIT_VERDICT_SHAPE_INVALID",
        )
    if delivery_ref and review_modes == {True}:
        return _invalid_verdict_submission(
            "delivery_ref 只绑定当前首次交付，复核必须提供准确原文引用",
            malformed=1,
        )
    if review_modes == {True}:
        return _submit_review_verdicts(
            state,
            consumer=consumer,
            verdicts=parsed,
            source_authority=source_authority,
        )
    observation = _VerdictOutputObservation(
        serialized_bytes=model_output_bytes,
        records=model_output_records,
        max_row_bytes=model_output_max_row_bytes,
    )
    result = _submit_initial_verdicts(
        state,
        parsed,
        consumer=consumer,
        delivery_ref=delivery_ref,
        source_authority=source_authority,
        observation=observation,
    )
    if shape_errors and result.get("ok") is True:
        result.update(
            {
                "partial": True,
                "malformed": len(shape_errors),
                "malformed_ack_ids": malformed_ack_ids,
                "errors": shape_errors,
                "repair_note": (
                    "格式正确的逐条结论已经持久化；格式不完整的记录仍保持 pending，"
                    "下一次 pull 只会重投尚未签收的记录。"
                ),
            }
        )
    return result


def _parsed_verdict_submission(
    state: WatchState,
    verdicts: list[dict[str, Any]] | None,
) -> tuple[dict[str, dict[str, Any]], list[str], list[str]]:
    parsed: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    malformed_ack_ids: list[str] = []
    duplicate_ack_ids: set[str] = set()
    for index, row in enumerate(verdicts or []):
        normalized, error = _normalize_verdict_row(row, state=state)
        if error:
            errors.append(f"verdicts[{index}]: {error}")
            if isinstance(row, dict):
                ack_id = str(row.get("ack_id") or "").strip()
                if ack_id:
                    malformed_ack_ids.append(ack_id)
            continue
        assert normalized is not None
        ack_id = str(normalized["ack_id"])
        if ack_id in parsed or ack_id in duplicate_ack_ids:
            parsed.pop(ack_id, None)
            duplicate_ack_ids.add(ack_id)
            malformed_ack_ids.append(ack_id)
            errors.append(f"verdicts[{index}]: ack_id 重复: {ack_id}")
            continue
        parsed[ack_id] = normalized
    review_modes = {bool(row.get("review")) for row in parsed.values()}
    if len(review_modes) > 1:
        # Mixing initial and review changes ledger semantics for the whole
        # invocation.  Keep the old fail-closed behavior rather than partially
        # applying an ambiguous operation.
        errors.append("同一次 verdict 调用不能混合首次判断和复核判断")
        malformed_ack_ids.extend(parsed)
        parsed.clear()
    return parsed, errors, list(dict.fromkeys(malformed_ack_ids))


def _invalid_verdict_submission(
    error: str,
    *,
    malformed: int = 0,
    errors: list[str] | None = None,
    error_code: str = "",
) -> dict[str, Any]:
    return {
        "ok": False,
        "acked_now": 0,
        "malformed": malformed,
        **({"errors": errors} if errors else {}),
        **({"error_code": error_code} if error_code else {}),
        "error": error,
    }


@dataclass(frozen=True)
class _VerdictOutputObservation:
    serialized_bytes: int = 0
    records: int = 0
    max_row_bytes: int = 0


def _submit_initial_verdicts(
    state: WatchState,
    parsed: dict[str, dict[str, Any]],
    *,
    consumer: str,
    delivery_ref: str | None,
    source_authority: dict[str, object] | None,
    observation: _VerdictOutputObservation,
) -> dict[str, Any]:
    remaining = dict(parsed)
    with _cursor_mutation_fence(
        state,
        source_authority=source_authority,
    ) as gate:
        if not bool(gate.get("ok")):
            return _invalid_verdict_submission(
                str(gate.get("error") or "来源工作者写账授权已失效"),
                error_code=str(gate.get("error_code") or "AUDIT_SOURCE_AUTHORIZATION_FAILED"),
            )
        if delivery_ref:
            remaining, delivery_error = _bind_verdict_evidence_on_cursor(
                state,
                consumer=consumer,
                delivery_ref=delivery_ref,
                verdicts=remaining,
            )
            if delivery_error:
                return _invalid_verdict_submission(
                    delivery_error,
                    error_code="AUDIT_DELIVERY_REF_INVALID",
                )
        try:
            settled = _settle_verdicts_on_cursor(
                state,
                remaining,
                consumer,
                source_authority=source_authority,
                model_output_bytes=observation.serialized_bytes,
                model_output_records=observation.records,
                model_output_max_row_bytes=observation.max_row_bytes,
            )
        except OSError as exc:
            return _invalid_verdict_submission(f"结论持久化失败，未确认签收: {exc}")
    return _initial_verdict_result(settled, remaining)


def _initial_verdict_result(
    settled: tuple[int, int, dict[str, int], list[str]] | None,
    remaining: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    acked_now, pending_remaining, counts, acked_ids = (
        settled if settled is not None else (0, 0, {"hit": 0, "clear": 0, "unsure": 0}, [])
    )
    return {
        "ok": True,
        "acked_now": acked_now,
        "verdicts_hit": counts["hit"],
        "verdicts_clear": counts["clear"],
        "verdicts_unsure": counts["unsure"],
        "pending_remaining": pending_remaining,
        "acked_ids": acked_ids,
        "unknown_ack_ids": sorted(remaining),
        "malformed": 0,
        "reviewed_now": 0,
        "reviewed_ids": [],
    }


# LLM: Current-delivery identity is host-owned.  The model copies one opaque
# row token adjacent to each verdict; the host validates every supplied token
# and resolves it to a durable ack id. Array position is never an identity
# signal. Omitted rows remain pending and are redelivered with fresh tokens;
# explicit ids remain available for historical review and recovery tooling.
# 函数用途: 把当前交付批中本轮实际返回的一次性逐条令牌机械绑定到耐久 ack_id；只核对
# 交付引用和令牌，不读取 note、分数或业务字段，也不允许混用 token/ack 两种身份协议。
def bind_current_delivery_verdict_tokens(
    state: WatchState,
    *,
    consumer: str,
    delivery_ref: str | None,
    verdicts: list[dict[str, Any]],
) -> dict[str, Any]:
    rows = [dict(row) for row in verdicts]
    has_ack = [bool(str(row.get("ack_id") or "").strip()) for row in rows]
    has_token = [bool(str(row.get("verdict_token") or "").strip()) for row in rows]
    if rows and all(has_ack) and not any(has_token):
        return {"ok": True, "verdicts": rows, "binding": "explicit_ack_id"}
    mode_error = _verdict_identity_mode_error(rows, has_ack, has_token)
    if mode_error:
        return {
            "ok": False,
            "error": mode_error,
            "error_code": "AUDIT_VERDICT_IDENTITY_MODE_INVALID",
        }
    requested_ref = str(delivery_ref or "").strip()
    delivery, delivery_error = _current_delivery_for_verdict_tokens(
        state,
        consumer=consumer,
        requested_ref=requested_ref,
    )
    if delivery_error:
        return {
            "ok": False,
            "error": delivery_error,
            "error_code": "AUDIT_DELIVERY_REF_INVALID",
        }
    assert delivery is not None
    delivered = delivery
    expected = {audit_verdict_token(requested_ref, ack_id): ack_id for ack_id in delivered}
    coverage = _verdict_token_coverage(rows, expected)
    if coverage["error"]:
        return {
            "ok": False,
            "error": coverage["error"],
            "error_code": "AUDIT_VERDICT_TOKEN_COVERAGE_INVALID",
        }
    return _complete_token_verdict_binding(rows, expected)


def _verdict_identity_mode_error(
    rows: list[dict[str, Any]],
    has_ack: list[bool],
    has_token: list[bool],
) -> str:
    if any(has_ack) or (rows and not all(has_token)) or not rows:
        return (
            "同一次 verdict 必须统一使用当前批 verdict_token，或统一使用显式 ack_id；"
            "不能混合、同时携带或遗漏逐条身份"
        )
    if any(row.get("review") is True for row in rows):
        return "历史复核必须逐条提供准确 ack_id，不能使用当前批次 verdict_token"
    return ""


def _current_delivery_for_verdict_tokens(
    state: WatchState,
    *,
    consumer: str,
    requested_ref: str,
) -> tuple[list[str] | None, str]:
    if not requested_ref:
        return None, "使用 verdict_token 时必须提供同一次 pull 返回的 delivery_ref"
    cursor = read_spool_cursor(state)
    inflight = cursor.get("inflight")
    if not isinstance(inflight, dict):
        return None, "当前没有可签收的在途 Audit 批次"
    if str(inflight.get("consumer") or "") != str(consumer or ""):
        return None, "delivery_ref 不属于当前来源工作者"
    expected_ref = str(inflight.get("delivery_ref") or "").strip()
    if not expected_ref or requested_ref != expected_ref:
        return None, "delivery_ref 已过期或不属于当前交付批次；请重新 pull"
    delivered = [
        str(value) for value in inflight.get("delivered_ack_ids") or [] if str(value or "").strip()
    ]
    pending = {
        str(value) for value in inflight.get("pending_acks") or [] if str(value or "").strip()
    }
    if not delivered or not set(delivered).issubset(pending):
        return None, "当前交付集合已变化；请重新 pull 获取新的 delivery_ref"
    return delivered, ""


def _verdict_token_coverage(
    rows: list[dict[str, Any]],
    expected: dict[str, str],
) -> dict[str, object]:
    supplied_tokens = [str(row.get("verdict_token") or "").strip() for row in rows]
    supplied_set = set(supplied_tokens)
    duplicate_tokens = sorted(token for token in supplied_set if supplied_tokens.count(token) > 1)
    missing_tokens = sorted(set(expected) - supplied_set)
    unknown_tokens = sorted(supplied_set - set(expected))
    if not duplicate_tokens and not unknown_tokens:
        return {"error": "", "missing": missing_tokens}
    duplicate_summary = _verdict_token_error_summary("重复", duplicate_tokens)
    unknown_summary = _verdict_token_error_summary("未知", unknown_tokens)
    coverage_rule = "本次提供的 verdict_token 必须唯一且属于当前 pull"
    problem_summary = f"{duplicate_summary}，{unknown_summary}"
    repair_hint = (
        "请只从当前 candidates 逐条原样复制；未提交的记录会保持 pending，"
        "下一次 pull 只返回这些欠账并生成新的 delivery_ref/verdict_token"
    )
    return {
        "missing": missing_tokens,
        "error": (
            f"{coverage_rule}；"
            f"当前交付 {len(expected)} 条，实际提交 {len(rows)} 条，"
            f"{problem_summary}。{repair_hint}"
        ),
    }


def _verdict_token_error_summary(label: str, tokens: list[str], *, limit: int = 8) -> str:
    """Bound repair feedback without hiding how many opaque ids were invalid."""

    visible = tokens[:limit]
    if len(tokens) <= limit:
        return f"{label} {len(tokens)} 个 {visible}"
    return f"{label} {len(tokens)} 个 {visible}（仅显示前 {limit} 个）"


def _complete_token_verdict_binding(
    rows: list[dict[str, Any]],
    expected: dict[str, str],
) -> dict[str, Any]:
    return {
        "ok": True,
        "verdicts": [
            {
                **{key: value for key, value in row.items() if key != "verdict_token"},
                "ack_id": expected[str(row["verdict_token"]).strip()],
            }
            for row in rows
        ],
        "binding": "delivery_token",
    }


# LLM: A model-written conclusion may select business meaning, but it cannot
# rebind that prose to a different durable record.  A valid delivery_ref plus
# ack_id lets the host bind the two redundant opaque evidence fields; explicit
# values are still verified and historical review keeps requiring all three.
# 函数用途: 在 verdict 落账前核对 ack_id、source_ref 与原文哈希属于同一条记录；
# 只做身份一致性校验和可信上下文补全，不解析 note、分数、事件字段或业务真假。
def validate_verdict_evidence_refs(
    state: WatchState,
    *,
    consumer: str,
    verdicts: list[dict[str, Any]],
    delivery_ref: str | None = None,
) -> dict[str, Any]:
    cursor = read_spool_cursor(state)
    inflight = cursor.get("inflight")
    current_delivery = isinstance(inflight, dict) and str(inflight.get("consumer") or "") == str(
        consumer or ""
    )
    pending = (
        {str(item) for item in (inflight.get("pending_acks") or []) if str(item)}
        if current_delivery
        else set()
    )
    evidence = (
        _inflight_evidence_facts(state, inflight)
        if current_delivery and isinstance(inflight, dict)
        else {}
    )
    host_can_bind = bool(str(delivery_ref or "").strip()) and current_delivery
    mismatches: list[str] = []
    for row in verdicts:
        ack_id = str(row.get("ack_id") or "").strip()
        supplied_ref = str(row.get("source_ref") or "").strip()
        supplied_hash = str(row.get("event_sha256") or "").strip()
        if row.get("review") is True:
            candidate, _raw_file, record = _find_audit_candidate_by_ack(state, ack_id)
            if candidate is None or record is None:
                mismatches.append(ack_id)
                continue
            expected_ref = audit_source_ref(state, ack_id)
            expected_hash = str(candidate.get("event_sha256") or "").strip()
            if (
                not supplied_ref
                or supplied_ref != expected_ref
                or not supplied_hash
                or not expected_hash
                or supplied_hash != expected_hash
            ):
                mismatches.append(ack_id)
            continue
        if ack_id not in pending:
            continue
        expected = evidence.get(ack_id) if isinstance(evidence.get(ack_id), dict) else {}
        expected_ref = audit_source_ref(state, ack_id)
        expected_hash = str(expected.get("event_sha256") or "").strip()
        if (
            (supplied_ref and supplied_ref != expected_ref)
            or not expected_hash
            or (supplied_hash and supplied_hash != expected_hash)
            or (not host_can_bind and (not supplied_ref or not supplied_hash))
        ):
            mismatches.append(ack_id)
    return {
        "ok": not mismatches,
        "mismatch_ack_ids": list(dict.fromkeys(mismatches)),
    }


def _normalize_verdict_row(
    row: object,
    *,
    state: WatchState | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """Validate one result by shape/range only; never interpret its semantics."""
    if not isinstance(row, dict):
        return None, "verdict 项必须是对象"
    allowed = {
        "ack_id",
        "source_ref",
        "event_sha256",
        "verdict",
        "score",
        "score_range",
        "dimensions",
        "note",
        "judge_mode",
        "judge_model",
        "judge_backend",
        "review",
        "finding",
    }
    extras = sorted(str(key) for key in row if key not in allowed)
    if extras:
        return None, f"verdict 项包含未知字段: {extras}"
    ack_id = str(row.get("ack_id") or "").strip()
    if not ack_id:
        return None, "verdict 项缺 ack_id"
    kind = str(row.get("verdict") or "").strip().lower()
    if kind not in _VERDICT_KINDS:
        return None, f"{ack_id}: verdict 必须是 hit/clear/unsure"
    score = _finite_number(row.get("score"))
    if score is None:
        return None, f"{ack_id}: score 必须是有限数字"
    score_range, range_error = _score_range(
        row.get("score_range"),
        default=(0, 100),
    )
    if range_error:
        return None, f"{ack_id}: {range_error}"
    assert score_range is not None
    if not score_range["min"] <= score <= score_range["max"]:
        return None, (f"{ack_id}: score={score} 不在 [{score_range['min']}, {score_range['max']}]")
    note = row.get("note", "")
    if not isinstance(note, str):
        return None, f"{ack_id}: note 必须是字符串"
    if kind in {"hit", "unsure"} and not note.strip():
        return None, f"{ack_id}: hit/unsure 的 note 必须是非空判断理由"
    dimensions, dimensions_error = _score_dimensions(row.get("dimensions"))
    if dimensions_error:
        return None, f"{ack_id}: {dimensions_error}"
    review = row.get("review", False)
    if not isinstance(review, bool):
        return None, f"{ack_id}: review 必须是布尔值"
    finding, finding_error = _normalize_inline_finding(
        row.get("finding"),
        state=state,
        default_stage="review" if review else "initial",
    )
    if finding_error:
        return None, f"{ack_id}: {finding_error}"
    if kind == "hit" and finding is None:
        return None, (
            f"{ack_id}: hit 必须携带 finding；"
            "程序只校验结构化结论，不从 note 或原文推断是否需要汇报"
        )
    if kind == "clear" and finding is not None:
        return None, (
            f"{ack_id}: clear 与 finding 互相矛盾；"
            "需要升级或继续调查时请提交 hit/unsure，程序不会猜哪一个字段才是真的"
        )
    if kind == "hit":
        raw_finding = row.get("finding")
        if isinstance(raw_finding, dict) and raw_finding.get("requires_llm_report") is False:
            return None, (
                f"{ack_id}: hit 与 finding.requires_llm_report=false 互相矛盾；"
                "省略该字段或显式提交 true"
            )
        # ``hit`` already is the model's typed decision that the record meets
        # the current Audit's report condition. Project the redundant routing
        # bit mechanically instead of making the model restate the same fact.
        assert finding is not None
        finding["requires_llm_report"] = True
    return {
        "ack_id": ack_id,
        "source_ref": str(row.get("source_ref") or "").strip(),
        "event_sha256": str(row.get("event_sha256") or "").strip(),
        "verdict": kind,
        "note": note.strip(),
        "score": score,
        "score_range": score_range,
        "dimensions": dimensions,
        "judge_mode": str(row.get("judge_mode") or "agent")[:40],
        "judge_model": str(row.get("judge_model") or "")[:200],
        "judge_backend": str(row.get("judge_backend") or "")[:80],
        "review": review,
        "finding": finding,
    }, ""


def _bind_verdict_evidence_on_cursor(
    state: WatchState,
    *,
    consumer: str,
    delivery_ref: str,
    verdicts: dict[str, dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], str]:
    """Verify every row identity inside one exact current delivery.

    ``delivery_ref`` proves the exact current batch and ``ack_id`` selects one
    delivered record.  The host binds that record's canonical source reference
    and opaque raw-record hash.  If the caller supplies either redundant value,
    it must still match.  The host never interprets the business conclusion.
    """

    cursor = read_spool_cursor(state)
    inflight = cursor.get("inflight")
    if not isinstance(inflight, dict):
        return {}, "当前没有可签收的在途 Audit 批次"
    if str(inflight.get("consumer") or "") != str(consumer or ""):
        return {}, "delivery_ref 不属于当前来源工作者"
    expected_ref = str(inflight.get("delivery_ref") or "").strip()
    if not expected_ref or str(delivery_ref or "").strip() != expected_ref:
        return {}, "delivery_ref 已过期或不属于当前交付批次；请重新 pull"
    pending = {
        str(value) for value in inflight.get("pending_acks") or [] if str(value or "").strip()
    }
    delivered = {
        str(value) for value in inflight.get("delivered_ack_ids") or [] if str(value or "").strip()
    }
    if not delivered or not delivered.issubset(pending):
        return {}, "当前交付集合已变化；请重新 pull 获取新的 delivery_ref"
    unknown = sorted(set(verdicts) - delivered)
    if unknown:
        return {}, f"verdicts 不属于当前交付: {unknown}"
    evidence = _inflight_evidence_facts(state, inflight)
    bound: dict[str, dict[str, Any]] = {}
    for ack_id, verdict in verdicts.items():
        fact = evidence.get(ack_id) if isinstance(evidence.get(ack_id), dict) else {}
        event_hash = str(fact.get("event_sha256") or "").strip()
        if not event_hash:
            return {}, f"{ack_id}: 当前交付缺少原文哈希，已拒绝签收"
        source_ref = audit_source_ref(state, ack_id)
        supplied_ref = str(verdict.get("source_ref") or "").strip()
        supplied_hash = str(verdict.get("event_sha256") or "").strip()
        if supplied_ref and supplied_ref != source_ref:
            return {}, f"{ack_id}: source_ref 与当前交付不一致"
        if supplied_hash and supplied_hash != event_hash:
            return {}, f"{ack_id}: event_sha256 与当前交付不一致"
        bound[ack_id] = {
            **verdict,
            "source_ref": source_ref,
            "event_sha256": event_hash,
        }
    return bound, ""


def _normalize_inline_finding(
    raw: object,
    *,
    state: WatchState | None,
    default_stage: str = "initial",
) -> tuple[dict[str, Any] | None, str]:
    if raw is None:
        return None, ""
    if not isinstance(raw, dict):
        return None, "finding 必须是对象"
    allowed = {
        "claim",
        "kind",
        "confidence",
        "finding_id",
        "stage",
        "needs_evidence",
        "urgency",
        "requires_llm_report",
        "evidence_refs",
    }
    extras = sorted(str(key) for key in raw if key not in allowed)
    if extras:
        return None, f"finding 包含未知字段: {extras}"
    claim = raw.get("claim")
    if not isinstance(claim, str) or not claim.strip():
        return None, "finding.claim 必须是非空字符串"
    finding_id = str(raw.get("finding_id") or "").strip()
    if finding_id and _FINDING_ID_RE.fullmatch(finding_id) is None:
        return None, "finding.finding_id 格式无效"
    urgency = str(raw.get("urgency") or "normal").strip().lower()
    if urgency not in {"normal", "urgent"}:
        return None, "finding.urgency 必须是 normal 或 urgent"
    for key in ("needs_evidence", "requires_llm_report"):
        if key in raw and not isinstance(raw.get(key), bool):
            return None, f"finding.{key} 必须是布尔值"
    raw_refs = raw.get("evidence_refs", [])
    if not isinstance(raw_refs, list):
        return None, "finding.evidence_refs 必须是数组"
    refs = [str(value).strip() for value in raw_refs if str(value or "").strip()]
    if len(refs) > 20:
        return None, "finding.evidence_refs 最多 20 项"
    if state is not None:
        for ref in refs:
            parsed = parse_audit_source_ref(ref)
            if parsed is not None and parsed[0] != state.watch_id:
                return None, "finding 不能引用其他 Audit 来源的 source_ref"
    return {
        "claim": claim.strip()[:2000],
        "kind": str(raw.get("kind") or "finding").strip() or "finding",
        "confidence": str(raw.get("confidence") or "").strip(),
        "finding_id": finding_id,
        "stage": str(raw.get("stage") or default_stage).strip() or default_stage,
        "needs_evidence": bool(raw.get("needs_evidence", False)),
        "urgency": urgency,
        "requires_llm_report": bool(raw.get("requires_llm_report", False)),
        "evidence_refs": list(dict.fromkeys(refs)),
    }, ""


def _submit_review_verdicts(
    state: WatchState,
    *,
    consumer: str,
    verdicts: dict[str, dict[str, Any]],
    source_authority: dict[str, object] | None = None,
) -> dict[str, Any]:
    """Append an optional second judgment without changing initial ACK accounting.

    The runtime validates only exact record existence and shape.  It does not decide
    which records require review, who should review them, or how a review affects
    reporting.
    """
    now = time.time()
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    with _cursor_mutation_fence(
        state,
        source_authority=source_authority,
    ) as gate:
        if not bool(gate.get("ok")):
            return {
                "ok": False,
                "acked_now": 0,
                "reviewed_now": 0,
                "reviewed_ids": [],
                "unknown_ack_ids": [],
                "malformed": 0,
                "error_code": str(gate.get("error_code") or "AUDIT_SOURCE_AUTHORIZATION_FAILED"),
                "error": str(gate.get("error") or "来源工作者复核授权已失效"),
            }
        for ack_id, verdict in verdicts.items():
            candidate, _raw_file, record = _find_audit_candidate_by_ack(
                state,
                ack_id,
            )
            history = _find_audit_verdicts(state, ack_id)
            if candidate is None:
                errors.append(f"{ack_id}: 没有对应的 owner 内原始记录")
                continue
            if not any(str(row.get("stage") or "initial") == "initial" for row in history):
                errors.append(f"{ack_id}: 首次判断尚未签收，不能登记复核")
                continue
            entry = _verdict_ledger_entry(
                state,
                _VerdictLedgerRequest(
                    ack_id=ack_id,
                    verdict=verdict,
                    consumer=consumer,
                    now=now,
                    stage="review",
                    candidate=candidate,
                    record=record,
                ),
                source_authority=source_authority,
            )
            last = history[-1] if history else None
            if not (
                isinstance(last, dict)
                and str(last.get("stage") or "") == "review"
                and _same_verdict_projection(last, entry)
            ):
                rows.append(entry)
        if errors:
            return {
                "ok": False,
                "acked_now": 0,
                "reviewed_now": 0,
                "reviewed_ids": [],
                "unknown_ack_ids": [],
                "malformed": 0,
                "errors": errors,
                "error": "复核引用无效；本次调用没有写账",
            }
        if not _verdict_ledger_append(state, rows):
            return {
                "ok": False,
                "acked_now": 0,
                "reviewed_now": 0,
                "reviewed_ids": [],
                "unknown_ack_ids": [],
                "malformed": 0,
                "error": "复核结论持久化失败",
            }
    ids = list(verdicts)
    return {
        "ok": True,
        "acked_now": 0,
        "reviewed_now": len(rows),
        "reviewed_ids": ids,
        "unknown_ack_ids": [],
        "malformed": 0,
    }


def _finite_number(value: object) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    if not math.isfinite(parsed):
        return None
    return value


def _score_range(
    raw: object,
    *,
    default: tuple[int | float, int | float] | None = None,
) -> tuple[dict[str, int | float] | None, str]:
    if raw is None and default is not None:
        return {"min": default[0], "max": default[1]}, ""
    if not isinstance(raw, dict):
        return None, "score_range 必须包含有限数字 min/max"
    extras = sorted(str(key) for key in raw if key not in {"min", "max"})
    if extras:
        return None, f"score_range 包含未知字段: {extras}"
    minimum = _finite_number(raw.get("min"))
    maximum = _finite_number(raw.get("max"))
    if minimum is None or maximum is None:
        return None, "score_range 必须包含有限数字 min/max"
    if minimum > maximum:
        return None, "score_range.min 不能大于 max"
    return {"min": minimum, "max": maximum}, ""


def _score_dimensions(
    raw: object,
) -> tuple[list[dict[str, Any]], str]:
    if raw is None:
        return [], ""
    if not isinstance(raw, list):
        return [], "dimensions 必须是数组"
    dimensions: list[dict[str, Any]] = []
    names: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            return [], f"dimensions[{index}] 必须是对象"
        extras = sorted(str(key) for key in item if key not in {"name", "score", "range", "reason"})
        if extras:
            return [], f"dimensions[{index}] 包含未知字段: {extras}"
        name = str(item.get("name") or "").strip()
        if not name:
            return [], f"dimensions[{index}].name 不能为空"
        if name in names:
            return [], f"维度名称重复: {name}"
        names.add(name)
        score = _finite_number(item.get("score"))
        if score is None:
            return [], f"dimensions[{index}].score 必须是有限数字"
        item_range = None
        if item.get("range") is not None:
            item_range, range_error = _score_range(item.get("range"))
            if range_error:
                return [], f"dimensions[{index}].{range_error}"
            assert item_range is not None
            if not item_range["min"] <= score <= item_range["max"]:
                return [], (
                    f"dimensions[{index}].score={score} 不在 "
                    f"[{item_range['min']}, {item_range['max']}]"
                )
        dimension: dict[str, Any] = {"name": name, "score": score}
        if item_range is not None:
            dimension["range"] = item_range
        if item.get("reason") is not None:
            if not isinstance(item.get("reason"), str):
                return [], f"dimensions[{index}].reason 必须是字符串"
            dimension["reason"] = str(item["reason"])
        dimensions.append(dimension)
    return dimensions, ""


def _settle_verdicts_on_cursor(
    state: WatchState,
    remaining: dict[str, dict[str, Any]],
    consumer: str,
    *,
    source_authority: dict[str, object] | None = None,
    model_output_bytes: int = 0,
    model_output_records: int = 0,
    model_output_max_row_bytes: int = 0,
) -> tuple[int, int, dict[str, int], list[str]] | None:
    """在读游标的在途欠账上销账(remaining 原地消耗,销掉的条目从中移除):
    返回 (销账数, 本批剩余欠账数, 各结论计数);无在途/无交集返回 None(没参与)。
    销账 = 结论进台账 + acked 逐条推进 + 欠账收缩;欠账清零才摘在途(发新批的闸)。"""
    cursor = read_spool_cursor(state)
    inflight = cursor.get("inflight") if isinstance(cursor.get("inflight"), dict) else None
    if inflight is None or not isinstance(inflight.get("pending_acks"), list):
        return None
    pending = [str(x) for x in inflight.get("pending_acks") or []]
    matched = [aid for aid in pending if aid in remaining]
    if not matched:
        return None
    now = time.time()
    context = _InitialSettlementContext(
        state=state,
        inflight=inflight,
        consumer=consumer,
        source_authority=source_authority,
        now=now,
    )
    ledger_rows, row_counts, finding_count = _initial_verdict_ledger_rows(
        context,
        remaining,
        matched,
    )
    left = [aid for aid in pending if aid not in set(matched)]
    updated = _with_initial_verdict_accounting(
        cursor,
        matched=matched,
        row_counts=row_counts,
        finding_count=finding_count,
    )
    observation = _VerdictOutputObservation(
        serialized_bytes=model_output_bytes,
        records=model_output_records,
        max_row_bytes=model_output_max_row_bytes,
    )
    updated = _with_settlement_throughput(
        updated,
        context,
        matched=matched,
        left=left,
        observation=observation,
    )
    updated = _with_next_settlement_inflight(updated, inflight, left, now=now)
    changed_rows = [
        row
        for row in ledger_rows
        if not _same_verdict_projection(_find_audit_verdict(state, row["ack_id"]), row)
    ]
    updated = _with_recent_processing_latency(updated, changed_rows, now=now)
    if not _verdict_ledger_append(state, changed_rows):
        raise OSError("verdict ledger 写入失败")
    if not _write_spool_cursor(state, updated):
        raise OSError("ack cursor 写入失败")
    return len(matched), len(left), row_counts, matched


@dataclass(frozen=True)
class _InitialSettlementContext:
    state: WatchState
    inflight: dict[str, Any]
    consumer: str
    source_authority: dict[str, object] | None
    now: float


def _initial_verdict_ledger_rows(
    context: _InitialSettlementContext,
    remaining: dict[str, dict[str, Any]],
    matched: list[str],
) -> tuple[list[dict[str, Any]], dict[str, int], int]:
    evidence = _inflight_evidence_facts(context.state, context.inflight)
    rows: list[dict[str, Any]] = []
    counts = {"hit": 0, "clear": 0, "unsure": 0}
    finding_count = 0
    for ack_id in matched:
        verdict = remaining.pop(ack_id)
        kind = str(verdict["verdict"])
        counts[kind] += 1
        entry = _initial_verdict_ledger_entry(context, ack_id, verdict, evidence.get(ack_id, {}))
        finding_count += int("finding" in entry)
        rows.append(entry)
    return rows, counts, finding_count


def _initial_verdict_ledger_entry(
    context: _InitialSettlementContext,
    ack_id: str,
    verdict: dict[str, Any],
    evidence: dict[str, Any],
) -> dict[str, Any]:
    entry = {
        "ack_id": ack_id,
        "verdict": str(verdict["verdict"]),
        "score": verdict["score"],
        "by": str(context.consumer or ""),
        "processing_run_id": str(context.consumer or ""),
        "t": round(context.now, 3),
        "stage": "initial",
        "acknowledged": True,
        "source_ref": audit_source_ref(context.state, ack_id),
        "score_range": verdict["score_range"],
        "dimensions": verdict["dimensions"],
        "note": str(verdict["note"]),
        **evidence,
    }
    if verdict.get("finding") is not None:
        entry["finding"] = dict(verdict["finding"])
    _apply_source_authority(entry, context.consumer, context.source_authority)
    for key in ("judge_mode", "judge_model", "judge_backend"):
        if verdict.get(key):
            entry[key] = verdict[key]
    return entry


def _apply_source_authority(
    entry: dict[str, Any],
    consumer: str,
    authority: dict[str, object] | None,
) -> None:
    if not authority:
        return
    entry["processing_run_id"] = str(authority.get("run_id") or consumer or "")
    entry["processing_attempt_id"] = str(authority.get("attempt_id") or "")
    entry["lease_epoch"] = int(authority.get("lease_epoch") or 0)


def _with_initial_verdict_accounting(
    cursor: dict[str, Any],
    *,
    matched: list[str],
    row_counts: dict[str, int],
    finding_count: int,
) -> dict[str, Any]:
    updated = {**cursor, "candidates_acked": acked_candidates(cursor) + len(matched)}
    for kind, count in row_counts.items():
        if count:
            updated[f"verdicts_{kind}"] = int(cursor.get(f"verdicts_{kind}") or 0) + count
    if finding_count:
        updated["findings_submitted"] = int(cursor.get("findings_submitted") or 0) + finding_count
    return updated


def _with_settlement_throughput(
    cursor: dict[str, Any],
    context: _InitialSettlementContext,
    *,
    matched: list[str],
    left: list[str],
    observation: _VerdictOutputObservation,
) -> dict[str, Any]:
    delivered_ids = [
        str(value)
        for value in context.inflight.get("delivered_ack_ids") or []
        if str(value or "").strip()
    ]
    matched_set = set(matched)
    if not matched or not matched_set.issubset(set(delivered_ids)):
        return cursor
    observation_valid = (
        observation.records == len(matched)
        and observation.serialized_bytes > 0
        and observation.max_row_bytes > 0
    )
    sampled_inflight = {
        **context.inflight,
        "delivered_ack_ids": list(matched),
        "pending_acks": left,
        "model_output_records": observation.records if observation_valid else 0,
        "model_output_bytes": observation.serialized_bytes if observation_valid else 0,
        "model_output_max_row_bytes": observation.max_row_bytes if observation_valid else 0,
    }
    return _finalize_delivery_throughput(
        cursor,
        sampled_inflight,
        now=context.now,
        allow_recovery_growth=bool(delivered_ids) and matched_set == set(delivered_ids),
        recovery_max_tokens=max(
            1,
            int(context.state.tuning.guarantee_batch_max_tokens or 45_000),
        ),
    )


def _with_next_settlement_inflight(
    cursor: dict[str, Any],
    inflight: dict[str, Any],
    left: list[str],
    *,
    now: float,
) -> dict[str, Any]:
    updated = dict(cursor)
    if left:
        next_inflight = {
            **inflight,
            "pending_acks": left,
            "delivered_ack_ids": [],
            "model_output_records": 0,
            "model_output_bytes": 0,
            "model_output_max_row_bytes": 0,
        }
        next_inflight.pop("delivery_ref", None)
        updated["inflight"] = next_inflight
    else:
        updated.pop("inflight", None)
    updated["updated_at"] = now
    return updated


def _same_verdict_projection(
    existing: dict[str, Any] | None,
    current: dict[str, Any],
) -> bool:
    if existing is None:
        return False
    keys = (
        "verdict",
        "score",
        "score_range",
        "dimensions",
        "note",
        "finding",
    )
    return all(existing.get(key) == current.get(key) for key in keys)


@dataclass(frozen=True)
class _VerdictLedgerRequest:
    ack_id: str
    verdict: dict[str, Any]
    consumer: str
    now: float
    stage: str
    candidate: dict[str, Any]
    record: dict[str, Any]


def _verdict_ledger_entry(
    state: WatchState,
    request: _VerdictLedgerRequest,
    *,
    source_authority: dict[str, object] | None = None,
) -> dict[str, Any]:
    entry = {
        "ack_id": request.ack_id,
        "verdict": str(request.verdict["verdict"]),
        "score": request.verdict["score"],
        "by": str(request.consumer or ""),
        "processing_run_id": str(request.consumer or ""),
        "t": round(request.now, 3),
        "stage": request.stage,
        "acknowledged": request.stage == "initial",
        "source_ref": audit_source_ref(state, request.ack_id),
        "score_range": request.verdict["score_range"],
        "dimensions": request.verdict["dimensions"],
        "note": str(request.verdict["note"]),
        **_candidate_evidence_fact(
            state,
            request.ack_id,
            request.candidate,
            request.record,
        ),
    }
    for key in ("judge_mode", "judge_model", "judge_backend"):
        if request.verdict.get(key):
            entry[key] = request.verdict[key]
    if request.verdict.get("finding") is not None:
        entry["finding"] = dict(request.verdict["finding"])
    _apply_source_authority(entry, request.consumer, source_authority)
    return entry


def _candidate_evidence_fact(
    state: WatchState,
    ack_id: str,
    candidate: dict[str, Any],
    record: dict[str, Any],
) -> dict[str, Any]:
    event = candidate.get("event")
    event_id = str(event.get("event_id") or "") if isinstance(event, dict) else ""
    item: dict[str, Any] = {
        "owner_id": state.owner_id,
        "audit_id": state.audit_root_task_id,
        "audit_run_epoch": max(0, int(state.audit_run_epoch or 0)),
        "ingest_run_epoch": max(0, int(record.get("audit_run_epoch") or 0)),
        "source_id": state.source_id,
        "watch_id": state.watch_id,
        "source_ref": str(candidate.get("source_ref") or audit_source_ref(state, ack_id)),
        "spool_seq": int(record.get("spool_seq") or 0),
        "spool_generation": int(record.get("generation") or 0),
        "stream_pos": candidate.get("stream_pos"),
        "source_cursor": record.get("cursor_to"),
        "observed_at": record.get("t"),
        "raw_complete": not (isinstance(event, dict) and "__truncated__" in event),
    }
    if event_id:
        item["event_id"] = event_id
    if candidate.get("event_sha256"):
        item["event_sha256"] = str(candidate["event_sha256"])
    if candidate.get("event_bytes") is not None:
        item["event_bytes"] = int(candidate["event_bytes"])
    return item


# LLM: Read candidate metadata only from the exact inflight spool range being settled.
# This ties every verdict to immutable raw evidence without trusting model-supplied refs.
# 函数用途: 在结论入账时补上原日志引用、哈希和位置，方便以后按一条记录复查。
def _inflight_evidence_facts(
    state: WatchState,
    inflight: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    try:
        records = _scan_spool_range(state, inflight)
    except OSError:
        return {}
    facts: dict[str, dict[str, Any]] = {}
    for record in records:
        for row in ensure_ack_ids(record):
            if not isinstance(row, dict):
                continue
            ack_id = str(row.get("ack_id") or "")
            facts[ack_id] = _candidate_evidence_fact(
                state,
                ack_id,
                row,
                record,
            )
            facts[ack_id]["delivery_attempt"] = max(
                1,
                int(inflight.get("delivery_attempt") or 1),
            )
            facts[ack_id]["retry_count"] = max(
                0,
                int(inflight.get("delivery_attempt") or 1) - 1,
            )
    return facts


def audit_receipt_facts(state: WatchState) -> dict[str, Any]:
    """覆盖回执(用户敢不重审的唯一凭证,任意时刻可查):入队 X · 已判 Y · 待判 M ·
    丢弃 0。纯盘上/内存结构计数拼装,零推断:
    - enqueued=引擎累计入队候选;judged=全消费面逐条销账合计;pending=差值(还在判,
      不是漏)。dropped=已入队后未判即消失的(重投缺口)+ 保证档启用后引擎有损计数的
      增量(启用后就不该再涨)——恒 0 是硬约束,>0 即 bug 亮红,绝不粉饰。
    - source_gap_events:源端滚动缓冲淘汰造成的拉取缺口(丢在源端,不是队列里),如实单列。"""
    written = int(state.totals.get("spool_candidates", 0) or 0)
    _consumed, acked = consumed_and_acked_on_disk(state.owner_home, state.watch_id)
    cursor = read_spool_cursor(state)
    gap = int(cursor.get("redelivery_gap_candidates") or 0)
    verdict_counts = {
        kind: int(cursor.get(f"verdicts_{kind}") or 0) for kind in ("hit", "clear", "unsure")
    }
    lossy_now = sum(
        int(state.engine.totals.get(k, 0) or 0)
        for k in ("suppressed", "overflow", "audit_throttled")
    )
    lossy_baseline = sum(
        int(state.audit_baseline.get(k, 0) or 0)
        for k in ("suppressed", "overflow", "audit_throttled")
    )
    receipt = {
        "mode": "audit_guarantee",
        "enqueued": written,
        "judged": min(acked, written) if written else acked,
        "scored": min(acked, written) if written else acked,
        "pending": max(0, written - acked),
        "dropped": gap + max(0, lossy_now - lossy_baseline),
        "verdicts": verdict_counts,
        "findings_submitted": int(cursor.get("findings_submitted") or 0),
        "source_gap_events": int(state.totals.get("gap_events", 0) or 0),
    }
    projection = read_json_object_report(
        state_dir(state.owner_home) / f"{state.watch_id}.inline-findings.cursor.json",
        context="audit_receipt.inline_findings",
    )
    if projection.load_error is None:
        receipt["findings_projected"] = int(projection.payload.get("projected_total") or 0)
    skips = int(state.totals.get("disk_backpressure_skips", 0) or 0)
    if skips > 0:
        receipt["disk_backpressure_skips"] = skips
    return receipt


def audit_capacity_facts(
    state: WatchState,
    *,
    now: float | None = None,
) -> dict[str, Any]:
    """Return cheap, current throughput/backlog health without scanning history."""
    observed_at = time.time() if now is None else float(now)
    written = int(state.totals.get("spool_candidates", 0) or 0)
    _consumed, acked = consumed_and_acked_on_disk(state.owner_home, state.watch_id)
    pending = max(0, written - acked)
    cursor = read_spool_cursor(state)
    throughput = cursor.get("processing_throughput")
    latency = cursor.get("processing_latency")
    projected_latency = (
        {key: value for key, value in latency.items() if key != "recent_seconds"}
        if isinstance(latency, dict)
        else {}
    )
    oldest_fetched_at = _oldest_pending_fetched_at(state, cursor)
    oldest_age = (
        max(0.0, observed_at - oldest_fetched_at) if pending > 0 and oldest_fetched_at > 0 else 0.0
    )
    backlog_threshold = max(
        1,
        int(getattr(state.tuning, "capacity_alert_backlog_records", 1000) or 1000),
    )
    age_threshold = max(
        1,
        int(getattr(state.tuning, "capacity_alert_oldest_seconds", 900) or 900),
    )
    capacity_alert = {
        "active": bool(pending >= backlog_threshold or oldest_age >= age_threshold),
        "backlog_threshold_records": backlog_threshold,
        "oldest_threshold_seconds": age_threshold,
        "reasons": [
            reason
            for reason, matched in (
                ("backlog_threshold", pending >= backlog_threshold),
                ("oldest_pending_threshold", oldest_age >= age_threshold),
            )
            if matched
        ],
    }
    return {
        "schema_version": "audit-capacity-facts.v1",
        "pending": pending,
        "ingest_records_per_second": _current_ingest_records_per_second(
            state,
            written=written,
            observed_at=observed_at,
        ),
        "processing_throughput": dict(throughput) if isinstance(throughput, dict) else {},
        "processing_latency": projected_latency,
        "oldest_pending_age_seconds": round(oldest_age, 3),
        "capacity_alert": capacity_alert,
        "observed_at": observed_at,
    }


def _update_ingest_throughput(state: WatchState, *, now: float) -> None:
    """Advance a short durable accepted-ingest window on complete source reads."""

    current_records = max(
        0,
        int(state.totals.get("spool_candidates", 0) or 0),
    )
    started_at = float(state.ingest_window_started_at or 0.0)
    start_records = max(0, int(state.ingest_window_start_records or 0))
    if started_at <= 0 or now <= started_at or current_records < start_records:
        state.ingest_window_started_at = now
        state.ingest_window_start_records = current_records
        state.ingest_records_per_second = 0.0
        state.ingest_rate_updated_at = now
        return
    elapsed = now - started_at
    if elapsed < _INGEST_RATE_WINDOW_SECONDS:
        return
    state.ingest_records_per_second = round(
        max(0, current_records - start_records) / elapsed,
        6,
    )
    state.ingest_rate_updated_at = now
    state.ingest_window_started_at = now
    state.ingest_window_start_records = current_records


def _current_ingest_records_per_second(
    state: WatchState,
    *,
    written: int,
    observed_at: float,
) -> float:
    """Return current accepted throughput, never a stale lifetime average."""

    if state.closed or float(state.window_finalized_at or 0.0) > 0:
        return 0.0
    updated_at = float(state.ingest_rate_updated_at or 0.0)
    if updated_at > 0:
        if observed_at - updated_at > _INGEST_RATE_STALE_SECONDS:
            return 0.0
        return round(max(0.0, float(state.ingest_records_per_second or 0.0)), 6)
    elapsed = max(0.001, observed_at - float(state.opened_at or observed_at))
    return round(max(0, int(written or 0)) / elapsed, 6)


def _oldest_pending_fetched_at(
    state: WatchState,
    cursor: dict[str, Any],
) -> float:
    """Read only the current inflight or first unread complete spool row."""
    inflight = cursor.get("inflight")
    if isinstance(inflight, dict) and inflight.get("pending_acks"):
        observed = float(inflight.get("oldest_fetched_at") or 0.0)
        if observed > 0:
            return observed
        try:
            records = _scan_spool_range(state, inflight)
        except OSError:
            records = []
        return min(
            (
                float(record.get("fetched_at") or record.get("t") or 0.0)
                for record in records
                if float(record.get("fetched_at") or record.get("t") or 0.0) > 0
            ),
            default=0.0,
        )
    offset = max(0, int(cursor.get("offset") or 0))
    if int(cursor.get("generation") or 0) != state.spool_generation:
        offset = 0
    read_seq = max(0, int(cursor.get("read_seq") or 0))
    try:
        with spool_path(state).open("r", encoding="utf-8") as handle:
            handle.seek(offset)
            while True:
                row, offset, complete, _row_bytes = _next_spool_row(handle, offset)
                if not complete:
                    return 0.0
                if row is None or int(row.get("spool_seq") or 0) <= read_seq:
                    continue
                return float(row.get("fetched_at") or row.get("t") or 0.0)
    except (OSError, TypeError, ValueError):
        return 0.0


# LLM: This is the public, read-only backlog projection used after automatic verdict settlement.
# It must read the same cursor facts as receipt accounting and never infer semantic status.
# 函数用途: 返回当前真正未判完的数量，供工具在自动写账后刷新显示，避免展示旧积压。
def spool_backlog_facts(state: WatchState) -> dict[str, Any]:
    return _backlog_info(state, read_spool_cursor(state))


# LLM: Resolve one owner-scoped logical audit ref against durable spool/archive and verdict ledgers.
# Never search another owner's directory and never mutate retention or cursor state.
# 函数用途: 按 ack_id 找回某一条原始日志及当时的研判记录，用于事后审计和复核。
def inspect_audit_record(state: WatchState, ack_id: str) -> dict[str, Any]:
    target = str(ack_id or "").strip()
    seq_text, separator, index_text = target.partition(":")
    if not separator or not seq_text.isdigit() or not index_text.isdigit():
        return {"ok": False, "error": "ack_id 格式应为 <spool_seq>:<index>"}
    candidate, raw_file, record = _find_audit_candidate(
        state,
        int(seq_text),
        int(index_text),
    )
    verdict_history = _find_audit_verdicts(state, target)
    verdict = verdict_history[-1] if verdict_history else None
    lifecycle = _audit_lifecycle_events(state, target)
    if candidate is None and verdict is None and not lifecycle:
        return {"ok": False, "error": f"没有找到审计记录: {target}"}
    event = candidate.get("event") if isinstance(candidate, dict) else None
    event_hash = str(candidate.get("event_sha256") or "") if isinstance(candidate, dict) else ""
    raw_complete = not (isinstance(event, dict) and "__truncated__" in event)
    payload: dict[str, Any] = {
        "ok": True,
        "owner_id": state.owner_id,
        "audit_id": state.audit_root_task_id,
        "source_id": state.source_id,
        "watch_id": state.watch_id,
        "audit_root_task_id": state.audit_root_task_id,
        "ack_id": target,
        "source_ref": audit_source_ref(state, target),
        "source_url": state.source_url,
        "raw_event": event,
        "raw_complete": raw_complete,
        "stream_pos": (candidate.get("stream_pos") if isinstance(candidate, dict) else None),
        "source_cursor": record.get("cursor_to") if record else None,
        "source_location": {
            "stream_pos": (candidate.get("stream_pos") if isinstance(candidate, dict) else None),
            "cursor": record.get("cursor_to") if record else None,
            "spool_seq": int(record.get("spool_seq") or 0) if record else None,
            "spool_generation": (int(record.get("generation") or 0) if record else None),
        },
        "event_bytes": (candidate.get("event_bytes") if isinstance(candidate, dict) else None),
        "verdict": verdict,
        "verdict_history": verdict_history,
        "processing_status": _audit_processing_status(
            candidate=candidate,
            verdict_history=verdict_history,
            lifecycle=lifecycle,
        ),
    }
    if isinstance(verdict, dict):
        payload["processing_run_id"] = str(
            verdict.get("processing_run_id") or verdict.get("by") or ""
        )
        payload["retry_count"] = int(verdict.get("retry_count") or 0)
    if raw_file:
        payload["raw_record_file"] = raw_file
    observed_at = float(record.get("t") or 0.0) if record else 0.0
    if observed_at:
        payload["observed_at"] = observed_at
        payload["fetched_at"] = float(record.get("fetched_at") or observed_at)
    if event_hash:
        payload["event_sha256"] = event_hash
    return payload


# LLM: Archive and active spool are the one durable raw-event chain; verdict files are separate
# projections. Scanning by encoded sequence keeps lookup exact without another canonical store.
# 函数用途: 在当前 spool 和已轮转归档中定位某个批次下标对应的原始记录。
def _find_audit_candidate(
    state: WatchState,
    spool_seq: int,
    index: int,
) -> tuple[dict[str, Any] | None, str, dict[str, Any]]:
    directory = state_dir(state.owner_home)
    paths = (
        directory / f"{state.watch_id}.archive.ndjson",
        spool_path(state),
    )
    for path in paths:
        record = _audit_spool_record(path, spool_seq)
        if record is None:
            continue
        rows = ensure_ack_ids(record)
        if 0 <= index < len(rows) and isinstance(rows[index], dict):
            return dict(rows[index]), path.name, dict(record)
        return None, path.name, dict(record)
    return None, "", {}


def _find_audit_candidate_by_ack(
    state: WatchState,
    ack_id: str,
) -> tuple[dict[str, Any] | None, str, dict[str, Any]]:
    seq_text, separator, index_text = str(ack_id or "").partition(":")
    if not separator or not seq_text.isdigit() or not index_text.isdigit():
        return None, "", {}
    return _find_audit_candidate(state, int(seq_text), int(index_text))


# LLM: This helper reads one append-only audit file and returns the exact sequence only.
# It does not fall back to fuzzy IDs or cross-owner search.
# 函数用途: 从一个审计队列文件中按精确 spool 序号读取记录，供原文反查主链复用。
def _audit_spool_record(path: Path, spool_seq: int) -> dict[str, Any] | None:
    for line in _audit_spool_lines(path):
        record = _parse_spool_line(line)
        if record is not None and int(record.get("spool_seq") or 0) == spool_seq:
            return record
    return None


def _audit_spool_lines(path: Path):
    """逐行读取可能很大的审计归档；文件不存在时产生空迭代，不把整份归档载入内存。"""
    try:
        handle = path.open("r", encoding="utf-8")
    except OSError:
        return
    with handle:
        yield from handle


# LLM: Verdict ledger is append-only. Return the last matching row so a future explicit
# re-evaluation can supersede an earlier projection without rewriting history.
# 函数用途: 从逐条结论账本中找到指定记录最后一次入账的分数、结论、理由和模型。
def _find_audit_verdict(
    state: WatchState,
    ack_id: str,
) -> dict[str, Any] | None:
    history = _find_audit_verdicts(state, ack_id)
    return history[-1] if history else None


def _find_audit_verdicts(
    state: WatchState,
    ack_id: str,
) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    try:
        with _verdict_ledger_path(state).open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict) and str(row.get("ack_id") or "") == ack_id:
                    found.append(dict(row))
    except OSError:
        return []
    return found


def _audit_lifecycle_path(state: WatchState) -> Path:
    return state_dir(state.owner_home) / f"{state.watch_id}.lifecycle.ndjson"


def _audit_lifecycle_events(
    state: WatchState,
    ack_id: str,
) -> list[dict[str, Any]]:
    report = read_jsonl_objects_report(
        _audit_lifecycle_path(state),
        context="watch_audit.lifecycle.read",
    )
    return [dict(row) for row in report.records if str(row.get("ack_id") or "") == ack_id]


def _audit_processing_status(
    *,
    candidate: dict[str, Any] | None,
    verdict_history: list[dict[str, Any]],
    lifecycle: list[dict[str, Any]],
) -> dict[str, Any]:
    acknowledged_rows = [
        row for row in verdict_history if str(row.get("stage") or "initial") == "initial"
    ]
    review_rows = [row for row in verdict_history if str(row.get("stage") or "") == "review"]
    report_rows = [
        row
        for row in lifecycle
        if str(row.get("event") or "") == "reported"
        and str(row.get("delivery_status") or "") == "sent"
    ]
    phase = "missing"
    if candidate is not None:
        phase = "persisted"
    if acknowledged_rows:
        phase = "judged"
    if review_rows:
        phase = "reviewed"
    if report_rows:
        phase = "reported"
    return {
        "phase": phase,
        "persisted": candidate is not None,
        "acknowledged": bool(acknowledged_rows),
        "reviewed": bool(review_rows),
        "reported": bool(report_rows),
        "review_count": len(review_rows),
        "report_count": len(report_rows),
        "last_reviewed_at": (review_rows[-1].get("t") if review_rows else None),
        "last_reported_at": (report_rows[-1].get("t") if report_rows else None),
        "delivery_receipt_ids": [
            str(row.get("receipt_id") or "")
            for row in report_rows
            if str(row.get("receipt_id") or "")
        ],
    }


def record_audit_delivery_refs(
    owner_home: Path,
    source_refs: list[str] | tuple[str, ...],
    *,
    receipt_id: str,
    channel: str,
    delivered_at: float | None = None,
) -> list[str]:
    """Record only committed, exact owner-local Audit refs as reported.

    External callers invoke this after the shared delivery service returns
    ``sent``; CLI/internal callers invoke it only after the owner-visible local
    transcript append commits.  The function does not infer refs from message
    text and cannot search another owner's directory.
    """
    receipt = str(receipt_id or "").strip()
    if not receipt:
        return []
    current = time.time() if delivered_at is None else float(delivered_at)
    grouped: dict[str, list[tuple[str, str]]] = {}
    for source_ref in dict.fromkeys(str(item or "").strip() for item in source_refs):
        parsed = parse_audit_source_ref(source_ref)
        if parsed is None:
            continue
        watch_id, ack_id = parsed
        grouped.setdefault(watch_id, []).append((ack_id, source_ref))
    recorded: list[str] = []
    for watch_id, refs in grouped.items():
        state = load_state(Path(owner_home), watch_id)
        if state is None or not state.audit_guarantee:
            continue
        valid = _reported_lifecycle_rows(
            state,
            refs,
            receipt=receipt,
            channel=channel,
            delivered_at=current,
        )
        if not valid:
            continue
        if _append_audit_lifecycle_rows(state, valid):
            recorded.extend(str(row["source_ref"]) for row in valid)
    return list(dict.fromkeys(recorded))


def audit_source_refs_reported(
    owner_home: Path,
    source_refs: list[str] | tuple[str, ...],
) -> bool:
    """Return whether every exact owner-local Audit ref has a committed receipt."""

    refs = list(dict.fromkeys(str(item or "").strip() for item in source_refs))
    if not refs:
        return False
    grouped: dict[str, list[str]] = {}
    for source_ref in refs:
        parsed = parse_audit_source_ref(source_ref)
        if parsed is None:
            return False
        watch_id, ack_id = parsed
        grouped.setdefault(watch_id, []).append(ack_id)
    for watch_id, ack_ids in grouped.items():
        state = load_state(Path(owner_home), watch_id)
        if state is None or not state.audit_guarantee:
            return False
        report = read_jsonl_objects_report(
            _audit_lifecycle_path(state),
            context="watch_audit.lifecycle.delivery_status",
        )
        if report.load_errors:
            return False
        delivered = {
            str(row.get("ack_id") or "")
            for row in report.records
            if str(row.get("event") or "") == "reported"
            and str(row.get("delivery_status") or "") == "sent"
            and str(row.get("receipt_id") or "").strip()
        }
        if any(ack_id not in delivered for ack_id in ack_ids):
            return False
    return True


def _reported_lifecycle_rows(
    state: WatchState,
    refs: list[tuple[str, str]],
    *,
    receipt: str,
    channel: str,
    delivered_at: float,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for ack_id, source_ref in refs:
        candidate, _raw_file, _record = _find_audit_candidate_by_ack(state, ack_id)
        if candidate is None:
            continue
        rows.append(
            {
                "event": "reported",
                "delivery_status": "sent",
                "ack_id": ack_id,
                "source_ref": source_ref,
                "receipt_id": receipt,
                "channel": str(channel or ""),
                "t": round(delivered_at, 3),
            }
        )
    return rows


def _append_audit_lifecycle_rows(
    state: WatchState,
    rows: list[dict[str, Any]],
) -> bool:
    """Append new typed lifecycle rows atomically enough for the NDJSON ledger."""
    path = _audit_lifecycle_path(state)
    try:
        with locked_json_path(path):
            existing = read_jsonl_objects_report(
                path,
                context="watch_audit.lifecycle.dedupe",
            ).records
            keys = {
                (
                    str(row.get("event") or ""),
                    str(row.get("ack_id") or ""),
                    str(row.get("receipt_id") or ""),
                )
                for row in existing
            }
            fresh = [
                row
                for row in rows
                if (
                    str(row["event"]),
                    str(row["ack_id"]),
                    str(row["receipt_id"]),
                )
                not in keys
            ]
            if fresh:
                path.parent.mkdir(parents=True, exist_ok=True)
                blob = "".join(
                    json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
                    for row in fresh
                )
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(blob)
                    handle.flush()
                    os.fsync(handle.fileno())
    except OSError:
        _LOGGER.warning(
            "audit lifecycle append failed (watch=%s)",
            state.watch_id,
            exc_info=True,
        )
        return False
    return True


def _redeliver_inflight(
    state: WatchState, cursor: dict, inflight: dict, consumer: str
) -> list[dict]:
    """把前任消费者的在途批原样重投给新消费者:只换在途归属,不动交付游标/确认计数。
    重投可能造成重复判读(前任可能判完了没来得及 ack)——重复上报无害,漏判才是丢。"""
    if int(inflight.get("generation") or 0) != state.spool_generation:
        return []  # 轮转已换代,在途记录不复存在(rotation 有在途不轮转,此为防御残留)
    try:
        records = _scan_spool_range(state, inflight)
    except OSError:
        return []
    if not records:
        return []
    updated = dict(inflight)
    updated["consumer"] = str(consumer or "")
    updated["delivered_at"] = time.time()
    updated["delivery_attempt"] = max(
        1,
        int(inflight.get("delivery_attempt") or 1) + 1,
    )
    _write_spool_cursor(state, {**cursor, "inflight": updated, "updated_at": time.time()})
    return records


def _scan_spool_range(state: WatchState, inflight: dict) -> list[dict]:
    """按在途标记重读 (from_seq, to_seq] 区间的记录(从 from_offset 起顺扫)。"""
    from_seq = int(inflight.get("from_seq") or 0)
    to_seq = int(inflight.get("to_seq") or 0)
    visible_seq = _visible_spool_seq(state)
    if visible_seq is not None:
        to_seq = min(to_seq, visible_seq)
    offset = max(0, int(inflight.get("from_offset") or 0))
    records: list[dict] = []
    with spool_path(state).open("r", encoding="utf-8") as handle:
        handle.seek(offset)
        while True:
            row, offset, complete, _row_bytes = _next_spool_row(handle, offset)
            if not complete:
                break
            if row is None:
                continue
            seq = int(row.get("spool_seq") or 0)
            if seq <= from_seq:
                continue
            if seq > to_seq:
                break
            records.append(row)
    return records


def _scan_spool(
    state: WatchState,
    cursor: dict,
    max_candidates: int,
    *,
    max_bytes: int = 0,
) -> tuple[list[dict], int, int, int]:
    """从读游标偏移顺扫 spool,收集未读记录;世代不符则从头扫(轮转后偏移作废)。
    返回 (records, 扫后偏移, 候选数, 起扫偏移)——起扫偏移供在途标记记录重投起点。"""
    offset = int(cursor.get("offset") or 0)
    if int(cursor.get("generation") or 0) != state.spool_generation:
        offset = 0
    start_offset = offset
    read_seq = int(cursor.get("read_seq") or 0)
    visible_seq = _visible_spool_seq(state)
    with spool_path(state).open("r", encoding="utf-8") as handle:
        handle.seek(offset)
        records, end_offset, taken = _collect_spool_rows(
            handle,
            offset,
            read_seq,
            max_candidates,
            max_bytes=max_bytes,
            max_spool_seq=visible_seq,
            model_visible_bytes=state.audit_guarantee,
        )
    return records, end_offset, taken, start_offset


def _collect_spool_rows(
    handle,
    offset: int,
    read_seq: int,
    max_candidates: int,
    *,
    max_bytes: int = 0,
    max_spool_seq: int | None = None,
    model_visible_bytes: bool = False,
) -> tuple[list[dict], int, int]:
    """顺扫 spool，只在完整来源记录边界组批。

    普通 watch 沿用物理 spool 字节。Audit 的每个物理行本来就是一条完整
    来源记录；它交给模型时会去掉存储元数据并加入 verdict_token，因此 Audit
    的上下文边界按实际模型可见投影计字节，不能让 owner/audit/cursor 等磁盘
    元数据白白吃掉判读口粮。首条记录自身超过额度时仍整条交付，极端单条由
    统一的模型可见投影明确截断，原文继续完整保存在审计账。
    """
    records: list[dict] = []
    taken = 0
    taken_bytes = 0
    while taken < max_candidates:
        row_start = offset
        row, offset, complete, row_bytes = _next_spool_row(handle, offset)
        if not complete:
            break
        if row is None:
            continue
        seq = int(row.get("spool_seq") or 0)
        if max_spool_seq is not None and seq > max_spool_seq:
            return records, row_start, taken
        if seq <= read_seq:
            continue
        row_candidates = len(row.get("candidates") or [])
        if records and taken + row_candidates > max_candidates:
            return records, row_start, taken
        budget_bytes = _audit_model_visible_row_bytes(row) if model_visible_bytes else row_bytes
        if records and max_bytes > 0 and taken_bytes + budget_bytes > max_bytes:
            return records, row_start, taken
        records.append(row)
        taken += row_candidates
        taken_bytes += budget_bytes
    return records, offset, taken


_AUDIT_MODEL_TOKEN_PLACEHOLDER = "vt-" + ("0" * 24)


def _audit_model_visible_row_bytes(record: dict[str, Any]) -> int:
    """Measure the exact ordinary Audit row shape sent to the model.

    Delivery tokens are opaque but fixed-width.  Their value is irrelevant to
    sizing, so a same-width placeholder lets the storage reader account for
    the real protocol without depending on a not-yet-created delivery_ref.
    """
    rows = [
        {
            "verdict_token": _AUDIT_MODEL_TOKEN_PLACEHOLDER,
            **candidate_model_view(row, max_event_tokens=0, include_triage=False),
        }
        for row in ensure_ack_ids(record)
    ]
    return len(json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _next_spool_row(handle, offset: int) -> tuple[dict | None, int, bool, int]:
    """读下一条完整记录行;尾部半行(写入中)视为流末,offset 停在半行前。"""
    line = handle.readline()
    if not line or not line.endswith("\n"):
        return None, offset, False, 0
    return (
        _parse_spool_line(line),
        handle.tell(),
        True,
        len(line.encode("utf-8")),
    )


def _parse_spool_line(line: str) -> dict | None:
    try:
        row = json.loads(line)
    except json.JSONDecodeError:
        return None
    return row if isinstance(row, dict) else None


def _backlog_info(state: WatchState, cursor: dict) -> dict[str, Any]:
    written = int(state.totals.get("spool_candidates", 0) or 0)
    consumed, acked = consumed_and_acked_on_disk(state.owner_home, state.watch_id)
    info = {
        "records_unread": max(0, state.spool_seq - int(cursor.get("read_seq") or 0)),
        "candidates_unread": max(0, written - consumed),
        # 未判完口径(ack):未读 + 在途(交付出去还没被下一次 pull 确认)。唤醒兜底/
        # 收口守卫用这把尺——交付≠判完,消费者死在判读中途的批不能从账上消失。
        "candidates_unjudged": max(0, written - acked),
    }
    gap = int(cursor.get("redelivery_gap_candidates") or 0)
    if gap > 0:
        info["redelivery_gap_candidates"] = gap
    throughput = cursor.get("processing_throughput")
    if isinstance(throughput, dict):
        info["processing_throughput"] = dict(throughput)
    inflight = cursor.get("inflight")
    if state.audit_guarantee and isinstance(inflight, dict):
        delivery_ref = str(inflight.get("delivery_ref") or "").strip()
        if delivery_ref:
            info["delivery_ref"] = delivery_ref
            info["delivered_ack_ids"] = [
                str(value)
                for value in inflight.get("delivered_ack_ids") or []
                if str(value or "").strip()
            ]
    return info


def read_spool_cursor(state: WatchState) -> dict[str, Any]:
    report = read_json_object_report(_read_sidecar_path(state), context="watch_spool.read_cursor")
    return {} if report.load_error is not None else dict(report.payload)


def update_audit_batch_context(
    state: WatchState,
    *,
    consumer: str,
    estimated_input_tokens: int,
    rendered_bytes: int,
    source_authority: dict[str, object] | None = None,
) -> bool:
    """Replace provisional sizing for this watch's current model-facing batch."""
    with _cursor_mutation_fence(
        state,
        source_authority=source_authority,
    ) as gate:
        if not bool(gate.get("ok")):
            return False
        cursor = read_spool_cursor(state)
        inflight = cursor.get("inflight") if isinstance(cursor.get("inflight"), dict) else None
        if (
            not inflight
            or str(inflight.get("consumer") or "") != str(consumer or "")
            or not isinstance(inflight.get("pending_acks"), list)
            or not inflight.get("pending_acks")
        ):
            return False
        updated = {
            **inflight,
            "audit_root_task_id": state.audit_root_task_id,
            "estimated_input_tokens": max(1, int(estimated_input_tokens)),
            "rendered_bytes": max(0, int(rendered_bytes)),
        }
        return _write_spool_cursor(
            state,
            {
                **cursor,
                "inflight": updated,
                "updated_at": time.time(),
            },
        )


def record_audit_batch_failure(
    state: WatchState,
    *,
    reason: str,
) -> bool:
    """Persist a smaller whole-record replay ceiling after a failed model slice.

    The append-only spool and canonical inflight range remain untouched.  A
    successor therefore sees the same unacknowledged records, while
    ``_pending_record_projection`` exposes only a smaller prefix.  Repeating
    one supervisor observation is idempotent by delivery ref and attempt.
    """

    if reason not in {"provider_timeout", "runner_timeout", "slice_rotation"}:
        return False
    with _cursor_mutation_fence(state, source_authority=None) as gate:
        if not bool(gate.get("ok")):
            return False
        cursor = read_spool_cursor(state)
        inflight = cursor.get("inflight") if isinstance(cursor.get("inflight"), dict) else None
        if not inflight or not inflight.get("pending_acks"):
            return False
        pending = {str(value) for value in inflight.get("pending_acks") or [] if str(value)}
        delivered = {str(value) for value in inflight.get("delivered_ack_ids") or [] if str(value)}
        # A complete source record is the smallest legal replay unit.  If the
        # failed view already contains only one record, halving its token ceiling
        # cannot make the next delivery smaller; it merely leaves this source at
        # one model call per record forever.  Preserve the durable record for the
        # normal successor retry and let the existing oversized-record view path
        # handle a genuinely huge single record.
        if len(delivered) <= 1:
            return False
        # A routine bounded slice may end after real partial progress.  That is
        # not evidence that the batch was oversized: the next view already
        # projects only remaining ack ids.  Shrink on slice rotation only when
        # this delivered view settled nothing at all.
        if reason == "slice_rotation" and delivered and delivered != pending:
            return False
        delivery_identity = ":".join(
            (
                str(inflight.get("delivery_ref") or ""),
                str(max(1, int(inflight.get("delivery_attempt") or 1))),
            )
        )
        recovery = (
            dict(cursor.get("batch_recovery") or {})
            if isinstance(cursor.get("batch_recovery"), dict)
            else {}
        )
        if str(recovery.get("last_failed_delivery") or "") == delivery_identity:
            return True
        delivered_tokens = max(
            1,
            int(inflight.get("estimated_input_tokens") or 1),
        )
        previous_ceiling = max(
            0,
            int(recovery.get("max_batch_tokens") or 0),
        )
        failed_ceiling = max(1, delivered_tokens // 2)
        next_ceiling = (
            min(previous_ceiling, failed_ceiling) if previous_ceiling > 0 else failed_ceiling
        )
        recovery.update(
            {
                "schema": _AUDIT_BATCH_RECOVERY_SCHEMA,
                "max_batch_tokens": next_ceiling,
                "last_failed_input_tokens": delivered_tokens,
                "last_failure_reason": reason,
                "last_failed_delivery": delivery_identity,
                "failure_count": max(0, int(recovery.get("failure_count") or 0)) + 1,
                "updated_at": time.time(),
            }
        )
        return _write_spool_cursor(
            state,
            {
                **cursor,
                "batch_recovery": recovery,
                "updated_at": time.time(),
            },
        )


def _write_spool_cursor(state: WatchState, payload: dict[str, Any]) -> bool:
    path = _read_sidecar_path(state)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = _unique_tmp(path)
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
        )
        tmp.replace(path)
    except OSError:
        _LOGGER.warning("spool read-cursor write failed (watch=%s)", state.watch_id, exc_info=True)
        return False
    return True


def _lease_owner() -> str:
    return f"pid:{os.getpid()}"


def _lease_payload(state: WatchState, lease_id: str) -> dict[str, Any]:
    return {
        "owner": _lease_owner(),
        "watch_id": state.watch_id,
        "lease_id": lease_id,
        "heartbeat_at": time.time(),
    }


def _lease_is_fresh(lease: dict[str, Any], *, now: float | None = None) -> bool:
    try:
        heartbeat_at = float(lease.get("heartbeat_at") or 0.0)
    except (TypeError, ValueError):
        return False
    return ((time.time() if now is None else now) - heartbeat_at) <= _LEASE_FRESH_SECONDS


def _read_lease(state: WatchState) -> dict[str, Any] | None:
    report = read_json_object_report(_lease_path(state), context="watch_harvester.lease")
    if report.load_error is not None:
        return None
    lease = dict(report.payload)
    return lease if _lease_is_fresh(lease) else None


def _unreadable_lease_is_fresh(path: Path, *, now: float) -> bool:
    try:
        return (now - path.stat().st_mtime) <= _LEASE_FRESH_SECONDS
    except OSError:
        return False


def _claim_harvester_lease(state: WatchState) -> tuple[bool, dict[str, Any]]:
    """Atomically claim the one physical source-reader slot for a watch.

    The claim is published before ``Thread.start``.  A concurrent caller thus
    observes the fresh claim even during the small interval before the local
    registry contains the new handle.  Fresh malformed metadata is treated as
    occupied: starting a second reader is less safe than briefly waiting for
    the stale window and taking over.
    """
    path = _lease_path(state)
    now = time.time()
    lease_id = uuid4().hex
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with locked_json_path(path):
            report = read_json_object_report(path, context="watch_harvester.lease_claim")
            if report.load_error is None:
                current = dict(report.payload)
                if current and _lease_is_fresh(current, now=now):
                    return False, current
            elif _unreadable_lease_is_fresh(path, now=now):
                return False, {
                    "watch_id": state.watch_id,
                    "error_code": "HARVESTER_LEASE_UNREADABLE",
                    "heartbeat_at": now,
                }
            payload = _lease_payload(state, lease_id)
            write_json_file_atomic_unlocked(path, payload, sort_keys=False)
            return True, payload
    except OSError:
        _LOGGER.warning(
            "harvester lease claim failed (watch=%s)",
            state.watch_id,
            exc_info=True,
        )
        return False, {
            "watch_id": state.watch_id,
            "error_code": "HARVESTER_LEASE_UNAVAILABLE",
            "heartbeat_at": now,
        }


def _write_lease(state: WatchState, lease_id: str) -> bool:
    """Renew only this harvester's lease; never overwrite a successor."""
    path = _lease_path(state)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with locked_json_path(path):
            report = read_json_object_report(path, context="watch_harvester.lease_renew")
            if report.load_error is not None:
                return False
            current = dict(report.payload)
            if (
                str(current.get("owner") or "") != _lease_owner()
                or str(current.get("watch_id") or "") != state.watch_id
                or str(current.get("lease_id") or "") != lease_id
            ):
                return False
            write_json_file_atomic_unlocked(
                path,
                _lease_payload(state, lease_id),
                sort_keys=False,
            )
            return True
    except OSError:
        _LOGGER.debug("harvester lease write failed (watch=%s)", state.watch_id, exc_info=True)
        return False


def _clear_lease(state: WatchState, lease_id: str) -> None:
    """Remove only this harvester's lease; a late old worker cannot clear takeover."""
    path = _lease_path(state)
    try:
        with locked_json_path(path):
            report = read_json_object_report(path, context="watch_harvester.lease_clear")
            if report.load_error is not None:
                return
            current = dict(report.payload)
            if (
                str(current.get("owner") or "") == _lease_owner()
                and str(current.get("watch_id") or "") == state.watch_id
                and str(current.get("lease_id") or "") == lease_id
            ):
                path.unlink(missing_ok=True)
    except OSError:
        pass


def harvester_block(state: WatchState) -> dict[str, Any]:
    """给 pull/status 载荷的收割者健康块(模型据此如实上报源健康/积压)。"""
    handle = harvesters.get_live(state.watch_id)
    lease = _read_lease(state)
    block = {
        "running": handle is not None or lease is not None,
        "mode": "local" if handle is not None else ("remote" if lease is not None else "off"),
        "spool_seq": state.spool_seq,
        "last_error": state.last_error or "",
    }
    # 背压观测口(纯计数,验收/排障可见"抬取在等判读"):被钳过几拍、当前是否触顶。
    skips = int(state.totals.get("backpressure_skips", 0) or 0)
    if skips > 0:
        block["backpressure_skips"] = skips
    disk_skips = int(state.totals.get("disk_backpressure_skips", 0) or 0)
    if disk_skips > 0:
        block["disk_backpressure_skips"] = disk_skips
    if _backpressured(state):
        block["backpressure_active"] = True
    return block


__all__ = [
    "acked_candidates",
    "audit_source_ref",
    "audit_verdict_token",
    "audit_capacity_facts",
    "audit_receipt_facts",
    "audit_source_refs_reported",
    "backpressure_ceiling",
    "bind_current_delivery_verdict_tokens",
    "candidate_ack_id",
    "consumed_and_acked_on_disk",
    "content_batch_size",
    "ensure_ack_ids",
    "ensure_harvester",
    "harvester_block",
    "harvesters",
    "is_content_mode",
    "inspect_audit_record",
    "judge_headroom",
    "judge_quota",
    "overload_threshold",
    "parse_audit_source_ref",
    "read_spool_cursor",
    "read_spool_records",
    "record_audit_batch_failure",
    "record_audit_delivery_refs",
    "spool_path",
    "spool_backlog_facts",
    "spool_unread_bytes",
    "spool_unread",
    "stop_harvester",
    "submit_verdicts",
    "update_audit_batch_context",
    "validate_verdict_evidence_refs",
]
