# LLM: 所有安装/用户均为临时资料；真实回环HTTP不替换生产组合根。
# 模块用途: 回归生产Gateway接线及A/C工具资产合同。
import json
from dataclasses import replace

import pytest

from agent_py_agent.agent.plugin_host.contracts import decode_call
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.plugin_permissions.host_grants import validate_host_grant
from agent_py_agent.tests.test_plugin_host_v2_grants import grant_payload


# LLM: 不接受C执行器不能表示的工具名，入站grant与动作应在IO前一致拒绝。
# 函数用途: 锁定工具名96字符及字母数字下划线边界。
@pytest.mark.parametrize("name", ["read.file", "tool:read", "read-file", "x" * 97])
def test_tool_schema_matches_execution_allowlist(name):
    grant = grant_payload()
    grant["execution"]["tool_names"] = [name]
    with pytest.raises(HostAPIError):
        validate_host_grant(grant)
    with pytest.raises(HostAPIError):
        decode_call(json.dumps({"schema": "plugin_host_call.v2", "action": "execute.tool",
            "as_user": {"user_id": "alice"}, "idempotency_key": "execute_test_key_1",
            "payload": {"thread_id": "thread-one", "tool_name": name, "arguments": {}}}).encode())


# LLM: 资产ID不清洗；首字符必须字母数字，防止A批准但C拒绝的合同断层。
# 函数用途: 锁定script/workflow资产编号一致性。
@pytest.mark.parametrize("kind", ["script", "workflow"])
@pytest.mark.parametrize("identifier", ["-asset", ".asset", "_asset", ":asset"])
def test_asset_schema_matches_execution_allowlist(kind, identifier):
    grant = grant_payload()
    grant["execution"]["asset_refs"] = [{"kind": kind, "id": identifier, "revision": 1, "sha256": "a" * 64}]
    with pytest.raises(HostAPIError):
        validate_host_grant(grant)
    with pytest.raises(HostAPIError):
        decode_call(json.dumps({"schema": "plugin_host_call.v2", "action": "execute." + kind,
            "as_user": {"user_id": "alice"}, "idempotency_key": "execute_test_key_1",
            "payload": {"thread_id": "thread-one", kind + "_id": identifier, "expected_revision": 1}}).encode())


# LLM: 用原安装激活链取得真实令牌，不通过伪造binding获得C投影。
# 函数用途: 提供完整批准范围的生产HTTP服务及安装。
@pytest.fixture
def production_host(tmp_path, request):
    from agent_py_agent.agent import plugin_host_api as api
    from agent_py_agent.agent.gateway_parts.http_service import GatewayHTTPServer
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.agent.plugin_activation import PluginActivationRequest
    from agent_py_agent.agent.plugin_activation_record import (
        PluginActivation,
        plugin_catalog_digest,
    )
    from agent_py_agent.agent.plugin_activation_ref import PluginActivationRef
    from agent_py_agent.agent.plugin_manifest_host_v9 import host_capabilities
    from agent_py_agent.tests import test_plugin_host_v2_grants as _grants

    options = getattr(request, "param", {})
    owner, store, entry, plan, os_grant, grant = _grants.installed_grant(tmp_path, options.get("caps", sorted(host_capabilities())))
    if options.get("extra"):
        grant["target_user_ids"].append(options["extra"])
    grant.update(manageable_user_ids=["alice"], creatable_user_ids=["alice"])
    default_calls = 4 if _grants.installed_grant.__name__ == "installed_grant" else grant["execution"]["max_tool_calls"]
    grant["execution"].update(tool_names=["read_file", "write_file"], max_tool_calls=options.get("max_tool_calls", default_calls),
        asset_refs=options.get("asset_refs") or [{"kind": "script", "id": "script-one", "revision": 1, "sha256": "a" * 64}])
    preparing = PluginActivation(plan, "preparing", permission_json=json.dumps({**os_grant, "host_api_grant": grant}))
    prepared = store.change_activation(PluginActivationRequest(plan.operation_id, entry.revision, preparing)).installation
    active = replace(preparing, phase="active", catalog_sha256=plugin_catalog_digest(entry.manifest))
    enabled = store.change_activation(PluginActivationRequest(plan.operation_id, prepared.revision, active)).installation
    ref = PluginActivationRef.from_owner(owner, entry.manifest.plugin_id, active.activation_id)
    from types import SimpleNamespace
    paths = SimpleNamespace(state=tmp_path / "state.json") if options.get("lightweight") else gateway_paths_from_root(tmp_path / "gateway")
    server = GatewayHTTPServer(0, paths)
    server.start()
    token = api.issue_host_api_env(ref, entry.manifest.plugin_id)[api.HOST_API_TOKEN_ENV]
    yield server, token, owner, enabled
    server.stop()


# LLM: 原HTTP传输和原v2解码器必须经过；期望结果从socket读回而非直接调用域。
# 函数用途: 发送带稳定幂等键的真实生产请求。
def production_call(host, action, payload=None, *, user=None, key=None, status=200):
    from agent_py_agent.tests.test_plugin_host_v2_grants import http_request
    row = {"schema": "plugin_host_call.v2", "action": action, "payload": payload or {}}
    if key is not None:
        row["idempotency_key"] = key
    if user is not None:
        row["as_user"] = {"user_id": user}
    import time
    for _ in range(8):
        actual, body = http_request(host[0], host[1], row)
        if actual != 429 or body.get("error", {}).get("code") != "RATE_LIMITED":
            break
        assert body["error"]["outcome"] == "not_started"
        time.sleep(0.25)
    assert actual == status, body
    assert body["ok"], body
    return body["data"]


# LLM: 显式空HostComposition旧单测保持不变；此处构造真实paths而不替换端口。
# 函数用途: 证明实际Gateway发现目录的42项均接入，而非仅host动作。
def test_production_gateway_wires_all_actions(production_host):
    rows = production_call(production_host, "host.capabilities.get")["actions"]
    assert len(rows) == 42
    assert all(row["available"] for row in rows), [row["action"] for row in rows if not row["available"]]


# LLM: A/C投影必须保留实际动作/实参/幂等键和获批资产，不能把请求替换成chat.send。
# 函数用途: 检查真实入站授权转C投影不丢意图。
def test_production_factory_projects_actual_call_and_assets(production_host):
    from agent_py_agent.agent.plugin_host.authorization import authorize_call
    server, token, *_ = production_host
    call = decode_call(json.dumps({"schema": "plugin_host_call.v2", "action": "users.local.create",
        "payload": {"user_id": "alice"}, "idempotency_key": "create_alice_key_1"}).encode())
    authorized = authorize_call(server.plugin_host.composition.issuer.ingress(token), call)
    factory = server.plugin_host.composition.issuer._resolver.factory
    actual = factory.verify(authorized)
    assert actual.action == "users.local.create"
    assert json.loads(actual.payload_json)["user_id"] == "alice"
    assert actual.idempotency_key == call.idempotency_key
    assert actual.grant.execution_policy.allowed_assets[0].asset_id == "script-one"


# LLM: 响应全部来自真实socket；文件工具必须只改alice工作区，禁用后既有来源立即失效。
# 函数用途: 回归生产本地用户、会话、原请求与受限工具的正向链。
def test_production_gateway_owner_conversation_and_execution(production_host):
    host = production_host
    user = production_call(host, "users.local.create", {"user_id": "alice"}, key="create_alice_key_1", status=201)
    assert user["user_id"] == "alice"
    assert production_call(host, "users.local.get", {"user_id": "alice"})["status"] == "enabled"
    assert production_call(host, "users.local.quota.get", {"user_id": "alice"})["usage"]["active_agents"] == 0
    threads = {}
    for owner in (None, "alice"):
        created = production_call(host, "conversations.threads.create", {"title": "生产接线"}, user=owner,
            key="create_thread_key_1", status=201)
        tid = created["thread_id"]
        threads[owner] = tid
        assert production_call(host, "conversations.threads.list", user=owner)["items"][0]["thread_id"] == tid
        request = production_call(host, "conversations.messages.send", {"thread_id": tid, "text": "你好"},
            user=owner, key="send_message_key_1", status=202)
        assert request["state"] == "queued"
        polled = production_call(host, "conversations.requests.poll", {"request_id": request["request_id"]}, user=owner)
        assert polled["request"]["state"] == "queued"
        cancelled = production_call(host, "conversations.requests.cancel", {"request_id": request["request_id"],
            "expected_revision": polled["request"]["revision"]}, user=owner, key="cancel_message_key", status=202)
        assert cancelled["cancel_requested"] is True
        assert production_call(host, "conversations.messages.list", {"thread_id": tid}, user=owner)["complete"] is True
        for action in ("memory.search", "tasks.list", "goals.list", "interactions.pending_approvals.list", "interactions.pending_inputs.list"):
            payload = {"query": "不存在"} if action == "memory.search" else {"thread_id": tid}
            assert production_call(host, action, payload, user=owner)["items"] == []
    assert threads[None] != threads["alice"]
    for name, arguments in (("write_file", {"path": "hello.txt", "content": "只属于alice"}),
                            ("read_file", {"path": "hello.txt"})):
        result = production_call(host, "execute.tool", {"thread_id": threads["alice"], "tool_name": name,
            "arguments": arguments}, user="alice", key="execute_" + name + "_key_1", status=202)
        assert result["accepted"] is True
        poll = production_call(host, "conversations.requests.poll", {"request_id": result["request_id"]}, user="alice")
        assert poll["request"]["state"] == "succeeded", poll
    root = host[2].root
    assert (root / "owners/local/alice/workspace/hello.txt").read_text() == "只属于alice"
    assert not (host[2].workspace_dir / "hello.txt").exists()


# LLM: 轻量启动不是空组合的信号，显式测试空组合仍由原fixture自行注入。
# 函数用途: 守住仅state路径的默认生产会话入口。
@pytest.mark.parametrize("production_host", [{"lightweight": True}], indirect=True)
def test_default_gateway_business_api_is_wired(production_host):
    assert production_call(production_host, "conversations.threads.list")["items"] == []


# LLM: execute动作的静态能力已由A检查，C冷身份读取不能额外要求read能力。
# 函数用途: 守住公开宣称的最小执行权限组合。
@pytest.mark.parametrize("production_host", [{"caps": ["execute", "act_as_user"]},
    {"caps": ["read", "execute", "act_as_user"]}], indirect=True)
def test_declared_minimum_execution_capabilities_are_sufficient(production_host):
    from agent_py_agent.agent.plugin_host.authorization import authorize_call
    from agent_py_agent.agent.plugin_host.contracts import resolve_execution_context
    from agent_py_agent.agent.user_space.local_user_lifecycle import (
        LocalUserLifecycle,
        LocalUserOperation,
    )
    server, token, *_ = production_host
    resolver = server.plugin_host.composition.issuer._resolver
    LocalUserLifecycle(resolver.store).create("alice", LocalUserOperation("test-install", "a" * 64, "create-alice-test"))
    call = decode_call(json.dumps({"schema": "plugin_host_call.v2", "action": "execute.tool", "as_user": {"user_id": "alice"},
        "idempotency_key": "minimal_execute_key", "payload": {"thread_id": "thread-one", "tool_name": "read_file", "arguments": {"path": "x"}}}).encode())
    authorized = authorize_call(server.plugin_host.composition.issuer.ingress(token), call)
    assert resolve_execution_context(authorized).permission.is_admin is False


# LLM: 公开schema允许的附加UID不能污染另一个已登记目标；实际选非法目标仍拒绝。
# 函数用途: 守住A/C用户编号集合投影的局部失败隔离。
@pytest.mark.parametrize("production_host", [{"extra": "default"}, {"extra": "a" * 33}], indirect=True)
def test_publicly_accepted_uid_does_not_poison_other_valid_target(production_host):
    production_call(production_host, "users.local.create", {"user_id": "alice"}, key="create_alice_key_1", status=201)
    assert production_call(production_host, "conversations.threads.list", user="alice")["items"] == []


# LLM: 规范运行路径不能只做词法包含；点段或中间链接都必须在原Store打开前拒绝。
# 函数用途: 回归生产组合根的路径逃逸闸门。
@pytest.mark.parametrize("attack", ["dotdot", "symlink"])
def test_gateway_runtime_path_rejects_escape(tmp_path, attack):
    from types import SimpleNamespace

    from agent_py_agent.agent.plugin_host.contracts import OwnerReadContext
    from agent_py_agent.agent.plugin_host.gateway_composition import GatewayHostResources
    from agent_py_agent.agent.user_space.owner_resolver import resolve_owner_home
    owner = resolve_owner_home(tmp_path)
    owner.home_dir.mkdir(parents=True)
    if attack == "dotdot":
        path = owner.home_dir / ".." / "alice" / "conversations"
    else:
        other = tmp_path / "elsewhere"
        other.mkdir()
        path = owner.home_dir / "linked"
        path.symlink_to(other, target_is_directory=True)
    server = SimpleNamespace(agent=SimpleNamespace(config=SimpleNamespace(conversation_workspace=str(path))), control_plane=None)
    resources = GatewayHostResources(server, tmp_path)
    with pytest.raises(HostAPIError, match="TARGET_DENIED"):
        resources.store_for(OwnerReadContext(owner.identity, owner, None))


# LLM: A签发期的binding只能解析上限，不能绕过真实AuthorizedCall执行业务。
# 函数用途: 防止冷策略上下文被复用于实际工具执行。
def test_cold_binding_cannot_construct_business_invoker(production_host):
    from agent_py_agent.agent.plugin_host.authorization import authorize_call
    from agent_py_agent.agent.plugin_host.execution import RestrictedToolInvoker
    production_call(production_host, "users.local.create", {"user_id": "alice"}, key="create_alice_key_1", status=201)
    server, token, *_ = production_host
    composition = server.plugin_host.composition
    call = decode_call(json.dumps({"schema": "plugin_host_call.v2", "action": "execute.tool", "as_user": {"user_id": "alice"},
        "idempotency_key": "cold_binding_test1", "payload": {"thread_id": "thread-one", "tool_name": "read_file", "arguments": {"path": "x"}}}).encode())
    authorized = authorize_call(composition.issuer.ingress(token), call)
    factory = composition.issuer._resolver.factory
    cold = factory.resolve_execution_context(authorized.binding)
    with pytest.raises(HostAPIError, match="SOURCE_INVALID"):
        RestrictedToolInvoker(factory, cold)


# LLM: 生产组合公开的execute.script必须接入真实ScriptStore和ScriptRunner，缺失资产返回404而非503未知结局。
# 函数用途: 验证生产HTTP下未注册脚本明确拒绝与已注册脚本真实执行闭环。
def test_production_gateway_script_asset_read_and_execution(tmp_path):
    import hashlib
    from types import SimpleNamespace

    from agent_py_agent.agent.assets.descriptor import AssetDescriptor
    from agent_py_agent.agent.assets.script_store import ScriptStore
    from agent_py_agent.tests.test_plugin_host_v2_grants import http_request

    code = 'import json\nprint(json.dumps({"ok": True, "greeting": "hello-alice"}))\n'
    code_sha = hashlib.sha256(code.encode("utf-8")).hexdigest()
    fixture = production_host.__wrapped__(tmp_path, SimpleNamespace(
        param={"asset_refs": [{"kind": "script", "id": "script-one", "revision": 1, "sha256": code_sha}]}
    ))
    host = next(fixture)
    try:
        production_call(host, "users.local.create", {"user_id": "alice"}, key="script_alice_create_1", status=201)
        thread = production_call(host, "conversations.threads.create", {"title": "script"}, user="alice",
                                 key="script_thread_create_1", status=201)
        status, missing = http_request(host[0], host[1], {
            "schema": "plugin_host_call.v2", "action": "execute.script", "as_user": {"user_id": "alice"},
            "idempotency_key": "script_missing_exec_1",
            "payload": {"thread_id": thread["thread_id"], "script_id": "script-one", "expected_revision": 1},
        })
        assert status == 404 and missing["error"]["code"] == "ASSET_NOT_FOUND" and missing["error"]["outcome"] == "not_started"
        alice_home = host[2].root / "owners/local/alice"
        ScriptStore(alice_home, extra_roots=()).save_script(
            AssetDescriptor(id="script-one", kind="script", name="script-one", metadata={"revision": 1}),
            code,
        )
        accepted = production_call(host, "execute.script", {
            "thread_id": thread["thread_id"], "script_id": "script-one", "expected_revision": 1,
        }, user="alice", key="script_approved_exec_1", status=202)
        poll = production_call(host, "conversations.requests.poll", {"request_id": accepted["request_id"]}, user="alice")
        assert poll["request"]["state"] == "succeeded", poll
    finally:
        next(fixture, None)


# LLM: 验证worker层对插件消息的零工具/断网策略、目标线程绑定，以及篡改策略或申请聊天工具时的失败即关闭。
# 函数用途: 覆盖插件请求进入Gateway worker时的正反向执行边界合同。
def test_production_worker_enforces_plugin_execution_policy_and_thread_binding(production_host):
    from types import SimpleNamespace

    from agent_py_agent.agent.gateway_parts.request_context import (
        GatewayAskRunContext,
        GatewayConversationLoadRequest,
        gateway_conversation_context,
    )
    from agent_py_agent.agent.gateway_parts.request_execution import (
        _gateway_run_params,
        _GatewayRunParamsRequest,
    )
    from agent_py_agent.tests.test_plugin_host_v2_grants import http_request

    thread = production_call(production_host, "conversations.threads.create", {}, key="worker_policy_thread1", status=201)
    tid = thread["thread_id"]
    sent = production_call(production_host, "conversations.messages.send",
                           {"thread_id": tid, "text": "check worker", "tool_policy": "none"},
                           key="worker_policy_msg_001", status=202)
    resources = production_host[0].plugin_host.composition.ports.observations.services.resources
    paths = resources.paths_for(SimpleNamespace(owner_home=production_host[2]))
    path = paths.inbox / f"{sent['request_id']}.json"
    request = json.loads(path.read_text(encoding="utf-8"))
    agent = SimpleNamespace(
        config=SimpleNamespace(my_agent_home=str(production_host[2].root)),
        home_paths=SimpleNamespace(root=production_host[2].root),
        conversation_store=resources.store_for(SimpleNamespace(owner_home=production_host[2])),
    )
    conversation = gateway_conversation_context(
        GatewayConversationLoadRequest(agent, request, request["id"], "check worker")
    )
    assert conversation.thread_id == tid
    ctx = GatewayAskRunContext(agent, request, path, paths.responses / f"{request['id']}.json", request["id"], None)
    params = _gateway_run_params(_GatewayRunParamsRequest(request, ctx, conversation, "check worker"))
    assert params.allowed_tools == []

    production_call(production_host, "users.local.create", {"user_id": "alice"}, key="worker_policy_alice1", status=201)
    alice_thread = production_call(production_host, "conversations.threads.create", {}, user="alice",
                                   key="worker_policy_athread1", status=201)
    status, denied = http_request(production_host[0], production_host[1], {
        "schema": "plugin_host_call.v2", "action": "conversations.messages.send",
        "as_user": {"user_id": "alice"},
        "idempotency_key": "worker_policy_msg_002",
        "payload": {"thread_id": alice_thread["thread_id"], "text": "bounded tools", "tool_policy": "bounded"},
    })
    assert status == 503 and denied["error"]["code"] == "HOST_NOT_READY"

    with pytest.raises(RuntimeError, match="plugin"):
        bad = {**request, "metadata": {**request["metadata"], "plugin_execution_policy": {
            **request["metadata"]["plugin_execution_policy"], "tool_names": ["read_file"]}}}
        _gateway_run_params(_GatewayRunParamsRequest(bad, ctx, conversation, "check worker"))
