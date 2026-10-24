# LLM: 使用临时真实SQLite；测试物理连接与作用域，不改事务、existing_only或宿主库。
# 模块用途: 验证工具工作项连接复用不继承父连接且结束即关闭。
import sqlite3
from contextvars import copy_context
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.tool_loop import round_execution as rounds
from agent_py_agent.agent.io.sqlite_scope import (
    scoped_connection,
    sqlite_connection_factory,
    sqlite_connection_scope,
)
from agent_py_agent.tests.test_tool_round_execution import _round_request, _success


# LLM: 工厂记录实际物理连接，仍执行标准SQLite与真实scope策略。
# 函数用途: 在同一工作项内对同库做两次独立借用。
def borrow_twice(path, physical):
    # LLM: SQLite连接只在真正创建线程构造，不将父物理连接写入Context。
    # 函数用途: 创建并记录一条临时物理连接。
    def connect():
        conn = sqlite3.connect(path, factory=sqlite_connection_factory())
        physical.append(conn)
        return conn

    for _ in range(2):
        with scoped_connection(path, False, connect) as conn:
            conn.execute("select 1").fetchone()


# LLM: 串行和并行工具应有相同单项scope；cap关闭也应遵守sqlite开关。
# 函数用途: 暴露串行入口未建立单工具连接边界。
@pytest.mark.parametrize("entry", ["serial", "parallel"])
@pytest.mark.parametrize("cap", [0, 1])
@pytest.mark.parametrize("reuse", [False, True])
def test_serial_work_item_has_own_sqlite_scope(tmp_path, entry, cap, reuse):
    physical = []
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=tmp_path),
                            config=SimpleNamespace(sqlite_connection_reuse=reuse, max_parallel_tool_calls_per_owner=cap))

    # LLM: execute_one是真工作项边界，借用原SQLite scope而不是模拟缓存。
    # 函数用途: 在一次工具调用内重复连接。
    def execute(work):
        borrow_twice(tmp_path / "worker.db", physical)
        return _success(work, "ok")

    request = _round_request(agent=agent, params=SimpleNamespace(tool_context=[], task_attributes={}),
                             tool_rounds=1, response=None, calls=[{"tool": "read"}],
                             execute_one=execute, record_one=lambda _: None)
    if entry == "serial":
        rounds._execute_serial_step(request, request.calls, 0, 0, rounds._ToolRoundProgress())
    else:
        rounds._execute_parallel_segment(request, request.calls, start_idx=1)
    assert len(physical) == (1 if reuse else 2)
    try:
        physical[0].execute("select 1")
    except sqlite3.ProgrammingError:
        pass
    else:
        raise AssertionError("工具完成后连接仍开放")


# LLM: copy_context只能复制scope引用；worker必须先换成本线程新scope，再执行任何连接。
# 函数用途: 检查父子连接完全隔离且Context退出不会关闭父连接。
def test_worker_clears_inherited_connection_scope(tmp_path):
    from agent_py_agent.agent.tooling.owner_tool_scheduler import OwnerToolScheduler, ToolWorkItem
    parent, child = [], []
    with sqlite_connection_scope(True):
        borrow_twice(tmp_path / "shared.db", parent)
        item = copy_context().run(lambda: ToolWorkItem("child", lambda: borrow_twice(tmp_path / "shared.db", child),
                                                      sqlite_reuse=True))
        OwnerToolScheduler.for_owner(tmp_path, 1).submit(item).result(5)
        assert len(parent) == len(child) == 1 and parent[0] is not child[0]
        assert parent[0].execute("select 1").fetchone() == (1,)


# LLM: 真嵌套executor内借连接；父scope在子调用期间必须隔离，配置可继承但物理连接不能。
# 函数用途: 检查inline与legacy嵌套的单项SQLite边界。
@pytest.mark.parametrize("cap,count", [(1, 2), (0, 2), (0, 1)])
def test_nested_calls_reuse_only_their_own_connections(tmp_path, cap, count):
    from agent_py_agent.agent.tooling.owner_tool_scheduler import OwnerToolScheduler, ToolWorkItem
    from agent_py_agent.tests.test_tool_program import Probe, run
    parent, children = [], []

    # LLM: 每个子调用单独记录物理连接，执行器/权限/归档仍走原Probe测试入口。
    # 类用途: 在真实嵌套handler中观察SQLite。
    class SqlProbe(Probe):
        # LLM: 每次handler两次同库借用应复用一条，退出应关闭。
        # 函数用途: 连接后执行原Probe逻辑。
        def execute(self, params):
            physical = []
            borrow_twice(tmp_path / "shared.db", physical)
            children.append(physical)
            return super().execute(params)

    # LLM: 父连接在子调用前后都保持有效，子调用完成后不能持有父lease。
    # 函数用途: 从带scope的真实工作项执行嵌套程序。
    def invoke():
        borrow_twice(tmp_path / "shared.db", parent)
        outcome, _ = run(tmp_path, [{"id": "nested", "calls": [
            {"tool": "probe", "arguments": {"value": n}} for n in range(count)]}],
            probe=SqlProbe(), owner_scope_root=str(tmp_path))
        assert outcome.ok, outcome.output
        assert parent[0].execute("select 1").fetchone() == (1,)

    item = ToolWorkItem("parent", invoke, sqlite_reuse=True)
    if cap:
        OwnerToolScheduler.for_owner(tmp_path, cap).submit(item).result(5)
    else:
        item.run()
    assert len(parent) == 1 and len(children) == count
    assert all(len(group) == 1 for group in children)
    assert len({id(group[0]) for group in [parent, *children]}) == count + 1
