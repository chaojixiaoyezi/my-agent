"""单线程用量账的可重建定位与数字投影；JSONL 始终是费用事实来源。"""
from __future__ import annotations

import json
import sqlite3

from ..runtime_errors import DataCorruptionError
from .models import ThreadModelUsageEvent
from .observation_index import ObservationIndex, _identity, _reader
from .store_usage import (
    _PURPOSES,
    _UNKNOWN_PURPOSE_KEY_MAX_COUNT,
    _UNKNOWN_PURPOSE_KEYS,
    _UNKNOWN_PURPOSE_KEYS_LOCK,
    _display_purpose_key,
    _model_usage_identity_payload,
    _model_usage_scope,
    _purpose_rows,
    _sum_model_call_summaries,
    _sum_usage_partition,
)
from .thread_ledger_segments import archive_dir_for, archive_stamp, read_manifest


# LLM: 仅复用已审计的私有连接生命周期；不使用观察领域的ack、pending或journal协议。
# 类用途: 为一个会话提供幂等事件查询、范围累计和精确数字汇总。
class UsageIndex(ObservationIndex):
    # LLM: 延迟打开数据库，路径独立且不影响原JSONL和追加锁。
    # 函数用途: 绑定一个会话用量来源。
    def __init__(self, path, thread_id):
        self.source = path
        self.thread_id = thread_id
        self.path = path.parent / f".{path.stem}-usage.sqlite3"
        self.last_work = {}

    # LLM: 事件表只保存身份/位置；范围和总量为增量数字投影，可从原账重建。
    # 函数用途: 初始化私有数据库及主键索引。
    def _open(self):
        conn = sqlite3.connect(f"{self.path.as_uri()}?mode=rw", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA cache_size=-1024")
            conn.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT)")
            conn.execute("CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY,offset INTEGER,length INTEGER,digest TEXT)")
            conn.execute("CREATE TABLE IF NOT EXISTS totals(scope TEXT PRIMARY KEY,calls TEXT,count INTEGER,known INTEGER)")
            conn.execute("CREATE TABLE IF NOT EXISTS diagnostics(key TEXT PRIMARY KEY,count INTEGER)")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
            if "file" not in columns:
                conn.execute("ALTER TABLE events ADD COLUMN file TEXT NOT NULL DEFAULT ''")
                self._put(conn, "source", "null")
                self._put(conn, "offset", 0)
            conn.execute("CREATE INDEX IF NOT EXISTS events_source ON events(file)")
            for table in ("active_totals", "carried_totals"):
                conn.execute(f"CREATE TABLE IF NOT EXISTS {table}(scope TEXT PRIMARY KEY,calls TEXT,count INTEGER,known INTEGER)")
            for table in ("active_diagnostics", "carried_diagnostics"):
                conn.execute(f"CREATE TABLE IF NOT EXISTS {table}(key TEXT PRIMARY KEY,count INTEGER)")
            conn.execute("CREATE TABLE IF NOT EXISTS archives(file TEXT PRIMARY KEY,offset INTEGER,size INTEGER,complete INTEGER)")
            conn.execute("CREATE INDEX IF NOT EXISTS archives_work ON archives(complete,file)")
            conn.commit()
            return conn
        except BaseException:
            conn.close()
            raise

    # LLM: 不把created_at当重放身份；首次原账时间仍由精确行返回。
    # 函数用途: 生成与原幂等检查等价的稳定载荷。
    def _digest(self, event):
        return json.dumps(_model_usage_identity_payload(event), sort_keys=True, ensure_ascii=False, separators=(",", ":"))

    # LLM: 重建投影由持久字节游标恢复，每次最多128行；正常追加只读新后缀。
    # 函数用途: 追平一个有界批次，返回是否可以使用完整数字投影。
    def prepare(self, conn, *, with_events=True):
        stamp = json.dumps(archive_stamp(self.source))
        if self._get(conn, "archive_stamp", "") != stamp:
            self._seed_archives(conn)
        identity = _identity(self.source)
        stamp = json.dumps(identity)
        old = self._get(conn, "source", "null")
        previous = json.loads(old)
        offset = int(self._get(conn, "offset"))
        if old != stamp and not (identity and previous and list(identity[:2]) == previous[:2]
                                and identity[2] > previous[2] and offset <= identity[2]):
            if previous is not None or offset:
                self._reset_active(conn)
            offset = 0
        if identity is None:
            self._put(conn, "source", stamp)
            self._put(conn, "offset", 0)
            return self._prepare_archived_events(conn) if with_events else True
        if offset == identity[2]:
            return self._prepare_archived_events(conn) if with_events else True
        consumed = 0
        with _reader(self.source) as handle:
            handle.seek(offset)
            for _ in range(128):
                start = handle.tell()
                raw = handle.readline(1048577)
                if not raw:
                    break
                consumed += len(raw)
                if len(raw) > 1048576:
                    raise DataCorruptionError("model usage row exceeds bounded read limit")
                if raw.strip():
                    try:
                        event = ThreadModelUsageEvent.from_dict(json.loads(raw))
                        if event.thread_id != self.thread_id:
                            raise ValueError("thread model usage scope mismatch")
                        self.install(conn, event, start, len(raw))
                    except (ValueError, TypeError, KeyError) as exc:
                        raise DataCorruptionError("conversation model usage ledger is unreadable") from exc
                offset = handle.tell()
        if _identity(self.source) != identity:
            raise DataCorruptionError("model usage source changed during indexing")
        self._put(conn, "source", stamp)
        self._put(conn, "offset", offset)
        self.last_work = {"bytes_read": consumed}
        return offset == identity[2] and (not with_events or self._prepare_archived_events(conn))

    def _reset_active(self, conn):
        """活动身份被替换时重放活动段；已结转数字不能被清零。"""
        conn.execute("DELETE FROM events WHERE file=''")
        for table in ("active_totals", "active_diagnostics", "totals", "diagnostics"):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("INSERT INTO totals SELECT * FROM carried_totals")
        conn.execute("INSERT INTO diagnostics SELECT * FROM carried_diagnostics")

    def _add_total(self, conn, table, scope, calls, count, known):
        row = conn.execute(f"SELECT * FROM {table} WHERE scope=?", (scope,)).fetchone()
        prior = json.loads(row["calls"]) if row else {}
        total = _sum_model_call_summaries([prior, calls], count_unknown=False)
        conn.execute(f"INSERT OR REPLACE INTO {table} VALUES (?,?,?,?)",
                     (scope, json.dumps(total), count + (row["count"] if row else 0),
                      int(known and (row["known"] if row else True))))

    def _seed_archives(self, conn):
        """仅冷恢复/布局异常时由 Σcarry 播种；普通封段用 sealed 增量结转。"""
        manifest = read_manifest(archive_dir_for(self.source))
        entries = manifest["segments"]
        complete = manifest["coverage"] == "complete"
        if not entries and int(self._get(conn, "segment_count")):
            complete = False
        old_source = json.loads(self._get(conn, "source", "null"))
        old_offset = int(self._get(conn, "offset"))
        for table in ("carried_totals", "carried_diagnostics"):
            conn.execute(f"DELETE FROM {table}")
        for entry in entries:
            carry = entry.get("carry")
            if not isinstance(carry, dict) or not isinstance(carry.get("totals"), list):
                complete = False
            else:
                for row in carry["totals"]:
                    self._add_total(conn, "carried_totals", row["scope"], row["calls"], row["count"], row["known"])
                for purpose, count in carry.get("diagnostics", {}).items():
                    conn.execute("INSERT INTO carried_diagnostics VALUES (?,?) ON CONFLICT(key) DO UPDATE SET count=count+excluded.count",
                                 (purpose, count))
            # 原事务在 rename 后回滚：已有定位随原 inode 迁入封存，尚未索引的后缀继续有界回填。
            source = entry.get("source_identity")
            offset = 0
            if source and old_source and source[:2] == old_source[:2]:
                conn.execute("UPDATE events SET file=? WHERE file=''", (entry["file"],))
                offset = old_offset
            conn.execute("INSERT OR IGNORE INTO archives VALUES (?,?,?,?)",
                         (entry["file"], offset, entry["bytes"], int(offset == entry["bytes"])))
        self._reset_active(conn)
        self._put(conn, "source", "null")
        self._put(conn, "offset", 0)
        self._put(conn, "coverage", "complete" if complete else "partial")
        self._put(conn, "segment_count", len(entries))
        self._put(conn, "archive_stamp", json.dumps(archive_stamp(self.source)))

    def carry(self, conn):
        """只读当前有界活动段的数字，不把先前结转再计进新段。"""
        return {"totals": [{"scope": row["scope"], "calls": json.loads(row["calls"]),
                            "count": row["count"], "known": bool(row["known"])}
                           for row in conn.execute("SELECT * FROM active_totals")],
                "diagnostics": dict(conn.execute("SELECT key,count FROM active_diagnostics"))}

    def sealed(self, conn, entry):
        """原锁内完成 canonical 封段后，增量搬移定位并结转本段数字。"""
        for row in entry["carry"]["totals"]:
            self._add_total(conn, "carried_totals", row["scope"], row["calls"], row["count"], row["known"])
        for purpose, count in entry["carry"]["diagnostics"].items():
            conn.execute("INSERT INTO carried_diagnostics VALUES (?,?) ON CONFLICT(key) DO UPDATE SET count=count+excluded.count",
                         (purpose, count))
        conn.execute("UPDATE events SET file=? WHERE file=''", (entry["file"],))
        conn.execute("DELETE FROM active_totals")
        conn.execute("DELETE FROM active_diagnostics")
        conn.execute("INSERT OR REPLACE INTO archives VALUES (?,?,?,1)", (entry["file"], entry["bytes"], entry["bytes"]))
        self._put(conn, "source", "null")
        self._put(conn, "offset", 0)
        self._put(conn, "segment_count", int(self._get(conn, "segment_count")) + 1)
        self._put(conn, "archive_stamp", json.dumps(archive_stamp(self.source)))

    def _prepare_archived_events(self, conn):
        """投影丢失时每次最多补 128 条定位；summary 不需要回读封存正文。"""
        remaining = 128
        while remaining:
            row = conn.execute("SELECT * FROM archives WHERE complete=0 ORDER BY file LIMIT 1").fetchone()
            if row is None:
                return True
            # 缺叶和空段也消耗预算；否则损坏来源会在同一 EOF 位置永久空转。
            remaining -= 1
            path = archive_dir_for(self.source) / row["file"]
            eof = False
            try:
                with _reader(path) as handle:
                    handle.seek(row["offset"])
                    while remaining:
                        offset = handle.tell()
                        raw = handle.readline(1048577)
                        if not raw:
                            eof = True
                            break
                        if len(raw) > 1048576:
                            raise DataCorruptionError("model usage row exceeds bounded read limit")
                        if raw.strip():
                            event = ThreadModelUsageEvent.from_dict(json.loads(raw))
                            if event.thread_id != self.thread_id:
                                raise ValueError("thread model usage scope mismatch")
                            self._install_event(conn, event, offset, len(raw), row["file"])
                        remaining -= 1
                    offset = handle.tell()
            except FileNotFoundError:
                self._put(conn, "coverage", "partial")
                offset = row["size"]
            except (ValueError, TypeError, KeyError) as exc:
                raise DataCorruptionError("conversation model usage ledger is unreadable") from exc
            if (eof or offset > row["size"]) and offset != row["size"]:
                self._put(conn, "coverage", "partial")
            conn.execute("UPDATE archives SET offset=?,complete=? WHERE file=?",
                         (offset, int(eof or offset >= row["size"]), row["file"]))
        return conn.execute("SELECT 1 FROM archives WHERE complete=0 LIMIT 1").fetchone() is None

    # LLM: 用量原账先追加，定位和数字后在同一个投影事务提交；重放主键不能重复加费。
    # 函数用途: 安装一个规范事件并累计范围与线程数字。
    def install(self, conn, event, offset, length):
        if not self._install_event(conn, event, offset, length, ""):
            return
        for purpose in _purpose_rows(event.model_calls):
            if purpose not in _PURPOSES:
                for table in ("diagnostics", "active_diagnostics"):
                    conn.execute(f"INSERT INTO {table} VALUES (?,1) ON CONFLICT(key) DO UPDATE SET count=count+1", (purpose,))
        for scope in ("", json.dumps(_model_usage_scope(event.to_dict()))):
            for table in ("totals", "active_totals"):
                self._add_total(conn, table, scope, event.model_calls, 1, "purpose_breakdown" in event.model_calls)

    def _install_event(self, conn, event, offset, length, file):
        digest = self._digest(event)
        existing = conn.execute("SELECT digest FROM events WHERE id=?", (event.event_id,)).fetchone()
        if existing:
            if existing[0] != digest:
                raise DataCorruptionError("model usage event id reused with different input")
            return False
        conn.execute("INSERT INTO events(id,offset,length,digest,file) VALUES (?,?,?,?,?)", (event.event_id, offset, length, digest, file))
        return True

    # LLM: 只对当前事件ID读取原账精确行，不返回索引缓存的业务记录。
    # 函数用途: 查询已提交事件以校验重放。
    def event(self, conn, event_id):
        row = conn.execute("SELECT offset,length,file FROM events WHERE id=?", (event_id,)).fetchone()
        if row is None:
            return None
        path = archive_dir_for(self.source) / row["file"] if row["file"] else self.source
        try:
            with _reader(path) as handle:
                handle.seek(row["offset"])
                raw = handle.read(row["length"])
        except FileNotFoundError as exc:
            raise DataCorruptionError("model usage indexed source is unreadable; coverage=partial") from exc
        try:
            event = ThreadModelUsageEvent.from_dict(json.loads(raw))
        except (ValueError, TypeError, KeyError) as exc:
            raise DataCorruptionError("model usage indexed row is unreadable") from exc
        if event.event_id != event_id or event.thread_id != self.thread_id:
            raise DataCorruptionError("model usage indexed row identity mismatch")
        return event

    # LLM: 原累计delta算法仍使用同一范围数字，不扫描该范围的历史行。
    # 函数用途: 取得一个范围的累计数字。
    def prior(self, conn, event):
        row = conn.execute("SELECT calls FROM totals WHERE scope=?", (json.dumps(_model_usage_scope(event.to_dict())),)).fetchone()
        return [json.loads(row[0])] if row else []

    # LLM: 调用方仍持有原append-once锁，JSONL追加成功后才调用此方法。
    # 函数用途: 发布新事件与账本字节位置，崩溃窗口由prepare重放。
    def committed(self, conn, event, offset):
        identity = _identity(self.source)
        self.install(conn, event, offset, identity[2] - offset)
        self._put(conn, "source", json.dumps(identity))
        self._put(conn, "offset", identity[2])

    # LLM: 只有prepare完整时可调用；用途缺失不反推为零，保留原summary字段合同。
    # 函数用途: 从完整数字投影返回一个会话汇总。
    def summary(self, conn):
        if self._get(conn, "coverage", "complete") != "complete":
            raise DataCorruptionError("conversation model usage ledger is unreadable; coverage=partial")
        # 旧口径是每次summary对每个未知用途的原始行次计数，不计投影维护和写入。
        with _UNKNOWN_PURPOSE_KEYS_LOCK:
            keys = _UNKNOWN_PURPOSE_KEYS["keys"]
            for purpose, count in conn.execute("SELECT key,count FROM diagnostics ORDER BY rowid"):
                key = _display_purpose_key(purpose)
                if key not in keys and len(keys) >= _UNKNOWN_PURPOSE_KEY_MAX_COUNT:
                    _UNKNOWN_PURPOSE_KEYS["overflow_count"] += count
                else:
                    keys[key] = keys.get(key, 0) + count
        row = conn.execute("SELECT * FROM totals WHERE scope='' ").fetchone()
        calls = json.loads(row["calls"]) if row else {}
        provider = dict.fromkeys(("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_write_input_tokens", "call_count"), 0)
        estimated = dict.fromkeys(("input_tokens", "output_tokens", "call_count", "unfinished_input_tokens", "unfinished_call_count"), 0)
        breakdown = calls.get("usage_breakdown") or {}
        _sum_usage_partition(provider, breakdown.get("provider"))
        _sum_usage_partition(estimated, breakdown.get("estimated"))
        purposes = calls.get("purpose_breakdown")
        return {"schema": "thread_model_usage_summary.v1", "thread_id": self.thread_id,
                "event_count": row["count"] if row else 0, "provider": provider, "estimated": estimated,
                **({"purpose_breakdown": purposes, "purpose_totals_known": bool(row["known"])} if purposes else {})}
