"""完成学习责任的可重建定位投影；任务 JSON 仍是唯一状态权威。"""
from __future__ import annotations

import hashlib

from .session_list_index import SessionListIndex, _stamp


# LLM: 只选未发布 wake 或需要修旧读取器的完成任务，不把所有终态放入维护热集合。
# 函数用途: 判断 canonical 任务是否仍欠一次学习交接修复。
def learning_repair_owed(payload):
    metadata = payload.get("metadata") or {}
    enqueue = metadata.get("task_learning_enqueue") or {}
    return payload.get("status") == "done" and (
        enqueue.get("status") == "pending" and not metadata.get("task_learning_wake_id")
        or enqueue.get("status") == "ineligible" and enqueue.get("reason") == "canonical_completion_reader_unbound")


# LLM: 复用原私有 SQLite、锁忙/损坏及有界重建协议；候选只保留 owed，另存全量定位 stamp，不存执行许可。
# 类用途: 为每次维护定位至多一页学习待办，不遍历已经结清的终态历史。
class SessionTaskPendingIndex(SessionListIndex):
    # LLM: 与公开任务列表分库，不能污染其过滤键、游标或排序合同。
    # 函数用途: 绑定同一 canonical 目录的独立待办索引。
    def __init__(self, root, *, reader):
        super().__init__(root, kind="session_task", reader=reader)
        self.path = self.root.parent / f".{self.root.name}-learning-pending.sqlite3"
        self.scope = hashlib.sha256(str(self.path.resolve()).encode()).hexdigest()[:24]

    # LLM: 旧库没有非 owed 定位；同一原索引锁内幂等升级，重开有界回填而非宣称完整。
    # 函数用途: 建立只含路径、文件身份和代次的引用表，兼容既有待办数据库。
    def _open_projection(self):
        conn = super()._open_projection()
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS task_references "
                         "(path TEXT PRIMARY KEY, stamp TEXT, generation INTEGER)")
            if self._meta(conn, "references_schema") != "1":
                self._put_meta(conn, "references_schema", 1)
                self._put_meta(conn, "directory", "")
                self._put_meta(conn, "complete", 0)
                self._put_meta(conn, "offset", 0)
                self._put_meta(conn, "reference_probe_after", "")
            return conn
        except BaseException:
            conn.close()
            raise

    # LLM: 仍用原目录 stamp 和分批枚举；仅在一轮完整回填结束时清理失效定位，不给热页加全表扫描。
    # 函数用途: 随原回填代次清理全量引用表中已经删除的任务。
    def _refresh(self, conn):
        previous = (self._meta(conn, "complete", "0"), self._meta(conn, "generation", "0"))
        complete = super()._refresh(conn)
        current = (self._meta(conn, "complete", "0"), self._meta(conn, "generation", "0"))
        if current[0] == "1" and current != previous:
            conn.execute("DELETE FROM task_references WHERE generation<>?", (int(current[1]),))
        return complete

    # LLM: 非 owed 文件也可能被旧写者原位改成 owed；每页只探测两个定位，变化才读 canonical。
    # 函数用途: 用持久化 keyset 游标跨实例轮询全量引用，保留原候选分页与 revision 语义。
    def _probe_file_identities(self, conn):
        after = self._meta(conn, "reference_probe_after")
        rows = conn.execute("SELECT path,stamp FROM task_references WHERE path>? ORDER BY path LIMIT 2",
                            (after,)).fetchall()
        if not rows:
            rows = conn.execute("SELECT path,stamp FROM task_references ORDER BY path LIMIT 2").fetchall()
        changed = False
        for row in rows:
            path = self.root / row["path"]
            if _stamp(path) != row["stamp"]:
                self._record(conn, path, int(self._meta(conn, "generation", "0")))
                changed = True
        self._put_meta(conn, "reference_probe_after", rows[-1]["path"] if rows else "")
        if changed:
            self._put_meta(conn, "revision", int(self._meta(conn, "revision", "0")) + 1)
        return changed

    # LLM: 只从最新任务重算成员；同一索引锁内替换或移除，旧快照不能重新加入已结清任务。
    # 函数用途: 在提交和回填时同步一条学习责任引用。
    def _record(self, conn, path, generation):
        relative = str(path.relative_to(self.root))
        if not path.exists():
            conn.execute("DELETE FROM entries WHERE path=?", (relative,))
            conn.execute("DELETE FROM task_references WHERE path=?", (relative,))
            return
        version = _stamp(path)
        payload, error = self.reader(path)
        conn.execute("INSERT OR REPLACE INTO task_references VALUES (?,?,?)",
                     (relative, version, generation))
        if error:
            # 坏候选仍须报告，不能作为“已结清”的证明。
            conn.execute("INSERT OR REPLACE INTO entries VALUES (?,?,?,?,?)",
                         (relative, 0.0, "", version, generation))
        elif learning_repair_owed(payload):
            conn.execute("INSERT OR REPLACE INTO entries VALUES (?,?,?,?,?)",
                         (relative, float(payload.get("created_at") or 0), "", version, generation))
        else:
            conn.execute("DELETE FROM entries WHERE path=?", (relative,))
