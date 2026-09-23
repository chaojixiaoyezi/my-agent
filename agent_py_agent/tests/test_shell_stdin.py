"""普通批处理命令不能读取 Gateway/TUI 的输入；显式管道与独立 PTY 不混用。"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.attempt.sandbox import AttemptExecutionSandbox
from agent_py_agent.agent.subagents.shell_gateway_execution import _run_subprocess_with_budget
from agent_py_agent.agent.tooling import shell


@pytest.mark.skipif(os.name == "nt", reason="真实 POSIX 管道；Windows spawn 参数另由代码路径保持一致")
@pytest.mark.parametrize("entry", ["foreground", "controlled", "attempt"])
@pytest.mark.parametrize("explicit_pipe", [False, True])
def test_batch_shell_never_consumes_host_stdin(tmp_path, monkeypatch, entry, explicit_pipe):
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"gateway-private-input\n")
    os.close(write_fd)
    popen = subprocess.Popen
    observed = []

    # LLM: 模拟启动宿主的输入管道；缺失 stdin 时才代入该管道，子命令仍是真实进程。
    # 函数用途: 在不读取 pytest 或用户终端输入的前提下，复现继承输入与跨进程抢读。
    def spawn(*args, **kwargs):
        observed.append(kwargs.get("stdin"))
        kwargs.setdefault("stdin", read_fd)
        return popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", spawn)
    script = "import sys; print(repr(sys.stdin.readline()))"
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"
    if explicit_pipe:
        command = "printf 'provided\\n' | " + command
    argv = ["/bin/sh", "-c", command]
    try:
        if entry == "foreground":
            monkeypatch.setattr(shell, "_sandbox_exec", lambda *a: (argv, False))
            tool = SimpleNamespace(path_access_policy=SimpleNamespace(owner_scope_root=None), protected_persona_root=None)
            result = shell._run_attempt_sandboxed_shell_command(tool, command, tmp_path, 5, None, None, None)
            output = result.stdout
        elif entry == "controlled":
            decision = SimpleNamespace(argv=argv, cwd=str(tmp_path))
            result = _run_subprocess_with_budget(decision, 1000, 1000, 5)
            output = result.stdout.decode()
        else:
            sandbox = object.__new__(AttemptExecutionSandbox)
            monkeypatch.setattr(sandbox, "require_ready", lambda: None)
            monkeypatch.setattr(sandbox, "build_argv", lambda args: args)
            result = sandbox.run(argv, timeout=5)
            output = result.stdout
        # 前台收尾可能启动只读身份探测；所有命令都必须关闭宿主输入。
        assert observed and all(stdin == subprocess.DEVNULL for stdin in observed)
        assert output.strip() == ("'provided\\n'" if explicit_pipe else "''")
        assert os.read(read_fd, 100) == b"gateway-private-input\n"
    finally:
        os.close(read_fd)
