
from __future__ import annotations

# LLM: 维护操作只改可重建投影与显式调用的本库事实；FTS 回填完成键须与实际回填同事务提交。
# LLM: C只消费当次scope；事件删除与pending同事务，JSONL成功后才清账，联验events锁序与partial恢复。
# 模块用途: 提供索引维护、C完整重建和可恢复的流式事件裁剪，不决定owner权限。

"""implements LocalStore maintenance, statistics, reset, FTS rebuild, and integrity checks.

这个文件只管维护类动作。
比如重建 FTS、清空索引准备重建、统计每种来源有多少记录，以及检查正文文件有没有丢。
"""

import hashlib
import json
import os
import tempfile
import time
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

from ..common.json_io import (
    _sync_parent_directory,
    locked_json_path,
    write_private_text_file_atomic_unlocked,
)
from ..common.nofollow_fs import ensure_private_dir

_EVENTS_PRUNE_PENDING_KEY = "events_prune_pending_ids"
_DEFAULT_WRITE_ATOMIC_UNLOCKED = write_private_text_file_atomic_unlocked
STEADY_STATE_EVENTS_KEEP_LAST = 20000
STEADY_STATE_EVENTS_MAX_DELETE = 5000


# LLM: 维护器通过 schema 提供的连接访问当前库，不缓存其它用户数据；显式 reset 保持旧契约。
# LLM: C仅rebuild与prune_events固定叶闭合，其它维护writer仍各守原真实链；事件只删本批实际DB删除的ID。
# 类用途: 管理全文索引与有界事件维护，保同库事务、恢复账本和JSONL独有记录。
class LocalStoreMaintenanceMixin:

    # LLM: pending与真实DB删除同事务；普通SQL→文件锁，C文件锁→短事务；成功后按ID清账且不丢并发新批次。
    # 函数用途: 裁剪一批旧事件并恢复上次未完成的JSONL清理，预演不写、失败留下准确恢复ID。
    def prune_events(self, *, before: float, keep_last: int = 1000, max_delete: int = 5000,
                     dry_run: bool = False) -> dict[str, int | bool]:
        options = (float(before), max(0, int(keep_last)), max(1, int(max_delete)))
        with self._connection() as conn:
            selected = _select_prunable_event_ids(conn, *options)
            pending = _read_pending_event_ids(conn)
        if dry_run or not (selected or pending):
            return _event_prune_result(len(selected), set(), 0, dry_run=bool(dry_run))
        if self.coordinator_io:
            with self._coordinator_events_lock():
                selected_count, deleted, targets = self._coordinator_transaction(
                    lambda conn: _prepare_event_prune(conn, options))
                removed = self._prune_event_jsonl(set(targets))
            if targets:
                self._coordinator_transaction(lambda conn: _clear_pending_event_ids(conn, targets))
        else:
            with self._connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                # 普通writer先取得SQLite写锁，再取JSONL锁，不能反向拿锁造成相互等待。
                with locked_json_path(self.events_path):
                    selected_count, deleted, targets = _prepare_event_prune(conn, options)
                    conn.commit()
                    removed = self._prune_event_jsonl(set(targets))
            # 先释放文件锁再申请清账写事务，避免与持DB锁等待追加的writer互锁。
            if targets:
                _clear_pending_events_transaction(self, targets)
        return _event_prune_result(selected_count, deleted, removed)

    # LLM: 只由显式/后台维护周期调用，每次至多max_delete；不在单事件热路径扫描，不VACUUM或清理records正文。
    # 函数用途: 按默认保留两万条、每批最多五千条执行一次可恢复维护。
    def prune_events_steady_state(self, *, keep_last: int = STEADY_STATE_EVENTS_KEEP_LAST,
                                 max_delete: int = STEADY_STATE_EVENTS_MAX_DELETE,
                                 before: float | None = None, dry_run: bool = False) -> dict[str, int | bool]:
        return self.prune_events(before=time.time() if before is None else before,
                                 keep_last=keep_last, max_delete=max_delete, dry_run=dry_run)

    # LLM: 调用方已持同events_path锁且SQL/pending已提交；文件读/解析门外，只有最后发表沿C原_local_write。
    # 函数用途: 以逐行私有临时文件保留无关和坏行原字节，成功才返回，失败由pending供下次重试。
    def _prune_event_jsonl(self, target_ids: set[str]) -> int:
        if not target_ids or not self.events_path.exists():
            return 0
        return _stream_prune_event_jsonl(self, target_ids)

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


# LLM: pending只接受非空字符串ID数组；坏账拒绝继续删除，避免覆盖不可解释的恢复状态。
# 函数用途: 从同库metadata读取上次尚未从JSONL移除的精确事件ID。
def _read_pending_event_ids(conn) -> list[str]:
    row = conn.execute("SELECT value FROM metadata WHERE key=?", (_EVENTS_PRUNE_PENDING_KEY,)).fetchone()
    if row is None:
        return []
    pending = json.loads(row[0])
    if not isinstance(pending, list) or any(not isinstance(item, str) or not item for item in pending):
        raise ValueError("invalid_events_prune_pending_ids")
    return list(dict.fromkeys(pending))


# LLM: 只在已有写事务内更新固定metadata键；空账删除键，不碰schema/FTS及其它维护标记。
# 函数用途: 将仍待擦除的事件ID数组保存到原库的恢复账本。
def _write_pending_event_ids(conn, pending):
    if pending:
        conn.execute("INSERT INTO metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (_EVENTS_PRUNE_PENDING_KEY, json.dumps(pending)))
    else:
        conn.execute("DELETE FROM metadata WHERE key=?", (_EVENTS_PRUNE_PENDING_KEY,))


# LLM: 调用方已BEGIN；先读旧pending，再删并重核SQLite实际消失ID；两者与账本原子提交。
# 函数用途: 准备一次可恢复的事件清理批次，触发器忽略的行绝不进入待擦除集。
def _prepare_event_prune(conn, options):
    pending = _read_pending_event_ids(conn)
    selected, deleted = _delete_prunable_events(conn, *options)
    targets = list(dict.fromkeys([*pending, *sorted(deleted)]))
    if targets:
        _write_pending_event_ids(conn, targets)
    return selected, deleted, targets


# LLM: JSONL已成功处理targets；重新读取当前账，只减本批ID以保留文件锁释放后并发登记的新批次。
# 函数用途: 清除已经完成的恢复责任，不误删其它维护者的新挂起ID。
def _clear_pending_event_ids(conn, targets):
    completed = set(targets)
    remaining = [item for item in _read_pending_event_ids(conn) if item not in completed]
    _write_pending_event_ids(conn, remaining)


# LLM: 普通模式必须在释放events文件锁之后才能重取SQLite写锁；BEGIN下读改写账本防并发丢更新。
# 函数用途: 用独立短事务完成普通模式的恢复清账，失败保留可幂等重试的pending。
def _clear_pending_events_transaction(store, targets):
    with store._connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        _clear_pending_event_ids(conn, targets)
        conn.commit()


# LLM: 原输入按surrogateescape/newline空串读取，再按JSONL的LF边界解析；内存只随单记录及有界ID集合变化。
# 函数用途: 将未命中的行逐字节流式复制到同目录暂存文件，不拼接整份流水。
def _copy_retained_event_lines(path, retained, target_ids):
    removed = 0
    with path.open("r", encoding="utf-8", errors="surrogateescape", newline="") as handle:
        for line in _event_jsonl_lines(handle):
            if _pruned_event_line(line, target_ids):
                removed += 1
            else:
                retained.write(line)
    return removed


# LLM: newline空串的裸CR分片不能当独立JSONL记录；仅暂存当前LF记录，不缓存其它保留行。
# 函数用途: 拼回被通用换行拆开的坏记录，防止合法前缀被当成目标行误删。
def _event_jsonl_lines(handle):
    fragments = []
    for fragment in handle:
        fragments.append(fragment)
        if fragment.endswith("\n"):
            yield "".join(fragments)
            fragments.clear()
    if fragments:
        yield "".join(fragments)


# LLM: 只替换同events_path且调用者持原锁；生产路径不读回暂存正文，C仅一次replace沿原末门。
# 函数用途: 原子发表已同步的流式文件；保留既有替换函数的monkeypatch故障注入合同。
def _publish_pruned_event_file(store, temporary):
    if write_private_text_file_atomic_unlocked is not _DEFAULT_WRITE_ATOMIC_UNLOCKED:
        # 测试替身兼容路径允许整份读回，默认生产路径不走这里。
        with temporary.open("r", encoding="utf-8", errors="surrogateescape", newline="") as handle:
            content = handle.read()
        operation = partial(write_private_text_file_atomic_unlocked,
            store.events_path, content, preserve_bytes=True,
            **({"replace_once": True} if store.coordinator_io else {}))
    else:
        operation = partial(os.replace, temporary, store.events_path)
    if store.coordinator_io:
        store._local_write(operation)
    else:
        operation()


# LLM: 同目录mkstemp独占0600，文件fsync后替换；无新命中也补父目录fsync，覆盖上次replace后同步失败窗口。
# 函数用途: 以不随文件行数增长的内存执行重写，无命中时不替换原文件。
def _stream_prune_event_jsonl(store, target_ids):
    descriptor, name = tempfile.mkstemp(prefix=f".{store.events_path.name}.", suffix=".tmp",
                                        dir=store.events_path.parent)
    temporary = Path(name)
    primary = None
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", errors="surrogateescape", newline="") as retained:
            removed = _copy_retained_event_lines(store.events_path, retained, target_ids)
            if removed:
                retained.flush()
                os.fsync(retained.fileno())
        if removed:
            _publish_pruned_event_file(store, temporary)
        _sync_parent_directory(store.events_path)
        return removed
    except BaseException as exc:
        primary = exc
        raise
    finally:
        _discard_event_temp(temporary, primary)


# LLM: 清理失败不盖掉原始读取/同步/撤权异常；无主错误时清理失败照常传播，pending仍不清。
# 函数用途: 尽力删除本次暂存文件，并保留真正导致裁剪失败的异常身份。
def _discard_event_temp(temporary, primary):
    try:
        temporary.unlink(missing_ok=True)
    except BaseException:
        if primary is None:
            raise


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
