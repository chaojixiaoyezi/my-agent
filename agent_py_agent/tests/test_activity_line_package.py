"""activity-line 真实标准包、独立 Python、原 MCP 客户端与展示服务的组件验收；不代替 TUI 验收。"""

from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from agent_py_agent.agent.plugin_display.protocol import normalize_display
from agent_py_agent.agent.plugin_display.service import PanelQuery, PluginDisplayService
from agent_py_agent.agent.plugin_manifest import PLUGIN_PACKAGE_SCHEMA_V2
from agent_py_agent.agent.plugin_package import inspect_plugin_package
from agent_py_agent.agent.tooling.mcp_client import MCPStdioClient
from agent_py_agent.tests.test_mcp_client import _config
from agent_py_agent.tests.test_plugin_api_build import installed_sdk  # noqa: F401
from agent_py_agent.tests.test_plugin_display_service import _ManualExecutor
from scripts.build_plugin_api import ROOT
from scripts.build_plugin_package import build_plugin_package


# LLM: 插件从实际源码经标准后端构建；临时环境只安装包内 wheel，不注入源目录。
# 函数用途: 构建并安装 activity-line，返回解释器与包描述。
@pytest.fixture(scope="module")
def installed_line(request, tmp_path_factory):
    python, _sdk = request.getfixturevalue("installed_sdk")
    root = tmp_path_factory.mktemp("activity-line-package")
    bundle = build_plugin_package(ROOT / "plugins/activity-line", "activity_line/declaration.json", (), root / "line.zip")
    package = inspect_plugin_package(bundle.read_bytes())
    with ZipFile(bundle) as archive:
        name = package.manifest.entry_wheel
        wheel = root / name.rsplit("/", 1)[1]
        wheel.write_bytes(archive.read(name))
    result = subprocess.run([sys.executable, "-m", "pip", "--isolated", "--python", str(python),
                             "install", "--no-index", "--no-deps", "--no-cache-dir", str(wheel)],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    return python, package.manifest


def test_package_is_display_only_v2(installed_line):
    _python, manifest = installed_line
    assert manifest.to_payload()["schema_version"] == PLUGIN_PACKAGE_SCHEMA_V2
    assert manifest.tools == () and [panel.id for panel in manifest.panels] == ["line"]
    assert [(action.name, action.kind, action.target) for action in manifest.actions] == [("show", "display", "line")]


def test_real_process_renders_through_display_service(installed_line):
    python, manifest = installed_line
    installation = SimpleNamespace(manifest=manifest, activation=SimpleNamespace(activation_id="act-1"), enabled=True)
    clients = []

    def factory(_owner, row):
        client = MCPStdioClient(_config("", name="activity-line", command=str(python), args=["-I", "-m", "activity_line"]))
        client.activation_ref = SimpleNamespace(require=lambda: row)
        clients.append(client)
        return client

    executor = _ManualExecutor()
    service = PluginDisplayService(installations=lambda _owner: (installation,), client_factory=factory,
                                   executor=executor)
    activity = {"active_task_count": 1, "subagents": [{}],
                "main_activity": {"phase": "tool", "activity": "读取文件", "started_at": 1.0}}
    query = PanelQuery(object(), "owner", "thread:t", activity, (("activity-line", "line"),))
    try:
        assert service.panels(query)[0]["state"] == "loading"
        executor.run_all()
        panel = service.panels(query)[0]
        assert panel["state"] == "ready", panel
        assert panel["display"]["lines"][0] == "● 工作中 · 读取文件"
        assert "子代理 1" in panel["display"]["lines"][1]
        assert clients[0].tools_changed is False and clients[0].list_tools() == []
        # 插件自身的输出仍要通过核心校验
        assert normalize_display("text", {"lines": panel["display"]["lines"]}) == panel["display"]
    finally:
        service.close()
    assert clients[0].is_closed()
