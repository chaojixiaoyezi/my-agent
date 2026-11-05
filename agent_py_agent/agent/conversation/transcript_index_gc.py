"""消息清单是唯一索引根；首拍只记证明，宽限期后重读当前根再清扫。"""

from __future__ import annotations

import json
import math
import stat
from pathlib import Path

from ..common.json_io import locked_json_path
from ..user_space.storage_lanes import (
    StorageLaneRequest,
    lane_cursor_window,
    owner_conversation_roots,
)
from .hamt_reachability import (
    HAMT_SWEEP_GRACE_SECONDS,
    HAMT_SWEEP_SLICE_NODES,
    _bounded_read,
    _BudgetExceeded,
    _Directory,
    _LaneWork,
    _MeasuredIndex,
    _owner_path,
    mark_reachable_nodes,
)
from .message_scan import transcript_manifest


# LLM: 只按真实目录流选有界窗口；目录名本身不构成清扫许可。
# 函数用途: 列出一个规范消息根中拥有索引目录的线程名字。
def _thread_names(home, messages):
    archive = _owner_path(home, messages / "archive")
    with _Directory(archive) as directory:
        for name in directory.names():
            if not stat.S_ISDIR(directory.stamp(name).st_mode):
                continue
            try:
                _owner_path(home, archive / name / "index")
            except FileNotFoundError:
                continue
            except OSError:
                # 交给线程边界报告坏目录，不能让它终止整个名称流。
                yield name
                continue
            yield name


# LLM: 必须持有与消息写者完全相同的 <tid>.jsonl 锁；只增加计量，不更改 manifest 解析规则。
# 函数用途: 验证当前规范消息清单及其全部索引节点。
def _mark_current(work, messages, tid):
    manifest_path = messages / (tid + ".manifest.json")
    raw = _bounded_read(work, messages, (manifest_path.name,))
    if json.loads(raw).get("phase") == "prepared":
        raise OSError("transcript rotation is still in flight")
    work.read(len(raw))
    manifest = transcript_manifest(messages / (tid + ".jsonl"))
    if manifest is None:
        raise OSError("transcript manifest is unavailable")
    index = _MeasuredIndex(messages, ("archive", tid, "index"), work)
    result = mark_reachable_nodes(index, [manifest["index"]], max_nodes=HAMT_SWEEP_SLICE_NODES)
    if not result.complete:
        raise OSError("transcript index mark is incomplete")
    return result, manifest["index"]["ref"]["sha256"] if manifest["index"] is not None else ""


# LLM: pending 不存节点列表；第二拍只能在原 marked_at 的宽限期结束后执行，且总是重标当前根。
# 函数用途: 准备或消费一个线程的两阶段索引清扫证明。
def _thread_slice(work, messages, tid, pending):
    namespace = _owner_path(work.request.home, messages / "archive" / tid / "index")
    result, digest = _mark_current(work, messages, tid)
    started = work.request.now
    if pending is not None:
        started = pending.get("marked_at")
        if type(started) not in (float, int) or not math.isfinite(started):
            raise OSError("invalid transcript mark time")
        if work.request.now < started + HAMT_SWEEP_GRACE_SECONDS:
            return pending
    if not work.preview(namespace, result.reachable, started):
        return pending if work.sweep_state is not None else None
    with locked_json_path(
        messages / (tid + ".jsonl"), blocking=False, birth_commit=work.request.run_write
    ):
        _owner_path(work.request.home, namespace)
        result, digest = _mark_current(work, messages, tid)
        if pending is None:
            work.sweep_state = None
            return {"tid": tid, "root_sha": digest, "marked_at": started, "candidates": 1}
        complete = work.sweep(namespace, result.reachable, started)
        return None if complete else pending


# LLM: 坏线程只拒绝自身删除；计量不重复收取入场额度，单条预算也必须能做实际工作。
# 函数用途: 隔离一个线程的失败，保留其他健康线程继续维护的机会。
def _try_thread(work, messages, tid, pending):
    before = work.items
    try:
        return "ok", _thread_slice(work, messages, tid, pending)
    except BlockingIOError:
        work.skip("lock_busy")
        return "lock_busy", pending
    except _BudgetExceeded:
        return "budget_exhausted", pending
    except (OSError, ValueError, TypeError, KeyError):
        work.sweep_state = None
        work.error("transcript_index_incomplete", "messages/archive/" + tid)
        return "error", None
    finally:
        if work.items == before and not work.exhausted():
            work.items += 1


# LLM: legal hold 与无索引 owner 零写门；消息正文、分段和 manifest 全部只读。
# 函数用途: 对本所有者各规范会话根执行一个可续跑的消息索引维护片。
def maintain_transcript_index_lane(request: StorageLaneRequest) -> dict:
    work = _LaneWork(request)
    cursor = {
        "root_index": max(0, int(request.cursor.get("root_index", 0))),
        "after": str(request.cursor.get("after", "")),
    }
    pending = request.cursor.get("pending")
    if isinstance(pending, dict):
        cursor["pending"] = dict(pending)
    if request.legal_hold:
        return work.receipt(request.cursor, True, "legal_hold")
    if work.exhausted():
        return work.receipt(cursor, False, "budget_exhausted")
    try:
        roots = owner_conversation_roots(request.home)
        while cursor["root_index"] < len(roots):
            conversation = roots[cursor["root_index"]]
            try:
                messages = _owner_path(request.home, conversation / "messages")
            except FileNotFoundError:
                cursor.update(root_index=cursor["root_index"] + 1, after="")
                continue
            except OSError:
                work.error("transcript_index_incomplete", "messages")
                cursor.pop("pending", None)
                work.sweep_state = None
                cursor.update(root_index=cursor["root_index"] + 1, after="")
                continue
            pending = cursor.get("pending")
            if pending is not None:
                tid = pending.get("tid", "")
                if not tid or Path(tid).name != tid or tid in {".", ".."}:
                    raise OSError("invalid transcript pending thread")
                status, replacement = _try_thread(work, messages, tid, pending)
                if status in {"lock_busy", "budget_exhausted"}:
                    return work.receipt(cursor, False, status)
                if replacement is not None:
                    cursor["pending"] = replacement
                    return work.receipt(
                        cursor, False, "budget_exhausted" if work.exhausted() else "grace_period"
                    )
                del cursor["pending"]
                cursor["after"] = tid
            try:
                names = lane_cursor_window(
                    cursor, _thread_names(request.home, messages), work.budget.max_items
                )
            except FileNotFoundError:
                names = []
            for tid in names:
                if work.exhausted():
                    return work.receipt(cursor, False, "budget_exhausted")
                status, replacement = _try_thread(work, messages, tid, None)
                if status in {"lock_busy", "budget_exhausted"}:
                    return work.receipt(cursor, False, status)
                if replacement is not None:
                    cursor["pending"] = replacement
                    return work.receipt(cursor, False, "grace_period")
                if work.sweep_state is not None:
                    return work.receipt(cursor, False, "budget_exhausted")
                cursor["after"] = tid
            # 满窗口只能证明这一页已处理，不能清空 after 并宣称整个根已经追平。
            if len(names) >= work.budget.max_items:
                return work.receipt(cursor, False, "budget_exhausted")
            cursor.update(root_index=cursor["root_index"] + 1, after="")
        cursor.update(root_index=0, after="")
        return work.receipt(
            cursor, not work.errors, "reachability_incomplete" if work.errors else ""
        )
    except BlockingIOError:
        work.skip("lock_busy")
        return work.receipt(cursor, False, "lock_busy")
    except (OSError, ValueError, TypeError, KeyError):
        work.error("transcript_index_incomplete", "messages/archive")
        return work.receipt(cursor, False, "reachability_incomplete")
