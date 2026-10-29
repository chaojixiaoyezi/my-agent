"""Wave9 A3: external plugin source is the SUT; no vertical routes enter Host."""
from __future__ import annotations

import ast
import base64
import hashlib
import importlib.util
import json
import os
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

_DEFAULT_LOCAL_PORTAL_SOURCE = Path.home() / ".my-agent/releases/claude-tools/07-tasks/web-portal-plugin-handoff"
_REPO_FIXTURE_PORTAL_SOURCE = Path(__file__).resolve().parent / "fixtures/web_portal_plugin_handoff"
SOURCE = Path(
    os.environ.get(
        "MY_AGENT_PORTAL_SOURCE_DIR",
        str(_DEFAULT_LOCAL_PORTAL_SOURCE if _DEFAULT_LOCAL_PORTAL_SOURCE.exists() else _REPO_FIXTURE_PORTAL_SOURCE),
    )
)


# LLM: Regression/test helper, confined to temporary state and controlled loopback fixtures.
# 函数用途: 验证接口行为、持久化与安全拒绝（test_standalone_service_is_implemented）。
def test_standalone_service_is_implemented():
    tree = ast.parse((SOURCE / "workbench_server.py").read_text())
    assert any(isinstance(n, ast.ClassDef) and n.name == "SecOpsService" for n in tree.body), (
        "The authorized standalone SQLite + stdlib API engine has not been implemented"
    )


# LLM: Regression/test helper, confined to temporary state and controlled loopback fixtures.
# 函数用途: 准备隔离测试输入并回收本用例资源（portal_module）。
@pytest.fixture
def portal_module():
    test_standalone_service_is_implemented()  # Never execute the legacy monkeypatch bootstrap.
    name = "wave9_portal_test_module"
    spec = importlib.util.spec_from_file_location(name, SOURCE / "workbench_server.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# LLM: Regression/test helper, confined to temporary state and controlled loopback fixtures.
# 函数用途: 准备隔离测试输入并回收本用例资源（portal）。
@pytest.fixture
def portal(portal_module, tmp_path):
    server, service = portal_module.create_secops_server(
        "127.0.0.1", 0, tmp_path,
        admin_token="unit-test-admin", allowed_scopes=("127.0.0.0/8",),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    service.test_url = f"http://127.0.0.1:{server.server_port}"
    try:
        yield service
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        service.close()


# LLM: Regression/test helper, confined to temporary state and controlled loopback fixtures.
# 函数用途: 准备隔离测试输入并回收本用例资源（api）。
def api(portal, path, body=None, method=None, token="unit-test-admin"):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(
        portal.test_url + path,
        None if body is None else json.dumps(body).encode(),
        headers, method=method or ("GET" if body is None else "POST"),
    )
    try:
        response = urllib.request.urlopen(req, timeout=15)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        return response.status, json.load(response)


# LLM: Status-only HTTP client also handles SSE/non-JSON denial targets without mistaking a JSON decoder failure for auth evidence.
# 函数用途: 读取真实HTTP状态并立即关闭响应，避免SSE格式干扰认证回归断言。
def r3_http_status(portal, path, body, token):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(portal.test_url + path,
                                     None if body is None else json.dumps(body).encode(), headers)
    try:
        response = urllib.request.urlopen(request, timeout=10)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        return response.status


# LLM: HTTP regressions intercept only the legacy adapter, never bypass the actual authentication dispatcher.
# 函数用途: 验证所有旧门户路由先认证，且全局旧状态不能被其他owner读取。
@pytest.mark.parametrize("path,body", [
    ("/api/pentest/probe-node", {"ip": "127.0.0.1", "port": 9}),
    ("/api/pentest/projects", None), ("/api/dsh/streams/private", None),
    ("/api/pentest/session/stream-sse?stream_id=private", None),
    ("/api/%64sh/streams/private", None),
])
def test_r3_legacy_requires_auth_and_owner(portal, portal_module, monkeypatch, path, body):
    calls = []
    monkeypatch.setattr(portal_module, "_dispatch_pentest_api", lambda *_: calls.append("pentest") or {})
    monkeypatch.setattr(portal_module, "_dispatch_dsh_api", lambda *_: (200, calls.append("dsh") or {}))
    status = r3_http_status(portal, path, body, "")
    assert status == 401 and calls == []
    other = portal.issue_session("other-owner", is_admin=True)
    assert r3_http_status(portal, path, body, other) == 403
    assert calls == []


# LLM: Real loopback listener detects any socket effect; denied requests must not even reach its accept queue.
# 函数用途: 复现空范围和越界目标探测，验证拒绝发生在真正连接之前。
@pytest.mark.parametrize("scopes", [(), ("192.0.2.0/24",)])
def test_r3_probe_scope_denies_before_connect(portal, scopes):
    import select
    import socket
    portal.allowed_scopes = scopes
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        status, _ = api(portal, "/api/pentest/probe-node", {
            "ip": "127.0.0.1", "port": listener.getsockname()[1],
        })
        assert status in (403, 422)
        assert not select.select([listener], [], [], 0.05)[0]


# LLM: Approved loopback scope must still permit an actual connect, without demo/fabricated reachability responses.
# 函数用途: 验证获准探测使用真实回环端口，固定IP出站并保留实际回执。
def test_r3_probe_authorized_connect_and_legacy_read(portal, portal_module, monkeypatch):
    import select
    import socket
    calls = []
    monkeypatch.setattr(portal_module, "_dispatch_dsh_api", lambda *_: (200, calls.append(True) or {"result": "owned"}))
    portal.legacy_reader = lambda path, query, principal: portal_module._dispatch_dsh_api(portal, "GET", path, query, {})[1]
    assert api(portal, "/api/dsh/streams/owned")[1]["result"] == "owned"
    assert calls == [True]
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        status, result = api(portal, "/api/pentest/probe-node", {
            "ip": "127.0.0.1", "port": listener.getsockname()[1],
        })
        assert status == 200 and result["reachable"] is True
        assert select.select([listener], [], [], 0.5)[0]


# LLM: Execute actual legacy module selection, replacing only outbound IO with a bounded reply to avoid any real remote access.
# 函数用途: 复现旧模块隐式代理和冷启动缺接口，要求未适配路由明确503且完全无出站尝试。
@pytest.mark.parametrize("path,body", [
    ("/api/dsh/session/stream_poll?stream_id=unit", None),
    ("/api/pentest/remote-agent-ask", {"prompt": "synthetic-only"}),
    ("/api/pentest/workspace", None),
])
def test_r3_unconfigured_legacy_never_loads_remote_forwarders(portal, portal_module, monkeypatch, path, body):
    import io
    portal.allowed_scopes = ()
    monkeypatch.delenv("DSH_DISABLE_REMOTE_15", raising=False)
    original = urllib.request.urlopen
    attempted = []

    # LLM: Keep the API client's loopback HTTP real; capture all legacy outbound attempts without opening any external socket.
    # 函数用途: 在真实旧转发器的网络边界截获请求，合成有限响应以避免失败后落入用户文件读写。
    def local_only(request, *args, **kwargs):
        url = request.full_url if hasattr(request, "full_url") else str(request)
        if url.startswith(portal.test_url + "/"):
            return original(request, *args, **kwargs)
        attempted.append(url)
        return io.BytesIO(b'{"ok":true,"synthetic":true}')

    monkeypatch.setattr(portal_module.urllib.request, "urlopen", local_only)
    status, _ = api(portal, path, body)
    assert status == 503
    assert attempted == []


# LLM: No configured password verifier means no password credential; browser admin/owner flags confer no authority.
# 函数用途: 任意密码及未启用密码登录均不能生成假管理员会话。
def test_r3_password_login_disabled_and_bad_credentials(portal):
    body = {"username": "admin", "password": "synthetic-invalid-password", "is_admin": True}
    status, result = api(portal, "/api/auth/login", body, token="")
    assert (status, result.get("error")) == (401, "INVALID_CREDENTIALS")
    assert portal._all("portal_sessions", "_sessions") == []


# LLM: The trusted verifier is the only password-to-principal authority; issued tokens must work via persisted hash-only sessions.
# 函数用途: 受信密码校验成功才能登录，绑定真实owner/角色，拒绝浏览器提权并保留会话可用性。
def test_r3_password_verified_session_is_real(portal):
    portal.password_authenticator = lambda name, password: (
        {"owner_id": "password-owner", "is_admin": False}
        if (name, password) == ("alice", "synthetic-correct-password") else None
    )
    assert api(portal, "/api/auth/login", {"username": "alice", "password": "wrong"}, token="")[0] == 401
    status, result = api(portal, "/api/auth/login", {
        "username": "alice", "password": "synthetic-correct-password", "is_admin": True, "owner_id": "local/main",
    }, token="")
    assert status == 200 and result["owner_id"] == "password-owner" and result["is_admin"] is False
    assert api(portal, "/api/secops/alerts", token=result["session_token"])[0] == 200
    assert api(portal, "/api/dsh/streams/private", token=result["session_token"])[0] == 403
    rows = portal._all("portal_sessions", "_sessions")
    assert len(rows) == 1 and result["session_token"] not in json.dumps(rows)


# LLM: Enrollment goes through real HTTP and TOTP code consumption; clock advancement avoids reuse of the enrollment counter.
# 函数用途: 为隔离用户启用真实TOTP并返回后续挑战可用的密钥。
def r3_enable_mfa(portal, module, owner):
    portal.clock = lambda: 1234567890
    token = portal.issue_session(owner)
    setup = api(portal, "/api/auth/mfa/totp/setup", {}, token=token)[1]
    code = module.totp_code(setup["secret"], int(portal.clock()) // 30)
    assert api(portal, "/api/auth/mfa/totp/verify", {"code": code}, token=token)[0] == 200
    portal.clock = lambda: 1234567920
    return setup["secret"]


# LLM: Provider IO alone is substituted; callback, session policy, TOTP and protected API access use the real HTTP/SQLite path.
# 函数用途: SSO已完成首因子仍须TOTP，错码/伪造升级无效，成功后仅升级该会话且不能重放。
@pytest.mark.parametrize("root", ["/api/auth", "/api/pentest/secops/auth"])
def test_r3_sso_mfa_pending_then_session_upgrade(portal, portal_module, root):
    issuer, subject = "https://sso.unit.test", "mfa-user"
    owner = "sso/" + hashlib.sha256((issuer + "\0" + subject).encode()).hexdigest()
    secret = r3_enable_mfa(portal, portal_module, owner)
    config = {"issuer": issuer, "authorization_endpoint": issuer + "/authorize", "token_endpoint": issuer + "/token",
              "userinfo_endpoint": issuer + "/userinfo", "client_id": "test", "redirect_uri": "http://127.0.0.1/callback"}
    portal._put("portal_sso_config", "default", config, "_sso")
    portal._http_json = lambda url, *args, **kwargs: ({"access_token": "synthetic-provider-access"}
                                                    if url.endswith("/token") else {"sub": subject})
    status, start = api(portal, root + "/sso/initiate", {}, token="")
    assert status == 200, start
    status, result = api(portal, root + "/sso/callback", {
        "state": start["state"], "code": "synthetic-auth-code", "mfa_verified": True,
    }, token="")
    assert status == 200 and result.get("authenticated") is False and result.get("mfa_required") is True
    token = result["session_token"]
    assert api(portal, "/api/secops/alerts", token=token)[0] == 403
    assert api(portal, "/api/dsh/streams/private", token=token)[0] == 403
    session = api(portal, root + "/session", token=token)
    assert session[0] == 200 and session[1]["authenticated"] is False
    code = portal_module.totp_code(secret, int(portal.clock()) // 30)
    assert api(portal, root + "/mfa/totp/verify", {"code": code, "user_id": "other"}, token=token)[0] == 403
    assert api(portal, root + "/mfa/totp/verify", {"code": "not-a-code"}, token=token)[0] == 401
    assert api(portal, "/api/secops/alerts", token=token)[0] == 403
    assert api(portal, root + "/mfa/totp/verify", {"code": code}, token=token)[0] == 200
    assert api(portal, root + "/session", token=token)[1]["authenticated"] is True
    assert api(portal, "/api/secops/alerts", token=token)[0] == 200
    assert api(portal, root + "/mfa/totp/verify", {"code": code}, token=token)[0] == 409


# LLM: Password login uses the same MFA-aware session issuer as SSO, never trusting callback/body bypass flags.
# 函数用途: 有TOTP的密码用户先取得受限会话，不能跳过第二因子读取受保护数据。
def test_r3_password_login_also_requires_mfa(portal, portal_module):
    owner = "password-mfa-owner"
    secret = r3_enable_mfa(portal, portal_module, owner)
    portal.password_authenticator = lambda *_: {"owner_id": owner, "is_admin": False}
    status, result = api(portal, "/api/auth/login", {"username": "user", "password": "synthetic-only"}, token="")
    assert status == 200 and result.get("mfa_required") is True and result.get("authenticated") is False
    token = result["session_token"]
    assert api(portal, "/api/secops/alerts", token=token)[0] == 403
    code = portal_module.totp_code(secret, int(portal.clock()) // 30)
    assert api(portal, "/api/pentest/secops/auth/mfa/totp/verify", {"code": code}, token=token)[0] == 200
    assert api(portal, "/api/secops/alerts", token=token)[0] == 200


# LLM: Trusted external resolver tokens cannot be mutated locally; TOTP must not falsely claim session upgrade without a local session key.
# 函数用途: 外部认证未提供本地会话升级合同则明确拒绝，不返回下次访问仍会失败的假authenticated成功。
def test_r3_external_mfa_token_has_no_fake_local_upgrade(portal, portal_module):
    owner = "external-mfa-owner"
    secret = r3_enable_mfa(portal, portal_module, owner)
    portal.auth_resolver = lambda token: {"owner_id": owner, "is_admin": False} if token == "synthetic-external-token" else None
    code = portal_module.totp_code(secret, int(portal.clock()) // 30)
    status, result = api(portal, "/api/auth/mfa/totp/verify", {"code": code}, token="synthetic-external-token")
    assert (status, result.get("error")) == (409, "MFA_SESSION_NOT_OWNED")
    assert api(portal, "/api/secops/alerts", token="synthetic-external-token")[0] == 403


# LLM: Completing one challenge must not elevate sibling sessions; expired and legacy-shaped sessions obey the same live policy.
# 函数用途: 验证同owner双会话仅升级当前会话，旧字段和过期令牌不能绕过MFA。
def test_r3_mfa_upgrade_is_per_session_and_expiry_fails_closed(portal, portal_module):
    owner = "mfa-session-owner"
    secret = r3_enable_mfa(portal, portal_module, owner)
    first, second = portal.issue_session(owner), portal.issue_session(owner)
    key = hashlib.sha256(second.encode()).hexdigest()
    row = portal._get("portal_sessions", key, "_sessions")
    for field in ("mfa_required", "mfa_verified", "authenticated"):
        row.pop(field, None)
    portal._put("portal_sessions", key, row, "_sessions")
    assert api(portal, "/api/secops/alerts", token=second)[0] == 403
    code = portal_module.totp_code(secret, int(portal.clock()) // 30)
    assert api(portal, "/api/auth/mfa/totp/verify", {"code": code}, token=first)[0] == 200
    assert api(portal, "/api/secops/alerts", token=first)[0] == 200
    assert api(portal, "/api/secops/alerts", token=second)[0] == 403
    assert api(portal, "/api/auth/mfa/totp/verify", {"code": code}, token=second)[0] == 409
    portal.clock = lambda: row["expires_at"] + 1
    assert api(portal, "/api/auth/session", token=first)[0] == 401
    assert api(portal, "/api/auth/mfa/totp/verify", {"code": code}, token=second)[0] == 401


# LLM: Raw HTTP client preserves NDJSON framing and caller-provided connector metadata.
# 函数用途: 将原始字节与请求头送入真实解析入口，不用服务内部调用代替NDJSON测试。
def r3_raw_api(portal, path, raw, headers=None):
    headers = {"Authorization": "Bearer " + portal.admin_token, "Content-Type": "application/x-ndjson", **(headers or {})}
    request = urllib.request.Request(portal.test_url + path, raw, headers, method="POST")
    try:
        response = urllib.request.urlopen(request, timeout=15)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        return response.status, json.load(response)


# LLM: Each documented connector identity carrier must survive NDJSON parsing and owner-scoped ingestion.
# 函数用途: 验证查询、请求头、事件行及一致组合都能摄入，并保留幂等与租户隔离。
@pytest.mark.parametrize("carrier", ["query", "header", "rows", "all"])
def test_r3_ndjson_device_identity_carriers(portal, carrier):
    portal.register_connector("ndjson-device")
    path, headers = "/api/secops/ingest/alert-stream", {}
    events = [{"event_id": "nd-a"}, {"event_id": "nd-b"}]
    if carrier in ("query", "all"):
        path += "?device_id=ndjson-device"
    if carrier in ("header", "all"):
        headers["X-Device-Id"] = "ndjson-device"
    if carrier in ("rows", "all"):
        for row in events:
            row["device_id"] = "ndjson-device"
    raw = b"\n".join(json.dumps(row).encode() for row in events)
    status, result = r3_raw_api(portal, path, raw, headers)
    assert status == 200 and result["device_id"] == "ndjson-device", result
    assert result["inserted"] == 2
    assert r3_raw_api(portal, path, raw, headers)[1]["duplicates"] == 2
    stored = portal._all("secops_alert_events")
    assert len(stored) == 2 and {row["device_id"] for row in stored} == {"ndjson-device"}
    other = portal.issue_session("ndjson-other")
    assert r3_raw_api(portal, path, raw, {**headers, "Authorization": "Bearer " + other})[0] == 403
    other_admin = portal.issue_session("ndjson-other", is_admin=True)
    assert r3_raw_api(portal, path, raw, {**headers, "Authorization": "Bearer " + other_admin})[0] == 404
    assert portal._all("secops_alert_events", "ndjson-other") == []


# LLM: Conflicting identity sources must fail the complete batch before any SQLite insertion; JSON shares the row invariant.
# 函数用途: 拒绝跨设备、重复查询值及来源冲突，验证不会半批写入或静默改写设备字段。
@pytest.mark.parametrize("kind", ["query-row", "query-header", "mixed-rows", "duplicate-query", "json-row"])
def test_r3_ingest_device_conflicts_are_atomic(portal, kind):
    portal.register_connector("device-a")
    portal.register_connector("device-b")
    path, headers = "/api/secops/ingest/alert-stream", {}
    events = [{"event_id": "good", "device_id": "device-a"}, {"event_id": "bad", "device_id": "device-b"}]
    if kind == "query-row":
        path += "?device_id=device-a"
    elif kind == "query-header":
        path += "?device_id=device-a"
        headers["X-Device-Id"] = "device-b"
        events = [{"event_id": "bad"}]
    elif kind == "duplicate-query":
        path += "?device_id=device-a&device_id=device-b"
        events = [{"event_id": "bad"}]
    if kind == "json-row":
        headers["Content-Type"] = "application/json"
        raw = json.dumps({"device_id": "device-a", "events": events}).encode()
    else:
        raw = b"\n".join(json.dumps(row).encode() for row in events)
    assert r3_raw_api(portal, path, raw, headers)[0] == 422
    assert portal._all("secops_alert_events") == []


# LLM: Repack only synthetic test inputs, updating declared digests so malformed handshakes reach the actual MCP boundary.
# 函数用途: 替换测试包入口并同步清单指纹，不修改产品构建器或已验产物。
def r3_repack(package, replacements=None, extra=None):
    import io
    import zipfile
    with zipfile.ZipFile(io.BytesIO(package)) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    files.update(replacements or {})
    manifest = json.loads(files["plugin.json"])
    for record in manifest["files"]:
        record["sha256"] = hashlib.sha256(files[record["path"]]).hexdigest()
    files["plugin.json"] = json.dumps(manifest).encode()
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w") as archive:
        for name, raw in files.items():
            archive.writestr(name, raw)
        if extra:
            archive.writestr(*extra)
    return target.getvalue()


# LLM: Real child processes respond with one controlled protocol defect; failure must reap the process and never persist running state.
# 函数用途: 对抗空初始化、缺版本、错误信封与非对象响应，验证握手拒绝和真实进程清理。
@pytest.mark.parametrize("mode", ["empty", "missing-version", "wrong-version", "unknown-fields", "non-dict", "error", "bad-tools-version", "valid"])
def test_r3_mcp_initialize_response_validation(portal, portal_module, package_bytes, monkeypatch, mode):
    import io
    import zipfile
    with zipfile.ZipFile(io.BytesIO(package_bytes)) as archive:
        entry = json.loads(archive.read("plugin.json"))["entry"]["command"]
    script = '''import json,sys
mode = MODE
for line in sys.stdin:
    request = json.loads(line)
    if 'id' not in request:
        continue
    init = request['method'] == 'initialize'
    result = {'protocolVersion':'2024-11-05','capabilities':{},'serverInfo':{'name':'unit','version':'1'}} if init else {'tools':[]}
    response = {'jsonrpc':'2.0','id':request['id'],'result':result}
    if init and mode == 'empty': response['result'] = {}
    if init and mode == 'missing-version': response.pop('jsonrpc')
    if init and mode == 'wrong-version': response['jsonrpc'] = '1.0'
    if init and mode == 'unknown-fields': response['result'] = {'ready':True}
    if init and mode == 'non-dict': response = []
    if init and mode == 'error': response['error'] = None
    if not init and mode == 'bad-tools-version': response.pop('jsonrpc')
    print(json.dumps(response), flush=True)
'''.replace("MODE", repr(mode))
    package = r3_repack(package_bytes, {entry: script.encode()})
    status, plugin = deploy(portal, package)
    assert status == 200, plugin
    actual_popen = portal_module.subprocess.Popen
    processes = []

    # LLM: Save the real constructor before patching; capture actual handles without replacing the subprocess or handshake.
    # 函数用途: 记录本次真实子进程，供失败清理与管道关闭断言使用。
    def record_process(*args, **kwargs):
        process = actual_popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(portal_module.subprocess, "Popen", record_process)
    status, result = api(portal, "/api/plugins/remote/" + plugin["plugin_id"] + "/mcp/spawn-remote", {})
    if mode == "valid":
        assert status == 200 and result["status"] == "running", result
        return
    assert status in (422, 502), result
    assert processes and all(process.poll() is not None and process.stdin.closed and process.stdout.closed for process in processes)
    assert not portal._daemons
    assert all(row["status"] != "running" for row in portal._all("remote_mcp_Daemons"))


# LLM: 真实子进程先关闭fd0再返回合法初始化，稳定触发后续写入与close再次flush的BrokenPipeError；不用假管道替代故障。
# 函数用途: 验证HTTP结构化失败、真实进程终止、流关闭、映射回收及失败落盘，连续两次重试不占守护上限。
def test_r4_mcp_closed_stdin_persists_failure_and_releases_handles(portal, portal_module, package_bytes, monkeypatch):
    import contextlib
    import io
    import signal
    import zipfile
    with zipfile.ZipFile(io.BytesIO(package_bytes)) as archive:
        entry = json.loads(archive.read("plugin.json"))["entry"]["command"]
    script = b'''import json,os,sys,time
request = json.loads(sys.stdin.buffer.readline())
os.close(0)
print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':{'protocolVersion':'2024-11-05'}}),flush=True)
time.sleep(60)
'''
    status, plugin = deploy(portal, r3_repack(package_bytes, {entry: script}))
    assert status == 200, plugin
    actual_popen = portal_module.subprocess.Popen
    processes = []

    # LLM: 仅记录真实Popen句柄，不修改stdin/stdout或握手时序；子进程在初始化响应前已关闭输入。
    # 函数用途: 取得本次请求实际创建的进程，供退出码与资源关闭断言使用。
    def record_process(*args, **kwargs):
        process = actual_popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(portal_module.subprocess, "Popen", record_process)
    try:
        for attempt in range(2):
            previous_ids = {row["daemon_id"] for row in portal._all("remote_mcp_Daemons")}
            status, result = api(portal, "/api/plugins/remote/" + plugin["plugin_id"] + "/mcp/spawn-remote", {})
            assert (status, result.get("error")) == (502, "MCP_TRANSPORT_FAILED"), result
            process = processes[-1]
            assert process.poll() is not None
            assert process.stdin.closed and process.stdout.closed
            assert portal._daemons == {} and portal._daemon_owners == {}
            rows = portal._all("remote_mcp_Daemons")
            assert len(rows) == attempt + 1
            assert all(row["status"] == "failed" and row["tools"] == [] for row in rows)
            added = [row for row in rows if row["daemon_id"] not in previous_ids]
            assert len(added) == 1
            failure = added[0]
            assert failure["exit_code"] == process.returncode
            assert failure["error"] == "MCP_TRANSPORT_FAILED" and failure["error_type"] == "BrokenPipeError"
            assert failure["cleanup_confirmed"] is True
        portal.close()
        assert portal._daemons == {} and portal._daemon_owners == {}
    finally:
        # 即使红例失败也回收本测试自己的进程与管道；这些兜底发生在全部产品断言之后。
        for process in processes:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    with contextlib.suppress(OSError):
                        stream.close()


# LLM: 每个清理故障只注入单一系统边界，进程和三条流仍为真实资源；无法核验归属时必须保留未知而非盲杀。
# 函数用途: 验证任一流关闭、终止、等待或poll异常都不跳过其他清理及两个映射的finally回收。
@pytest.mark.parametrize("fault", ["stdin", "stdout", "stderr", "terminate", "wait", "poll"])
def test_r4_mcp_stop_cleanup_steps_are_independent(portal, portal_module, monkeypatch, fault):
    import contextlib
    import signal
    import subprocess
    from types import SimpleNamespace
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=True)
    streams = {name: getattr(process, name) for name in ("stdin", "stdout", "stderr")}
    actual_poll, actual_wait, actual_killpg = process.poll, process.wait, os.killpg
    key = "r4-test-owned-process"
    portal._daemons[key], portal._daemon_owners[key] = process, "local/main"
    try:
        if fault in streams:
            stream = streams[fault]

            # LLM: 先释放真实流再模拟flush/close的OSError，验证失败后续步骤，而不制造测试自己的FD泄漏。
            # 函数用途: 在指定流关闭边界制造一次清理异常。
            def close_fault():
                stream.close()
                raise OSError("synthetic close failure")

            setattr(process, fault, SimpleNamespace(close=close_fault))
        elif fault == "terminate":
            # LLM: 仅第一次TERM发送失败，保留真实KILL和进程组归属检查，要求后续回收仍执行。
            # 函数用途: 模拟终止信号的系统异常，不拦截测试兜底使用的原始killpg。
            def kill_fault(pid, sig):
                if pid == process.pid and sig == signal.SIGTERM:
                    raise OSError("synthetic signal failure")
                return actual_killpg(pid, sig)

            monkeypatch.setattr(portal_module.os, "killpg", kill_fault)
        else:
            attempts = []
            actual = actual_poll if fault == "poll" else actual_wait

            # LLM: 第一次系统状态/等待调用失败，后续调用恢复真实Popen方法，不假造进程退出。
            # 函数用途: 验证状态未知或wait异常不会跳过流关闭和句柄移除。
            def process_fault(*args, **kwargs):
                attempts.append(True)
                if len(attempts) == 1:
                    raise OSError("synthetic process observation failure")
                return actual(*args, **kwargs)

            monkeypatch.setattr(process, fault, process_fault)
        try:
            stopped = portal._stop_process(key)
        except Exception as exc:
            stopped = type(exc).__name__
        assert stopped is (fault != "poll"), stopped
        assert all(stream.closed for stream in streams.values())
        assert key not in portal._daemons and key not in portal._daemon_owners
        if fault == "poll":
            assert actual_poll() is None  # 无法确认活锚点归属，不能发信号冒充已终止。
        else:
            assert actual_poll() is not None
    finally:
        if actual_poll() is None:
            actual_killpg(process.pid, signal.SIGKILL)
            actual_wait(timeout=5)
        for stream in streams.values():
            with contextlib.suppress(OSError):
                stream.close()
        portal._daemons.pop(key, None)
        portal._daemon_owners.pop(key, None)


# LLM: Archive links must be rejected even with a directory suffix; real ordinary directories stay valid controls.
# 函数用途: 验证ZIP目录符号链接及tar软硬链接在部署前拒绝，不把普通目录误拒。
@pytest.mark.parametrize("kind", ["zip-dir-link", "zip-file-link", "zip-directory", "tar-dir-link", "tar-hard-link", "tar-directory"])
def test_r3_archive_directory_symlinks_rejected(portal, package_bytes, kind):
    import io
    import stat
    import tarfile
    import zipfile
    if kind.startswith("zip"):
        info = zipfile.ZipInfo("link" if kind == "zip-file-link" else "link/")
        info.create_system = 3
        info.external_attr = ((stat.S_IFDIR if kind == "zip-directory" else stat.S_IFLNK) | 0o755) << 16
        package = r3_repack(package_bytes, extra=(info, b"" if kind == "zip-directory" else b"bin/plugin_main.py"))
    else:
        target = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(package_bytes)) as archive, tarfile.open(fileobj=target, mode="w:gz") as tar:
            for name in archive.namelist():
                raw = archive.read(name)
                info = tarfile.TarInfo(name)
                info.size = len(raw)
                tar.addfile(info, io.BytesIO(raw))
            info = tarfile.TarInfo("link/")
            info.type = tarfile.DIRTYPE if kind == "tar-directory" else (tarfile.LNKTYPE if kind == "tar-hard-link" else tarfile.SYMTYPE)
            info.linkname = "bin/plugin_main.py"
            tar.addfile(info)
        package = target.getvalue()
    status, result = deploy(portal, package)
    assert status == (200 if kind.endswith("directory") else 422), result
    if status != 200:
        assert portal._all("remote_plugins") == []


# LLM: AST checks every definition including nested helpers and methods; decorators do not hide missing contract comments.
# 函数用途: 自动检查测试全文与Wave9服务区的双层注释，新增定义也必须进入覆盖。
def test_r3_dual_layer_comments_cover_all_definitions():
    missing = []
    for path in (Path(__file__), SOURCE / "workbench_server.py"):
        source = path.read_text()
        lines = source.splitlines()
        first = source[:source.index("# A3 service:")].count("\n") + 1 if path.name == "workbench_server.py" else 1
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) or node.lineno < first:
                continue
            start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])]) - 1
            prefix = "\n".join(lines[max(0, start - 2):start])
            human = "类用途" if isinstance(node, ast.ClassDef) else "函数用途"
            if "# LLM:" not in prefix or "# " + human + ":" not in prefix:
                missing.append((path.name, node.lineno, node.name))
    assert not missing, missing


# LLM: Regression/test helper, confined to temporary state and controlled loopback fixtures.
# 函数用途: 验证接口行为、持久化与安全拒绝（test_authentication_and_sqlite_closure）。
def test_authentication_and_sqlite_closure(portal):
    assert api(portal, "/api/secops/alerts", token="")[0] == 401
    import sqlite3
    with sqlite3.connect(portal.state_dir / "portal_state.db") as conn:
        tables = {r[0] for r in conn.execute("select name from sqlite_master where type='table'")}
    assert {
        "remote_plugins", "remote_mcp_Daemons", "secops_connectors", "secops_alert_events",
        "secops_pcap_investigations", "secops_soar_playbooks", "secops_soar_executions",
        "secops_asm_scans", "secops_api_risk_reports", "secops_repro_dispatches",
    } <= tables


# LLM: Regression/test helper, confined to temporary state and controlled loopback fixtures.
# 函数用途: 验证接口行为、持久化与安全拒绝（test_package_is_portable_and_contains_server）。
def test_package_is_portable_and_contains_server(tmp_path):
    source = (SOURCE / "build_plugin_package.py").read_text()
    assert 'Path.home() / "my-agent-dsh"' not in source
    spec = importlib.util.spec_from_file_location("wave9_builder", SOURCE / "build_plugin_package.py")
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    path, snapshot = builder.build_workbench_plugin_zip(tmp_path / "plugin.zip")
    import zipfile
    with zipfile.ZipFile(path) as archive:
        assert "workbench_server.py" in archive.namelist()
        manifest = json.loads(archive.read("plugin.json"))
        assert {r["path"] for r in manifest["files"]} == set(archive.namelist()) - {"plugin.json"}
    assert snapshot.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()


# LLM: Regression/test helper, confined to temporary state and controlled loopback fixtures.
# 函数用途: 验证接口行为、持久化与安全拒绝（test_extracted_package_http_and_deploy_without_host_python）。
def test_extracted_package_http_and_deploy_without_host_python(tmp_path, package_bytes):
    import io
    import selectors
    import subprocess
    import zipfile
    from types import SimpleNamespace
    extracted = tmp_path / "extracted"
    with zipfile.ZipFile(io.BytesIO(package_bytes)) as archive:
        archive.extractall(extracted)  # Only the product-inspected fixture produced by this builder.
    env = dict(os.environ, MY_AGENT_PLUGIN_DATA_DIR=str(tmp_path / "data"),
               MY_AGENT_PORTAL_ADMIN_TOKEN="unit-test-admin")
    env.pop("PYTHONPATH", None)
    process = subprocess.Popen([sys.executable, "-S", str(extracted / "bin/plugin_main.py"),
                                "--serve-http", "--port", "0"], cwd=extracted, env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            assert selector.select(10), "extracted standalone server did not produce a startup receipt"
            line = process.stdout.readline()
        assert line, process.stderr.read(4096).decode(errors="replace") if process.poll() is not None else "standalone process exited before startup"
        receipt = json.loads(line)
        endpoint = SimpleNamespace(test_url=f"http://127.0.0.1:{receipt['address'][1]}")
        assert api(endpoint, "/api/secops/alerts")[0] == 200
        assert api(endpoint, "/api/secops/alerts", token="")[0] == 401
        assert deploy(endpoint, package_bytes)[0] == 200
        assert (tmp_path / "data/portal_state.db").is_file()
    finally:
        process.terminate()
        process.communicate(timeout=10)


# LLM: Regression/test helper, confined to temporary state and controlled loopback fixtures.
# 函数用途: 准备隔离测试输入并回收本用例资源（package_bytes）。
@pytest.fixture
def package_bytes(tmp_path):
    spec = importlib.util.spec_from_file_location("wave9_builder_fixture", SOURCE / "build_plugin_package.py")
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    path, _ = builder.build_workbench_plugin_zip(tmp_path / "pkg.zip")
    return path.read_bytes()


# LLM: Regression/test helper, confined to temporary state and controlled loopback fixtures.
# 函数用途: 准备隔离测试输入并回收本用例资源（deploy）。
def deploy(portal, package_bytes):
    return api(portal, "/api/plugins/remote/deploy", {
        "package_base64": base64.b64encode(package_bytes).decode(),
        "sha256": hashlib.sha256(package_bytes).hexdigest(),
    })


# LLM: Regression/test helper, confined to temporary state and controlled loopback fixtures.
# 函数用途: 验证接口行为、持久化与安全拒绝（test_plugin_lifecycle_and_actual_mcp_handshake）。
def test_plugin_lifecycle_and_actual_mcp_handshake(portal, package_bytes):
    status, body = deploy(portal, package_bytes)
    assert status == 200, body
    plugin_id = body["plugin_id"]
    root = "/api/plugins/remote/" + plugin_id
    assert api(portal, root + "/mcp/spawn-remote", {"command": ["/bin/sh", "-c", "id;id"]})[0] == 403
    status, daemon = api(portal, root + "/mcp/spawn-remote", {})
    assert status == 200, daemon
    assert daemon["status"] == "running" and daemon["tools"]
    assert api(portal, "/api/plugins/remote/mcp/list")[1]["daemons"][0]["daemon_id"] == daemon["daemon_id"]
    assert api(portal, root + "/mcp/stop", {"daemon_id": daemon["daemon_id"]})[1]["status"] == "stopped"
    for action in ("disable", "enable", "reload", "status", "uninstall"):
        assert api(portal, root + "/lifecycle", {"action": action})[0] == 200


# LLM: Regression/test helper, confined to temporary state and controlled loopback fixtures.
# 函数用途: 验证接口行为、持久化与安全拒绝（test_package_hash_mismatch_rejected）。
def test_package_hash_mismatch_rejected(portal, package_bytes):
    assert api(portal, "/api/plugins/remote/deploy", {
        "package_base64": base64.b64encode(package_bytes).decode(), "sha256": "0" * 64,
    })[0] == 422


# LLM: Regression/test helper, confined to temporary state and controlled loopback fixtures.
# 函数用途: 验证接口行为、持久化与安全拒绝（test_totp_rfc6238_enrollment_replay_and_disable）。
def test_totp_rfc6238_enrollment_replay_and_disable(portal, portal_module):
    assert portal_module.totp_code(base64.b32encode(b"12345678901234567890").decode(), 1, 8) == "94287082"
    portal.clock = lambda: 1234567890
    status, setup = api(portal, "/api/auth/mfa/totp/setup", {})
    assert status == 200, setup
    counter = int(portal.clock()) // 30
    code = portal_module.totp_code(setup["secret"], counter)
    assert api(portal, "/api/auth/mfa/totp/verify", {"code": code})[0] == 200
    status, replay = api(portal, "/api/auth/mfa/totp/verify", {"code": code})
    assert (status, replay["error"]) == (409, "TOTP_CODE_REPLAYED")
    assert api(portal, "/api/auth/mfa/totp/setup", {})[0] == 409
    portal.clock = lambda: 1234567920
    assert api(portal, "/api/auth/mfa/totp/disable", {
        "code": portal_module.totp_code(setup["secret"], counter + 1),
    })[1]["enabled"] is False


# LLM: Regression/test helper, confined to temporary state and controlled loopback fixtures.
# 函数用途: 验证接口行为、持久化与安全拒绝（test_host_bridge_is_called_and_denial_never_succeeds）。
def test_host_bridge_is_called_and_denial_never_succeeds(portal, package_bytes):
    calls = []
    portal.host_broker_call = lambda action, payload: calls.append((action, payload)) or {"ok": True}
    assert deploy(portal, package_bytes)[0] == 200
    assert calls[0][0] == "plugins.packages.install"
    portal.host_broker_call = lambda *_: {"ok": False, "error": "DENIED"}
    assert api(portal, "/api/plugins/remote/host-workbench/lifecycle", {"action": "disable"})[0] == 502


# LLM: Regression/test helper, confined to temporary state and controlled loopback fixtures.
# 函数用途: 验证接口行为、持久化与安全拒绝（test_archive_targz_and_traversal_rejection）。
def test_archive_targz_and_traversal_rejection(portal, package_bytes):
    import io
    import tarfile
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(package_bytes)) as archive, tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name in archive.namelist():
            content = archive.read(name)
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    assert deploy(portal, buf.getvalue())[0] == 200
    malformed = io.BytesIO()
    with zipfile.ZipFile(malformed, "w") as archive:
        archive.writestr("../escape.py", "must not extract")
    assert deploy(portal, malformed.getvalue())[0] == 422
    assert not (portal.state_dir.parent / "escape.py").exists()


# LLM: Regression/test helper, confined to temporary state and controlled loopback fixtures.
# 函数用途: 验证接口行为、持久化与安全拒绝（test_user_cannot_install_and_cannot_override_totp_identity）。
def test_user_cannot_install_and_cannot_override_totp_identity(portal, package_bytes):
    token = portal.issue_session("user-a")
    status, result = api(portal, "/api/plugins/remote/deploy", {
        "package_base64": base64.b64encode(package_bytes).decode(),
        "sha256": hashlib.sha256(package_bytes).hexdigest(), "is_admin": True,
    }, token=token)
    assert (status, result["error"]) == (403, "ADMIN_REQUIRED")
    assert api(portal, "/api/auth/mfa/totp/setup", {"user_id": "victim"}, token=token)[0] == 403
    assert api(portal, "/api/auth/mfa/totp/setup", {}, token=token)[0] == 200
    assert portal._get("portal_totp", "totp", "victim") is None


# LLM: Regression/test helper, confined to temporary state and controlled loopback fixtures.
# 函数用途: 验证接口行为、持久化与安全拒绝（test_totp_concurrent_replay_and_persistent_counter）。
def test_totp_concurrent_replay_and_persistent_counter(portal, portal_module):
    import concurrent.futures
    portal.clock = lambda: 1234567890
    setup = api(portal, "/api/auth/mfa/totp/setup", {})[1]
    body = {"code": portal_module.totp_code(setup["secret"], int(portal.clock()) // 30)}
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        statuses = sorted(pool.map(lambda _: api(portal, "/api/auth/mfa/totp/verify", body)[0], range(2)))
    assert statuses == [200, 409]
    second = portal_module.SecOpsService(portal.state_dir, admin_token="unit-test-admin")
    second.clock = portal.clock
    try:
        assert second.handle("POST", "/api/auth/mfa/totp/verify", body, {
            "Authorization": "Bearer unit-test-admin",
        })[0] == 409
    finally:
        second.close()


# LLM: Regression/test helper, confined to temporary state and controlled loopback fixtures.
# 函数用途: 验证接口行为、持久化与安全拒绝（test_totp_pending_setup_is_idempotent_and_host_denial_keeps_mfa）。
def test_totp_pending_setup_is_idempotent_and_host_denial_keeps_mfa(portal, portal_module):
    portal.clock = lambda: 1234567890
    first = api(portal, "/api/auth/mfa/totp/setup", {})[1]
    second = api(portal, "/api/auth/mfa/totp/setup", {})[1]
    assert first["secret"] == second["secret"]
    counter = int(portal.clock()) // 30
    assert api(portal, "/api/auth/mfa/totp/verify", {
        "code": portal_module.totp_code(first["secret"], counter),
    })[0] == 200
    portal.clock = lambda: 1234567920
    portal.host_broker_call = lambda *_: {"ok": False, "error": "DENIED"}
    assert api(portal, "/api/auth/mfa/totp/disable", {
        "code": portal_module.totp_code(first["secret"], counter + 1),
    })[0] == 502
    assert portal._get("portal_totp", "totp")["enabled"] is True


# LLM: Regression/test helper, confined to temporary state and controlled loopback fixtures.
# 函数用途: 验证接口行为、持久化与安全拒绝（test_unknown_mcp_prevents_false_lifecycle_success）。
def test_unknown_mcp_prevents_false_lifecycle_success(portal, package_bytes):
    assert deploy(portal, package_bytes)[0] == 200
    portal._put("remote_mcp_Daemons", "unknown-test", {
        "daemon_id": "unknown-test", "plugin_id": "host-workbench",
        "status": "unknown_after_restart", "tools": [], "pid": 123456,
    })
    for action in ("disable", "reload", "uninstall"):
        assert api(portal, "/api/plugins/remote/host-workbench/lifecycle", {"action": action})[0] == 409
    assert portal._get("remote_plugins", "host-workbench")["status"] == "enabled"
    assert api(portal, "/api/plugins/remote/host-workbench/mcp/spawn-remote", {})[0] == 409


# LLM: Exercise entry exit without a preceding list request; broker must see zero mutations on denial.
# 函数用途: 验证进程提前退出时，不先查列表也不能重新启动或先发出宿主停用动作。
@pytest.mark.parametrize("operation", ["spawn", "disable"])
def test_mcp_exited_entry_checked_before_spawn_or_host_lifecycle(portal, package_bytes, operation):
    assert deploy(portal, package_bytes)[0] == 200
    status, daemon = api(portal, "/api/plugins/remote/host-workbench/mcp/spawn-remote", {})
    assert status == 200, daemon
    process = portal._daemons[daemon["daemon_id"]]
    process.terminate()  # This exact Popen was created by this fixture and has no workers.
    process.wait(timeout=3)
    calls = []
    portal.host_broker_call = lambda action, payload: calls.append(action) or {"ok": True}
    if operation == "spawn":
        status, result = api(portal, "/api/plugins/remote/host-workbench/mcp/spawn-remote", {})
    else:
        status, result = api(portal, "/api/plugins/remote/host-workbench/lifecycle", {"action": "disable"})
    assert status == 409, result
    assert calls == []
    assert portal._get("remote_mcp_Daemons", daemon["daemon_id"])["status"].startswith("unknown_")


# LLM: Reload must replace a live MCP group, not just change an SQL status flag.
# 函数用途: 验证重载确实重新握手新进程，旧守护记录保持已停止。
def test_reload_restarts_actual_mcp(portal, package_bytes):
    assert deploy(portal, package_bytes)[0] == 200
    old = api(portal, "/api/plugins/remote/host-workbench/mcp/spawn-remote", {})[1]
    status, reloaded = api(portal, "/api/plugins/remote/host-workbench/lifecycle", {"action": "reload"})
    assert status == 200, reloaded
    assert len(reloaded["restarted_daemons"]) == 1
    new = portal._get("remote_mcp_Daemons", reloaded["restarted_daemons"][0])
    assert new["daemon_id"] != old["daemon_id"] and new["status"] == "running" and new["tools"]
    assert portal._get("remote_mcp_Daemons", old["daemon_id"])["status"] == "stopped"


# LLM: Regression/test helper, confined to temporary state and controlled loopback fixtures.
# 函数用途: 验证接口行为、持久化与安全拒绝（test_totp_host_operation_reservation_blocks_concurrent_reset）。
def test_totp_host_operation_reservation_blocks_concurrent_reset(portal, portal_module):
    import concurrent.futures
    entered, release = threading.Event(), threading.Event()
    secret = base64.b32encode(b"12345678901234567890").decode()
    portal.clock = lambda: 1234567890

    # LLM: Regression/test helper, confined to temporary state and controlled loopback fixtures.
    # 函数用途: 准备隔离测试输入并回收本用例资源（broker）。
    def broker(action, payload):
        if action.endswith(".setup"):
            entered.set()
            assert release.wait(5)
            return {"ok": True, "secret": secret}
        return {"ok": True}

    portal.host_broker_call = broker
    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        future = pool.submit(api, portal, "/api/auth/mfa/totp/setup", {})
        try:
            assert entered.wait(5)
            assert api(portal, "/api/auth/mfa/totp/setup", {})[0] == 409
            assert api(portal, "/api/auth/mfa/totp/verify", {
                "code": portal_module.totp_code(secret, int(portal.clock()) // 30),
            })[0] == 409
            assert portal._get("portal_totp", "totp")["enabled"] is False
        finally:
            release.set()
        assert future.result()[0] == 200
    assert api(portal, "/api/auth/mfa/totp/verify", {
        "code": portal_module.totp_code(secret, int(portal.clock()) // 30),
    })[0] == 200
    assert portal._get("portal_totp", "totp")["enabled"] is True


# LLM: Regression/test helper, confined to temporary state and controlled loopback fixtures.
# 函数用途: 验证接口行为、持久化与安全拒绝（test_totp_setup_does_not_reset_bruteforce_window）。
def test_totp_setup_does_not_reset_bruteforce_window(portal, portal_module):
    portal.clock = lambda: 1234567890
    setup = api(portal, "/api/auth/mfa/totp/setup", {})[1]
    correct = portal_module.totp_code(setup["secret"], int(portal.clock()) // 30)
    wrong = "000000" if correct != "000000" else "000001"
    for _ in range(5):
        assert api(portal, "/api/auth/mfa/totp/verify", {"code": wrong})[0] == 401
    assert api(portal, "/api/auth/mfa/totp/setup", {})[1]["secret"] == setup["secret"]
    assert api(portal, "/api/auth/mfa/totp/verify", {"code": correct})[0] == 429


# LLM: Regression/test helper, confined to temporary state and controlled loopback fixtures.
# 函数用途: 验证接口行为、持久化与安全拒绝（test_mcp_owned_process_group_reaps_cooperative_worker）。
def test_mcp_owned_process_group_reaps_cooperative_worker(portal, package_bytes):
    import io
    import signal
    import time
    import zipfile
    with zipfile.ZipFile(io.BytesIO(package_bytes)) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    # This fixture alone launches a worker. Its leader reaps that child on group termination.
    script = files["bin/plugin_main.py"].decode()
    marker = "def main() -> int:\n"
    assert marker in script
    script = script.replace(marker, marker + '''    import subprocess, signal
    worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    Path(os.environ["MY_AGENT_PLUGIN_DATA_DIR"], "worker.pid").write_text(str(worker.pid))
    def shutdown_group(*_args):
        worker.wait(timeout=3)
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, shutdown_group)
''')
    files["bin/plugin_main.py"] = script.encode()
    manifest = json.loads(files["plugin.json"])
    next(item for item in manifest["files"] if item["path"] == "bin/plugin_main.py")["sha256"] = hashlib.sha256(files["bin/plugin_main.py"]).hexdigest()
    files["plugin.json"] = json.dumps(manifest).encode()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    worker_pid = None
    try:
        assert deploy(portal, buf.getvalue())[0] == 200
        status, daemon = api(portal, "/api/plugins/remote/host-workbench/mcp/spawn-remote", {})
        assert status == 200, daemon
        worker_pid = int((portal.state_dir / "daemons" / daemon["daemon_id"] / "worker.pid").read_text())
        os.kill(worker_pid, 0)
        assert api(portal, "/api/plugins/remote/host-workbench/mcp/stop", {"daemon_id": daemon["daemon_id"]})[0] == 200
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                os.kill(worker_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.02)
        else:
            pytest.fail("owned MCP worker outlived successful stop")
    finally:
        if worker_pid:
            try:
                os.kill(worker_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


# LLM: Regression/test helper, confined to temporary state and controlled loopback fixtures.
# 函数用途: 验证接口行为、持久化与安全拒绝（test_push_real_http_exact_signature_and_idempotency）。
def test_push_real_http_exact_signature_and_idempotency(portal):
    import hmac
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    received = []

    # LLM: Loopback-only push receiver checks exact signed bytes and emits a bounded receipt.
    # 类用途: 接收合成复现推送并记录原字节，不访问生产节点。
    class Receiver(BaseHTTPRequestHandler):
        # LLM: Regression/test helper, confined to temporary state and controlled loopback fixtures.
        # 函数用途: 准备隔离测试输入并回收本用例资源（do_POST）。
        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            received.append((self.path, raw, dict(self.headers)))
            expected = hmac.new(b"synthetic-test-signing-material", (
                self.headers["X-Agent-Timestamp"] + "." + self.headers["X-Agent-Nonce"] + "." +
                hashlib.sha256(raw).hexdigest()
            ).encode(), hashlib.sha256).hexdigest()
            assert hmac.compare_digest(expected, self.headers["X-Agent-Signature"])
            assert self.headers["X-Agent-Push-Token"] == "synthetic-unit-token"
            payload = json.dumps({"ok": True, "push_id": "push-real-1", "remote_thread_id": "thread-real-1", "remote_run_id": "run-real-1"}).encode()
            self.send_response(202)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        # LLM: Regression/test helper, confined to temporary state and controlled loopback fixtures.
        # 函数用途: 准备隔离测试输入并回收本用例资源（log_message）。
        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    portal.remote_nodes = {"lab": {"url": f"http://127.0.0.1:{server.server_port}/remote-agent/push", "token_ref": "token-fixture", "secret_ref": "sign-fixture"}}
    portal.secret_resolver = lambda ref: {"token-fixture": "synthetic-unit-token", "sign-fixture": "synthetic-test-signing-material"}[ref]
    try:
        request = {"node_id": "lab", "payload": {"task": "controlled unit repro", "unicode": "回环"}, "idempotency_key": "unit_push_key_123456789"}
        status, result = api(portal, "/api/pentest/secops/repro-dispatch", request)
        assert status == 200, result
        assert result["status"] == "accepted" and result["push_id"] == "push-real-1"
        assert result["remote_thread_id"] == "thread-real-1" and result["remote_run_id"] == "run-real-1"
        assert json.loads(received[0][1]) == request["payload"]
        assert api(portal, "/api/pentest/secops/repro-dispatch", request)[1]["dispatch_id"] == result["dispatch_id"]
        assert len(received) == 1
        assert api(portal, "/api/pentest/secops/repro-dispatch/" + result["dispatch_id"])[1]["push_id"] == "push-real-1"
        status, persisted = api(portal, "/api/pentest/secops/repro-dispatch/" + result["dispatch_id"] + "/status")
        assert status == 200 and persisted == result
        other = portal.issue_session("other-owner")
        for suffix in ("", "/status"):
            path = "/api/pentest/secops/repro-dispatch/" + result["dispatch_id"] + suffix
            assert api(portal, path, token=other)[0] == 404
            assert api(portal, path, token="")[0] == 401
        assert api(portal, "/api/pentest/secops/repro-dispatch", request, token="")[0] == 401
        assert len(received) == 1, "状态查询不能再次发送推送"
        request["payload"] = {"different": True}
        assert api(portal, "/api/pentest/secops/repro-dispatch", request)[0] == 409
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


# LLM: Regression/test helper, confined to temporary state and controlled loopback fixtures.
# 函数用途: 验证接口行为、持久化与安全拒绝（test_push_hook_failure_persists_unknown_without_fake_receipt）。
def test_push_hook_failure_persists_unknown_without_fake_receipt(portal):
    portal.remote_nodes = {"lab": {"url": "https://unit.example.test/remote-agent/push", "token_ref": "token", "secret_ref": "sign"}}
    portal.secret_resolver = lambda _: "synthetic-only-test-value"
    portal.remote_push_sender = lambda *_args: {"ok": False}
    assert api(portal, "/api/pentest/secops/repro-dispatch", {"node_id": "lab", "payload": {"task": "test"}})[0] == 502
    row = portal._all("secops_repro_dispatches")[0]
    assert row["status"] == "unknown" and row["push_id"] is None
    assert "synthetic-only-test-value" not in json.dumps(row)


# LLM: Regression/test helper, confined to temporary state and controlled loopback fixtures.
# 函数用途: 验证接口行为、持久化与安全拒绝（test_sso_pkce_backchannel_callback_replay_and_privilege_floor）。
def test_sso_pkce_backchannel_callback_replay_and_privilege_floor(portal):
    import urllib.parse
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    observed = []

    # LLM: Local OAuth fixture records the token exchange and supplies a fixed non-admin subject.
    # 类用途: 在回环验证PKCE后端链路，不信任身份响应中的管理员声明。
    class Provider(BaseHTTPRequestHandler):
        # LLM: Regression/test helper, confined to temporary state and controlled loopback fixtures.
        # 函数用途: 准备隔离测试输入并回收本用例资源（do_POST）。
        def do_POST(self):
            form = urllib.parse.parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
            observed.append(form)
            self.reply({"access_token": "synthetic-provider-access", "token_type": "Bearer"})

        # LLM: Regression/test helper, confined to temporary state and controlled loopback fixtures.
        # 函数用途: 准备隔离测试输入并回收本用例资源（do_GET）。
        def do_GET(self):
            assert self.headers["Authorization"] == "Bearer synthetic-provider-access"
            self.reply({"sub": "user-123", "is_admin": True, "owner_id": "local/main"})

        # LLM: Regression/test helper, confined to temporary state and controlled loopback fixtures.
        # 函数用途: 准备隔离测试输入并回收本用例资源（reply）。
        def reply(self, data):
            raw = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        # LLM: Regression/test helper, confined to temporary state and controlled loopback fixtures.
        # 函数用途: 准备隔离测试输入并回收本用例资源（log_message）。
        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    config = {"issuer": origin, "authorization_endpoint": origin + "/authorize", "token_endpoint": origin + "/token", "userinfo_endpoint": origin + "/userinfo", "client_id": "unit-client", "redirect_uri": "http://127.0.0.1/callback"}
    try:
        assert api(portal, "/api/auth/sso/config", config, method="PUT")[0] == 200
        assert api(portal, "/api/auth/sso/config")[1]["configured"] is True
        status, start = api(portal, "/api/auth/sso/initiate", {}, token="")
        assert status == 200, start
        params = urllib.parse.parse_qs(urllib.parse.urlsplit(start["authorization_url"]).query)
        assert params["code_challenge_method"] == ["S256"]
        assert api(portal, "/api/auth/sso/callback", {"state": "forged", "code": "code"}, token="")[0] == 409
        body = {"state": start["state"], "code": "synthetic-auth-code", "is_admin": True}
        status, result = api(portal, "/api/auth/sso/callback", body, token="")
        assert status == 200, result
        assert result["is_admin"] is False and result["owner_id"] != "local/main"
        verifier = observed[0]["code_verifier"][0]
        assert base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode() == params["code_challenge"][0]
        assert api(portal, "/api/auth/sso/callback", body, token="")[0] == 409
        assert api(portal, "/api/auth/sso/config", config, method="PUT", token=result["session_token"])[0] == 403
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


import concurrent.futures
import contextlib
import socket
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


# LLM: Test-only loopback receiver records real connector HTTP, never real device effects.
# 函数用途: 启动回环动作接收器并在退出时清理。
@contextlib.contextmanager
def action_receiver(reject=False):
    received = []

    # LLM: Local connector fixture returns explicit success/rejection without real device effects.
    # 类用途: 记录SOAR合成动作及失败回执，不实际隔离设备。
    class Receiver(BaseHTTPRequestHandler):
        # LLM: Test POST handler returns explicit upstream success or failure.
        # 函数用途: 保存真实动作载荷并发送有界 JSON 响应。
        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            raw = json.dumps({'ok': not reject, 'status': 'failed' if reject else 'succeeded',
                              'receipt': 'loopback-confirmed'}).encode()
            self.send_response(503 if reject else 200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        # LLM: Test-only log suppression.
        # 函数用途: 关闭测试请求的标准错误日志。
        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Receiver)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield 'http://127.0.0.1:%s/action' % server.server_port, received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)


# LLM: Trusted registration is out of band; the HTTP caller cannot supply this URL.
# 函数用途: 将回环接收器登记为可信 EDR 测试连接器。
def action_connector(portal, url):
    portal.register_connector('edr-test', kind='edr', config={
        'action_url': url, 'allow_insecure_loopback': True, 'timeout': 2,
    })


# LLM: Tests approval owner/action/target/digest/expiry and sequential replay over real HTTP.
# 函数用途: 验证伪造和错配审批均被拒绝，只有真实审批触发一次连接器。
def test_soar_approval_binding_and_real_connector(portal):
    path = '/api/secops/soar/execute-action'
    request = {'action': 'isolate_host', 'device_id': 'edr-test', 'params': {'reason': 'test'}}
    with action_receiver() as (url, received):
        action_connector(portal, url)
        for approval in (None, 'forged'):
            body = dict(request, approved=True)
            if approval:
                body['approval_id'] = approval
            status, result = api(portal, path, body)
            assert (status, result['error']) == (403, 'SOAR_APPROVAL_REQUIRED')
        portal.approve_action('expired', 'isolate_host', 'edr-test', request['params'],
                              expires_at=portal.clock() - 1)
        assert api(portal, path, dict(request, approval_id='expired'))[0] == 403
        portal.approve_action('mismatch', 'isolate_host', 'edr-test', {'reason': 'other'})
        assert api(portal, path, dict(request, approval_id='mismatch'))[0] == 403
        portal.approve_action('wrong-device', 'isolate_host', 'different', request['params'])
        assert api(portal, path, dict(request, approval_id='wrong-device'))[0] == 403
        portal.approve_action('wrong-action', 'block_ip', 'edr-test', request['params'])
        assert api(portal, path, dict(request, approval_id='wrong-action'))[0] == 403
        portal.approve_action('wrong-owner', 'isolate_host', 'edr-test', request['params'],
                              owner_id='other-owner')
        assert api(portal, path, dict(request, approval_id='wrong-owner'))[0] == 403
        assert received == []
        portal.approve_action('valid', 'isolate_host', 'edr-test', request['params'])
        status, result = api(portal, path, dict(request, approval_id='valid'))
        assert status == 200, result
        assert result['status'] == 'succeeded' and len(received) == 1
        assert received[0]['action'] == 'isolate_host'
        assert received[0]['params'] == request['params']
        assert api(portal, path, dict(request, approval_id='valid'))[0] == 403
        assert len(received) == 1
        assert portal._get('secops_soar_executions', result['execution_id'])['status'] == 'succeeded'


# LLM: Concurrent HTTP calls exercise atomic SQLite consumption.
# 函数用途: 验证同一审批并发提交只执行一次。
def test_soar_atomic_consumption(portal):
    body = {'action': 'block_ip', 'device_id': 'edr-test', 'params': {'ip': '127.0.0.2'},
            'approval_id': 'single-shot'}
    with action_receiver() as (url, received):
        action_connector(portal, url)
        portal.approve_action('single-shot', 'block_ip', 'edr-test', body['params'])
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: api(portal, '/api/secops/soar/execute-action', body),
                                    range(2)))
        assert sorted(status for status, _ in results) == [200, 403]
        assert len(received) == 1


# LLM: Unknown/unconfigured effects reject; real upstream failure persists without fake state.
# 函数用途: 验证未知动作、本地连接器及上游失败不能返回伪成功。
def test_soar_unknown_unconfigured_and_upstream_failure(portal):
    path = '/api/secops/soar/execute-action'
    portal.register_connector('local-test')
    assert api(portal, path, {'action': 'run_shell', 'device_id': 'local-test'})[0] == 422
    status, result = api(portal, path, {'action': 'get_status', 'device_id': 'local-test'})
    assert status == 409 and result['error'] == 'SOAR_EXECUTOR_UNAVAILABLE'
    with action_receiver(reject=True) as (url, received):
        action_connector(portal, url)
        status, result = api(portal, path, {'action': 'get_status', 'device_id': 'edr-test'})
        assert status == 502, result
        assert len(received) == 1
        assert any(row['status'] == 'failed' and row['error_detail']
                   for row in portal._all('secops_soar_executions'))


# LLM: A dangerous saved sibling and body code must never run during pure single-node debug.
# 函数用途: 验证只执行已保存的指定纯节点而不运行危险兄弟节点。
def test_debug_saved_node_isolation(portal):
    portal.register_workflow('wf-isolation', [
        {'node_id': 'pick', 'operation': 'select_fields', 'fields': ['answer']},
        {'node_id': 'danger', 'operation': 'shell', 'command': 'never-run'},
    ])
    status, result = api(portal, '/api/workflows/wf-isolation/debug-node', {
        'node_id': 'pick', 'mock_inputs': {'answer': 42, 'hidden': 'not-selected'},
        'operation': 'shell', 'command': 'never-run',
    })
    assert status == 200, result
    assert result['status'] == 'succeeded' and result['output_data'] == {'answer': 42}
    assert result['duration_ms'] >= 0 and isinstance(result['logs'], list)
    assert result['error_detail'] is None
    assert api(portal, '/api/workflows/wf-isolation/debug-node', {
        'node_id': 'danger', 'mock_inputs': {},
    })[0] == 422
    assert api(portal, '/api/workflows/wf-isolation/debug-node', {
        'node_id': 'absent', 'mock_inputs': {},
    })[0] == 404
    assert portal._get('portal_workflows', 'wf-isolation')['nodes'][0]['node_id'] == 'pick'


# LLM: Host hook output is authoritative; denial must not fall back to fake local success.
# 函数用途: 验证 Host 调试桥实际返回和拒绝被采用。
def test_debug_host_hook_actual_output_and_denial(portal):
    calls = []
    portal.register_workflow('wf-hook', [{'node_id': 'one', 'operation': 'identity'}])
    portal.host_broker_call = lambda action, payload: calls.append((action, payload)) or {
        'ok': True, 'status': 'succeeded', 'duration_ms': 7,
        'output_data': {'from': 'host'}, 'logs': ['host actually ran'], 'error_detail': None,
    }
    status, result = api(portal, '/api/workflows/wf-hook/debug-node', {
        'node_id': 'one', 'mock_inputs': {'x': 1},
    })
    assert status == 200 and result['output_data'] == {'from': 'host'}
    assert result['duration_ms'] == 7 and calls[0][0] == 'workflows.steps.debug'
    assert calls[0][1]['node_id'] == 'one'
    assert 'nodes' not in calls[0][1] and calls[0][1]['isolated'] is True
    portal.host_broker_call = lambda *_args: {'ok': False, 'error': 'DENIED'}
    assert api(portal, '/api/workflows/wf-hook/debug-node', {
        'node_id': 'one', 'mock_inputs': {},
    })[0] == 502


# LLM: Bounded polling only reads this owner's persisted scan.
# 函数用途: 等待扫描进入终态，不产生额外探测。
def wait_action_scan(portal, scan_id):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        status, result = api(portal, '/api/secops/asm/scans/' + scan_id)
        assert status == 200, result
        if result['status'] in ('completed', 'failed', 'cancelled'):
            return result
        time.sleep(0.02)
    raise AssertionError('bounded loopback scan did not finish')


# LLM: Real loopback socket proves TCP connection observation, not external reachability.
# 函数用途: 验证 TCP 实测、SQLite 持久结果及主机端口边界。
def test_asm_real_loopback_and_bounds(portal):
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen(4)
        port = listener.getsockname()[1]
        status, queued = api(portal, '/api/secops/asm/dispatch-scan', {
            'targets': ['127.0.0.1'], 'ports': [port],
        })
        assert status == 200, queued
        result = wait_action_scan(portal, queued['scan_id'])
        assert result['status'] == 'completed' and result['results'][0]['state'] == 'open'
        assert result['results'][0]['ip'] == '127.0.0.1' and result['results'][0]['port'] == port
        assert portal._get('secops_asm_scans', queued['scan_id'])['results'] == result['results']
    for request in (
        {'targets': ['10.0.0.1'], 'ports': [443]},
        {'targets': ['127.0.0.1/24'], 'ports': [443]},
        {'targets': ['127.0.0.1'], 'ports': list(range(1, 18))},
        {'targets': ['127.0.0.1'], 'ports': [0]},
    ):
        assert api(portal, '/api/secops/asm/dispatch-scan', request)[0] in (403, 422)


# LLM: Even an allowed domain may not grant private networks implicitly.
# 函数用途: 验证获准域名解析到未授权私网时在连接前拒绝。
def test_asm_dns_private_policy(portal, monkeypatch):
    portal.allowed_scopes = ('trusted.example.test',)
    calls = []
    real_resolution = socket.getaddrinfo

    # LLM: Test DNS replacement never contacts a resolver for the synthetic domain.
    # 函数用途: 仅模拟目标域名，保留 HTTP 客户端自身的回环地址解析。
    def private_resolution(host, port, *args, **kwargs):
        if host != 'trusted.example.test':
            return real_resolution(host, port, *args, **kwargs)
        calls.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('10.0.0.1', port or 0))]

    monkeypatch.setattr(socket, 'getaddrinfo', private_resolution)
    status, result = api(portal, '/api/secops/asm/dispatch-scan', {
        'targets': ['trusted.example.test'], 'ports': [443],
    })
    assert status == 403, result
    assert calls == ['trusted.example.test']


# LLM: Scope must come from authenticated principal, not request body.
# 函数用途: 验证其他租户的扫描与保存工作流均不可读取。
def test_action_owner_isolation(portal):
    portal._put('secops_asm_scans', 'foreign-scan', {'scan_id': 'foreign-scan', 'status': 'completed'},
                owner='foreign-owner')
    portal.register_workflow('foreign-workflow', [{'node_id': 'x', 'operation': 'identity'}],
                             owner_id='foreign-owner')
    assert api(portal, '/api/secops/asm/scans/foreign-scan')[0] == 404
    assert api(portal, '/api/workflows/foreign-workflow/debug-node', {
        'node_id': 'x', 'mock_inputs': {}, 'owner_id': 'foreign-owner',
    })[0] == 404


# LLM: First DNS answer is frozen; a second lookup would point outside the allowed network.
# 函数用途: 验证扫描工作线程不会重新解析已冻结域名。
def test_asm_frozen_dns_uses_real_loopback(portal, monkeypatch):
    portal.allowed_scopes = ('127.0.0.0/8', 'frozen.example.test')
    real_resolution = socket.getaddrinfo
    calls = []

    # LLM: Synthetic domain resolution stays test-local and preserves API client's own IP lookup.
    # 函数用途: 首次返回回环地址，任何额外解析返回未授权地址以暴露重绑定缺陷。
    def rebinding_resolution(host, port, *args, **kwargs):
        if host != 'frozen.example.test':
            return real_resolution(host, port, *args, **kwargs)
        calls.append(host)
        ip = '127.0.0.1' if len(calls) == 1 else '10.0.0.1'
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, port or 0))]

    monkeypatch.setattr(socket, 'getaddrinfo', rebinding_resolution)
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen(4)
        status, queued = api(portal, '/api/secops/asm/dispatch-scan', {
            'targets': ['frozen.example.test'], 'ports': [listener.getsockname()[1]],
        })
        assert status == 200, queued
        result = wait_action_scan(portal, queued['scan_id'])
        assert result['results'][0]['state'] == 'open'
        assert result['results'][0]['ip'] == '127.0.0.1'
        assert calls == ['frozen.example.test']


# LLM: Real accepts measure low frequency; service close event must cancel remaining queued probes.
# 函数用途: 验证跨扫描每目标限速和关闭事件中断后续连接。
def test_asm_cross_scan_rate_and_close_cancellation(portal):
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen(8)
        listener.settimeout(4)
        request = {'targets': ['127.0.0.1'], 'ports': [listener.getsockname()[1]]}
        times, scan_ids = [], []
        for _ in range(2):
            status, queued = api(portal, '/api/secops/asm/dispatch-scan', request)
            assert status == 200, queued
            scan_ids.append(queued['scan_id'])
            connection, _address = listener.accept()
            times.append(time.monotonic())
            connection.close()
        assert times[1] - times[0] >= 0.49
        for scan_id in scan_ids:
            assert wait_action_scan(portal, scan_id)['status'] == 'completed'
        status, queued = api(portal, '/api/secops/asm/dispatch-scan', {
            'targets': ['127.0.0.1'], 'ports': list(range(41000, 41016)),
        })
        assert status == 200, queued
        portal._closed.set()
        cancelled = wait_action_scan(portal, queued['scan_id'])
        assert cancelled['status'] == 'cancelled' and len(cancelled['results']) < 16


# LLM: Pure whitelist includes identity/merge/template; bad templates never evaluate Python.
# 函数用途: 验证剩余纯操作和模板缺失输入的真实失败返回。
def test_debug_pure_operation_results(portal):
    portal.register_workflow('wf-pure', [
        {'node_id': 'identity', 'operation': 'identity'},
        {'node_id': 'merge', 'operation': 'merge', 'values': {'saved': True}},
        {'node_id': 'template', 'operation': 'template', 'template': 'Hello ${name}'},
        {'node_id': 'invalid', 'operation': 'template', 'template': '${x.__class__}'},
    ])
    path = '/api/workflows/wf-pure/debug-node'
    assert api(portal, path, {'node_id': 'identity', 'mock_inputs': {'x': 1}})[1]['output_data'] == {'x': 1}
    assert api(portal, path, {'node_id': 'merge', 'mock_inputs': {'x': 1}})[1]['output_data'] == {
        'x': 1, 'saved': True,
    }
    assert api(portal, path, {'node_id': 'template', 'mock_inputs': {'name': 'world'}})[1]['output_data'] == 'Hello world'
    missing = api(portal, path, {'node_id': 'template', 'mock_inputs': {}})[1]
    assert missing['status'] == 'failed' and missing['error_detail'] == 'WORKFLOW_TEMPLATE_MISSING_INPUT'
    assert api(portal, path, {'node_id': 'invalid', 'mock_inputs': {}})[0] == 422


import sqlite3
import struct
from http.server import HTTPServer


# LLM: Synthetic packet factory used by pure and HTTP tests; never sends packets.
# 函数用途: 构造合法大小端 PCAP，含 VLAN、IPv4、DNS 或 TCP/HTTP。
def data_test_pcap(endian="<", vlan=False, tcp=False, count=1):
    payload = (b"GET /profile HTTP/1.1\r\nHost: unit.invalid\r\n\r\n" if tcp else
               b"\x00\x01\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x04unit\x07invalid\x00\x00\x01\x00\x01")
    frames = []
    for i in range(count):
        if tcp:
            transport = struct.pack("!HHIIBBHHH", 40000, 80 + i, 0, 0, 80, 2, 8192, 0, 0) + payload
        else:
            transport = struct.pack("!HHHH", 40000, 53, 8 + len(payload), 0) + payload
        ip = struct.pack("!BBHHHBBH4s4s", 69, 0, 20 + len(transport), i, 0, 64, 6 if tcp else 17, 0,
                         b"\x0a\x00\x00\x01", b"\x0a\x00\x00\x02")
        ethernet = b"\0" * 12 + (b"\x81\x00\x00\x01\x08\x00" if vlan else b"\x08\x00")
        frame = ethernet + ip + transport
        frames.append(struct.pack(endian + "IIII", 100 + i, 0, len(frame), len(frame)) + frame)
    return struct.pack(endian + "IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1) + b"".join(frames)


# LLM: Pure regression, checks actual parsed fields and malformed-input failures.
# 函数用途: 核验大小端、VLAN、DNS、HTTP、端口扫描和空包证据口径。
def test_data_pure_pcap(portal_module):
    import pytest
    for endian in ("<", ">"):
        parsed = portal_module.parse_secops_pcap(data_test_pcap(endian, vlan=True))
        assert parsed["packet_count"] == 1
        flow = parsed["flows"][0]
        assert (flow["src_ip"], flow["dst_ip"], flow["src_port"], flow["dst_port"], flow["protocol"]) == (
            "10.0.0.1", "10.0.0.2", 40000, 53, "UDP")
        assert "unit.invalid" in parsed["dns_queries"]
    parsed = portal_module.parse_secops_pcap(data_test_pcap(tcp=True, count=10))
    assert any(a["kind"] == "port_scan" for a in parsed["anomalies"])
    assert parsed["http_hints"][0]["method"] == "GET"
    periodic = portal_module.parse_secops_pcap(data_test_pcap(count=6))
    assert any(a["kind"] == "periodic_traffic" for a in periodic["anomalies"])
    for blob in (b"not a pcap", data_test_pcap()[:-1]):
        with pytest.raises(portal_module.PortalError):
            portal_module.parse_secops_pcap(blob)
    with pytest.raises(portal_module.PortalError):
        portal_module.parse_secops_pcap(data_test_pcap(), max_packets=0)
    empty = struct.pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1)
    assert portal_module.parse_secops_pcap(empty)["query_evidence"]["source_has_no_data"] is None


# LLM: Independent fixture encoder only; lengths, padding and duplicated trailers follow the wire format, not product helpers.
# 函数用途: 编码合成 PCAPNG 块，不运行或发送其中的流量。
def data_test_pcapng_block(kind, body, endian="<"):
    padded = body + b"\0" * (-len(body) % 4)
    length = len(padded) + 12
    return struct.pack(endian + "II", kind, length) + padded + struct.pack(endian + "I", length)


# LLM: Synthetic SHB/IDB/EPB/SPB fixture; packet bytes reuse a legal classic packet without passing through the parser.
# 函数用途: 生成大小端最小抓包，支持接口时间选项、无时间戳简单包和截断捕获。
def data_test_pcapng(endian="<", *, simple=False, options=b"", snaplen=65535, captured=None, vlan=False):
    section = data_test_pcapng_block(0x0A0D0D0A, struct.pack(endian + "IHHq", 0x1A2B3C4D, 1, 0, -1), endian)
    interface = data_test_pcapng_block(1, struct.pack(endian + "HHI", 1, 0, snaplen) + options, endian)
    frame = data_test_pcap(vlan=vlan)[40:]
    count = len(frame) if captured is None else captured
    if simple:
        packet = struct.pack(endian + "I", len(frame)) + frame[:count]
    else:
        packet = struct.pack(endian + "IIIII", 0, 0, 100500000, count, len(frame)) + frame[:count]
    return section + interface + data_test_pcapng_block(3 if simple else 6, packet, endian)


# LLM: Both byte orders decode EPB/SPB metadata; timestamp-less SPB stays unknown.
# 函数用途: 核验最小PCAPNG包块的流、DNS和来源证据，不编造时间。
@pytest.mark.parametrize("endian", ["<", ">"])
@pytest.mark.parametrize("simple", [False, True])
def test_data_pcapng_minimal_packet_blocks(portal_module, endian, simple):
    blob = data_test_pcapng(endian, simple=simple)
    parsed = portal_module.parse_secops_pcap(blob)
    assert parsed["packet_count"] == 1 and parsed["flow_count"] == 1
    assert parsed["dns_queries"] == ["unit.invalid"]
    flow = parsed["flows"][0]
    assert (flow["src_ip"], flow["dst_ip"], flow["protocol"]) == ("10.0.0.1", "10.0.0.2", "UDP")
    evidence = parsed["query_evidence"]
    assert evidence["source_kind"] == "uploaded_pcapng" and evidence["input_bytes"] == len(blob)
    assert evidence["byte_order"] == ("little" if endian == "<" else "big")
    assert evidence["captured_records"] == evidence["decoded_packets"] == 1
    expected = None if simple else 100.5
    assert evidence["observed_time_range"] == {"start": expected, "end": expected}
    assert flow["first_seen"] == flow["last_seen"] == expected
    if simple:
        assert "pcapng_packets_without_timestamps" in parsed["warnings"]


# LLM: Interface timestamps use declared decimal/binary resolution and signed offset.
# 函数用途: 验证接口计时选项，避免统一按微秒误解时间。
@pytest.mark.parametrize("endian", ["<", ">"])
@pytest.mark.parametrize("resolution,units", [(9, 10**9), (0x80 | 10, 2**10)])
def test_data_pcapng_interface_timestamp_options(portal_module, endian, resolution, units):
    options = (struct.pack(endian + "HHB3x", 9, 1, resolution)
               + struct.pack(endian + "HHq", 14, 8, -10) + b"\0" * 4)
    parsed = portal_module.parse_secops_pcap(data_test_pcapng(endian, options=options))
    assert parsed["flows"][0]["first_seen"] == pytest.approx(100500000 / units - 10)
    assert parsed["query_evidence"]["timestamp_resolution"] == units


# LLM: Section switches reset interface IDs, but packet/byte budgets remain capture-wide.
# 函数用途: 验证跨节大小端、多接口引用与输入上限，拒绝借新节绕过限额。
def test_data_pcapng_sections_interfaces_and_packet_limits(portal_module):
    first = data_test_pcapng()
    second = data_test_pcapng(">", simple=True)
    parsed = portal_module.parse_secops_pcap(first + second, max_packets=2)
    assert parsed["packet_count"] == 2 and parsed["flows"][0]["packets"] == 2
    assert parsed["query_evidence"]["byte_order"] == "mixed"
    assert parsed["flows"][0]["first_seen"] == parsed["flows"][0]["last_seen"] == 100.5
    with pytest.raises(portal_module.PortalError) as error:
        portal_module.parse_secops_pcap(first + second, max_packets=1)
    assert error.value.code == "PCAP_LIMIT"
    with pytest.raises(portal_module.PortalError) as error:
        portal_module.parse_secops_pcap(first + b"\0" * (8 * 1024 * 1024))
    assert error.value.code == "INVALID_PCAP"
    empty = portal_module.parse_secops_pcap(first[:48])
    assert empty["packet_count"] == 0 and empty["query_evidence"]["source_has_no_data"] is None
    # Interface 1 is valid only after its IDB, and carries its own snap length.
    interface = data_test_pcapng_block(1, struct.pack("<HHI", 1, 0, 65535))
    packet = bytearray(first[48:])
    struct.pack_into("<I", packet, 8, 1)
    assert portal_module.parse_secops_pcap(first[:48] + interface + packet)["packet_count"] == 1
    with pytest.raises(portal_module.PortalError) as error:
        portal_module.parse_secops_pcap(first + second[:28] + packet)
    assert error.value.code == "MALFORMED_PCAP"


# LLM: Snaplen truncation is evidence of an undecoded packet, not a complete flow.
# 函数用途: 核验EPB/SPB截断计数和覆盖警告，不把缺失字节解释为完整流。
@pytest.mark.parametrize("simple", [False, True])
def test_data_pcapng_snaplen_truncation(portal_module, simple):
    parsed = portal_module.parse_secops_pcap(data_test_pcapng(simple=simple, snaplen=34, captured=34))
    assert parsed["packet_count"] == 1 and parsed["flow_count"] == 0
    assert parsed["query_evidence"]["truncated_capture_packets"] == 1
    assert "snaplen_truncated_packets" in parsed["warnings"]


# LLM: Compare the same legal wire truncation across all supported containers; no product encoding helpers are used.
# 函数用途: 构造完整头部不足的合法截断包，或原始长度也同样短的真正畸形包。
def data_test_short_capture(kind, captured, *, vlan=False, original_short=False):
    if kind == "classic":
        blob = data_test_pcap(vlan=vlan)
        original = captured if original_short else len(blob) - 40
        return blob[:24] + struct.pack("<IIII", 100, 0, captured, original) + blob[40:40 + captured]
    blob = bytearray(data_test_pcapng(simple=kind == "spb", snaplen=captured, captured=captured, vlan=vlan))
    if original_short:
        struct.pack_into("<I", blob, 56 if kind == "spb" else 72, captured)
    return bytes(blob)


# LLM: Container-proven truncation can cut headers and must retain packet/skip counts.
# 函数用途: 三种容器共同验证合法短捕获不会导致整份抓包误拒。
@pytest.mark.parametrize("kind", ["classic", "epb", "spb"])
@pytest.mark.parametrize("captured", [10, 14, 16, 24])
def test_pcap_truncated_headers_are_undecoded_not_malformed(portal_module, kind, captured):
    parsed = portal_module.parse_secops_pcap(data_test_short_capture(kind, captured))
    assert parsed["packet_count"] == 1 and parsed["flow_count"] == 0
    assert parsed["query_evidence"]["truncated_capture_packets"] == 1
    assert parsed["query_evidence"]["unsupported_or_skipped_packets"] == 1
    assert "snaplen_truncated_packets" in parsed["warnings"]


# LLM: Incomplete VLAN tags distinguish snaplen loss from malformed original bytes.
# 函数用途: 验证截断VLAN头只计未解码，不越界读取标签。
@pytest.mark.parametrize("kind", ["classic", "epb", "spb"])
def test_pcap_truncated_vlan_header_is_undecoded(portal_module, kind):
    parsed = portal_module.parse_secops_pcap(data_test_short_capture(kind, 16, vlan=True))
    assert parsed["packet_count"] == 1 and parsed["flow_count"] == 0
    assert parsed["query_evidence"]["unsupported_or_skipped_packets"] == 1


# LLM: Truncation tolerance must not permit malformed complete short frames.
# 函数用途: 用未截断短包作反例，防止修复放宽包格式校验。
@pytest.mark.parametrize("kind", ["classic", "epb", "spb"])
def test_pcap_nontruncated_short_frame_still_rejected(portal_module, kind):
    with pytest.raises(portal_module.PortalError) as error:
        portal_module.parse_secops_pcap(data_test_short_capture(kind, 10, original_short=True))
    assert error.value.code == "MALFORMED_PCAP"


# LLM: Explicit section length must align with the actual section/end boundary.
# 函数用途: 对照合法节长度与错位长度，校验节边界合同。
@pytest.mark.parametrize("delta", [-4, 0, 4])
def test_pcapng_explicit_section_length(portal_module, delta):
    blob = bytearray(data_test_pcapng())
    struct.pack_into("<q", blob, 16, len(blob) - 28 + delta)
    if delta == 0:
        assert portal_module.parse_secops_pcap(bytes(blob))["packet_count"] == 1
    else:
        with pytest.raises(portal_module.PortalError) as error:
            portal_module.parse_secops_pcap(bytes(blob))
        assert error.value.code == "MALFORMED_PCAP"


# LLM: Interface ID and all 64 timestamp bits determine each packet observation time.
# 函数用途: 验证高32位时间与不同接口时钟，不丢高位或套错分辨率。
def test_pcapng_multiple_interface_resolutions_and_timestamp_high_word(portal_module):
    first = data_test_pcapng()
    options = struct.pack("<HHB3x", 9, 1, 9) + b"\0" * 4
    interface = data_test_pcapng_block(1, struct.pack("<HHI", 1, 0, 65535) + options)
    packet = bytearray(first[48:])
    struct.pack_into("<III", packet, 8, 1, 1, 1)
    parsed = portal_module.parse_secops_pcap(first + interface + packet)
    assert parsed["packet_count"] == 2
    assert parsed["query_evidence"]["timestamp_resolution"] is None
    assert parsed["flows"][0]["first_seen"] == pytest.approx((2**32 + 1) / 10**9)
    assert parsed["flows"][0]["last_seen"] == 100.5


# LLM: Repeated SPBs carry no time evidence and cannot establish periodicity.
# 函数用途: 验证无时间戳包不会被补零并误报周期性通信。
def test_pcapng_simple_packets_do_not_invent_periodicity(portal_module):
    first = data_test_pcapng(simple=True)
    parsed = portal_module.parse_secops_pcap(first[:48] + first[48:] * 6)
    assert parsed["packet_count"] == 6
    assert parsed["flows"][0]["first_seen"] is None
    assert not any(a["kind"] == "periodic_traffic" for a in parsed["anomalies"])


# LLM: Option terminators, unique clock fields and exact SPB length are wire boundaries.
# 函数用途: 拒绝畸形选项结束、重复计时声明与包块多余数据。
@pytest.mark.parametrize("damage", ["bad_end_length", "after_end", "duplicate_resolution", "spb_extra"])
def test_pcapng_option_terminators_and_simple_length(portal_module, damage):
    if damage == "spb_extra":
        blob = data_test_pcapng(simple=True)
        body = blob[56:-4] + b"\0" * 4
        blob = blob[:48] + data_test_pcapng_block(3, body)
    else:
        options = {"bad_end_length": struct.pack("<HH", 0, 1),
                   "after_end": b"\0" * 4 + b"bad!",
                   "duplicate_resolution": struct.pack("<HHB3x", 9, 1, 6) * 2}[damage]
        blob = data_test_pcapng(options=options)
    with pytest.raises(portal_module.PortalError) as error:
        portal_module.parse_secops_pcap(blob)
    assert error.value.code == "MALFORMED_PCAP"


# LLM: Classic endian and micro/nanosecond variants remain positive shared-decoder controls.
# 函数用途: 验证四种经典magic不因PCAPNG扩展回退或误算时间。
@pytest.mark.parametrize("endian", ["<", ">"])
@pytest.mark.parametrize("resolution,magic", [(10**6, 0xA1B2C3D4), (10**9, 0xA1B23C4D)])
def test_classic_pcap_all_four_magic_values(portal_module, endian, resolution, magic):
    blob = bytearray(data_test_pcap(endian))
    struct.pack_into(endian + "I", blob, 0, magic)
    struct.pack_into(endian + "I", blob, 28, resolution // 2)
    parsed = portal_module.parse_secops_pcap(bytes(blob))
    assert parsed["packet_count"] == 1 and parsed["dns_queries"] == ["unit.invalid"]
    assert parsed["flows"][0]["first_seen"] == 100.5
    assert parsed["query_evidence"]["timestamp_resolution"] == resolution


# LLM: Mutated PCAPNG headers fail structurally rather than reading outside bounded bytes.
# 函数用途: 覆盖BOM、长度、接口、捕获尺寸和选项的畸形输入。
@pytest.mark.parametrize("damage", [
    "bom", "short_block", "unaligned_block", "trailer", "truncated", "missing_interface",
    "unknown_interface", "captured_gt_original", "snaplen", "short_packet", "bad_options",
])
def test_data_pcapng_rejects_malformed_blocks(portal_module, damage):
    blob = bytearray(data_test_pcapng())
    if damage == "bom":
        blob[8:12] = b"bad!"
    elif damage == "short_block":
        struct.pack_into("<I", blob, 4, 8)
    elif damage == "unaligned_block":
        struct.pack_into("<I", blob, 4, 29)
    elif damage == "trailer":
        struct.pack_into("<I", blob, len(blob) - 4, 12)
    elif damage == "truncated":
        blob = blob[:-1]
    elif damage == "missing_interface":
        blob = blob[:28] + blob[48:]
    elif damage == "unknown_interface":
        struct.pack_into("<I", blob, 56, 1)
    elif damage == "captured_gt_original":
        struct.pack_into("<I", blob, 72, 1)
    elif damage == "snaplen":
        struct.pack_into("<I", blob, 40, 14)
    elif damage == "short_packet":
        blob = blob[:48] + data_test_pcapng_block(6, b"\0" * 16)
    else:
        blob = data_test_pcapng(options=struct.pack("<HH", 9, 100))
    with pytest.raises(portal_module.PortalError) as error:
        portal_module.parse_secops_pcap(bytes(blob))
    assert error.value.status == 422 and error.value.code == "MALFORMED_PCAP"


# LLM: Real HTTP upload persists exact parser evidence and the uploaded-byte digest.
# 函数用途: 核验PCAPNG从回环上传到SQLite归档的来源和指纹一致。
def test_pcapng_http_persists_parsed_evidence(portal):
    raw = data_test_pcapng(">")
    status, result = api(portal, "/api/secops/investigations/pcap-inspect", {
        "pcap_base64": base64.b64encode(raw).decode(),
    })
    assert status == 200 and result["packet_count"] == 1
    assert result["sha256"] == hashlib.sha256(raw).hexdigest()
    row = portal._get("secops_pcap_investigations", result["investigation_id"])
    assert row["query_evidence"] == result["query_evidence"]


# LLM: Pure risk tests distinguish static evidence from runtime proof and forbid secret echo.
# 函数用途: 覆盖 JSON、安全 YAML、授权继承、BOLA、PII 字段及危险 YAML 拒绝。
def test_data_pure_risk(portal_module):
    import pytest
    spec = {"openapi": "3.0.0", "paths": {"/users/{user_id}": {"get": {
        "responses": {"200": {"content": {"application/json": {"schema": {
            "properties": {"ssn": {"type": "string"}, "api_key": {"example": "NEVER_ECHO_THIS"}}
        }}}}}}}}}
    result = portal_module.analyze_secops_api_risk({"openapi": spec, "traffic": [
        {"request": {"body": {"password": "NEVER_ECHO_THIS"}}, "response": {"phone": "NEVER_ECHO_THIS"}}
    ]})
    encoded = json.dumps(result)
    assert "NEVER_ECHO_THIS" not in encoded
    assert {"unauthenticated_route", "potential_bola", "sensitive_field"} <= {f["kind"] for f in result["findings"]}
    assert all(f["runtime_verified"] is False for f in result["findings"])
    yaml = "openapi: 3.0.0\npaths:\n  /users/{id}:\n    get:\n      security: []\n      responses: {}\n"
    assert portal_module.analyze_secops_api_risk({"openapi_yaml": yaml})["route_count"] == 1
    secured = {"openapi": "3.0.0", "security": [{"bearerAuth": []}], "paths": {"/me": {"get": {"responses": {}}}}}
    assert not any(f["kind"] == "unauthenticated_route" for f in portal_module.analyze_secops_api_risk({"openapi": secured})["findings"])
    for unsafe in ("openapi: &a 3.0.0\npaths: *a", "!!python/object/apply:os.system []", "openapi: 3.0.0\npaths: ["):
        with pytest.raises(portal_module.PortalError):
            portal_module.analyze_secops_api_risk({"openapi_yaml": unsafe})
    yaml = "openapi: 3.0.0\nsecurity:\n  - bearerAuth: []\npaths:\n  /users/{id}:\n    get:\n      parameters:\n        - name: id\n          in: path\n          required: true\n          schema:\n            type: string\n      responses: {}\n"
    result = portal_module.analyze_secops_api_risk({"openapi_yaml": yaml})
    assert result["route_count"] == 1
    assert not any(f["kind"] == "unauthenticated_route" for f in result["findings"])
    assert any(f["kind"] == "potential_bola" and f.get("field_name") == "id" for f in result["findings"])


# LLM: Parent HTTP and SQLite integration test; requires parent fixtures, not import-only stubs.
# 函数用途: 验证幂等写入、严重度、分页过滤、零结果证据、错误与 SQL 持久化。
def test_data_http_alert_query_persistence(portal):
    portal.register_connector("siem-unit")
    root = "/api/secops"
    payload = {"device_id": "siem-unit", "events": [
        {"event_id": "evt-a", "severity": "CRITICAL", "timestamp": 100, "message": "alpha"},
        {"event_id": "evt-b", "severity": "warning", "timestamp": 200, "message": "beta"},
    ]}
    status, result = api(portal, root + "/ingest/alert-stream", payload)
    assert status == 200, result
    assert result["inserted"] == 2
    assert api(portal, root + "/ingest/alert-stream", payload)[1]["duplicates"] == 2
    status, found = api(portal, root + "/connectors/siem-unit/query", {
        "query": "severity:critical", "start_time": 90, "end_time": 150, "limit": 1,
    })
    assert status == 200, found
    assert found["events"][0]["event_id"] == "evt-a"
    assert found["query_evidence"]["returned_count"] == 1
    status, empty = api(portal, root + "/connectors/siem-unit/query", {"query": "message:absent"})
    assert status == 200 and empty["query_evidence"]["source_has_no_data"] is None
    assert api(portal, root + "/alerts?severity=medium")[1]["events"][0]["event_id"] == "evt-b"
    assert api(portal, root + "/connectors/siem-unit/query", {"limit": -1})[0] == 422
    assert api(portal, root + "/connectors/siem-unit/query", {"query": "x OR SQL"})[0] == 422
    assert api(portal, root + "/ingest/alert-stream", {"device_id": "siem-unit", "events": [{"severity": "nonsense"}]})[0] == 422
    assert api(portal, root + "/alerts", {}, method="POST")[0] == 405
    with sqlite3.connect(portal.state_dir / "portal_state.db") as conn:
        assert conn.execute("select count(*) from secops_alert_events where owner_id=?", ("local/main",)).fetchone()[0] == 2


# LLM: Authenticated handler boundary test uses explicit trusted principals, not spoofable headers.
# 函数用途: 验证连接器和告警跨 owner 隔离，不信任 body 的 owner。
def test_data_cross_owner_isolation(portal):
    import pytest
    portal.register_connector("private-unit", owner_id="owner-a")
    one = {"owner_id": "owner-a", "is_admin": True}
    other = {"owner_id": "owner-b", "is_admin": True}
    root = "/api/secops"
    portal.handle_data("POST", root + "/ingest/alert-stream", {
        "device_id": "private-unit", "owner_id": "owner-b",
        "events": [{"event_id": "private", "timestamp": 1, "severity": "high"}],
    }, {}, one)
    assert portal.handle_data("GET", root + "/alerts", None, {}, other)["events"] == []
    with pytest.raises(Exception) as error:
        portal.handle_data("POST", root + "/connectors/private-unit/query", {}, {}, other)
    assert error.value.status == 404
    assert len(portal._all("secops_alert_events", "owner-a")) == 1
    assert portal._all("secops_alert_events", "owner-b") == []
    portal.handle_data("POST", root + "/api-risk/analyze", {"traffic": [{"password": "HIDDEN"}]}, {}, one)
    portal.handle_data("POST", root + "/investigations/pcap-inspect", {"pcap_base64": base64.b64encode(data_test_pcap()).decode()}, {}, one)
    for table in ("secops_api_risk_reports", "secops_pcap_investigations"):
        assert len(portal._all(table, "owner-a")) == 1
        assert portal._all(table, "owner-b") == []


# LLM: Real loopback HTTP connector check; parent supplies actual request and secret transport.
# 函数用途: 验证配置连接器的真实 HTTP 调用、查询证据和未配置失败。
def test_data_connector_loopback_http(portal):
    received = []
    # LLM: Loopback connector fixture returns controlled rows and records actual query transport.
    # 类用途: 模拟告警查询接收端，不代表真实SIEM设备。
    class ConnectorHandler(BaseHTTPRequestHandler):
        # LLM: Test response emits structured upstream events after reading the real request.
        # 函数用途: 接收查询并返回有界的测试响应。
        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            if received[-1].get("query") == "upstream-error":
                self.send_response(503)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            content = json.dumps({"events": [{"event_id": "upstream", "timestamp": 5, "severity": "high"}], "has_more": False}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        # LLM: Test-only log suppression has no transport effects.
        # 函数用途: 避免回环 HTTP 日志干扰测试输出。
        def log_message(self, *args):
            pass
    server = HTTPServer(("127.0.0.1", 0), ConnectorHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        portal.register_connector("remote-unit", kind="siem", config={
            "query_url": f"http://127.0.0.1:{server.server_port}/query", "allow_insecure_loopback": True,
        })
        status, result = api(portal, "/api/secops/connectors/remote-unit/query", {"query": "severity:high", "limit": 3})
        assert status == 200, result
        assert received[0]["limit"] == 3
        assert result["events"][0]["event_id"] == "upstream"
        assert result["query_evidence"]["source_kind"] == "external_http"
        assert api(portal, "/api/secops/connectors/remote-unit/query", {"query": "upstream-error"})[0] >= 500
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
    portal.register_connector("not-configured", kind="edr", config={})
    assert api(portal, "/api/secops/connectors/not-configured/query", {})[0] >= 400


# LLM: Analysis HTTP tests verify outputs and SQLite rows, not just function existence.
# 函数用途: 验证 PCAP 和风险分析入口、畸形输入错误及真实持久化。
def test_data_http_analysis_persistence(portal):
    status, pcap = api(portal, "/api/secops/investigations/pcap-inspect", {"pcap_base64": base64.b64encode(data_test_pcap()).decode()})
    assert status == 200 and pcap["packet_count"] == 1, pcap
    status, risk = api(portal, "/api/secops/api-risk/analyze", {"openapi_yaml": "openapi: 3.0.0\npaths:\n  /users/{id}:\n    get:\n      responses: {}\n"})
    assert status == 200 and risk["route_count"] == 1, risk
    with sqlite3.connect(portal.state_dir / "portal_state.db") as conn:
        for table in ("secops_pcap_investigations", "secops_api_risk_reports"):
            assert conn.execute("select count(*) from " + table + " where owner_id=?", ("local/main",)).fetchone()[0] == 1
    assert api(portal, "/api/secops/investigations/pcap-inspect", {"pcap_base64": "!!!"})[0] == 422


# LLM: Regression/test helper, confined to temporary state and controlled loopback fixtures.
# 函数用途: 验证接口行为、持久化与安全拒绝（test_traffic_auth_object_id_and_secret_fields_are_static_leads）。
def test_traffic_auth_object_id_and_secret_fields_are_static_leads(portal):
    status, result = api(portal, "/api/secops/api-risk/analyze", {"traffic": [{
        "request": {"url": "https://unit.example.test/users/123?api_key=DO_NOT_RETURN_VALUE", "headers": {}},
        "response": {"ssn": "DO_NOT_RETURN_VALUE", "bank_card": "DO_NOT_RETURN_VALUE"},
    }]})
    assert status == 200, result
    assert {"unauthenticated_route", "potential_bola", "sensitive_field"} <= {item["kind"] for item in result["findings"]}
    assert all(item["runtime_verified"] is False for item in result["findings"])
    assert "DO_NOT_RETURN_VALUE" not in json.dumps(result)


# LLM: Regression/test helper, confined to temporary state and controlled loopback fixtures.
# 函数用途: 验证接口行为、持久化与安全拒绝（test_saved_soar_playbook_is_persistent_and_cannot_replace_approval）。
def test_saved_soar_playbook_is_persistent_and_cannot_replace_approval(portal):
    action = {"action": "block_ip", "device_id": "edr-test", "params": {"ip": "127.0.0.2"}}
    portal.register_playbook("plan-one", [action])
    assert portal._get("secops_soar_playbooks", "plan-one")["actions"] == [action]
    body = dict(action, playbook_id="plan-one")
    assert api(portal, "/api/secops/soar/execute-action", body)[0] == 403
    with action_receiver() as (url, received):
        action_connector(portal, url)
        portal.approve_action("plan-approved", "block_ip", "edr-test", action["params"])
        status, result = api(portal, "/api/secops/soar/execute-action", dict(body, approval_id="plan-approved"))
        assert status == 200, result
        assert len(received) == 1 and result["playbook_id"] == "plan-one"
        assert api(portal, "/api/secops/soar/execute-action", dict(body, params={"ip": "different"}))[0] == 403
