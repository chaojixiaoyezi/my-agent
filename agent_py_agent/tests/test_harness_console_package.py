"""harness-console 真实标准包、独立 Python 与原 MCP 客户端的组件验收；不代替 TUI、审批或真实 Gateway 验收。

宿主只读 API 用测试内的假服务（本地 http.server，校验 X-Plugin-Host-Token，返回固定投影）经环境变量注入；
插件网页用 urllib 真实访问；桌面窗口用记录 argv 的假"浏览器"脚本经 chrome_path 注入，测试从不打开真实浏览器。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from zipfile import ZipFile

import pytest

from agent_py_agent.agent.plugin_manifest import PLUGIN_PACKAGE_SCHEMA_V4
from agent_py_agent.agent.plugin_package import inspect_plugin_package
from agent_py_agent.agent.tooling.mcp_client import MCPStdioClient
from agent_py_agent.tests.test_mcp_client import _config
from agent_py_agent.tests.test_plugin_api_build import installed_sdk  # noqa: F401
from scripts.build_plugin_api import ROOT
from scripts.build_plugin_package import build_plugin_package

HOST_TOKEN = "fake-host-token-7f3a9c"
THREADS = [{"thread_id": "t1", "title": "重构插件 <b>宿主</b>", "status": "active", "updated_at": time.time() - 30,
            "compact_generation": 2, "secret_extra": "不应透传"},
           {"thread_id": "t2", "title": "写周报", "status": "idle", "updated_at": time.time() - 7200,
            "compact_generation": 0}]
ACTIVITY = {"t1": {"activity": {"phase": "tool_running", "activity": "正在运行 pytest", "started_at": time.time() - 65,
                                "updated_at": time.time(), "active_task_count": 1, "subagent_count": 0, "compact_count": 2},
                   "run_state": {"state": "working", "active_task_count": 1, "subagent_count": 0},
                   "context": {"known": True, "compact_count": 2, "context_window_tokens": 200000,
                               "compact_trigger_tokens": 180000, "current_tokens": 23981, "messages_tokens": 2779,
                               "runtime_guidance_tokens": 0, "tool_schema_tokens": 15316, "estimated": False}},
            "t2": {"activity": {"phase": "waiting_permission", "activity": "等待审批 shell"},
                   "run_state": {"state": "waiting"}, "context": {"known": False}}}


# LLM: 插件从实际源码经标准后端构建（不需要 SDK 依赖）；临时环境只安装包内 wheel，不注入源目录。
# 函数用途: 构建并安装 harness-console，返回解释器和包描述。
@pytest.fixture(scope="module")
def installed_console(request, tmp_path_factory):
    python, _sdk = request.getfixturevalue("installed_sdk")
    root = tmp_path_factory.mktemp("harness-console-package")
    bundle = build_plugin_package(ROOT / "plugins/harness-console", "harness_console/declaration.json", (),
                                  root / "hc.zip")
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


# LLM: 假宿主 API 只模拟公开协议：POST JSON、令牌头校验、revoked 时 403；记录每次请求便于断言缓存与 thread_id。
# 函数用途: 启动一个假宿主只读 API，返回 (地址, 状态字典)；测试结束关闭。
@pytest.fixture
def fake_host():
    state = {"revoked": False, "requests": []}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append(body)
            if state["revoked"] or self.headers.get("X-Plugin-Host-Token") != HOST_TOKEN:
                return self.reply(403, {"ok": False, "error": "宿主 API 令牌无效或插件已停用"})
            result = {"ok": True, "owner_loaded": True, "at": time.time()}
            topics = body["topics"]
            if "threads" in topics:
                result["threads"] = THREADS
            if "activity" in topics:
                result["activity"] = ACTIVITY.get(body["thread_id"], {})
            if "plugins" in topics:
                result["plugins"] = [{"plugin_id": "harness-console", "version": "0.1.0", "enabled": True, "summary": "工作台"},
                                     {"plugin_id": "web-board", "version": "0.1.1", "enabled": False, "summary": "浏览"}]
            if "gateway" in topics:
                result["gateway"] = {"pid": 4242, "port": 8420}
            self.reply(200, result)

        def reply(self, status, payload):
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            return

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/plugin-host/query", state
    httpd.shutdown()
    httpd.server_close()


# LLM: 按宿主相同方式注入设置、数据目录和（可选）宿主 API 环境；进程由原 MCP 客户端启动与回收。
# 函数用途: 启动一个安装后的 harness-console MCP 进程，返回 (客户端, 数据目录)。
def start(installed, settings=None, host_url=None):
    python, _ = installed
    data_dir = tempfile.mkdtemp(prefix="harness-console-data-")
    env = {"MY_AGENT_PLUGIN_SETTINGS": json.dumps({"open_browser": False, **(settings or {})}),
           "MY_AGENT_PLUGIN_DATA_DIR": data_dir}
    if host_url:
        env.update(MY_AGENT_HOST_API_URL=host_url, MY_AGENT_HOST_API_TOKEN=HOST_TOKEN)
    client = MCPStdioClient(_config("", name="harness-console", command=str(python),
                                    args=["-I", "-m", "harness_console"], env=env))
    client.start()
    return client, Path(data_dir)


# 函数用途: 调用无参工具并解析 JSON 正文，同时确认工具结果不含任何令牌。
def invoke(client, tool):
    result = client.call_tool(tool, {})
    assert "token" not in result["content"] and HOST_TOKEN not in result["content"]
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


# 函数用途: 判断进程是否仍存在。
def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


# 函数用途: 从 open 结果读私有链接文件，带令牌访问首页，返回 (基础地址, cookie 头)。
def login(opened):
    assert oct(Path(opened["link_file"]).stat().st_mode & 0o777) == "0o600"
    url = Path(opened["link_file"]).read_text(encoding="utf-8").strip()
    parts = urlsplit(url)
    assert parts.hostname == "127.0.0.1" and parts.port == opened["port"] and "token=" in parts.query
    base = f"http://127.0.0.1:{opened['port']}"
    status, body, headers = fetch(url)
    assert status == 200 and "my-agent 工作台" in body
    return base, headers["Set-Cookie"].split(";", 1)[0], (body, headers)


def test_package_is_v4_with_read_host_api(installed_console):
    _, manifest = installed_console
    assert manifest.to_payload()["schema_version"] == PLUGIN_PACKAGE_SCHEMA_V4
    assert manifest.host_api == ("read",)
    assert {tool.name: tool.requested_effect for tool in manifest.tools} == {
        "open": "mutating", "desktop": "mutating", "stop": "mutating"}


def test_open_state_revoke_and_stop(installed_console, fake_host):
    host_url, host = fake_host
    client, _ = start(installed_console, host_url=host_url)
    try:
        assert {tool.name for tool in client.list_tools()} == {"open", "desktop", "stop"}
        error, opened = invoke(client, "open")
        assert not error and opened["serving"] and opened["browser_opened"] is False and opened["host_api"] == "ok"
        assert opened["address"] == f"http://127.0.0.1:{opened['port']}/" and not opened["reused"]
        base = f"http://127.0.0.1:{opened['port']}"
        assert fetch(f"{base}/")[0] == 403 and fetch(f"{base}/?token=wrong")[0] == 403
        base, cookie, (page, headers) = login(opened)
        assert "HttpOnly" in headers["Set-Cookie"] and "SameSite=Strict" in headers["Set-Cookie"]
        # 无外部资源：页面里没有任何外部地址或外链资源标签，CSP 只放行 self 与内联
        for marker in ("http://", "https://", "src=", "<link", "@import", HOST_TOKEN):
            assert marker not in page, marker
        policy = headers["Content-Security-Policy"]
        assert "default-src 'none'" in policy and "connect-src 'self'" in policy and "http" not in policy

        # API 只认 cookie：查询令牌不行
        token = Path(opened["link_file"]).read_text().strip().split("token=", 1)[1]
        assert fetch(f"{base}/api/state")[0] == 403 and fetch(f"{base}/api/state?token={token}")[0] == 403
        status, text, _ = fetch(f"{base}/api/state", headers={"Cookie": cookie})
        assert status == 200 and HOST_TOKEN not in text and "不应透传" not in text
        state = json.loads(text)
        assert state["host"]["status"] == "ok" and state["gateway"] == {"pid": 4242, "port": 8420}
        assert [row["thread_id"] for row in state["threads"]] == ["t1", "t2"] and state["selected"] == "t1"
        assert state["threads"][0]["title"] == "重构插件 <b>宿主</b>" and state["threads"][0]["compact_generation"] == 2
        assert state["run"]["state"] == "working" and state["run"]["activity"] == "正在运行 pytest"
        assert state["context"]["current_tokens"] == 23981 and state["context"]["tool_schema_tokens"] == 15316
        assert [(p["plugin_id"], p["enabled"]) for p in state["plugins"]] == [("harness-console", True), ("web-board", False)]
        assert state["poll_seconds"] == 2

        # 1 秒缓存：紧接着的同一请求不再打宿主
        count = len(host["requests"])
        assert fetch(f"{base}/api/state", headers={"Cookie": cookie})[0] == 200 and len(host["requests"]) == count
        state = json.loads(fetch(f"{base}/api/state?thread=t2", headers={"Cookie": cookie})[1])
        assert state["selected"] == "t2" and state["run"]["state"] == "waiting" and state["context"] == {"known": False}
        assert {"topics": ["activity"], "thread_id": "t2"} in host["requests"]

        # 写方法与 HEAD 拒绝；再次 open 复用同一服务
        assert fetch(f"{base}/", method="POST", headers={"Cookie": cookie})[0] == 405
        assert fetch(f"{base}/", method="HEAD", headers={"Cookie": cookie})[0] == 405
        error, again = invoke(client, "open")
        assert not error and again["reused"] and again["port"] == opened["port"]

        # 宿主令牌失效后报"插件已停用或令牌失效"
        host["revoked"] = True
        time.sleep(1.2)
        state = json.loads(fetch(f"{base}/api/state", headers={"Cookie": cookie})[1])
        assert state["host"] == {"status": "revoked", "message": "插件已停用或令牌失效"} and state["threads"] == []

        error, stopped = invoke(client, "stop")
        assert not error and stopped["serving"] is False and stopped["stop_reason"] == "stopped"
        assert not port_open(opened["port"])
    finally:
        client.stop()


def test_missing_host_api_env_reports_unavailable(installed_console):
    client, _ = start(installed_console)
    try:
        error, opened = invoke(client, "open")
        assert not error and opened["host_api"] == "unavailable"
        base, cookie, _ = login(opened)
        state = json.loads(fetch(f"{base}/api/state", headers={"Cookie": cookie})[1])
        assert state["host"]["status"] == "unavailable" and "宿主 API 不可用" in state["host"]["message"]
    finally:
        client.stop()
    assert not port_open(opened["port"])


# 函数用途: 生成一个记录 argv 后长睡的假浏览器脚本（exec 保证 pid 不变）。
def fake_chrome(tmp_path):
    record, script = tmp_path / "argv.json", tmp_path / "fake-chrome"
    script.write_text(f"#!/bin/sh\n{sys.executable} -c 'import json,sys; json.dump(sys.argv[1:], open(sys.argv[1], \"w\"))' "
                      f"{record} \"$@\"\nexec sleep 300\n")
    script.chmod(0o755)
    return script, record


@pytest.mark.skipif(os.name == "nt", reason="假浏览器用 sh 脚本")
def test_desktop_launches_app_window_and_stop_closes_it(installed_console, fake_host, tmp_path):
    script, record = fake_chrome(tmp_path)
    client, data_dir = start(installed_console, {"chrome_path": str(script)}, host_url=fake_host[0])
    try:
        error, opened = invoke(client, "desktop")
        assert not error and opened["window"] == "app" and opened["window_reused"] is False
        pid = opened["window_pid"]
        deadline = time.monotonic() + 10
        while not record.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        argv = json.loads(record.read_text())[1:]
        link = Path(opened["link_file"]).read_text().strip()
        assert argv[0] == f"--app={link}" and "token=" in argv[0]
        assert f"--user-data-dir={data_dir / 'app-profile'}" in argv
        assert {"--no-first-run", "--no-default-browser-check", "--window-size=1100,760"} <= set(argv)
        assert pid_alive(pid) and port_open(opened["port"])
        error, again = invoke(client, "desktop")
        assert not error and again["window_reused"] and again["window_pid"] == pid

        error, stopped = invoke(client, "stop")
        assert not error and stopped["window_closed"] is True
        assert not pid_alive(pid) and not port_open(opened["port"])

        # 插件进程退出时同样回收窗口
        error, reopened = invoke(client, "desktop")
        assert not error and reopened["window"] == "app"
    finally:
        client.stop()
    assert not pid_alive(reopened["window_pid"]) and not port_open(reopened["port"])


def test_invalid_settings_fail_startup(installed_console):
    python, _ = installed_console
    for settings in ({"poll_seconds": 0}, {"poll_seconds": 31}, {"open_browser": "yes"}, {"unknown": 1}):
        environment = dict(os.environ, MY_AGENT_PLUGIN_SETTINGS=json.dumps(settings))
        result = subprocess.run([str(python), "-I", "-m", "harness_console"], env=environment,
                                input="", capture_output=True, text=True, timeout=15)
        assert result.returncode == 2 and "插件设置无效" in result.stderr, settings
