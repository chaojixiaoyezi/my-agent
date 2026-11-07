"""冷启动导入顺序、完整分片注册与 W1 HTTP 业务路由的运行时回归。"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from agent_py_agent.agent.gateway_parts import business_api_v13 as v13
from agent_py_agent.tests import test_gap_w1_wire_roundtrip as roundtrip

wire = roundtrip.wire


@pytest.mark.parametrize("first_module", [
    "agent_py_agent.agent.gateway_parts.http_service", v13.__name__, *v13.V13_SLICE_MODULES,
])
def test_cold_import_and_http_construction_preserve_complete_registry(tmp_path, first_module):
    # 每个入口用全新解释器，防止 pytest 收集时先加载注册器而掩盖循环依赖。
    script = """
import importlib
import sys
from pathlib import Path

importlib.import_module(sys.argv[1])
from agent_py_agent.agent.gateway_parts.http_service import GatewayHTTPServer
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root

paths = gateway_paths_from_root(Path(sys.argv[2]) / 'gateway')
server = GatewayHTTPServer(0, paths)
assert server.paths is paths

from agent_py_agent.agent.gateway_parts import business_api_v13 as registry
from agent_py_agent.agent.gateway_parts.business_api_v13_gap158_w1 import install_actions

actions = registry._ACTIONS
install_actions()
install_actions()
assert registry._ACTIONS is actions
specs = registry.v13_action_specs()
assert 'llm.gateway.chat_completions' in specs
assert 'llm_records.list' in specs
for name in registry.V13_SLICE_MODULES:
    module = importlib.import_module(name)
    for action in module.V13_ACTIONS:
        assert isinstance(action, registry.V13Action)
        assert specs[action.name].definition is action
"""
    result = subprocess.run(
        [sys.executable, "-c", script, first_module, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("action", ["llm.gateway.test", "llm_records.list"])
def test_registered_w1_business_routes_roundtrip(wire, action):
    status, _, envelope = roundtrip.business(wire, action)
    assert status == 200, envelope
    assert envelope["state"] == "completed" and envelope["error"] is None
    if action == "llm.gateway.test":
        assert envelope["data"]["ok"] is True
        assert envelope["data"]["model"] == "private-alice"
        assert len(wire[2]) == 1
    else:
        assert envelope["data"]["items"] == []
        assert wire[2] == []
