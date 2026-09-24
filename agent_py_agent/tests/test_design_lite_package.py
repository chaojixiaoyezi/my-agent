"""design-lite 真实标准包、随包 Skill、独立 Python 与原 MCP 客户端的组件验收；不代替 TUI、审批或模型验收。"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from zipfile import ZipFile

import pytest

from agent_py_agent.agent.capability.skills import parse_skill_file
from agent_py_agent.agent.path_access_policy import PathAccessPolicy
from agent_py_agent.agent.plugin_manifest import PLUGIN_PACKAGE_SCHEMA_V3
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

SKILL_PATH = "design_lite/skills/design-card/SKILL.md"


# LLM: SDK/插件均从实际源码经标准后端构建；临时环境只安装包内入口 wheel，不注入源目录。
# 函数用途: 为组件测试安装真正的 design-lite 发行包，返回解释器、manifest、包描述和入口 wheel 文件名列表。
@pytest.fixture(scope="module")
def installed_design(request, tmp_path_factory):
    python, sdk = request.getfixturevalue("installed_sdk")
    root = tmp_path_factory.mktemp("design-package")
    bundle = build_plugin_package(ROOT / "plugins/design-lite", "design_lite/declaration.json",
                                  (sdk,), root / "design.zip")
    package = inspect_plugin_package(bundle.read_bytes())
    with ZipFile(bundle) as archive:
        raw = json.loads(archive.read("plugin.json"))
        name = package.manifest.entry_wheel
        wheel = root / name.rsplit("/", 1)[1]
        wheel.write_bytes(archive.read(name))
    with ZipFile(wheel) as archive:
        wheel_names = archive.namelist()
        (root / "SKILL.md").write_bytes(archive.read(SKILL_PATH))
    result = subprocess.run([sys.executable, "-m", "pip", "--isolated", "--python", str(python),
        "install", "--no-index", "--no-deps", "--no-cache-dir", str(wheel)],
        capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    return python, package.manifest, raw, wheel_names, root / "SKILL.md"


@pytest.fixture
def workspace(tmp_path):
    path = tmp_path / "ws"
    path.mkdir()
    return path


@pytest.fixture
def design(installed_design):
    client = MCPStdioClient(_config("", name="design-lite", command=str(installed_design[0]),
                                   args=["-I", "-m", "design_lite"]))
    client.start()
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


def create(client, workspace, output="d.html", template="card", **extra):
    return invoke(client, workspace, "create", template=template, output=output, **extra)


def test_package_is_v3_and_ships_skill(installed_design):
    _, manifest, raw, wheel_names, skill = installed_design
    assert raw["schema_version"] == PLUGIN_PACKAGE_SCHEMA_V3 and raw["skills"] == ["design-card"]
    assert manifest.skills == ("design-card",)
    assert SKILL_PATH in wheel_names
    card = parse_skill_file(skill, source="plugin:design-lite", require_frontmatter=True)
    assert card.name == "design-card" and card.description


def test_tools_and_extensions_match_manifest(design, installed_design):
    manifest = installed_design[1]
    assert {tool.name: tool.input_schema for tool in design.list_tools()} == {
        tool.name: tool.input_schema for tool in manifest.tools}
    assert {tool.name: tool.requested_effect for tool in manifest.tools} == {"create": "mutating", "edit": "mutating"}
    assert design.server_info == {"name": "design-lite", "version": manifest.version}


@pytest.mark.parametrize("template", ["card", "poster", "landing"])
def test_create_each_template_is_self_contained_and_escaped(design, workspace, template):
    title, subtitle = '<script>alert("x")</script> & 新品', "副标题 <b>加粗</b> 'q'"
    error, result = create(design, workspace, output=f"out/{template}.html", template=template,
                           title=title, subtitle=subtitle, color="#AABBCC")
    assert not error, result
    target = workspace / "out" / f"{template}.html"
    text = target.read_text(encoding="utf-8")
    assert target.stat().st_mode & 0o777 == 0o644
    assert result["path"] == f"out/{template}.html" and result["overwritten"] is False
    assert result["fields"] == {"title": title, "subtitle": subtitle, "color": "#aabbcc"}
    lowered = text.lower()
    assert "<script" not in lowered and "<link" not in lowered and "@import" not in lowered
    assert "url(" not in lowered and "src=" not in lowered and not re.search(r"https?://", lowered)
    assert '<meta name="generator" content="design-lite ' in text
    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt; &amp; 新品" in text
    assert "&lt;b&gt;加粗&lt;/b&gt; &#x27;q&#x27;" in text
    assert ":root{--dl-color:#aabbcc}" in text and "var(--dl-color)" in text
    assert text.count('data-dl-field="title"') == 2 and text.count('data-dl-field="subtitle"') == 1


def test_create_default_color_and_rejections(design, workspace):
    error, result = create(design, workspace, template="poster", title="海报")
    assert not error and result["fields"]["color"] == "#e4572e" and result["fields"]["subtitle"] == ""
    for color in ("red", "#123", "#12345g", "#1234567"):
        error, result = create(design, workspace, output="c.html", title="x", color=color)
        assert error and result["code"] == "INVALID_COLOR", color
    error, result = create(design, workspace, output="t.html", template="slides", title="x")
    assert error and result["code"] == "UNKNOWN_TEMPLATE" and "card" in result["message"]
    error, result = create(design, workspace, output="note.txt", title="x")
    assert error and result["code"] == "INVALID_PATH"
    assert sorted(path.name for path in workspace.iterdir()) == ["d.html"]


def test_existing_output_is_refused_unless_overwrite(design, workspace):
    assert not create(design, workspace, title="第一版")[0]
    first = (workspace / "d.html").read_bytes()
    error, result = create(design, workspace, title="第二版")
    assert error and result["code"] == "OUTPUT_EXISTS"
    assert (workspace / "d.html").read_bytes() == first
    error, result = create(design, workspace, template="landing", title="第二版", overwrite=True)
    assert not error and result["overwritten"] is True
    assert "第二版" in (workspace / "d.html").read_text(encoding="utf-8")


def test_edit_changes_only_requested_fields(design, workspace):
    assert not create(design, workspace, template="landing", title="旧 标题", subtitle="旧副标题")[0]
    target = workspace / "d.html"
    os.chmod(target, 0o600)
    before = target.read_bytes()
    error, result = invoke(design, workspace, "edit", path="d.html", title="新 & <标题>")
    assert not error, result
    assert result["fields"] == [{"field": "title", "old": "旧 标题", "new": "新 & <标题>", "changed": True}]
    after = target.read_bytes()
    assert after == before.replace("旧 标题".encode(), "新 &amp; &lt;标题&gt;".encode())
    assert target.stat().st_mode & 0o777 == 0o600
    error, result = invoke(design, workspace, "edit", path="d.html", color="#ABCDEF", subtitle="")
    assert not error, result
    assert {item["field"]: (item["old"], item["new"]) for item in result["fields"]} == {
        "color": ("#0f766e", "#abcdef"), "subtitle": ("旧副标题", "")}
    assert target.read_bytes() == after.replace(b"--dl-color:#0f766e", b"--dl-color:#abcdef").replace(
        "旧副标题".encode(), b"")


def test_edit_rejects_foreign_or_tampered_files(design, workspace):
    (workspace / "plain.html").write_text("<html><head><title>x</title></head><body></body></html>")
    (workspace / "binary.html").write_bytes(b"\xff\xfe<html>")
    assert not create(design, workspace, output="tampered.html", title="x")[0]
    tampered = workspace / "tampered.html"
    tampered.write_text(tampered.read_text().replace('<h1 data-dl-field="title">', '<h1 class="x" data-dl-field="title">'))
    for name in ("plain.html", "binary.html", "tampered.html"):
        before = (workspace / name).read_bytes()
        error, result = invoke(design, workspace, "edit", path=name, title="y")
        assert error and result["code"] == "NOT_DESIGN_FILE", (name, result)
        assert (workspace / name).read_bytes() == before
    assert not create(design, workspace, output="nosub.html", title="x")[0]
    nosub = workspace / "nosub.html"
    nosub.write_text(re.sub(r'<p data-dl-field="subtitle"></p>\n', "", nosub.read_text()))
    error, result = invoke(design, workspace, "edit", path="nosub.html", subtitle="y")
    assert error and result["code"] == "FIELD_MISSING" and result["fields"] == ["subtitle"]
    error, result = invoke(design, workspace, "edit", path="nosub.html")
    assert error and result["code"] == "NO_FIELDS"
    error, result = invoke(design, workspace, "edit", path="nosub.html", color="blue")
    assert error and result["code"] == "INVALID_COLOR"
    error, result = invoke(design, workspace, "edit", path="none.html", title="y")
    assert error and result["code"] == "FILE_NOT_FOUND"


@pytest.mark.parametrize("kind", ["outside", "traversal", "symlink", "parent_symlink", "write_scope"])
def test_out_of_scope_or_unsafe_paths_are_rejected(design, workspace, tmp_path, kind):
    (workspace / "real").mkdir()
    assert not create(design, workspace, output="real/a.html", title="原标题")[0]
    original = (workspace / "real/a.html").read_bytes()
    outside = tmp_path / "outside.html"
    narrow = None
    if kind == "outside":
        path = str(outside)
    elif kind == "traversal":
        path = "real/../real/a.html"
    elif kind == "symlink":
        (workspace / "link.html").symlink_to(workspace / "real/a.html")
        path = "link.html"
    elif kind == "parent_symlink":
        (workspace / "linkdir").symlink_to(workspace / "real", target_is_directory=True)
        path = "linkdir/a.html"
    else:
        narrow = write_context(workspace, {"allowed_write_roots": [str(workspace / "elsewhere")]})
        path = "real/a.html"
    codes = {"INVALID_PATH", "UNSAFE_PATH", "OUTPUT_EXISTS", "PATH_READ_SCOPE_BLOCKED", "PATH_WRITE_SCOPE_BLOCKED"}
    error, result = invoke(design, workspace, "create", write_ctx=narrow, template="card", output=path,
                           title="x", overwrite=True)
    assert error and result["code"] in codes - {"OUTPUT_EXISTS"}, result
    error, result = invoke(design, workspace, "edit", write_ctx=narrow, path=path, title="x")
    assert error and result["code"] in codes, result
    assert (workspace / "real/a.html").read_bytes() == original and not outside.exists()


def test_missing_write_context_fails_without_writing(design, workspace):
    error, result = invoke(design, workspace, "create", write=False, template="card", output="d.html", title="x")
    assert error and result["code"] == "MISSING_WRITE_CONTEXT" and not (workspace / "d.html").exists()
    assert not create(design, workspace, title="x")[0]
    before = (workspace / "d.html").read_bytes()
    error, result = invoke(design, workspace, "edit", write=False, path="d.html", title="y")
    assert error and result["code"] == "MISSING_WRITE_CONTEXT"
    assert (workspace / "d.html").read_bytes() == before
    error, result = invoke(design, workspace, "create", template="card", output="e.html", title="x", extra=1)
    assert error and result["code"] == "INVALID_ARGUMENTS"
