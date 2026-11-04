# LLM: 每个逻辑库独占永久屏障；不是可丢弃搜索投影。只保存删除身份/hash，不保存被删正文。
# 模块用途: 用有版本的SQLite键索引代替随历史删除量增长的整份JSON重写。
from __future__ import annotations

import json
import os
import sqlite3
import stat
from contextlib import closing, contextmanager
from pathlib import Path
from uuid import uuid4

from ..common.nofollow_fs import NoFollowPathError, open_directory_beneath


# LLM: 短事务内的两类主键查询，不遍历历史删除；实例不保留连接或跨请求缓存。
# 类用途: 管理一个正式库的永久删除身份和正文hash。
class StorageTombstoneIndex:
    batch_size = 1000

    # LLM: 只绑定规范库路径及宿主派生owner，构造没有I/O副作用。
    # 函数用途: 固定本库的SQLite屏障文件。
    def __init__(self, memory_path, owner):
        self.path = Path(memory_path).with_name(Path(memory_path).name + ".storage_tombstones.sqlite3")
        self.owner = owner

    # LLM: 写者已持formal锁；缺库先原子发表完整schema，只读不创建文件，存量坏权威仍拒绝且不重建覆盖。
    # 函数用途: 打开本次短连接，拒绝链接或异常文件，不留下未初始化的正式库。
    def _connect(self, *, write=False):
        parent = open_directory_beneath(self.path.parent, (), require_dir_fd=True)
        try:
            try:
                metadata = os.stat(self.path.name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                if not write:
                    raise
                self._initialize_file(parent)
                metadata = os.stat(self.path.name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise NoFollowPathError("invalid storage tombstone index")
            conn = sqlite3.connect(self.path.absolute().as_uri() + ("?mode=rw" if write else "?mode=ro"), uri=True, timeout=0)
            conn.row_factory = sqlite3.Row
            return conn
        finally:
            os.close(parent)

    # LLM: 仅缺库且已持formal锁时调用；完整schema/初始水位同事务，close+fsync后才replace，失败不动旧JSON删除权威。
    # 函数用途: 在同目录0600临时库中完成首次初始化，避免单次IO故障毒化所有后续读写。
    def _initialize_file(self, parent):
        name = f".{self.path.name}.{uuid4().hex}.tmp"
        descriptor = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
                             0o600, dir_fd=parent)
        try:
            uri = self.path.with_name(name).absolute().as_uri() + "?mode=rw"
            with closing(sqlite3.connect(uri, uri=True, timeout=0)) as conn, conn:
                conn.execute("BEGIN IMMEDIATE")
                self._initialize(conn)
            os.fsync(descriptor)
            os.replace(name, self.path.name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            os.close(descriptor)
            for suffix in ("", "-journal", "-wal", "-shm"):
                try:
                    os.unlink(name + suffix, dir_fd=parent)
                except FileNotFoundError:
                    pass

    # LLM: 元数据与屏障同事务；错误不解释为空账；缺库只有显式首次写入可初始化。
    # 函数用途: 验证owner、修订号和迁移水位。
    def _state(self, conn):
        row = conn.execute("SELECT value FROM metadata WHERE key='state'").fetchone()
        state = json.loads(row[0]) if row else None
        if (not isinstance(state, dict) or state.get("owner") != self.owner
                or type(state.get("revision")) is not int or state["revision"] < 0
                or type(state.get("complete")) is not bool
                or type(state.get("cursor")) is not int or state["cursor"] < 0
                or "migrated_revision" in state and (type(state["migrated_revision"]) is not int
                                                     or state["migrated_revision"] < 0)):
            raise ValueError("MEMORY_TOMBSTONES_UNREADABLE")
        return state

    # LLM: 只在无新式marker时核一个元数据行；完整SQL账存在即不能静默降级到旧空账。
    # 函数用途: 区分尚未完成的兼容迁移和已成为永久权威的索引。
    def has_committed_authority(self):
        try:
            with closing(self._connect()) as conn:
                return self._state(conn)["complete"]
        except FileNotFoundError:
            return False
        except sqlite3.Error as exc:
            raise ValueError("MEMORY_TOMBSTONES_UNREADABLE") from exc

    # LLM: 与has_committed_authority同口径只读一行；只有迁移完成后才交出水位，供marker缺失时证明索引恰是旧账导入。
    # 函数用途: 返回完整迁移的修订号、导入完成水位和来源身份。
    def migration_receipt(self) -> dict | None:
        try:
            with closing(self._connect()) as conn:
                state = self._state(conn)
        except FileNotFoundError:
            return None
        except sqlite3.Error as exc:
            raise ValueError("MEMORY_TOMBSTONES_UNREADABLE") from exc
        if not state["complete"]:
            return None
        return {"revision": state["revision"], "migrated_revision": state.get("migrated_revision"),
                "source_identity": state.get("source_identity")}

    # LLM: 完成前不能发放查询快照；根或JSON marker要求的修订号不允许回退。
    # 函数用途: 在同一只读事务中核屏障水位并交出点查接口。
    @contextmanager
    def snapshot(self, *, minimum_revision=0):
        try:
            with closing(self._connect()) as conn:
                conn.execute("BEGIN")
                state = self._state(conn)
                if not state["complete"]:
                    raise ValueError("MEMORY_TOMBSTONES_MIGRATING")
                if state["revision"] < minimum_revision:
                    raise ValueError("MEMORY_TOMBSTONES_UNREADABLE")
                yield _TombstoneSnapshot(conn, state["revision"])
        except sqlite3.Error as exc:
            raise ValueError("MEMORY_TOMBSTONES_UNREADABLE") from exc

    # LLM: 首次出生在私有暂存事务内；迁移重试幂等核表，查询不建表，错误不解释为空账。
    # 函数用途: 初始化屏障表及未完成迁移状态，不发表不完整删除快照。
    def _initialize(self, conn):
        conn.execute("CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS deleted_entries(entry_id TEXT PRIMARY KEY,version INTEGER NOT NULL,payload TEXT NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS deleted_hashes(content_hash TEXT PRIMARY KEY)")
        if conn.execute("SELECT 1 FROM metadata WHERE key='state'").fetchone() is None:
            conn.execute("INSERT INTO metadata VALUES ('state',?)", (json.dumps({
                "owner": self.owner, "revision": 0, "complete": False, "cursor": 0,
            }),))

    # LLM: 每批最多1000条，只改受影响ID/hash；历史hash只增不减，较旧版本不得覆盖新删除回执。
    # 函数用途: 合并本批删除并返回是否有新屏障事实。
    def _merge(self, conn, records):
        from ._jsonl_maintenance import require_maintenance_event
        from .jsonl import _record_payload

        changed = False
        for record in records:
            require_maintenance_event(record)
            if (record.action != "remove" or record.attributes or record.content
                    or not isinstance(record.removed_content_hashes, (tuple, list))
                    or any(not isinstance(digest, str) for digest in record.removed_content_hashes)):
                raise ValueError("MEMORY_TOMBSTONES_UNREADABLE")
            old = conn.execute("SELECT version,payload FROM deleted_entries WHERE entry_id=?", (record.entry_id,)).fetchone()
            previous = _validated_tombstone_payload(record.entry_id, old[0], old[1]) if old else None
            selected = record if previous is None or record.version >= previous.version else previous
            payload = _record_payload(selected)
            payload["removed_content_hashes"] = sorted(set(record.removed_content_hashes) | set(previous.removed_content_hashes if previous else ()))
            raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            if old is None or old[1] != raw:
                conn.execute("INSERT OR REPLACE INTO deleted_entries VALUES (?,?,?)", (record.entry_id, selected.version, raw))
                conn.executemany("INSERT OR IGNORE INTO deleted_hashes VALUES (?)", ((digest,) for digest in payload["removed_content_hashes"]))
                changed = True
        return changed

    # LLM: 调用方持原formal锁，输入已校验；游标与本批键同事务，重启后不把半迁移当空屏障。
    # 函数用途: 将旧格式的一个记录切片耐久导入，不清掉已存在的删除。
    def migrate_step(self, records, *, source_identity, commit=None):
        def publish():
            with closing(self._connect(write=True)) as conn, conn:
                self._initialize(conn)
                state = self._state(conn)
                was_complete = state["complete"]
                if state.get("source_identity") != source_identity:
                    state.update(cursor=0, complete=False, source_identity=source_identity)
                    state.pop("migrated_revision", None)
                start = state["cursor"]
                if start > len(records):
                    raise ValueError("MEMORY_TOMBSTONES_UNREADABLE")
                batch = records[start:start + self.batch_size]
                if start < len(records) and not batch:
                    raise ValueError("MEMORY_TOMBSTONES_UNREADABLE")
                changed = self._merge(conn, batch)
                state.update(cursor=start + len(batch), complete=start + len(batch) == len(records),
                             revision=state["revision"] + int(changed))
                # 首次完成时记下导入水位；之后任何正式删除都会让修订号越过它，旧账不再能代表全集。
                if state["complete"] and not was_complete:
                    state["migrated_revision"] = state["revision"]
                conn.execute("UPDATE metadata SET value=? WHERE key='state'", (json.dumps(state),))
                return state
        return publish() if commit is None else commit(publish)

    # LLM: 屏障必须先于根；CAS修订号与键写同事务，事务中断无半批，调用方复用原配额与C末门。
    # 函数用途: 原子提交本次实际新增的删除键并返回新修订号。
    def publish(self, records, *, expected_revision, commit=None):
        records = tuple(records)
        if not records:
            with self.snapshot(minimum_revision=expected_revision) as snapshot:
                if snapshot.revision != expected_revision:
                    raise ValueError("MEMORY_GENERATION_CONFLICT")
                return snapshot.revision
        def publish():
            with closing(self._connect(write=True)) as conn, conn:
                conn.execute("BEGIN IMMEDIATE")
                state = self._state(conn)
                if not state["complete"] or state["revision"] != expected_revision:
                    raise ValueError("MEMORY_GENERATION_CONFLICT")
                changed = self._merge(conn, records)
                state["revision"] += int(changed)
                conn.execute("UPDATE metadata SET value=? WHERE key='state'", (json.dumps(state),))
                return state["revision"]
        return publish() if commit is None else commit(publish)


# LLM: 只活在一个SQLite读事务；小批查询O(B log D)，full_dict仅供原全量维护消费者。
# 类用途: 区分热路径点查与明确的全账枚举。
class _TombstoneSnapshot:
    # LLM: 不拥有连接，不能越过外层context使用。
    # 函数用途: 绑定已验证修订和本次只读连接。
    def __init__(self, conn, revision):
        self.conn, self.revision = conn, revision

    # LLM: 两个主键都以等值条件访问；不生成大IN或线性hash遍历。
    # 函数用途: 判断有界候选中哪些身份或正文已被永久遗忘。
    def blocked(self, pairs):
        return {entry_id for entry_id, digest in pairs if self.conn.execute(
            "SELECT 1 FROM deleted_entries WHERE entry_id=?", (entry_id,)).fetchone() is not None or (
                digest and self.conn.execute("SELECT 1 FROM deleted_hashes WHERE content_hash=?", (digest,)).fetchone() is not None)}

    # LLM: 只读取指定ID的删除回执，不把相同正文hash命中误报为同ID删除。
    # 函数用途: 给恢复和批次校验提供精确删除身份。
    def entries(self, ids):
        result = {}
        for entry_id in ids:
            row = self.conn.execute("SELECT version,payload FROM deleted_entries WHERE entry_id=?", (entry_id,)).fetchone()
            if row is not None:
                result[entry_id] = _validated_tombstone_payload(entry_id, row[0], row[1])
        return result

    # LLM: 保留StorageTombstones.load()原全量dict合同；冷维护显式承担全量工作，不用于召回。
    # 函数用途: 还原完整永久删除账。
    def full_dict(self):
        result = {}
        for row in self.conn.execute("SELECT entry_id,version,payload FROM deleted_entries ORDER BY entry_id"):
            result[row[0]] = _validated_tombstone_payload(*row)
        return result


# LLM: SQLite可读不等于永久账语义有效；冷枚举、点查回执和增量合并共用同一验真，不扫描无关删除行。
# 函数用途: 拒绝非remove、身份或修订不一致的删除载荷。
def _validated_tombstone_payload(entry_id, version, raw):
    from ._jsonl_maintenance import require_maintenance_event
    from .jsonl import _memory_record_from_obj

    payload = json.loads(raw)
    hashes = payload.get("removed_content_hashes", ()) if isinstance(payload, dict) else None
    if not isinstance(hashes, (list, tuple)) or any(not isinstance(digest, str) for digest in hashes):
        raise ValueError("MEMORY_TOMBSTONES_UNREADABLE")
    record = _memory_record_from_obj(payload)
    require_maintenance_event(record)
    if (record.entry_id != entry_id or type(version) is not int or record.version != version
            or record.action != "remove" or record.content or record.attributes):
        raise ValueError("MEMORY_TOMBSTONES_UNREADABLE")
    return record
