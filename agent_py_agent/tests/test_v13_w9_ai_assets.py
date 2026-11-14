"""#92 原生资产事实、无秘密投影和生产客户端真实治理往返。"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.business_spaces import v13_w9_ai_assets as assets
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.tests import test_rework_r8_business_wire_contract as wire_harness

gateway = wire_harness.gateway
thin_client = wire_harness.thin_client


# LLM: 仅提供调用 DTO；单元测试显式替换 I/O 叶，不把替身当成真实事务验收。
# 函数用途: 组装隔离 home 下的领域调用。
@pytest.fixture
def unit_call(tmp_path, monkeypatch):
    from agent_py_agent.agent.settings import AgentConfig
    from agent_py_agent.agent.user_space.home_layout import home_paths
    from agent_py_agent.agent.user_space.owner_resolver import (
        home_paths_with_owner,
        resolve_owner_home,
    )

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MY_AGENT_HOME", str(tmp_path / "home"))
    home = home_paths(tmp_path / "home")
    home = home_paths_with_owner(home, resolve_owner_home(home.root))
    host = SimpleNamespace(home_paths=home, config=AgentConfig(business_spaces_enabled=True), business_v13_providers={})
    return SimpleNamespace(action="ai_assets.list", params={}, owner_id=home.owner_id,
        subject_id="local", context=SimpleNamespace(host=host, check=lambda action, facts: None),
        owner_home=home.owner_home_dir, clock=lambda: 1793664000.0,
        idempotency_key="unit-ai-assets-0001", expected_revision=None)


# LLM: 生产客户端原样发送并解析真实信封；不替换HTTP运输，不手造返回 JSON。
# 函数用途: 通过真实 Gateway 调用一个扩展业务动作。
def wire_call(gateway, action, params=None, identity=None, key=None, revision=None):
    from agent_py_agent.agent.gateway_parts import business_api_v13
    from agent_py_agent.agent.plugin_host.business_action_specs import (
        business_schema_bundle,
        standalone_schema,
    )
    from agent_py_agent.agent.plugin_host.schemas import validate_object
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
    from agent_py_agent.cli.chat_client_context import post_gateway_json

    identity = identity or OwnerIdentity.provider_user("feishu", "business-alice")
    payload = {"action": action, "request_id": "w9-" + action, "params": params or {}}
    if key is not None:
        payload["idempotency_key"] = key
    if revision is not None:
        payload["expected_revision"] = revision
    status, body = post_gateway_json(thin_client(gateway, identity), "/client/business", payload, timeout=10.0)
    assert "state" in body, (status, body)
    schema = standalone_schema(business_schema_bundle()["response_envelope_schema"])
    result_schema = business_api_v13.v13_action_spec(action).result_schema
    schema["$defs"].update(result_schema.pop("$defs", {}))
    schema["properties"]["data"] = {"anyOf": [{"$ref": "#/$defs/EmptyData"}, result_schema]}
    envelope = validate_object(body, schema)
    if body["error"] is None:
        business_api_v13.v13_action_spec(action).validate_result(body["data"])
    assert (envelope["error"] is None and status == 200 and envelope["state"] == "completed") or (
        envelope["error"] is not None and status >= 400 and envelope["state"] == "failed"), (status, envelope)
    return {**envelope, "http_status": status, "ok": envelope["error"] is None}


# LLM: 只按可信 owner 解析目录，用 W0 官方种子函数写原集合。
# 函数用途: 将真实测试记录送入指定 owner 的原 v13 存储。
def seed_rows(gateway, collection, rows, identity=None):
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home
    from agent_py_agent.tests import test_v13_w0_harness

    identity = identity or OwnerIdentity.provider_user("feishu", "business-alice")
    owner = resolve_owner_home(gateway.agent.home_paths.root, identity)
    test_v13_w0_harness.v13_seed(owner.home_dir, collection, [{**row, "owner_id": owner.owner_id} for row in rows])
    return owner


# LLM: 模型写入原目录保存入口，测试秘密仅为人工合成字符串。
# 函数用途: 物化一个真实模型服务商和档案。
def seed_model(home, endpoint="https://models.example.test/v1", profile_id="11111111-1111-4111-8111-111111111111"):
    from agent_py_agent.agent.plugin_host.model_settings_v2_handlers import ModelSettingsV2Handlers

    result = ModelSettingsV2Handlers(home).save_provider({"provider_id": "example", "display_name": "Example",
        "api_base": endpoint, "api_key": "TEST_ONLY_KEY_DO_NOT_USE", "custom_headers": {"x-test": "TEST_HEADER_SECRET"},
        "models": [{"profile_id": profile_id, "model_name": "fixture-model", "model_backend": "openai_compatible",
            "model_context_window_tokens": 32768}]})
    assert result["providers"][0]["models"][0]["profile_id"] == profile_id
    return "ai-" + profile_id


# LLM: 用原安装接口保存静态测试包，不启用、不启动插件。
# 函数用途: 在同一临时数据根建立真实插件安装事实。
def seed_plugin(tmp_path):
    from agent_py_agent.tests.test_plugin_management import manager
    from agent_py_agent.tests.test_plugin_package import _bundle

    service, _source = manager(tmp_path)
    service.install_package_bytes(_bundle(), operation_id="w9-test-install-package")
    return "ai-sample-peek"


# LLM: 用原 ConversationStore/ModelUsageStore 保存物理身份，不向产品塞造假的外部服务库存。
# 函数用途: 建立本 owner 原用量账的一个真实事件。
def seed_usage(call):
    from agent_py_agent.agent.conversation.store import ConversationStore

    store = ConversationStore(call.owner_home / "conversations")
    thread = store.threads.create(canonical_user_id="local", owner_id=call.owner_id,
        owner_home=str(call.owner_home), title="资产用量测试")
    store.model_usage.append_once({"thread_id": thread.thread_id, "event_id": "usage-asset-event",
        "request_id": "usage-request", "run_id": "run-assets", "task_id": "task-assets", "source": "test",
        "model_calls": {"schema": "model_call_summary.v1", "physical_model_attempt_count": 1, "backends": ["https://user:TEST_URL_SECRET@usage.example.test/v1?key=TEST_QUERY_SECRET"],
            "backend_attempt_counts": {"https://user:TEST_URL_SECRET@usage.example.test/v1?key=TEST_QUERY_SECRET": 1}},
        "now": call.clock()})
    call.context.host.conversation_store = store
    return store


# LLM: 读取与变更前后核原事实源；发现过程不应创建或改变任何目录内容。
# 函数用途: 验证四类真实来源、风险和敏感字段白名单。
def test_discovery_projects_models_mcp_plugins_usage_with_risk(unit_call, tmp_path):
    host = unit_call.context.host
    external = seed_model(host.home_paths)
    host.config.mcp_servers = {"asset-01": {"command": "fixture-command", "args": ["TEST_ARG_SECRET"],
        "env": {"KEY": "TEST_ENV_SECRET"}}, "remote": {"url": "https://user:TEST_MCP_SECRET@mcp.example.test/rpc?token=TEST_QUERY_SECRET"}}
    plugin = seed_plugin(tmp_path)
    seed_usage(unit_call)
    before = {str(p): p.read_bytes() for p in host.home_paths.root.rglob("*") if p.is_file()}
    rows, complete = assets._discover(unit_call)
    after = {str(p): p.read_bytes() for p in host.home_paths.root.rglob("*") if p.is_file()}
    assert before == after and complete
    by_id = {row["asset_id"]: assets._view(row) for row in rows}
    assert {row["kind"] for row in by_id.values()} == {"model_endpoint", "mcp_server", "agent_plugin", "external_service"}
    assert by_id[external]["risk"]["level"] == "high"
    assert by_id["ai-asset-01"]["risk"]["level"] == "medium"
    assert by_id[plugin]["risk"]["level"] == "unknown"
    assert by_id["ai-usage.example.test"]["endpoint_host"] == "usage.example.test"
    output = json.dumps(by_id)
    for secret in ("TEST_ONLY_KEY_DO_NOT_USE", "TEST_HEADER_SECRET", "TEST_ARG_SECRET", "TEST_ENV_SECRET",
        "TEST_MCP_SECRET", "TEST_QUERY_SECRET", "TEST_URL_SECRET"):
        assert secret not in output
    local_rows = assets._model_rows(unit_call)
    local_rows[0]["endpoint_host"] = "127.0.0.1"
    assert assets._view({**local_rows[0], "asset_id": external})["risk"]["level"] == "low"


# LLM: 冷 owner 的 host.config 可能来自管理员；不把主用户 MCP 当成本用户资产。
# 函数用途: 验证配置 owner 不匹配时不泄漏主宿主声明。
def test_mcp_config_cross_owner_is_not_projected(unit_call):
    unit_call.owner_id = "providers/feishu/users/business-alice"
    unit_call.context.host.config.mcp_servers = {"private-main": {"command": "private-command"}}
    assert assets._mcp_rows(unit_call) == ([], False)


# LLM: 同名不同类型加后缀；后缀再撞原 ID 时所有结果仍唯一且与输入顺序无关。
# 函数用途: 验证原生 ID 碰撞处理稳定，不靠覆盖丢行。
def test_stable_asset_id_collision_and_second_collision():
    rows = [{"native_id": "shared", "kind": "mcp_server"},
        {"native_id": "shared", "kind": "agent_plugin"},
        {"native_id": "shared-mcp_server", "kind": "model_endpoint"}]
    first = assets._asset_ids(rows)
    assert first == assets._asset_ids(list(reversed(rows)))
    assert len({row["asset_id"] for row in first}) == len(rows)
    assert any(row["asset_id"] == "ai-shared-agent_plugin" for row in first)


# LLM: 风险不从名字推导，也不把 sanction 后的外部服务凭空视为低风险。
# 函数用途: 验证风险规则仅由结构化端点和能力触发。
@pytest.mark.parametrize("kind,host,capability,sanctioned,level", [
    ("model_endpoint", "::1", False, False, "low"),
    ("model_endpoint", "192.168.1.4", False, False, "high"),
    ("model_endpoint", "models.example.test", False, True, "unknown"),
    ("mcp_server", None, True, True, "medium"),
    ("agent_plugin", None, False, False, "unknown"),
])
def test_risk_is_structured(kind, host, capability, sanctioned, level):
    row = {"kind": kind, "endpoint_host": host, "network_or_exec": capability, "name": "network exec trusted"}
    assert assets._risk(row, sanctioned)["level"] == level


# LLM: 产品直接调用也必须经过 W0 原 native admin，不用 RBAC roles 或布尔声明替代。
# 函数用途: 验证领域入口不会绕过管理员权威。
def test_sanction_calls_native_admin_before_discovery(unit_call, monkeypatch):
    import sys

    called = []

    # LLM: 测试只替换管理员检查叶，检查拒绝发生在读取目标之前。
    # 函数用途: 记录拒绝并返回原生错误。
    def deny(call):
        called.append(call.owner_id)
        raise HostAPIError("BUSINESS_ACCESS_DENIED", 403)

    module_name = "agent_py_agent.agent.gateway_parts.business_api_v13"
    if module_name in sys.modules:
        monkeypatch.setattr(sys.modules[module_name], "v13_require_admin", deny)
    else:
        monkeypatch.setitem(sys.modules, module_name, SimpleNamespace(v13_require_admin=deny))
    monkeypatch.setattr(assets, "_discover", lambda call: pytest.fail("拒绝后不应发现资产"))
    with pytest.raises(HostAPIError) as error:
        assets.sanction(unit_call)
    assert error.value.code == "BUSINESS_ACCESS_DENIED" and called == [unit_call.owner_id]


# LLM: main 走真实 native admin，普通用户即使有授权动作也不能通过该门。
# 函数用途: 验证示例 sanction、历史、幂等、跨 owner 与不存在目标。
def test_sanction_example_params_admin_only_wire(gateway):
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity

    main = OwnerIdentity.local_main()
    gateway.agent.config.mcp_servers = {"asset-01": {"command": "fixture-command"}}
    params = {"asset_id": "ai-asset-01", "sanctioned": True}
    first = wire_call(gateway, "ai_assets.sanction", params, main, "w9-sanction-example-0001")
    assert (first["http_status"], first["state"], first["error"]) == (200, "completed", None), first
    assert first["data"]["asset"]["sanction_state"] == "sanctioned"
    replay = wire_call(gateway, "ai_assets.sanction", params, main, "w9-sanction-example-0001")
    assert replay["data"] == first["data"]
    conflict = wire_call(gateway, "ai_assets.sanction", {**params, "sanctioned": False}, main, "w9-sanction-example-0001")
    assert (conflict["http_status"], conflict["error"]["code"]) == (409, "IDEMPOTENCY_CONFLICT")
    restricted = wire_call(gateway, "ai_assets.sanction", {**params, "sanctioned": False}, main, "w9-sanction-example-0002")
    assert restricted["data"]["asset"]["sanction_state"] == "restricted"
    denied = wire_call(gateway, "ai_assets.sanction", params, key="w9-sanction-nonadmin-0001")
    assert (denied["http_status"], denied["error"]["code"]) == (403, "BUSINESS_ACCESS_DENIED"), denied
    absent = wire_call(gateway, "ai_assets.sanction", {"asset_id": "ai-absent", "sanctioned": True}, main, "w9-sanction-absent-0001")
    assert (absent["http_status"], absent["error"]["code"]) == (404, "RESOURCE_NOT_FOUND")
    from agent_py_agent.agent.business_spaces.v13_store import V13Store

    store = V13Store.read_only(gateway.agent.home_paths.owner_home_dir)
    decision = store.get("ai_asset_decisions", "ai-asset-01")
    assert decision["revision"] == 2 and len(decision["history"]) == 2


# LLM: 分页使用真实来源快照，决定变化使旧游标失效；过滤不会悄悄返回另一 owner 决定。
# 函数用途: 验证列表过滤、HMAC 游标、owner 隔离和标准信封。
def test_envelopes_and_pages_wire(gateway):
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity

    main = OwnerIdentity.local_main()
    gateway.agent.config.mcp_servers = {"asset-01": {"command": "one"}, "asset-02": {"command": "two"}}
    first = wire_call(gateway, "ai_assets.list", {"limit": 1}, main)
    assert first["ok"] and first["http_status"] == 200 and first["error"] is None, first
    cursor = first["data"]["next_cursor"]
    assert cursor
    second = wire_call(gateway, "ai_assets.list", {"limit": 1, "cursor": cursor}, main)
    assert second["ok"] and second["data"]["items"] != first["data"]["items"]
    for params, identity in (({"cursor": cursor + "x"}, main), ({"cursor": cursor, "kind": "mcp_server"}, main),
        ({"cursor": cursor}, OwnerIdentity.provider_user("feishu", "business-bob"))):
        invalid = wire_call(gateway, "ai_assets.list", params, identity)
        assert (invalid["http_status"], invalid["error"]["code"]) == (410, "CURSOR_EXPIRED"), invalid
    wire_call(gateway, "ai_assets.sanction", {"asset_id": "ai-asset-01", "sanctioned": True}, main, "w9-page-sanction-key")
    stale = wire_call(gateway, "ai_assets.list", {"cursor": cursor}, main)
    assert stale["error"]["code"] == "CURSOR_EXPIRED"
    filtered = wire_call(gateway, "ai_assets.list", {"kind": "mcp_server", "sanction_state": "sanctioned"}, main)
    assert [row["asset_id"] for row in filtered["data"]["items"]] == ["ai-asset-01"]


# LLM: 已存决定不能单独制造资产存在性，也不能跨 owner 投影管理员审批。
# 函数用途: 验证另一 owner 的决定对本 owner 无效。
def test_cross_owner_decisions_wire(gateway):
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity

    gateway.agent.config.mcp_servers = {"asset-01": {"command": "fixture-command"}}
    seed_rows(gateway, "ai_asset_decisions", [{"asset_id": "ai-asset-01", "sanction_state": "sanctioned",
        "decided_by": "other-admin", "reason": "OTHER_OWNER_PRIVATE_REASON", "history": [], "revision": 1,
        "created_at": None, "updated_at": None}])
    result = wire_call(gateway, "ai_assets.list", identity=OwnerIdentity.local_main())
    assert result["ok"], result
    row = next(row for row in result["data"]["items"] if row["asset_id"] == "ai-asset-01")
    assert row["sanction_state"] == "unreviewed" and "OTHER_OWNER_PRIVATE_REASON" not in json.dumps(result)
