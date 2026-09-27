"""正在运行的 my-agent 安装目录写保护（2026-09-27，用户担心 my-agent 改自己代码会不会出事）。

锁定：
- 安装目录只按进程事实认定：虚拟环境保护整个环境；装在 site-packages 时只保护包目录；源码检出运行不保护。
- 开关 protect_running_runtime 默认开；关闭时不加保护。
- 文件工具：命中安装目录就拒，更深的允许目录也不能穿过；只有这个键时不会把原本不限范围的写入变成全拒。
- Shell 与终端：安装目录并入沙箱只读路径（Full Access 的 Seatbelt/bwrap 同样执行）。
- 端到端：本机管理员 Full Access 主会话仍能写开发工作树等外部目录，但写不了正在运行的安装目录。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import runtime_write_guards as guards
from agent_py_agent.agent.agent_core.tool_runtime_ledger import write_boundary_with_runtime_ledger
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.tooling.registry_invoke import (
    AuthorizedToolDispatchRequest,
    _tool_params_with_runtime_boundary,
)
from agent_py_agent.agent.tooling.write_boundary import (
    RUNTIME_INSTALL_ROOTS_KEY,
    validate_write_boundary,
)

_ADMIN = SimpleNamespace(owner_provider="local", owner_kind="main", owner_id="main")


def _write(target: Path, boundary: dict[str, object], workspace: Path) -> str:
    return validate_write_boundary(
        "write_file", {"path": str(target), "content": "x"},
        workspace_root=workspace, path_access_mode="full", write_boundary=boundary,
    )


def test_install_roots_follow_process_facts(tmp_path):
    venv, base = tmp_path / "runtime-step13o", tmp_path / "python-base"
    assert guards.install_roots_for(venv, base, venv / "lib" / "site-packages" / "agent_py_agent") == (venv.resolve(),)
    package = tmp_path / "usr" / "lib" / "site-packages" / "agent_py_agent"
    assert guards.install_roots_for(base, base, package) == (package.resolve(),)
    checkout = tmp_path / "my-agent" / "agent_py_agent"
    assert guards.install_roots_for(base, base, checkout) == ()


@pytest.mark.parametrize("enabled", [True, False])
def test_guard_marks_install_dir_only_when_enabled(tmp_path, monkeypatch, enabled):
    runtime = tmp_path / "runtime"
    monkeypatch.setattr(guards, "running_install_roots", lambda: (runtime,))
    boundary: dict[str, object] = {}
    guards.attach_running_install_guard(boundary, SimpleNamespace(protect_running_runtime=enabled))
    assert boundary == ({RUNTIME_INSTALL_ROOTS_KEY: [str(runtime)]} if enabled else {})


def test_file_tools_refuse_install_dir_even_under_a_deeper_allowed_root(tmp_path):
    runtime = tmp_path / "runtime"
    inner = runtime / "lib" / "site-packages" / "agent_py_agent"
    boundary = {RUNTIME_INSTALL_ROOTS_KEY: [str(runtime)], "allowed_write_roots": [str(inner), str(tmp_path / "work")]}
    assert "正在运行的 my-agent 安装目录" in _write(inner / "core.py", boundary, tmp_path)
    assert _write(tmp_path / "work" / "a.py", boundary, tmp_path) == ""


def test_install_key_alone_keeps_unscoped_writes_unscoped(tmp_path):
    boundary = {RUNTIME_INSTALL_ROOTS_KEY: [str(tmp_path / "runtime")]}
    assert _write(tmp_path / "anywhere" / "a.py", boundary, tmp_path) == ""


def test_shell_and_terminal_get_install_dir_as_read_only(tmp_path):
    boundary = {
        "forbidden_write_roots": [str(tmp_path / "control")],
        RUNTIME_INSTALL_ROOTS_KEY: [str(tmp_path / "runtime")],
        "allowed_write_roots": [str(tmp_path / "work")],
    }
    for tool_name in ("run_command", "terminal_session"):
        params = _tool_params_with_runtime_boundary(AuthorizedToolDispatchRequest(
            tool_name=tool_name, tool=SimpleNamespace(), tool_params={"action": "status"},
            workspace_root=tmp_path, write_boundary=boundary,
        ))
        assert params["__sandbox_protected_write_paths"] == [str(tmp_path / "control"), str(tmp_path / "runtime")]


def test_full_access_admin_keeps_external_writes_but_not_the_running_install(tmp_path, monkeypatch):
    runtime, home, worktree = tmp_path / "runtime", tmp_path / "owners" / "main", tmp_path / "my-agent-self"
    monkeypatch.setattr(guards, "running_install_roots", lambda: (runtime,))
    agent = SimpleNamespace(
        config=AgentConfig(access_mode="full-access"),
        home_paths=SimpleNamespace(**vars(_ADMIN), owner_home_dir=home),
        tools=SimpleNamespace(owner_scope_root=None),
    )
    params = SimpleNamespace(write_boundary={"allowed_write_roots": [str(home)]}, context_scope="default", run_id="")
    boundary = write_boundary_with_runtime_ledger(agent, params)
    assert "allowed_write_roots" not in boundary  # Full Access 本来就不是目录白名单
    assert _write(worktree / "agent_py_agent" / "x.py", boundary, home) == ""
    assert "正在运行的 my-agent 安装目录" in _write(runtime / "lib" / "x.py", boundary, home)
    agent.config = AgentConfig(access_mode="full-access", protect_running_runtime=False)
    unguarded = write_boundary_with_runtime_ledger(agent, params)
    assert _write(runtime / "lib" / "x.py", unguarded, home) == ""
