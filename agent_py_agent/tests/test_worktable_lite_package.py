"""worktable-lite 真实标准包、独立 Python、原 MCP 客户端与展示服务的组件验收；不代替 TUI 验收。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from agent_py_agent.agent.plugin_display.protocol import (
    MAX_LINE_CHARS,
    MAX_TEXT_LINES,
    normalize_display,
)
from agent_py_agent.agent.plugin_display.service import PanelQuery, PluginDisplayService
from agent_py_agent.agent.plugin_manifest import canonical_plugin_settings
from agent_py_agent.agent.plugin_package import inspect_plugin_package
from agent_py_agent.agent.tooling.mcp_client import MCPStdioClient
from agent_py_agent.tests.test_mcp_client import _config
from agent_py_agent.tests.test_plugin_api_build import installed_sdk  # noqa: F401
from agent_py_agent.tests.test_plugin_display_service import _ManualExecutor
from scripts.build_plugin_api import ROOT
from scripts.build_plugin_package import build_plugin_package

HINT = "回到某个会话：my-agent resume <会话编号>"


# LLM: 插件从实际源码经标准后端构建；临时环境只安装包内 wheel，不注入源目录。
# 函数用途: 构建并安装 worktable-lite，返回解释器、包描述和 wheel 内声明。
@pytest.fixture(scope="module")
def installed_worktable(request, tmp_path_factory):
    python, _sdk = request.getfixturevalue("installed_sdk")
    root = tmp_path_factory.mktemp("worktable-lite-package")
    bundle = build_plugin_package(ROOT / "plugins/worktable-lite", "worktable_lite/declaration.json", (),
                                  root / "worktable.zip")
    package = inspect_plugin_package(bundle.read_bytes())
    with ZipFile(bundle) as archive:
        name = package.manifest.entry_wheel
        wheel = root / name.rsplit("/", 1)[1]
        wheel.write_bytes(archive.read(name))
    with ZipFile(wheel) as archive:
        declaration = json.loads(archive.read("worktable_lite/declaration.json"))
    result = subprocess.run([sys.executable, "-m", "pip", "--isolated", "--python", str(python),
                             "install", "--no-index", "--no-deps", "--no-cache-dir", str(wheel)],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    return python, package.manifest, declaration


# LLM: 与宿主相同：设置经 MY_AGENT_PLUGIN_SETTINGS 在进程启动时注入；每组设置独立服务与进程，结束即回收。
# 函数用途: 用给定设置启动真实插件进程，按给定会话行渲染一次面板并返回文本行。
def render(installed, sessions: list[dict], settings: dict | None = None) -> list[str]:
    python, manifest, _declaration = installed
    installation = SimpleNamespace(manifest=manifest, activation=SimpleNamespace(activation_id="act-1"), enabled=True)
    env = {} if settings is None else {"MY_AGENT_PLUGIN_SETTINGS": json.dumps(settings)}
    clients = []

    def factory(_owner, row):
        client = MCPStdioClient(_config("", name="worktable-lite", command=str(python),
                                        args=["-I", "-m", "worktable_lite"], env=env))
        client.activation_ref = SimpleNamespace(require=lambda: row)
        clients.append(client)
        return client

    executor = _ManualExecutor()
    service = PluginDisplayService(installations=lambda _owner: (installation,), client_factory=factory,
                                   executor=executor)
    query = PanelQuery(object(), "owner", "thread:t", {}, (("worktable-lite", "sessions"),),
                       sessions=lambda: sessions)
    try:
        service.panels(query)
        executor.run_all()
        panel = service.panels(query)[0]
        assert panel["state"] == "ready", panel
        # 插件自身的输出仍要通过核心校验且不触发截断
        assert normalize_display("text", {"lines": panel["display"]["lines"]}) == panel["display"]
        lines = panel["display"]["lines"]
        assert panel["display"]["truncated"] is False and len(lines) <= MAX_TEXT_LINES
        assert all(len(line) <= MAX_LINE_CHARS for line in lines)
        assert clients[0].list_tools() == []
    finally:
        service.close()
    assert clients[0].is_closed()
    return lines


# 函数用途: 生成一条宿主清洗后形状的会话行。
def _row(session_id: str, updated_at, *, current: bool = False, channel: str = "tui") -> dict:
    return {"session_id": session_id, "updated_at": updated_at, "created_at": updated_at,
            "channel": channel, "current": current}


def test_package_is_display_only_with_preference_settings(installed_worktable):
    _python, manifest, declaration = installed_worktable
    assert manifest.tools == ()
    assert [(panel.id, panel.kind, panel.topics) for panel in manifest.panels] == [("sessions", "text", ("sessions",))]
    assert [(action.name, action.kind, action.target) for action in manifest.actions] == [("list", "display", "sessions")]
    assert declaration["summary"] == "在面板查看本用户最近的会话，并给出回到原会话的命令"
    schema = manifest.settings_schema
    assert set(schema["properties"]) == {"max_rows", "hide_current"}
    # 宿主用原 schema 校验器接受合法偏好、拒绝越界值
    canonical_plugin_settings({"max_rows": 20, "hide_current": True}, schema)
    for bad in ({"max_rows": 0}, {"max_rows": 21}, {"hide_current": "yes"}, {"other": 1}):
        with pytest.raises(ValueError):
            canonical_plugin_settings(bad, schema)


def test_empty_list(installed_worktable):
    assert render(installed_worktable, []) == ["还没有会话记录"]


def test_multiple_sessions_with_current_and_relative_time(installed_worktable):
    now = time.time()
    rows = [_row("s-now", now - 5, current=True), _row("s-min", now - 5 * 60 - 20, channel="feishu"),
            _row("s-hour", now - 3 * 3600 - 60), _row("s-day", now - 2 * 86400 - 60, channel="")]
    assert render(installed_worktable, rows) == [
        "▶ s-now · 刚刚 · tui（当前）",
        "  s-min · 5 分钟前 · feishu",
        "  s-hour · 3 小时前 · tui",
        "  s-day · 2 天前 · 渠道未知",
        HINT,
    ]


def test_max_rows_truncates(installed_worktable):
    now = time.time()
    rows = [_row(f"s-{index}", now - index * 86400 - 60) for index in range(1, 11)]
    lines = render(installed_worktable, rows, {"max_rows": 3})
    assert lines == ["  s-1 · 1 天前 · tui", "  s-2 · 2 天前 · tui", "  s-3 · 3 天前 · tui", HINT]
    # 默认最多 8 条
    assert len(render(installed_worktable, rows)) == 9
    # 上限 20 条时会话行让出一行给提示，不被核心截断
    many = [_row(f"s-{index}", now) for index in range(20)]
    lines = render(installed_worktable, many, {"max_rows": 20})
    assert len(lines) == MAX_TEXT_LINES and lines[-1] == HINT


def test_hide_current(installed_worktable):
    now = time.time()
    rows = [_row("s-cur", now, current=True), _row("s-old", now - 120)]
    assert render(installed_worktable, rows, {"hide_current": True}) == ["  s-old · 2 分钟前 · tui", HINT]
    assert render(installed_worktable, rows[:1], {"hide_current": True}) == ["没有其他会话记录"]


def test_missing_time(installed_worktable):
    row = {"session_id": "s-x", "updated_at": None, "created_at": None, "channel": "tui", "current": False}
    assert render(installed_worktable, [row]) == ["  s-x · 时间未知 · tui", HINT]


def test_invalid_settings_stop_without_leaking_value(installed_worktable):
    python, _, _ = installed_worktable
    environment = dict(os.environ, MY_AGENT_PLUGIN_SETTINGS=json.dumps({"max_rows": "private-invalid-value"}))
    result = subprocess.run([str(python), "-I", "-m", "worktable_lite"], env=environment,
                            input="", capture_output=True, text=True, timeout=15)
    assert result.returncode == 2 and result.stdout == ""
    assert "插件设置无效" in result.stderr and "private-invalid-value" not in result.stderr
