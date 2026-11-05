"""消息清单是唯一索引根；首拍只记证明，宽限期后重读当前根再清扫。"""

from __future__ import annotations

import json
import math
import stat
from pathlib import Path

from ..common.json_io import locked_json_path
from ..user_space.storage_lanes import (
    STORAGE_LANE_CURSOR_MAX_BYTES,
    StorageLaneRequest,
    lane_cursor_window,
    owner_conversation_roots,
)
from .hamt_reachability import (
    HAMT_MARK_MAX_NODES,
    HAMT_SWEEP_GRACE_SECONDS,
    _bounded_read,
    _BudgetExceeded,
    _Directory,
    _LaneWork,
    _MeasuredIndex,
    _owner_path,
    mark_reachable_nodes,
)
from .message_scan import transcript_manifest

# 同时保留独立宽限证明的线程数上限，避免串行等待七天且保持游标有界。
TRANSCRIPT_INDEX_PENDING_MAX_COUNT = 48


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
    result = mark_reachable_nodes(index, [manifest["index"]], max_nodes=HAMT_MARK_MAX_NODES)
    if not result.complete:
        if not result.dangling:
            raise _BudgetExceeded("transcript HAMT mark node allowance exhausted")
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


# LLM: 首线程额度耗尽只保护并跳过本线程；后续线程普通片用尽则保留续点，下片获得首线程额度。
# 函数用途: 隔离一个线程的失败，保留其他健康线程继续维护的机会。
def _try_thread(work, messages, tid, pending):
    before = work.items
    first = False
    try:
        with work.thread_allowance() as first:
            return "ok", _thread_slice(work, messages, tid, pending)
    except BlockingIOError:
        work.skip("lock_busy")
        return "lock_busy", pending
    except _BudgetExceeded:
        if first:
            work.sweep_state = None
            work.skip("mark_budget_exceeded")
            return "mark_budget_exceeded", None
        return "budget_exhausted", pending
    except FileNotFoundError:
        work.sweep_state = None
        if pending is not None:
            work.skip("pending_root_gone")
            return "ok", None
        work.error("transcript_index_incomplete", "messages/archive/" + tid)
        return "error", None
    except (OSError, ValueError, TypeError, KeyError):
        work.sweep_state = None
        work.error("transcript_index_incomplete", "messages/archive/" + tid)
        return "error", None
    finally:
        if work.items == before:
            work.items += 1


# LLM: 旧单线程字典只迁移形状，不重置 marked_at；root 必须匹配本所有者本次发现的规范消息根。
# 函数用途: 读取有界且按会话根区分身份的待清扫证明。
def _pending_rows(work, cursor, roots):
    home = work.request.home
    root_keys = [str(root.relative_to(home.owner_home_dir)) for root in roots]
    rows = cursor.get("pending", [])
    if isinstance(rows, dict):
        index = cursor["root_index"]
        if index < len(root_keys):
            rows = [{**rows, "root": root_keys[index]}]
        else:
            rows = []
            work.skip("pending_root_gone")
    if not isinstance(rows, list):
        work.skip("pending_invalid")
        return []
    result, seen = [], set()
    for row in rows:
        if not isinstance(row, dict) or len(result) >= TRANSCRIPT_INDEX_PENDING_MAX_COUNT:
            work.skip("pending_invalid")
            continue
        root, tid, started = row.get("root"), row.get("tid"), row.get("marked_at")
        try:
            valid_time = type(started) in (float, int) and math.isfinite(started)
        except OverflowError:
            valid_time = False
        if (
            not isinstance(root, str) or not isinstance(tid, str)
            or not tid or Path(tid).name != tid or tid in {".", ".."}
            or not valid_time
            or not isinstance(row.get("root_sha"), str)
            or type(row.get("candidates")) is not int or row["candidates"] < 1
            or (root, tid) in seen
        ):
            work.skip("pending_invalid")
            continue
        seen.add((root, tid))
        if root not in root_keys:
            work.skip("pending_root_gone")
            continue
        try:
            _owner_path(home, home.owner_home_dir / root / "messages/archive" / tid / "index")
        except FileNotFoundError:
            work.skip("pending_root_gone")
            continue
        except OSError:
            work.error("transcript_index_incomplete", "messages/archive/" + tid)
            continue
        proof = {key: row[key] for key in ("root", "tid", "root_sha", "marked_at", "candidates")}
        if not _append_pending(work, cursor, result, proof):
            work.skip("pending_full")
    return result


# LLM: 数量和最终回执游标字节数同时封顶；无法登记只多保留孤儿，不授予删除权。
# 函数用途: 尝试为另一个线程保存独立的宽限起点。
def _append_pending(work, cursor, pending, row):
    if len(pending) >= TRANSCRIPT_INDEX_PENDING_MAX_COUNT:
        return False
    candidate = {**cursor, "pending": [*pending, row]}
    encoded = json.dumps(work.receipt(candidate, False)["cursor"], separators=(",", ":"), allow_nan=False)
    if len(encoded) > STORAGE_LANE_CURSOR_MAX_BYTES - 1024:
        return False
    pending.append(row)
    return True


# LLM: pending 独立计时，等待项不占据发现续点；轮次完成与宽限/逐线程错误分开表达。
# 函数用途: 对本所有者各规范会话根执行一个可续跑的消息索引维护片。
def maintain_transcript_index_lane(request: StorageLaneRequest) -> dict:
    work = _LaneWork(request)
    cursor = {
        "root_index": max(0, int(request.cursor.get("root_index", 0))),
        "after": str(request.cursor.get("after", "")),
    }
    if request.legal_hold:
        return work.receipt(request.cursor, True, "legal_hold")
    # 原字典也在零预算时保留，不能因一次只读空片遗失已等待的宽限证明。
    if "pending" in request.cursor:
        cursor["pending"] = request.cursor["pending"]
    if request.budget.exhausted():
        return work.receipt(cursor, False, "budget_exhausted")
    try:
        roots = owner_conversation_roots(request.home)
        pending = _pending_rows(work, cursor, roots)
        cursor["pending"] = pending
        messages_by_root = {
            str(root.relative_to(request.home.owner_home_dir)): root / "messages"
            for root in roots
        }
        processed = set()
        resume = work.sweep_state and work.sweep_state["namespace"]
        ordered = sorted(pending, key=lambda row: (
            str(Path(row["root"]) / "messages/archive" / row["tid"] / "index") != resume
        ))
        phase_two_ran = False
        for row in ordered:
            if request.now < row["marked_at"] + HAMT_SWEEP_GRACE_SECONDS:
                work.skip("pending_grace")
                continue
            if phase_two_ran:
                continue
            if work._allowance_used and work.exhausted():
                return work.receipt(cursor, False, "budget_exhausted")
            phase_two_ran = True
            messages = messages_by_root[row["root"]]
            status, replacement = _try_thread(work, messages, row["tid"], row)
            if status in {"lock_busy", "budget_exhausted"}:
                return work.receipt(cursor, False, status)
            processed.add((row["root"], row["tid"]))
            if replacement is None:
                pending.remove(row)
            elif work.sweep_state is not None:
                return work.receipt(cursor, False, "budget_exhausted")
        while cursor["root_index"] < len(roots):
            conversation = roots[cursor["root_index"]]
            try:
                messages = _owner_path(request.home, conversation / "messages")
            except FileNotFoundError:
                cursor.update(root_index=cursor["root_index"] + 1, after="")
                continue
            except OSError:
                work.error("transcript_index_incomplete", "messages")
                work.sweep_state = None
                cursor.update(root_index=cursor["root_index"] + 1, after="")
                continue
            root_key = str(conversation.relative_to(request.home.owner_home_dir))
            known = processed | {(row["root"], row["tid"]) for row in pending}
            try:
                names = lane_cursor_window(
                    cursor,
                    (tid for tid in _thread_names(request.home, messages) if (root_key, tid) not in known),
                    work.budget.max_items,
                )
            except FileNotFoundError:
                names = []
            for tid in names:
                if work._allowance_used and work.exhausted():
                    return work.receipt(cursor, False, "budget_exhausted")
                if len(pending) >= TRANSCRIPT_INDEX_PENDING_MAX_COUNT:
                    if work.exhausted():
                        return work.receipt(cursor, False, "budget_exhausted")
                    work.skip("pending_full")
                    work.items += 1
                    cursor["after"] = tid
                    continue
                status, replacement = _try_thread(work, messages, tid, None)
                if status in {"lock_busy", "budget_exhausted"}:
                    return work.receipt(cursor, False, status)
                if replacement is not None:
                    if not _append_pending(work, cursor, pending, {**replacement, "root": root_key}):
                        work.skip("pending_full")
                if work.sweep_state is not None:
                    return work.receipt(cursor, False, "budget_exhausted")
                cursor["after"] = tid
            # 满窗口只能证明这一页已处理，不能清空 after 并宣称整个根已经追平。
            if len(names) >= work.budget.max_items:
                return work.receipt(cursor, False, "budget_exhausted")
            cursor.update(root_index=cursor["root_index"] + 1, after="")
        cursor.update(root_index=0, after="")
        if not pending:
            cursor.pop("pending", None)
        return work.receipt(
            cursor, True, "reachability_incomplete" if work.errors else ""
        )
    except BlockingIOError:
        work.skip("lock_busy")
        return work.receipt(cursor, False, "lock_busy")
    except (OSError, ValueError, TypeError, KeyError):
        if not isinstance(cursor.get("pending"), list):
            cursor.pop("pending", None)
        work.error("transcript_index_incomplete", "messages/archive")
        return work.receipt(cursor, False, "reachability_incomplete")
