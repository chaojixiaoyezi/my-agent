# LLM: ptyleak 的回合/任务收口集成测试：用真实 agent.run（echo 后端）走完整收口链，断言回合结束时
#   宿主按结构化归属收回 PTY 会话。沙箱内不能嵌套 Seatbelt，_sandbox_exec 被换成直跑命令，
#   这里验证的是收口接线与归属选择，不是沙箱本身；真实沙箱路径由 test_pty_sessions 的既有用例覆盖。
# 模块用途: 端到端钉住“回合结束/任务结束会收回该归属的 PTY 会话，且不误收其它任务”。
from __future__ import annotations

import os
import shlex
import sys
import time

import pytest

from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.tooling import pty_sessions as pty
from agent_py_agent.agent.tooling.pty_sessions import pty_session_registry

pytestmark = pytest.mark.skipif(os.name == "nt", reason="stdlib pty is POSIX-only")


@pytest.fixture(autouse=True)
def _clear_pty_sessions():
    pty_session_registry.clear()
    pty_session_registry.idle_timeout_seconds = 0.0
    yield
    pty_session_registry.clear()


@pytest.fixture
def _unsandboxed_spawn(monkeypatch):
    monkeypatch.setattr(pty, "_sandbox_exec", lambda command, target, owner_home=None, **kw: (command, True))


# LLM: 回收的判定是“进程真的退出 + 有终止回执”；真机可观测时 termination.confirmed 也应为 True。
# 函数用途: 有界等待测试 PTY 进程被收回并断言回执存在。
def _wait_pty_dead(session, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and session.process.poll() is None:
        time.sleep(0.02)
    assert session.process.poll() is not None, "PTY 进程未被收回"
    while time.monotonic() < deadline and session.termination is None:
        time.sleep(0.02)
    assert session.termination is not None, "缺少终止回执"


# LLM: 会话身份用与工具侧同源的字段：owner_home 取 home_paths（写边界 canonical_owner_home_root 的来源），
#   线程/任务/run 取本轮结构化属性；这样测试里的归属与生产 _handler_run_scope 一致。
# 函数用途: 构造与生产一致的 PTY 会话归属快照。
def _session_scope(agent, thread_id: str, task_id: str, run_id: str) -> dict:
    return {
        "owner_home": str(agent.home_paths.owner_home_dir),
        "session_id": thread_id,
        "root_task_id": task_id,
        "task_id": f"gateway:{task_id}",
        "run_id": run_id,
        "attempt_id": f"attempt-{run_id}",
    }


# 函数用途: 起一个归属于给定快照的真实 PTY 长命令会话。
def _start_session(root, scope: dict, command: str):
    return pty_session_registry.start(command, root, run_scope=scope)


def _long_command() -> str:
    return f"{shlex.quote(sys.executable)} -c 'import time; time.sleep(100000)'"


def test_turn_end_reclaims_pty_sessions_of_the_conversation_task(tmp_path, _unsandboxed_spawn):
    agent = SimpleAgent(AgentConfig(model_backend="echo"), tmp_path)
    thread = agent.conversation_store.threads.get_or_create(
        {"channel": "cli", "channel_conversation_id": "c-1", "channel_user_id": "u-1"}
    )
    attrs = {"conversation_thread_id": thread.thread_id}
    session = _start_session(tmp_path, _session_scope(agent, thread.thread_id, "task-x", "run-x"),
                             _long_command())
    other = _start_session(tmp_path, _session_scope(agent, thread.thread_id, "task-other", "run-other"),
                           _long_command())
    time.sleep(0.5)
    assert session.process.poll() is None and other.process.poll() is None

    agent.run("hello", run_id="run-x", task_id="task-x", task_attributes=attrs, save=False)

    _wait_pty_dead(session)
    assert other.process.poll() is None, "其它任务的会话不应被本回合收回"


def test_task_sweep_reclaims_earlier_runs_of_the_same_task(tmp_path, _unsandboxed_spawn):
    agent = SimpleAgent(AgentConfig(model_backend="echo"), tmp_path)
    thread = agent.conversation_store.threads.get_or_create(
        {"channel": "cli", "channel_conversation_id": "c-1", "channel_user_id": "u-1"}
    )
    attrs = {"conversation_thread_id": thread.thread_id}
    earlier = _start_session(tmp_path, _session_scope(agent, thread.thread_id, "task-x", "run-old"),
                             _long_command())
    other = _start_session(tmp_path, _session_scope(agent, thread.thread_id, "task-other", "run-other"),
                           _long_command())
    time.sleep(0.5)

    agent.run("hello", run_id="run-x", task_id="task-x", task_attributes=attrs, save=False)

    _wait_pty_dead(earlier)
    assert other.process.poll() is None


def test_turn_end_without_task_identity_falls_back_to_run(tmp_path, _unsandboxed_spawn):
    agent = SimpleAgent(AgentConfig(model_backend="echo"), tmp_path)
    session = _start_session(tmp_path, _session_scope(agent, "thread-free", "", "run-free"),
                             _long_command())
    other = _start_session(tmp_path, _session_scope(agent, "thread-free", "", "run-other"),
                           _long_command())
    time.sleep(0.5)

    agent.run("hello", run_id="run-free", task_id="", task_attributes={}, save=False)

    _wait_pty_dead(session)
    assert other.process.poll() is None
