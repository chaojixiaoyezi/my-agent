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
def _request(native_http, method: str, path: str, *, user: str = "business-alice", headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
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
    try:
        conn.request(method, path, headers=req_headers)
        response = conn.getresponse()
        content = response.read()
        lower_headers = {k.lower(): v for k, v in response.getheaders()}
        return response.status, lower_headers, content
    finally:
        conn.close()


# LLM: 驱动 Node.js 运行前端协议测试，确保 app.js 在真实 JS 引擎中零报错。
# 函数用途: 在独立的 Node 子进程中执行测试脚本并断言退出码为 0。
def _run_node(script: str, **fixtures) -> None:
    app_path = Path(__file__).resolve().parents[2] / "examples/business_space_ui/app.js"
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
    disk_path = Path(__file__).resolve().parents[2] / "examples/business_space_ui/index.html"
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
      data: {items: [], count: 0, status: 'ok', coverage: 'complete'},
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

  await surface.team.resumeAgent('ag_1');
  assert.equal(calls.at(-1).body.action, 'background_agents.resume');

  await surface.team.retireAgent('ag_1', 'drain');
  assert.equal(calls.at(-1).body.action, 'background_agents.retire');

  await surface.team.sendMessage('ag_1', '问候');
  assert.equal(calls.at(-1).body.action, 'background_agents.messages.send');

  await surface.team.listMessages({kind: 'agent', id: 'ag_1'});
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

  await surface.delivery.getPermissions({kind: 'agent', id: 'ag_1'});
  assert.equal(calls.at(-1).body.action, 'permissions.get');

  assert.equal(calls.length, 31, '31 个活体动作均已成功接线');
})().catch(e => { console.error(e); process.exitCode = 1; });
    """)


# LLM: 验证 HTML 模板中 6 面板全部存在且包含所需控制按钮。
# 函数用途: 验证 index.html 的 6 面板结构与 DOM 控件完整性。
def test_reference_surface_html_panels_and_buttons_contract():
    html_path = Path(__file__).resolve().parents[2] / "examples/business_space_ui/index.html"
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
