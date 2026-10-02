"""run_tests.py 导入即报错的守卫用例。

run_tests.py 是手动运行的真实冒烟脚本，模块顶层就会执行 CLI 命令（含真实模型调用和 gateway stop/start）。
09-26 与 10-02 两次被 pytest 点名收集，都在导入阶段打到了真实 ~/.my-agent。守卫保证：被 import 或被收集时
立即报错、什么也不执行；只有 `python3 agent_py_agent/tests/run_tests.py` 手动运行才会跑。

复现方法:
    bash ~/.my-agent/releases/claude-tools/3a-scripts/run_files312.sh <worktree> <basetemp> \
        agent_py_agent/tests/test_run_tests_script_guard.py
"""

from __future__ import annotations

import runpy
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "run_tests.py"


def test_importing_the_smoke_script_raises_before_any_side_effect(tmp_path, monkeypatch):
    # 临时目录被改到 tmp_path：守卫要是没先拦住，脚本建的冒烟工作区会落在这里，用例能看见。
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    with pytest.raises(RuntimeError, match="手动运行的真实冒烟脚本"):
        runpy.run_path(str(SCRIPT), run_name="agent_py_agent.tests.run_tests")
    assert not any(tmp_path.glob("agent-full-smoke-*")), "守卫之前不能建任何冒烟工作区"


def test_pytest_collecting_the_script_by_path_fails_loudly(tmp_path):
    # 点名交给 pytest 时收集直接报错（rc 非 0），而不是悄悄执行脚本。
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", str(SCRIPT), "-q", "-p", "no:cacheprovider",
         "--basetemp", str(tmp_path / "bt"), "-o", "addopts="],
        capture_output=True, text=True, timeout=120,
    )
    assert completed.returncode != 0
    assert "手动运行的真实冒烟脚本" in completed.stdout + completed.stderr
