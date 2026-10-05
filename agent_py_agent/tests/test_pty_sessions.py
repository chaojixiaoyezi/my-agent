from __future__ import annotations

import json
import os
import shlex
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.tooling.pty_sessions import (
    TerminalSessionTool,
    pty_session_registry,
    reclaim_pty_sessions,
)
from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions

pytestmark = pytest.mark.skipif(os.name == "nt", reason="stdlib pty is POSIX-only")


@pytest.fixture(autouse=True)
def _clear_pty_sessions():
    pty_session_registry.clear()
    yield
    pty_session_registry.clear()


def _tool(root: Path) -> TerminalSessionTool:
    shell = ShellTool(root, options=ShellToolOptions(workspace_roots=[root]))
    return TerminalSessionTool(shell)


def _payload(result):
    assert result.ok, result.output
    return json.loads(result.output)


def test_terminal_session_runs_real_interactive_pty(tmp_path: Path) -> None:
    tool = _tool(tmp_path)
    command = f"{shlex.quote(sys.executable)} -q"
    run_scope = {"owner_id": "owner-a", "session_id": "thread-a"}
    started = _payload(
        tool.execute({"action": "start", "command": command, "__run_scope": run_scope})
    )
    assert "pid" not in started

    listed = _payload(tool.execute({"action": "list", "__run_scope": run_scope}))
    assert listed["count"] == 1
    assert listed["sessions"][0]["session_id"] == started["session_id"]
    assert "pid" not in listed["sessions"][0]

    resized = _payload(
        tool.execute(
            {
                "action": "resize",
                "session_id": started["session_id"],
                "columns": 100,
                "rows": 30,
                "__run_scope": run_scope,
            }
        )
    )
    assert (resized["columns"], resized["rows"]) == (100, 30)

    written = _payload(
        tool.execute(
            {
                "action": "write",
                "session_id": started["session_id"],
                "data": "print(6 * 7)",
                "append_newline": True,
                "__run_scope": run_scope,
            }
        )
    )
    assert written["bytes"] > 0

    deadline = time.time() + 5
    output = ""
    cursor = 0
    while time.time() < deadline and "42" not in output:
        time.sleep(0.05)
        read = _payload(
            tool.execute(
                {
                    "action": "read",
                    "session_id": started["session_id"],
                    "cursor": cursor,
                    "__run_scope": run_scope,
                }
            )
        )
        cursor = read["cursor"]
        output += read["output"]

    assert "42" in output
    closed = _payload(
        tool.execute(
            {
                "action": "close",
                "session_id": started["session_id"],
                "__run_scope": run_scope,
            }
        )
    )
    assert closed["status"] == "closed"


def test_terminal_session_reuses_shell_command_policy(tmp_path: Path) -> None:
    result = _tool(tmp_path).execute({"action": "start", "command": "rm -rf /"})

    assert result.ok is False
    assert result.error_code == "COMMAND_POLICY_BLOCKED"


def test_terminal_session_requires_existing_session(tmp_path: Path) -> None:
    result = _tool(tmp_path).execute({"action": "read", "session_id": "pty-missing"})

    assert result.ok is False
    assert result.error_code == "PROCESS_NOT_FOUND"


# LLM: 使用真实 ToolExecutor 与耐久操作账本；不能用 handler 的局部结果代替整链错误归类。
# 函数用途: 将 PTY 控制调用送过授权和副作用核对，验证零写入不会变成 UNKNOWN。
def _execute_transport(tmp_path, tool, arguments):
    from agent_py_agent.agent.local_storage import LocalStore
    from agent_py_agent.agent.tooling.executor import ToolExecutor, ToolExecutorRequest
    from agent_py_agent.tests._tool_runtime_harness import (
        canonical_test_call,
        runtime_snapshot_for_tools,
    )

    snapshot = runtime_snapshot_for_tools({"terminal_session": tool})
    store = LocalStore(tmp_path / "operations.db", enable_fts=False)
    execution = ToolExecutor().execute(ToolExecutorRequest(
        call=canonical_test_call(snapshot, "terminal_session", arguments),
        runtime_snapshot=snapshot, workspace_root=tmp_path, workspace_roots=(tmp_path,),
        approval_mode="auto", operation_store=store, operation_store_required=True,
        operation_owner_id="test-owner",
    ))
    rows = store.list_tool_operations(owner_id="test-owner", run_id=snapshot.run_id)
    return execution.result, rows


@pytest.mark.parametrize("action", ["write", "resize", "close"])
def test_missing_pty_transport_is_not_an_unknown_effect(tmp_path, action):
    result, operations = _execute_transport(tmp_path, _tool(tmp_path), {
        "action": action, "session_id": "bg-old-handle",
        **({"data": "hello"} if action == "write" else {}),
        **({"columns": 80, "rows": 24} if action == "resize" else {}),
    })

    assert result.handler_executed
    assert result.error_code == "PROCESS_NOT_FOUND"
    assert result.effect_outcome == "not_started"
    assert len(operations) == 1
    assert operations[0].status == "failed"
    assert operations[0].result["effect_outcome"] == "not_started"


@pytest.mark.parametrize("written", [0, 2])
def test_pty_partial_input_retains_unknown_effect(tmp_path, monkeypatch, written):
    from agent_py_agent.agent.tooling.pty_sessions import PtyWriteError

    def fail_write(*args):
        raise PtyWriteError("TOOL_TIMEOUT", written)

    monkeypatch.setattr(pty_session_registry, "write", fail_write)
    result, operations = _execute_transport(tmp_path, _tool(tmp_path), {
        "action": "write", "session_id": "pty-existing", "data": "hello",
    })
    expected = "unknown" if written else "not_started"
    assert result.effect_outcome == expected
    assert operations[0].status == ("unknown" if written else "failed")
    assert (result.error_code == "TOOL_OPERATION_OUTCOME_UNKNOWN") is bool(written)


def test_pty_exited_before_write_reports_zero_bytes(tmp_path, monkeypatch):
    import threading

    monkeypatch.setattr(pty_session_registry, "get", lambda *args: SimpleNamespace(
        closed=True, process=SimpleNamespace(poll=lambda: 0), write_lock=threading.Lock(),
    ))
    result = _tool(tmp_path).execute({"action": "write", "session_id": "exited", "data": "x"})
    assert not result.ok
    assert result.effect_outcome == "not_started"
    assert result.result_envelope["bytes_written"] == 0


def test_pty_exit_after_completed_write_does_not_erase_delivery(tmp_path, monkeypatch):
    # 成功返回代表字节已交给内核；进程随即退出不能反向变成未执行或 UNKNOWN。
    monkeypatch.setattr(pty_session_registry, "write", lambda *args: SimpleNamespace(
        closed=True, process=SimpleNamespace(poll=lambda: 0),
    ))
    result = _tool(tmp_path).execute({"action": "write", "session_id": "finished", "data": "x"})
    assert _payload(result)["bytes"] == 1


def test_other_conversation_cannot_write_pty_and_does_not_halt_turn(tmp_path):
    tool = _tool(tmp_path)
    started = _payload(tool.execute({
        "action": "start", "command": f"{shlex.quote(sys.executable)} -q",
        "__run_scope": {"owner_id": "owner-a", "session_id": "thread-a"},
    }))
    result = tool.execute({
        "action": "write", "session_id": started["session_id"], "data": "print(42)",
        "__run_scope": {"owner_id": "owner-a", "session_id": "thread-b"},
    })
    assert result.error_code == "PROCESS_NOT_FOUND"
    assert result.effect_outcome == "not_started"
    assert pty_session_registry.get(started["session_id"]).process.poll() is None


def test_terminal_session_carries_structured_write_roots_to_sandbox(
    tmp_path: Path, monkeypatch
) -> None:
    task_root = tmp_path / "task"
    task_root.mkdir()
    captured: dict[str, object] = {}

    def fake_start(
        command,
        target,
        owner_home=None,
        write_roots=None,
        read_roots=None,
        protected_write_paths=None,
        run_scope=None,
        *,
        private_roots=(),
    ):
        captured.update(
            command=command,
            target=target,
            owner_home=owner_home,
            write_roots=write_roots,
            read_roots=read_roots,
            protected_write_paths=protected_write_paths,
            run_scope=run_scope,
            private_roots=private_roots,
        )
        raise OSError("captured")

    monkeypatch.setattr(pty_session_registry, "start", fake_start)
    result = _tool(tmp_path).execute(
        {
            "action": "start",
            "command": "python -q",
            "__sandbox_write_roots": [str(task_root)],
            "__sandbox_read_roots": [str(tmp_path / "shared")],
        }
    )

    assert result.ok is False
    assert result.error_code == "COMMAND_FAILED"
    assert captured["write_roots"] == (task_root.resolve(),)
    assert captured["read_roots"] == ((tmp_path / "shared").resolve(),)
    # 终端会话与前台/后台命令一样，把 Shell 工具的拒读根交给沙箱（未配置时为空）。
    assert captured["private_roots"] == ()


def test_terminal_session_scope_rejects_other_task(tmp_path: Path) -> None:
    from agent_py_agent.agent.tooling.pty_sessions import _pty_access_scope

    first = _pty_access_scope(
        tmp_path,
        (tmp_path / "task-a",),
        run_scope={"owner_id": "owner-a", "session_id": "thread-a"},
    )
    same = _pty_access_scope(
        tmp_path,
        (tmp_path / "task-a",),
        run_scope={"owner_id": "owner-a", "session_id": "thread-a"},
    )
    other = _pty_access_scope(
        tmp_path,
        (tmp_path / "task-b",),
        run_scope={"owner_id": "owner-a", "session_id": "thread-a"},
    )
    other_conversation = _pty_access_scope(
        tmp_path,
        (tmp_path / "task-a",),
        run_scope={"owner_id": "owner-a", "session_id": "thread-b"},
    )
    other_read = _pty_access_scope(
        tmp_path,
        (tmp_path / "task-a",),
        (tmp_path / "shared-b",),
        run_scope={"owner_id": "owner-a", "session_id": "thread-a"},
    )

    assert first == same
    assert first != other
    assert first != other_conversation
    assert first != other_read


# LLM: 只创建测试自己的本地进程，不借用 Gateway 或用户终端；fixture 负责最终回收。
# 函数用途: 用真实 PTY 验证宿主身份选择和进程退出，不靠模型汇报断言停止。
def _start_owned_pty(tmp_path, **identity):
    scope = {
        "owner_home": str(tmp_path), "session_id": "thread-a", "root_task_id": "task-a",
        "task_id": "gateway:task-a", "run_id": "run-a", "attempt_id": "attempt-a", **identity,
    }
    command = f"{shlex.quote(sys.executable)} -c 'import time; time.sleep(60)'"
    return pty_session_registry.start(command, tmp_path, run_scope=scope)


# LLM: 返回命令已受理不是退出证据；测试等待 registry 保存的实际核对回执。
# 函数用途: 有界等待测试 PTY 的进程树终止，并同时确认直接进程已经退出。
def _assert_pty_stopped(session):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and not (session.termination and session.termination.confirmed):
        time.sleep(0.02)
    assert session.termination and session.termination.confirmed
    assert session.process.poll() is not None


def test_task_stop_survives_tool_return_and_preserves_other_tasks_and_resume(tmp_path):
    first = _start_owned_pty(tmp_path)
    others = [
        _start_owned_pty(tmp_path, root_task_id="task-b"),
        _start_owned_pty(tmp_path, session_id="thread-b"),
        _start_owned_pty(tmp_path, owner_home=str(tmp_path / "other-owner")),
    ]
    ids = pty_session_registry.request_stop(owner_home=tmp_path, thread_id="thread-a", task_id="task-a")
    resumed = _start_owned_pty(tmp_path, attempt_id="resumed-attempt")
    assert ids == (first.session_id,)
    _assert_pty_stopped(first)
    assert all(session.process.poll() is None for session in [*others, resumed])


def test_run_stop_uses_exact_attempt_and_requires_owner(tmp_path):
    first = _start_owned_pty(tmp_path)
    other_attempt = _start_owned_pty(tmp_path, attempt_id="attempt-b")
    other_run = _start_owned_pty(tmp_path, run_id="run-b")
    assert pty_session_registry.request_stop(owner_home="", run_id="run-a") == ()
    assert pty_session_registry.request_stop(owner_home=tmp_path) == ()
    assert pty_session_registry.request_stop(
        owner_home=tmp_path, run_id="run-a", attempt_id="attempt-a",
    ) == (first.session_id,)
    _assert_pty_stopped(first)
    assert other_attempt.process.poll() is None and other_run.process.poll() is None


@pytest.mark.parametrize("during_popen", [False, True])
def test_stop_during_start_closes_late_process(tmp_path, monkeypatch, during_popen):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from agent_py_agent.agent.common.cancellation import ToolCancelled
    from agent_py_agent.agent.tooling import pty_sessions as pty

    entered, release = threading.Event(), threading.Event()
    owner, attr = (pty.subprocess, "Popen") if during_popen else (pty, "_sandbox_exec")
    original = getattr(owner, attr)

    def hold(*args, **kwargs):
        value = original(*args, **kwargs)
        entered.set()
        assert release.wait(5)
        return value

    monkeypatch.setattr(owner, attr, hold)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_start_owned_pty, tmp_path)
        try:
            assert entered.wait(3)
            ids = pty_session_registry.request_stop(
                owner_home=tmp_path, thread_id="thread-a", task_id="task-a",
            )
            assert len(ids) == 1
        finally:
            release.set()
        with pytest.raises(ToolCancelled):
            future.result(timeout=8)
    assert not pty_session_registry._pending_starts
    session = pty_session_registry.get(ids[0])
    assert (session is not None) is during_popen
    if session is not None:
        _assert_pty_stopped(session)


def test_cancelled_token_never_starts_pty(tmp_path, monkeypatch):
    from agent_py_agent.agent.common.cancellation import (
        CancellationToken,
        ToolCancelled,
        bind_cancellation_token,
    )
    from agent_py_agent.agent.tooling import pty_sessions as pty

    token = CancellationToken()
    token.cancel()
    monkeypatch.setattr(pty.subprocess, "Popen", lambda *a, **kw: pytest.fail("cancelled launch"))
    with bind_cancellation_token(token), pytest.raises(ToolCancelled):
        _start_owned_pty(tmp_path)
    assert not pty_session_registry._sessions and not pty_session_registry._pending_starts


def test_unconfirmed_close_keeps_live_session_and_unknown_effect(tmp_path, monkeypatch):
    from agent_py_agent.agent.tooling import pty_sessions as pty
    from agent_py_agent.agent.tooling.process_registry import ProcessTerminationReceipt

    tool = _tool(tmp_path)
    started = _payload(tool.execute({"action": "start", "command": f"{shlex.quote(sys.executable)} -q"}))
    session = pty_session_registry.get(started["session_id"])
    with monkeypatch.context() as patch:
        patch.setattr(pty, "terminate_process_tree", lambda pid, proc: ProcessTerminationReceipt(
            "SIGTERM", False, None, 1, (pid,),
        ))
        result = tool.execute({"action": "close", "session_id": session.session_id})
        assert result.error_code == "TOOL_OPERATION_OUTCOME_UNKNOWN"
        assert result.effect_outcome == "unknown"
        assert not session.closed and session.process.poll() is None
    pty_session_registry.close(session.session_id)
    _assert_pty_stopped(session)


def test_close_after_natural_exit_does_not_signal_reusable_pid(tmp_path, monkeypatch):
    from agent_py_agent.agent.tooling import pty_sessions as pty

    session = pty_session_registry.start(f"{shlex.quote(sys.executable)} -c 'pass'", tmp_path)
    session.process.wait(timeout=3)
    monkeypatch.setattr(pty, "terminate_process_tree", lambda *a: pytest.fail("already exited PID"))
    pty_session_registry.close(session.session_id)
    assert session.termination.confirmed and session.termination.method == "already_exited"


def test_pty_start_hands_the_private_root_to_the_sandbox(tmp_path: Path, monkeypatch) -> None:
    from agent_py_agent.agent.tooling import pty_sessions

    captured: dict[str, object] = {}

    def fake_sandbox_exec(command, target, owner_home, **kwargs):
        captured.update(kwargs, owner_home=owner_home)
        raise OSError("captured")

    monkeypatch.setattr(pty_sessions, "_sandbox_exec", fake_sandbox_exec)
    with pytest.raises(OSError, match="captured"):
        pty_session_registry.start("true", tmp_path, tmp_path, private_roots=(str(tmp_path / "home"),))

    # start 打包后交给 _spawn，再原样交给沙箱，不在途中丢掉拒读根。
    assert captured["owner_home"] == tmp_path
    assert captured["private_roots"] == (str(tmp_path / "home"),)


# ---------------------------------------------------------------------------
# ptyleak（2026-10-05）：回合/任务收口与空闲兜底回收。
# 本环境不能嵌套 Seatbelt（sandbox-exec 起不来），下面这些用例把 _sandbox_exec 换成直跑命令，
# 专测注册表的“按归属收回”语义；真实沙箱路径由上面的既有用例覆盖（沙箱外复核）。
# ---------------------------------------------------------------------------
@pytest.fixture
def _unsandboxed_spawn(monkeypatch):
    from agent_py_agent.agent.tooling import pty_sessions as pty

    monkeypatch.setattr(pty, "_sandbox_exec", lambda command, target, owner_home=None, **kw: (command, True))


# LLM: 回收的判定是“进程真的退出 + 有终止回执”；真机可观测时 termination.confirmed 也应为 True，
#   本环境内核观测受限，不能把 confirmed 当作唯一事实。
# 函数用途: 有界等待测试 PTY 进程被收回并断言回执存在。
def _wait_pty_dead(session, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and session.process.poll() is None:
        time.sleep(0.02)
    assert session.process.poll() is not None, "PTY 进程未被收回"
    while time.monotonic() < deadline and session.termination is None:
        time.sleep(0.02)
    assert session.termination is not None, "缺少终止回执"


def test_reclaim_pty_sessions_kills_task_sessions_and_preserves_others(tmp_path, _unsandboxed_spawn):
    first = _start_owned_pty(tmp_path)
    other = _start_owned_pty(tmp_path, root_task_id="task-b", run_id="run-b")

    receipt = reclaim_pty_sessions(str(tmp_path), thread_id="thread-a", root_task_id="task-a", run_id="run-a")

    assert receipt["status"] == "requested"
    assert receipt["session_ids"] == [first.session_id]
    _wait_pty_dead(first)
    assert other.process.poll() is None


def test_reclaim_pty_sessions_task_scope_covers_other_runs_of_same_task(tmp_path, _unsandboxed_spawn):
    # 任务范围收回覆盖同一任务其它 run 遗留的会话（回合收口扫全任务），别的任务不受影响。
    earlier = _start_owned_pty(tmp_path, run_id="run-old")
    other = _start_owned_pty(tmp_path, root_task_id="task-b", run_id="run-b")

    receipt = reclaim_pty_sessions(str(tmp_path), thread_id="thread-a", root_task_id="task-a", run_id="run-new")

    assert receipt["session_ids"] == [earlier.session_id]
    _wait_pty_dead(earlier)
    assert other.process.poll() is None


def test_reclaim_pty_sessions_falls_back_to_run_identity(tmp_path, _unsandboxed_spawn):
    session = _start_owned_pty(tmp_path)
    other = _start_owned_pty(tmp_path, run_id="run-b")

    receipt = reclaim_pty_sessions(str(tmp_path), run_id="run-a")

    assert receipt["scope"] == "run" and receipt["session_ids"] == [session.session_id]
    _wait_pty_dead(session)
    assert other.process.poll() is None


def test_reclaim_pty_sessions_without_owner_or_identity_is_not_selected(tmp_path, _unsandboxed_spawn):
    session = _start_owned_pty(tmp_path)
    try:
        assert reclaim_pty_sessions("")["status"] == "not_selected"
        assert reclaim_pty_sessions(str(tmp_path))["status"] == "not_selected"
        assert session.process.poll() is None
    finally:
        pty_session_registry.close(session.session_id)


def test_idle_sweep_reclaims_idle_sessions_only(tmp_path, _unsandboxed_spawn):
    idle = _start_owned_pty(tmp_path)
    busy = _start_owned_pty(tmp_path, run_id="run-b")
    pty_session_registry.idle_timeout_seconds = 60.0
    try:
        idle.last_active_at = time.time() - 120
        busy.last_active_at = time.time()
        assert pty_session_registry.sweep_idle() == (idle.session_id,)
        _wait_pty_dead(idle)
        assert busy.process.poll() is None
        # 阈值 0 = 不限制：再空闲也不收。
        pty_session_registry.idle_timeout_seconds = 0.0
        busy.last_active_at = time.time() - 10_000
        assert pty_session_registry.sweep_idle() == ()
        assert busy.process.poll() is None
    finally:
        pty_session_registry.idle_timeout_seconds = 0.0


def test_idle_sweeper_thread_starts_once_and_is_daemon(tmp_path, _unsandboxed_spawn):
    pty_session_registry.set_idle_timeout_minutes(30)
    thread = pty_session_registry._idle_thread
    assert thread is not None and thread.is_alive() and thread.daemon
    pty_session_registry.set_idle_timeout_minutes(30)
    assert pty_session_registry._idle_thread is thread
    pty_session_registry.idle_timeout_seconds = 0.0


# LLM: 终止确认依赖宿主能读完整进程表（本沙箱 /bin/ps 不可用，confirmed 恒为 False）并真跑沙箱内 PTY；
#   这条只在具备能力的环境跑：默认缺能力按环境 skip，MY_AGENT_TEST_REQUIRE_CAPABILITIES=1 时缺能力直接失败，
#   防止 3a 沙箱外复跑时静默漏跑（与 b7lnx 同一开关与 marker 机制）。
# 函数用途: 在真机上断言回收回执 termination.confirmed=True。
@pytest.mark.sandbox_capability("ps", "nested_sandbox_exec")
def test_reclaim_pty_sessions_confirms_termination_outside_sandbox(tmp_path):
    session = _start_owned_pty(tmp_path)
    receipt = reclaim_pty_sessions(str(tmp_path), thread_id="thread-a", root_task_id="task-a", run_id="run-a")
    assert receipt["status"] == "requested" and receipt["session_ids"] == [session.session_id]
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and not (session.termination and session.termination.confirmed):
        time.sleep(0.02)
    assert session.termination is not None and session.termination.confirmed is True
    assert session.process.poll() is not None


def test_subprocess_env_defaults_pagers_to_cat(monkeypatch):
    from agent_py_agent.agent.tooling.shell import _subprocess_text_env

    monkeypatch.delenv("PAGER", raising=False)
    monkeypatch.delenv("GIT_PAGER", raising=False)
    env = _subprocess_text_env(None)
    # PTY/交互命令默认不分页：分页器等按键会让命令永不退出（ptyleak 根因）。
    assert env["PAGER"] == "cat" and env["GIT_PAGER"] == "cat"


def test_subprocess_env_keeps_explicit_pagers(monkeypatch):
    from agent_py_agent.agent.tooling.shell import _subprocess_text_env

    monkeypatch.setenv("PAGER", "less")
    monkeypatch.setenv("GIT_PAGER", "delta")
    env = _subprocess_text_env(None)
    # 调用方显式给了值就尊重。
    assert env["PAGER"] == "less" and env["GIT_PAGER"] == "delta"


# 函数用途: 并发设置空闲阈值只启动一条巡检线程（3a 终审：原实现在锁外 start，“已登记未启动”窗口里并发调用者会再开一条）。
def test_idle_sweeper_starts_once_under_concurrent_configure(monkeypatch):
    import threading
    from types import SimpleNamespace

    from agent_py_agent.agent.tooling import pty_sessions as module

    registry = module.PtySessionRegistry()
    starts = []
    rivals = []

    class _ProbeThread:
        def __init__(self, target=None, args=(), name="", daemon=None):
            self.started = False

        def is_alive(self):
            return self.started

        def start(self):
            starts.append(self)
            if len(starts) == 1:
                # 在“已登记、尚未启动完成”的窗口里插入一个并发调用者
                rival = threading.Thread(target=module._configure_idle_timeout, args=(registry, 30))
                rivals.append(rival)
                rival.start()
                rival.join(0.3)
            self.started = True

    monkeypatch.setattr(module, "threading", SimpleNamespace(Thread=_ProbeThread, Lock=threading.Lock))
    module._configure_idle_timeout(registry, 30)
    for rival in rivals:
        rival.join(5)
    assert len(starts) == 1, "并发设置只能启动一条巡检线程"
