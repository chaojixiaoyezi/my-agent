"""冷进程锁定后端/设置导入顺序，注册往返不构造后端或访问网络。"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


# LLM: 每项都用独立解释器和当前仓库路径；不复用 pytest 的模块缓存，不访问真实配置。
# 函数用途: 证明六个入口单独冷导入时不会触发设置与后端的循环导入。
@pytest.mark.parametrize("module", [
    "agent_py_agent.agent.backends.base",
    "agent_py_agent.agent.backends.registry",
    "agent_py_agent.agent.backends.http",
    "agent_py_agent.agent.backends.typesafe_decision",
    "agent_py_agent.agent.backends.vision_capability",
    "agent_py_agent.agent.settings.model_provider_schema",
])
def test_backend_cold_import_does_not_cycle(module):
    repo = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, "-c", f"import importlib; importlib.import_module({module!r})"],
        cwd=repo,
        env={**os.environ,
             "PYTHONPATH": os.pathsep.join([str(repo), str(repo / "agent_py_agent")]),
             "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    stderr_lines = result.stderr.splitlines()
    assert result.returncode == 0, stderr_lines[-1] if stderr_lines else f"exit code {result.returncode}"


# LLM: 只触碰唯一测试键并在 finally 清理；不构造后端或改变其他注册项。
# 函数用途: 核对登记、查找、名称快照和清理仍保持原有行为。
def test_backend_registry_round_trip_preserves_constructor_identity():
    from agent_py_agent.agent.backends import registry

    # LLM: 作为不可执行的测试哨兵；任何提前构造都会使测试失败。
    # 函数用途: 阻止登记和查找阶段偷偷执行后端构造器。
    def constructor(config):
        raise AssertionError("登记和查找不能执行后端构造器")

    name = "b1r1-probe"
    assert name not in registry.registered_backend_names()
    try:
        registry.register_backend(name, constructor)
        assert registry.resolve_registered_backend(name) is constructor
        assert name in registry.registered_backend_names()
    finally:
        registry._BACKENDS.pop(name, None)
    assert name not in registry.registered_backend_names()
