"""Owner-local、可按字节游标重放的调度历史投影；JSONL仍是原始账本。"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import stat
from contextlib import closing
from pathlib import Path

from ..common.json_io import append_private_jsonl_records_unlocked, locked_json_path
from ..common.nofollow_fs import ensure_private_dir, open_readonly_file_beneath

logger = logging.getLogger(__name__)


# LLM: 文件身份和首尾有界锚点只证明投影来源，不签发调度或投递权限。
# 函数用途: 在不整读历史的情况下取得稳定来源版本，prefix用于复核追加前的已覆盖范围。
def _snapshot(path, *, prefix=None):
    try:
        descriptor = open_readonly_file_beneath(path.parent, (path.name,))
    except FileNotFoundError:
        return {"stamp": [], "head": "", "tail": ""}
    with os.fdopen(descriptor, "rb") as handle:
        before = os.fstat(handle.fileno())
        size = before.st_size if prefix is None else min(prefix, before.st_size)
        head = handle.read(min(4096, size))
        handle.seek(max(0, size - 4096))
        tail = handle.read(min(4096, size))
        after = os.fstat(handle.fileno())
        stamp = [before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns]
        check = [after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns]
        if stamp != check:
            raise BlockingIOError("history changed during snapshot")
        current = path.stat(follow_symlinks=False)
        if current.st_ino != before.st_ino or current.st_dev != before.st_dev:
            raise BlockingIOError("history identity changed")
    return {"stamp": stamp, "head": hashlib.sha256(head).hexdigest(), "tail": hashlib.sha256(tail).hexdigest()}


# LLM: 索引只有查询和重放进度；原owner/schema解析器仍是行可信度裁决，所有连接均短借用并关闭。
# 类用途: 按run_id点查及job/时间顺序分页，按有界批次重建旧JSONL。
class SchedulerHistoryIndex:
    # LLM: 只建本owner同目录的投影；SQLite文件私有创建，拒绝链接及非常规文件。
    # 函数用途: 初始化可重建索引，不读取历史正文。
    def __init__(self, source: Path, owner: dict, parser):
        self.source, self.owner, self.parser = Path(source), dict(owner), parser
        self.path = self.source.with_suffix(".index.sqlite3")
        self._install()

    # LLM: 安装只补齐本owner缺失的投影文件和既有schema，不读取、删除或改写规范历史。
    # 函数用途: 用构造器原有流程创建私有SQLite文件及全部表索引。
    def _install(self):
        ensure_private_dir(self.source.parent)
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
        with closing(self._connect()) as conn, conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE IF NOT EXISTS history_state (key TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS history_rows (generation INTEGER, run_id TEXT, job_id TEXT, "
                         "ended_at REAL, payload TEXT, position INTEGER, PRIMARY KEY(generation,run_id))")
            conn.execute("CREATE TABLE IF NOT EXISTS history_effects (generation INTEGER, run_id TEXT, payload TEXT, "
                         "position INTEGER, PRIMARY KEY(generation,run_id))")
            conn.execute("CREATE INDEX IF NOT EXISTS history_job_time ON history_rows(generation,job_id,ended_at DESC,run_id DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS history_time ON history_rows(generation,ended_at DESC,run_id DESC)")

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
        old, current = state["stamp"], snapshot["stamp"]
        same = state.get("owner") == self.owner and state.get("stamp") == current and all(state.get(k) == snapshot[k] for k in ("head", "tail"))
        if same:
            return state
        append = False
        if old and current and old[:2] == current[:2] and old[2] < current[2] and state.get("owner") == self.owner:
            prefix = _snapshot(self.source, prefix=old[2])
            append = all(prefix[k] == state.get(k) for k in ("head", "tail"))
        if not append:
            state = {"generation": state["generation"] + 1, "cursor": 0, "owner": self.owner, "errors": []}
        state.update(snapshot)
        return state

    # LLM: 进度与本批行在同一个SQLite事务提交；失败不能提前消费旧游标。
    # 函数用途: 发布新的已回放位置及来源指纹。
    def _save(self, conn, state):
        conn.execute("INSERT INTO history_state(key,payload) VALUES('progress',?) "
                     "ON CONFLICT(key) DO UPDATE SET payload=excluded.payload", (json.dumps(state, ensure_ascii=False),))

    # LLM: effect保留最后原行，列表保留最后有效行；position让旧批次回填不能倒盖新追加。
    # 函数用途: 按原schema和owner规则写入一个历史行的两种查找投影。
    def _put_record(self, conn, state, raw, position):
        if isinstance(raw, dict) and isinstance(raw.get("run_id"), str):
            conn.execute("INSERT INTO history_effects VALUES(?,?,?,?) ON CONFLICT(generation,run_id) DO UPDATE "
                         "SET payload=excluded.payload,position=excluded.position WHERE excluded.position>=history_effects.position",
                         (state["generation"], raw["run_id"], json.dumps(raw, ensure_ascii=False), position))
        run, error = self.parser(raw, terminal=True)
        if error:
            state["errors"] = sorted(set(state["errors"]) | {error})
        elif run is not None:
            ended = float(run.get("ended_at") or run.get("created_at") or 0.0)
            conn.execute("INSERT INTO history_rows VALUES(?,?,?,?,?,?) ON CONFLICT(generation,run_id) DO UPDATE "
                         "SET job_id=excluded.job_id,ended_at=excluded.ended_at,payload=excluded.payload,position=excluded.position "
                         "WHERE excluded.position>=history_rows.position", (state["generation"], str(run["run_id"]),
                         run["job_id"], ended, json.dumps(run, ensure_ascii=False), position))

    # LLM: 正常追加持同一个源锁，原私有JSONL先发布，索引和进度后提交；间隙中止可由backfill补齐。
    # 函数用途: 把终态追加与可重放进度关联，不让旧数据未回填阻止新run直接点查。
    def append(self, records):
        if not records:
            return
        with locked_json_path(self.source), closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            before = _snapshot(self.source)
            state = self._prepare(self._state(conn), before)
            start = before["stamp"][2] if before["stamp"] else 0
            append_private_jsonl_records_unlocked(self.source, records)
            position = start
            for raw in records:
                position += len((json.dumps(raw, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
                self._put_record(conn, state, raw, position)
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
                                        self._put_record(conn, state, raw, state["cursor"])
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
    def page_queries(self, *, job_id, limit, generation=1):
        sql = "SELECT payload FROM history_rows WHERE generation=?"
        args = [generation]
        if job_id:
            sql += " AND job_id=?"
            args.append(job_id)
        sql += " ORDER BY ended_at DESC,run_id DESC LIMIT ?"
        args.append(limit + 1)
        return [(sql, tuple(args))]

    # LLM: 仅校验有界锚点，不同步回填；不完整索引只返回已覆盖结果并披露原因，版本不符不返回旧投影。
    # 函数用途: 在同一SQLite读快照中取当前页或准确run。
    def read(self, *, job_id="", limit=50, run_id=None):
        try:
            with locked_json_path(self.source), closing(self._connect(read_only=True)) as conn:
                conn.execute("BEGIN")
                state = self._state(conn)
                snapshot = _snapshot(self.source)
                if (state.get("owner") != self.owner or any(state.get(key) != snapshot[key] for key in ("stamp", "head", "tail"))):
                    return [], ["SCHEDULER_HISTORY_INDEX_INCOMPLETE"]
                errors = list(state["errors"])
                size = snapshot["stamp"][2] if snapshot["stamp"] else 0
                if state["cursor"] < size or state.get("pending") or state.get("skipping"):
                    errors.append("SCHEDULER_HISTORY_INDEX_INCOMPLETE")
                if run_id is not None:
                    rows = conn.execute("SELECT payload FROM history_effects WHERE generation=? AND run_id=?",
                                        (state["generation"], run_id)).fetchall()
                else:
                    sql, args = self.page_queries(job_id=job_id, limit=limit, generation=state["generation"])[0]
                    rows = conn.execute(sql, args).fetchall()
                return [json.loads(row[0]) for row in rows], sorted(set(errors))
        except (OSError, sqlite3.Error, ValueError) as exc:
            return [], ["SCHEDULER_HISTORY_READ_FAILED", type(exc).__name__]
