"""genui-lite 真实标准包、独立 Python 与原 MCP 客户端的组件验收；不代替 TUI、审批或模型验收。"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from zipfile import ZipFile

import pytest

from agent_py_agent.agent.plugin_manifest import PLUGIN_PACKAGE_SCHEMA_V3
from agent_py_agent.agent.plugin_package import inspect_plugin_package
from agent_py_agent.agent.tooling.mcp_client import MCPStdioClient
from agent_py_agent.tests.test_mcp_client import _config
from agent_py_agent.tests.test_plugin_api_build import installed_sdk  # noqa: F401
from agent_py_agent.tests.test_savepoint_lite_package import invoke, write_context
from scripts.build_plugin_api import ROOT
from scripts.build_plugin_package import build_plugin_package

SALES = [{"月份": "1月", "销售额": 12800, "订单数": 64}, {"月份": "2月", "销售额": 25600, "订单数": 71},
         {"月份": "3月", "销售额": 6400.5, "订单数": 40}]


# LLM: SDK/插件均从实际源码经标准后端构建；临时环境只安装包内入口 wheel，不注入源目录。
# 函数用途: 为组件测试安装真正的 genui-lite 发行包，并返回原 manifest 与入口 wheel 文件名列表。
@pytest.fixture(scope="module")
def installed_genui(request, tmp_path_factory):
    python, sdk = request.getfixturevalue("installed_sdk")
    root = tmp_path_factory.mktemp("genui-package")
    bundle = build_plugin_package(ROOT / "plugins/genui-lite", "genui_lite/declaration.json", (sdk,), root / "genui.zip")
    package = inspect_plugin_package(bundle.read_bytes())
    with ZipFile(bundle) as archive:
        raw_manifest = json.loads(archive.read("plugin.json"))
        name = package.manifest.entry_wheel
        wheel = root / name.rsplit("/", 1)[1]
        wheel.write_bytes(archive.read(name))
    with ZipFile(wheel) as archive:
        wheel_names = archive.namelist()
    result = subprocess.run([sys.executable, "-m", "pip", "--isolated", "--python", str(python),
        "install", "--no-index", "--no-deps", "--no-cache-dir", str(wheel)],
        capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    return python, package.manifest, raw_manifest, wheel_names


# LLM: 按宿主相同方式传入设置环境变量；进程由原 MCP 客户端启动与回收。
# 函数用途: 启动一个安装后的 genui-lite MCP 进程。
def start(installed, settings=None):
    env = {} if settings is None else {"MY_AGENT_PLUGIN_SETTINGS": json.dumps(settings)}
    client = MCPStdioClient(_config("", name="genui-lite", command=str(installed[0]),
                                   args=["-I", "-m", "genui_lite"], env=env))
    client.start()
    return client


@pytest.fixture
def genui(installed_genui):
    client = start(installed_genui)
    try:
        yield client
    finally:
        client.stop()


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    return root


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def test_package_is_v3_with_bundled_skill_and_tools_match(genui, installed_genui):
    _, manifest, raw, wheel_names = installed_genui
    assert raw["schema_version"] == PLUGIN_PACKAGE_SCHEMA_V3 and raw["skills"] == ["genui-table"]
    assert manifest.skills == ("genui-table",)
    assert "genui_lite/skills/genui-table/SKILL.md" in wheel_names
    tools = genui.list_tools()
    assert {tool.name: tool.input_schema for tool in tools} == {tool.name: tool.input_schema for tool in manifest.tools}
    assert {tool.name: tool.requested_effect for tool in manifest.tools} == {"table": "read_only", "export": "mutating"}


def test_table_supports_both_formats_and_bar_chart(genui, workspace):
    write_json(workspace / "a.json", SALES)
    write_json(workspace / "b.json", {"columns": ["月份", "销售额", "订单数"],
                                      "rows": [[row["月份"], row["销售额"], row["订单数"]] for row in SALES]})
    error, first = invoke(genui, workspace, "table", write=False, path="a.json", chart="销售额")
    assert not error, first
    error, second = invoke(genui, workspace, "table", write=False, path="b.json", chart="销售额")
    assert not error and first["markdown"] == second["markdown"] and first["chart"] == second["chart"]
    assert first["markdown"].splitlines()[0] == "| 月份 | 销售额 | 订单数 |"
    assert "| 3月 | 6400.5 | 40 |" in first["markdown"]
    bars = [line.count("█") for line in first["chart"].splitlines()]
    assert bars == [15, 30, 8]
    assert first["rows"] == 3 and first["columns"] == 3 and not first["truncated"]
    assert first["summary"] == "共 3 行 3 列。"
    error, plain = invoke(genui, workspace, "table", write=False, path="a.json")
    assert not error and "chart" not in plain


def test_table_errors_and_row_truncation(installed_genui, workspace):
    write_json(workspace / "a.json", SALES)
    (workspace / "bad.json").write_text("{not json", encoding="utf-8")
    write_json(workspace / "nested.json", [{"a": {"x": 1}}])
    write_json(workspace / "scalar.json", 3)
    write_json(workspace / "mismatch.json", {"columns": ["a", "b"], "rows": [[1, 2], [3]]})
    write_json(workspace / "keys.json", [{"a": 1}, {"b": 2}])
    client = start(installed_genui, {"max_rows": 2, "max_input_bytes": 4096})
    try:
        for path, chart, code in (("bad.json", None, "INVALID_JSON"), ("nested.json", None, "UNSUPPORTED_FORMAT"),
                                  ("scalar.json", None, "UNSUPPORTED_FORMAT"), ("mismatch.json", None, "COLUMN_MISMATCH"),
                                  ("keys.json", None, "COLUMN_MISMATCH"), ("a.json", "月份", "CHART_COLUMN_NOT_NUMERIC"),
                                  ("a.json", "利润", "CHART_COLUMN_NOT_FOUND"), ("none.json", None, "FILE_NOT_FOUND")):
            arguments = {"path": path} if chart is None else {"path": path, "chart": chart}
            error, result = invoke(client, workspace, "table", write=False, **arguments)
            assert error and result["code"] == code, (path, result)
            assert "Traceback" not in result["message"]
        error, result = invoke(client, workspace, "table", write=False, path="a.json", chart="销售额")
        assert not error and result["truncated"] and result["shown_rows"] == 2 and result["rows"] == 3
        assert "仅显示前 2 行" in result["summary"] and len(result["chart"].splitlines()) == 2
        write_json(workspace / "big.json", [{"a": "x" * 5000}])
        error, result = invoke(client, workspace, "table", write=False, path="big.json")
        assert error and result["code"] == "FILE_TOO_LARGE"
    finally:
        client.stop()


def test_export_writes_standalone_escaped_html(genui, workspace):
    rows = SALES + [{"月份": '<script>alert("x")</script>&', "销售额": 1, "订单数": 1}]
    write_json(workspace / "a.json", rows)
    error, result = invoke(genui, workspace, "export", path="a.json", output="out/报表.html", chart="销售额",
                           title="<b>上半年</b>")
    assert not error, result
    target = workspace / "out/报表.html"
    page = target.read_text(encoding="utf-8")
    assert result == {"output": "out/报表.html", "bytes": len(page.encode()), "rows": 4, "columns": 3,
                      "shown_rows": 4, "truncated": False, "summary": "共 4 行 3 列。"}
    assert "<script" not in page.lower()
    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;&amp;" in page
    assert "&lt;b&gt;上半年&lt;/b&gt;" in page and "<b>" not in page
    assert not re.search(r"https?:|//[a-z]|<link|@import|url\(|\bsrc=|\bhref=", page, re.IGNORECASE)
    assert "<svg" in page and page.count("<rect") == 4 and "<table>" in page
    assert target.stat().st_mode & 0o777 == 0o644


def test_export_existing_output_requires_overwrite(genui, workspace):
    write_json(workspace / "a.json", SALES)
    target = workspace / "a.html"
    target.write_text("keep", encoding="utf-8")
    error, result = invoke(genui, workspace, "export", path="a.json", output="a.html")
    assert error and result["code"] == "OUTPUT_EXISTS" and target.read_text() == "keep"
    error, result = invoke(genui, workspace, "export", path="a.json", output="a.html", overwrite=True)
    assert not error and "<table>" in target.read_text(encoding="utf-8")
    assert target.stat().st_mode & 0o777 == 0o644


@pytest.mark.parametrize("output, code", [("a.txt", "INVALID_OUTPUT"), ("../x.html", "INVALID_OUTPUT"),
                                          ("OUTSIDE", "PATH_WRITE_SCOPE_BLOCKED"), ("link/x.html", "UNSAFE_PATH")])
def test_export_rejects_bad_or_out_of_scope_output(genui, workspace, tmp_path, output, code):
    write_json(workspace / "a.json", SALES)
    (workspace / "real").mkdir()
    (workspace / "link").symlink_to(workspace / "real", target_is_directory=True)
    output =str(tmp_path / "outside.html") if output == "OUTSIDE" else output
    error, result = invoke(genui, workspace, "export", path="a.json", output=output)
    assert error and result["code"] == code, result
    assert not (tmp_path / "outside.html").exists() and list((workspace / "real").iterdir()) == []


def test_export_read_out_of_scope_and_write_scope_narrowed(genui, workspace, tmp_path):
    write_json(tmp_path / "secret.json", SALES)
    error, result = invoke(genui, workspace, "export", path=str(tmp_path / "secret.json"), output="a.html")
    assert error and result["code"] == "PATH_READ_SCOPE_BLOCKED", result
    write_json(workspace / "a.json", SALES)
    narrow = write_context(workspace, {"allowed_write_roots": [str(workspace / "elsewhere")]})
    error, result = invoke(genui, workspace, "export", write_ctx=narrow, path="a.json", output="a.html")
    assert error and result["code"] == "PATH_WRITE_SCOPE_BLOCKED"
    assert not (workspace / "a.html").exists()


def test_export_without_write_context_fails(genui, workspace):
    write_json(workspace / "a.json", SALES)
    error, result = invoke(genui, workspace, "export", write=False, path="a.json", output="a.html")
    assert error and result["code"] == "MISSING_WRITE_CONTEXT"
    assert not (workspace / "a.html").exists()
    result = genui.call_tool("table", {"path": "a.json"})
    assert result["isError"] and json.loads(result["content"])["code"] == "MISSING_CONTEXT"
    error, result = invoke(genui, workspace, "export", path="a.json", output="a.html", overwrite="yes")
    assert error and result["code"] == "INVALID_ARGUMENTS"
