"""真实标准插件包、独立 Python 和原 MCP 客户端的组件验收；不代替 TUI 或模型验收。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from zipfile import ZipFile

import pytest

from agent_py_agent.agent.plugin_manifest import PluginPackageError
from agent_py_agent.agent.plugin_package import inspect_plugin_package
from agent_py_agent.agent.plugin_runtime import plugin_tool_name
from agent_py_agent.agent.tooling.mcp_client import MCPStdioClient
from agent_py_agent.agent.workspace_read_context import WORKSPACE_READ_EXTENSION
from agent_py_agent.tests._tool_runtime_harness import execute_registry_test_call
from agent_py_agent.tests.plugin_activation_fixtures import invoke_registered_tool, plugin_registry
from agent_py_agent.tests.test_mcp_client import _config
from agent_py_agent.tests.test_plugin_api_build import installed_sdk  # noqa: F401
from agent_py_agent.tests.test_plugin_management import manager
from agent_py_agent.tests.test_workspace_read_context import read_context
from scripts.build_plugin_api import ROOT
from scripts.build_plugin_package import build_plugin_package


# LLM: SDK/插件均从实际源码经标准后端构建；临时环境只安装外包声明的 wheel，不注入源目录。
# 函数用途: 为原 MCP 组件测试安装真正的样本发行包，并返回原 manifest 用于同源检查。
@pytest.fixture(scope="module")
def installed_peek(request, tmp_path_factory):
    python, sdk = request.getfixturevalue("installed_sdk")
    root = tmp_path_factory.mktemp("peek-package")
    bundle = build_plugin_package(ROOT / "plugins/workspace-peek", "workspace_peek/declaration.json",
                                  (sdk,), root / "peek.zip")
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


# LLM: 客户端沿原连接/终止实现管理自己的样本进程；不会连接真实 Gateway 或触碰用户安装表。
# 函数用途: 为每个独立场景启动并最终关闭安装后的 MCP 插件。
@pytest.fixture
def peek(installed_peek):
    python, _, _ = installed_peek
    client = MCPStdioClient(_config("", name="workspace-peek", command=str(python),
                                   args=["-I", "-m", "workspace_peek"]))
    try:
        client.start()
        yield client
    finally:
        client.stop()


# LLM: 组件仅传入真实 SDK 协议值；宿主 registry 的上下文组装仍由独立合同测试覆盖，不能称为端到端管理链。
# 函数用途: 发起原 MCP 工具请求并解析插件返回的实际 JSON 正文。
def invoke(client, context, tool="show", **arguments):
    result = client.call_tool(tool, arguments, request_meta={WORKSPACE_READ_EXTENSION: context.to_payload()})
    return result["isError"], json.loads(result["content"])


def test_installed_tools_match_manifest(peek, installed_peek):
    _, manifest, _ = installed_peek
    tools = peek.list_tools()
    assert {tool.name: tool.input_schema for tool in tools} == {tool.name: tool.input_schema for tool in manifest.tools}
    assert peek.server_info == {"name": "workspace-peek", "version": manifest.version}


def test_unicode_preview_pages_preserve_exact_bytes_and_newlines(peek, tmp_path):
    context = read_context(tmp_path)
    content = "a🙂中文\r\n最后一行🙂"
    (tmp_path / "中文 空格.txt").write_bytes(content.encode())
    cursor, pieces, end = None, [], 0
    while True:
        arguments = {"path": "中文 空格.txt", "bytes": 4}
        if cursor:
            arguments["cursor"] = cursor
        error, page = invoke(peek, context, **arguments)
        assert not error, page
        assert page["start"] == end
        pieces.append(page["text"])
        end, cursor = page["end"], page["cursor"]
        if cursor is None:
            assert page["eof"]
            break
    assert "".join(pieces) == content
    assert end == len(content.encode())


@pytest.mark.parametrize("content,code", [(b"", None), (b"abc\x00", "BINARY_CONTENT"), (b"\xff", "NOT_UTF8")])
def test_empty_and_nontext_files(peek, tmp_path, content, code):
    (tmp_path / "file").write_bytes(content)
    error, result = invoke(peek, read_context(tmp_path), path="file")
    assert error is (code is not None)
    assert result.get("code") == code
    if code is None:
        assert result["eof"] and result["text"] == "" and result["cursor"] is None


def test_missing_or_changed_context_never_falls_back_to_process_cwd(peek, tmp_path):
    result = peek.call_tool("show", {"path": "pyproject.toml"})
    assert result["isError"] and json.loads(result["content"])["code"] == "MISSING_CONTEXT"
    (tmp_path / "file").write_text("abcdefgh")
    original = read_context(tmp_path)
    error, first = invoke(peek, original, path="file", bytes=4)
    assert not error
    narrowed = read_context(tmp_path, boundary={"read_scope_mode": "exact", "allowed_read_roots": [str(tmp_path / "file")]})
    error, result = invoke(peek, narrowed, path="file", cursor=first["cursor"])
    assert error and result["code"] == "INVALID_CURSOR"
    empty = read_context(tmp_path, boundary={"read_scope_mode": "exact", "allowed_read_roots": []})
    error, result = invoke(peek, empty, path="file")
    assert error and result["code"] == "PATH_READ_SCOPE_BLOCKED"


def test_changed_file_rejects_original_cursor(peek, tmp_path):
    path = tmp_path / "file"
    path.write_text("abcdefgh")
    context = read_context(tmp_path)
    _, first = invoke(peek, context, path="file", bytes=4)
    path.write_text("changed file")
    error, result = invoke(peek, context, path="file", cursor=first["cursor"])
    assert error and result["code"] == "INVALID_CURSOR"


@pytest.mark.parametrize("kind", ["symlink", "parent_symlink", "hardlink", "fifo", "credential", "traversal"])
def test_unreadable_paths_fail_without_breaking_next_request(peek, tmp_path, kind):
    (tmp_path / "dir").mkdir()
    (tmp_path / "dir/file").write_text("safe")
    candidate = tmp_path / "candidate"
    if kind == "symlink":
        candidate.symlink_to(tmp_path / "dir/file")
    elif kind == "parent_symlink":
        candidate.symlink_to(tmp_path / "dir", target_is_directory=True)
        candidate /= "file"
    elif kind == "hardlink":
        os.link(tmp_path / "dir/file", candidate)
    elif kind == "fifo":
        os.mkfifo(candidate)
    elif kind == "credential":
        candidate = tmp_path / ".env"
        candidate.write_text("private")
    else:
        candidate = tmp_path / "dir/../dir/file"
    context = read_context(tmp_path)
    error, result = invoke(peek, context, path=str(candidate))
    assert error, result
    (tmp_path / "good").write_text("still usable")
    error, result = invoke(peek, context, path="good")
    assert not error and result["text"] == "still usable"


def test_tree_sorted_paging_depth_and_denied_names(peek, tmp_path):
    (tmp_path / "z").write_text("z")
    (tmp_path / "a").mkdir()
    (tmp_path / "a/nested").write_text("nested")
    (tmp_path / ".env").write_text("private")
    (tmp_path / "linked").symlink_to(tmp_path / "a", target_is_directory=True)
    context = read_context(tmp_path)
    error, first = invoke(peek, context, "tree", depth=2, limit=1)
    assert not error and first["denied"] == 2 and first["scanned"] == 5
    assert first["entries"][0]["path"] == "a" and first["total"] == 3
    error, rest = invoke(peek, context, "tree", depth=2, limit=200, cursor=first["cursor"])
    assert not error and [item["path"] for item in rest["entries"]] == ["a/nested", "z"]
    assert rest["cursor"] is None
    error, shallow = invoke(peek, context, "tree", depth=1)
    assert not error and [item["path"] for item in shallow["entries"]] == ["a", "z"]
    (tmp_path / "new").write_text("new")
    error, changed = invoke(peek, context, "tree", depth=2, cursor=first["cursor"])
    assert error and changed["code"] == "INVALID_CURSOR"


def test_concurrent_requests_keep_separate_workspaces(peek, tmp_path):
    contexts = []
    for index in range(3):
        root = tmp_path / str(index)
        root.mkdir()
        (root / "file").write_text(str(index))
        contexts.append(read_context(root))
    with ThreadPoolExecutor(max_workers=3) as executor:
        results = list(executor.map(lambda context: invoke(peek, context, path="file"), contexts))
    assert [(error, value["text"]) for error, value in results] == [(False, "0"), (False, "1"), (False, "2")]


def test_scan_budget_configuration_is_effective(installed_peek, tmp_path):
    python, _, _ = installed_peek
    for name in ("a", "b", "c"):
        (tmp_path / name).write_text(name)
    client = MCPStdioClient(_config("", command=str(python), args=["-I", "-m", "workspace_peek"],
        env={"MY_AGENT_PLUGIN_SETTINGS": json.dumps({"scan_entries": 2, "page_bytes": 4})}))
    try:
        client.start()
        error, result = invoke(client, read_context(tmp_path), "tree")
        assert error and result["code"] == "SCAN_LIMIT" and "cursor" not in result
        (tmp_path / "a").write_text("abcdefgh")
        error, result = invoke(client, read_context(tmp_path), path="a")
        assert not error and result["text"] == "abcd" and result["cursor"]
    finally:
        client.stop()


@pytest.mark.parametrize("arguments", [{"path": "file", "bytes": True}, {"path": "file", "bytes": 0},
                                       {"path": "file", "cursor": "%%%"}, {"path": "file", "unknown": "x"}])
def test_bad_arguments_return_errors(peek, tmp_path, arguments):
    (tmp_path / "file").write_text("abcdefgh")
    error, _ = invoke(peek, read_context(tmp_path), **arguments)
    assert error


def test_package_requires_complete_dependency_set(tmp_path):
    with pytest.raises(PluginPackageError):
        build_plugin_package(ROOT / "plugins/workspace-peek", "workspace_peek/declaration.json", (), tmp_path / "bad.zip")
    assert not (tmp_path / "bad.zip").exists()


def test_invalid_configuration_stops_without_leaking_value(installed_peek):
    python, _, _ = installed_peek
    environment = dict(os.environ, MY_AGENT_PLUGIN_SETTINGS=json.dumps({"page_bytes": "private-invalid-value"}))
    result = subprocess.run([str(python), "-I", "-m", "workspace_peek"], env=environment,
                            input="", capture_output=True, text=True, timeout=15)
    assert result.returncode == 2 and result.stdout == ""
    assert "插件设置无效" in result.stderr and "private-invalid-value" not in result.stderr


def test_invalid_json_does_not_break_next_protocol_request(installed_peek):
    python, _, _ = installed_peek
    requests = "invalid json\n" + json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}) + "\n"
    result = subprocess.run([str(python), "-I", "-m", "workspace_peek"],
                            input=requests, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0 and result.stderr == ""
    malformed, initialized = [json.loads(line) for line in result.stdout.splitlines()]
    assert malformed["error"]["code"] == -32700
    assert initialized["id"] == 1 and initialized["result"]["serverInfo"]["name"] == "workspace-peek"


# LLM: 管理命令始终读取原目录版本，失败立即暴露；审批回调只用于开发组件，不计真实用户审批或 TUI。
# 函数用途: 经原管理入口执行一项成功操作，供实际包生命周期组合复用。
def manage(service, command, request_id):
    result = service.command(command, revision=service.catalog().revision, request_id=request_id,
        request_permission=lambda value, **_: {"permission_id": value["permission_id"], "decision": "approved"})
    assert result["state"] == "succeeded", result
    return result


def test_actual_package_original_install_call_disable_reenable_remove(installed_peek, tmp_path):
    _, _, bundle = installed_peek
    service, source = manager(tmp_path)
    source.write_bytes(bundle.read_bytes())
    revision = service.catalog().revision
    installed = manage(service, f'/plugins install "{source}"', "install")
    replay = service.command(f'/plugins install "{source}"', revision=revision, request_id="install")
    assert replay["state"] == "succeeded" and replay["operation_id"] == installed["operation_id"]
    assert not service.installations.snapshot()[0].enabled
    listed = service.command("/plugins list", revision=service.catalog().revision, request_id="list")
    assert listed["ok"] and "workspace-peek 0.1.0（停用）" in listed["message"]
    assert service.installations.snapshot()[0].manifest.summary in listed["message"]
    info = service.command("/plugins info workspace-peek", revision=service.catalog().revision, request_id="info")
    assert info["ok"] and "workspace-peek 0.1.0（停用）" in info["message"]
    assert "/plugins@workspace-peek show" in info["message"]
    assert "普通中文示例：" in info["message"] and "必填设置：无" in info["message"]
    assert "page_bytes" in info["message"] and "先执行 /plugins enable workspace-peek" in info["message"]
    text = "abcdefghijklmnop"
    (tmp_path / "input.txt").write_text(text)
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"page_bytes": 0}))
    invalid = service.command(f'/plugins configure workspace-peek --file "{settings}"',
                              revision=service.catalog().revision, request_id="bad-settings")
    assert invalid["state"] in {"failed", "rejected"}, invalid
    assert service.installations.snapshot()[0].settings_json is None
    settings.write_text(json.dumps({"page_bytes": 4}))
    manage(service, f'/plugins configure workspace-peek --file "{settings}"', "configure")
    enabled = manage(service, "/plugins enable workspace-peek", "enable")
    assert enabled["details"]["candidate_cleanup"]["confirmed"]
    assert "workspace-peek 0.1.0（启用）" in enabled["message"]
    assert "普通中文示例：" in enabled["message"] and "/plugins remove workspace-peek" in enabled["message"]
    assert enabled["message"].endswith("查询：/plugins status enable")
    explicit = manage(service, "/plugins@workspace-peek show input.txt", "explicit")
    assert json.loads(json.loads(explicit["output"])["result"])["text"] == text[:4]
    assert explicit["connection_cleanup"]["confirmed"]
    registry = plugin_registry(service)
    name = plugin_tool_name("workspace-peek", "show")
    try:
        registry.prepare_for_run()
        call = invoke_registered_tool(service, registry, name, {"path": "input.txt", "bytes": 16})
        assert call["state"] == "succeeded", call
        assert json.loads(json.loads(call["stored_output"])["result"])["text"] == text
        old = registry.runtime_snapshot(run_id="original")
        activation_id = service.installations.snapshot()[0].activation_id
        disabled = manage(service, "/plugins disable workspace-peek", "disable")
        assert disabled["details"]["released"]
        rejected = invoke_registered_tool(service, registry, name, {"path": "input.txt"}, request_id="old", snapshot=old)
        assert rejected["state"] == "failed", rejected
        registry.prepare_for_run()
        assert name not in registry.tools and "read_file" in registry.tools
        reenabled = manage(service, "/plugins enable workspace-peek", "reenable")
        assert "普通中文示例：" in reenabled["message"]
        assert service.installations.snapshot()[0].activation_id != activation_id
        old_enable = service.command("/plugins status enable", revision="", request_id="old-enable-query")
        assert old_enable["state"] == "succeeded" and "普通中文示例：" not in old_enable["message"]
        again = manage(service, "/plugins@workspace-peek show input.txt --bytes 16", "again")
        assert json.loads(json.loads(again["output"])["result"])["text"] == text
        removed = manage(service, "/plugins remove workspace-peek", "remove")
        assert removed["details"]["removed"] and not service.installations.snapshot()
        registry.prepare_for_run()
        assert name not in registry.tools and "read_file" in registry.tools
        assert (tmp_path / "input.txt").read_text() == text
        core = execute_registry_test_call(registry, "read_file", {"path": "input.txt"}, call_id="core-after-remove")
        assert core.ok and text in core.output, core
    finally:
        registry.close_mcp_clients()
