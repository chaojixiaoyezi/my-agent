"""context-inspector 真实标准包、独立 Python、原 MCP 客户端与展示服务的组件验收；不代替 TUI 验收。"""

from __future__ import annotations

import json
import subprocess
import sys
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from agent_py_agent.agent.plugin_display.protocol import MAX_STATUS_FIELDS, normalize_display
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
# 函数用途: 构建并安装 context-inspector，返回解释器、包描述和 wheel 内声明。
@pytest.fixture(scope="module")
def installed_inspector(request, tmp_path_factory):
    python, _sdk = request.getfixturevalue("installed_sdk")
    root = tmp_path_factory.mktemp("context-inspector-package")
    bundle = build_plugin_package(ROOT / "plugins/context-inspector", "context_inspector/declaration.json", (),
                                  root / "inspector.zip")
    package = inspect_plugin_package(bundle.read_bytes())
    with ZipFile(bundle) as archive:
        name = package.manifest.entry_wheel
        wheel = root / name.rsplit("/", 1)[1]
        wheel.write_bytes(archive.read(name))
    with ZipFile(wheel) as archive:
        declaration = json.loads(archive.read("context_inspector/declaration.json"))
    result = subprocess.run([sys.executable, "-m", "pip", "--isolated", "--python", str(python),
                             "install", "--no-index", "--no-deps", "--no-cache-dir", str(wheel)],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    return python, package.manifest, declaration


def test_package_is_display_only_v2(installed_inspector):
    _python, manifest, declaration = installed_inspector
    assert manifest.to_payload()["schema_version"] == PLUGIN_PACKAGE_SCHEMA_V2
    assert manifest.tools == ()
    assert [(panel.id, panel.kind, panel.topics) for panel in manifest.panels] == [("context", "status", ("context",))]
    assert [(action.name, action.kind, action.target) for action in manifest.actions] == [("show", "display", "context")]
    assert declaration["summary"] == "在面板查看当前会话的上下文组成与压缩次数"
    assert declaration["settings_schema"] == {"type": "object", "properties": {}}


def test_real_process_renders_unknown_and_known(installed_inspector):
    python, manifest, _declaration = installed_inspector
    installation = SimpleNamespace(manifest=manifest, activation=SimpleNamespace(activation_id="act-1"), enabled=True)
    clients = []

    def factory(_owner, row):
        client = MCPStdioClient(_config("", name="context-inspector", command=str(python),
                                        args=["-I", "-m", "context_inspector"]))
        client.activation_ref = SimpleNamespace(require=lambda: row)
        clients.append(client)
        return client

    executor = _ManualExecutor()
    service = PluginDisplayService(installations=lambda _owner: (installation,), client_factory=factory,
                                   executor=executor)

    def render(activity: dict) -> dict:
        query = PanelQuery(object(), "owner", "thread:t", activity, (("context-inspector", "context"),))
        service.panels(query)
        executor.run_all()
        panel = service.panels(query)[0]
        assert panel["state"] == "ready", panel
        # 插件自身的输出仍要通过核心校验且不触发截断
        assert normalize_display("status", {"fields": panel["display"]["fields"]}) == panel["display"]
        assert panel["display"]["truncated"] is False and len(panel["display"]["fields"]) <= MAX_STATUS_FIELDS
        return {item["label"]: item["value"] for item in panel["display"]["fields"]}

    try:
        # 缺快照：只提示未知，不显示任何数字（即便压缩次数非零）
        assert render({"compact_count": 3}) == {"上下文": "还没有本会话的上下文快照（发送一条消息后出现）"}
        usage = {"context_window_tokens": 200000, "compact_trigger_tokens": 160000, "current_tokens": 12345,
                 "messages_tokens": 8000, "runtime_guidance_tokens": 3000, "tool_schema_tokens": 1345,
                 "estimated": True}
        assert render({"compact_count": 2, "context_usage": usage}) == {
            "当前用量（估算）": "12,345 / 200,000（6.2%）",
            "自动压缩触发线": "160,000 / 200,000（80.0%）",
            "消息历史": "8,000",
            "运行引导": "3,000",
            "工具目录": "1,345",
            "已压缩次数": "2",
        }
        exact = render({"compact_count": 0, "context_usage": {**usage, "estimated": False}})
        assert exact["当前用量"] == "12,345 / 200,000（6.2%）" and "当前用量（估算）" not in exact
        assert clients[0].tools_changed is False and clients[0].list_tools() == []
    finally:
        service.close()
    assert clients[0].is_closed()
