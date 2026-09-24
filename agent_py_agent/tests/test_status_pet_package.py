"""status-pet 真实标准包、独立 Python、原 MCP 客户端与展示服务的组件验收；不代替 TUI 验收。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from agent_py_agent.agent.plugin_display.protocol import (
    MAX_LINE_CHARS,
    MAX_TEXT_LINES,
    normalize_display,
)
from agent_py_agent.agent.plugin_display.service import PanelQuery, PluginDisplayService
from agent_py_agent.agent.plugin_manifest import PLUGIN_PACKAGE_SCHEMA_V2, canonical_plugin_settings
from agent_py_agent.agent.plugin_package import inspect_plugin_package
from agent_py_agent.agent.tooling.mcp_client import MCPStdioClient
from agent_py_agent.tests.test_mcp_client import _config
from agent_py_agent.tests.test_plugin_api_build import installed_sdk  # noqa: F401
from agent_py_agent.tests.test_plugin_display_service import _ManualExecutor
from agent_py_agent.tests.test_plugin_management import manager
from scripts.build_plugin_api import ROOT
from scripts.build_plugin_package import build_plugin_package

_ACTIVITIES = {
    "working": {"active_task_count": 1, "subagents": [{}, {}],
                "main_activity": {"phase": "tool", "activity": "读取文件", "started_at": 1.0}},
    "waiting": {"active_task_count": 1, "subagents": [{}],
                "main_activity": {"phase": "waiting_permission", "activity": "等待审批", "started_at": 1.0}},
    "idle": {},
}


# LLM: 插件从实际源码经标准后端构建；临时环境只安装包内 wheel，不注入源目录。
# 函数用途: 构建并安装 status-pet，返回解释器、包描述与 ZIP 路径。
@pytest.fixture(scope="module")
def installed_pet(request, tmp_path_factory):
    python, _sdk = request.getfixturevalue("installed_sdk")
    root = tmp_path_factory.mktemp("status-pet-package")
    bundle = build_plugin_package(ROOT / "plugins/status-pet", "status_pet/declaration.json", (), root / "pet.zip")
    package = inspect_plugin_package(bundle.read_bytes())
    with ZipFile(bundle) as archive:
        name = package.manifest.entry_wheel
        wheel = root / name.rsplit("/", 1)[1]
        wheel.write_bytes(archive.read(name))
    result = subprocess.run([sys.executable, "-m", "pip", "--isolated", "--python", str(python),
                             "install", "--no-index", "--no-deps", "--no-cache-dir", str(wheel)],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    return python, package.manifest, bundle


# LLM: 每种设置起一个真实插件进程，经展示服务依次渲染三种运行状态；设置走宿主同款环境变量注入。
# 函数用途: 返回 {状态: 核心校验后的面板行}。
def _render_states(installed_pet, settings: dict | None) -> dict:
    python, manifest, _ = installed_pet
    installation = SimpleNamespace(manifest=manifest, activation=SimpleNamespace(activation_id="act-1"), enabled=True)
    env = {} if settings is None else {"MY_AGENT_PLUGIN_SETTINGS": canonical_plugin_settings(settings, manifest.settings_schema)}
    clients = []

    def factory(_owner, row):
        client = MCPStdioClient(_config("", name="status-pet", command=str(python),
                                        args=["-I", "-m", "status_pet"], env=env))
        client.activation_ref = SimpleNamespace(require=lambda: row)
        clients.append(client)
        return client

    executor = _ManualExecutor()
    service = PluginDisplayService(installations=lambda _owner: (installation,), client_factory=factory,
                                   executor=executor)
    rendered = {}
    try:
        for state, activity in _ACTIVITIES.items():
            query = PanelQuery(object(), "owner", "thread:t", activity, (("status-pet", "pet"),))
            service.panels(query)
            executor.run_all()
            panel = service.panels(query)[0]
            assert panel["state"] == "ready", panel
            display = panel["display"]
            # 插件输出必须原样通过核心校验：不截断、不超限、无控制字符
            assert normalize_display("text", {"lines": display["lines"]}) == display
            assert display["truncated"] is False and len(display["lines"]) <= min(5, MAX_TEXT_LINES)
            assert all(len(line) <= MAX_LINE_CHARS for line in display["lines"])
            assert all(ord(ch) >= 32 and ord(ch) != 127 for line in display["lines"] for ch in line)
            rendered[state] = display["lines"]
        assert clients[0].list_tools() == []
    finally:
        service.close()
    assert all(client.is_closed() for client in clients)
    return rendered


def test_package_is_display_only_v2_with_settings(installed_pet):
    _python, manifest, _ = installed_pet
    assert manifest.to_payload()["schema_version"] == PLUGIN_PACKAGE_SCHEMA_V2
    assert manifest.plugin_id == "status-pet" and manifest.summary == "用文字小宠物显示工作、等待、空闲状态"
    assert manifest.tools == () and [(panel.id, panel.kind, panel.topics) for panel in manifest.panels] == [
        ("pet", "text", ("run_state", "activity"))]
    assert [(action.name, action.kind, action.target) for action in manifest.actions] == [("show", "display", "pet")]
    assert set(manifest.settings_schema["properties"]) == {"style", "name"}


def test_default_cat_renders_three_distinct_states(installed_pet):
    rendered = _render_states(installed_pet, None)
    assert rendered["working"][-1] == "小咪 正在工作 · 子代理 2 · 读取文件"
    assert rendered["waiting"][-1].startswith("【等待审批】小咪") and "子代理 1" in rendered["waiting"][-1]
    assert rendered["idle"][-1] == "小咪 空闲中，随时待命"
    assert len({tuple(lines[:-1]) for lines in rendered.values()}) == 3
    assert "( O.O )" in "\n".join(rendered["waiting"]) and "( -.- )" in "\n".join(rendered["idle"])


@pytest.mark.parametrize(("style", "marker"), [("whale", "__><"), ("robot", "[ ^_^ ]")])
def test_style_and_name_settings_take_effect(installed_pet, style, marker):
    rendered = _render_states(installed_pet, {"style": style, "name": "蓝蓝"})
    assert marker in "\n".join(rendered["working"])
    assert "( o.o )" not in "\n".join(rendered["working"])
    assert all("蓝蓝" in lines[-1] and "小咪" not in lines[-1] for lines in rendered.values())
    assert rendered["waiting"][-1].startswith("【等待审批】蓝蓝")
    assert len({tuple(lines[:-1]) for lines in rendered.values()}) == 3


@pytest.mark.parametrize("settings", [{"style": "dragon"}, {"name": ""}, {"name": "一二三四五六七八九十甲乙丙"},
                                      {"style": 1}, {"color": "red"}])
def test_host_schema_rejects_invalid_settings(installed_pet, settings):
    _python, manifest, _ = installed_pet
    with pytest.raises(ValueError):
        canonical_plugin_settings(settings, manifest.settings_schema)


def test_configure_rejects_invalid_then_accepts_valid(installed_pet, tmp_path):
    _, _, bundle = installed_pet
    service, source = manager(tmp_path)
    source.write_bytes(bundle.read_bytes())
    approve = {"request_permission": lambda value, **_: {"permission_id": value["permission_id"], "decision": "approved"}}
    installed = service.command(f'/plugins install "{source}"', revision=service.catalog().revision,
                                request_id="install", **approve)
    assert installed["state"] == "succeeded", installed
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"style": "dragon", "name": "蓝蓝"}))
    invalid = service.command(f'/plugins configure status-pet --file "{settings}"',
                              revision=service.catalog().revision, request_id="bad-settings")
    assert invalid["state"] in {"failed", "rejected"}, invalid
    assert service.installations.snapshot()[0].settings_json is None
    settings.write_text(json.dumps({"style": "whale", "name": "蓝蓝"}))
    valid = service.command(f'/plugins configure status-pet --file "{settings}"',
                            revision=service.catalog().revision, request_id="configure", **approve)
    assert valid["state"] == "succeeded", valid
    assert json.loads(service.installations.snapshot()[0].settings_json) == {"style": "whale", "name": "蓝蓝"}


def test_process_rejects_invalid_settings_without_leaking_value(installed_pet):
    python, _, _ = installed_pet
    environment = dict(os.environ, MY_AGENT_PLUGIN_SETTINGS=json.dumps({"style": "private-invalid-value"}))
    result = subprocess.run([str(python), "-I", "-m", "status_pet"], env=environment,
                            input="", capture_output=True, text=True, timeout=15)
    assert result.returncode == 2 and result.stdout == ""
    assert "插件设置无效" in result.stderr and "private-invalid-value" not in result.stderr
