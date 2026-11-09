"""沿现有提交链标记 compact 证据；退休台账仅提供旁树删除的正面证明。"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from ..common.json_io import locked_json_path, read_json_object_report
from ..common.nofollow_fs import open_directory_beneath
from ..memory_store.retired_thread_journal import (
    RetiredThreadPage,
    iter_retired_threads,
    retired_thread_state,
)
from ..user_space.storage_lanes import (
    StorageLaneRequest,
    lane_cursor_window,
    owner_conversation_roots,
)
from .compact_checkpoint import _read_cold_payload, iter_committed_compact_checkpoints
from .hamt_reachability import (
    HAMT_MARK_MAX_NODES,
    _bounded_read,
    _BudgetExceeded,
    _Directory,
    _LaneWork,
    _MeasuredIndex,
    _owner_path,
    _regular,
    _same_seal,
    mark_reachable_nodes,
)
from .models import ConversationThread


# LLM: 不拿孤立 ledger 猜线程状态；任何不确定或多根重复记录均保留整棵树。
# 函数用途: 从当前所有者规范会话根取得一个可验证的原线程记录。
def _thread_record(work, tid):
    home = work.request.home
    found = None
    for conversation in owner_conversation_roots(home):
        try:
            directory = _owner_path(home, conversation / "threads")
            with _Directory(directory) as opened:
                info = opened.stamp(tid + ".json")
                if not _regular(info):
                    raise OSError("untrusted thread record")
            path = directory / (tid + ".json")
            raw = _bounded_read(
                work, home.owner_home_dir, path.relative_to(home.owner_home_dir).parts
            )
            report = read_json_object_report(_JsonSnapshot(path, raw))
            if report.load_error is not None or found is not None:
                raise OSError("unreadable or ambiguous thread record")
            found = ConversationThread.from_dict(report.payload)
            if found.thread_id != tid:
                raise OSError("thread identity mismatch")
        except FileNotFoundError:
            continue
    return found


@dataclass(frozen=True)
class _JsonSnapshot:
    """给原 report 解析器提供已计量的只读字节，不再次按路径打开可能变大的文件。"""

    path: Path
    raw: bytes

    # LLM: 快照来自成功的有界 no-follow 读取，不再产生文件 I/O。
    # 函数用途: 告诉原报告解析器这个快照确实存在。
    def exists(self):
        return True

    # LLM: 只解码已固定的字节，原 JSON 解析和错误报告仍由 read_json_object_report 负责。
    # 函数用途: 把线程记录快照交给原解析器。
    def read_text(self, encoding="utf-8"):
        return self.raw.decode(encoding)

    # LLM: 报告路径只指向原候选，不持久化任何新状态。
    # 函数用途: 在原报告里保留线程记录的真实文件身份。
    def __str__(self):
        return str(self.path)


# LLM: 同名引用只允许完整封印相同，冲突在去重前拒绝，不能覆盖掉坏根。
# 函数用途: 去重保存一个已声明的根引用。
def _retain(target, name, reference):
    if name in target and not _same_seal(target[name], reference):
        raise OSError("conflicting seals for one immutable object")
    target[name] = reference


# LLM: 热行也是候选根持有者；完整链枚举结束前不发出任何清扫写门。
# 函数用途: 收齐所有代次的索引、时间轴、冷载荷和不可变副本名字。
def _collect_roots(work, root, thread):
    tid = thread.thread_id
    raw = _bounded_read(work, root, ("conversations", tid + ".jsonl"))
    hot = []
    for line in raw.splitlines():
        if line.strip():
            row = json.loads(line)
            if not isinstance(row, dict) or row.get("thread_id") != tid:
                raise OSError("invalid compact ledger row")
            hot.append(row)
    indexes, timelines, payloads, copies = {}, {}, {}, set()

    def collect(row):
        view = row.get("carried_view", {})
        if not isinstance(view, dict):
            raise OSError("invalid carried view")
        for name in (
            "messages",
            "tools",
            "scope_heads",
            "readable_bases",
            "checkpoint_lineage",
            "thread_timeline",
        ):
            reference = view.get(name)
            if reference is not None:
                target = timelines if name == "thread_timeline" else indexes
                _retain(target, reference["ref"]["ref"], reference)
        for name in ("operation_evidence_ref", "provider_compaction_ref"):
            reference = row.get(name)
            if reference is not None:
                _retain(payloads, reference["ref"], reference)
        identity = row.get("checkpoint_id")
        if not isinstance(identity, str) or not identity:
            raise OSError("invalid checkpoint identity")
        copies.add(hashlib.sha256(identity.encode("utf-8")).hexdigest() + ".checkpoint.json")

    for row in hot:
        collect(row)
    # 原链读取器仍是验证权威。先有界读取每个会用到的冷副本，并为其后原读取器的真实字节留足额度。
    work.read(len(raw))
    hot_by_id = {row.get("checkpoint_id"): row for row in hot}
    current, seen = thread.compact_checkpoint_id, set()
    while current:
        if current in seen:
            raise OSError("cyclic checkpoint read preflight")
        seen.add(current)
        row = hot_by_id.get(current)
        if row is None:
            name = hashlib.sha256(current.encode("utf-8")).hexdigest() + ".checkpoint.json"
            cold = _bounded_read(work, root, ("archive", tid, name))
            work.read(len(cold))
            row = json.loads(cold)
        if not isinstance(row, dict) or not isinstance(row.get("previous_checkpoint_id", ""), str):
            raise OSError("invalid checkpoint read preflight")
        current = row.get("previous_checkpoint_id", "")
    agent = SimpleNamespace(home_paths=work.request.home)
    for row in iter_committed_compact_checkpoints(agent, thread, hydrate=False):
        collect(row)
    for reference in payloads.values():
        work.read(reference["byte_length"])
        _read_cold_payload(root, tid, reference)
    marked = []
    for namespace, roots in (("indexes", indexes), ("timeline", timelines)):
        index = _MeasuredIndex(root, ("evidence", tid, namespace), work)
        result = mark_reachable_nodes(index, roots.values(), max_nodes=HAMT_MARK_MAX_NODES)
        if not result.complete:
            if not result.dangling:
                raise _BudgetExceeded("compact HAMT mark node allowance exhausted")
            raise OSError("compact HAMT mark is incomplete")
        marked.append(result.reachable)
    return (*marked, frozenset(payloads), frozenset(copies))


# LLM: 原账本锁保持到清扫完成；活树绝不删除 ledger、meta 或 gen 冷段。
# 函数用途: 在全部根验证成功后清理一个活线程的四个不可变命名空间。
def _sweep_spaces(work, root, tid, marks, *, preview):
    indexes, timeline, payloads, copies = marks
    spaces = (
        (("evidence", tid, "indexes"), indexes),
        (("evidence", tid, "timeline"), timeline),
        (("evidence", tid), payloads),
        (("archive", tid), copies),
    )
    resume = work.sweep_state and work.sweep_state["namespace"]
    allowed = {
        str(root.joinpath(*parts).relative_to(work.request.home.owner_home_dir))
        for parts, _ in spaces
    }
    if resume not in allowed:
        resume = None
        work.sweep_state = None
    for parts, reachable in spaces:
        key = str(root.joinpath(*parts).relative_to(work.request.home.owner_home_dir))
        if resume and key != resume:
            continue
        resume = None
        try:
            path = _owner_path(work.request.home, root.joinpath(*parts))
        except FileNotFoundError:
            work.sweep_state = None
            continue
        suffix = ".checkpoint.json" if parts[0] == "archive" else ".json"
        if preview:
            if work.preview(path, reachable, work.request.now, suffix=suffix):
                return "ready"
            if work.sweep_state is not None:
                return "pending"
        elif not work.sweep(path, reachable, work.request.now, suffix=suffix):
            return "pending"
    return "complete"


# LLM: 锁出生前必须既有完整根又有到期叶；锁内重收根，不能用无锁观察授权删除。
# 函数用途: 无变更零写门，有变更才在原账本锁下清扫活线程。
def _live_thread(work, root, tid, thread):
    from .compact_index_lifecycle import maintain_index_store
    from .immutable_node_store import ImmutableNodeStore

    stores = [ImmutableNodeStore(root, ('evidence', tid, name)) for name in ('indexes', 'timeline')]
    indexed = any(store.exists() for store in stores)
    if not indexed and thread.compact_checkpoint_id:
        from .compact_checkpoint import load_latest_committed_compact_checkpoint

        head = load_latest_committed_compact_checkpoint(SimpleNamespace(home_paths=work.request.home), thread)
        indexed = head is not None and 'carried_view' in head
    if indexed:
        from .compact_index_lifecycle import index_store_due

        if not index_store_due(work, root, thread, stores):
            return True
        ledger = root / 'conversations' / (tid + '.jsonl')
        with locked_json_path(ledger, blocking=False, birth_commit=work.request.run_write):
            current = _thread_record(work, tid)
            if current is None:
                raise OSError('thread record disappeared')
            return maintain_index_store(work, root, current)
    marks = _collect_roots(work, root, thread)
    stage = _sweep_spaces(work, root, tid, marks, preview=True)
    if stage != "ready":
        return stage == "complete"
    ledger = root / "conversations" / (tid + ".jsonl")
    with locked_json_path(ledger, blocking=False, birth_commit=work.request.run_write):
        current = _thread_record(work, tid)
        if current is None:
            raise OSError("thread record disappeared")
        marks = _collect_roots(work, root, current)
        return _sweep_spaces(work, root, tid, marks, preview=False) == "complete"


# LLM: 无台账旧树保守计数；台账不完整时不能把未见行当成从未退休。
# 函数用途: 给未登记且无线程记录的旧旁树增加可见欠账。
def _count_untracked(work, root, tid):
    offset = 0
    for _ in range(work.budget.max_items):
        try:
            page = _journal_page(work, offset)
        except _BudgetExceeded:
            work.skip("untracked_inventory_incomplete")
            return
        if any(row.get("thread_id") == tid for row in page.rows):
            return
        if page.complete:
            break
        if page.next_offset <= offset:
            return
        offset = page.next_offset
    else:
        work.skip("untracked_inventory_incomplete")
        return
    for name in ("evidence", "archive"):
        try:
            _owner_path(work.request.home, root / name / tid)
        except FileNotFoundError:
            continue
        work.skip("untracked_trees")
        return


# LLM: 原分页器读取前用剩余预算探测物理行；原台账只追加，分页器仍负责协议解析。
# 函数用途: 按物理字节读取一个退休记录，不能丢掉空白字节的计量。
def _journal_page(work, offset):
    home = work.request.home
    path = Path(home.owner_data_dir) / "retention/retired_threads.jsonl"
    try:
        raw = _bounded_read(
            work,
            home.owner_home_dir,
            path.relative_to(home.owner_home_dir).parts,
            offset=offset,
            line=True,
            max_bytes=max(0, (work.available_bytes() - 1) // 2),
        )
    except FileNotFoundError:
        return RetiredThreadPage((), offset, True)
    if not raw:
        return RetiredThreadPage((), offset, True)
    extra = int(raw.endswith(b"\n"))
    work.read(len(raw) + extra)
    page = iter_retired_threads(home, offset=offset, max_rows=1)
    if raw.endswith(b"\n") and page.next_offset != offset + len(raw):
        raise OSError("retired journal changed during bounded read")
    if extra and page.complete:
        work.size -= 1
    return page


# LLM: 只读原台账的单链接元数据，不跟随父链及叶链接。
# 函数用途: 固定退休代次证明所依据的原台账水位。
def _journal_stamp(home):
    parent = _owner_path(home, Path(home.owner_data_dir) / "retention")
    with _Directory(parent) as directory:
        info = directory.stamp("retired_threads.jsonl")
        if not _regular(info):
            raise OSError("untrusted retired journal")
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


# LLM: 旧 action 不得覆盖后续退休代次；未读完台账尾部只保存标量续点，不能开始删除。
# 函数用途: 查明同线程是否再次退休，返回可在写门内复核的水位。
def _latest_retirement(work, row, next_offset, cursor):
    stamp = _journal_stamp(work.request.home)
    digest = hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()
    probe = work.retired_probe
    valid = (
        isinstance(probe, dict)
        and probe.get("row") == cursor["journal_offset"]
        and probe.get("sha") == digest
        and probe.get("inode") == list(stamp[:2])
        and type(probe.get("after")) is int
        and next_offset <= probe["after"] <= stamp[2]
    )
    offset = probe["after"] if valid else next_offset
    while offset < stamp[2]:
        work.retired_probe = {
            "row": cursor["journal_offset"],
            "sha": digest,
            "inode": list(stamp[:2]),
            "after": offset,
        }
        if work.exhausted():
            return "pending", None
        page = _journal_page(work, offset)
        work.items += 1
        if not page.rows or page.next_offset <= offset:
            return ("undecidable" if page.next_offset > offset else "unknown"), None
        if any(later.get("thread_id") == row["thread_id"] for later in page.rows):
            work.retired_probe = None
            return "superseded", None
        offset = page.next_offset
    latest = _journal_stamp(work.request.home)
    work.retired_probe = {
        "row": cursor["journal_offset"],
        "sha": digest,
        "inode": list(stamp[:2]),
        "after": offset,
    }
    if latest != stamp:
        return "pending", None
    return "current", stamp


# LLM: 名称流的窗口内存有界；合法线程名仍须通过原线程记录和锁复核。
# 函数用途: 只流式发现本所有者的常规 compact 热账本名字。
def _live_names(home, directory):
    _owner_path(home, directory)
    with _Directory(directory) as opened:
        for name in opened.names():
            if name.endswith(".jsonl") and _regular(opened.stamp(name)):
                yield name


# LLM: 最多合并三个有界名称窗口，旧 evidence/archive 没有 ledger 也不会漏记为无人维护。
# 函数用途: 合并热账本线程和无账本旧树的候选线程窗口。
def _live_window(home, root, cursor, limit):
    names = set()
    try:
        names.update(lane_cursor_window(cursor, _live_names(home, root / "conversations"), limit))
    except FileNotFoundError:
        pass  # best-effort: 缺失热目录仍须发现旧旁树，不授予删除权。
    for name in ("evidence", "archive"):
        try:
            path = _owner_path(home, root / name)
            with _Directory(path) as directory:
                candidates = (
                    name + ".jsonl"
                    for name in directory.names()
                    if stat.S_ISDIR(directory.stamp(name).st_mode)
                )
                names.update(lane_cursor_window(cursor, candidates, limit))
        except FileNotFoundError:
            continue
    return sorted(names)[:limit]


# LLM: 仅在正面退休证明下使用；逐层流式展开，拒绝所有链接和异常叶，扫描不在写门内。
# 函数用途: 后序枚举一个退休旁树中的常规文件和空目录候选。
def _retired_tree_entries(path, depth=0):
    if depth > 32:
        raise OSError("retired compact tree is too deep")
    try:
        with _Directory(path) as directory:
            for name in directory.names():
                info = directory.stamp(name)
                child = path / name
                if stat.S_ISDIR(info.st_mode):
                    yield from _retired_tree_entries(child, depth + 1)
                elif _regular(info):
                    if not name.endswith('.lock'):
                        yield child, info, False
                else:
                    raise OSError("untrusted retired compact leaf")
        # generation 锁锚永不换 inode；仅剩锁锚的目录也必须保留。
        with _Directory(path) as remaining:
            if any(remaining.names()):
                return
        with _Directory(path.parent) as parent:
            yield path, parent.stamp(path.name), True
    except FileNotFoundError:
        return


# LLM: 同一写门内复核四态、所有会话根和候选 inode；不递归，不跟随叶链接。
# 函数用途: 删除一个仍由退休证明覆盖的文件或空目录。
def _delete_retired_entry(work, row, path, initial, directory, proof):
    home = work.request.home
    if (
        work.exhausted()
        or _journal_stamp(home) != proof
        or retired_thread_state(home, row) != "purged"
    ):
        return False
    if _thread_record(work, row["thread_id"]) is not None:
        return False
    with _Directory(path.parent) as parent:
        try:
            current = parent.stamp(path.name)
        except FileNotFoundError:
            return True
        if (current.st_dev, current.st_ino) != (initial.st_dev, initial.st_ino):
            return False
        if directory:
            if not stat.S_ISDIR(current.st_mode):
                return False
            if parent.fd is not None:
                os.rmdir(path.name, dir_fd=parent.fd)
            else:
                path.rmdir()
        else:
            if not _regular(current):
                return False
            if path.name.startswith('.nodes.sqlite3') and not path.name.endswith('.lock'):
                from ..runtime_db.recovery_guard import runtime_db_generation_guard

                database = path.with_name('.nodes.sqlite3')
                with runtime_db_generation_guard(database, exclusive=True):
                    parent.unlink(path.name)
            else:
                parent.unlink(path.name)
        work.deleted += 1
        work.freed += 0 if directory else current.st_size
    return True


# LLM: 预算中断保留当前台账字节位置，下片重扫已变小的同一棵树；原锁叶永不删除。
# 函数用途: 在原 compact ledger 锁下分批清除一个已清空回收站的退休线程旁树。
def _purge_retired(work, root, row, proof):
    tid = row["thread_id"]
    if _thread_record(work, tid) is not None:
        return False
    ledger = root / "conversations" / (tid + ".jsonl")
    objects = False
    for name in ("evidence", "archive"):
        try:
            _owner_path(work.request.home, root / name / tid)
        except FileNotFoundError:
            continue
        objects = True
    try:
        with _Directory(_owner_path(work.request.home, ledger.parent)) as directory:
            for name in (tid + ".jsonl", tid + ".meta.json", tid + ".jsonl.meta.json"):
                try:
                    info = directory.stamp(name)
                except FileNotFoundError:
                    continue
                if not _regular(info):
                    raise OSError("untrusted retired compact metadata")
                objects = True
    except FileNotFoundError:
        pass  # best-effort: 无账本目录仍继续检查已确认存在的退休旁树。
    if not objects:
        return True
    # 不因一条过期台账就新建 conversations 目录或空锁。
    try:
        _owner_path(work.request.home, ledger.parent)
    except FileNotFoundError:
        has_tree = False
        for name in ("evidence", "archive"):
            try:
                _owner_path(work.request.home, root / name / tid)
            except FileNotFoundError:
                continue
            has_tree = True
        if not has_tree:
            return True

        def birth():
            # 原目录已消失但退休旁树仍在时，只在正面证明仍成立的写门内恢复规范锁目录。
            if (
                _journal_stamp(work.request.home) != proof
                or retired_thread_state(work.request.home, row) != "purged"
                or _thread_record(work, tid) is not None
            ):
                return False
            parts = ledger.parent.relative_to(work.request.home.owner_home_dir).parts
            descriptor = open_directory_beneath(
                work.request.home.owner_home_dir, parts, create=True
            )
            os.close(descriptor)
            return True

        if not work.request.run_write(birth):
            return False
    with locked_json_path(ledger, blocking=False, birth_commit=work.request.run_write):
        for name in ("evidence", "archive"):
            path = root / name / tid
            try:
                _owner_path(work.request.home, path)
            except FileNotFoundError:
                continue
            for target, info, directory in _retired_tree_entries(path):
                if work.exhausted():
                    return False
                if not work.request.run_write(
                    lambda: _delete_retired_entry(work, row, target, info, directory, proof)
                ):
                    return False
                work.items += 1
        for name in (tid + ".meta.json", tid + ".jsonl.meta.json", tid + ".jsonl"):
            with _Directory(ledger.parent) as parent:
                try:
                    info = parent.stamp(name)
                except FileNotFoundError:
                    continue
            if work.exhausted() or not _regular(info):
                return False
            target = ledger.parent / name
            if not work.request.run_write(
                lambda: _delete_retired_entry(work, row, target, info, False, proof)
            ):
                return False
            work.items += 1
    return True


# LLM: 等待/未知行结束本轮且保留精确字节位置；complete 只表示本轮结束，不授予清扫权。
# 函数用途: 消费一个受预算限制的退休证明窗口。
def _retired_slice(work, root, cursor):
    while not work.exhausted():
        page = _journal_page(work, cursor["journal_offset"])
        if not page.rows:
            if page.complete and page.next_offset == cursor["journal_offset"]:
                return True, ""
            if page.next_offset > cursor["journal_offset"]:
                work.skip("retired_thread_malformed")
                cursor["journal_offset"] = page.next_offset
                continue
            work.skip("retired_thread_unknown")
            work.error("retired_thread_state_unknown", "data/retention/retired_threads.jsonl")
            cursor.update(phase="live", after="")
            return True, "retired_thread_unknown"
        row = page.rows[0]
        state = retired_thread_state(work.request.home, row)
        if state in {"waiting", "unknown"}:
            work.skip("retired_thread_" + state)
            if state == "unknown":
                work.error("retired_thread_state_unknown", "data/retention/retired_threads.jsonl")
            cursor.update(phase="live", after="")
            return True, "retired_thread_" + state
        if state == "restored":
            work.skip("restored")
            work.items += 1
        else:
            latest, proof = _latest_retirement(work, row, page.next_offset, cursor)
            if latest == "pending":
                return False, "budget_exhausted" if work.exhausted() else "retired_journal_changed"
            if latest == "undecidable":
                work.skip("retired_thread_undecidable")
                work.error("retired_thread_state_unknown", "data/retention/retired_threads.jsonl")
                cursor["journal_offset"] = page.next_offset
                work.retired_probe = None
                continue
            if latest == "unknown":
                work.skip("retired_thread_unknown")
                work.error("retired_thread_state_unknown", "data/retention/retired_threads.jsonl")
                cursor.update(phase="live", after="")
                return True, "retired_thread_unknown"
            if latest == "superseded":
                work.skip("superseded_retirement")
                cursor["journal_offset"] = page.next_offset
                continue
            if work.exhausted():
                return False, "budget_exhausted"
            try:
                before = work.items
                done = _purge_retired(work, root, row, proof)
                if work.items == before and not work.exhausted():
                    work.items += 1
            except BlockingIOError:
                work.skip("lock_busy")
                return False, "lock_busy"
            if not done:
                return False, "budget_exhausted" if work.exhausted() else "retired_thread_changed"
        cursor["journal_offset"] = page.next_offset
        work.retired_probe = None
        if page.complete:
            return True, ""
    return False, "budget_exhausted"


# LLM: 首线程使用独立标记额度；超额保留整树并推进，轮次完成与逐线程错误分开表达。
# 函数用途: 执行一个受预算限制的 compact 可达性维护片。
def maintain_compact_reachability_lane(request: StorageLaneRequest) -> dict:
    work = _LaneWork(request)
    cursor = {
        "after": str(request.cursor.get("after", "")),
        "journal_offset": max(0, int(request.cursor.get("journal_offset", 0))),
        "phase": "retired" if request.cursor.get("phase") == "retired" else "live",
    }
    if request.legal_hold:
        return work.receipt(request.cursor, True, "legal_hold")
    if request.budget.exhausted():
        return work.receipt(cursor, False, "budget_exhausted")
    root = Path(request.home.owner_compact_dir)
    try:
        _owner_path(request.home, root)
        incomplete = False
        if cursor["phase"] == "live":
            names = _live_window(request.home, root, cursor, work.budget.max_items)
            for name in names:
                if work._allowance_used and work.exhausted():
                    return work.receipt(cursor, False, "budget_exhausted")
                tid = name[:-6]
                before = work.items
                first = False
                try:
                    with work.thread_allowance() as first:
                        try:
                            thread = _thread_record(work, tid)
                        except _BudgetExceeded:
                            raise
                        except (OSError, ValueError, TypeError, KeyError):
                            thread = None
                        if thread is None:
                            work.sweep_state = None
                            work.skip("thread_record_unavailable")
                        elif not _live_thread(work, root, tid, thread):
                            return work.receipt(cursor, False, "budget_exhausted")
                    if thread is None:
                        # 退休台账的名称查找不是线程标记，继续遵守原片预算。
                        _count_untracked(work, root, tid)
                except BlockingIOError:
                    work.skip("lock_busy")
                    return work.receipt(cursor, False, "lock_busy")
                except _BudgetExceeded:
                    if not first:
                        return work.receipt(cursor, False, "budget_exhausted")
                    work.sweep_state = None
                    work.skip("mark_budget_exceeded")
                except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
                    work.error("compact_reachability_incomplete", "conversations/" + name)
                    incomplete = True
                if work.items == before:
                    work.items += 1
                cursor["after"] = name
            if len(names) >= work.budget.max_items:
                return work.receipt(cursor, False, "budget_exhausted")
            cursor.update(phase="retired", after="")
        done, deferred = _retired_slice(work, root, cursor)
        if done:
            cursor.update(phase="live", after="")
        return work.receipt(
            cursor, done, "reachability_incomplete" if incomplete else deferred
        )
    except _BudgetExceeded:
        return work.receipt(cursor, False, "budget_exhausted")
    except FileNotFoundError:
        return work.receipt(cursor, True)
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        work.error("compact_reachability_incomplete", "compact")
        return work.receipt(cursor, False, "reachability_incomplete")
