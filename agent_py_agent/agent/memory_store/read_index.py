# LLM: 本库是可删除重建的owner内投影，只返回ID/version/hash/位置；调用方必须回读同代正文并重核墓碑/scope。
# 模块用途: 为长期记忆冷热分片提供带耐久游标和coverage的SQLite读索引。
from __future__ import annotations

import json
import os
import sqlite3
import stat
import time
from contextlib import closing

from ..common.global_write_commit import execute_global_write
from ..common.nofollow_fs import NoFollowPathError
from ..common.text_norm import fold_key
from .generation_manifest import MemoryGenerationStore


# LLM: 只表达派生坐标/水位已换代，不吞真正分片完整性错误或native拒绝。
# 类用途: 让调用方将代次冲突显式降级到当前正式库。
class MemoryReadIndexGenerationConflict(ValueError):
    pass


# LLM: 不保存来源callback或连接；只读查询不创建DB，显式step每次至多1000条，C写事务固定一次末门。
# 类用途: 管理单个正式逻辑库的派生搜索水位与分片定位。
class MemoryReadIndex:
    # LLM: 不触发DDL或扫描；不同basename的通用库不能共用同一个索引。
    # 函数用途: 绑定当前owner与确定的SQLite派生路径。
    def __init__(self, memory_path, *, coordinator_io=False):
        self.store = MemoryGenerationStore(memory_path)
        name = "memory_read_index.sqlite3" if self.store.path.name == "memory.jsonl" else self.store.path.name + ".read_index.sqlite3"
        self.path = self.store.path.with_name(name)
        self.coordinator_io = coordinator_io

    # LLM: 创建前拒绝符号链接和多链接，初始种子0600；只读mode=ro不能意外创建缺失库。
    # 函数用途: 打开本次短连接，拒绝异常索引路径。
    def _connect(self, *, write=False):
        if write:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
            except FileExistsError:
                pass
            else:
                os.close(fd)
        metadata = self.path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise NoFollowPathError("invalid memory read index")
        connection = sqlite3.connect(self.path.absolute().as_uri() + ("?mode=rw" if write else "?mode=ro"), uri=True, timeout=0)
        connection.row_factory = sqlite3.Row
        return connection

    # LLM: 语义签名含坐标/ID/version/索引正文，touch不重置；仍以当前根绑定候选，旧格式索引需重建。
    # 函数用途: 查询派生水位，缺库时返回可观察的missing而非零条完整结果。
    def coverage(self, root=None, *, connection=None):
        root = root or self.store.manifest()
        if root is None:
            return {"status": "unmigrated", "indexed_records": 0, "total_records": None}
        try:
            if connection is None:
                with closing(self._connect()) as conn:
                    row = conn.execute("SELECT value FROM metadata WHERE key='watermark'").fetchone()
            else:
                row = connection.execute("SELECT value FROM metadata WHERE key='watermark'").fetchone()
            state = json.loads(row[0]) if row else {}
            if not isinstance(state, dict):
                raise ValueError("MEMORY_INDEX_WATERMARK_INVALID")
        except FileNotFoundError:
            state = {}
        except (sqlite3.Error, ValueError, TypeError):
            return {"status": "unavailable", "indexed_records": None, "total_records": root["total_records"]}
        current = (state.get("index_signature") == root.get("index_signature", root["operation_id"])
                   and state.get("format_version") == 2 and state.get("owner") == self.store.tombstones.owner)
        return {"status": ("complete" if state.get("complete") else "partial") if current else "missing",
                "operation_id": root["operation_id"], "generation": root["generation"],
                "indexed_records": state.get("indexed_records", 0) if current else 0,
                "total_records": root["total_records"], "cursor": state.get("cursor", [0, 0]) if current else [0, 0]}

    # LLM: 调用方持formal锁及原quota准入；先准备不超过batch_size的记录，SQLite只在固定短事务中发表水位。
    # 函数用途: 有界追平本代索引，重启按分片号/行号续作，不重扫所有旧行。
    def rebuild_step(self, *, batch_size=1000, admission=None):
        from ..user_space.owner_quota import OwnerQuotaChange
        from .jsonl import _memory_record_from_obj, memory_content_hash

        if type(batch_size) is not int or not 1 <= batch_size <= 1000:
            raise ValueError("MEMORY_INDEX_BATCH_INVALID")
        root = self.store.manifest()
        if root is None:
            return self.coverage(root)
        before = self.coverage(root)
        if before["status"] == "complete":
            return before
        if before["status"] == "unavailable":
            raise ValueError("MEMORY_READ_INDEX_UNREADABLE")
        segment_no, row_no = before["cursor"]
        prepared = []
        while segment_no < len(root["segments"]) and len(prepared) < batch_size:
            segment = root["segments"][segment_no]
            rows = self.store.segment_rows(segment)
            while row_no < len(rows) and len(prepared) < batch_size:
                record = _memory_record_from_obj(rows[row_no])
                if record is None:
                    raise ValueError("MEMORY_AUTHORITY_UNREADABLE")
                prepared.append((record.entry_id, record.version, memory_content_hash(record.content),
                    segment_no, row_no, _lexical_projection(record)))
                row_no += 1
            if row_no == len(rows):
                segment_no, row_no = segment_no + 1, 0
        state = {"owner": self.store.tombstones.owner, "operation_id": root["operation_id"],
                 "index_signature": root.get("index_signature", root["operation_id"]), "format_version": 2,
                 "generation": root["generation"], "indexed_records": before["indexed_records"] + len(prepared),
                 "cursor": [segment_no, row_no], "complete": segment_no == len(root["segments"])}
        if admission is not None:
            try:
                size = self.path.stat().st_size
            except FileNotFoundError:
                size = 0
            admission.check([OwnerQuotaChange(self.path, size + 65536 + sum(len(r[-1].encode()) * 4 + 512 for r in prepared))])

        # LLM: 本叶只有已准备的有界SQL和连接生命周期，不读取正文或等待领域锁；忙库零等待失败保原水位。
        # 函数用途: 在同一SQLite事务内写候选与耐久游标。
        def publish():
            with closing(self._connect(write=True)) as conn, conn:
                conn.execute("PRAGMA secure_delete=ON")
                conn.execute("CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
                conn.execute("CREATE TABLE IF NOT EXISTS entries(id TEXT PRIMARY KEY,version INTEGER,content_hash TEXT,segment INTEGER,row_no INTEGER,search_text TEXT)")
                conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(id UNINDEXED,search_text,tokenize='trigram')")
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT value FROM metadata WHERE key='watermark'").fetchone()
                actual = json.loads(row[0]) if row else {}
                if actual.get("owner") not in (None, state["owner"]):
                    raise ValueError("MEMORY_INDEX_OWNER_MISMATCH")
                if (actual.get("index_signature") != state["index_signature"] or actual.get("format_version") != 2):
                    conn.execute("DELETE FROM entries")
                    conn.execute("DELETE FROM entries_fts")
                elif actual.get("indexed_records", 0) != before["indexed_records"]:
                    raise ValueError("MEMORY_INDEX_CURSOR_CONFLICT")
                conn.executemany("INSERT OR REPLACE INTO entries VALUES (?,?,?,?,?,?)", prepared)
                conn.executemany("INSERT INTO entries_fts(id,search_text) VALUES (?,?)", [(row[0], row[-1]) for row in prepared])
                conn.execute("INSERT OR REPLACE INTO metadata VALUES ('watermark',?)", (json.dumps(state),))
        if self.coordinator_io:
            execute_global_write(publish, require_scope=True)
        else:
            publish()
        return self.coverage(root)

    # LLM: 同一只读事务取水位及OR候选；短词/bigram编码成trigram可检索token，不丢英文关键词。
    # 函数用途: 从冷热读索引选取有界候选并附实际coverage。
    def query(self, query, *, limit=64):
        root = self.store.manifest()
        if root is None or not 1 <= limit <= 1000:
            return [], self.coverage(root)
        folded = fold_key(query)[:512]
        from ..retrieval.lexical import tokenize

        terms = list(dict.fromkeys([*("z" + token.encode().hex() for token in tokenize(folded)),
            *(term for term in [folded, *folded.split()] if len(term) >= 3)]))[:64]
        try:
            connection = self._connect()
        except FileNotFoundError:
            return [], self.coverage(root)
        with closing(connection) as conn:
            conn.execute("BEGIN")
            coverage = self.coverage(root, connection=conn)
            if coverage["status"] != "complete":
                return [], coverage
            if terms:
                expression = " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)
                rows = conn.execute("SELECT e.* FROM entries_fts f JOIN entries e ON e.id=f.id WHERE entries_fts MATCH ? ORDER BY rank LIMIT ?", (expression, limit + 1)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM entries WHERE instr(search_text,?) > 0 ORDER BY id LIMIT ?", (folded, limit + 1)).fetchall()
        coverage = {**coverage, "result_truncated": len(rows) > limit}
        return [{**dict(row), "operation_id": root["operation_id"]} for row in rows[:limit]], coverage

    # LLM: 坐标只解释于绑定根；换代显式冲突而非完整空命中，读取后再次核根及当前墓碑。
    # 函数用途: 将候选恢复为当前真正可见的正式事实。
    def resolve(self, candidates, *, operation_id=None):
        from .jsonl import _memory_record_from_obj, memory_content_hash

        root = self.store.manifest()
        bound = {item.get("operation_id") for item in candidates}
        if operation_id is not None:
            bound.add(operation_id)
        if root is None or bound != {root["operation_id"]}:
            raise MemoryReadIndexGenerationConflict("MEMORY_READ_INDEX_GENERATION_CONFLICT")
        blocked = self.store.tombstones.load()
        loaded, records = {}, []
        for item in candidates:
            index, offset = item["segment"], item["row_no"]
            if not 0 <= index < len(root["segments"]):
                continue
            if index not in loaded:
                loaded[index] = self.store.segment_rows(root["segments"][index])
            rows = loaded[index]
            if not 0 <= offset < len(rows):
                continue
            record = _memory_record_from_obj(rows[offset])
            if (record is not None and not self.store.tombstones.excludes(record.entry_id,
                    memory_content_hash(record.content), records=blocked) and record.entry_id == item["id"]
                    and (not record.expires_at or record.expires_at > time.time())
                    and record.version == item["version"] and memory_content_hash(record.content) == item["content_hash"]):
                records.append(record)
        current = self.store.manifest()
        if current is None or current["operation_id"] != root["operation_id"]:
            raise MemoryReadIndexGenerationConflict("MEMORY_READ_INDEX_GENERATION_CONFLICT")
        blocked = self.store.tombstones.load()
        return [record for record in records if not self.store.tombstones.excludes(
            record.entry_id, memory_content_hash(record.content), records=blocked)]

    # LLM: 删除后整份派生库可丢弃，清空字节后移除精确DB及journal/WAL/SHM；调用方持formal锁，无常驻连接。
    # 函数用途: 让已遗忘正文不留在SQLite空闲页或FTS历史段中。
    def invalidate(self):
        # LLM: 固定四个本库路径的擦除进入当前C末门，符号链接/多链接拒绝，不扩大到其它owner。
        # 函数用途: 安全擦除本次删除的派生索引文件。
        def erase():
            for suffix in ("", "-journal", "-wal", "-shm"):
                path = self.path.with_name(self.path.name + suffix)
                try:
                    descriptor = os.open(path, os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0))
                except FileNotFoundError:
                    continue
                try:
                    metadata = os.fstat(descriptor)
                    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                        raise NoFollowPathError("invalid memory read index")
                    os.ftruncate(descriptor, 0)
                    os.fsync(descriptor)
                    path.unlink()
                finally:
                    os.close(descriptor)
        if self.coordinator_io:
            execute_global_write(erase, require_scope=True)
        else:
            erase()


# LLM: 原词法分词与index_text为唯一语义；额外hex token只用于FTS候选，不作为正式正文。
# 函数用途: 将完整可检索属性及中文短词投影进现有trigram索引。
def _lexical_projection(record):
    from ..retrieval.lexical import tokenize
    from ..retrieval.text_vector_cache import index_text

    text = index_text(record.content, record.attributes)
    return fold_key(text) + " " + " ".join("z" + token.encode().hex() for token in dict.fromkeys(tokenize(text)))
