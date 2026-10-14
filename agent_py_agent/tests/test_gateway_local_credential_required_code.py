"""本机缺凭据固定为 LOCAL_CREDENTIAL_REQUIRED，错误分类不泄露目标存在性。

受管入口在业务读取之前统一鉴权；有效凭据仍进入原本人/其他 owner 与归档诊断路径。
退休开关不再提供回环例外，远程拒绝与缺凭据仍可区分。仅用临时根、随机端口，不读真实秘密。
"""
from __future__ import annotations

import http.client
import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.auth.manager import AuthManager
from agent_py_agent.agent.auth.middleware import (
    LOCAL_CREDENTIAL_REQUIRED,
    AuthMiddleware,
    require_trusted_source,
)
from agent_py_agent.agent.common.json_io import write_json_file_atomic
from agent_py_agent.agent.gateway_parts import http_service
from agent_py_agent.agent.gateway_parts.http_service import (
    GatewayHTTPServer,
    GatewayHTTPServerParams,
)
from agent_py_agent.agent.gateway_parts.local_client_token import ensure_local_client_credential
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root

LOCAL = "127.0.0.1"


# LLM: 原中间件始终要求宿主凭据；不再把退休字段传入产品构造器。
# 函数用途: 构造当前凭据门，非空凭据仅在宿主内存绑定。
def _middleware(credential: str = "") -> AuthMiddleware:
    middleware = AuthMiddleware(AuthManager(admin_user_id="admin", auth_enabled=True))
    if credential:
        middleware.set_local_client_credential(credential)
    return middleware


# 函数用途: 构造带数据根与 owner home 的假 Agent，推导出的数据根与 root 一致（G1 合同）。
def _agent_with_root(root):
    return SimpleNamespace(home_paths=SimpleNamespace(root=root, owner_home_dir=root / "owners" / "local" / "main"))


# 函数用途: 用随机端口发一次请求，返回 (状态码, JSON 或文本)。
def _request(port, method, path, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(method, path, body=b"{}" if method == "POST" else None, headers=headers or {})
        response = connection.getresponse()
        data = response.read()
        content_type = response.getheader("Content-Type", "")
        return response.status, json.loads(data) if content_type.startswith("application/json") else data.decode()
    finally:
        connection.close()


# --- 单元层：判定只读结构化事实 ---------------------------------------------


def test_needs_local_credential_only_for_enforced_loopback_without_credential():
    forced = _middleware(credential="right-token")
    # 这一格才成立。
    assert forced.needs_local_credential(LOCAL, {}) is True
    assert forced.needs_local_credential("::1", {}) is True
    # 带了凭据：不是缺凭据。
    assert forced.needs_local_credential(LOCAL, {"X-Gateway-Token": "right-token"}) is False
    # 远程来源：是越权，不是缺凭据。
    assert forced.needs_local_credential("192.0.2.10", {}) is False
    # 来源未知：强制档下按不可信处理，但不属于"回环"这一格。
    assert forced.needs_local_credential(None, {}) is False


def test_needs_local_credential_remains_true_without_a_migration_switch():
    # 默认构造也必须要求凭据，不能借不存在的迁移字段恢复信任。
    assert _middleware(credential="right-token").needs_local_credential(LOCAL, {}) is True


@pytest.mark.parametrize("peer,headers,expected_code", [
    (LOCAL, {}, True),
    (LOCAL, {"X-Gateway-Token": "wrong-token"}, True),
    (LOCAL, {"X-Gateway-Token": "right-token"}, False),
    ("192.0.2.10", {}, False),
    (None, {}, False),
])
def test_require_trusted_source_body_carries_code_only_in_the_enforced_loopback_cell(peer, headers, expected_code):
    middleware = _middleware(credential="right-token")
    replies = []

    def handler_for():
        return SimpleNamespace(_auth_middleware=middleware, headers=headers, client_address=(peer, 51234),
                               _send_json=lambda status, body: replies.append((status, body)))

    accepted = require_trusted_source(handler_for())
    if not accepted:
        assert expected_code is False
        return
    status, body = replies[-1]
    assert status == 403
    # 既有字段保持：状态码与 error 不变。
    assert body["error"] == "forbidden" and "message" in body
    assert ("error_code" in body) is expected_code
    if expected_code:
        assert body["error_code"] == LOCAL_CREDENTIAL_REQUIRED


def test_require_trusted_source_remote_peer_denial_has_no_local_code():
    # 远程无凭据是普通来源拒绝，不能冒充本机客户端缺凭据。
    middleware = _middleware()
    replies = []
    handler = SimpleNamespace(_auth_middleware=middleware, headers={}, client_address=("192.0.2.10", 51234),
                              _send_json=lambda status, body: replies.append((status, body)))
    assert require_trusted_source(handler) is True
    assert "error_code" not in replies[-1][1]


# --- HTTP 层：真实 handler + 随机端口 ---------------------------------------


# LLM: 强制档服务只用于本文件；不 patch 业务处理器——/ask、/control、/client/notices 的
#   require_trusted_source 必须真跑，否则测的就是替身而不是鉴权路径。
# 函数用途: 起一个强制档随机端口服务，并暴露本机凭据供带凭据请求使用。
@pytest.fixture
def enforced_listener(tmp_path):
    root = tmp_path / "data-root"
    credential = ensure_local_client_credential(root)
    server = GatewayHTTPServer(0, gateway_paths_from_root(tmp_path / "queue"),
                               params=GatewayHTTPServerParams(agent=_agent_with_root(root),
                                                              auth_middleware=_middleware(credential)))
    server.start()
    try:
        yield server, server.server.server_address[1], credential
    finally:
        server.stop()


# 函数用途: 用默认中间件起真实隔离服务，验证没有退休开关也必须鉴权。
@pytest.fixture
def default_listener(tmp_path):
    root = tmp_path / "data-root"
    server = GatewayHTTPServer(0, gateway_paths_from_root(tmp_path / "queue"),
                               params=GatewayHTTPServerParams(agent=_agent_with_root(root),
                                                              auth_middleware=_middleware()))
    server.start()
    try:
        yield server, server.server.server_address[1]
    finally:
        server.stop()


# 函数用途: 断言某个挂 require_trusted_source 的端点在无凭据时返回带码的 403，带凭据时不再带码。
def _assert_guarded_endpoint(port, method, path, credential):
    status, body = _request(port, method, path)
    assert status == 403, f"{path} 无凭据应被拒"
    assert body["error_code"] == LOCAL_CREDENTIAL_REQUIRED, f"{path} 应带本机凭据缺失码"
    assert body["error"] == "forbidden"
    # 带凭据后鉴权关放行：端点会继续走自己的业务校验（可能因缺业务参数返回 4xx），
    # 关键是它不再带"缺本机凭据"这个码——那证明拒绝来自业务而不是凭据档。
    with_cred_status, with_cred_body = _request(port, method, path, {"X-Gateway-Token": credential})
    assert with_cred_status != 403 or with_cred_body.get("error_code") != LOCAL_CREDENTIAL_REQUIRED, (
        f"{path} 带凭据后不该再判本机凭据缺失"
    )
    assert "LOCAL_CREDENTIAL_REQUIRED" not in json.dumps(with_cred_body)


# 抽三个挂 require_trusted_source 的入口覆盖：/ask（提交）、/control（系统命令）、/client/notices（会话读取）。
@pytest.mark.parametrize("method,path", [
    ("POST", "/ask"),
    ("POST", "/control"),
    ("POST", "/client/notices"),
])
def test_guarded_endpoints_carry_code_without_credential(enforced_listener, method, path):
    _server, port, credential = enforced_listener
    assert port != 8420
    _assert_guarded_endpoint(port, method, path, credential)


# 三个只读入口：无凭据统一在业务读取前拒绝，带凭据保留原业务分流。
@pytest.mark.parametrize("method,path,expected_status", [
    ("GET", "/progress/fixture-request", 403),
    ("GET", "/input-status/fixture-request", 403),
    ("GET", "/result/fixture-request", 403),
])
def test_self_judging_read_endpoints_carry_code_without_credential(
    enforced_listener, method, path, expected_status
):
    _server, port, credential = enforced_listener
    status, body = _request(port, method, path)
    assert status == expected_status
    assert body["error_code"] == LOCAL_CREDENTIAL_REQUIRED
    # 带了凭据就不带这个码：/progress 变成 200，另两个仍是"记录不存在"的干净 404。
    with_cred_status, with_cred_body = _request(port, method, path, {"X-Gateway-Token": credential})
    assert "error_code" not in with_cred_body, f"{path} 带凭据不该再带缺凭据码"
    if path.startswith("/progress/"):
        assert with_cred_status == 200
    else:
        assert with_cred_status == 404


# --- /progress 三态：记录不存在 404 / 无权限 403（g2bfix4 做法 A）----------------
#
# LLM: 普通客户端（有效凭据 + 非终端身份头）查 /progress：记录不存在回 404、记录属于别人回 403，
#   与 /result、/input-status 同口径；两种拒绝都干净（凭据有效，不带缺凭据码）。同一端口上
#   无凭据请求两种目标形状都在业务读取前统一403并带LOCAL_CREDENTIAL_REQUIRED。
# 函数用途: 造"别人的记录"，在强制档端口上钉住 /progress 的 404/403 分流与缺凭据码两侧。
def test_progress_missing_record_is_404_and_other_owner_is_403(tmp_path):
    root = tmp_path / "data-root"
    credential = ensure_local_client_credential(root)
    paths = gateway_paths_from_root(tmp_path / "queue")
    other_id = "fixture-progress-other"
    write_json_file_atomic(paths.inbox / f"{other_id}.json", {"user_id": "someone-else"})
    server = GatewayHTTPServer(0, paths,
                               params=GatewayHTTPServerParams(agent=_agent_with_root(root),
                                                              auth_middleware=_middleware(credential)))
    server.start()
    try:
        port = server.server.server_address[1]
        assert port != 8420
        user_headers = {"X-Gateway-Token": credential, "X-User-Id": "local-user", "X-Channel": "feishu"}
        # 记录不存在：干净 404（凭据有效，不带缺凭据码）。
        status, body = _request(port, "GET", "/progress/fixture-progress-missing", user_headers)
        assert (status, body["error"]) == (404, "not found")
        assert "error_code" not in body
        # 别人的记录：403。
        status, body = _request(port, "GET", f"/progress/{other_id}", user_headers)
        assert (status, body["error"]) == (403, "forbidden")
        assert "error_code" not in body
        # 本人的记录：放行（chunk 还不存在时是 200 空事件）。
        own_id = "fixture-progress-own"
        write_json_file_atomic(paths.inbox / f"{own_id}.json", {"user_id": "local-user"})
        status, body = _request(port, "GET", f"/progress/{own_id}", user_headers)
        assert status == 200
        assert body == {"request_id": own_id, "events": [], "next": 0}
        # 无凭据回环：缺失与别人的记录均在业务读取前403，不泄露存在性。
        status, body = _request(port, "GET", "/progress/fixture-progress-missing")
        assert (status, body.get("error_code")) == (403, LOCAL_CREDENTIAL_REQUIRED)
        status, body = _request(port, "GET", f"/progress/{other_id}")
        assert (status, body.get("error_code")) == (403, LOCAL_CREDENTIAL_REQUIRED)
    finally:
        server.stop()


def test_default_middleware_rejects_submit_and_reads_before_business(default_listener):
    _server, port = default_listener
    for method, path in [("POST", "/ask"), ("GET", "/progress/fixture-request"),
                         ("GET", "/input-status/fixture-request"), ("GET", "/result/fixture-request")]:
        status, body = _request(port, method, path)
        assert status == 403 and body["error"] == "forbidden"
        assert body["error_code"] == LOCAL_CREDENTIAL_REQUIRED
        assert "request_id" not in body and "result_load_error" not in body


def test_remote_peer_denial_never_carries_code(tmp_path):
    # 远程不可信来源经 require_trusted_source 被拒时不带这个码（它不是"没带凭据"，是真越权）。
    middleware = _middleware(credential="right-token")
    replies = []
    handler = SimpleNamespace(_auth_middleware=middleware, headers={}, client_address=("192.0.2.10", 5555),
                              _send_json=lambda status, body: replies.append((status, body)))
    assert require_trusted_source(handler) is True
    assert replies[-1][0] == 403
    assert "error_code" not in replies[-1][1]


# --- /result 目标记录异常：凭据前检不泄露正文或诊断 -------------------------
# LLM: 无凭据统一在业务读取前403；有凭据保留原管理员数据/损坏诊断，不能抹掉业务可用性断言。
# 函数用途: 在临时账中预置其他 owner 的记录与坏 JSON，返回准确 request_id。
def _seed_other_owner_and_corrupt(request_paths):
    other_id, corrupt_id = "fixture-other-owner", "fixture-corrupt"
    write_json_file_atomic(request_paths.inbox / f"{other_id}.json", {"user_id": "someone-else"})
    request_paths.processing.mkdir(parents=True, exist_ok=True)
    (request_paths.processing / f"{corrupt_id}.json").write_text("{not json", encoding="utf-8")
    return other_id, corrupt_id


# 函数用途: 造出"记录属于别人"与"记录损坏"两条 /result 403 形状，断言无凭据带码、带凭据不带码。
@pytest.mark.parametrize("shape", ["other_owner", "corrupt"])
def test_result_self_judged_forbidden_branches_carry_code(tmp_path, shape):
    root = tmp_path / "data-root"
    credential = ensure_local_client_credential(root)
    paths = gateway_paths_from_root(tmp_path / "queue")
    other_id, corrupt_id = _seed_other_owner_and_corrupt(paths)
    target = other_id if shape == "other_owner" else corrupt_id
    server = GatewayHTTPServer(0, paths,
                               params=GatewayHTTPServerParams(agent=_agent_with_root(root),
                                                              auth_middleware=_middleware(credential)))
    server.start()
    try:
        port = server.server.server_address[1]
        assert port != 8420
        status, body = _request(port, "GET", f"/result/{target}")
        assert status == 403, f"{shape} 应被拒，实际 {status} {body}"
        assert body["error_code"] == LOCAL_CREDENTIAL_REQUIRED, f"{shape} 的 403 应带缺凭据码"
        assert body["error"] == "forbidden"
        # 带凭据后：鉴权档不再判缺凭据，该请求放行到端点自身的业务判定（本条形状会拿到
        # 记录内容或损坏诊断），关键是不再出现"缺本机凭据"这个码。
        with_cred_status, with_cred_body = _request(port, "GET", f"/result/{target}",
                                                    {"X-Gateway-Token": credential})
        assert "LOCAL_CREDENTIAL_REQUIRED" not in json.dumps(with_cred_body), f"{shape} 带凭据不该带缺凭据码"
        assert with_cred_status != 403
    finally:
        server.stop()


# 函数用途: 默认中间件同样必须在读取别人或损坏记录之前拒绝，无目标细节。
@pytest.mark.parametrize("shape", ["other_owner", "corrupt"])
def test_default_middleware_rejects_queued_or_corrupt_result_without_details(tmp_path, shape):
    root = tmp_path / "data-root"
    paths = gateway_paths_from_root(tmp_path / "queue")
    other_id, corrupt_id = _seed_other_owner_and_corrupt(paths)
    target = other_id if shape == "other_owner" else corrupt_id
    server = GatewayHTTPServer(0, paths,
                               params=GatewayHTTPServerParams(agent=_agent_with_root(root),
                                                              auth_middleware=_middleware()))
    server.start()
    try:
        status, body = _request(server.server.server_address[1], "GET", f"/result/{target}")
        assert status == 403 and body["error_code"] == LOCAL_CREDENTIAL_REQUIRED
        assert body["error"] == "forbidden"
        assert "request_id" not in body and "result_load_error" not in body
    finally:
        server.stop()


# --- 归档终态 403（_send_archived_terminal_result）-------------------------


# LLM: _send_archived_terminal_result 的 _can_read_payload 分支要读到"归档终态文件存在、结构与
#   request id 都自洽、但 payload 里 user_id 不是本人"的形状。归档必须满足
#   read_gateway_terminal_envelope_report 的校验：schema_version=gateway_terminal_request.v1、
#   id 与文件名一致、terminal_response 非空字典且它的 id 也等于 request id；缺任一项都会先落到
#   load_error 分支（返回 "terminal result unavailable"），测不到本次要补的那一处 403。
# 函数用途: 造出"归档终态属于别人"的形状，验证 /result 的该 403 分支也带缺凭据码。
def test_result_archived_terminal_owned_by_someone_else_carries_code(tmp_path):
    root = tmp_path / "data-root"
    credential = ensure_local_client_credential(root)
    paths = gateway_paths_from_root(tmp_path / "queue")
    terminal_id = "fixture-archived-other"
    write_json_file_atomic(paths.terminal / f"{terminal_id}.json", {
        "schema_version": "gateway_terminal_request.v1",
        "id": terminal_id,
        "user_id": "someone-else",
        "terminal_response": {"id": terminal_id, "status": "done"},
    })
    server = GatewayHTTPServer(0, paths,
                               params=GatewayHTTPServerParams(agent=_agent_with_root(root),
                                                              auth_middleware=_middleware(credential)))
    server.start()
    try:
        port = server.server.server_address[1]
        assert port != 8420
        status, body = _request(port, "GET", f"/result/{terminal_id}")
        assert status == 403, f"归档终态属于别人应被拒，实际 {status} {body}"
        assert body["error_code"] == LOCAL_CREDENTIAL_REQUIRED
        with_cred_status, with_cred_body = _request(port, "GET", f"/result/{terminal_id}",
                                                    {"X-Gateway-Token": credential})
        assert "LOCAL_CREDENTIAL_REQUIRED" not in json.dumps(with_cred_body)
        assert with_cred_status != 403
    finally:
        server.stop()


# --- 归档读不出（load_error 分支）的 403（g2bfix1c）---------------------------


# LLM: 原归档业务仅在凭据已通过后执行，管理员仍拿500与原诊断；未认证者无法得知归档损坏。
# 函数用途: 在隔离数据根中放一条损坏归档，让真实HTTP验证入口拒绝和管理员诊断两侧。
def _seed_broken_archive(request_paths):
    broken_id = "fixture-broken-archive"
    request_paths.terminal.mkdir(parents=True, exist_ok=True)
    (request_paths.terminal / f"{broken_id}.json").write_text("{not valid json", encoding="utf-8")
    return broken_id


# 函数用途: 强制档 + 回环 + 无凭据请求损坏归档 → 403 且带缺凭据码；带凭据走管理员侧 500 + 诊断、不带码。
def test_result_broken_archive_carries_code_without_credential(tmp_path):
    root = tmp_path / "data-root"
    credential = ensure_local_client_credential(root)
    paths = gateway_paths_from_root(tmp_path / "queue")
    broken_id = _seed_broken_archive(paths)
    server = GatewayHTTPServer(0, paths,
                               params=GatewayHTTPServerParams(agent=_agent_with_root(root),
                                                              auth_middleware=_middleware(credential)))
    server.start()
    try:
        port = server.server.server_address[1]
        assert port != 8420
        status, body = _request(port, "GET", f"/result/{broken_id}")
        assert status == 403, f"未认证请求应在归档读取前被拒，实际 {status} {body}"
        assert body["error_code"] == LOCAL_CREDENTIAL_REQUIRED, "归档读不出的 403 应带缺凭据码"
        assert body["error"] == "forbidden"
        assert "request_id" not in body
        assert "result_load_error" not in body, "普通用户不该拿到诊断细节"

        # 带凭据：鉴权档不再判缺凭据，请求放行到管理员侧——500 + result_load_error，且不带缺凭据码。
        with_cred_status, with_cred_body = _request(port, "GET", f"/result/{broken_id}",
                                                    {"X-Gateway-Token": credential})
        assert with_cred_status == 500, f"管理员侧仍是诊断视图，实际 {with_cred_status} {with_cred_body}"
        assert with_cred_body["error"] == "terminal result unavailable"
        assert with_cred_body["result_load_error"], "管理员侧应保留诊断细节"
        assert "LOCAL_CREDENTIAL_REQUIRED" not in json.dumps(with_cred_body)
    finally:
        server.stop()


# 函数用途: 默认凭据门不因坏归档而打开管理员诊断，未认证访问统一拒绝。
def test_default_middleware_broken_archive_does_not_expose_diagnostic_view(tmp_path):
    root = tmp_path / "data-root"
    paths = gateway_paths_from_root(tmp_path / "queue")
    broken_id = _seed_broken_archive(paths)
    server = GatewayHTTPServer(0, paths,
                               params=GatewayHTTPServerParams(agent=_agent_with_root(root),
                                                              auth_middleware=_middleware()))
    server.start()
    try:
        status, body = _request(server.server.server_address[1], "GET", f"/result/{broken_id}")
        # 缺凭据时在读取归档前拒绝，不泄露坏档存在性或管理员诊断。
        assert status == 403 and body["error"] == "forbidden"
        assert body["error_code"] == LOCAL_CREDENTIAL_REQUIRED
        assert "request_id" not in body and "result_load_error" not in body

    finally:
        server.stop()
