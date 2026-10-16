# LLM: SQLite 的 in_transaction 不覆盖未结束 SELECT；弱跟踪游标，归还写连接还必须清空已回填的WAL帧，防旧帧重放覆盖替换库。
# 模块用途: 为可复用连接提供可证明的语句清理，归还时释放 WAL 读锁。

from __future__ import annotations

import sqlite3
from weakref import WeakSet


# LLM: 直接 cursor.execute 的 DDL 没有 total_changes 增量；语句执行时记录结构化结果类型，游标被 GC 也不丢写入标记。
# 类用途: 为连接记录需要收口 WAL 的语句，不解析 SQL 文本。
class _TrackedSQLiteCursor(sqlite3.Cursor):
    # LLM: SQLite 的 description 为 None 表示无结果列；事务与参数仍完全交标准库。
    # 函数用途: 执行 SQL 并记录可能写库的语句。
    def execute(self, sql, parameters=()):
        result = super().execute(sql, parameters)
        self.connection._checkpoint_needed |= self.description is None
        return result

    # LLM: 批量语句可能写库，即使之后游标被释放也必须保留归还清理事实。
    # 函数用途: 执行批量 SQL 并通知连接收口 WAL。
    def executemany(self, sql, parameters):
        self.connection._checkpoint_needed = True
        return super().executemany(sql, parameters)

    # LLM: 脚本可能含隐式提交和 DDL，不能只依赖 total_changes。
    # 函数用途: 执行脚本并保守记录写入。
    def executescript(self, sql):
        self.connection._checkpoint_needed = True
        return super().executescript(sql)


# LLM: 保持 sqlite3.Connection 类型与提交语义；弱引用避免长回合累积游标，清理失败由作用域退役整条连接。
# 类用途: 记录连接创建的游标，确保下一次借用之前没有未结束语句。
class TrackedSQLiteConnection(sqlite3.Connection):
    # LLM: 继承标准库原构造接口，不引入可变服务参数；首次使用才创建弱集合，不另开连接/事务。
    # 函数用途: 为已打开的物理连接初始化一次语句跟踪状态。
    def _ensure_cursor_tracking(self) -> None:
        if hasattr(self, "_lease_cursors"):
            return
        self._lease_cursors: WeakSet[sqlite3.Cursor] = WeakSet()
        self._checkpoint_needed = False
        self._checkpoint_changes = 0

    # LLM: 即使调用方提供游标工厂也纳入跟踪；游标仍由 SQLite 执行，不改变 SQL 或参数。
    # 函数用途: 创建并登记一条可能占用读锁的语句。
    def cursor(self, factory=None):
        self._ensure_cursor_tracking()
        cursor = super().cursor(factory or _TrackedSQLiteCursor)
        self._checkpoint_needed |= factory is not None
        self._lease_cursors.add(cursor)
        return cursor

    # LLM: 标准库快捷执行会绕过 Python cursor 覆盖，必须显式走登记入口；事务行为不变。
    # 函数用途: 执行单条 SQL 并返回已跟踪游标。
    def execute(self, sql, parameters=()):
        cursor = self.cursor().execute(sql, parameters)
        self._checkpoint_needed |= cursor.description is None
        return cursor

    # LLM: 批量执行仍由标准库处理参数与事务，只增加语句归还清理。
    # 函数用途: 执行批量 SQL 并返回已跟踪游标。
    def executemany(self, sql, parameters):
        self._checkpoint_needed = True
        return self.cursor().executemany(sql, parameters)

    # LLM: 脚本的隐式提交契约仍属标准库，不能由复用模块改写。
    # 函数用途: 执行 SQL 脚本并登记其游标。
    def executescript(self, sql):
        self._checkpoint_needed = True
        return self.cursor().executescript(sql)

    # LLM: 归还期间关闭所有仍存活游标，包含只读 SELECT；不提交，异常须交给调用方退役连接。
    # 函数用途: 结束本次借用的语句并释放 WAL 读锁。
    def close_lease_cursors(self) -> None:
        self._ensure_cursor_tracking()
        for cursor in tuple(self._lease_cursors):
            self._checkpoint_needed |= cursor.description is None
            cursor.close()
        self._lease_cursors.clear()
        self._checkpoint_idle_writes()

    # LLM: PASSIVE回填不清空旧帧，主库替换后仍可能被重放；有帧时再零忙等截断，失败就退役，不手删sidecar。
    #   只读无变更不checkpoint；业务busy_timeout仅在截断期间临时置零，随后恢复，不改变提交/回滚。
    # 函数用途: 收口本次写入的 WAL 页，证明不了干净就交由作用域关闭连接。
    def _checkpoint_idle_writes(self) -> None:
        if not self._checkpoint_needed and self.total_changes == self._checkpoint_changes:
            return
        busy, frames, completed = self._query_pragma("PRAGMA wal_checkpoint(PASSIVE)")
        if busy or frames != completed:
            raise sqlite3.OperationalError("WAL pages remain pending at lease return")
        if frames > 0:
            self._truncate_idle_wal()
        self._checkpoint_needed = False
        self._checkpoint_changes = self.total_changes

    # LLM: 查询只用于内部固定PRAGMA，结果按值归一化，不用sqlite3.Row对象身份判断成功；游标无条件关闭。
    # 函数用途: 读取一行SQLite维护结果，并关闭临时游标。
    def _query_pragma(self, statement: str) -> tuple:
        cursor = super().execute(statement)
        try:
            return tuple(cursor.fetchone())
        finally:
            cursor.close()

    # LLM: 最新帧读锁也会阻止TRUNCATE，不能等业务30秒；临时零忙等，原超时无条件恢复，失败交scope退役。
    # 函数用途: 非阻塞清空已提交的WAL帧，避免缓存连接留下可重放的旧库页。
    def _truncate_idle_wal(self) -> None:
        timeout, = self._query_pragma("PRAGMA busy_timeout")
        super().execute("PRAGMA busy_timeout=0").close()
        try:
            result = self._query_pragma("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            super().execute(f"PRAGMA busy_timeout={timeout}").close()
        if result != (0, 0, 0):
            raise sqlite3.OperationalError("WAL frames cannot be reset at lease return")
