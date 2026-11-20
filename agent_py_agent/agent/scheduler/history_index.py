"""Owner-local、可按字节游标重放的调度历史投影；JSONL仍是原始账本。"""
from __future__ import annotations

import errno
import hashlib
import heapq
import json
import logging
import os
import sqlite3
import stat
import time
from collections.abc import Iterable
from contextlib import closing
from itertools import islice
from pathlib import Path

from ..common.json_io import (
    append_private_jsonl_records_unlocked,
    locked_json_path,
    seal_private_jsonl_tail_unlocked,
)
from ..common.nofollow_fs import ensure_private_dir, open_readonly_file_beneath
from ..io.sqlite_scope import ensure_private_sqlite_birth

logger = logging.getLogger(__name__)


# LLM: 文件身份和首尾有界锚点只证明投影来源，不签发调度或投递权限。
# 函数用途: 在不整读历史的情况下取得稳定来源版本，prefix用于复核追加前的已覆盖范围。
class _ProofBudgetExceeded(OSError):
    """本轮canonical读取额度不足；保持unknown，不能重新签发一份独立额度。"""


class _ProofReadBudget:
    def __init__(self, maximum, deadline, shared, receipt):
        self.maximum, self.deadline, self.shared, self.receipt = maximum, deadline, shared, receipt
        self.used = 0
        if shared is not None:
            self.deadline = min(deadline, shared.deadline) if deadline is not None else shared.deadline

    def remaining(self):
        if (self.deadline is not None and time.monotonic() >= self.deadline
                or self.shared is not None and self.shared.exhausted()):
            raise _ProofBudgetExceeded("共享调度终态证明预算已耗尽；保留候选并在下一维护片续验。")
        available = self.maximum - self.used
        if self.shared is not None:
            available = min(available, self.shared.max_bytes - self.shared.size)
        return max(0, available)

    def read(self, handle, count):
        if count > self.remaining():
            raise _ProofBudgetExceeded("本轮剩余canonical字节额度不足以完成源快照；保留候选，不扩大预算。")
        raw = handle.read(count)
        self.used += len(raw)
        self.receipt["bytes_read"] = self.used
        if self.shared is not None:
            self.shared.charge(size=len(raw))
        return raw


def _snapshot(path, *, prefix=None, read_budget=None):
    try:
        descriptor = open_readonly_file_beneath(path.parent, (path.name,))
    except FileNotFoundError:
        return {"stamp": [], "head": "", "tail": ""}
    with os.fdopen(descriptor, "rb") as handle:
        before = os.fstat(handle.fileno())
        size = before.st_size if prefix is None else min(prefix, before.st_size)
        head = (handle.read(min(4096, size)) if read_budget is None
                else read_budget.read(handle, min(4096, size)))
        handle.seek(max(0, size - 4096))
        tail = (handle.read(min(4096, size)) if read_budget is None
                else read_budget.read(handle, min(4096, size)))
        after = os.fstat(handle.fileno())
        stamp = [before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns]
        check = [after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns]
        if stamp != check:
            raise BlockingIOError("history changed during snapshot")
        current = path.stat(follow_symlinks=False)
        if current.st_ino != before.st_ino or current.st_dev != before.st_dev:
            raise BlockingIOError("history identity changed")
    return {"stamp": stamp, "head": hashlib.sha256(head).hexdigest(), "tail": hashlib.sha256(tail).hexdigest()}


def _verify_exact_source(path, handle, snapshot):
    expected = snapshot["stamp"]
    try:
        observed = os.fstat(handle.fileno()), path.stat(follow_symlinks=False)
    except OSError as exc:
        raise BlockingIOError(errno.EAGAIN, "scheduler history could not be verified after exact read") from exc
    for info in observed:
        if (info.st_nlink != 1 or [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns] != expected):
            raise BlockingIOError(errno.EAGAIN, "scheduler history changed during exact read")


# LLM: 索引只有查询和重放进度；原owner/schema解析器仍是行可信度裁决，所有连接均短借用并关闭。
# 类用途: 按run_id点查及job/时间顺序分页，按有界批次重建旧JSONL。
class SchedulerHistoryIndex:
    # LLM: 只建本owner同目录的投影；SQLite文件私有创建，拒绝链接及非常规文件。
    # 函数用途: 初始化可重建索引，不读取历史正文。
    def __init__(self, source: Path, owner: dict, parser, *, read_only: bool = False):
        self.source, self.owner, self.parser = Path(source), dict(owner), parser
        self.path = self.source.with_suffix(".index.sqlite3")
        if not read_only:
            self._install()

    # LLM: 安装只补齐本owner缺失的投影文件和既有schema，不读取、删除或改写规范历史。
    # 函数用途: 用构造器原有流程创建私有SQLite文件及全部表索引。
    def _install(self):
        ensure_private_dir(self.source.parent)
        ensure_private_sqlite_birth(self.path)
        with closing(self._connect()) as conn, conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("CREATE TABLE IF NOT EXISTS history_state (key TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS history_rows (generation INTEGER, run_id TEXT, job_id TEXT, "
                         "ended_at REAL, payload TEXT, position INTEGER, PRIMARY KEY(generation,run_id))")
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(history_rows)")}
            for name, kind in (("line_offset", "INTEGER"), ("line_length", "INTEGER"),
                               ("status", "TEXT"), ("trigger", "TEXT")):
                if name not in columns:
                    conn.execute(f"ALTER TABLE history_rows ADD COLUMN {name} {kind}")
            conn.execute("CREATE TABLE IF NOT EXISTS history_effects (generation INTEGER, run_id TEXT, payload TEXT, "
                         "position INTEGER, PRIMARY KEY(generation,run_id))")
            # 同一运行可能留下多条回执；精确匹配线程或领取代次时保留原行顺序。
            conn.execute("CREATE TABLE IF NOT EXISTS history_line_versions (generation INTEGER, run_id TEXT, "
                         "position INTEGER, line_length INTEGER NOT NULL, PRIMARY KEY(generation,run_id,position)) "
                         "WITHOUT ROWID")
            conn.execute("CREATE INDEX IF NOT EXISTS history_job_time ON history_rows(generation,job_id,ended_at DESC,run_id DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS history_time ON history_rows(generation,ended_at DESC,run_id DESC)")
            for name, fields in (
                ("status", "status"), ("trigger", "trigger"), ("status_trigger", "status,trigger"),
            ):
                for prefix, job in (("", ""), ("job_", "job_id,")):
                    conn.execute(f"CREATE INDEX IF NOT EXISTS history_{prefix}{name}_time ON history_rows"
                                 f"(generation,{job}{fields},ended_at DESC,run_id DESC)")

    # LLM: 只读查询不能建缺库、迁移或修改源；写者由同一history文件锁串行。
    # 函数用途: 打开并由调用者关闭一个原生SQLite连接。
    def _connect(self, *, read_only=False):
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            if read_only:
                raise
            self._install()
            logger.warning("SQLITE_SCHEMA_REINSTALLED store=scheduler_history path=%s", self.path)
            info = self.path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError("invalid history index file")
        conn = sqlite3.connect(self.path.as_uri() + ("?mode=ro" if read_only else "?mode=rw"), uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    # LLM: 单行持久进度绑定完整owner，不能把其他owner的索引当本人的结果。
    # 函数用途: 读取投影游标和已覆盖来源版本。
    def _state(self, conn):
        row = conn.execute("SELECT payload FROM history_state WHERE key='progress'").fetchone()
        return json.loads(row[0]) if row else {"generation": 1, "cursor": 0, "owner": self.owner,
                                              "stamp": [], "head": "", "tail": "", "errors": []}

    # LLM: 回放前验证来源；退代只增加generation，不在热维护中删除整代历史索引。
    # 函数用途: 识别追加、替换或截断，避免旧投影覆盖新账本。
    def _prepare(self, state, snapshot):
        if state.get("line_index") and self._matches(state, snapshot):
            return state
        # 本写者的追加在同一事务更新快照。外部变化与旧格式需要有界重放，以免保留中部改写后的旧定位。
        return {**snapshot, "generation": state["generation"] + 1, "cursor": 0,
                "owner": self.owner, "errors": [], "line_index": True, "rebuilt_at": time.time()}

    def _matches(self, state, snapshot):
        return state.get("owner") == self.owner and all(state.get(key) == value for key, value in snapshot.items())

    def _complete(self, state, snapshot):
        size = snapshot["stamp"][2] if snapshot["stamp"] else 0
        return (state.get("line_index") and self._matches(state, snapshot) and state["cursor"] == size
                and not state.get("pending") and not state.get("skipping"))

    @staticmethod
    def _read_line(handle, position, length, size, *, meter=None, require_newline=False):
        if type(position) is not int or type(length) is not int or not 0 < length <= position <= size:
            raise ValueError("invalid history line location")
        handle.seek(position - length)
        raw = handle.read(length) if meter is None else meter.read(handle, length)
        if len(raw) != length or (not raw.endswith(b"\n") and (require_newline or position != size)):
            raise ValueError("invalid history line boundary")
        return json.loads(raw)

    # LLM: 进度与本批行在同一个SQLite事务提交；失败不能提前消费旧游标。
    # 函数用途: 发布新的已回放位置及来源指纹。
    def _save(self, conn, state):
        conn.execute("INSERT INTO history_state(key,payload) VALUES('progress',?) "
                     "ON CONFLICT(key) DO UPDATE SET payload=excluded.payload", (json.dumps(state, ensure_ascii=False),))

    # LLM: effect保留最后原行，列表保留最后有效行；position让旧批次回填不能倒盖新追加。
    # 函数用途: 按原schema和owner规则写入一个历史行的两种查找投影。
    def _put_record(self, conn, state, raw, position, length):
        run_id = str(raw.get("run_id") or "")
        if run_id:
            conn.execute("INSERT INTO history_effects VALUES(?,?,?,?) ON CONFLICT(generation,run_id) DO UPDATE "
                         "SET payload=excluded.payload,position=excluded.position WHERE excluded.position>=history_effects.position",
                         (state["generation"], run_id, json.dumps(raw, ensure_ascii=False), position))
            conn.execute("INSERT OR IGNORE INTO history_line_versions(generation,run_id,position,line_length) "
                         "VALUES (?,?,?,?)", (state["generation"], run_id, position, length))
        run, error = self.parser(raw, terminal=True)
        if error:
            state["errors"] = sorted(set(state["errors"]) | {error})
        elif run is not None:
            ended = float(run.get("ended_at") or run.get("created_at") or 0.0)
            conn.execute("INSERT INTO history_rows(generation,run_id,job_id,ended_at,payload,position,line_offset,"
                         "line_length,status,trigger) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(generation,run_id) DO UPDATE "
                         "SET job_id=excluded.job_id,ended_at=excluded.ended_at,payload=excluded.payload,position=excluded.position,"
                         "line_offset=excluded.line_offset,line_length=excluded.line_length,status=excluded.status,trigger=excluded.trigger "
                         "WHERE excluded.position>=history_rows.position", (state["generation"], str(run["run_id"]),
                         run["job_id"], ended, json.dumps(run, ensure_ascii=False), position,
                         position - length, length, str(run.get("status") or ""), str(run.get("trigger") or "")))

    # LLM: 正常追加持同一个源锁，原私有JSONL先发布，索引和进度后提交；间隙中止可由backfill补齐。
    # 函数用途: 把终态追加与可重放进度关联，不让旧数据未回填阻止新run直接点查。
    def append(self, records):
        if not records:
            return
        with locked_json_path(self.source), closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            seal_private_jsonl_tail_unlocked(self.source)
            before = _snapshot(self.source)
            state = self._prepare(self._state(conn), before)
            start = before["stamp"][2] if before["stamp"] else 0
            append_private_jsonl_records_unlocked(self.source, records)
            position = start
            for raw in records:
                length = len((json.dumps(raw, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
                position += length
                self._put_record(conn, state, raw, position, length)
            if state["cursor"] == start and not state.get("pending"):
                state["cursor"] = position
            state.update(_snapshot(self.source))
            self._save(conn, state)

    # LLM: 仅显式后台维护读取旧正文，每批最多256行/256KiB；部分长行保存有界片段，跨重启继续。
    # 函数用途: 分批回填历史，原行与游标同事务发布，错误不使游标无进展。
    def backfill(self, *, max_rows=256, max_bytes=262144):
        with locked_json_path(self.source), closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            snapshot = _snapshot(self.source)
            state = self._prepare(self._state(conn), snapshot)
            size = snapshot["stamp"][2] if snapshot["stamp"] else 0
            used, rows = 0, 0
            pending = bytes.fromhex(state.pop("pending", ""))
            skipping = state.pop("skipping", False)
            if state["cursor"] < size:
                descriptor = open_readonly_file_beneath(self.source.parent, (self.source.name,))
                with os.fdopen(descriptor, "rb") as handle:
                    handle.seek(state["cursor"])
                    while rows < max_rows and used < max_bytes:
                        chunk = handle.readline(min(65536, max_bytes - used))
                        if not chunk:
                            break
                        used += len(chunk)
                        state["cursor"] += len(chunk)
                        if not skipping:
                            pending += chunk
                            if len(pending) > 1048576:
                                skipping, pending = True, b""
                                state["errors"] = sorted(set(state["errors"]) | {"SCHEDULER_HISTORY_ROW_TOO_LARGE"})
                        if chunk.endswith(b"\n") or state["cursor"] >= size:
                            rows += 1
                            if pending.strip() and not skipping:
                                try:
                                    raw = json.loads(pending)
                                except (ValueError, UnicodeError) as exc:
                                    state["errors"] = sorted(set(state["errors"]) | {type(exc).__name__})
                                else:
                                    if isinstance(raw, dict):
                                        self._put_record(conn, state, raw, state["cursor"], len(pending))
                                    else:
                                        state["errors"] = sorted(set(state["errors"]) | {"ValueError"})
                            pending, skipping = b"", False
            state["pending"], state["skipping"] = pending.hex(), skipping
            if _snapshot(self.source) != snapshot:
                raise BlockingIOError("history changed during backfill")
            self._save(conn, state)
            return {"complete": state["cursor"] == size and not pending and not skipping,
                    "rows": rows, "bytes": used, "cursor": state["cursor"]}

    # LLM: 双排序索引覆盖有/无job两种页；B+1只在本页解码，不做SQL临时排序。
    # 函数用途: 构造可被EXPLAIN核验的索引分页语句。
    def page_queries(self, *, job_id, limit, generation=1, since=None, until=None, statuses=(), trigger=""):
        queries = []
        filtering = bool(job_id or statuses or trigger) or since is not None or until is not None
        for status in dict.fromkeys(statuses or ("",)):
            sql = ("SELECT ended_at,run_id,line_offset,line_length FROM history_rows WHERE generation=?" if filtering
                   else "SELECT payload FROM history_rows WHERE generation=?")
            args = [generation]
            for name, value in (("job_id", job_id), ("status", status), ("trigger", trigger)):
                if value:
                    sql += f" AND {name}=?"
                    args.append(value)
            if since is not None:
                sql += " AND ended_at>=?"
                args.append(float(since))
            if until is not None:
                sql += " AND ended_at<=?"
                args.append(float(until))
            sql += " ORDER BY ended_at DESC,run_id DESC LIMIT ?"
            queries.append((sql, (*args, limit + 1)))
        return queries

    @property
    def last_read_coverage(self) -> str:
        """保留既有查询属性；筛选覆盖由当前查询的结构化错误报告。"""
        return ""

    # LLM: 仅校验有界锚点，不同步回填；不完整索引只返回已覆盖结果并披露原因，版本不符不返回旧投影。
    # 函数用途: 在同一SQLite读快照中取当前页或准确run。
    def read(self, *, job_id="", limit=50, run_id=None, statuses=(), since=None, until=None, trigger=""):
        try:
            with locked_json_path(self.source), closing(self._connect(read_only=True)) as conn:
                conn.execute("BEGIN")
                state = self._state(conn)
                snapshot = _snapshot(self.source)
                if not state.get("line_index") or not self._matches(state, snapshot):
                    return [], ["SCHEDULER_HISTORY_INDEX_INCOMPLETE"]
                errors = list(state["errors"])
                size = snapshot["stamp"][2] if snapshot["stamp"] else 0
                if state["cursor"] < size or state.get("pending") or state.get("skipping"):
                    errors.append("SCHEDULER_HISTORY_INDEX_INCOMPLETE")
                if run_id is not None:
                    row = conn.execute("SELECT e.position,v.line_length FROM history_effects AS e "
                                       "LEFT JOIN history_line_versions AS v USING(generation,run_id,position) "
                                       "WHERE e.generation=? AND e.run_id=?", (state["generation"], run_id)).fetchone()
                    if row is None:
                        return [], sorted(set(errors))
                    descriptor = open_readonly_file_beneath(self.source.parent, (self.source.name,))
                    with os.fdopen(descriptor, "rb") as handle:
                        raw = self._read_line(handle, row["position"], row["line_length"], size)
                        _verify_exact_source(self.source, handle, snapshot)
                    if not isinstance(raw, dict) or str(raw.get("run_id") or "") != run_id:
                        raise BlockingIOError("history changed during exact query")
                    return [raw], sorted(set(errors))
                statuses = tuple(str(item) for item in (statuses or ()) if item)
                filtering = bool(job_id or statuses or trigger) or since is not None or until is not None
                queries = self.page_queries(
                    job_id=job_id,
                    limit=limit,
                    generation=state["generation"],
                    since=since,
                    until=until,
                    statuses=statuses,
                    trigger=str(trigger or ""),
                )
                if not filtering:
                    sql, args = queries[0]
                    return [json.loads(row[0]) for row in conn.execute(sql, args)], sorted(set(errors))
                candidates = heapq.merge(*(conn.execute(sql, args) for sql, args in queries),
                                          key=lambda row: (row["ended_at"] is not None, row["ended_at"], row["run_id"]),
                                          reverse=True)
                candidates = list(islice(candidates, limit + 1))
                if not candidates:
                    return [], sorted(set(errors))
                descriptor = open_readonly_file_beneath(self.source.parent, (self.source.name,))
                with os.fdopen(descriptor, "rb") as handle:
                    filtered = []
                    for row in candidates:
                        offset, length = row["line_offset"], row["line_length"]
                        raw = self._read_line(handle, offset + length, length, size)
                        if not isinstance(raw, dict) or str(raw.get("run_id") or "") != row["run_id"]:
                            raise ValueError("history line identity changed")
                        run, error = self.parser(raw, terminal=True)
                        if error or run is None:
                            raise ValueError(error or "history terminal line invalid")
                        filtered.append(run)
                    _verify_exact_source(self.source, handle, snapshot)
                return filtered, sorted(set(errors))
        except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
            return [], ["SCHEDULER_HISTORY_READ_FAILED", type(exc).__name__]

    def membership(self, run_ids: Iterable[str]) -> tuple[frozenset[str], bool]:
        """在同一只读快照内按分片核对 run_id 归属；仅当索引完整且未滞后时返回权威结果。"""
        try:
            targets = tuple(rid for rid in dict.fromkeys(str(item or "") for item in run_ids) if rid)
            with locked_json_path(self.source), closing(self._connect(read_only=True)) as conn:
                conn.execute("BEGIN")
                snapshot = _snapshot(self.source)
                state = self._state(conn)
                if (not snapshot["stamp"] or not self._complete(state, snapshot)
                    or {"SCHEDULER_HISTORY_ROW_TOO_LARGE", "UnicodeDecodeError"}.intersection(state["errors"])
                ):
                    return frozenset(), False
                if not targets:
                    return frozenset(), True
                found: set[str] = set()
                generation = state["generation"]
                size = snapshot["stamp"][2]
                chunk_size = 500
                descriptor = open_readonly_file_beneath(self.source.parent, (self.source.name,))
                with os.fdopen(descriptor, "rb") as handle:
                    for offset in range(0, len(targets), chunk_size):
                        batch = targets[offset : offset + chunk_size]
                        placeholders = ",".join("?" for _ in batch)
                        rows = conn.execute(
                            "SELECT run_id,position,(SELECT line_length FROM history_line_versions AS v "
                            "WHERE (v.generation,v.run_id,v.position)=(history_effects.generation,"
                            "history_effects.run_id,history_effects.position)) AS line_length "
                            f"FROM history_effects WHERE generation=? AND run_id IN ({placeholders})",
                            (generation, *batch),
                        )
                        for row in rows:
                            raw = self._read_line(handle, row["position"], row["line_length"], size)
                            if not isinstance(raw, dict) or str(raw.get("run_id") or "") != row["run_id"]:
                                return frozenset(), False
                            found.add(row["run_id"])
                    _verify_exact_source(self.source, handle, snapshot)
                return frozenset(found), True
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
            return frozenset(), False

    def locate_terminals(self, run_ids, matches, *, parser):
        """定位每个运行最新的匹配终态，保留同一运行的历史回执顺序。"""
        try:
            with locked_json_path(self.source), closing(self._connect(read_only=True)) as conn:
                conn.execute("BEGIN")
                state, snapshot = self._state(conn), _snapshot(self.source)
                if (not self._complete(state, snapshot)
                        or {"SCHEDULER_HISTORY_ROW_TOO_LARGE", "UnicodeDecodeError"}.intersection(state["errors"])):
                    return None
                if not snapshot["stamp"]:
                    return {}
                size = snapshot["stamp"][2]
                terminals = {}
                descriptor = open_readonly_file_beneath(self.source.parent, (self.source.name,))
                with os.fdopen(descriptor, "rb") as handle:
                    for run_id in run_ids:
                        rows = conn.execute(
                            "SELECT position,line_length FROM history_line_versions "
                            "WHERE generation=? AND run_id=? ORDER BY position DESC",
                            (state["generation"], run_id),
                        )
                        for row in rows:
                            raw = self._read_line(handle, row["position"], row["line_length"], size)
                            if not isinstance(raw, dict) or str(raw.get("run_id") or "") != run_id:
                                return None
                            terminal, _error = parser(raw, terminal=True)
                            if terminal is not None and matches(terminal):
                                terminals[run_id] = terminal
                                break
                    _verify_exact_source(self.source, handle, snapshot)
                return terminals
        except BlockingIOError:
            raise
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
            return None

    def terminal_run_proof(self, run_id, *, thread_id, cutoff, max_bytes=1048576, deadline=None, scan_budget=None):
        """点查仅作定位，按末字节位置重读原行；不创建索引、不回填、不用投影签发删除权。"""
        import math

        unknown = {"status": "unknown", "run_id": run_id, "thread_id": thread_id,
                   "side_effects": "not_started", "bytes_read": 0, "byte_scope": "canonical_source"}
        if (not isinstance(run_id, str) or not run_id.startswith("srun_")
                or type(max_bytes) is not int or max_bytes < 0):
            return {**unknown, "reason": "SCHEDULER_PROOF_ARGUMENT_INVALID"}
        meter = _ProofReadBudget(max_bytes, deadline, scan_budget, unknown)
        try:
            if meter.remaining() <= 0:
                raise _ProofBudgetExceeded("canonical证明读取字节额度为零；保留候选等待下一维护片。")
        except _ProofBudgetExceeded as exc:
            return {**unknown, "reason": "SCHEDULER_PROOF_BUDGET_EXHAUSTED", "message": str(exc)}
        try:
            from ..io.readonly_sqlite import readonly_sqlite_connection
        except ImportError as exc:
            return {**unknown, "reason": "SCHEDULER_PROOF_READONLY_SQLITE_UNAVAILABLE",
                    "error_type": type(exc).__name__, "message": str(exc)}
        try:
            with readonly_sqlite_connection(self.path, blocking=False, deadline=meter.deadline) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("BEGIN")
                state, snapshot = self._state(conn), _snapshot(self.source, read_budget=meter)
                size = snapshot["stamp"][2] if snapshot["stamp"] else 0
                if not size or not self._complete(state, snapshot):
                    return {**unknown, "reason": "SCHEDULER_HISTORY_INDEX_INCOMPLETE"}
                row = conn.execute("SELECT e.payload,e.position,v.line_length FROM history_effects AS e "
                                   "LEFT JOIN history_line_versions AS v USING(generation,run_id,position) "
                                   "WHERE e.generation=? AND e.run_id=?", (state["generation"], run_id)).fetchone()
                if row is None:
                    return {**unknown, "reason": "SCHEDULER_PROOF_RUN_MISSING"}
                position, length = row["position"], row["line_length"]
                if type(position) is not int or type(length) is not int or not 0 < length <= position <= size:
                    return {**unknown, "reason": "SCHEDULER_PROOF_POSITION_INVALID"}
                # 留出第二次源快照的准确head/tail额度；不得读完行后才发现本片已超支。
                if length > meter.remaining() - 2 * min(4096, size):
                    raise _ProofBudgetExceeded("剩余额度不足以同时重读原行及复核源快照；保留候选。")
                descriptor = open_readonly_file_beneath(self.source.parent, (self.source.name,))
                with os.fdopen(descriptor, "rb") as handle:
                    try:
                        payload = self._read_line(handle, position, length, size, meter=meter, require_newline=True)
                    except ValueError:
                        return {**unknown, "reason": "SCHEDULER_PROOF_ROW_BOUNDARY_INVALID"}
                if payload != json.loads(row["payload"]) or _snapshot(self.source, read_budget=meter) != snapshot:
                    return {**unknown, "reason": "SCHEDULER_PROOF_SOURCE_CHANGED"}
                parsed, error = self.parser(payload, terminal=True)
                if error or parsed is None:
                    return {**unknown, "reason": error or "SCHEDULER_PROOF_SCHEMA_INVALID"}
                ended = parsed.get("ended_at")
                if (parsed.get("run_id") != run_id or parsed.get("thread_id") != thread_id
                        or parsed.get("owner") != self.owner
                        or parsed.get("status") not in {"done", "failed", "cancelled", "skipped"}
                        or parsed.get("delivery_status") not in {"sent", "suppressed", "local_only", "no_channel"}
                        or type(ended) not in (int, float) or not math.isfinite(ended) or not 0 < ended < cutoff
                        or parsed.get("cancel_requested_at") and parsed.get("cancel_cleanup_confirmed") is not True
                        or any(parsed.get(key) for key in ("delivery_pending", "consume_pending", "callback_pending"))):
                    return {**unknown, "reason": "SCHEDULER_PROOF_NOT_SETTLED"}
                return {**unknown, "status": "settled", "run_id": run_id, "thread_id": thread_id,
                        "owner": dict(self.owner), "terminal_status": parsed["status"], "ended_at": ended,
                        "execution_mode": (parsed.get("execution") or {}).get("mode", "followup"),
                        "source_generation": state["generation"], "source_stamp": snapshot["stamp"],
                        "source_position": position, "side_effects": "not_started"}
        except _ProofBudgetExceeded as exc:
            return {**unknown, "reason": "SCHEDULER_PROOF_BUDGET_EXHAUSTED", "message": str(exc)}
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError) as exc:
            return {**unknown, "reason": "SCHEDULER_PROOF_READ_FAILED", "error_type": type(exc).__name__,
                    "message": str(exc)}
