"""Q1：前台等待、继承环境及操作领取时钟的可复现回归。"""
from __future__ import annotations

import os
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent_py_agent.agent.runtime_db import managed_operation_store as managed
from agent_py_agent.agent.tooling import shell


# LLM: 用可推进时钟隔离调度噪声；只替换采集和退出观察，不启动或终止任何真实进程。
# 函数用途: 构造输出已关闭但父进程暂未退出的前台等待环境。
@pytest.fixture
def closed_pipe_clock(monkeypatch):
    clock = SimpleNamespace(now=0.0, sleeps=[], probes=[])

    # LLM: 每次休眠必须正数且不超原取消检查周期，模拟单调时钟而不真正等待。
    # 函数用途: 记录一次等待并推进测试时间。
    def sleep(seconds):
        assert 0 < seconds <= 0.02
        clock.sleeps.append(seconds)
        clock.now += seconds

    capture = SimpleNamespace(done=lambda: True, finish=lambda timeout: True,
                              result=lambda: ("original-output", "", {"complete": True}))
    monkeypatch.setattr(shell, "time", SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep))
    monkeypatch.setattr(shell, "ProcessOutputCapture", lambda proc: capture)
    monkeypatch.setattr(shell, "capture_process_birth_token", lambda pid: "birth")
    monkeypatch.setattr(shell, "is_interrupted", lambda: False)
    monkeypatch.setattr(shell, "cancellation_requested", lambda: False)
    clock.capture = capture
    return clock


# LLM: 直接检查真实等待循环的探测时点；不靠墙钟阈值掩盖退避错误。
# 函数用途: 锁住2/4/8/16/32/50毫秒退避及后续50毫秒封顶。
def test_foreground_eof_exit_probes_use_capped_exponential_backoff(monkeypatch, closed_pipe_clock):
    clock = closed_pipe_clock
    receipt = object()

    # LLM: 前七次探测仍活，第八次才返回真实退出形态，不把管道EOF当退出。
    # 函数用途: 记录父进程退出探测时间并保留退出码。
    def finish(proc, *, expected_birth_token):
        assert expected_birth_token == "birth"
        clock.probes.append(clock.now)
        return (7, receipt) if len(clock.probes) == 8 else (None, None)

    monkeypatch.setattr(shell, "finish_foreground_process", finish)
    result = shell._communicate_process(SimpleNamespace(pid=123), command="synthetic", timeout=3)
    assert clock.probes == pytest.approx([0, .002, .006, .014, .030, .062, .112, .162])
    assert result.returncode == 7 and result.stdout == "original-output"
    assert result.termination is receipt


# LLM: 退避中的每轮仍检查取消和deadline；模拟回收结果，不向宿主PID发信号。
# 函数用途: 验证较长退出退避不会延迟取消或超时处理。
@pytest.mark.parametrize("cancel", [True, False])
def test_foreground_exit_backoff_keeps_cancellation_and_deadline(monkeypatch, closed_pipe_clock, cancel):
    clock = closed_pipe_clock
    receipt = object()
    kill = Mock(return_value=receipt)
    monkeypatch.setattr(shell, "_kill_process_group", kill)
    monkeypatch.setattr(shell, "finish_foreground_process", lambda *args, **kwargs: (None, None))
    if cancel:
        monkeypatch.setattr(shell, "cancellation_requested", lambda: clock.now >= .01)
    with pytest.raises(shell.CommandInterruptedError if cancel else shell.CommandTimeoutError):
        shell._communicate_process(SimpleNamespace(pid=123), command="synthetic", timeout=.03)
    assert clock.now <= (.03 if cancel else .05)
    kill.assert_called_once()


# LLM: 只给测试进程注入合成malloc值；核验副本擦洗而非修改Gateway自身环境。
# 函数用途: 用户子进程不继承Gateway调优变量，普通业务变量和宿主环境保持不变。
@pytest.mark.parametrize("sandboxed", [True, False])
def test_subprocess_environment_drops_only_gateway_malloc_tuning(monkeypatch, sandboxed):
    names = ("MALLOC_ARENA_MAX", "MALLOC_TRIM_THRESHOLD_", "MallocLargeCache")
    for name in names:
        monkeypatch.setenv(name, "synthetic-tuning")
    monkeypatch.setenv("Q1_BUSINESS_ENV", "keep")
    env = shell._subprocess_text_env(sandboxed=sandboxed)
    assert all(name not in env for name in names)
    assert env["Q1_BUSINESS_ENV"] == "keep"
    assert all(os.environ[name] == "synthetic-tuning" for name in names)


# LLM: 真RuntimeDB登记权威链；只在BEGIN IMMEDIATE真正返回后推进时钟，不改原SQL或执行权限。
# 函数用途: 确认handler开始时间不包含取得写锁前的等待，显式时刻仍原样保留。
@pytest.mark.parametrize("requested_now,expected", [(0.0, 250.0), (None, 250.0), (123.0, 123.0)])
def test_managed_claim_clock_is_sampled_after_write_lock(tmp_path, monkeypatch, requested_now, expected):
    from agent_py_agent.agent.local_storage.tool_operations import (
        ToolOperationClaimRequest,
        new_tool_operation_holder,
    )
    from agent_py_agent.agent.runtime_db.repository import RuntimeRepository

    repo = RuntimeRepository(tmp_path / "runtime.db")
    chain = repo.record_run_creation(owner_id="owner", goal="合成时钟", conversation_task_id="task",
                                     thread_id="thread", run_id="run", role="assistant")
    clock = {"now": 100.0}
    connection = repo._runtime_connection

    # LLM: 只代理同一sqlite连接的execute，保留查询、提交和上下文清理合同。
    # 类用途: 在实际取得写锁的边界推进合成时钟。
    class Connection:
        # LLM: 所有SQL原样执行；仅BEGIN成功之后改变测试时间。
        # 函数用途: 暴露写锁取得前后两个可区分的时刻。
        def execute(self, sql, *args):
            result = self.conn.execute(sql, *args)
            if sql == "BEGIN IMMEDIATE":
                clock["now"] = 250.0
            return result

        # LLM: 未替换方法仍调用原sqlite连接，不改变事务行为。
        # 函数用途: 转发查询代理之外的连接方法。
        def __getattr__(self, name):
            return getattr(self.conn, name)

    # LLM: 保留原连接的关闭/提交语义，只在本测试领取阶段安装SQL观察代理。
    # 函数用途: 给ManagedOperationStore提供同一个真实库连接。
    @contextmanager
    def observed_connection():
        with connection() as conn:
            proxy = Connection()
            proxy.conn = conn
            yield proxy

    monkeypatch.setattr(repo, "_runtime_connection", observed_connection)
    monkeypatch.setattr(managed, "time", SimpleNamespace(time=lambda: clock["now"]))
    request = ToolOperationClaimRequest(owner_id="owner", run_id="run", task_id="task", operation_id="op",
        tool="synthetic", args_hash="hash", idempotency_key="key", idempotency_scope="operation",
        idempotency_namespace="synthetic", holder=new_tool_operation_holder(), lease_expires_at=1000.0,
        attempt_id=str(chain["attempt_id"]), now=requested_now)
    claim = managed.ManagedOperationStore(repo).claim_tool_operation(request)
    with connection() as conn:
        row = conn.execute("SELECT handler_started_at FROM tool_operations WHERE operation_id = 'op'").fetchone()
    assert claim.action == "execute"
    assert row["handler_started_at"] == expected
    assert claim.record.lease_expires_at == 1000.0
