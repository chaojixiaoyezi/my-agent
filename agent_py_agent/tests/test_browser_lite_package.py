"""browser-lite 真实标准包、独立 Python、原 MCP 客户端与真实浏览器的组件验收；不代替 TUI、审批或模型验收。"""

from __future__ import annotations

import json
import os
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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

# 纯标准库模块（帧编解码、浏览器探测）直接从插件源码导入做单测；它们不依赖 SDK 或宿主
sys.path.insert(0, str(ROOT / "plugins/browser-lite/src"))
from browser_lite.launcher import browser_candidates  # noqa: E402
from browser_lite.websocket import (  # noqa: E402
    OP_BINARY,
    OP_CLOSE,
    OP_CONTINUATION,
    OP_PING,
    OP_PONG,
    OP_TEXT,
    MessageAssembler,
    WebSocketClient,
    WebSocketError,
    apply_mask,
    encode_frame,
    parse_frame,
)

HAS_BROWSER = any(path.is_file() and os.access(path, os.X_OK) for path in browser_candidates())
needs_browser = pytest.mark.skipif(not HAS_BROWSER, reason="本机没有 Chrome/Chromium")

FORM_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>测试表单</title></head>
<body>
<h1>报名</h1>
<form id="form">
  <label>姓名 <input id="name" name="name" type="text"></label>
  <label>选项 <select id="choice" name="choice">
    <option value="">请选择</option><option value="A">甲</option><option value="B">乙</option>
  </select></label>
  <button id="submit" type="submit">提交</button>
</form>
<p id="result"></p>
<a id="away" href="https://example.com/">外部链接</a>
<script>
document.getElementById("form").addEventListener("submit", (event) => {
  event.preventDefault();
  const name = document.getElementById("name").value;
  const choice = document.getElementById("choice").value;
  document.getElementById("result").textContent = "已提交：" + name + "/" + choice;
});
</script>
</body></html>
"""


# ---------- WebSocket 帧编解码纯单测 ----------

# LLM: 测试 helper：按服务端方向生成不带掩码的帧，用于喂给客户端解析。
# 函数用途: 构造服务端发出的原始 WebSocket 帧。
def server_frame(opcode: int, payload: bytes, *, fin: bool = True) -> bytes:
    size = len(payload)
    head = bytes([(0x80 if fin else 0) | opcode])
    if size < 126:
        head += bytes([size])
    elif size < 65536:
        head += bytes([126]) + struct.pack("!H", size)
    else:
        head += bytes([127]) + struct.pack("!Q", size)
    return head + payload


@pytest.mark.parametrize("size,marker", [(0, 0), (125, 125), (126, 126), (65535, 126), (65536, 127), (70000, 127)])
def test_client_frames_are_masked_with_length_branches(size, marker):
    payload = bytes(range(256)) * (size // 256) + bytes(range(size % 256))
    key = b"\x01\x02\x03\x04"
    frame = encode_frame(OP_TEXT, payload, mask_key=key)
    assert frame[0] == 0x80 | OP_TEXT and frame[1] & 0x80 and frame[1] & 0x7F == marker
    header = 2 + {125: 0, 126: 2, 127: 8}.get(marker, 0)
    assert frame[header:header + 4] == key
    assert frame[header + 4:] == apply_mask(payload, key) and (size == 0 or frame[header + 4:] != payload)
    fin, opcode, decoded, used = parse_frame(frame, expect_masked=True)
    assert (fin, opcode, decoded, used) == (True, OP_TEXT, payload, len(frame))
    # 任何截断前缀都视为数据不足，而不是错误
    for cut in (1, 2, header, header + 3, len(frame) - 1):
        if 0 < cut < len(frame):
            assert parse_frame(frame[:cut], expect_masked=True) is None
    assert apply_mask(apply_mask(payload, key), key) == payload


def test_server_frames_parse_and_protocol_violations_are_rejected():
    frame = server_frame(OP_TEXT, "你好".encode()) + b"tail"
    assert parse_frame(frame) == (True, OP_TEXT, "你好".encode(), len(frame) - 4)
    with pytest.raises(WebSocketError):
        parse_frame(encode_frame(OP_TEXT, b"x"))  # 服务端帧不能带掩码
    with pytest.raises(WebSocketError):
        parse_frame(bytes([0x80 | 0x40 | OP_TEXT, 0]))  # 保留位
    with pytest.raises(WebSocketError):
        parse_frame(bytes([0x83, 0]))  # 未知操作码
    with pytest.raises(WebSocketError):
        parse_frame(server_frame(OP_PING, b"x", fin=False))  # 控制帧分片
    with pytest.raises(WebSocketError):
        parse_frame(server_frame(OP_PING, b"x" * 126))  # 控制帧过长
    with pytest.raises(WebSocketError):
        parse_frame(server_frame(OP_TEXT, b"x" * 200), limit=100)  # 单帧超限
    with pytest.raises(ValueError):
        encode_frame(OP_TEXT, b"x", mask_key=b"123")


def test_fragments_are_reassembled_and_bad_sequences_rejected():
    assembler = MessageAssembler(limit=10)
    assert assembler.feed(False, OP_TEXT, "你".encode()[:2]) is None
    assert assembler.feed(True, OP_PING, b"p") == ("ping", b"p")  # 控制帧可插在分片之间
    assert assembler.feed(True, OP_PONG, b"") is None
    assert assembler.feed(True, OP_CONTINUATION, "你".encode()[2:] + b"!") == ("message", "你!")
    assert assembler.feed(True, OP_CLOSE, b"\x03\xe8") == ("close", b"\x03\xe8")
    for frames in ([(True, OP_CONTINUATION, b"x")], [(False, OP_TEXT, b"a"), (True, OP_TEXT, b"b")],
                   [(True, OP_BINARY, b"x")], [(False, OP_TEXT, b"123456"), (True, OP_CONTINUATION, b"78901")],
                   [(True, OP_TEXT, b"\xff")]):
        fresh = MessageAssembler(limit=10)
        with pytest.raises(WebSocketError):
            for frame in frames:
                fresh.feed(*frame)


def test_client_answers_ping_reassembles_split_reads_and_handles_close():
    local, remote = socket.socketpair()
    client = WebSocketClient(local, leftover=server_frame(OP_TEXT, b"first"))
    try:
        assert client.recv_text(1) == "first"
        stream = (server_frame(OP_PING, b"hb") + server_frame(OP_TEXT, b"par", fin=False)
                  + server_frame(OP_CONTINUATION, b"t" * 200))
        # 分多次写入，客户端必须跨 recv 拼帧
        for index in range(0, len(stream), 7):
            remote.sendall(stream[index:index + 7])
        assert client.recv_text(2) == "par" + "t" * 200
        pong = remote.recv(64)
        assert parse_frame(pong, expect_masked=True)[1:3] == (OP_PONG, b"hb")
        with pytest.raises(TimeoutError):
            client.recv_text(0.1)
        client.send_text("往返")
        assert parse_frame(remote.recv(64), expect_masked=True)[2] == "往返".encode()
        remote.sendall(server_frame(OP_CLOSE, b"\x03\xe8"))
        with pytest.raises(WebSocketError):
            client.recv_text(1)
        assert client.closed
    finally:
        client.close()
        remote.close()


# ---------- 真实包与真实浏览器 ----------

# LLM: SDK/插件均从实际源码经标准后端构建；临时环境只安装包内入口 wheel，不注入源目录。
# 函数用途: 为组件测试安装真正的 browser-lite 发行包，并返回原 manifest。
@pytest.fixture(scope="module")
def installed_browser(request, tmp_path_factory):
    python, sdk = request.getfixturevalue("installed_sdk")
    root = tmp_path_factory.mktemp("browser-package")
    bundle = build_plugin_package(ROOT / "plugins/browser-lite", "browser_lite/declaration.json",
                                  (sdk,), root / "browser-lite.zip")
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
def dirs(tmp_path):
    workspace, data = tmp_path / "ws", tmp_path / "data"
    workspace.mkdir()
    data.mkdir()
    (workspace / "form.html").write_text(FORM_HTML, encoding="utf-8")
    return workspace, data


# LLM: 按宿主相同方式传入数据目录和设置环境变量；进程由原 MCP 客户端启动与回收。
# 函数用途: 启动一个安装后的 browser-lite MCP 进程。
def start(installed, data, settings=None):
    python, _ = installed
    env = {"MY_AGENT_PLUGIN_DATA_DIR": str(data)}
    if settings is not None:
        env["MY_AGENT_PLUGIN_SETTINGS"] = json.dumps(settings)
    client = MCPStdioClient(_config("", name="browser-lite", command=str(python),
                                   args=["-I", "-m", "browser_lite"], env=env, timeout=60.0))
    client.start()
    return client


@pytest.fixture
def browser(installed_browser, dirs):
    client = start(installed_browser, dirs[1])
    try:
        yield client
    finally:
        client.stop()


# LLM: 只拼装真实 SDK 协议值；context=False 模拟宿主没有下发读取上下文。
# 函数用途: 发起原 MCP 工具请求并解析插件返回的 JSON 正文。
def invoke(client, workspace, tool, *, context=True, **arguments):
    meta = {WORKSPACE_READ_EXTENSION: read_context(workspace).to_payload()} if context else None
    result = client.call_tool(tool, arguments, request_meta=meta)
    return result["isError"], json.loads(result["content"])


# LLM: pid 被回收后 os.kill(pid, 0) 抛 ProcessLookupError；给回收留有限等待。
# 函数用途: 判断进程是否已经不存在。
def gone(pid, wait=10.0):
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.1)
    return False


def test_tools_match_manifest(browser, installed_browser):
    _, manifest = installed_browser
    tools = browser.list_tools()
    assert {tool.name: tool.input_schema for tool in tools} == {tool.name: tool.input_schema for tool in manifest.tools}
    assert {tool.name: tool.requested_effect for tool in manifest.tools} == {
        "open": "mutating", "read": "read_only", "click": "mutating", "fill": "mutating", "close": "mutating"}
    assert browser.server_info == {"name": "browser-lite", "version": manifest.version}
    declared = {tool.name: tool for tool in manifest.tools}
    assert manifest.observes and manifest.to_payload()["schema_version"] == "plugin_package.v5"
    assert (declared["read"].observation.target_kind, declared["read"].observation.max_candidates) == ("page", 50)
    for name in ("click", "fill"):
        assert (declared[name].observation_ref.target_kind, declared[name].observation_ref.param) == ("page", "candidate_id")
        assert "selector" not in declared[name].input_schema["required"], "填候选 ID 时可以不给 selector"


# LLM: 与 invoke 同源，但返回完整 MCP 结果（含 structuredContent）并可附宿主观察 _meta；只拼装真实协议值。
# 函数用途: 发起一次带可选观察上下文的工具调用，返回原始结果。
def invoke_full(client, workspace, tool, *, observation=None, **arguments):
    meta = {WORKSPACE_READ_EXTENSION: read_context(workspace).to_payload()}
    if observation is not None:
        meta["my-agent/observation"] = observation
    return client.call_tool(tool, arguments, request_meta=meta)


# 函数用途: 宿主复核通过后会附给插件的候选上下文形状。
def _observation_meta(key: str, generation: str) -> dict:
    return {"version": "1", "observation_id": "obs-" + "0" * 24, "key": key, "target": {"ref": "page", "generation": generation}}


@needs_browser
def test_candidates_resolve_by_key_and_generation_and_go_stale_after_navigation(browser, dirs):
    workspace, _ = dirs
    assert not invoke(browser, workspace, "open", url="form.html")[0]
    listed = invoke_full(browser, workspace, "read")
    assert not listed["isError"] and "my_agent_observation" not in listed["content"], "观察只进 structuredContent，正文不重复"
    observation = listed["structuredContent"]["my_agent_observation"]
    assert observation["schema"] == "plugin_observation.v1" and observation["target"] == {"ref": "page", "generation": "1"}
    candidates = observation["candidates"]
    assert [c["role"] for c in candidates] == ["textbox", "select", "button"]
    assert [c["actions"] for c in candidates] == [["fill", "click"], ["fill", "click"], ["click"]]
    assert candidates[2]["label"] == "提交" and all(c["key"].split(".")[1] == str(i) for i, c in enumerate(candidates))
    name_key, submit_key = candidates[0]["key"], candidates[2]["key"]

    filled = invoke_full(browser, workspace, "fill", candidate_id="cand-0123456789abcdef", value="张三",
                         observation=_observation_meta(name_key, "1"))
    assert not filled["isError"] and json.loads(filled["content"]) == {"filled": "input", "value": "张三"}, "按候选填写，不需要 selector"
    clicked = invoke_full(browser, workspace, "click", candidate_id="cand-0123456789abcdef", observation=_observation_meta(submit_key, "1"))
    assert not clicked["isError"] and json.loads(clicked["content"])["clicked"] == "button"
    assert invoke(browser, workspace, "read", selector="#result")[1]["items"][0]["text"] == "已提交：张三/"

    missing = invoke_full(browser, workspace, "click", candidate_id="cand-0123456789abcdef", observation=_observation_meta("deadbeef.0", "1"))
    assert missing["isError"] and missing["structuredContent"] == {"my_agent_observation_error": {"code": "not_found"}}
    assert json.loads(missing["content"])["code"] == "OBSERVATION_NOT_FOUND"
    no_meta = invoke_full(browser, workspace, "click", candidate_id="cand-0123456789abcdef")
    assert no_meta["isError"] and json.loads(no_meta["content"])["code"] == "MISSING_CONTEXT"
    neither = invoke_full(browser, workspace, "click")
    assert neither["isError"] and json.loads(neither["content"])["code"] == "INVALID_ARGUMENTS"

    assert not invoke(browser, workspace, "open", url="form.html")[0], "重新打开页面换代次"
    stale = invoke_full(browser, workspace, "click", candidate_id="cand-0123456789abcdef", observation=_observation_meta(submit_key, "1"))
    assert stale["isError"] and stale["structuredContent"] == {"my_agent_observation_error": {"code": "stale"}}
    fresh = invoke_full(browser, workspace, "read")["structuredContent"]["my_agent_observation"]
    assert fresh["target"]["generation"] == "2" and fresh["candidates"][2]["key"] == submit_key, "同一选择器在新代次里键形状不变、代次不同"


@needs_browser
def test_open_read_fill_click_read_then_close(browser, dirs):
    workspace, data = dirs
    error, opened = invoke(browser, workspace, "open", url="form.html")
    assert not error, opened
    assert opened["title"] == "测试表单" and opened["url"].startswith("file://") and "报名" in opened["text"]
    pid = opened["browser_pid"]
    assert (data / "profile").is_dir() and not gone(pid, wait=0)
    error, listed = invoke(browser, workspace, "read")
    assert not error, listed
    assert [(item["tag"], item["id"]) for item in listed["items"]] == [
        ("input", "name"), ("select", "choice"), ("button", "submit")]
    select = listed["items"][1]
    assert [option["value"] for option in select["options"]] == ["", "A", "B"] and select["visible"]
    error, filled = invoke(browser, workspace, "fill", selector="#name", value="张三")
    assert not error and filled == {"filled": "input", "value": "张三"}
    error, filled = invoke(browser, workspace, "fill", selector="#choice", value="乙")
    assert not error and filled["value"] == "B"
    error, clicked = invoke(browser, workspace, "click", selector="#submit")
    assert not error, clicked
    assert clicked["navigated"] is False and clicked["title"] == "测试表单"
    error, result = invoke(browser, workspace, "read", selector="#result")
    assert not error and result["items"][0]["text"] == "已提交：张三/B"
    # 同一插件进程复用同一个浏览器
    error, again = invoke(browser, workspace, "open", url=(workspace / "form.html").as_uri())
    assert not error and again["browser_pid"] == pid
    error, closed = invoke(browser, workspace, "close")
    assert not error and closed["closed"] is True
    assert gone(pid) and (data / "profile").is_dir() and not any((data / "profile").iterdir())
    error, result = invoke(browser, workspace, "read")
    assert error and result["code"] == "NO_PAGE"


@needs_browser
def test_selector_and_fill_errors(browser, dirs):
    workspace, _ = dirs
    error, result = invoke(browser, workspace, "read")
    assert error and result["code"] == "NO_PAGE"
    assert not invoke(browser, workspace, "open", url="form.html")[0]
    cases = [("click", {"selector": "#nope"}, "SELECTOR_NOT_FOUND"), ("click", {"selector": "label"}, "SELECTOR_AMBIGUOUS"),
             ("read", {"selector": "[["}, "INVALID_SELECTOR"), ("fill", {"selector": "#submit", "value": "x"}, "NOT_FILLABLE"),
             ("fill", {"selector": "#choice", "value": "丙"}, "OPTION_NOT_FOUND"), ("fill", {"selector": "input,select", "value": "x"},
             "SELECTOR_AMBIGUOUS")]
    for tool, arguments, code in cases:
        error, result = invoke(browser, workspace, tool, **arguments)
        assert error and result["code"] == code, (tool, result)
    error, result = invoke(browser, workspace, "open", url="form.html", extra="x")
    assert error and result["code"] == "INVALID_ARGUMENTS"
    error, result = invoke(browser, workspace, "read", context=False)
    assert error and result["code"] == "MISSING_CONTEXT"


# LLM: 测试 helper：本机 HTTP 服务，/ok 返回页面，/jump 302 到不允许的外部地址。
# 类用途: 模拟本机被测站点的重定向。
class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/jump":
            self.send_response(302)
            self.send_header("Location", "https://example.com/")
            self.end_headers()
            return
        body = "<title>本机页</title><p>ok</p>".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def local_site():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@needs_browser
def test_disallowed_addresses_and_redirects_are_stopped(browser, dirs, tmp_path, local_site):
    workspace, _ = dirs
    outside = tmp_path / "outside.html"
    outside.write_text("<title>外部</title>", encoding="utf-8")
    for url in ("https://example.com", outside.as_uri(), "../outside.html", "ftp://127.0.0.1/", "javascript:alert(1)"):
        error, result = invoke(browser, workspace, "open", url=url)
        assert error and result["code"] == "URL_NOT_ALLOWED", (url, result)
    error, result = invoke(browser, workspace, "open", url=local_site + "/ok")
    assert not error and result["title"] == "本机页"
    error, result = invoke(browser, workspace, "open", url=local_site + "/jump")
    assert error and result["code"] == "URL_NOT_ALLOWED" and result["url"].startswith("https://example.com"), result
    # 点击页面里的外链：导航被拦截，报错并停止
    assert not invoke(browser, workspace, "open", url="form.html")[0]
    error, result = invoke(browser, workspace, "click", selector="#away")
    assert error and result["code"] == "URL_NOT_ALLOWED", result
    assert "allowed_hosts" in result["message"] and result["url"] == "https://example.com/"
    error, result = invoke(browser, workspace, "read", selector="h1")
    assert not error and result["url"] == "about:blank" and result["count"] == 0, result
    # 页面内的子资源与脚本请求同样受限：外部 fetch、工作区外 file:// 图片被拦截，工作区内资源放行
    (workspace / "ok.txt").write_text("ok", encoding="utf-8")
    (workspace / "sub.html").write_text(
        "<title>子资源</title><script>"
        "const probe = (url) => fetch(url, {mode: 'no-cors'}).then(() => 'ok', () => 'blocked');"
        f"Promise.all([probe('https://example.com/x'), probe('{local_site}/ok')]).then("
        "(r) => { document.title = r.join(','); });</script>", encoding="utf-8")
    assert not invoke(browser, workspace, "open", url="sub.html")[0]
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        error, result = invoke(browser, workspace, "read", selector="title")
        if result["title"] != "子资源":
            break
        time.sleep(0.2)
    assert result["title"] == "blocked,ok", result


@needs_browser
def test_allowed_hosts_setting_is_enforced(installed_browser, dirs, local_site):
    workspace, data = dirs
    client = start(installed_browser, data, {"allowed_hosts": ["localhost"]})
    try:
        error, result = invoke(client, workspace, "open", url=local_site + "/ok")
        assert error and result["code"] == "URL_NOT_ALLOWED" and "allowed_hosts" in result["message"]
    finally:
        client.stop()


def test_missing_browser_is_reported_by_every_tool(installed_browser, dirs, tmp_path):
    workspace, data = dirs
    client = start(installed_browser, data, {"chrome_path": str(tmp_path / "no-such-chrome")})
    try:
        for tool, arguments in (("open", {"url": "form.html"}), ("read", {}), ("click", {"selector": "#submit"}),
                                ("fill", {"selector": "#name", "value": "x"}), ("close", {})):
            error, result = invoke(client, workspace, tool, **arguments)
            assert error and result["code"] == "BROWSER_UNAVAILABLE", (tool, result)
            assert result["message"].startswith("浏览器不可用：未找到 Chrome/Chromium")
    finally:
        client.stop()
    assert not (data / "profile").exists()


@needs_browser
def test_host_stop_reaps_browser(installed_browser, dirs):
    workspace, data = dirs
    client = start(installed_browser, data)
    try:
        error, opened = invoke(client, workspace, "open", url="form.html")
        assert not error, opened
    finally:
        client.stop()
    assert gone(opened["browser_pid"])


# LLM: 不经 MCP 客户端直接起插件进程，才能分别验证 stdin EOF 与仅对插件 pid 发 SIGTERM 的退出路径。
# 函数用途: 启动插件、打开测试页并返回进程和浏览器 pid。
def raw_open(installed, workspace, data):
    python, _ = installed
    process = subprocess.Popen([str(python), "-I", "-m", "browser_lite"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, env={"MY_AGENT_PLUGIN_DATA_DIR": str(data), "HOME": os.environ["HOME"],
                                                              "PATH": os.environ.get("PATH", "")},
                               start_new_session=True)
    meta = {WORKSPACE_READ_EXTENSION: read_context(workspace).to_payload()}
    for request in ({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                     "params": {"name": "open", "arguments": {"url": "form.html"}, "_meta": meta}}):
        process.stdin.write((json.dumps(request) + "\n").encode())
        process.stdin.flush()
        response = json.loads(process.stdout.readline())
    body = json.loads(response["result"]["content"][0]["text"])
    assert not response["result"]["isError"], body
    return process, body["browser_pid"]


@needs_browser
@pytest.mark.parametrize("how", ["eof", "sigterm"])
def test_plugin_exit_closes_browser(installed_browser, dirs, how):
    workspace, data = dirs
    process, pid = raw_open(installed_browser, workspace, data)
    try:
        if how == "eof":
            process.stdin.close()
        else:
            os.kill(process.pid, signal.SIGTERM)
        assert process.wait(timeout=20) == 0
        assert gone(pid)
        assert not any((data / "profile").iterdir())
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)


@needs_browser
def test_idle_timeout_closes_browser(installed_browser, dirs):
    workspace, data = dirs
    client = start(installed_browser, data, {"idle_close_seconds": 5})
    try:
        error, opened = invoke(client, workspace, "open", url="form.html")
        assert not error, opened
        assert gone(opened["browser_pid"], wait=15)
        error, result = invoke(client, workspace, "read")
        assert error and result["code"] == "NO_PAGE"
    finally:
        client.stop()


def test_missing_data_dir_and_invalid_settings(installed_browser, dirs):
    python, _ = installed_browser
    workspace, _ = dirs
    if HAS_BROWSER:
        client = MCPStdioClient(_config("", command=str(python), args=["-I", "-m", "browser_lite"]))
        try:
            client.start()
            error, result = invoke(client, workspace, "open", url="form.html")
            assert error and result["code"] == "MISSING_DATA_DIR"
        finally:
            client.stop()
    for bad in ({"allowed_hosts": "localhost"}, {"idle_close_seconds": 0}, {"unknown": 1}):
        environment = dict(os.environ, MY_AGENT_PLUGIN_SETTINGS=json.dumps(bad))
        result = subprocess.run([str(python), "-I", "-m", "browser_lite"], env=environment,
                                input="", capture_output=True, text=True, timeout=15)
        assert result.returncode == 2 and "插件设置无效" in result.stderr
