"""持久规划作用域保留原租期和事务隔离，停机错误不能遗留后续owner连接。"""
import sqlite3
import threading
from contextlib import nullcontext
from contextvars import copy_context
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.io import sqlite_scope as original
from agent_py_agent.agent.runtime_db.sqlite_scope import (
    activate_sqlite_scope,
    close_persistent_sqlite_scope,
    new_persistent_sqlite_scope,
)
from agent_py_agent.cli.gateway_loops import _BackgroundMainSupervisor


# LLM: 每次使用真实SQLite工厂及原租期协议；物理连接只供测试观测，不交给产品调用方。
# 函数用途: 在指定持久作用域里查询并返回底层连接供寿命断言。
def _query(scope, path):
    with activate_sqlite_scope(scope):
        with original.scoped_connection(path, False, lambda: sqlite3.connect(path, factory=original.sqlite_connection_factory())) as lease:
            assert lease.execute("SELECT 1").fetchone() == (1,)
    return next(iter(scope.entries.values())).conn


# LLM: 跨拍原物理连接必须相同，但已归还lease仍不可用，关闭持久表后物理连接必须关闭。
# 函数用途: 验证三项新API真正延长连接而不延长借用权限。
def test_persistent_scope_reuses_only_physical_connection(tmp_path):
    scope = new_persistent_sqlite_scope()
    path = tmp_path / "runtime.db"
    first = _query(scope, path)
    assert _query(scope, path) is first
    assert original._SQLITE_ACTIVE_SCOPE.get() is None
    close_persistent_sqlite_scope(scope)
    close_persistent_sqlite_scope(scope)
    with pytest.raises(sqlite3.ProgrammingError):
        first.execute("SELECT 1")
    with pytest.raises(RuntimeError):
        with activate_sqlite_scope(scope):
            pass


# LLM: 用真实WAL事务模拟闲置连接遗留读快照，外部写者与checkpoint均是真实SQLite。
# 函数用途: 验证每拍退出激活会commit释放读事务，而不关闭复用连接。
def test_activation_releases_read_snapshot_for_checkpoint(tmp_path):
    scope = new_persistent_sqlite_scope()
    path = tmp_path / "runtime.db"
    conn = _query(scope, path)
    conn.execute("PRAGMA journal_mode=WAL").fetchone()
    conn.execute("CREATE TABLE facts(value)")
    conn.commit()
    with activate_sqlite_scope(scope):
        conn.execute("BEGIN")
        conn.execute("SELECT * FROM facts").fetchall()
        assert conn.in_transaction
        with sqlite3.connect(path) as writer:
            writer.execute("INSERT INTO facts VALUES (1)")
    assert not conn.in_transaction
    with sqlite3.connect(path, timeout=0.1) as writer:
        assert writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
    assert _query(scope, path) is conn
    close_persistent_sqlite_scope(scope)


# LLM: 内层同表激活不能提交仍在借用的外层事务，归还仍由原协议回滚未提交写入。
# 函数用途: 验证持久激活不破坏既有嵌套事务隔离。
def test_nested_activation_never_commits_outer_borrow(tmp_path):
    scope = new_persistent_sqlite_scope()
    path = tmp_path / "runtime.db"
    conn = _query(scope, path)
    conn.execute("CREATE TABLE facts(value)")
    conn.commit()
    with activate_sqlite_scope(scope):
        with original.scoped_connection(path, False, lambda: None) as lease:
            lease.execute("INSERT INTO facts VALUES (1)")
            with activate_sqlite_scope(scope):
                pass
            assert lease.in_transaction
    assert conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 0
    close_persistent_sqlite_scope(scope)


# LLM: 同一scope被禁用或另一scope遮蔽后重入，内层正常/异常退出均不能清理外层有效事务。
# 函数用途: 用真实写事务锁住间接嵌套的租期与Context恢复边界。
@pytest.mark.parametrize("other_scope", [False, True])
@pytest.mark.parametrize("fail_inner", [False, True])
def test_shadowed_activation_preserves_outer_transaction(tmp_path, other_scope, fail_inner):
    scope = new_persistent_sqlite_scope()
    shadow = new_persistent_sqlite_scope() if other_scope else None
    conn = _query(scope, tmp_path / "runtime.db")
    conn.execute("CREATE TABLE facts(value)")
    conn.commit()
    error = ValueError("inner activation failed")
    with activate_sqlite_scope(scope):
        with original.scoped_connection(tmp_path / "runtime.db", False, lambda: None) as lease:
            lease.execute("INSERT INTO facts VALUES (1)")
            with activate_sqlite_scope(shadow):
                with pytest.raises(ValueError) if fail_inner else nullcontext():
                    with activate_sqlite_scope(scope):
                        assert original._SQLITE_ACTIVE_SCOPE.get() is scope
                        if fail_inner:
                            raise error
                assert original._SQLITE_ACTIVE_SCOPE.get() is shadow
            assert original._SQLITE_ACTIVE_SCOPE.get() is scope
            assert not scope.closed
            assert lease.in_transaction
            lease.commit()
    assert original._SQLITE_ACTIVE_SCOPE.get() is None
    assert conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 1
    close_persistent_sqlite_scope(scope)
    if shadow is not None:
        close_persistent_sqlite_scope(shadow)


# LLM: 复制Context里的旧当前值不等于尚在执行的外层激活；重入必须照常释放本拍读快照。
# 函数用途: 防止用Context激活栈记录深度时留下过期嵌套状态。
def test_copied_context_does_not_skip_later_activation_cleanup(tmp_path):
    scope = new_persistent_sqlite_scope()
    conn = _query(scope, tmp_path / "runtime.db")
    with activate_sqlite_scope(scope):
        copied = copy_context()

    # LLM: 同一创建线程在复制的Context里启动新一拍，未归还事务必须由该拍边界释放。
    # 函数用途: 模拟当前值尚在但原激活已退出后的真实事务。
    def read_again():
        with activate_sqlite_scope(scope):
            conn.execute("BEGIN")
            conn.execute("SELECT 1").fetchone()

    copied.run(read_again)
    assert not conn.in_transaction
    close_persistent_sqlite_scope(scope)


# LLM: 异常遗留借用按原合同退役后，下一次必须经过Gateway真实规划scope入口重建并再次可查询。
# 函数用途: 验证scope.closed分支能恢复下一拍，而非只检查新建函数本身。
def test_gateway_recreates_retired_planner_scope_on_next_activation(tmp_path):
    supervisor = object.__new__(_BackgroundMainSupervisor)
    supervisor._base_agent = SimpleNamespace(config=SimpleNamespace(sqlite_connection_reuse=True))
    path = tmp_path / "runtime.db"
    with supervisor._planner_sqlite_scope("owner"):
        retired = supervisor._owner_planner_sqlite_scopes["owner"]
        borrow = original.scoped_connection(path, False, lambda: sqlite3.connect(path))
        borrow.__enter__()
    assert retired.closed
    borrow.__exit__(None, None, None)
    with supervisor._planner_sqlite_scope("owner"):
        replacement = supervisor._owner_planner_sqlite_scopes["owner"]
        assert replacement is not retired
        with original.scoped_connection(path, False, lambda: sqlite3.connect(path)) as lease:
            assert lease.execute("SELECT 1").fetchone() == (1,)
    supervisor._retain_owner_planner_scopes(set())
    assert replacement.closed


# LLM: 首条真实连接close后抛错，第二owner仍需关闭；测试不得把忽略异常当作清理完成。
# 类用途: 在物理连接确实关闭后注入可定位的清理失败。
class _CloseErrorConnection(sqlite3.Connection):
    # LLM: 错误发生于关闭观察点，不阻止SQLite释放自身资源。
    # 函数用途: 模拟一个owner的连接清理失败。
    def close(self):
        super().close()
        raise RuntimeError("first close failed")


# LLM: 两个scope均归当前线程，以隔离多owner清理控制流，不涉及异线程SQLite限制。
# 函数用途: 准备一个关闭失败、一个正常关闭的持久连接表。
def _closing_supervisor(tmp_path):
    scopes, connections = {}, []
    for key, factory in (("first", _CloseErrorConnection), ("second", sqlite3.Connection)):
        scope = new_persistent_sqlite_scope()
        connection = sqlite3.connect(tmp_path / f"{key}.db", factory=factory)
        scope.entries[(scope.pid, scope.thread_id, tmp_path / f"{key}.db", False)] = original._ConnectionEntry(connection, (0, 0), False)
        scopes[key] = scope
        connections.append(connection)
    supervisor = object.__new__(_BackgroundMainSupervisor)
    supervisor._owner_planner_sqlite_scopes = scopes
    supervisor._owner_planner_scope_used_at = {}
    supervisor._executor = supervisor._curator_executor = None
    return supervisor, connections


# LLM: 传播第一条close错误之前必须尝试全部owner，不放宽为只检查字典存在。
# 函数用途: 复现第一owner关闭失败不得中止其余owner的回收。
def test_scope_retirement_closes_all_owners_after_one_close_error(tmp_path):
    supervisor, connections = _closing_supervisor(tmp_path)
    with pytest.raises(RuntimeError, match="first close failed"):
        supervisor._retain_owner_planner_scopes(set())
    assert supervisor._owner_planner_sqlite_scopes == {}
    with pytest.raises(sqlite3.ProgrammingError):
        connections[1].execute("SELECT 1")


# LLM: 停机既有业务异常的对象身份优先；清理失败仍要关完其余owner，不能覆盖原错误。
# 函数用途: 验证shutdown双重失败时保留原始异常。
def test_shutdown_cleanup_does_not_replace_original_error(tmp_path):
    supervisor, connections = _closing_supervisor(tmp_path)
    error = ValueError("shutdown wait failed")

    # LLM: 在原wait观察点抛固定异常，检查不是只保留同类的新异常。
    # 函数用途: 模拟已存在的停机核收错误。
    def fail_wait():
        raise error

    supervisor._shutdown_control = SimpleNamespace(stop=lambda: None, wait=fail_wait)
    with pytest.raises(ValueError) as caught:
        supervisor.shutdown()
    assert caught.value is error
    assert supervisor._owner_planner_sqlite_scopes == {}
    with pytest.raises(sqlite3.ProgrammingError):
        connections[1].execute("SELECT 1")


# LLM: 原机制允许异线程归还但把物理回收留给创建者；真实未提交事务必须在本拍边界失效而非跨拍保锁。
# 函数用途: 复现缓存及溢出连接异地归还后，由规划线程回收并回滚写入。
@pytest.mark.parametrize("overflow", [False, True])
def test_foreign_return_is_reclaimed_at_planner_boundary(tmp_path, overflow):
    scope = new_persistent_sqlite_scope()
    path = tmp_path / "runtime.db"
    initial = sqlite3.connect(path)
    initial.execute("CREATE TABLE facts(value)")
    initial.close()

    # LLM: 复用真实跟踪工厂，不放宽SQLite的创建线程限制。
    # 函数用途: 打开用于原租期协议的物理连接。
    def connect():
        return sqlite3.connect(path, factory=original.sqlite_connection_factory())

    with activate_sqlite_scope(scope):
        outer = original.scoped_connection(path, False, connect)
        lease = outer.__enter__()
        context = original.scoped_connection(path, False, connect) if overflow else outer
        if overflow:
            lease = context.__enter__()
        physical = next(iter(scope.uncached.values())) if overflow else next(iter(scope.entries.values())).conn
        lease.execute("INSERT INTO facts VALUES (1)")
        errors = []

        # LLM: 只在异线程归还租期，按旧合同该线程不能操作物理连接。
        # 函数用途: 模拟由其它线程结束借用的合法清理路径。
        def give_back():
            try:
                context.__exit__(None, None, None)
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=give_back)
        thread.start()
        thread.join()
        assert not errors
        if overflow:
            outer.__exit__(None, None, None)
    closed = False
    try:
        physical.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        closed = True
    assert closed
    writer = sqlite3.connect(path, timeout=0.1)
    try:
        assert writer.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 0
        writer.execute("INSERT INTO facts VALUES (2)")
        writer.commit()
    finally:
        writer.close()
