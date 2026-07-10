"""F11⑤ owner-scoped(降权)用户 pip 依赖装进自己家、装完能 import,不污染系统。

机制:run_command 的子进程环境注入 PYTHONUSERBASE=<owner_home>/.local——它既决定 pip --user 装到
哪、也决定 Python user-site 从哪 import,所以普通用户装的依赖落在自己家、装完直接能用,且不写系统
站点。非 venv 时顺带 PIP_USER=1 让 pip 默认 --user。admin 提权(owner_home 空)= 不动 = 可全局装。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from agent_py_agent.agent.tooling.shell import (
    ShellTool,
    ShellToolOptions,
    _apply_owner_scoped_pip_env,
    _subprocess_text_env,
)


def _expected_user_base(owner_home: Path) -> str:
    return str(owner_home.expanduser().resolve(strict=False) / ".local")


def test_pip_env_sets_user_base_and_pip_user_outside_venv(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(sys, "base_prefix", sys.prefix)  # 伪装非 venv
    owner_home = tmp_path / "owners" / "local" / "main"
    env: dict[str, str] = {}
    _apply_owner_scoped_pip_env(env, str(owner_home))
    assert env["PYTHONUSERBASE"] == _expected_user_base(owner_home)
    assert env["PIP_USER"] == "1"


def test_pip_env_no_pip_user_inside_venv(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(sys, "base_prefix", sys.prefix + "__base_differs")  # 伪装 venv
    owner_home = tmp_path / "h"
    env: dict[str, str] = {}
    _apply_owner_scoped_pip_env(env, str(owner_home))
    assert env["PYTHONUSERBASE"] == _expected_user_base(owner_home)
    assert "PIP_USER" not in env  # venv 内不强制 --user(pip 会拒绝)


def test_pip_env_noop_for_admin(tmp_path) -> None:
    env: dict[str, str] = {}
    _apply_owner_scoped_pip_env(env, "")  # admin/单租户:不动,保持全局
    assert "PYTHONUSERBASE" not in env and "PIP_USER" not in env


def test_subprocess_env_carries_user_base(tmp_path) -> None:
    owner_home = tmp_path / "owners" / "local" / "main"
    env = _subprocess_text_env(str(owner_home))
    assert env["PYTHONUSERBASE"] == _expected_user_base(owner_home)


def test_real_python_user_site_lands_in_owner_home(tmp_path) -> None:
    """真起一个 python 子进程,带注入的环境:user-site(pip --user 装包/导入位置)落在 owner home 下。"""
    owner_home = tmp_path / "owners" / "local" / "main"
    env = _subprocess_text_env(str(owner_home))
    proc = subprocess.run(
        [sys.executable, "-c", "import site; print(site.getusersitepackages())"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert _expected_user_base(owner_home) in proc.stdout.strip()


def test_run_command_wires_owner_scope_to_env(tmp_path, monkeypatch) -> None:
    """run_command 端到端:owner-scoped ShellTool 跑 python,子进程实际看到 PYTHONUSERBASE 指向 owner
    home。测试替换隔离启动参数只为观察子进程 env，不代表产品存在无沙箱降级路径。"""
    monkeypatch.setattr(
        "agent_py_agent.agent.tooling.shell._sandbox_exec",
        lambda command, target, owner_home: (command, True),
    )
    monkeypatch.setenv("MY_AGENT_HOME", str(tmp_path / ".my-agent"))
    owner_home = tmp_path / ".my-agent" / "owners" / "local" / "main"
    workspace = tmp_path / "ws"
    workspace.mkdir()
    tool = ShellTool(
        workspace,
        options=ShellToolOptions(workspace_roots=[workspace], owner_scope_root=str(owner_home)),
    )
    result = tool.execute(
        {"command": f"{sys.executable} -c \"import site; print(site.getusersitepackages())\""}
    )
    assert result.ok, result.output
    assert _expected_user_base(owner_home) in result.output
