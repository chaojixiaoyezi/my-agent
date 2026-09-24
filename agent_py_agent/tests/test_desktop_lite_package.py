"""desktop-lite 真实标准包、独立 Python 与原 MCP 客户端的组件验收；不代替 TUI、审批或真实桌面验收。

所有系统程序都用假可执行脚本经设置注入：脚本把收到的 argv 与 stdin 记到文件，测试里从不真的弹通知、打开应用或改剪贴板。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from zipfile import ZipFile

import pytest

from agent_py_agent.agent.plugin_package import inspect_plugin_package
from agent_py_agent.agent.tooling.mcp_client import MCPStdioClient
from agent_py_agent.agent.workspace_read_context import WORKSPACE_READ_EXTENSION
from agent_py_agent.tests.test_mcp_client import _config
from agent_py_agent.tests.test_plugin_api_build import installed_sdk  # noqa: F401
from agent_py_agent.tests.test_workspace_read_context import read_context
from scripts.build_plugin_api import ROOT
from scripts.build_plugin_package import build_plugin_package

pytestmark = pytest.mark.skipif(sys.platform != "darwin" and not sys.platform.startswith("linux"),
                                reason="desktop-lite 只实现 macOS 与 Linux")
NOTIFY_SETTING = "osascript_path" if sys.platform == "darwin" else "notify_send_path"
TRICKY = 'a"b\\c" & do shell script "touch /tmp/pwned" & "'


# LLM: SDK/插件均从实际源码经标准后端构建；临时环境只安装包内入口 wheel，不注入源目录。
# 函数用途: 为组件测试安装真正的 desktop-lite 发行包，并返回原 manifest。
@pytest.fixture(scope="module")
def installed_desktop(request, tmp_path_factory):
    python, sdk = request.getfixturevalue("installed_sdk")
    root = tmp_path_factory.mktemp("desktop-lite-package")
    bundle = build_plugin_package(ROOT / "plugins/desktop-lite", "desktop_lite/declaration.json", (sdk,), root / "dl.zip")
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


@pytest.fixture
def workspace(tmp_path):
    path = tmp_path / "ws"
    path.mkdir()
    return path


# 函数用途: 生成一个可执行的假系统程序；记录 pid/argv/stdin 后按 mode 正常退出、失败退出或长睡。
def fake_program(tmp_path, mode: str = "ok"):
    record = tmp_path / "fake-record.json"
    script = tmp_path / f"fake-{mode}"
    script.write_text(f"""#!{sys.executable}
import json, os, sys, time
data = sys.stdin.buffer.read().decode("utf-8")
with open({str(record)!r}, "w") as handle:
    json.dump({{"pid": os.getpid(), "argv": sys.argv[1:], "stdin": data}}, handle)
if {mode!r} == "sleep":
    time.sleep(60)
sys.exit(3 if {mode!r} == "fail" else 0)
""")
    script.chmod(0o755)
    return script, record


# LLM: 按宿主相同方式传入设置环境变量；进程由原 MCP 客户端启动与回收。
# 函数用途: 启动一个安装后的 desktop-lite MCP 进程。
def start(installed, settings=None):
    python, _ = installed
    env = {} if settings is None else {"MY_AGENT_PLUGIN_SETTINGS": json.dumps(settings)}
    client = MCPStdioClient(_config("", name="desktop-lite", command=str(python),
                                   args=["-I", "-m", "desktop_lite"], env=env))
    client.start()
    return client


# 函数用途: 带逐次读取上下文调用工具并解析 JSON 正文。
def invoke(client, workspace, tool, **arguments):
    meta = {WORKSPACE_READ_EXTENSION: read_context(workspace).to_payload()}
    result = client.call_tool(tool, arguments, request_meta=meta)
    return result["isError"], json.loads(result["content"])


def test_tools_match_manifest_and_are_mutating(installed_desktop):
    _, manifest = installed_desktop
    client = start(installed_desktop)
    try:
        assert {tool.name: tool.input_schema for tool in client.list_tools()} == {
            tool.name: tool.input_schema for tool in manifest.tools}
        assert {tool.name: tool.requested_effect for tool in manifest.tools} == {
            "notify": "mutating", "open": "mutating", "clipboard": "mutating"}
        result = client.call_tool("notify", {"title": "t", "message": "m"})
        assert result["isError"] and json.loads(result["content"])["code"] == "MISSING_CONTEXT"
    finally:
        client.stop()


def test_notify_passes_text_as_argv_without_injection(installed_desktop, workspace, tmp_path):
    script, record = fake_program(tmp_path)
    client = start(installed_desktop, {NOTIFY_SETTING: str(script)})
    try:
        error, result = invoke(client, workspace, "notify", title=TRICKY, message="第二行\n" + TRICKY)
        bad = [invoke(client, workspace, "notify", **arguments)[1]["code"] for arguments in (
            {"title": "x" * 81, "message": "m"}, {"title": "t", "message": "x" * 301},
            {"title": "", "message": "m"}, {"title": "t\x00", "message": "m"}, {"title": "t"})]
    finally:
        client.stop()
    assert not error and result == {"tool": "notify", "program": str(script), "exit_code": 0}
    seen = json.loads(record.read_text())
    if sys.platform == "darwin":
        assert seen["argv"] == ["-", TRICKY, "第二行\n" + TRICKY]
        assert seen["stdin"] == ("on run argv\n\tdisplay notification (item 2 of argv) "
                                 "with title (item 1 of argv)\nend run\n")
    else:
        assert seen["argv"] == ["--", TRICKY, "第二行\n" + TRICKY] and seen["stdin"] == ""
    assert bad == ["INVALID_ARGUMENTS"] * 5


def test_open_rejects_unsafe_paths_and_passes_absolute_path(installed_desktop, workspace, tmp_path):
    script, record = fake_program(tmp_path)
    (workspace / "docs").mkdir()
    (workspace / "docs/报告.pdf").write_bytes(b"%PDF-1.4")
    (workspace / "link.pdf").symlink_to(workspace / "docs/报告.pdf")
    (workspace / "run.command").write_text("echo hi")
    (workspace / "Tool.APP").write_text("x")
    (workspace / "plain").write_text("x")
    (workspace / "plain").chmod(0o755)
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"%PDF")
    client = start(installed_desktop, {"open_path": str(script)})
    try:
        cases = [("link.pdf", "UNSAFE_PATH"), ("docs", "UNSAFE_PATH"), ("none.pdf", "FILE_NOT_FOUND"),
                 ("docs/../docs/报告.pdf", "INVALID_PATH"), (str(outside), "PATH_READ_SCOPE_BLOCKED"),
                 ("run.command", "BLOCKED_FILE_TYPE"), ("Tool.APP", "BLOCKED_FILE_TYPE"),
                 ("plain", "BLOCKED_FILE_TYPE")]
        for path, code in cases:
            error, result = invoke(client, workspace, "open", path=path)
            assert error and result["code"] == code, (path, result)
        assert not record.exists()
        error, result = invoke(client, workspace, "open", path="docs/报告.pdf")
    finally:
        client.stop()
    target = str(workspace / "docs/报告.pdf")
    assert not error and result == {"tool": "open", "program": str(script), "exit_code": 0, "path": target}
    assert json.loads(record.read_text())["argv"] == [target]


def test_clipboard_sends_text_through_stdin(installed_desktop, workspace, tmp_path):
    script, record = fake_program(tmp_path)
    text = "复制 $(rm -rf ~) `id`\n" + TRICKY
    client = start(installed_desktop, {"clipboard_path": str(script)})
    try:
        error, result = invoke(client, workspace, "clipboard", text=text)
    finally:
        client.stop()
    assert not error and result == {"tool": "clipboard", "program": str(script), "exit_code": 0, "chars": len(text)}
    seen = json.loads(record.read_text())
    assert seen["stdin"] == text and seen["argv"] == []


def test_missing_program_and_failure_are_reported(installed_desktop, workspace, tmp_path):
    script, _ = fake_program(tmp_path, "fail")
    (workspace / "a.pdf").write_bytes(b"%PDF")
    missing = str(tmp_path / "no/such/program")
    client = start(installed_desktop, {NOTIFY_SETTING: missing, "open_path": missing, "clipboard_path": str(script)})
    try:
        _, notify = invoke(client, workspace, "notify", title="t", message="m")
        _, opened = invoke(client, workspace, "open", path="a.pdf")
        error, copied = invoke(client, workspace, "clipboard", text="x")
    finally:
        client.stop()
    assert notify["code"] == "DESKTOP_UNAVAILABLE" and notify["message"].startswith("桌面通知不可用：")
    assert missing in notify["message"] and NOTIFY_SETTING in notify["message"]
    assert opened["code"] == "DESKTOP_UNAVAILABLE" and opened["message"].startswith("打开文件不可用：")
    assert error and copied["code"] == "COMMAND_FAILED" and copied["exit_code"] == 3 and copied["program"] == str(script)


def test_timeout_kills_program(installed_desktop, workspace, tmp_path):
    script, record = fake_program(tmp_path, "sleep")
    client = start(installed_desktop, {"clipboard_path": str(script), "command_timeout_seconds": 1})
    try:
        error, result = invoke(client, workspace, "clipboard", text="hello")
    finally:
        client.stop()
    assert error and result["code"] == "COMMAND_TIMEOUT" and result["timeout_seconds"] == 1
    assert result["program"] == str(script)
    with pytest.raises(ProcessLookupError):
        os.kill(json.loads(record.read_text())["pid"], 0)


def test_invalid_settings_fail_startup(installed_desktop):
    python, _ = installed_desktop
    for settings in ({"command_timeout_seconds": 0}, {"command_timeout_seconds": True}, {"unknown": ""}):
        environment = dict(os.environ, MY_AGENT_PLUGIN_SETTINGS=json.dumps(settings))
        result = subprocess.run([str(python), "-I", "-m", "desktop_lite"], env=environment,
                                input="", capture_output=True, text=True, timeout=15)
        assert result.returncode == 2 and "插件设置无效" in result.stderr, settings
