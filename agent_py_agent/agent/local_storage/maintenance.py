
from __future__ import annotations

# LLM: 维护操作只改可重建投影与显式调用的本库事实；FTS 回填完成键须与实际回填同事务提交。
# LLM: C重建/显式事件裁剪只消费原scope固定事务及末写，不存source或恢复账；联验schema、事件锁序与partial。
# 模块用途: 提供索引维护、C完整重建与有界事件裁剪，不决定owner权限或新增批次状态。

"""implements LocalStore maintenance, statistics, reset, FTS rebuild, and integrity checks.

这个文件只管维护类动作。
比如重建 FTS、清空索引准备重建、统计每种来源有多少记录，以及检查正文文件有没有丢。
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..common.json_io import locked_json_path, write_private_text_file_atomic_unlocked
from ..common.nofollow_fs import ensure_private_dir


# LLM: 维护器通过 schema 提供的连接访问当前库，不缓存其它用户数据；显式 reset 保持旧契约。
# LLM: C仅rebuild与prune_events固定叶闭合，其它维护writer仍各守原真实链；事件只删本批实际DB删除的ID。
# 类用途: 管理全文索引与显式有界事件维护，保同库事务、原恢复标记和JSONL独有记录。
class LocalStoreMaintenanceMixin:

    # LLM: 显式有界维护；保护最新keep_last并严格早于before，仅删除本事务真正消失的ID对应JSONL行。
    #   默认SQL→事件锁保持原writer锁序；C沿事件锁→原短事务→短原子写，JSONL独有/坏行逐字节保留，失败保真实partial。
    # 函数用途: 裁剪一批过期事件，支持无写预演，不改schema或把JSONL当数据库投影重建。
    def prune_events(self, *, before: float, keep_last: int = 1000, max_delete: int = 5000,
                     dry_run: bool = False) -> dict[str, int | bool]:
        options = (float(before), max(0, int(keep_last)), max(1, int(max_delete)))
        with self._connection() as conn:
            selected = _select_prunable_event_ids(conn, *options)
        if dry_run or not selected:
            return _event_prune_result(len(selected), set(), 0, dry_run=bool(dry_run))
        if self.coordinator_io:
            with self._coordinator_events_lock():
                selected_count, deleted = self._coordinator_transaction(
                    lambda conn: _delete_prunable_events(conn, *options))
                removed = self._prune_event_jsonl(deleted)
        else:
            with self._connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                # 普通writer先取得SQLite写锁，再取JSONL锁，不能反向拿锁造成相互等待。
                with locked_json_path(self.events_path):
                    selected_count, deleted = _delete_prunable_events(conn, *options)
                    conn.commit()
                    removed = self._prune_event_jsonl(deleted)
        return _event_prune_result(selected_count, deleted, removed)

    # LLM: 调用方已持原事件文件锁且SQL已提交；仅滤本批真实删除ID，读/解析门外，C只有最后replace沿原_local_write。
    # 函数用途: 流式读取事件流水，保留无关、JSONL独有及不可解析行的原始字节后原子写回。
    def _prune_event_jsonl(self, target_ids: set[str]) -> int:
        if not target_ids or not self.events_path.exists():
            return 0
        retained, removed = [], 0
        with self.events_path.open("r", encoding="utf-8", errors="surrogateescape", newline="") as handle:
            for line in handle:
                if _pruned_event_line(line, target_ids):
                    removed += 1
                else:
                    retained.append(line)
        if removed:
            content = "".join(retained)
            # LLM: 只写原events_path已准备正文；原锁仍持有，C只允许一次replace。
            # 函数用途: 私有原子替换过滤后的事件流水，保留所有未删除行字节。
            def operation():
                write_private_text_file_atomic_unlocked(self.events_path, content, preserve_bytes=True,
                    **({"replace_once": True} if self.coordinator_io else {}))
            if self.coordinator_io:
                self._local_write(operation)
            else:
                operation()
        return removed

    # LLM: 读取正文权威回填投影，开始即持写锁；完成键与回填同提交，中途失败不谎报已完成。
    # LLM: C门外准备全行/正文，BEGIN后精确CAS再原完整事务；默认原算法不变，O(N)本地门不假称恒短。
    # 函数用途: 原子重建FTS，C仅在同库/正文快照仍准确时清pending。
    def rebuild_fts(self) -> int:
        """用 `records` 表和文件系统内容重建 FTS5 索引。"""

        if self.coordinator_io:
            return self._rebuild_fts_coordinator()

        if not self.fts_available:
            return 0
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM records_fts")
            rows = conn.execute("SELECT id, title, content_path, content_preview FROM records").fetchall()
            count = 0
            for row in rows:
                content = self._read_content(row)
                self._replace_fts_row(conn, row["id"], row["title"], content)
                count += 1
            self._record_event(conn, "fts_rebuilt", "", {"count": count})
            self._record_fts_rebuild_complete(conn)
            conn.commit()
        self._fts_needs_rebuild = False
        return count

    # LLM: 原快照只活于本次栈；预持事件锁后完整BEGIN/CAS/DML/event/marker/commit同门，无跨批影子状态。
    # 函数用途: 用已准备正文原子回填C的同一个FTS库，变化则DELETE前忙退出。
    def _rebuild_fts_coordinator(self):
        if not self.fts_available:
            return 0
        snapshot = self._prepare_fts_rebuild()

        # LLM: SQLite写锁已try取得，先全行/状态/正文代次CAS；不在门内重读正文或发网络。
        # 函数用途: 验准准备快照后按原相序执行完整FTS本地事务。
        def operation(conn):
            self._validate_fts_rebuild(conn, snapshot)
            conn.execute("DELETE FROM records_fts")
            for record_id, title, content in snapshot.contents:
                self._replace_fts_row(conn, record_id, title, content)
            count = len(snapshot.contents)
            self._record_event(conn, "fts_rebuilt", "", {"count": count},
                               append_records=self._append_coordinator_event_records)
            self._record_fts_rebuild_complete(conn)
            return count
        with self._coordinator_events_lock():
            count = self._coordinator_transaction(operation)
        self._fts_needs_rebuild = False
        return count

    # LLM: 全列/raw类型及正文读取都在Root外；原_read_content与缺失preview沿用，读取摘要只属本次准备事实。
    # 函数用途: 从同库记录和稳定正文代次准备FTS输入，不将hash替代正文或row字段。
    def _prepare_fts_rebuild(self):
        identity = _rebuild_db_identity(self.db_path)
        with self._connection() as conn:
            columns, rows = _read_rebuild_rows(conn)
            state = _read_rebuild_state(conn)
        if _rebuild_db_identity(self.db_path) != identity:
            raise BlockingIOError("local_store_rebuild_database_changed")
        contents, versions = [], []
        content_bytes = 0
        for values in rows:
            row = dict(zip(columns, values))
            path = self._resolve_content_path(row["content_path"])
            before = _rebuild_body_version(path)
            content = self._read_content(row)
            if _rebuild_body_version(path) != before:
                raise BlockingIOError("local_store_rebuild_content_changed")
            raw = _rebuild_content_bytes(content)
            contents.append((row["id"], row["title"], content))
            versions.append((path, before, hashlib.sha256(raw).hexdigest()))
            content_bytes += len(raw)
        return _FtsRebuildSnapshot(columns, rows, state, identity,
                                   tuple(contents), tuple(versions), content_bytes)

    # LLM: 原单txn已BEGIN，完整raw比较不只count/hash/time；代次末核守原原子正文writer边界，不声称约束任意外部原地篡改。
    # 函数用途: 在任何FTS删除前拒绝库/记录/正文已经变化的准备快照。
    def _validate_fts_rebuild(self, conn, snapshot):
        columns, rows = _read_rebuild_rows(conn)
        if (columns != snapshot.columns or not _typed_rebuild_rows_equal(rows, snapshot.rows)
                or not _typed_rebuild_rows_equal(_read_rebuild_state(conn), snapshot.state)
                or _rebuild_db_identity(self.db_path) != snapshot.db_identity):
            raise BlockingIOError("local_store_rebuild_snapshot_changed")
        for path, version, _digest in snapshot.versions:
            if _rebuild_body_version(path) != version:
                raise BlockingIOError("local_store_rebuild_content_changed")

    def reset(self, *, remove_content_files: bool = False, reset_events_file: bool = True) -> None:
        with self._connection() as conn:
            if self.fts_available:
                conn.execute("DELETE FROM records_fts")
            conn.execute("DELETE FROM events")
            conn.execute("DELETE FROM records")
            conn.commit()
        if reset_events_file:
            ensure_private_dir(self.events_path.parent)
            self.events_path.write_text("", encoding="utf-8")
        if remove_content_files:
            self._remove_content_files()

    def _remove_content_files(self) -> None:
        """Best-effort cleanup for content files after database reset."""
        if not self.files_dir.exists():
            return
        for path in self.files_dir.glob("*.txt"):
            try:
                path.unlink()
            except OSError:
                continue

    def count_records(self, *, source_type: str | None = None) -> int:
        """统计记录数，可按 source_type 过滤。"""

        where, params = self._record_filters(source_type=source_type)
        with self._connection() as conn:
            return int(conn.execute(f"SELECT COUNT(*) FROM records {where}", params).fetchone()[0])

    def source_counts(self) -> dict[str, int]:
        """按 source_type 汇总记录数。"""

        with self._connection() as conn:
            rows = conn.execute(
                "SELECT source_type, COUNT(*) AS count FROM records GROUP BY source_type ORDER BY source_type"
            ).fetchall()
        return {str(row["source_type"]): int(row["count"]) for row in rows}

    def missing_content_files(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """检查 records 指向的正文文件是否还存在。"""

        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT id, source_type, source_id, title, content_path
                FROM records
                ORDER BY updated_at DESC
                """
            ).fetchall()
        missing: list[dict[str, Any]] = []
        for row in rows:
            path = self._resolve_content_path(row["content_path"])
            if path.exists():
                continue
            missing.append(
                {
                    "id": row["id"],
                    "source_type": row["source_type"],
                    "source_id": row["source_id"],
                    "title": row["title"],
                    "content_path": str(path),
                }
            )
            if limit > 0 and len(missing) >= limit:
                break
        return missing

    def stats(self) -> dict[str, Any]:
        """返回本地事实源的当前状态。"""

        with self._connection() as conn:
            record_count = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
            event_count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        return {
            "db_path": str(self.db_path),
            "files_dir": str(self.files_dir),
            "events_path": str(self.events_path),
            "fts5_enabled": self.fts_available,
            "record_count": record_count,
            "event_count": event_count,
        }


# LLM: 只读当前事务中的events；时间相等时沿原timeline的seq定序，不以JSONL、显示时间或条数猜删除集合。
# 函数用途: 选出不在最新保护窗口内、严格过期且至多max_delete条的事件ID。
def _select_prunable_event_ids(conn, before: float, keep_last: int, max_delete: int) -> set[str]:
    total = int(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])
    if total <= keep_last:
        return set()
    return {row[0] for row in conn.execute(
        "SELECT event_id FROM events WHERE created_at < ? AND seq NOT IN "
        "(SELECT seq FROM events ORDER BY created_at DESC, seq DESC LIMIT ?) "
        "ORDER BY created_at ASC, seq ASC LIMIT ?", (before, keep_last, max_delete))}


# LLM: 调用方已BEGIN IMMEDIATE；每批900参数兼容SQLite变量上限，删除后重查避免触发器忽略的行被误清JSONL。
# 函数用途: 重核本批选择并删除，返回实际选择数量和真正从数据库消失的精确ID集合。
def _delete_prunable_events(conn, before: float, keep_last: int, max_delete: int) -> tuple[int, set[str]]:
    selected = _select_prunable_event_ids(conn, before, keep_last, max_delete)
    identifiers, deleted = sorted(selected), set()
    for offset in range(0, len(identifiers), 900):
        batch = identifiers[offset:offset + 900]
        placeholders = ",".join("?" for _ in batch)
        conn.execute(f"DELETE FROM events WHERE event_id IN ({placeholders})", batch)
        remaining = {row[0] for row in conn.execute(
            f"SELECT event_id FROM events WHERE event_id IN ({placeholders})", batch)}
        deleted.update(set(batch) - remaining)
    return len(selected), deleted


# LLM: 标量、坏JSON、递归解码超限和非字符串ID都保留；不按时间/正文猜归属，不吞其它存储或来源异常。
# 函数用途: 只把成功解码且精确命中实际删除ID的事件行过滤掉。
def _pruned_event_line(line: str, target_ids: set[str]) -> bool:
    try:
        row = json.loads(line)
    except (ValueError, RecursionError):
        return False
    return isinstance(row, dict) and isinstance(row.get("event_id"), str) and row["event_id"] in target_ids


# LLM: 只汇报实际选择、提交删除与JSONL过滤数量，不把跨介质partial伪装成全事务回滚。
# 函数用途: 生成有界事件裁剪的稳定公开回执。
def _event_prune_result(selected: int, deleted: set[str], removed: int, *, dry_run: bool = False) -> dict[str, int | bool]:
    return {"ok": True, "dry_run": dry_run, "selected": selected, "deleted_db": len(deleted), "deleted_jsonl": removed}


# LLM: 只保存本次准确原表/正文输入，不是缓存、恢复表或权限；完成后随调用栈释放，不保存source。
# 类用途: 将门外准备的FTS参数和完整CAS事实传入同一次本地事务。
@dataclass(frozen=True)
class _FtsRebuildSnapshot:
    columns: tuple
    rows: tuple
    state: tuple
    db_identity: tuple
    contents: tuple
    versions: tuple
    content_bytes: int


# LLM: SELECT全列按原唯一id排序，不归一化字段值；新列/任一字段/类型变化都会参与CAS。
# 函数用途: 读取同库当前完整records快照，供门外准备与门内精确复核共用。
def _read_rebuild_rows(conn):
    cursor = conn.execute("SELECT * FROM records ORDER BY id")
    columns = tuple(column[0] for column in cursor.description)
    return columns, tuple(tuple(row) for row in cursor.fetchall())


# LLM: 只复核原schema摘要/FTS版本/待回填键及真实虚表声明，不新建状态或把无关event误当records变化。
# 函数用途: 取得这次重建所依赖的实际库内结构状态。
def _read_rebuild_state(conn):
    rows = conn.execute(
        "SELECT key, value FROM metadata WHERE key IN (?, ?, ?) ORDER BY key",
        ("sqlite_schema_fingerprint", "records_fts_schema_version", "records_fts_rebuild_pending"),
    ).fetchall()
    definition = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='records_fts'"
    ).fetchall()
    return tuple(tuple(row) for row in [*rows, *definition])


# LLM: sqlite返回的每个标量原类型和值都比较，不将1/1.0、None、bytes/text或JSON字符串折叠。
# 函数用途: 判断两次完整SQL读取是否精确相同。
def _typed_rebuild_rows_equal(current, original):
    return (len(current) == len(original) and all(
        len(left) == len(right) and all(type(a) is type(b) and a == b for a, b in zip(left, right))
        for left, right in zip(current, original)))


# LLM: 仅同路径当前库文件身份用于拒绝换库，不把mtime变化或无关event当全文记录变化。
# 函数用途: 给单次重建记录真实数据库文件身份。
def _rebuild_db_identity(path):
    info = path.stat()
    return info.st_dev, info.st_ino


# LLM: 精确捕获原子正文writer的实际代次，缺失/断链也有明确事实；不以None表示稳定正文。
# 函数用途: 比较原读取前后及最后事务时的正文文件状态。
def _rebuild_body_version(path: Path):
    try:
        info = path.stat()
    except FileNotFoundError:
        try:
            link = path.lstat()
        except FileNotFoundError:
            return ("missing",)
        return ("missing_target", *_rebuild_stat_generation(link))
    return ("present", *_rebuild_stat_generation(info))


# LLM: 文件代次只做变化否定，摘要来自实际读取；不宣称防住任意恶意恢复元数据的外部写者。
# 函数用途: 取得原要求的dev/ino/size/mtime/ctime版本元组。
def _rebuild_stat_generation(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


# LLM: 只统计并摘要原读取输入，不改变传给FTS的实际content值或原preview fallback类型。
# 函数用途: 为本次测试/快照记录实际读取正文的字节数与摘要输入。
def _rebuild_content_bytes(content):
    if isinstance(content, bytes):
        return content
    return str(content).encode("utf-8", "replace")
