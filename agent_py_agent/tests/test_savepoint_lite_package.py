"""savepoint-lite 真实标准包、独立 Python 与原 MCP 客户端的组件验收；不代替 TUI、审批或模型验收。"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from zipfile import ZipFile

import pytest

from agent_py_agent.agent.path_access_policy import PathAccessPolicy
from agent_py_agent.agent.plugin_package import inspect_plugin_package
from agent_py_agent.agent.tooling.mcp_client import MCPStdioClient
from agent_py_agent.agent.tooling.workspace_write_scope import build_workspace_write_context
from agent_py_agent.agent.workspace_read_context import WORKSPACE_READ_EXTENSION
from agent_py_agent.agent.workspace_write_context import WORKSPACE_WRITE_EXTENSION
from agent_py_agent.tests.test_mcp_client import _config
from agent_py_agent.tests.test_plugin_api_build import installed_sdk  # noqa: F401
from agent_py_agent.tests.test_workspace_read_context import read_context
from scripts.build_plugin_api import ROOT
from scripts.build_plugin_package import build_plugin_package


# LLM: SDK/插件均从实际源码经标准后端构建；临时环境只安装包内入口 wheel，不注入源目录。
# 函数用途: 为组件测试安装真正的 savepoint-lite 发行包，并返回原 manifest。
@pytest.fixture(scope="module")
def installed_savepoint(request, tmp_path_factory):
    python, sdk = request.getfixturevalue("installed_sdk")
    root = tmp_path_factory.mktemp("savepoint-package")
    bundle = build_plugin_package(ROOT / "plugins/savepoint-lite", "savepoint_lite/declaration.json",
                                  (sdk,), root / "savepoint.zip")
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


# LLM: 工作区与插件数据目录是 tmp_path 下互不包含的两个目录，便于断言工作区里没有多出文件。
# 函数用途: 创建测试用工作区和插件数据目录。
@pytest.fixture
def dirs(tmp_path):
    workspace, data = tmp_path / "ws", tmp_path / "data"
    workspace.mkdir()
    data.mkdir()
    return workspace, data


# LLM: 按宿主相同方式传入数据目录和设置环境变量；进程由原 MCP 客户端启动与回收。
# 函数用途: 启动一个安装后的 savepoint-lite MCP 进程。
def start(installed, data, settings=None):
    python, _ = installed
    env = {"MY_AGENT_PLUGIN_DATA_DIR": str(data)}
    if settings is not None:
        env["MY_AGENT_PLUGIN_SETTINGS"] = json.dumps(settings)
    client = MCPStdioClient(_config("", name="savepoint-lite", command=str(python),
                                   args=["-I", "-m", "savepoint_lite"], env=env))
    client.start()
    return client


@pytest.fixture
def savepoint(installed_savepoint, dirs):
    client = start(installed_savepoint, dirs[1])
    try:
        yield client
    finally:
        client.stop()


# LLM: 与宿主 registry 同源的写入上下文构造；boundary 为 None 时写入根只取 cwd。
# 函数用途: 生成测试用写入上下文。
def write_context(cwd, boundary=None):
    policy = PathAccessPolicy.from_values(mode="normal", dangerous_roots=[])
    return build_workspace_write_context(cwd=cwd, write_boundary=boundary, path_policy=policy)


# LLM: 只拼装真实 SDK 协议值；write=False 模拟宿主没有下发写入上下文。
# 函数用途: 发起原 MCP 工具请求并解析插件返回的 JSON 正文。
def invoke(client, workspace, tool, *, write=True, write_ctx=None, **arguments):
    meta = {WORKSPACE_READ_EXTENSION: read_context(workspace).to_payload()}
    if write:
        meta[WORKSPACE_WRITE_EXTENSION] = (write_ctx or write_context(workspace)).to_payload()
    result = client.call_tool(tool, arguments, request_meta=meta)
    return result["isError"], json.loads(result["content"])


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def files_under(root):
    return sorted(str(path.relative_to(root)) for path in root.rglob("*"))


def test_tools_and_extensions_match_manifest(savepoint, installed_savepoint):
    _, manifest = installed_savepoint
    tools = savepoint.list_tools()
    assert {tool.name: tool.input_schema for tool in tools} == {tool.name: tool.input_schema for tool in manifest.tools}
    assert {tool.name: tool.requested_effect for tool in manifest.tools} == {
        "save": "read_only", "list": "read_only", "restore": "mutating"}
    assert savepoint.server_info == {"name": "savepoint-lite", "version": manifest.version}


def test_save_list_restore_round_trip_keeps_workspace_clean(savepoint, dirs):
    workspace, data = dirs
    target = workspace / "笔记 1.md"
    original = "第一版\r\n内容🙂".encode()
    target.write_bytes(original)
    os.chmod(target, 0o640)
    before = files_under(workspace)
    error, saved = invoke(savepoint, workspace, "save", write=False, path="笔记 1.md")
    assert not error, saved
    assert saved["sha256"] == sha(original) and saved["bytes"] == len(original)
    changed = b"second version"
    target.write_bytes(changed)
    error, second = invoke(savepoint, workspace, "save", write=False, path=str(target))
    assert not error and second["id"] != saved["id"]
    error, listed = invoke(savepoint, workspace, "list", write=False, path="笔记 1.md")
    assert not error and listed["current_sha256"] == sha(changed) and listed["damaged"] == 0
    assert [(item["id"], item["current"], item["sha256"]) for item in listed["snapshots"]] == [
        (saved["id"], False, sha(original)[:12]), (second["id"], True, sha(changed)[:12])]
    error, restored = invoke(savepoint, workspace, "restore", path="笔记 1.md", id=saved["id"],
                             expect=sha(changed)[:8].upper())
    assert not error, restored
    assert restored["previous_sha256"] == sha(changed) and target.read_bytes() == original
    assert target.stat().st_mode & 0o777 == 0o640
    assert files_under(workspace) == before
    # 快照只存在于插件数据目录
    assert all(name.startswith("snapshots") for name in files_under(data))
    assert len([name for name in files_under(data) if name.endswith(".bin")]) == 2


def test_expect_mismatch_is_rejected_with_current_hash(savepoint, dirs):
    workspace, _ = dirs
    target = workspace / "a.txt"
    target.write_bytes(b"one")
    _, saved = invoke(savepoint, workspace, "save", write=False, path="a.txt")
    target.write_bytes(b"someone else edited")
    error, result = invoke(savepoint, workspace, "restore", path="a.txt", id=saved["id"], expect=sha(b"one")[:8])
    assert error and result["code"] == "EXPECT_MISMATCH" and result["current_sha256"] == sha(b"someone else edited")
    assert target.read_bytes() == b"someone else edited"
    error, result = invoke(savepoint, workspace, "restore", path="a.txt", id=saved["id"], expect="zzzzzzzz")
    assert error and result["code"] == "INVALID_EXPECT"


def test_write_scope_rejection_leaves_file_untouched(savepoint, dirs):
    workspace, _ = dirs
    (workspace / "locked").mkdir()
    target = workspace / "locked/file.txt"
    target.write_bytes(b"v1")
    _, saved = invoke(savepoint, workspace, "save", write=False, path="locked/file.txt")
    target.write_bytes(b"v2")
    narrow = write_context(workspace, {"allowed_write_roots": [str(workspace / "elsewhere")]})
    error, result = invoke(savepoint, workspace, "restore", write_ctx=narrow, path="locked/file.txt",
                           id=saved["id"], expect=sha(b"v2")[:8])
    assert error and result["code"] == "PATH_WRITE_SCOPE_BLOCKED", result
    assert target.read_bytes() == b"v2"


@pytest.mark.parametrize("kind", ["symlink", "parent_symlink", "traversal", "outside"])
def test_unsafe_or_out_of_scope_paths_are_rejected(savepoint, dirs, tmp_path, kind):
    workspace, _ = dirs
    (workspace / "real").mkdir()
    (workspace / "real/file.txt").write_text("safe")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")
    if kind == "symlink":
        (workspace / "link.txt").symlink_to(workspace / "real/file.txt")
        path = "link.txt"
    elif kind == "parent_symlink":
        (workspace / "linkdir").symlink_to(workspace / "real", target_is_directory=True)
        path = "linkdir/file.txt"
    elif kind == "traversal":
        path = "real/../real/file.txt"
    else:
        path = str(outside)
    for tool, extra in (("save", {}), ("list", {}), ("restore", {"id": "20260101-000000-abcd", "expect": "0" * 8})):
        error, result = invoke(savepoint, workspace, tool, path=path, **extra)
        assert error and result["code"] in {"UNSAFE_PATH", "INVALID_PATH", "PATH_READ_SCOPE_BLOCKED"}, (tool, result)
    assert (workspace / "real/file.txt").read_text() == "safe" and outside.read_text() == "outside"


def test_symlink_swapped_in_after_save_is_not_followed_on_restore(savepoint, dirs, tmp_path):
    workspace, _ = dirs
    target = workspace / "a.txt"
    target.write_bytes(b"v1")
    _, saved = invoke(savepoint, workspace, "save", write=False, path="a.txt")
    victim = workspace / "victim.txt"
    victim.write_bytes(b"victim")
    target.unlink()
    target.symlink_to(victim)
    error, result = invoke(savepoint, workspace, "restore", path="a.txt", id=saved["id"], expect=sha(b"victim")[:8])
    assert error and result["code"] == "UNSAFE_PATH"
    assert victim.read_bytes() == b"victim" and target.is_symlink()


def test_restore_without_write_context_fails(savepoint, dirs):
    workspace, _ = dirs
    target = workspace / "a.txt"
    target.write_bytes(b"v1")
    _, saved = invoke(savepoint, workspace, "save", write=False, path="a.txt")
    target.write_bytes(b"v2")
    error, result = invoke(savepoint, workspace, "restore", write=False, path="a.txt", id=saved["id"],
                           expect=sha(b"v2")[:8])
    assert error and result["code"] == "MISSING_WRITE_CONTEXT"
    assert target.read_bytes() == b"v2"
    result = savepoint.call_tool("list", {"path": "a.txt"})
    assert result["isError"] and json.loads(result["content"])["code"] == "MISSING_CONTEXT"


def test_missing_file_unknown_snapshot_and_bad_arguments(savepoint, dirs):
    workspace, _ = dirs
    error, result = invoke(savepoint, workspace, "save", write=False, path="none.txt")
    assert error and result["code"] == "FILE_NOT_FOUND"
    error, result = invoke(savepoint, workspace, "list", write=False, path="none.txt")
    assert not error and result["snapshots"] == [] and result["current_sha256"] is None
    (workspace / "a.txt").write_bytes(b"v1")
    for snapshot_id in ("20260101-000000-abcd", "../../x"):
        error, result = invoke(savepoint, workspace, "restore", path="a.txt", id=snapshot_id, expect=sha(b"v1")[:8])
        assert error and result["code"] == "SNAPSHOT_NOT_FOUND"
    error, result = invoke(savepoint, workspace, "save", write=False, path="a.txt", extra="x")
    assert error and result["code"] == "INVALID_ARGUMENTS"


def test_size_and_count_limits_from_settings(installed_savepoint, dirs):
    workspace, data = dirs
    client = start(installed_savepoint, data, {"max_file_bytes": 4, "max_snapshots_per_file": 2})
    try:
        (workspace / "big.txt").write_bytes(b"12345")
        error, result = invoke(client, workspace, "save", write=False, path="big.txt")
        assert error and result["code"] == "FILE_TOO_LARGE"
        (workspace / "a.txt").write_bytes(b"1234")
        for _ in range(2):
            error, result = invoke(client, workspace, "save", write=False, path="a.txt")
            assert not error, result
        error, result = invoke(client, workspace, "save", write=False, path="a.txt")
        assert error and result["code"] == "SNAPSHOT_LIMIT"
        error, listed = invoke(client, workspace, "list", write=False, path="a.txt")
        assert len(listed["snapshots"]) == 2
    finally:
        client.stop()
    assert files_under(workspace) == ["a.txt", "big.txt"]


def test_corrupt_snapshot_is_not_restored(savepoint, dirs):
    workspace, data = dirs
    target = workspace / "a.txt"
    target.write_bytes(b"v1")
    _, saved = invoke(savepoint, workspace, "save", write=False, path="a.txt")
    content, = data.rglob(saved["id"] + ".bin")
    content.write_bytes(b"tampered")
    error, result = invoke(savepoint, workspace, "restore", path="a.txt", id=saved["id"], expect=sha(b"v1")[:8])
    assert error and result["code"] == "SNAPSHOT_CORRUPT" and target.read_bytes() == b"v1"


def test_missing_data_dir_and_invalid_settings(installed_savepoint, dirs):
    python, _ = installed_savepoint
    workspace, _ = dirs
    (workspace / "a.txt").write_bytes(b"v1")
    client = MCPStdioClient(_config("", command=str(python), args=["-I", "-m", "savepoint_lite"]))
    try:
        client.start()
        error, result = invoke(client, workspace, "save", write=False, path="a.txt")
        assert error and result["code"] == "MISSING_DATA_DIR"
    finally:
        client.stop()
    environment = dict(os.environ, MY_AGENT_PLUGIN_SETTINGS=json.dumps({"max_file_bytes": "private-invalid"}))
    result = subprocess.run([str(python), "-I", "-m", "savepoint_lite"], env=environment,
                            input="", capture_output=True, text=True, timeout=15)
    assert result.returncode == 2 and "插件设置无效" in result.stderr and "private-invalid" not in result.stderr
