# LLM: 访问层不是权限边界或连接池；只将 sqlite3 公开接口绑定到一次借用，归还/跨线程/fork 后拒绝旧引用。
# 模块用途: 让缓存物理连接能够复用，而连接、游标和预先保存的方法不能跨租期操作它。

from __future__ import annotations

import sqlite3
from collections.abc import Callable

_SQLITE_FETCH_SIZE_DEFAULT = object()


# LLM: 沿用 sqlite3.ProgrammingError 兼容原调用方，code 是生命周期拒绝的结构化事实，不靠异常文案判断。
# 类用途: 明确报告访问层已归还或不属于当前执行者，不执行任何数据库操作。
class SQLiteLeaseExpiredError(sqlite3.ProgrammingError):
    code = 'SQLITE_LEASE_EXPIRED'


# LLM: 用户行工厂也只能看到租期游标，不能经回调参数取得物理连接；标准sqlite3.Row本身不向用户泄露游标。
# 类用途: 在SQLite调用用户行工厂时保持相同的连接/游标寿命边界。
class _LeaseRowFactory:
    # LLM: 原工厂是调用方的数据接口，不执行它；本包装不额外打开连接或保存库状态。
    # 函数用途: 记录行转换回调及其所属租期。
    def __init__(self, lease: SQLiteConnectionLease, factory: Callable):
        self.lease = lease
        self.factory = factory

    # LLM: 回调发生前再次核租期，传入访问层而非raw cursor；行数据及回调返回结果不改写。
    # 函数用途: 为有效借用的查询转换一行，并阻止回调逃逸物理连接。
    def __call__(self, cursor: sqlite3.Cursor, row: tuple):
        self.lease._check_active()
        return self.factory(SQLiteCursorLease(self.lease, cursor), row)


# LLM: 属性读取和保存的 bound method 调用各自校验；结果游标必须再包租期，不能从 cursor.connection 泄漏原连接。
# 类用途: 共用连接和游标的公开属性、方法转发，SQL/参数/提交仍由 sqlite3 原样处理。
class _SQLiteLeaseAccess:
    # LLM: 这里只转发SQLite数据属性；方法有明确签名并在调用时校验，未声明的方法不能暴露raw bound method。
    # 函数用途: 读取合法租期里的SQLite属性，保持行工厂的公开身份。
    def __getattr__(self, name: str):
        self._lease._check_active()
        value = getattr(self._target, name)
        if isinstance(value, _LeaseRowFactory):
            return value.factory
        if callable(value) and name not in ('row_factory', 'text_factory'):
            raise AttributeError(f'SQLite lease does not expose method {name}')
        return value

    # LLM: 私有字段只存访问层状态；公开赋值先核租期，行工厂回调也不得泄露物理游标/连接。
    # 函数用途: 将有效租期中的 SQLite 属性设置原样交给物理对象。
    def __setattr__(self, name: str, value: object) -> None:
        if name.startswith('_'):
            object.__setattr__(self, name, value)
            return
        self._lease._check_active()
        if name == 'row_factory':
            self._lease._row_factory_changed |= self._target is self._lease._target
            value = self._lease._bind_row_factory(value)
        setattr(self._target, name, value)

    # LLM: 调用参数是固定结构的tuple而非可变服务接口；检查先于物理调用，不重写SQL/事务，游标继续绑定租期。
    # 函数用途: 为已声明的SQLite方法执行租期检查和真实调用。
    def _call(self, name: str, arguments: tuple = ()):
        self._lease._check_active()
        value = getattr(self._target, name)(*arguments)
        if not isinstance(value, sqlite3.Cursor):
            return value
        if value is self._target:
            return self
        return SQLiteCursorLease(self._lease, value)


# LLM: 连接和游标都支持这组标准执行签名，不能通过通用可变参数接口或未经校验的raw方法扩大寿命。
# 类用途: 共用连接/游标执行SQL和关闭的明确接口，保存的方法也会在调用时检查租期。
class _SQLiteStatementLease(_SQLiteLeaseAccess):
    # LLM: SQL/参数原样交SQLite，不自动提交；Connection返回租期游标，Cursor保持execute返回自身的语义。
    # 函数用途: 在有效租期内执行一条语句。
    def execute(self, sql, parameters=()):
        return self._call('execute', (sql, parameters))

    # LLM: 批量参数由SQLite迭代，不能在归还后的保存方法上继续执行。
    # 函数用途: 在有效租期内执行批量语句。
    def executemany(self, sql, parameters):
        return self._call('executemany', (sql, parameters))

    # LLM: 脚本的隐式提交仍由SQLite决定，不解析SQL内容或改变事务规则。
    # 函数用途: 在有效租期内执行SQL脚本。
    def executescript(self, sql):
        return self._call('executescript', (sql,))

    # LLM: 显式close仍交SQLite；scope也可继续退役连接，但旧租期close不能操作下一借用。
    # 函数用途: 关闭当前有效租期所访问的SQLite对象。
    def close(self):
        return self._call('close')


# LLM: 每次借用新建一个访问层，物理连接仍只由 sqlite_scope 持有/归还/关闭；不增加永久缓存或业务身份。
# 类用途: 记录这一次借用是否仍有效，让归还后的旧连接引用立刻失效。
class SQLiteConnectionLease(_SQLiteStatementLease):
    # LLM: owned只核PID/线程，构造不打开连接/查库；保存原行工厂以在归还后解除本租期回调引用。
    # 函数用途: 将一条已打开连接交给当前借用者，而不直接暴露物理接口。
    def __init__(self, conn: sqlite3.Connection, owned: Callable[[], bool]):
        self._target = conn
        self._owned = owned
        self._active = True
        self._initial_row_factory = conn.row_factory
        self._row_factory_changed = False

    # LLM: sqlite3.Row沿原C接口，用户回调则拿访问层；非callable仍交SQLite保持原属性契约。
    # 函数用途: 给自定义行转换回调绑定租期，标准行工厂不改形状。
    def _bind_row_factory(self, factory):
        if factory is sqlite3.Row or not callable(factory):
            return factory
        return _LeaseRowFactory(self, factory)

    # LLM: 不添加自动提交或幂等重试，过期访问在底层commit前拒绝。
    # 函数用途: 提交调用方明确要求提交的事务。
    def commit(self):
        return self._call('commit')

    # LLM: 调用方主动回滚与scope归还清理独立，租期检查不能省。
    # 函数用途: 回滚当前借用的事务。
    def rollback(self):
        return self._call('rollback')

    # LLM: 工厂参数仍传原连接入口，返回游标必须绑定当前租期并由原跟踪机制清理。
    # 函数用途: 为当前借用创建游标。
    def cursor(self, factory=None):
        return self._call('cursor', (factory,))

    # LLM: SQLite跟踪回调按原接口登记，不经由通用可变参数入口；回调闭包中的旧租期仍会拒绝访问。
    # 函数用途: 设置当前连接的SQL观察回调。
    def set_trace_callback(self, callback):
        return self._call('set_trace_callback', (callback,))

    # LLM: SQLite函数接收SQL值而非连接，回调重入的借用仍由scope的in_use规则决定。
    # 函数用途: 在当前有效连接登记普通SQLite函数。
    def create_function(self, name, narg, function):
        return self._call('create_function', (name, narg, function))

    # LLM: 连接自身是游标和访问转发唯一的租期权威，属性不执行数据库 I/O。
    # 函数用途: 为共用转发代码提供本次租期。
    @property
    def _lease(self) -> SQLiteConnectionLease:
        return self

    # LLM: 非创建者与已归还均在任何物理操作之前拒绝；即使下一个借用已开始，旧访问层也不重新激活。
    # 函数用途: 判断当前调用是否仍处于这次有效借用中。
    def _check_active(self) -> None:
        if not self._active or not self._owned():
            raise SQLiteLeaseExpiredError('SQLite connection lease is no longer active in this execution context')

    # LLM: 归还先失效；仅原线程恢复物理属性、解除租期回调，fork/异地线程不触碰继承对象。
    # 函数用途: 永久结束当前访问层及游标，避免本租期行工厂残留到下一借用。
    def _invalidate(self) -> None:
        self._active = False
        if self._row_factory_changed and self._owned():
            self._target.row_factory = self._initial_row_factory

    # LLM: sqlite3 的连接上下文不关闭连接；这里保留该语义，但 __enter__ 绝不能返回原始物理连接。
    # 函数用途: 支持借用中的标准 SQLite 事务上下文。
    def __enter__(self) -> SQLiteConnectionLease:
        self._call('__enter__')
        return self

    # LLM: 原提交/回滚语义交标准库处理，结束事务不结束外层租期；租期失效时先拒绝。
    # 函数用途: 转发连接上下文退出时的事务处理。
    def __exit__(self, exc_type, exc, traceback):
        return self._call('__exit__', (exc_type, exc, traceback))


# LLM: 游标与创建它的访问层同寿命；SQLite 原 weak tracking 仍负责释放语句，访问层不持有新业务缓存。
# 类用途: 防止半读游标、游标属性和保存的 fetch 方法绕开连接租期。
class SQLiteCursorLease(_SQLiteStatementLease):
    # LLM: 不重新执行语句，只包装已由原连接登记的游标；SQL 与 PRAGMA 仍由原工厂及 sqlite3 负责。
    # 函数用途: 将一条真实游标绑定到它的创建租期。
    def __init__(self, lease: SQLiteConnectionLease, cursor: sqlite3.Cursor):
        self._lease = lease
        self._target = cursor

    # LLM: 不能通过 cursor.connection 得到物理连接；有效租期返回同一访问层，过期则结构化拒绝。
    # 函数用途: 保留游标反查连接的公开接口而不放大寿命。
    @property
    def connection(self) -> SQLiteConnectionLease:
        self._lease._check_active()
        return self._lease

    # LLM: 延迟读取和保存的fetch方法都要在物理游标访问前核租期。
    # 函数用途: 读取当前结果的一行。
    def fetchone(self):
        return self._call('fetchone')

    # LLM: 无参数时保留arraysize，显式size原样传递；哨兵不把显式None误当省略参数。
    # 函数用途: 按SQLite原大小规则读取一批行。
    def fetchmany(self, size=_SQLITE_FETCH_SIZE_DEFAULT):
        return self._call('fetchmany', () if size is _SQLITE_FETCH_SIZE_DEFAULT else (size,))

    # LLM: 归还后的读取不允许继续持有旧游标语句或WAL读锁。
    # 函数用途: 读取当前结果的全部剩余行。
    def fetchall(self):
        return self._call('fetchall')

    # LLM: Python 迭代不会走 __getattr__，需要在这里和每一次 __next__ 分别校验。
    # 函数用途: 支持有效租期中的游标迭代。
    def __iter__(self) -> SQLiteCursorLease:
        self._lease._check_active()
        return self

    # LLM: 延迟生成器恢复后也必须核租期，不能用已归还游标继续读取或持有 WAL 语句。
    # 函数用途: 在本次借用仍有效时读取下一行。
    def __next__(self):
        self._lease._check_active()
        return next(self._target)
