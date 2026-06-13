"""批3 per-thread 协作中断钉子(长期助手 interrupt.py 蓝本)。

钉死契约:中断按线程隔离;register finally 清旗(线程复用安全);
工具循环安全点看旗收工;cancel 对线程形态按登记名递旗。
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

from agent_py_agent.agent.agent_core.orchestration.tools.cancel import _interrupt_dispatch_thread
from agent_py_agent.agent.agent_core.tool_loop.round_execution import (
    ToolRoundExecutionRequest,
    execute_tool_round,
)
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.concurrency.interrupt import (
    interrupt_by_name,
    is_interrupted,
    register_interruptible,
    set_interrupt,
)
from agent_py_agent.agent.tooling import ToolExecutionResult


def test_interrupt_is_thread_scoped():
    seen: dict[str, bool] = {}
    ready = threading.Event()
    release = threading.Event()

    def worker():
        ready.set()
        release.wait(timeout=5)
        seen["worker"] = is_interrupted()

    thread = threading.Thread(target=worker)
    thread.start()
    ready.wait(timeout=5)
    set_interrupt(True)  # 只立当前(主)线程的旗
    release.set()
    thread.join(timeout=5)
    assert is_interrupted() is True, "主线程自己看得到旗"
    assert seen["worker"] is False, "别的线程不受影响"
    set_interrupt(False)


def test_register_scope_clears_flag_and_name():
    with register_interruptible("t-clean"):
        assert interrupt_by_name("t-clean") is True
        assert is_interrupted() is True
    assert is_interrupted() is False, "退出 finally 必清旗(线程复用安全)"
    assert interrupt_by_name("t-clean") is False, "名字已注销"


def test_tool_round_stops_at_interrupt_safe_point():
    executed: list[str] = []
    records: list[tuple[str, str, str]] = []

    def execute_one(request):
        executed.append(str(request.payload["tool"]))
        return ToolExecutionResult("read_file", True, "ok")

    def record_one(record):
        records.append((str(record.payload["tool"]), record.result.output, record.result.error_code or ""))

    set_interrupt(True)
    try:
        execute_tool_round(
            ToolRoundExecutionRequest(
                agent=SimpleNamespace(),
                params=SimpleNamespace(tool_context=[]),
                tool_rounds=1,
                response=ModelResponse(text="round", backend="test"),
                calls=[{"tool": "read_file", "path": "/tmp/a.md"}, {"tool": "read_file", "path": "/tmp/b.md"}],
                execute_one=execute_one,
                record_one=record_one,
            )
        )
    finally:
        set_interrupt(False)
    assert executed == [], "中断旗下不再开新工具"
    assert records and records[0][2] == "CANCELLED", "复用 taxonomy 权威码,字段链(hint/动作)不丢"
    assert "已被取消" in records[0][1]


def test_cancel_signals_dispatch_thread_by_run_id():
    agent = SimpleNamespace(
        _background_subagent_dispatches={
            "launch-1": {"run_ids": ["run-a"], "thread_name": "t-cancel-target"}
        }
    )
    with register_interruptible("t-cancel-target"):
        assert _interrupt_dispatch_thread(agent, "run-a") == "signaled"
        assert is_interrupted() is True
    assert _interrupt_dispatch_thread(agent, "run-miss") == "not_found"
    assert _interrupt_dispatch_thread(SimpleNamespace(), "run-a") == "not_found", "无登记表不崩"
