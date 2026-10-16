"""PTY独立人格根的产品入口与真实平台投影；不把替代启动当OS保护通过。"""
import json
import re
import shlex
import sys
import time
from pathlib import Path

import pytest

from agent_py_agent.agent.attempt.sandbox import AttemptExecutionSandbox, AttemptSandboxSpec
from agent_py_agent.agent.tooling import pty_sessions as pty
from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions
from agent_py_agent.tests.test_agents_source_protection import protected as protected


# LLM: 每例使用独立注册表；生产clear保留未知责任，测试不能靠它删除另一例的资源记录。
# 函数用途: 隔离来源测试会话并在结束时请求真实关闭，不改变被测退役判据。
@pytest.fixture
def full_terminal(protected, tmp_path, monkeypatch):
    monkeypatch.setattr(pty, "pty_session_registry", pty.PtySessionRegistry())
    project = tmp_path / "unrelated-project"
    project.mkdir()
    tool = pty.TerminalSessionTool(ShellTool(project, options=ShellToolOptions(
        workspace_roots=[project], owner_scope_root="", protected_persona_root=str(protected.home),
        path_access_mode="full",
    )))
    pty.pty_session_registry.clear()
    yield tool, project
    pty.pty_session_registry.clear()


# LLM: 替代平台启动只观测profile；占位Seatbelt路径不用于执行，不代表真实OS保护通过。
# 函数用途: 验证产品入口把独立人格根传进沙箱配置。
def test_full_access_product_start_generates_source_profile_from_explicit_root(protected, full_terminal, monkeypatch):
    tool, project = full_terminal
    captured = {}

    def capture(command, target, owner_home, protected_persona_root, **host_options):
        captured.update(target=target, owner_home=owner_home)
        root = protected_persona_root
        spec = AttemptSandboxSpec(target, target, target, target, full_access=True,
                                  protected_persona_root=Path(root) if root else None,
                                  macos_sandbox_exec="/fake/sandbox-exec")
        argv = AttemptExecutionSandbox(spec)._macos_argv(["/bin/true"])
        captured["profile"] = argv[argv.index("-p") + 1]
        # 此测试观察实际生成的profile与会话，不执行Seatbelt；真实OS另测。
        return [sys.executable, "-c", "pass"], False

    monkeypatch.setattr(pty, "_sandbox_exec", capture)
    result = tool.execute({"action": "start", "command": f"{shlex.quote(sys.executable)} -c pass"})
    assert result.ok, result.output
    assert captured["owner_home"] is None
    assert captured["target"] == project
    for path in (protected.source, protected.control.parent):
        assert f"(literal {json.dumps(str(path))})" in captured["profile"], captured
    session = pty.pty_session_registry.get(json.loads(result.output)["session_id"])
    assert getattr(session.access_scope, "protected_persona_root", "") == str(protected.home)
    session.process.wait(timeout=3)
    assert protected.source.read_bytes() == protected.before
    assert protected.control.read_bytes() == protected.config_before


def test_full_access_session_boundary_does_not_share_handle_between_persona_roots(protected, full_terminal, monkeypatch):
    tool, project = full_terminal
    monkeypatch.setattr(pty, "_sandbox_exec", lambda *a, **kw: ([sys.executable, "-c", "pass"], False))
    result = tool.execute({"action": "start", "command": f"{shlex.quote(sys.executable)} -c pass"})
    assert result.ok, result.output
    assert json.loads(tool.execute({"action": "list"}).output)["count"] == 1
    other_root = protected.root / "owners/local/other"
    other_root.mkdir(parents=True)
    other = pty.TerminalSessionTool(ShellTool(project, options=ShellToolOptions(
        workspace_roots=[project], owner_scope_root="", protected_persona_root=str(other_root),
        path_access_mode="full",
    )))
    assert json.loads(other.execute({"action": "list"}).output)["count"] == 0


def test_terminal_stdin_probe_reads_actual_return_codes(full_terminal, monkeypatch):
    tool, _ = full_terminal
    monkeypatch.setattr(pty, "_sandbox_exec", lambda *a, **kw: (["/bin/bash", "--noprofile", "--norc"], False))
    started = tool.execute({"action": "start", "command": "/bin/bash --noprofile --norc"})
    assert started.ok, started.output
    session_id = json.loads(started.output)["session_id"]
    try:
        assert _terminal_stdin_rc(tool, session_id, "true") == 0
        assert _terminal_stdin_rc(tool, session_id, "false") == 1
    finally:
        exited = tool.execute({"action": "write", "session_id": session_id, "data": "exit", "append_newline": True})
        assert exited.ok, exited.output
        assert pty.pty_session_registry.get(session_id).process.wait(timeout=3) == 0
        closed = tool.execute({"action": "close", "session_id": session_id})
        assert closed.ok, closed.output


# LLM: 工具必须接受stdin并实际读到本次标记和返回码；调用write不代表命令成功。cursor不复用旧输出。
# 函数用途: 在真实PTY里运行一条命令并读取精确返回码，超时直接失败。
def _terminal_stdin_rc(tool, session_id, command):
    marker = "__MC2_" + str(time.time_ns()) + "_"
    written = tool.execute({"action": "write", "session_id": session_id,
                            "data": command + "; printf '" + marker + "%d__\\n' $?", "append_newline": True})
    assert written.ok, written.output
    cursor, output = 0, ""
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        read = tool.execute({"action": "read", "session_id": session_id, "cursor": cursor, "max_bytes": 65536})
        assert read.ok, read.output
        payload = json.loads(read.output)
        output += payload["output"]
        cursor = payload["cursor"]
        found = re.search(re.escape(marker) + r"(\d+)__", output)
        if found:
            return int(found.group(1))
        time.sleep(0.02)
    pytest.fail("PTY本次命令未读到返回码：" + output[-2000:])
