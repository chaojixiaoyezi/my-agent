"""web-board 真实标准包、独立 Python 与原 MCP 客户端的组件验收；不代替 TUI、审批或模型验收。

插件进程内起只绑 127.0.0.1 的网页服务，本文件用 urllib 真实访问：令牌、目录列表、转义预览、越界/链接拒绝、
只接受 GET/HEAD、status/stop、空闲自停，以及 MCP 客户端回收和 stdin EOF 两种进程退出后端口都被释放。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote, urlsplit
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


# LLM: SDK/插件均从实际源码经标准后端构建；临时环境只安装包内入口 wheel，不注入源目录。
# 函数用途: 为组件测试安装真正的 web-board 发行包，并返回原 manifest。
@pytest.fixture(scope="module")
def installed_board(request, tmp_path_factory):
    python, sdk = request.getfixturevalue("installed_sdk")
    root = tmp_path_factory.mktemp("web-board-package")
    bundle = build_plugin_package(ROOT / "plugins/web-board", "web_board/declaration.json", (sdk,), root / "wb.zip")
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


# 函数用途: 建一个含普通文件、子目录、HTML、图片、指向外部的链接的工作区，外加工作区外的秘密文件。
@pytest.fixture
def workspace(tmp_path):
    ws, outside = tmp_path / "ws", tmp_path / "outside"
    (ws / "site/sub").mkdir(parents=True)
    outside.mkdir()
    (outside / "secret.txt").write_text("TOP-SECRET")
    (ws / "top.txt").write_text("workspace top")
    (ws / "site/note.txt").write_text("hello <script>alert(1)</script> & done")
    (ws / "site/readme.md").write_text("# 标题\n正文")
    (ws / "site/page.html").write_text("<h1>Page</h1><script>alert(2)</script>")
    (ws / "site/pic.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
    (ws / "site/sub/deep.txt").write_text("deep file")
    (ws / "site/escape.txt").symlink_to(outside / "secret.txt")
    (ws / "site/escape_dir").symlink_to(outside)
    (ws / "site/alias").symlink_to(ws / "site/sub")
    return ws


# LLM: 按宿主相同方式传入设置环境变量；进程由原 MCP 客户端启动与回收。
# 函数用途: 启动一个安装后的 web-board MCP 进程。
def start(installed, settings=None):
    python, _ = installed
    # 测试中绝不真的打开浏览器；数据目录放临时位置，完整链接从那里读
    data_dir = tempfile.mkdtemp(prefix="web-board-data-")
    env = {"MY_AGENT_PLUGIN_SETTINGS": json.dumps({"open_browser": False, **(settings or {})}),
           "MY_AGENT_PLUGIN_DATA_DIR": data_dir}
    client = MCPStdioClient(_config("", name="web-board", command=str(python), args=["-I", "-m", "web_board"], env=env))
    client.start()
    return client


# 函数用途: 从 serve 结果指向的私有文件读取带令牌的完整链接，并确认令牌没有出现在工具结果里。
def link(served):
    url = Path(served["link_file"]).read_text(encoding="utf-8").strip()
    assert "token" not in json.dumps(served) and served["browser_opened"] is False
    assert oct(Path(served["link_file"]).stat().st_mode & 0o777) == "0o600"
    return url


# 函数用途: 带逐次读取上下文调用工具并解析 JSON 正文。
def invoke(client, ws, tool, **arguments):
    meta = {WORKSPACE_READ_EXTENSION: read_context(ws).to_payload()}
    result = client.call_tool(tool, arguments, request_meta=meta)
    return result["isError"], json.loads(result["content"])


# 函数用途: 发 HTTP 请求，返回 (状态码, 正文文本, 响应头)；4xx/5xx 也照常返回。
def fetch(url, method="GET", headers=None):
    request = urllib.request.Request(url, method=method, headers=headers or {}, data=b"x" if method == "POST" else None)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read().decode("utf-8", "replace"), response.headers
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace"), exc.headers


# 函数用途: 判断本机端口是否还能连上。
def port_open(port):
    with socket.socket() as sock:
        sock.settimeout(2)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def test_tools_match_manifest_and_effects(installed_board):
    _, manifest = installed_board
    client = start(installed_board)
    try:
        assert {tool.name: tool.input_schema for tool in client.list_tools()} == {
            tool.name: tool.input_schema for tool in manifest.tools}
        assert {tool.name: tool.requested_effect for tool in manifest.tools} == {
            "serve": "mutating", "status": "read_only", "stop": "mutating"}
        result = client.call_tool("serve", {"path": "."})
        assert result["isError"] and json.loads(result["content"])["code"] == "MISSING_CONTEXT"
        assert json.loads(client.call_tool("status", {})["content"])["serving"] is False
    finally:
        client.stop()


def test_serve_browse_and_stop(installed_board, workspace):
    client = start(installed_board, {"max_preview_bytes": 1024, "idle_stop_seconds": 600})
    try:
        error, served = invoke(client, workspace, "serve", path="site")
        assert not error, served
        url, port = link(served), served["port"]
        parts = urlsplit(url)
        assert parts.hostname == "127.0.0.1" and parts.port == port and "token=" in parts.query
        token = parts.query.split("token=", 1)[1]
        base = f"http://127.0.0.1:{port}"

        def page(path, p=None):
            query = f"token={token}" + (f"&p={quote(p, safe='')}" if p is not None else "")
            return fetch(f"{base}{path}?{query}")

        # 令牌：缺失/错误 403，查询参数正确 200 并下发 HttpOnly cookie，cookie 单独也能访问
        assert fetch(f"{base}/")[0] == 403
        assert fetch(f"{base}/?token=wrong")[0] == 403
        assert fetch(f"{base}/view?p=note.txt")[0] == 403
        status, body, headers = page("/")
        assert status == 200 and "web-board" in body and "site" in body
        for name in ("note.txt", "readme.md", "page.html", "pic.png", "sub/", "escape.txt"):
            assert name in body
        assert "top.txt" not in body
        cookie = headers["Set-Cookie"]
        assert "HttpOnly" in cookie and f"web_board_{port}={token}" in cookie
        assert fetch(f"{base}/?p=sub", headers={"Cookie": cookie.split(";", 1)[0]})[0] == 200
        assert "deep.txt" in page("/", "sub")[1]

        # 预览：文本转义、Markdown 以 <pre> 展示、HTML 走 sandbox iframe、图片走 <img> + /raw
        status, body, headers = page("/view", "note.txt")
        assert status == 200 and "hello &lt;script&gt;alert(1)&lt;/script&gt; &amp; done" in body
        assert "<script>" not in body and "default-src 'none'" in headers["Content-Security-Policy"]
        assert "<pre># 标题\n正文</pre>" in page("/view", "readme.md")[1]
        body = page("/view", "page.html")[1]
        assert '<iframe sandbox="" srcdoc="&lt;h1&gt;Page&lt;/h1&gt;&lt;script&gt;' in body and "<script>" not in body
        assert '<img src="/raw?p=pic.png"' in page("/view", "pic.png")[1]
        status, raw, headers = page("/raw", "pic.png")
        assert status == 200 and headers["Content-Type"] == "image/png" and "sandbox" in headers["Content-Security-Policy"]

        # 越界：.. / 绝对路径 / 链接指向外部（文件与目录）一律拒绝，秘密内容不出现
        for p, code in [("../top.txt", 400), ("sub/../../top.txt", 400), (str(workspace / "top.txt"), 400),
                        ("/etc/passwd", 400), ("escape.txt", 403), ("escape_dir/secret.txt", 403), ("alias/deep.txt", 403),
                        ("missing.txt", 404)]:
            for path in ("/view", "/raw"):
                status, body, _ = page(path, p)
                assert status == code and "TOP-SECRET" not in body, (path, p, status)
        assert page("/", "escape_dir")[0] == 403

        # 只接受 GET/HEAD
        assert fetch(f"{base}/?token={token}", method="POST")[0] == 405
        assert fetch(f"{base}/?token={token}", method="PUT")[0] == 405
        status, body, _ = fetch(f"{base}/?token={token}", method="HEAD")
        assert status == 200 and body == ""

        error, status_value = invoke(client, workspace, "status")
        assert not error and status_value["serving"] and status_value["address"] == served["address"]
        assert status_value["root"] == "site" and status_value["requests"] >= 20

        # 再次 serve 先停旧服务
        error, second = invoke(client, workspace, "serve", path=".")
        assert not error and second["replaced_previous"] and second["root"] == "."
        assert not port_open(port) and port_open(second["port"])
        assert "top.txt" in fetch(link(second))[1]

        error, stopped = invoke(client, workspace, "stop")
        assert not error and stopped["serving"] is False and stopped["address"] is None
        assert stopped["stop_reason"] == "stopped" and not port_open(second["port"])
        assert invoke(client, workspace, "stop")[1]["serving"] is False
    finally:
        client.stop()


def test_serve_rejects_bad_roots_and_keeps_old_service(installed_board, workspace, tmp_path):
    client = start(installed_board)
    try:
        _, served = invoke(client, workspace, "serve", path="site")
        for path, code in [("../outside", "INVALID_PATH"), (str(tmp_path / "outside"), "PATH_READ_SCOPE_BLOCKED"),
                           ("site/escape_dir", "PATH_READ_SCOPE_BLOCKED"), ("site/alias", "UNSAFE_PATH"), ("top.txt", "UNSAFE_PATH"), ("nope", "NOT_FOUND")]:
            error, result = invoke(client, workspace, "serve", path=path)
            assert error and result["code"] == code, (path, result)
        assert invoke(client, workspace, "status")[1]["address"] == served["address"] and port_open(served["port"])
        error, result = invoke(client, workspace, "serve", path="site", extra=1)
        assert error and result["code"] == "INVALID_ARGUMENTS"
    finally:
        client.stop()
    assert not port_open(served["port"])


def test_idle_timeout_stops_service(installed_board, workspace):
    client = start(installed_board, {"idle_stop_seconds": 1})
    try:
        _, served = invoke(client, workspace, "serve", path="site")
        assert port_open(served["port"])
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and invoke(client, workspace, "status")[1]["serving"]:
            time.sleep(0.2)
        status_value = invoke(client, workspace, "status")[1]
        assert status_value["serving"] is False and status_value["stop_reason"] == "idle"
        assert not port_open(served["port"])
    finally:
        client.stop()


def test_stdin_eof_ends_process_and_releases_port(installed_board, workspace):
    python, _ = installed_board
    meta = {WORKSPACE_READ_EXTENSION: read_context(workspace).to_payload()}
    requests = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                 "params": {"name": "serve", "arguments": {"path": "site"}, "_meta": meta}}]
    process = subprocess.Popen([str(python), "-I", "-m", "web_board"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               env={k: v for k, v in os.environ.items() if k != "MY_AGENT_PLUGIN_SETTINGS"})
    try:
        for item in requests:
            process.stdin.write(json.dumps(item).encode() + b"\n")
        process.stdin.flush()
        process.stdout.readline()
        served = json.loads(json.loads(process.stdout.readline())["result"]["content"][0]["text"])
        assert port_open(served["port"])
        process.stdin.close()
        assert process.wait(timeout=10) == 0
    finally:
        process.kill()
        process.wait()
    assert not port_open(served["port"])


def test_invalid_settings_fail_startup(installed_board):
    python, _ = installed_board
    for settings in ({"idle_stop_seconds": 0}, {"max_preview_bytes": True}, {"unknown": 1}):
        environment = dict(os.environ, MY_AGENT_PLUGIN_SETTINGS=json.dumps(settings))
        result = subprocess.run([str(python), "-I", "-m", "web_board"], env=environment,
                                input="", capture_output=True, text=True, timeout=15)
        assert result.returncode == 2 and "插件设置无效" in result.stderr, settings
