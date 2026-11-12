# LLM: 验证同源认证工作台托管与6面板 live 动作接线；涵盖线网往返、安全头、CSRF 注入与 Node 协议测试。
# 模块用途: 端到端验证 GET /client/business/ui 与 app.js 的真实 HTTP 鉴权、CSP、CSRF 会话绑定及参考消费端接线。
from __future__ import annotations

import http.client
import json
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.business_host_ports import wire_business_host_ports
from agent_py_agent.agent.gateway_parts.business_ui_http import (
    generate_csrf_token,
    verify_csrf_token,
)
from agent_py_agent.agent.gateway_parts.http_service import (
    GatewayHTTPServer,
    GatewayHTTPServerParams,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.user_space.home_layout import home_paths


# LLM: 构造原生Gateway HTTP测试服务；严格使用临时主目录与端口0，确保单测隔离。
# 函数用途: pytest 夹具，启动并管理一个测试专用的 GatewayHTTPServer 实例。
@pytest.fixture
def native_http(tmp_path):
    root = tmp_path / "native-ui-home"
    base = SimpleNamespace(
        home_paths=home_paths(root),
        config=AgentConfig(business_spaces_enabled=True),
    )
    server = GatewayHTTPServer(
        0,
        gateway_paths_from_root(root / "gateway"),
        params=GatewayHTTPServerParams(agent=base),
    )
    server.start()
    try:
        yield server, base
    finally:
        server.stop()


# LLM: 发起真实 HTTP 线网往返调用，禁止手写假 JSON 字典 Mock。
# 函数用途: 向测试网关发送底层 HTTP 请求并返回状态码、响应头与字节正文。
def _request(
    native_http,
    method: str,
    path: str,
    *,
    user: str = "business-alice",
    headers: dict[str, str] | None = None,
    body: bytes | str | None = None,
) -> tuple[int, dict[str, str], bytes]:
    server, _ = native_http
    conn = http.client.HTTPConnection("127.0.0.1", server.server.server_address[1], timeout=10)
    req_headers = {
        "X-Gateway-Token": server._local_client_credential,
        "X-User-Id": user,
        "X-Channel": "feishu",
        "Host": f"127.0.0.1:{server.server.server_address[1]}",
    }
    if headers:
        for k, v in headers.items():
            if v is None:
                req_headers.pop(k, None)
            else:
                req_headers[k] = v
    if isinstance(body, str):
        body = body.encode("utf-8")
    try:
        conn.request(method, path, body=body, headers=req_headers)
        response = conn.getresponse()
        content = response.read()
        lower_headers = {k.lower(): v for k, v in response.getheaders()}
        return response.status, lower_headers, content
    finally:
        conn.close()


# LLM: 驱动 Node.js 运行前端协议测试，确保 app.js 在真实 JS 引擎中零报错。
# 函数用途: 在独立的 Node 子进程中执行测试脚本并断言退出码为 0。
def _run_node(script: str, **fixtures) -> None:
    app_path = Path(__file__).resolve().parents[1] / "static/business_space_ui/app.js"
    assert app_path.is_file(), "参考消费端脚本不存在"
    node = shutil.which("node")
    assert node, "需要 Node 环境运行前端参考接线验收"
    constants = "".join(f"const {name} = {json.dumps(value, ensure_ascii=False)};\n" for name, value in fixtures.items())
    result = subprocess.run(
        [node, "-e", f"const ui = require({json.dumps(str(app_path))});\n{constants}{script}"],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# LLM: 未认证请求或跨站请求必须被网关凭据门与同源校验干净阻断。
# 函数用途: 验证未携带有效凭据或跨站来源的 GET /client/business/ui 与 app.js 均返回 403。
def test_reference_surface_unauthenticated_requests_rejected(native_http):
    # 缺 X-Gateway-Token
    status, _, _ = _request(native_http, "GET", "/client/business/ui", headers={"X-Gateway-Token": None})
    assert status == 403

    status_js, _, _ = _request(native_http, "GET", "/client/business/ui/app.js", headers={"X-Gateway-Token": None})
    assert status_js == 403

    # 伪造跨站 Origin
    status_cross, _, _ = _request(native_http, "GET", "/client/business/ui", headers={"Origin": "https://malicious.example.com"})
    assert status_cross == 403

    # Sec-Fetch-Site: cross-site
    status_site, _, _ = _request(native_http, "GET", "/client/business/ui", headers={"Sec-Fetch-Site": "cross-site"})
    assert status_site == 403


# LLM: 认证请求返回正确安全响应头且动态注入 CSRF Token，同时保证磁盘源文件零篡改。
# 函数用途: 验证 GET /client/business/ui 的安全头集合、CSRF meta 标签注入及源文件防污染。
def test_reference_surface_authenticated_html_serving_and_csrf_injection(native_http):
    server, base = native_http
    status, headers, content = _request(native_http, "GET", "/client/business/ui", user="alice-owner")
    assert status == 200
    assert "text/html" in headers.get("content-type", "")
    assert headers.get("content-security-policy") == "default-src 'self'; style-src 'self' 'unsafe-inline'"
    assert headers.get("cache-control") == "no-store"
    assert headers.get("x-content-type-options") == "nosniff"

    html = content.decode("utf-8")
    match_my_agent = re.search(r'<meta name="my-agent-csrf" content="([^"]+)">', html)
    match_csrf_token = re.search(r'<meta name="csrf-token" content="([^"]+)">', html)
    assert match_my_agent is not None, "未注入 meta[name='my-agent-csrf']"
    assert match_csrf_token is not None, "未注入 meta[name='csrf-token']"

    token = match_my_agent.group(1)
    assert token.startswith("csrf-"), f"CSRF 令牌前缀不合法: {token}"
    assert token == match_csrf_token.group(1), "两个 CSRF meta 标签内容不一致"

    # 校验注入的 CSRF 令牌对当前 Owner 与用户有效
    owner_id = "providers/feishu/users/alice-owner"
    assert verify_csrf_token(server, token, owner_id, "alice-owner") is True
    # 对不同用户或不同 Owner 应判定无效
    assert verify_csrf_token(server, token, owner_id, "bob-other") is False
    assert verify_csrf_token(server, token, "other-owner", "alice-owner") is False

    # 验证磁盘上的静态源文件没有被修改（仍保持 content=""）
    disk_path = Path(__file__).resolve().parents[1] / "static/business_space_ui/index.html"
    with open(disk_path, encoding="utf-8") as file_handle:
        disk_content = file_handle.read()
    assert '<meta name="csrf-token" content="">' in disk_content
    assert '<meta name="my-agent-csrf" content="">' in disk_content


# LLM: 验证业务工作台脚本的认证托管、安全响应头以及工厂导出函数。
# 函数用途: 验证 GET /client/business/ui/app.js 经由网关托管的安全头与脚本完整性。
def test_reference_surface_authenticated_app_js_serving(native_http):
    status, headers, content = _request(native_http, "GET", "/client/business/ui/app.js")
    assert status == 200
    assert "application/javascript" in headers.get("content-type", "")
    assert headers.get("content-security-policy") == "default-src 'self'; style-src 'self' 'unsafe-inline'"
    assert headers.get("cache-control") == "no-store"
    assert headers.get("x-content-type-options") == "nosniff"

    js = content.decode("utf-8")
    assert "createReferenceSurface" in js
    assert "BusinessClient" in js


# LLM: 验证 CSRF Token 的密码学生成与恒定时间校验逻辑。
# 函数用途: 单元测试 generate_csrf_token 与 verify_csrf_token 的确定性与防伪能力。
def test_reference_surface_csrf_token_binding_and_verification(native_http):
    server, base = native_http
    owner_id = base.home_paths.owner_id
    token1 = generate_csrf_token(server, owner_id, "user-1")
    token2 = generate_csrf_token(server, owner_id, "user-2")

    assert token1 != token2
    assert verify_csrf_token(server, token1, owner_id, "user-1") is True
    assert verify_csrf_token(server, token1, owner_id, "user-2") is False
    assert verify_csrf_token(server, token2, owner_id, "user-2") is True
    assert verify_csrf_token(server, "tampered-token", owner_id, "user-1") is False
    assert verify_csrf_token(server, "", owner_id, "user-1") is False


# LLM: 使用 Node 运行环境测试 6 个面板的完整动作绑定与 CSRF 头传递。
# 函数用途: 验证 createReferenceSurface 导出的 6 个面板均调用预期的后端协议与动作名。
def test_reference_surface_6_panel_node_wire_actions():
    _run_node(r"""
const assert = require('node:assert/strict');
(async () => {
  const calls = [];
  const fakeFetcher = async (path, options) => {
    const parsed = options.body ? JSON.parse(options.body) : null;
    calls.push({path, options, body: parsed});
    const envelope = {
      request_id: parsed?.request_id || 'test-req',
      operation_id: null,
      state: 'completed',
      data: {items: [], count: 0, status: 'ok', coverage: 'complete', agent: {identity_revision: 5}},
      error: null,
      allowed_actions: [],
      warnings: [],
      effect_outcome: 'committed'
    };
    return {
      ok: true,
      status: 200,
      json: async () => envelope,
      blob: async () => new Blob(['dummy-content'], {type: 'text/plain'})
    };
  };

  const surface = ui.createReferenceSurface({
    fetcher: fakeFetcher,
    csrf: () => 'test-csrf-binding'
  });

  assert.ok(surface.controller, 'Panel 1 controller 缺失');
  assert.ok(surface.materials, 'Panel 2 materials 缺失');
  assert.ok(surface.packages, 'Panel 3 packages 缺失');
  assert.ok(surface.team, 'Panel 4 team 缺失');
  assert.ok(surface.executions, 'Panel 5 executions 缺失');
  assert.ok(surface.delivery, 'Panel 6 delivery 缺失');

  // 1. Controller & task input
  await surface.controller.getController();
  assert.equal(calls.at(-1).body.action, 'controllers.get');
  assert.equal(calls.at(-1).options.headers['X-CSRF-Token'], 'test-csrf-binding');

  await surface.controller.submitTask('sp_1', '提交任务指令');
  assert.equal(calls.at(-1).body.action, 'tasks.submit');
  assert.equal(calls.at(-1).body.params.space_id, 'sp_1');
  assert.ok(calls.at(-1).body.idempotency_key, 'mutating 动作须携带幂等键');

  await surface.controller.assignTask('ag_1', 'sp_1', '指派任务指令');
  assert.equal(calls.at(-1).body.action, 'tasks.assign');

  await surface.controller.resolveInterface('bind_1', 'sp_1');
  assert.equal(calls.at(-1).body.action, 'interfaces.resolve');

  // 2. Business material
  await surface.materials.listAssets('sp_1');
  assert.equal(calls.at(-1).body.action, 'assets.list');

  await surface.materials.getAsset('sp_1', 'ast_1');
  assert.equal(calls.at(-1).body.action, 'assets.get');

  await surface.materials.registerAsset('sp_1', '材料名', 'document', 'art_1');
  assert.equal(calls.at(-1).body.action, 'assets.register');

  await surface.materials.listVersions('sp_1', 'ast_1');
  assert.equal(calls.at(-1).body.action, 'assets.versions.list');

  // 3. Packages & workflows
  await surface.packages.catalogPackages();
  assert.equal(calls.at(-1).body.action, 'spaces.packages.catalog');

  await surface.packages.listPackages('sp_1');
  assert.equal(calls.at(-1).body.action, 'spaces.packages.list');

  await surface.packages.previewPackages('sp_1', []);
  assert.equal(calls.at(-1).body.action, 'spaces.packages.preview');

  await surface.packages.listWorkflows();
  assert.equal(calls.at(-1).body.action, 'workflows.list');

  await surface.packages.getWorkflow('wf_1');
  assert.equal(calls.at(-1).body.action, 'workflows.get');

  // 4. Background team
  await surface.team.listAgents();
  assert.equal(calls.at(-1).body.action, 'background_agents.list');

  await surface.team.getAgent('ag_1');
  assert.equal(calls.at(-1).body.action, 'background_agents.get');

  await surface.team.suspendAgent('ag_1');
  assert.equal(calls.at(-1).body.action, 'background_agents.suspend');
  assert.equal(calls.at(-1).body.expected_revision, 5);

  await surface.team.resumeAgent('ag_1');
  assert.equal(calls.at(-1).body.action, 'background_agents.resume');
  assert.equal(calls.at(-1).body.expected_revision, 5);

  await surface.team.retireAgent('ag_1', 'drain');
  assert.equal(calls.at(-1).body.action, 'background_agents.retire');
  assert.equal(calls.at(-1).body.expected_revision, 5);

  await surface.team.sendMessage('ag_1', '问候');
  assert.equal(calls.at(-1).body.action, 'background_agents.messages.send');

  await surface.team.listMessages({kind: 'background_main', id: 'ag_1'});
  assert.equal(calls.at(-1).body.action, 'messages.list');

  // 5. Execution records
  await surface.executions.listExecutions();
  assert.equal(calls.at(-1).body.action, 'executions.list');

  await surface.executions.listToolCalls();
  assert.equal(calls.at(-1).body.action, 'tool_calls.list');

  await surface.executions.getToolCall('op_1');
  assert.equal(calls.at(-1).body.action, 'tool_calls.get');

  await surface.executions.readLogs('log_1');
  assert.equal(calls.at(-1).body.action, 'logs.read');

  await surface.executions.listEvents();
  assert.equal(calls.at(-1).body.action, 'events.list');

  // 6. Artifacts, review & release outcomes
  await surface.delivery.listArtifacts();
  assert.equal(calls.at(-1).body.action, 'artifacts.list');

  await surface.delivery.getChanges('task_1');
  assert.equal(calls.at(-1).body.action, 'changes.get');

  await surface.delivery.listApprovals();
  assert.equal(calls.at(-1).body.action, 'approvals.list');

  await surface.delivery.decideApproval('app_1', 'approved', 'ticket_1');
  assert.equal(calls.at(-1).body.action, 'approvals.decide');

  await surface.delivery.getPolicies('sp_1');
  assert.equal(calls.at(-1).body.action, 'policies.get');

  await surface.delivery.getPermissions({kind: 'background_main', id: 'ag_1'});
  assert.equal(calls.at(-1).body.action, 'permissions.get');

  assert.equal(calls.length, 34, '34 个活体动作均已成功接线');
})().catch(e => { console.error(e); process.exitCode = 1; });
    """)


# LLM: 验证 HTML 模板中 6 面板全部存在且包含所需控制按钮。
# 函数用途: 验证 index.html 的 6 面板结构与 DOM 控件完整性。
def test_reference_surface_html_panels_and_buttons_contract():
    html_path = Path(__file__).resolve().parents[1] / "static/business_space_ui/index.html"
    with open(html_path, encoding="utf-8") as file_handle:
        html = file_handle.read()
    for panel in ("controller", "materials", "packages", "team", "executions", "delivery"):
        assert f'data-panel="{panel}"' in html, f"缺少面板: {panel}"
    for button_id in (
        "controller-get", "submit", "task-assign", "interface-resolve",
        "asset-get", "asset-versions", "asset-register", "upload",
        "packages-catalog", "packages-preview", "workflow-run",
        "team-get", "team-suspend", "team-resume", "team-retire", "team-send", "team-messages",
        "tool-calls-list", "tool-call-get", "events-list", "read-log",
        "changes-get", "policies-get", "permissions-get", "approval-decide",
    ):
        assert f'id="{button_id}"' in html, f"缺少关键交互控件: {button_id}"
    assert 'id="permission-target-kind"' in html, "缺少权限目标类型选择器"
    assert '<option value="drain">' in html, "缺少 drain 选项"
    assert '<option value="cancel_tasks">' in html, "缺少 cancel_tasks 选项"


# LLM: 验证浏览器端请求（携带 Origin / Sec-Fetch-Site / X-CSRF-Token）严格实施 CSRF 校验，非浏览器请求（CLI/SDK）完全放行。
# 函数用途: 覆盖测试 POST /client/business 与 /client/business/artifacts 对 Token 的拒绝与放行行为。
def test_browser_request_token_enforcement_and_non_browser_bypass(native_http):
    server, base = native_http
    port = server.server.server_address[1]
    origin = f"http://127.0.0.1:{port}"

    # 从 GET /client/business/ui 动态提取真实注入的 CSRF Token
    ui_status, _, ui_content = _request(native_http, "GET", "/client/business/ui")
    assert ui_status == 200
    csrf_match = re.search(r'<meta name="my-agent-csrf" content="([^"]+)">', ui_content.decode("utf-8"))
    assert csrf_match is not None, "GET /client/business/ui 必须注入 my-agent-csrf meta"
    valid_csrf = csrf_match.group(1)

    post_body = json.dumps({
        "action": "system.capabilities",
        "request_id": "req-test-token-001",
        "params": {},
    })

    # 1. 纯 SDK/CLI 请求（无 Origin, 无 Sec-Fetch-Site, 无 X-CSRF-Token） -> 200 放行
    status, _, content = _request(
        native_http,
        "POST",
        "/client/business",
        headers={"Content-Type": "application/json"},
        body=post_body,
    )
    assert status == 200
    res = json.loads(content)
    assert res.get("state") == "completed"
    assert res.get("error") is None

    # 2. 浏览器请求（带 Origin），缺少 X-CSRF-Token -> 403 CAPABILITY_DENIED
    status, _, content = _request(
        native_http,
        "POST",
        "/client/business",
        headers={"Origin": origin, "Content-Type": "application/json"},
        body=post_body,
    )
    assert status == 403
    res = json.loads(content)
    assert res.get("error", {}).get("code") == "CAPABILITY_DENIED"

    # 3. 浏览器请求，携带非法 X-CSRF-Token -> 403 CAPABILITY_DENIED
    status, _, content = _request(
        native_http,
        "POST",
        "/client/business",
        headers={"Origin": origin, "X-CSRF-Token": "invalid-token", "Content-Type": "application/json"},
        body=post_body,
    )
    assert status == 403
    res = json.loads(content)
    assert res.get("error", {}).get("code") == "CAPABILITY_DENIED"

    # 4. 浏览器请求，Sec-Fetch-Site: cross-site -> 403 CAPABILITY_DENIED
    status, _, content = _request(
        native_http,
        "POST",
        "/client/business",
        headers={
            "Origin": origin,
            "Sec-Fetch-Site": "cross-site",
            "X-CSRF-Token": valid_csrf,
            "Content-Type": "application/json",
        },
        body=post_body,
    )
    assert status == 403
    res = json.loads(content)
    assert res.get("error", {}).get("code") == "CAPABILITY_DENIED"

    # 5. 浏览器请求，携带合法 X-CSRF-Token 与同源 Origin -> 200 成功
    status, _, content = _request(
        native_http,
        "POST",
        "/client/business",
        headers={"Origin": origin, "X-CSRF-Token": valid_csrf, "Content-Type": "application/json"},
        body=post_body,
    )
    assert status == 200
    res = json.loads(content)
    assert res.get("state") == "completed"
    assert res.get("error") is None

    # 6. Artifact 上传接口对 CSRF 的校验
    # 先创建测试空间以满足 upload_artifact 对 space_id 的合法性约束
    sp_body = json.dumps({
        "action": "spaces.create",
        "request_id": "req-sp-upload-001",
        "idempotency_key": "idemp-space-create-upload-token",
        "params": {"name": "Upload Token Space"},
    })
    _, _, sp_content = _request(
        native_http,
        "POST",
        "/client/business",
        headers={"Origin": origin, "X-CSRF-Token": valid_csrf, "Content-Type": "application/json"},
        body=sp_body,
    )
    space_id = json.loads(sp_content)["data"]["space_id"]

    boundary = "----TestBoundaryToken123"
    def _make_upload_body(key: str) -> bytes:
        return (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="space_id"\r\n\r\n'
            f"{space_id}\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="idempotency_key"\r\n\r\n'
            f"{key}\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="test.txt"\r\n'
            f"Content-Type: text/plain\r\n\r\n"
            f"Hello artifact content\r\n"
            f"--{boundary}--\r\n"
        ).encode()

    file_bytes_1 = _make_upload_body("artifact-upload-idempotency-key-01")
    file_bytes_2 = _make_upload_body("artifact-upload-idempotency-key-02")
    file_bytes_3 = _make_upload_body("artifact-upload-idempotency-key-03")

    # 6a. 浏览器请求（带 Origin），无 CSRF Token -> 403
    status, _, content = _request(
        native_http,
        "POST",
        "/client/business/artifacts",
        headers={
            "Origin": origin,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(file_bytes_1)),
        },
        body=file_bytes_1,
    )
    assert status == 403
    res = json.loads(content)
    assert res.get("error", {}).get("code") == "CAPABILITY_DENIED"

    # 6b. 浏览器请求，合法 CSRF Token -> 201 成功
    status, _, content = _request(
        native_http,
        "POST",
        "/client/business/artifacts",
        headers={
            "Origin": origin,
            "X-CSRF-Token": valid_csrf,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(file_bytes_2)),
        },
        body=file_bytes_2,
    )
    assert status == 201
    res = json.loads(content)
    assert res.get("available") is True

    # 6c. 非浏览器请求（无 Origin, 无 Token） -> 201 成功
    status, _, content = _request(
        native_http,
        "POST",
        "/client/business/artifacts",
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(file_bytes_3)),
        },
        body=file_bytes_3,
    )
    assert status == 201


# LLM: 验证参考消费端 app.js 与真实 Gateway HTTP 服务进行在线 Schema 对齐往返测试。
# 函数用途: 使用 Node.js 驱动 app.js 真实调用 Gateway，验证 suspendAgent/resumeAgent/retireAgent/messages.list/permissions.get 零 INVALID_SCHEMA 错误。
def test_reference_surface_aligned_schemas_real_http_roundtrip(tmp_path, monkeypatch):
    home = tmp_path / "node-roundtrip-home"
    monkeypatch.setenv("MY_AGENT_HOME", str(home))
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            enable_tools=True,
            business_spaces_enabled=True,
            memory_path="memory.jsonl",
            my_agent_home=str(home),
        ),
        tmp_path / "workspace",
    )
    wire_business_host_ports(agent)
    server = GatewayHTTPServer(
        0,
        gateway_paths_from_root(agent.home_paths.root / "gateway"),
        params=GatewayHTTPServerParams(agent=agent),
    )
    server.start()
    try:
        port = server.server.server_address[1]
        gateway_url = f"http://127.0.0.1:{port}"
        app_path = Path(__file__).resolve().parents[1] / "static/business_space_ui/app.js"
        node = shutil.which("node")
        assert node, "需要 Node 环境运行参考消费端真实线缆往返验收"

        script = f"""
const assert = require('node:assert/strict');
const app = require({json.dumps(str(app_path))});

(async () => {{
  const authHeaders = {{
    'X-Gateway-Token': {json.dumps(server._local_client_credential)},
    'X-User-Id': 'business-alice',
    'X-Channel': 'feishu',
    'Host': '127.0.0.1:{port}'
  }};

  // 1. 从 GET /client/business/ui 获取 CSRF Token
  const uiRes = await fetch('{gateway_url}/client/business/ui', {{
    headers: authHeaders
  }});
  assert.equal(uiRes.status, 200);
  const html = await uiRes.text();
  const csrfMatch = html.match(/<meta name="my-agent-csrf" content="([^"]+)">/);
  assert.ok(csrfMatch, '必须能从 /client/business/ui 获取 CSRF Token');
  const csrfToken = csrfMatch[1];

  const liveFetcher = (path, options) => fetch('{gateway_url}' + path, {{
    ...options,
    headers: {{
      ...options.headers,
      ...authHeaders,
      'Origin': '{gateway_url}',
      'X-CSRF-Token': csrfToken
    }}
  }});

  const client = new app.BusinessClient(liveFetcher, () => csrfToken);
  const surface = app.createReferenceSurface({{
    client: client,
    fetcher: liveFetcher,
    csrf: () => csrfToken
  }});

  // 2. 创建业务空间
  const spaceRes = await client.call('spaces.create', {{ name: 'Roundtrip Space' }}, {{
    mutating: true,
    idempotencyKey: 'idemp-space-create-roundtrip-01'
  }});
  assert.equal(spaceRes.state, 'completed', 'spaces.create 失败: ' + JSON.stringify(spaceRes.error));
  assert.equal(spaceRes.error, null);
  const spaceId = spaceRes.data.space_id;
  assert.ok(spaceId);

  // 3. 创建后台 Agent
  const agentRes = await client.call('background_agents.create', {{
    space_id: spaceId,
    name: 'Agent 1'
  }}, {{
    mutating: true,
    idempotencyKey: 'idemp-agent-create-roundtrip-01'
  }});
  assert.equal(agentRes.state, 'completed', 'background_agents.create 失败: ' + JSON.stringify(agentRes.error));
  assert.equal(agentRes.error, null);
  const agentId = agentRes.data.agent.agent_id;
  assert.ok(agentId);

  // 4. surface.team.suspendAgent (自动拉取 revision 并传递 CAS)
  const suspendRes = await surface.team.suspendAgent(agentId);
  assert.equal(suspendRes.state, 'completed', 'suspendAgent 失败: ' + JSON.stringify(suspendRes.error));
  assert.equal(suspendRes.error, null);
  assert.equal(suspendRes.data.agent.identity_state, 'suspended');

  // 5. surface.team.resumeAgent (自动拉取 revision 并传递 CAS)
  const resumeRes = await surface.team.resumeAgent(agentId);
  assert.equal(resumeRes.state, 'completed', 'resumeAgent 失败: ' + JSON.stringify(resumeRes.error));
  assert.equal(resumeRes.error, null);
  assert.equal(resumeRes.data.agent.identity_state, 'available');

  // 6. surface.team.retireAgent (自动拉取 revision 并传递 CAS，disposition: drain)
  const retireRes = await surface.team.retireAgent(agentId, 'drain');
  assert.equal(retireRes.state, 'completed', 'retireAgent 失败: ' + JSON.stringify(retireRes.error));
  assert.equal(retireRes.error, null);
  assert.equal(retireRes.data.agent.identity_state, 'retired');

  // 7. surface.team.listMessages (对齐后的 target: {{kind: 'background_main', id: agentId}})
  const messagesRes = await surface.team.listMessages({{ kind: 'background_main', id: agentId }});
  assert.equal(messagesRes.state, 'completed', 'listMessages 失败: ' + JSON.stringify(messagesRes.error));
  assert.equal(messagesRes.error, null);

  // 8. surface.delivery.getPermissions (对齐后的 target: {{kind: 'background_main', id: agentId}})
  const permRes = await surface.delivery.getPermissions({{ kind: 'background_main', id: agentId }});
  assert.equal(permRes.state, 'completed', 'getPermissions 失败: ' + JSON.stringify(permRes.error));
  assert.equal(permRes.error, null);
}})().catch(err => {{
  console.error(err);
  process.exit(1);
}});
"""
        result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        server.stop()
