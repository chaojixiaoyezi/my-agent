"""G2b 服务端结构化拒绝码 LOCAL_CREDENTIAL_REQUIRED（g2bfix1）。

背景与判据（见 docs/design/GATEWAY_LOCAL_TRUST.md 1.2 节 9b 建议）：
- 强制打开 gateway_require_local_credential 后，回环请求没带凭据会被拒，但客户端光看 403
  分不清"本机客户端没换代码/没带凭据"（重启即可）和"远程来源真的越权"。
- 本文件钉住：只有"强制档 + 回环 + 没带凭据"这一格带 error_code=LOCAL_CREDENTIAL_REQUIRED；
  带了凭据放行；开关关着行为完全不变（不带码、不拦）。
- 覆盖两类入口：挂 require_trusted_source 的端点，以及 /progress、/input-status、/result
  这三个"先降匿名、再由端点自己判 403/404"的读端点。

只用临时数据根、随机端口和假处理器；不读真实 secrets、不连真实 Gateway。
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


# 函数用途: 构造鉴权中间件；enforced 决定强制档位，credential 非空时绑定本机凭据。
def _middleware(enforced: bool, credential: str = "") -> AuthMiddleware:
    middleware = AuthMiddleware(AuthManager(admin_user_id="admin", auth_enabled=True),
                                require_local_credential=enforced)
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
    forced = _middleware(True, credential="right-token")
    # 这一格才成立。
    assert forced.needs_local_credential(LOCAL, {}) is True
    assert forced.needs_local_credential("::1", {}) is True
    # 带了凭据：不是缺凭据。
    assert forced.needs_local_credential(LOCAL, {"X-Gateway-Token": "right-token"}) is False
    # 远程来源：是越权，不是缺凭据。
    assert forced.needs_local_credential("192.0.2.10", {}) is False
    # 来源未知：强制档下按不可信处理，但不属于"回环"这一格。
    assert forced.needs_local_credential(None, {}) is False


def test_needs_local_credential_is_false_when_switch_off():
    # 迁移档（开关关着）行为不变：没有这个码。
    assert _middleware(False, credential="right-token").needs_local_credential(LOCAL, {}) is False


@pytest.mark.parametrize("peer,headers,expected_code", [
    (LOCAL, {}, True),
    (LOCAL, {"X-Gateway-Token": "wrong-token"}, True),
    (LOCAL, {"X-Gateway-Token": "right-token"}, False),
    ("192.0.2.10", {}, False),
    (None, {}, False),
])
def test_require_trusted_source_body_carries_code_only_in_the_enforced_loopback_cell(peer, headers, expected_code):
    middleware = _middleware(True, credential="right-token")
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


def test_require_trusted_source_switch_off_never_carries_code():
    # 开关关着：回环本来就放行，拒绝路径上也永远不带这个码。
    middleware = _middleware(False)
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
                                                              auth_middleware=_middleware(True, credential),
                                                              require_local_credential=True))
    server.start()
    try:
        yield server, server.server.server_address[1], credential
    finally:
        server.stop()


# 函数用途: 起一个开关关着的随机端口服务，用于证明迁移档行为不变。
@pytest.fixture
def migration_listener(tmp_path):
    root = tmp_path / "data-root"
    server = GatewayHTTPServer(0, gateway_paths_from_root(tmp_path / "queue"),
                               params=GatewayHTTPServerParams(agent=_agent_with_root(root),
                                                              auth_middleware=_middleware(False),
                                                              require_local_credential=False))
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


# 三个自判读端点：降匿名后由端点自己判 403/404，同样要能分辨"是没带凭据"。
@pytest.mark.parametrize("method,path,expected_status", [
    ("GET", "/progress/fixture-request", 403),
    ("GET", "/input-status/fixture-request", 404),
    ("GET", "/result/fixture-request", 404),
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


def test_guarded_endpoint_and_read_endpoint_switch_off_have_no_code(migration_listener):
    # 迁移档（开关关着）行为完全不变：回环放行（端点继续走自己的业务校验），读端点拒绝时也不带码。
    _server, port = migration_listener
    ask_status, ask_body = _request(port, "POST", "/ask")
    assert ask_status != 403, "开关关着时回环提交不该被鉴权拒绝"
    assert "LOCAL_CREDENTIAL_REQUIRED" not in json.dumps(ask_body)
    for path in ("/progress/fixture-request", "/input-status/fixture-request", "/result/fixture-request"):
        status, body = _request(port, "GET", path)
        assert "error_code" not in body, f"开关关着时 {path} 不该带缺凭据码"
        assert status in {200, 403, 404}


def test_remote_peer_denial_never_carries_code(tmp_path):
    # 远程不可信来源经 require_trusted_source 被拒时不带这个码（它不是"没带凭据"，是真越权）。
    middleware = _middleware(True, credential="right-token")
    replies = []
    handler = SimpleNamespace(_auth_middleware=middleware, headers={}, client_address=("192.0.2.10", 5555),
                              _send_json=lambda status, body: replies.append((status, body)))
    assert require_trusted_source(handler) is True
    assert replies[-1][0] == 403
    assert "error_code" not in replies[-1][1]


# --- /result 自己判的两处 403：记录属于别人、记录损坏（g2bfix1b） ---------------
#
# LLM: 这两处原先手写裸 403，和同一端点"记录不存在"分支（走 _denial_body）口径不一致：
#   没带凭据的旧客户端在"记录坏掉/是别人的"时仍只能看到裸 403，分不清是缺凭据还是真越权。
#   用例把两种形状都造出来（真实 handler + 随机端口），钉住带码/不带码两侧。
#   注意：降匿名后身份是 anonymous，记录里 user_id 写别的值才会落到"不是本人"这一支。
# 函数用途: 在隔离数据根里预置一条属于别人的队列记录和一条损坏记录，返回它们的 request_id。
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
                                                              auth_middleware=_middleware(True, credential),
                                                              require_local_credential=True))
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


# 函数用途: 开关关着时，两条 /result 自判 403 形状都不带缺凭据码（迁移档行为不变）。
@pytest.mark.parametrize("shape", ["other_owner", "corrupt"])
def test_result_self_judged_forbidden_branches_have_no_code_when_switch_off(tmp_path, shape):
    root = tmp_path / "data-root"
    paths = gateway_paths_from_root(tmp_path / "queue")
    other_id, corrupt_id = _seed_other_owner_and_corrupt(paths)
    target = other_id if shape == "other_owner" else corrupt_id
    server = GatewayHTTPServer(0, paths,
                               params=GatewayHTTPServerParams(agent=_agent_with_root(root),
                                                              auth_middleware=_middleware(False),
                                                              require_local_credential=False))
    server.start()
    try:
        status, body = _request(server.server.server_address[1], "GET", f"/result/{target}")
        assert "error_code" not in body, f"开关关着时 {shape} 不该带缺凭据码"
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
                                                              auth_middleware=_middleware(True, credential),
                                                              require_local_credential=True))
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


# LLM: _send_archived_terminal_result 还有一处自判拒绝在"降匿名读路径"上：归档文件存在但
#   read_gateway_terminal_envelope_report 报 load_error 时，普通用户拿 403、管理员拿 500 + 诊断。
#   403 那一侧原先手写裸 _send_json(status, body)，与同端点的其它拒绝口径不一致——客户端在
#   "归档坏掉"时仍只能靠状态码猜。本用例造一条结构损坏的归档文件（读不出即 load_error），
#   钉住三格：匿名带码、带凭据走管理员侧 500 + result_load_error、开关关着不带码。
# 函数用途: 在隔离数据根里放一条结构损坏的归档终态文件，返回它的 request_id。
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
                                                              auth_middleware=_middleware(True, credential),
                                                              require_local_credential=True))
    server.start()
    try:
        port = server.server.server_address[1]
        assert port != 8420
        status, body = _request(port, "GET", f"/result/{broken_id}")
        assert status == 403, f"匿名请求损坏归档应被拒，实际 {status} {body}"
        assert body["error_code"] == LOCAL_CREDENTIAL_REQUIRED, "归档读不出的 403 应带缺凭据码"
        assert body["error"] == "terminal result unavailable"
        assert body["request_id"] == broken_id
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


# 函数用途: 开关关着时，回环无凭据仍是可信来源，损坏归档走管理员诊断视图（500）且不带缺凭据码。
def test_result_broken_archive_has_no_code_when_switch_off(tmp_path):
    root = tmp_path / "data-root"
    paths = gateway_paths_from_root(tmp_path / "queue")
    broken_id = _seed_broken_archive(paths)
    server = GatewayHTTPServer(0, paths,
                               params=GatewayHTTPServerParams(agent=_agent_with_root(root),
                                                              auth_middleware=_middleware(False),
                                                              require_local_credential=False))
    server.start()
    try:
        status, body = _request(server.server.server_address[1], "GET", f"/result/{broken_id}")
        # 迁移档（开关关着）回环自带信任，降匿名不发生，所以走的是管理员侧诊断视图；
        # 关键是这条响应不带缺凭据码——迁移档行为不变。
        assert status == 500, f"迁移档回环是可信来源，损坏归档走诊断视图，实际 {status} {body}"
        assert body["error"] == "terminal result unavailable"
        assert "error_code" not in body, f"开关关着时不该带缺凭据码：{body}"
    finally:
        server.stop()
