# LLM: 只在临时安装和纯授权对象上验证，不读取真实用户权限。
# 模块用途: 锁定v9授权的闭合结构、确认绑定和激活生命周期。
from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.plugin_activation import PluginActivationRequest
from agent_py_agent.agent.plugin_activation_record import PluginActivation, plugin_catalog_digest
from agent_py_agent.agent.plugin_environment_plan import plan_plugin_environment
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.plugin_install_store import PluginInstallStore
from agent_py_agent.agent.plugin_installation import PluginInstallRequest
from agent_py_agent.agent.plugin_package import inspect_plugin_package
from agent_py_agent.agent.plugin_permissions.grants import PermissionSelection, permission_details
from agent_py_agent.agent.plugin_permissions.host_grants import validate_host_grant
from agent_py_agent.agent.plugin_runtime_facts import confirmation_code
from agent_py_agent.agent.user_space.owner_resolver import resolve_owner_home
from agent_py_agent.tests.test_plugin_manifest_v8 import _bundle
from agent_py_agent.tests.test_plugin_manifest_v9 import v9_payload


# LLM: 所有身份均为虚构，范围刻意有限，不能作真实启用授权。
# 函数用途: 提供设计合同规定的完整授权样本。
def grant_payload():
    return {
        "schema": "plugin_host_grant.v1", "grant_revision": 1,
        "installation_ref": "a" * 64, "package_sha256": "b" * 64, "activation_id": "c" * 64,
        "approved_capabilities": ["read", "chat", "act_as_user"],
        "target_user_ids": ["alice"], "manageable_user_ids": [], "creatable_user_ids": [],
        "threads": {"created_by_installation": True, "explicit_ids": []},
        "tasks": {"created_by_installation": True, "explicit_ids": []},
        "quota_ceiling": {"max_active_agents": 1, "max_subagents": 0, "max_depth": 0, "max_disk_mb": 64},
        "execution": {"tool_names": [], "asset_refs": [], "network": False, "max_concurrent_requests": 1,
                      "max_tokens_per_request": 1000, "max_duration_seconds": 60, "max_tool_calls": 0},
        "expires_at": None,
    }


# LLM: 返回副本必须与有效合同等值，不可借调用方别名改变授权。
# 函数用途: 验证完整授权可校验并得到独立副本。
def test_grant_accepts_complete_scope():
    value = grant_payload()
    actual = validate_host_grant(value)
    assert actual == value
    assert actual is not value


# LLM: 每个变异改变一种权限边界，严格布尔/整数和未知键都必须拒绝。
# 函数用途: 验证授权范围不能靠宽松解析扩大。
@pytest.mark.parametrize("path,value", [
    ("schema", "plugin_host_grant.v2"), ("grant_revision", True), ("grant_revision", 0),
    ("package_sha256", "bad"), ("activation_id", "x" * 64), ("installation_ref", ""),
    ("approved_capabilities", []), ("approved_capabilities", ["read", "read"]),
    ("approved_capabilities", ["admin"]), ("target_user_ids", ["main"]),
    ("target_user_ids", ["alice", "alice"]), ("creatable_user_ids", ["*"]),
    ("manageable_user_ids", ["Alice"]), ("threads.explicit_ids", ["*"]),
    ("threads.created_by_installation", 1), ("execution.network", True),
    ("execution.max_tokens_per_request", 1000001), ("execution.max_concurrent_requests", 9),
    ("execution.tool_names", ["*"]), ("execution.asset_refs", [{"id": "unbound"}]),
    ("quota_ceiling.max_depth", 5), ("expires_at", "2026-01-01"), ("owner_id", "main"),
])
def test_invalid_grant_rejected(path, value):
    grant = grant_payload()
    parts = path.split(".")
    container = grant if len(parts) == 1 else grant[parts[0]]
    container[parts[-1]] = value
    with pytest.raises((HostAPIError, ValueError)):
        validate_host_grant(grant)


# LLM: 缺省字段不能被补成无限授权，null只允许明确声明的到期时间。
# 函数用途: 验证每个顶层授权字段都必需。
@pytest.mark.parametrize("field", list(grant_payload()))
def test_grant_missing_fields_rejected(field):
    grant = deepcopy(grant_payload())
    del grant[field]
    with pytest.raises((HostAPIError, ValueError)):
        validate_host_grant(grant)


# LLM: 临时真实安装表、不启动包内容，激活CAS和序列化走产品链。
# 函数用途: 提供本机管理员下的未启用v9安装与绑定授权。
def installed_grant(tmp_path, capabilities=None):
    owner = resolve_owner_home(tmp_path)
    store = PluginInstallStore(owner)
    package = inspect_plugin_package(_bundle(v9_payload(capabilities)))
    entry = store.install(PluginInstallRequest(package, "install-test", 0)).installation
    plan = plan_plugin_environment(entry, "enable-test")
    os_grant = json.loads(json.dumps(permission_details(entry, plan, PermissionSelection(), "restricted")))
    grant = grant_payload()
    if capabilities is not None:
        grant["approved_capabilities"] = capabilities
    grant.update(installation_ref=entry.installation_ref, package_sha256=entry.package_sha256,
                 activation_id=PluginActivation(plan, "preparing").activation_id)
    return owner, store, entry, plan, os_grant, grant


# LLM: 原启用器只传permission_json，组合确认必须在激活边界拆成两个不可变子记录。
# 函数用途: 验证授权同表持久化、往返、固定安装引用以及不能同代热换授权。
def test_grant_persists_with_original_activation(tmp_path):
    owner, store, entry, plan, os_grant, grant = installed_grant(tmp_path)
    try:
        preparing = PluginActivation(plan, "preparing", permission_json=json.dumps({**os_grant, "host_api_grant": grant}))
    except ValueError:
        preparing = None
    assert preparing is not None, "v9确认必须被拆分成独立宿主授权和OS授权"
    assert preparing.to_payload()["host_api_grant"] == grant
    assert preparing.to_payload()["permission_grant"] == os_grant
    assert PluginActivation.from_payload(preparing.to_payload()) == preparing
    prepared = store.change_activation(PluginActivationRequest(plan.operation_id, entry.revision, preparing)).installation
    active = replace(preparing, phase="active", catalog_sha256=plugin_catalog_digest(entry.manifest))
    enabled = store.change_activation(PluginActivationRequest(plan.operation_id, prepared.revision, active)).installation
    assert enabled.installation_ref == entry.installation_ref
    assert PluginInstallStore(owner).snapshot()[0] == enabled
    changed = {**grant, "grant_revision": 2}
    with pytest.raises(ValueError):
        store.change_activation(PluginActivationRequest("revoke-test", enabled.revision,
            replace(active, phase="revoked", host_api_grant_json=json.dumps(changed))))


# LLM: v9不能利用旧的“无权限记录”准备通道，旧版本字节仍需保持原形。
# 函数用途: 验证缺失宿主授权时拒绝写入v9准备态。
def test_v9_cannot_prepare_without_host_grant(tmp_path):
    _, store, entry, plan, os_grant, _ = installed_grant(tmp_path)
    preparing = PluginActivation(plan, "preparing", permission_json=json.dumps(os_grant))
    with pytest.raises(ValueError):
        store.change_activation(PluginActivationRequest(plan.operation_id, entry.revision, preparing))
    assert "host_api_grant" not in preparing.to_payload()


# LLM: 码必须覆盖每个原始选择，而不是只绑定包或能力名。
# 函数用途: 验证同一确认码不能用于扩大目标、资源、配额或执行范围。
def test_confirmation_covers_complete_host_grant():
    original = grant_payload()
    for field in original:
        candidate = deepcopy(original)
        candidate[field] = None if original[field] is not None else "2030-01-01T00:00:00Z"
        assert confirmation_code({"host_api_grant": candidate}) != confirmation_code({"host_api_grant": original})


# LLM: 实际启用确认生成器必须承运新授权，不能靠测试直接组装代替产品接线。
# 函数用途: 验证默认仍需完整确认、关闭旧沙箱默认也不能给v9降wide。
def test_enable_previews_v9_grant_without_legacy_fallback(tmp_path):
    from agent_py_agent.agent.plugin_permissions.enable import (
        LegacyEnableContext,
        prepare_enable_authorization,
    )
    from agent_py_agent.agent.runtime_db.host_commands import HostCommandIdentity
    owner, _, entry, _, _, _ = installed_grant(tmp_path)
    identity = HostCommandIdentity(owner.owner_id, "main", "local", "test-thread", "preview-test")
    context = LegacyEnableContext(owner, None, SimpleNamespace(request=identity), {}, legacy_sandbox_default=False)
    target, plan, preview = prepare_enable_authorization(context, entry, None)
    assert "host_api_grant" in preview
    assert preview["mode"] == "restricted"
    host_grant = preview["host_api_grant"]
    assert host_grant["target_user_ids"] == []
    assert host_grant["approved_capabilities"] == list(entry.manifest.host_api)
    assert host_grant["installation_ref"] == target.installation_ref
    assert host_grant["activation_id"] == PluginActivation(plan, "preparing").activation_id


# LLM: 走真实安装prepare/activate流程，测试只不启动外部插件进程。
# 函数用途: 为令牌及授权测试创建实际可读取的固定激活。
def active_grant(tmp_path, capabilities=None):
    from agent_py_agent.agent.plugin_activation_ref import PluginActivationRef
    owner, store, entry, plan, os_grant, grant = installed_grant(tmp_path, capabilities)
    preparing = PluginActivation(plan, "preparing", permission_json=json.dumps({**os_grant, "host_api_grant": grant}))
    prepared = store.change_activation(PluginActivationRequest(plan.operation_id, entry.revision, preparing)).installation
    active = replace(preparing, phase="active", catalog_sha256=plugin_catalog_digest(entry.manifest))
    enabled = store.change_activation(PluginActivationRequest(plan.operation_id, prepared.revision, active)).installation
    return owner, store, enabled, PluginActivationRef.from_owner(owner, entry.manifest.plugin_id, active.activation_id)


# LLM: 调用环境发令牌的入口就是真实运行时入口，不能靠测试直接插入令牌表。
# 函数用途: 验证v9发放独立URL且旧协议不能绕过授权交集。
def test_v9_token_keeps_grant_and_rejects_legacy_query(tmp_path):
    from agent_py_agent.agent import plugin_host_api as api
    _, store, entry, ref = active_grant(tmp_path)
    api.set_host_api_base("http://127.0.0.1:8420")
    try:
        env = api.issue_host_api_env(ref, entry.manifest.plugin_id)
        assert env[api.HOST_API_URL_ENV].endswith("/plugin-host/v2/call")
        token = env[api.HOST_API_TOKEN_ENV]
        fixed = api.verify_host_api_token(token)
        assert fixed is not None and fixed.host_api_grant_json == entry.activation.host_api_grant_json
        with pytest.raises(ValueError):
            api.query_host(None, fixed, {"topics": ["threads"]})
        store.change_activation(PluginActivationRequest("revoke-test", entry.revision,
                                                       replace(entry.activation, phase="revoked")))
        assert api.verify_host_api_token(token) is None
    finally:
        api.set_host_api_base(None)


# LLM: 自动清理真实令牌表，不用假grant跳过原安装验证。
# 函数用途: 提供原安装及其真实来源发行器。
@pytest.fixture
def live_issuer(tmp_path):
    from agent_py_agent.agent import plugin_host_api as api
    from agent_py_agent.agent.plugin_host.authorization import IngressIssuer
    owner, store, entry, ref = active_grant(tmp_path)
    api.set_host_api_base("http://127.0.0.1:8420")
    token = api.issue_host_api_env(ref, entry.manifest.plugin_id)[api.HOST_API_TOKEN_ENV]
    policy = set(entry.manifest.host_api)
    issuer = IngressIssuer(capability_policy=lambda: frozenset(policy))
    yield issuer, token, policy, owner, store, entry
    api.set_host_api_base(None)


# LLM: 默认调用仍用正式解码器，以免测试绕过参数边界。
# 函数用途: 生成指定动作的公共请求。
def api_call(action="conversations.threads.list", payload=None, as_user=None):
    from agent_py_agent.agent.plugin_host.contracts import decode_call
    row = {"schema": "plugin_host_call.v2", "action": action, "payload": payload or {}}
    if as_user is not None:
        row["as_user"] = {"user_id": as_user}
    return decode_call(json.dumps(row).encode())


# LLM: 复制来源/授权DTO不等于真实发行；政策和激活变化应在已获得对象上立即生效。
# 函数用途: 验证对象来源、能力交集、冷读取以及实时撤权。
def test_real_issuer_rejects_forgery_and_rechecks_policy(live_issuer):
    from agent_py_agent.agent.plugin_host.authorization import authorize_call, revalidate_call
    from agent_py_agent.agent.plugin_host.contracts import AuthorizedCall, resolve_read_context
    issuer, token, policy, owner, store, entry = live_issuer
    source = issuer.ingress(token)
    authorized = authorize_call(source, api_call())
    assert isinstance(authorized, AuthorizedCall)
    assert authorized.effective_owner == owner.identity
    assert resolve_read_context(authorized).owner_home == owner
    with pytest.raises(HostAPIError):
        authorize_call(replace(source), api_call())
    with pytest.raises(HostAPIError):
        revalidate_call(replace(authorized))
    policy.remove("read")
    with pytest.raises(HostAPIError) as denied:
        revalidate_call(authorized)
    assert denied.value.code == "CAPABILITY_DENIED"
    policy.add("read")
    store.change_activation(PluginActivationRequest("revoke-test", entry.revision,
                                                   replace(entry.activation, phase="revoked")))
    with pytest.raises(HostAPIError):
        revalidate_call(authorized)


# LLM: 不在范围的目标先拒绝；在范围但C尚未注册时明确未就绪，不使用main invoker。
# 函数用途: 验证as_user的范围门和未接入失败关闭。
def test_targets_require_allowlist_and_real_resolver(live_issuer):
    from agent_py_agent.agent.plugin_host.authorization import authorize_call
    issuer, token, *_ = live_issuer
    source = issuer.ingress(token)
    with pytest.raises(HostAPIError) as denied:
        authorize_call(source, api_call(as_user="bobby"))
    assert denied.value.code == "TARGET_DENIED"
    with pytest.raises(HostAPIError) as pending:
        authorize_call(source, api_call(as_user="alice"))
    assert pending.value.code == "HOST_NOT_READY"


# LLM: 游标在原授权上签发；变异、过滤条件、快照过期均不得得到原始位置。
# 函数用途: 验证opaque游标、完整性和作用域绑定。
def test_cursor_integrity_scope_and_expiry(live_issuer):
    from agent_py_agent.agent.plugin_host.authorization import authorize_call
    from agent_py_agent.agent.plugin_host.cursors import CursorCodec
    issuer, token, *_ = live_issuer
    authorized = authorize_call(issuer.ingress(token), api_call())
    now = [100.0]
    codec = CursorCodec(clock=lambda: now[0])
    cursor = codec.encode(authorized, "snap-1", {"offset": 735}, {"status": "active"}, ttl_seconds=10)
    assert isinstance(cursor, str) and 0 < len(cursor) <= 2048
    decoded = codec.decode(authorized, cursor, {"status": "active"})
    assert decoded.snapshot_id == "snap-1" and decoded.position["offset"] == 735
    with pytest.raises(HostAPIError):
        codec.decode(authorized, cursor, {"status": "all"})
    with pytest.raises(HostAPIError):
        codec.decode(authorized, cursor[:-2] + "AA", {"status": "active"})
    now[0] = 110.0
    with pytest.raises(HostAPIError) as expired:
        codec.decode(authorized, cursor, {"status": "active"})
    assert expired.value.code == "CURSOR_EXPIRED"


# LLM: 空端口不是业务实现；发现接口不能因为capability存在就宣称可用。
# 函数用途: 验证组合根未注册失败关闭及静态发现目录。
def test_dispatch_missing_domain_and_discovery(live_issuer):
    from agent_py_agent.agent.plugin_host.contracts import (
        DomainResult,
        HostDomainPorts,
        authorize_call,
        dispatch_call,
    )
    issuer, token, *_ = live_issuer
    source = issuer.ingress(token)
    with pytest.raises(HostAPIError) as error:
        dispatch_call(authorize_call(source, api_call()), HostDomainPorts())
    assert error.value.code == "HOST_NOT_READY"
    result = dispatch_call(authorize_call(source, api_call("host.capabilities.get")), HostDomainPorts())
    assert isinstance(result, DomainResult)
    assert len(result.data["actions"]) == 42
    assert [row["action"] for row in result.data["actions"] if row["available"]] == ["host.capabilities.get"]


# LLM: 领域只接收AuthorizedCall；返回后撤权仍须拒绝送出成功结果。
# 函数用途: 验证注入端口按固定call方法执行和末核。
def test_dispatch_revalidates_after_domain(live_issuer):
    from agent_py_agent.agent.plugin_host.contracts import (
        DomainResult,
        HostDomainPorts,
        authorize_call,
        dispatch_call,
    )
    issuer, token, policy, *_ = live_issuer
    authorized = authorize_call(issuer.ingress(token), api_call())
    # LLM: 注入领域在返回前改变策略，用来核对外层末核。
    # 函数用途: 模拟计算成功但当前资格已失效。
    def domain(call):
        assert call is authorized
        policy.clear()
        return DomainResult(200, {"items": []})
    with pytest.raises(HostAPIError):
        dispatch_call(authorized, HostDomainPorts(conversation=SimpleNamespace(call=domain)))


# LLM: 使用真实GatewayHTTPHandler和回环socket，不用直接调用handler冒充路由端到端。
# 函数用途: 启动测试私有监听器，旧普通HTTP池只给一个worker以检查流隔离。
@pytest.fixture
def http_host(tmp_path, monkeypatch):
    from agent_py_agent.agent import plugin_host_api as api
    from agent_py_agent.agent.gateway_parts.bounded_http_server import GatewayBoundedHTTPServer
    from agent_py_agent.agent.gateway_parts.http_service import GatewayHTTPServer
    from agent_py_agent.agent.plugin_host.composition import HostComposition
    from agent_py_agent.agent.plugin_host.http_transport import HostTransport
    monkeypatch.setattr(GatewayBoundedHTTPServer, "max_request_workers", 1)
    _, store, entry, ref = active_grant(tmp_path)
    server = GatewayHTTPServer(0, SimpleNamespace(state=tmp_path / "state.json"))
    server.plugin_host = HostTransport(HostComposition())
    server.start()
    token = api.issue_host_api_env(ref, entry.manifest.plugin_id)[api.HOST_API_TOKEN_ENV]
    yield server, token, store, entry
    server.stop()


# LLM: 请求由http.client发送，状态与正文均从实际socket读回。
# 函数用途: 发送测试v2调用。
def http_request(server, token, body, headers=None, path="/plugin-host/v2/call"):
    from http.client import HTTPConnection
    client = HTTPConnection("127.0.0.1", server.server.server_address[1], timeout=3)
    supplied = {"Content-Type": "application/json", "X-Plugin-Host-Token": token, **(headers or {})}
    client.request("POST", path, body if isinstance(body, bytes) else json.dumps(body).encode(), supplied)
    response = client.getresponse()
    status, result = response.status, json.loads(response.read())
    client.close()
    return status, result


# LLM: 原HTTP入口必须真正能发现/拒绝，不把模块可导入当端到端验证。
# 函数用途: 验证实际v2路由、响应外壳、未接入状态与原JSON重复键拒绝。
def test_http_real_route_and_strict_envelope(http_host):
    server, token, *_ = http_host
    row = {"schema": "plugin_host_call.v2", "action": "host.capabilities.get", "payload": {}}
    status, result = http_request(server, token, row)
    assert status == 200
    assert result["schema"] == "plugin_host_result.v2" and result["ok"] is True
    assert len(result["data"]["actions"]) == 42
    assert result["meta"]["effective_owner"] == {"provider": "local", "owner_kind": "main", "owner_id": "main"}
    assert set(result["meta"]) == {"trace_id", "action", "observed_at", "effective_owner", "provenance", "warnings"}
    row["action"] = "conversations.threads.list"
    status, result = http_request(server, token, row)
    assert status == 503 and result["error"]["code"] == "HOST_NOT_READY"
    status, result = http_request(server, token, b'{"schema":"plugin_host_call.v2","schema":"bad","action":"host.capabilities.get","payload":{}}')
    assert status == 400 and result["error"]["code"] == "INVALID_SCHEMA"


# LLM: token不是浏览器凭据，也不能与管理员/Gateway身份头混用。
# 函数用途: 验证真实HTTP认证边界和错误不泄漏owner。
@pytest.mark.parametrize("headers", [{"Origin": "null"}, {"X-User-Id": "main"}, {"X-Channel": "local"},
                                    {"Authorization": "Bearer ignored"}, {"Cookie": "token=ignored"}])
def test_http_rejects_mixed_identity_and_browser_origin(http_host, headers):
    server, token, *_ = http_host
    status, result = http_request(server, token, {}, headers)
    assert status == 403
    assert "effective_owner" not in result["meta"]


# LLM: 流需真实socket保持开启；普通worker只有一个，若流占用它同步发现会超时。
# 函数用途: 验证独立SSE泵、单安装两流上限、停止失效及断开不取消业务。
def test_http_stream_does_not_occupy_normal_worker(http_host):
    import threading
    from http.client import HTTPConnection

    from agent_py_agent.agent.plugin_host.contracts import HostDomainPorts
    server, token, *_ = http_host
    release = threading.Event()
    # LLM: 订阅阻塞只占用生产者，结束不调用业务取消。
    # 函数用途: 提供一条事件后保持流开启。
    def events(authorized):
        yield {"event_id": "evt-1", "sequence": 1, "kind": "status", "created_at": "2026-10-23T00:00:00Z",
               "data": {"state": "running"}}
        release.wait(3)
    server.plugin_host.composition.ports = HostDomainPorts(conversation=SimpleNamespace(stream=events))
    clients = []
    row = {"schema": "plugin_host_call.v2", "action": "conversations.requests.stream", "payload": {"request_id": "req-1"}}
    try:
        for _ in range(2):
            client = HTTPConnection("127.0.0.1", server.server.server_address[1], timeout=2)
            client.request("POST", "/plugin-host/v2/stream", json.dumps(row),
                           {"Content-Type": "application/json", "X-Plugin-Host-Token": token})
            response = client.getresponse()
            clients.append((client, response))
            assert response.status == 200
            assert response.readline() == b"event: request\n"
            assert b'"event_id":"evt-1"' in response.readline()
        status, result = http_request(server, token, {**row, "action": "host.capabilities.get", "payload": {}})
        assert status == 200 and result["ok"] is True
        status, result = http_request(server, token, row, path="/plugin-host/v2/stream")
        assert status == 429 and result["error"]["code"] == "RATE_LIMITED"
    finally:
        release.set()
        for client, response in clients:
            response.close()
            client.close()


# LLM: bounded必须选择普通用户，不能因字段拼写不同落回main的reply-only政策。
# 函数用途: 锁定受限执行模式的目标门。
def test_bounded_message_requires_target():
    from agent_py_agent.agent.plugin_host.authorization import _check_capabilities
    from agent_py_agent.agent.plugin_host.contracts import decode_call
    row = {"schema": "plugin_host_call.v2", "action": "conversations.messages.send", "idempotency_key": "x" * 16,
           "payload": {"thread_id": "thread-1", "text": "hello", "tool_policy": "bounded"}}
    call = decode_call(json.dumps(row).encode())
    with pytest.raises(HostAPIError) as error:
        _check_capabilities(call, frozenset({"chat", "execute", "act_as_user"}))
    assert error.value.code == "TARGET_DENIED"


# LLM: 新协议限制不能反向改变旧OS记录的允许尺寸或序列化字节。
# 函数用途: 验证没有宿主授权的记录仍交原validator处理。
def test_legacy_permission_normalization_keeps_bytes():
    from agent_py_agent.agent.plugin_permissions.host_grants import normalize_activation_grant
    raw = json.dumps({"legacy": " " * 65536})
    activation = SimpleNamespace(permission_json=raw, host_api_grant_json=None)
    normalize_activation_grant(activation)
    assert activation.permission_json == raw and activation.host_api_grant_json is None


# LLM: 领域可分批接入，已声明只支持部分动作时不能宣称整个领域可用。
# 函数用途: 验证动作级就绪声明。
def test_discovery_respects_registered_action_subset(live_issuer):
    from agent_py_agent.agent.plugin_host.contracts import (
        DomainResult,
        HostDomainPorts,
        authorize_call,
        dispatch_call,
    )
    issuer, token, *_ = live_issuer
    domain = SimpleNamespace(actions=frozenset({"conversations.threads.get"}), call=lambda call: DomainResult(200, {}))
    result = dispatch_call(authorize_call(issuer.ingress(token), api_call("host.capabilities.get")),
                           HostDomainPorts(conversation=domain))
    ready = {row["action"] for row in result.data["actions"] if row["available"]}
    assert "conversations.threads.list" not in ready and "conversations.threads.get" in ready


# LLM: worker启动前撤权也要返回合法HTTP错误，不能发送没有HTTP头的SSE碎片。
# 函数用途: 验证预提交失败仍是协议有效响应。
def test_stream_revoked_before_headers_returns_http_error(live_issuer):
    import socket

    from agent_py_agent.agent.plugin_host.contracts import HostDomainPorts, authorize_call
    from agent_py_agent.agent.plugin_host.http_transport import StreamSession
    issuer, token, policy, *_ = live_issuer
    call = authorize_call(issuer.ingress(token), api_call("conversations.requests.stream", {"request_id": "req-1"}))
    left, right = socket.socketpair()
    right.settimeout(2)
    session = StreamSession(call, HostDomainPorts())
    session.socket = left
    session.ready.set()
    policy.clear()
    try:
        session.write()
        assert right.recv(4096).startswith(b"HTTP/1.1 403 ")
    finally:
        left.close()
        right.close()


# LLM: 只验证实际MCP构造环境发行，不启动进程；OS可用性独立于这条回归。
# 函数用途: 验证不申请read的合法v9仍能得到自己的v2令牌。
def test_runtime_issues_environment_for_chat_only_v9(tmp_path, monkeypatch):
    from agent_py_agent.agent import plugin_host_api as api
    from agent_py_agent.agent import plugin_runtime as runtime
    owner, _, entry, _ = active_grant(tmp_path, ["chat"])
    monkeypatch.setattr(runtime, "_launch_argv", lambda *args: ("fixture-entry", [], owner.home_dir))
    monkeypatch.setattr(runtime, "legacy_launch_policy", lambda *args: None)
    monkeypatch.setattr(runtime, "register_source_registry", lambda client: None)
    captured = []
    monkeypatch.setattr(runtime.MCPStdioClient, "__init__", lambda self, config, activation: captured.append(config))
    api.set_host_api_base("http://127.0.0.1:8420")
    try:
        runtime.PluginMCPClient(owner, entry)
        assert captured[0].env.get(api.HOST_API_URL_ENV, "").endswith("/plugin-host/v2/call")
        assert api.verify_host_api_token(captured[0].env[api.HOST_API_TOKEN_ENV]) is not None
    finally:
        api.set_host_api_base(None)


# LLM: 旧Broker不理解v9资源scope，即使宿主误调from_grant也必须拒绝。
# 函数用途: 验证v9不能绕过A0进入旧Broker。
def test_old_broker_rejects_v9_grant(live_issuer):
    from agent_py_agent.agent import plugin_host_api as api
    from agent_py_agent.agent.gateway_parts.plugin_broker_api import PluginBrokerService
    _, token, *_ = live_issuer
    with pytest.raises(ValueError):
        PluginBrokerService.from_grant(api.verify_host_api_token(token))


# LLM: 两种管理运输承运相同显式选择，确认仍由原manager负责；不模拟批准结果。
# 函数用途: 验证交互与非交互命令都不丢弃授权选择。
@pytest.mark.parametrize("interactive", [False, True])
def test_management_transports_host_selection(tmp_path, monkeypatch, interactive):
    from agent_py_agent.agent.gateway_parts import command_stream
    from agent_py_agent.agent.gateway_parts import plugin_command_service as service
    from agent_py_agent.agent.plugin_permissions.host_grants import (
        _selection_slot,
        selection_schema,
    )
    choice = {key: grant_payload()[key] for key in selection_schema()["properties"]}
    observed = []
    # LLM: 捕获真实上下文运输值，不把替身结果宣称为批准或启用。
    # 函数用途: 观察manager接收命令时的授权选择。
    def command(*args, **kwargs):
        observed.append(_selection_slot().get())
        return {"ok": True}
    manager = SimpleNamespace(command=command, context=SimpleNamespace(owner=resolve_owner_home(tmp_path)))
    monkeypatch.setattr(service, "_management", lambda *args: manager)
    monkeypatch.setattr(command_stream, "serve_command_stream", lambda handler, paths, owner, request, execute: execute(None, None))
    handler = SimpleNamespace(client_address=("127.0.0.1", 32100), _send_json=lambda *args: None)
    server = SimpleNamespace(agent=object(), paths=object())
    body = {"plugin_request_id": "selection-test", "catalog_revision": "revision", "host_api_selection": choice}
    if interactive:
        service._interactive_command(handler, server, body, "/plugins enable fixture")
    else:
        service.plugin_http_response(handler, server, body, text="/plugins enable fixture")
    assert observed and observed[0] is not None and json.loads(observed[0]) == choice
    assert _selection_slot().get() is None


# LLM: 使用真实安装和令牌；C的业务实现未在本任务内，仅通过规定DTO模拟resolver合同。
# 函数用途: 提供冷用户、修订及可收紧策略的受信factory替身。
@pytest.fixture
def target_issuer(tmp_path):
    from agent_py_agent.agent import plugin_host_api as api
    from agent_py_agent.agent.plugin_host.authorization import IngressIssuer
    from agent_py_agent.agent.plugin_host.contracts import (
        OwnerReadContext,
        OwnerResolutionResult,
        RestrictedExecutionPolicy,
        RestrictedPermission,
    )
    owner, _, entry, ref = active_grant(tmp_path, ["read", "chat", "execute", "act_as_user"])
    state = {"revision": 1, "execution_calls": 0, "extra_budget": 0, "wrong_source": False}
    # LLM: 纯解析不ensure，不创建目标目录或Agent。
    # 函数用途: 投影同一普通用户的当前修订。
    def read(binding):
        home = resolve_owner_home(owner.root, binding.effective_owner)
        return OwnerReadContext(home.identity, home, binding, state["revision"])
    # LLM: 测试返回受限合同，不是真实执行身份或权限批准。
    # 函数用途: 构造可变预算并记录执行factory是否被调用。
    def execute(binding):
        state["execution_calls"] += 1
        context = read(binding)
        budgets = {key: value for key, value in binding.grant["execution"].items() if key.startswith("max_")}
        budgets["max_tool_calls"] += state["extra_budget"]
        policy = RestrictedExecutionPolicy(budgets=budgets, policy_digest="test-policy")
        source = replace(binding) if state["wrong_source"] else binding
        return OwnerResolutionResult(context.identity, context.owner_home, RestrictedPermission(), policy,
                                     context.user_revision, source)
    api.set_host_api_base("http://127.0.0.1:8420")
    token = api.issue_host_api_env(ref, entry.manifest.plugin_id)[api.HOST_API_TOKEN_ENV]
    issuer = IngressIssuer(SimpleNamespace(resolve_read=read, resolve_execution=execute))
    try:
        yield issuer, token, state
    finally:
        issuer.close()
        api.set_host_api_base(None)


# LLM: 插件持execute能力不代表每个只读调用都有创建执行主体的权利。
# 函数用途: 验证只读上下文无初始化且不能升级成执行factory。
def test_read_call_cannot_resolve_execution(target_issuer):
    from agent_py_agent.agent.plugin_host.contracts import (
        authorize_call,
        resolve_execution_context,
        resolve_read_context,
    )
    issuer, token, state = target_issuer
    authorized = authorize_call(issuer.ingress(token), api_call(as_user="alice"))
    context = resolve_read_context(authorized)
    assert not context.owner_home.home_dir.exists()
    assert state["execution_calls"] == 0
    with pytest.raises(HostAPIError) as denied:
        resolve_execution_context(authorized)
    assert denied.value.code == "CAPABILITY_DENIED" and state["execution_calls"] == 0


# LLM: 已授权bounded调用仍须逐次检查同一用户修订、来源对象和预算。
# 函数用途: 验证resolver不得扩权、换源或在用户变化后继续。
@pytest.mark.parametrize("change", ["revision", "extra_budget", "wrong_source"])
def test_execution_resolver_live_fences(target_issuer, change):
    from agent_py_agent.agent.plugin_host.contracts import (
        authorize_call,
        decode_call,
        resolve_execution_context,
    )
    issuer, token, state = target_issuer
    row = {"schema": "plugin_host_call.v2", "action": "conversations.messages.send", "as_user": {"user_id": "alice"},
           "idempotency_key": "a" * 16, "payload": {"thread_id": "thread-1", "text": "hello", "tool_policy": "bounded"}}
    authorized = authorize_call(issuer.ingress(token), decode_call(json.dumps(row).encode()))
    context = resolve_execution_context(authorized)
    assert context.identity.owner_id == "alice" and context.permission.is_admin is False
    state[change] += 1
    with pytest.raises(HostAPIError):
        resolve_execution_context(authorized)


# LLM: 单页尺寸包含meta信封，不以data尺寸替代实际线路字节。
# 函数用途: 验证响应字节上限覆盖完整JSON。
def test_http_response_limit_includes_envelope(http_host):
    from agent_py_agent.agent.plugin_host.contracts import DomainResult, HostDomainPorts
    server, token, *_ = http_host
    server.plugin_host.composition.ports = HostDomainPorts(conversation=SimpleNamespace(
        call=lambda call: DomainResult(200, {"text": "x" * 262120})))
    status, result = http_request(server, token, {"schema": "plugin_host_call.v2", "action": "conversations.threads.list", "payload": {}})
    assert status == 413 and result["error"]["code"] == "PAYLOAD_TOO_LARGE"


# LLM: 使用真实监听和撤销记录；缩短的是测试心跳间隔，不改变生产默认值。
# 函数用途: 验证领域阻塞时心跳仍工作且激活撤销后不再送事件。
def test_http_idle_stream_heartbeat_and_revocation(http_host, monkeypatch):
    import threading
    from http.client import HTTPConnection

    from agent_py_agent.agent.plugin_host import http_transport as transport
    from agent_py_agent.agent.plugin_host.contracts import HostDomainPorts
    server, token, store, entry = http_host
    release = threading.Event()
    monkeypatch.setattr(transport, "HEARTBEAT_SECONDS", 0.05)
    # LLM: 模拟等待原业务，不把退订视为cancel。
    # 函数用途: 阻塞到测试释放后才结束生成器。
    def events(authorized):
        release.wait(3)
        yield from ()
    server.plugin_host.composition.ports = HostDomainPorts(conversation=SimpleNamespace(stream=events))
    client = HTTPConnection("127.0.0.1", server.server.server_address[1], timeout=2)
    row = {"schema": "plugin_host_call.v2", "action": "conversations.requests.stream", "payload": {"request_id": "req-1"}}
    try:
        client.request("POST", "/plugin-host/v2/stream", json.dumps(row),
                       {"Content-Type": "application/json", "X-Plugin-Host-Token": token})
        response = client.getresponse()
        assert response.status == 200 and response.readline() == b": heartbeat\n"
        assert response.readline() == b"\n"
        store.change_activation(PluginActivationRequest("revoke-stream", entry.revision,
                                                       replace(entry.activation, phase="revoked")))
        remaining = response.read()
        assert b"event: error\n" in remaining and b"PLUGIN_TOKEN_INVALID" in remaining
        assert b"event: request\n" not in remaining
    finally:
        release.set()
        client.close()


# LLM: 拒绝实际超长和重复认证头，不通过宽松handler解析冒充成功。
# 函数用途: 验证HTTP大小和唯一认证头约束。
def test_http_body_limit_and_duplicate_token(http_host):
    from http.client import HTTPConnection
    server, token, *_ = http_host
    status, result = http_request(server, token, b" " * 65537)
    assert status == 413 and result["error"]["code"] == "PAYLOAD_TOO_LARGE"
    client = HTTPConnection("127.0.0.1", server.server.server_address[1], timeout=2)
    try:
        client.putrequest("POST", "/plugin-host/v2/call")
        for _ in range(2):
            client.putheader("X-Plugin-Host-Token", token)
        client.putheader("Content-Length", "0")
        client.putheader("Content-Type", "application/json")
        client.endheaders()
        response = client.getresponse()
        result = json.loads(response.read())
        assert response.status == 401 and result["error"]["code"] == "PLUGIN_TOKEN_INVALID"
        assert set(result["meta"]) == {"trace_id", "action", "observed_at"}
    finally:
        client.close()


# LLM: 失败future模拟executor初始化失败前后时序；用真实socket确认关闭，不以集合清空替代资源退出。
# 函数用途: 验证无worker执行finally时仍关闭连接并保留原异常。
@pytest.mark.parametrize("phase", ["before_transfer", "after_transfer"])
def test_stream_failed_worker_closes_socket(live_issuer, phase):
    import io
    import socket
    from concurrent.futures import Future

    from agent_py_agent.agent.plugin_host.contracts import HostDomainPorts, authorize_call
    from agent_py_agent.agent.plugin_host.http_transport import StreamPool
    issuer, token, *_ = live_issuer
    authorized = authorize_call(issuer.ingress(token), api_call("conversations.requests.stream", {"request_id": "req-1"}))
    futures, faults = [], []
    error = RuntimeError("worker-init-failure")
    # LLM: 只模拟任务未进入函数的executor失败，不运行替代业务线程。
    # 函数用途: 返回可控制完成时序的future。
    def submit(work):
        future = Future()
        futures.append(future)
        if phase == "before_transfer":
            future.set_exception(error)
        return future
    pool = StreamPool()
    pool._executor = SimpleNamespace(submit=submit)
    pool._group = SimpleNamespace(retain_fault=faults.append)
    left, right = socket.socketpair()
    right.settimeout(1)
    handler = SimpleNamespace(wfile=io.BytesIO(), connection=left)
    try:
        if phase == "before_transfer":
            with pytest.raises(HostAPIError):
                pool.open(handler, authorized, HostDomainPorts())
            assert left.fileno() >= 0  # 还未转移，普通HTTP仍有权返回错误。
        else:
            pool.open(handler, authorized, HostDomainPorts())
            for future in futures:
                future.set_exception(error)
            assert right.recv(1) == b""  # fd转移后无writer，回调必须关闭。
        assert not pool._sessions and any(fault is error for fault in faults)
    finally:
        left.close()
        right.close()


# LLM: put中退订后不应再调用源next；这不同于已经进入且无法强制打断的next。
# 函数用途: 验证生产者停止后不主动继续消费事件源。
def test_stream_stopped_producer_does_not_read_again(live_issuer, monkeypatch):
    from agent_py_agent.agent.plugin_host import http_transport as transport
    from agent_py_agent.agent.plugin_host.contracts import HostDomainPorts
    observed, closed = [], []
    session = transport.StreamSession(None, HostDomainPorts())
    session.ready.set()
    # LLM: 第二次迭代只记录副作用，避免测试自身永久阻塞。
    # 函数用途: 观察退订后是否又读取了一条源记录。
    def events(*args):
        try:
            for index in range(2):
                observed.append(index)
                yield {}
        finally:
            closed.append(True)
    # LLM: 模拟队列背压期间断连。
    # 函数用途: 在首次交付时置停止标记。
    def put(item):
        session.stop.set()
    monkeypatch.setattr(transport, "stream_events", events)
    monkeypatch.setattr(transport, "validate_request_event", lambda item: item)
    monkeypatch.setattr(session, "put", put)
    session.produce()
    assert observed == [0] and closed == [True]
