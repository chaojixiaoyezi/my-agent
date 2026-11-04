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
    def prepare(self, conn):
        identity = _identity(self.source)
        stamp = json.dumps(identity)
        old = self._get(conn, "source", "null")
        previous = json.loads(old)
        offset = int(self._get(conn, "offset"))
        if old != stamp and not (identity and previous and list(identity[:2]) == previous[:2]
                                and identity[2] > previous[2] and offset <= identity[2]):
            conn.execute("DELETE FROM events")
            conn.execute("DELETE FROM totals")
            conn.execute("DELETE FROM diagnostics")
            offset = 0
        if identity is None:
            self._put(conn, "source", stamp)
            self._put(conn, "offset", 0)
            return True
        if offset == identity[2]:
            return True
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
        return offset == identity[2]

    # LLM: 用量原账先追加，定位和数字后在同一个投影事务提交；重放主键不能重复加费。
    # 函数用途: 安装一个规范事件并累计范围与线程数字。
    def install(self, conn, event, offset, length):
        digest = self._digest(event)
        existing = conn.execute("SELECT digest FROM events WHERE id=?", (event.event_id,)).fetchone()
        if existing:
            if existing[0] != digest:
                raise DataCorruptionError("model usage event id reused with different input")
            return
        conn.execute("INSERT INTO events VALUES (?,?,?,?)", (event.event_id, offset, length, digest))
        for purpose in _purpose_rows(event.model_calls):
            if purpose not in _PURPOSES:
                conn.execute("INSERT INTO diagnostics VALUES (?,1) ON CONFLICT(key) DO UPDATE SET count=count+1", (purpose,))
        for scope in ("", json.dumps(_model_usage_scope(event.to_dict()))):
            row = conn.execute("SELECT * FROM totals WHERE scope=?", (scope,)).fetchone()
            prior = json.loads(row["calls"]) if row else {}
            calls = _sum_model_call_summaries([prior, event.model_calls], count_unknown=False)
            count = row["count"] + 1 if row else 1
            known = (row["known"] if row else True) and "purpose_breakdown" in event.model_calls
            conn.execute("INSERT OR REPLACE INTO totals VALUES (?,?,?,?)", (scope, json.dumps(calls), count, int(known)))

    # LLM: 只对当前事件ID读取原账精确行，不返回索引缓存的业务记录。
    # 函数用途: 查询已提交事件以校验重放。
    def event(self, conn, event_id):
        row = conn.execute("SELECT offset,length FROM events WHERE id=?", (event_id,)).fetchone()
        if row is None:
            return None
        with _reader(self.source) as handle:
            handle.seek(row["offset"])
            raw = handle.read(row["length"])
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
