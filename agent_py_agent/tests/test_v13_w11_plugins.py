# ruff: noqa: F811
"""W11 插件动作的真实 /client/business 边界测试。

只种入临时生成的内容声明包，再经真实 HTTP 入口验证目录、启停、配置脱敏与
CAS、幂等回执和连接探测；探测要么返回结构化真实结果，要么明确
TEST_UNSUPPORTED，不出现无证据的成功。
"""

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_py_agent.agent.business_spaces import v13_w11_plugins as plugins
from agent_py_agent.agent.plugin_configuration import PluginConfigureRequest
from agent_py_agent.agent.plugin_install_store import PluginInstallStore
from agent_py_agent.agent.plugin_installation import PluginInstallRequest
from agent_py_agent.agent.plugin_manifest import canonical_plugin_settings
from agent_py_agent.agent.plugin_package import inspect_plugin_package
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home
from agent_py_agent.tests.test_v13_w0_harness import v13_call, v13_native
from scripts.build_capability_package import build_capability_package


# LLM: 插件写入口的安装者就是本机 main；关掉 per-user scoping 才让同一 HTTP 凭据落在该 owner。
# 函数用途: 把每个测试的真实 HTTP 身份固定到隔离夹具的 main owner。
@pytest.fixture(autouse=True)
def _single_owner(v13_native):
    v13_native[1].config.gateway_per_user_owner_scoping = False
    v13_native[1].config.enable_tools = True
    v13_native[1].config.enable_plugins = True
    v13_native[0].auth_middleware.auth_manager.admin_user_id = "business-alice"
    yield


# LLM: schema 同时覆盖显式 password、嵌套 secret 和普通字段，验证递归遮罩不误伤。
SECRET_SCHEMA = {
    "type": "object",
    "properties": {
        "api_key": {"type": "string", "format": "password"},
        "region": {"type": "string", "enum": ["us", "eu"]},
        "auth": {"type": "object", "properties": {"token": {"type": "string"}}},
    },
    "required": ["region"],
}


# LLM: owner 只取隔离夹具的 home root，不把真实宿主目录带进测试。
# 函数用途: 返回测试 owner 的规范 home。
def _owner(agent):
    return resolve_owner_home(agent.home_paths.root, OwnerIdentity.local_main())


# LLM: 包由真实构建脚本生成并重新检查，不手写 installations.json 或伪造 manifest。
# 函数用途: 在 owner 安装表种入一个内容声明包并返回安装记录。
def _install(agent, root, plugin_id="story-content", schema=None):
    source = root / ("pkg-" + plugin_id)
    source.mkdir(parents=True)
    content = b"# Content\n"
    (source / "CAPABILITY.md").write_bytes(content)
    declaration = {
        "plugin_id": plugin_id,
        "version": "1.0",
        "summary": "内容能力示例",
        "capability": {"description": "把长篇材料整理成分镜", "keywords": ["分镜"],
                       "entry_document": "CAPABILITY.md"},
        "files": [{"path": "CAPABILITY.md"}],
        "settings_schema": schema if schema is not None else {
            "type": "object", "properties": {}, "additionalProperties": False},
    }
    output = build_capability_package(declaration, source, root / (plugin_id + ".zip"))
    snapshot = inspect_plugin_package(output.read_bytes())
    store = PluginInstallStore(_owner(agent))
    expected = next((row.revision for row in store.snapshot()
                     if row.manifest.plugin_id == plugin_id), 0)
    return store.install(PluginInstallRequest(snapshot, "seed-install-" + plugin_id, expected, "private"))


# LLM: 配置种入走原 configure 的 schema 校验与 CAS，不直接改安装表文本。
# 函数用途: 为插件保存一份测试配置并返回更新后的安装记录。
def _configure(agent, plugin_id, values, expected_revision=1):
    store = PluginInstallStore(_owner(agent))
    entry = next(row for row in store.snapshot() if row.manifest.plugin_id == plugin_id)
    return store.configure(PluginConfigureRequest(
        plugin_id, entry.package_sha256, "seed-config-" + plugin_id, expected_revision,
        canonical_plugin_settings(values, entry.manifest.settings_schema)))


# LLM: 包源只返回声明结构，测试目录投影而非真实下载或安装。
# 类用途: 模拟宿主绑定的插件包索引。
class _Source:
    # LLM: 只模拟外部目录，不承担安装或授权。
    # 函数用途: 返回一页可安装插件声明。
    def index(self, query, kind, cursor, limit):
        return {"items": [{"package_id": "extra-plugin", "title": "额外插件", "summary": "索引示例",
                           "kind": "plugin", "versions": ["1.0", "1.1"], "latest_version": "1.1",
                           "compliance_frameworks": []}], "next_cursor": None}


# LLM: 真实 HTTP 是验收入口；这里只读取标准信封字段，不按文案猜结论。
# 函数用途: 断言一次成功响应并返回 data。
def _ok(server, action, params, **kwargs):
    user = kwargs.pop("user", "business-alice")
    status, body = v13_call(server, action, params, user=user, **kwargs)
    assert status == 200, body
    assert body["state"] == "completed" and body["error"] is None, body
    return body["data"]


# LLM: 错误核验只认 HTTP 状态与结构化错误码。
# 函数用途: 断言一次失败响应并返回 error 对象。
def _error(server, action, params, status_code, code, **kwargs):
    user = kwargs.pop("user", "business-alice")
    status, body = v13_call(server, action, params, user=user, **kwargs)
    assert status == status_code, body
    assert body["state"] == "failed" and body["error"]["code"] == code, body
    return body["error"]


# LLM: 目录来自原生 catalog 快照；包源缺失时覆盖口径 partial，绑定后才是 complete。
# 函数用途: 验证 plugins.catalog 的原生投影和可选 installable。
def test_catalog_projects_native_catalog_and_optional_installable(v13_native):
    _, agent = v13_native
    _install(agent, agent.home_paths.root / "seeds")
    data = _ok(v13_native, "plugins.catalog", {})
    assert data["coverage"] == "partial" and data["installable"] == []
    assert [item["plugin_id"] for item in data["plugins"]] == ["story-content"]
    view = data["plugins"][0]
    assert view["enabled"] is False and view["installation_revision"] == 1
    assert view["has_config_schema"] is False and view["category"] == "other"

    agent.business_v13_providers["package_source"] = _Source()
    bound = _ok(v13_native, "plugins.catalog", {})
    assert bound["coverage"] == "complete"
    item = bound["installable"][0]
    assert item["package_id"] == "extra-plugin" and item["kind"] == "plugin"
    assert item["versions"] == ["1.0", "1.1"] and item["latest_version"] == "1.1"


# LLM: issue 的示例参数必须原样可用；space_id 只是命名空间提示，不额外读包目录。
# 函数用途: 验证 plugins.list 的示例参数与目录投影。
def test_list_example_params(v13_native):
    _, agent = v13_native
    _install(agent, agent.home_paths.root / "seeds")
    listed = _ok(v13_native, "plugins.list", {"space_id": "soc-default"})
    assert [item["plugin_id"] for item in listed["items"]] == ["story-content"]


# LLM: 启停必须真正改变原生安装 revision 和 enabled，不是返回一个布尔回显。
# 函数用途: 验证 plugins.enable 与 plugins.disable 的原生生命周期。
def test_enable_disable_via_native_lifecycle(v13_native):
    _, agent = v13_native
    _install(agent, agent.home_paths.root / "seeds")
    key = "enable-" + uuid4().hex
    enabled = _ok(v13_native, "plugins.enable", {"plugin_id": "story-content"}, idempotency_key=key)
    assert enabled["plugin"]["enabled"] is True and enabled["plugin"]["installation_revision"] == 2
    assert enabled["plugin"]["activation_id"]

    replay = _ok(v13_native, "plugins.enable", {"plugin_id": "story-content"}, idempotency_key=key)
    assert replay == enabled
    assert next(row.revision for row in PluginInstallStore(_owner(agent)).snapshot()) == 2

    disabled = _ok(v13_native, "plugins.disable", {"plugin_id": "story-content"},
                   idempotency_key="disable-" + uuid4().hex)
    assert disabled["plugin"]["enabled"] is False
    assert disabled["plugin"]["installation_revision"] > enabled["plugin"]["installation_revision"]
    assert next(row.revision for row in PluginInstallStore(_owner(agent)).snapshot()) == \
        disabled["plugin"]["installation_revision"]


# LLM: 同一幂等键不能改换插件；未知插件在触发原生写之前返回 404。
# 函数用途: 验证启停的幂等冲突与缺失资源错误。
def test_enable_idempotency_conflict_and_missing_plugin(v13_native):
    _, agent = v13_native
    _install(agent, agent.home_paths.root / "seeds-a")
    _install(agent, agent.home_paths.root / "seeds-b", plugin_id="second-content")
    key = "enable-conflict-" + uuid4().hex
    _ok(v13_native, "plugins.enable", {"plugin_id": "story-content"}, idempotency_key=key)
    _error(v13_native, "plugins.enable", {"plugin_id": "second-content"}, 409,
           "IDEMPOTENCY_CONFLICT", idempotency_key=key)
    _error(v13_native, "plugins.enable", {"plugin_id": "missing-plugin"}, 404,
           "RESOURCE_NOT_FOUND", idempotency_key="missing-" + uuid4().hex)


# LLM: config_schema 用于前端动态表单；显式与嵌套秘密字段都只回 ***。
# 函数用途: 验证 plugins.config.get 的递归脱敏。
def test_config_get_masks_secret_fields(v13_native):
    _, agent = v13_native
    _install(agent, agent.home_paths.root / "seeds", plugin_id="config-content", schema=SECRET_SCHEMA)
    _configure(agent, "config-content",
               {"api_key": "super-secret", "region": "us", "auth": {"token": "nested-secret"}})

    read = _ok(v13_native, "plugins.config.get", {"plugin_id": "config-content"})
    assert read["config_schema"] == SECRET_SCHEMA and read["revision"] == 2
    assert read["values"] == {"api_key": "***", "region": "us", "auth": {"token": "***"}}
    assert set(read["secret_fields"]) == {"api_key", "auth.token"}
    status, raw = v13_call(v13_native, "plugins.config.get", {"plugin_id": "config-content"},
                           user="business-alice")
    assert status == 200 and "super-secret" not in json.dumps(raw) and "nested-secret" not in json.dumps(raw)


# LLM: 脱敏占位回写时从旧值恢复，普通字段照常更新；CAS 和幂等冲突都由原服务裁决。
# 函数用途: 验证 plugins.config.update 的脱敏保存与 CAS。
def test_config_update_keeps_masked_secrets_and_cas(v13_native):
    _, agent = v13_native
    _install(agent, agent.home_paths.root / "seeds", plugin_id="config-content", schema=SECRET_SCHEMA)
    _configure(agent, "config-content",
               {"api_key": "super-secret", "region": "us", "auth": {"token": "nested-secret"}})

    read = _ok(v13_native, "plugins.config.get", {"plugin_id": "config-content"})
    assert read["revision"] == 2
    assert read["values"] == {"api_key": "***", "region": "us", "auth": {"token": "***"}}
    assert set(read["secret_fields"]) == {"api_key", "auth.token"}
    status, raw = v13_call(v13_native, "plugins.config.get", {"plugin_id": "config-content"},
                           user="business-alice")
    assert status == 200 and "super-secret" not in json.dumps(raw) and "nested-secret" not in json.dumps(raw)

    key = "config-" + uuid4().hex
    updated = _ok(v13_native, "plugins.config.update",
                  {"plugin_id": "config-content",
                   "values": {"api_key": "***", "region": "eu", "auth": {"token": "***"}}},
                  idempotency_key=key, expected_revision=2)
    assert updated["revision"] == 3 and updated["values"]["api_key"] == "***"
    entry = next(row for row in PluginInstallStore(_owner(agent)).snapshot()
                 if row.manifest.plugin_id == "config-content")
    assert json.loads(entry.settings_json) == {
        "api_key": "super-secret", "region": "eu", "auth": {"token": "nested-secret"}}

    _error(v13_native, "plugins.config.update",
           {"plugin_id": "config-content", "values": {"region": "us"}}, 409, "REVISION_CONFLICT",
           idempotency_key="stale-" + uuid4().hex, expected_revision=2)
    _error(v13_native, "plugins.config.update",
           {"plugin_id": "config-content", "values": {"region": "us"}}, 409, "IDEMPOTENCY_CONFLICT",
           idempotency_key=key, expected_revision=2)


# LLM: 没有旧值时 *** 不能被写进配置；未知插件不泄漏存在性细节。
# 函数用途: 验证脱敏占位缺旧值与未知插件的错误映射。
def test_config_update_rejects_placeholder_without_stored_value(v13_native):
    _, agent = v13_native
    _install(agent, agent.home_paths.root / "seeds", plugin_id="fresh-content", schema=SECRET_SCHEMA)
    _error(v13_native, "plugins.config.update",
           {"plugin_id": "fresh-content", "values": {"api_key": "***", "region": "us"}}, 400,
           "INVALID_SCHEMA", idempotency_key="fresh-" + uuid4().hex, expected_revision=1)
    _error(v13_native, "plugins.config.get", {"plugin_id": "missing-plugin"}, 404, "RESOURCE_NOT_FOUND")


# LLM: 内容包没有声明 test/health 动作也没有通道适配器，必须明确 TEST_UNSUPPORTED。
# 函数用途: 验证 plugins.test_connection 不伪造成功。
def test_connection_real_probe_result_or_explicit_unsupported(v13_native):
    _, agent = v13_native
    _install(agent, agent.home_paths.root / "seeds")
    data = _ok(v13_native, "plugins.test_connection", {"plugin_id": "story-content"},
               idempotency_key="probe-" + uuid4().hex)
    assert data["ok"] is False and data["error_code"] == "TEST_UNSUPPORTED"
    assert data["latency_ms"] is None and data["detail"] and data["tested_at"]


# LLM: 声明探测的判定只读原生 state/error_code；命令失败时绝不能返回 ok=True。
# 函数用途: 验证声明的 test/health 结果映射。
def test_declared_probe_uses_structured_outcome():
    call = SimpleNamespace(clock=lambda: 0.0, idempotency_key="probe-key", owner_id="local/main",
                           action="plugins.test_connection", request_id="probe-request", params={}, subject_id="tester")
    entry = SimpleNamespace(manifest=SimpleNamespace(plugin_id="probe-plugin"))
    action = SimpleNamespace(name="health", kind="tool", available=True)

    class _Manager:
        # LLM: helper 单测的合成结果不代表真实探测。
        # 函数用途: 保存合成命令结果。
        def __init__(self, outcome):
            self.outcome = outcome

        # LLM: 目录版本仅用于 helper 单测参数。
        # 函数用途: 返回单测需要的目录版本。
        def catalog(self):
            return SimpleNamespace(revision=1)

        # LLM: 此处不执行插件；真实 MCP 另有独立用例。
        # 函数用途: 返回合成结果供映射单测使用。
        def command(self, text, *, revision, request_id):
            return self.outcome

    success = plugins._declared_probe_result(
        call, _Manager({"state": "succeeded", "ok": True}), entry, action)
    assert success["ok"] is True and success["error_code"] is None and success["latency_ms"] >= 0

    failed = plugins._declared_probe_result(
        call, _Manager({"state": "failed", "ok": False, "error_code": "PLUGIN_PROBE_FAILED"}), entry, action)
    assert failed["ok"] is False and failed["error_code"] == "PLUGIN_PROBE_FAILED"

    contradictory = plugins._declared_probe_result(
        call, _Manager({"state": "succeeded", "ok": False}), entry, action)
    assert contradictory["ok"] is False and contradictory["error_code"] == "PROBE_FAILED"


# LLM: 适配器 send_message 只有明确返回 True 才算送达；False 或异常都必须是失败。
# 函数用途: 验证原生通道适配器探测映射。
def test_adapter_probe_requires_explicit_true():
    call = SimpleNamespace(clock=lambda: 0.0, idempotency_key="adapter-key", owner_id="local/main",
                           action="plugins.test_connection", request_id="adapter-request",
                           params={"plugin_id": "channel-plugin"}, subject_id="business-alice")
    entry = SimpleNamespace(manifest=SimpleNamespace(plugin_id="channel-plugin"))

    class _Adapter:
        # LLM: 只覆盖 helper 返回与异常映射，不代表外部平台投递。
        # 函数用途: 保存合成适配器返回值。
        def __init__(self, outcome):
            self.outcome = outcome

        # LLM: 此处不发外部消息，HTTP 接线另有独立用例。
        # 函数用途: 返回或抛出合成投递结果。
        def send_message(self, target, message):
            if self.outcome == "raise":
                raise OSError("channel down")
            return self.outcome

    delivered = plugins._adapter_probe_result(call, entry, _Adapter(True))
    assert delivered["ok"] is True and delivered["error_code"] is None
    rejected = plugins._adapter_probe_result(call, entry, _Adapter(False))
    assert rejected["ok"] is False and rejected["error_code"] == "CHANNEL_DELIVERY_FAILED"
    broken = plugins._adapter_probe_result(call, entry, _Adapter("raise"))
    assert broken["ok"] is False and broken["error_code"] == "CHANNEL_DELIVERY_FAILED"


# LLM: 所有动作都走同一 V13 信封；成功和失败都必须保留 request_id 与 state，不返回裸 data。
# 函数用途: 验证 plugins.* 的成功与失败信封形状。
def test_envelopes(v13_native):
    _, agent = v13_native
    _install(agent, agent.home_paths.root / "seeds")
    for action, params in (("plugins.catalog", {}), ("plugins.list", {"space_id": "soc-default"}),
                           ("plugins.config.get", {"plugin_id": "story-content"})):
        status, body = v13_call(v13_native, action, params, user="business-alice")
        assert status == 200 and set(body) >= {"request_id", "state", "data", "error"}
        assert body["state"] == "completed" and body["error"] is None
    status, body = v13_call(v13_native, "plugins.enable", {"plugin_id": "missing-plugin"},
                            user="business-alice", idempotency_key="envelope-" + uuid4().hex)
    assert status == 404 and body["state"] == "failed" and body["data"] == {}
    assert body["error"]["code"] == "RESOURCE_NOT_FOUND"


# LLM: V13 管理面写动作必须在 owner 事务提交点重核原生管理员，普通身份不能借已有安装越权。
# 函数用途: 验证非管理员启停请求被末核拒绝且不改安装表。
def test_non_admin_cannot_use_v13_plugin_writes(v13_native):
    _, agent = v13_native
    _install(agent, agent.home_paths.root / "seeds")
    _error(v13_native, "plugins.enable", {"plugin_id": "story-content"}, 403, "BUSINESS_ACCESS_DENIED",
           idempotency_key="non-admin-" + uuid4().hex, user="business-bob")
    assert next(row.revision for row in PluginInstallStore(_owner(agent)).snapshot()) == 1


# LLM: 数组元素与对象节点都可能声明秘密，HTTP 回读及原安装表必须同时核验。
# 函数用途: 验证数组遮罩、占位保留与新增元素缺旧值拒绝。
@pytest.mark.parametrize("field,item_schema,stored,masked,missing", [
    ("entries", {"type": "string", "format": "password"}, ["array-secret"], ["***"], ["***", "***"]),
    ("endpoints", {"type": "object", "properties": {"token": {"type": "string"}}},
     [{"token": "object-secret"}], [{"token": "***"}], [{"token": "***"}, {"token": "***"}]),
    ("entries", {"type": "object", "x-secret": True, "properties": {"value": {"type": "string"}}},
     [{"value": "whole-secret"}], ["***"], ["***", "***"]),
])
def test_array_secrets_http_roundtrip(v13_native, field, item_schema, stored, masked, missing):
    _, agent = v13_native
    schema = {"type": "object", "properties": {field: {"type": "array", "items": item_schema},
                                                   "region": {"type": "string"}}}
    _install(agent, agent.home_paths.root / "seeds", schema=schema)
    _configure(agent, "story-content", {field: stored, "region": "us"})
    read = _ok(v13_native, "plugins.config.get", {"plugin_id": "story-content"})
    assert read["values"] == {field: masked, "region": "us"}
    updated = _ok(v13_native, "plugins.config.update",
                  {"plugin_id": "story-content", "values": {field: masked, "region": "eu"}},
                  idempotency_key="array-save-" + uuid4().hex, expected_revision=2)
    assert updated["values"] == {field: masked, "region": "eu"}
    assert updated["revision"] == 3
    entry = PluginInstallStore(_owner(agent)).snapshot()[0]
    assert json.loads(entry.settings_json) == {field: stored, "region": "eu"}
    _error(v13_native, "plugins.config.update", {"plugin_id": "story-content", "values": {field: missing}},
           400, "INVALID_SCHEMA", idempotency_key="array-missing-" + uuid4().hex, expected_revision=3)
    assert PluginInstallStore(_owner(agent)).snapshot()[0].revision == 3


# LLM: 不支持也是已完成结果，重放不能刷新 tested_at 或忽略异参冲突。
# 函数用途: 验证 unsupported 分支的耐久幂等合同。
def test_unsupported_probe_is_receipted(v13_native):
    _, agent = v13_native
    _install(agent, agent.home_paths.root / "seeds")
    key = "unsupported-" + uuid4().hex
    params = {"plugin_id": "story-content", "target": "first"}
    first = _ok(v13_native, "plugins.test_connection", params, idempotency_key=key)
    replay = _ok(v13_native, "plugins.test_connection", params, idempotency_key=key)
    assert first == replay
    _error(v13_native, "plugins.test_connection", {**params, "target": "second"},
           409, "IDEMPOTENCY_CONFLICT", idempotency_key=key)


# LLM: 配置直连安装 Store 仍必须消费原管理上下文中的总开关与工具策略。
# 函数用途: 验证关闭原开关时配置写入被拒且安装版本不变。
@pytest.mark.parametrize("switch", ["enable_tools", "enable_plugins"])
def test_config_respects_native_switches(v13_native, switch):
    _, agent = v13_native
    _install(agent, agent.home_paths.root / "seeds", schema=SECRET_SCHEMA)
    setattr(agent.config, switch, False)
    _error(v13_native, "plugins.config.update", {"plugin_id": "story-content", "values": {"region": "us"}},
           403, "BUSINESS_ACCESS_DENIED", idempotency_key="switch-" + uuid4().hex, expected_revision=1)
    assert PluginInstallStore(_owner(agent)).snapshot()[0].revision == 1


# LLM: 交错在原安装锁已进入而提交尚未发生时撤权，不能仅在 V13 回执末核拒绝。
# 函数用途: 验证配置真实提交边界重查 HTTP 管理员身份。
def test_config_revocation_before_native_commit(v13_native, monkeypatch):
    server, agent = v13_native
    _install(agent, agent.home_paths.root / "seeds", schema=SECRET_SCHEMA)
    original = PluginInstallStore._update_locked

    # LLM: 固定交错不依靠睡眠；下一步就是原安装表的 prepare 与 commit。
    # 函数用途: 在原生写入临界区内撤销当前 HTTP 管理员。
    def revoke(store, quota, prepare):
        server.auth_middleware.auth_manager.admin_user_id = "another-admin"
        return original(store, quota, prepare)

    monkeypatch.setattr(PluginInstallStore, "_update_locked", revoke)
    _error(v13_native, "plugins.config.update", {"plugin_id": "story-content", "values": {"region": "us"}},
           403, "BUSINESS_ACCESS_DENIED", idempotency_key="revoke-" + uuid4().hex, expected_revision=1)
    assert PluginInstallStore(_owner(agent)).snapshot()[0].revision == 1


# LLM: 工具策略来自 owner 的原配置文件，不能只测全局两个布尔开关。
# 函数用途: 验证原生 plugin_configure 禁用策略在 HTTP 写入口生效。
def test_config_respects_configure_allowed_policy(v13_native):
    from agent_py_agent.agent.user_space.home_layout import home_paths
    from agent_py_agent.agent.user_space.owner_resolver import home_paths_with_owner

    _, agent = v13_native
    _install(agent, agent.home_paths.root / "seeds", schema=SECRET_SCHEMA)
    home = home_paths_with_owner(home_paths(agent.home_paths.root), _owner(agent))
    home.owner_tool_policy_json.write_text(json.dumps({"schema_version": "tool-policy.v1",
                                                      "disabled_tools": ["plugin_configure"]}))
    _error(v13_native, "plugins.config.update", {"plugin_id": "story-content", "values": {"region": "us"}},
           403, "BUSINESS_ACCESS_DENIED", idempotency_key="policy-" + uuid4().hex, expected_revision=1)
    assert PluginInstallStore(_owner(agent)).snapshot()[0].revision == 1


# LLM: 交错固定在原工具已经准备好、execute 尚未发生的位置；不依赖时序概率。
# 函数用途: 验证原生启用执行边界重核 HTTP 管理资格。
def test_lifecycle_revocation_before_native_execute(v13_native, monkeypatch):
    from agent_py_agent.agent.plugin_management import PluginManagement

    server, agent = v13_native
    _install(agent, agent.home_paths.root / "seeds")
    original = PluginManagement._management_tool
    observed = []
    error_mapper = plugins._lifecycle_error

    # LLM: 保存真实原生命令回执，仅用于断言结构化拒绝原因。
    # 函数用途: 观察原工具错误映射而不修改结果。
    def capture(outcome):
        observed.append({key: outcome.get(key) for key in ("state", "error_code", "effect_outcome", "details")})
        return error_mapper(outcome)

    # LLM: 下一层会把这个原工具交给真实执行器，当前请求开始时仍有权限。
    # 函数用途: 在构造原工具之后撤权，验证实际 execute 核验而非请求前核验。
    def revoke(manager, repo, binding, arguments):
        tool = original(manager, repo, binding, arguments)
        server.auth_middleware.auth_manager.admin_user_id = "another-admin"
        return tool

    monkeypatch.setattr(PluginManagement, "_management_tool", revoke)
    monkeypatch.setattr(plugins, "_lifecycle_error", capture)
    status, body = v13_call(v13_native, "plugins.enable", {"plugin_id": "story-content"},
                            user="business-alice", idempotency_key="late-revoke-" + uuid4().hex)
    assert status == 403 and body["state"] == "failed", (body, observed)
    assert body["error"]["code"] == "BUSINESS_ACCESS_DENIED", body
    assert PluginInstallStore(_owner(agent)).snapshot()[0].revision == 1


# LLM: 当前 owner 私有宿主仅通过既有 _complete_host 端口取得；HTTP 组合壳不携带 registry。
# 函数用途: 验证 HTTP 探测使用完整宿主的适配器、失败映射及同键不重复发送。
@pytest.mark.parametrize("delivered", [True, False])
def test_http_adapter_uses_complete_owner_host(v13_native, delivered):
    _, agent = v13_native
    _install(agent, agent.home_paths.root / "seeds")
    sent = []

    # LLM: 这是测试传输接收端，不代表外部平台送达。
    # 类用途: 作为已绑定宿主适配器的可观察测试端点。
    class Receiver:
        # LLM: 只有明确布尔确认才可成为探测成功。
        # 函数用途: 保存真实入口交付的目标和消息。
        def send_message(self, target, message):
            sent.append((target, message))
            return delivered

    receiver = Receiver()
    agent.channel_registry = SimpleNamespace(adapter_for=lambda name: receiver if name == "story-content" else None)
    key = "http-probe-" + uuid4().hex
    params = {"plugin_id": "story-content", "target": "synthetic-target"}
    first = _ok(v13_native, "plugins.test_connection", params, idempotency_key=key)
    replay = _ok(v13_native, "plugins.test_connection", params, idempotency_key=key)
    assert first == replay and first["ok"] is delivered
    assert first["error_code"] == (None if delivered else "CHANNEL_DELIVERY_FAILED")
    assert len(sent) == 1 and sent[0][0] == "synthetic-target"
    assert "连通性测试" in sent[0][1].content
    _error(v13_native, "plugins.test_connection", {**params, "target": "other-target"},
           409, "IDEMPOTENCY_CONFLICT", idempotency_key=key)
    assert len(sent) == 1


# LLM: 声明 test/health 的包经过真实解析与原生命令选择，不向 helper 伪造 outcome。
# 函数用途: 验证停用插件的声明式探测返回原生失败而非 unsupported 或成功。
@pytest.mark.parametrize("action_name", ["test", "health"])
def test_declared_probe_http_native_rejection(v13_native, action_name):
    from agent_py_agent.tests.test_plugin_package import _bundle

    _, agent = v13_native

    # LLM: 使用原包声明形状，仅把公开动作改成明确的探测入口。
    # 函数用途: 为隔离包声明 test/health 动作。
    def declare(payload):
        payload["actions"][0]["name"] = action_name
        payload["actions"][0]["arguments"] = []
        payload["default_action"] = action_name

    package = inspect_plugin_package(_bundle(change=declare))
    PluginInstallStore(_owner(agent)).install(PluginInstallRequest(package, "seed-declared", 0))
    key = "declared-" + uuid4().hex
    params = {"plugin_id": "sample-peek"}
    first = _ok(v13_native, "plugins.test_connection", params, idempotency_key=key)
    assert first["ok"] is False and first["error_code"] == "PLUGIN_NOT_ENABLED", first
    assert first["latency_ms"] >= 0
    assert _ok(v13_native, "plugins.test_connection", params, idempotency_key=key) == first


# LLM: 本地普通账号沿原生命周期登记，不把一个自报的非管理员 namespace 当账号资格。
# 函数用途: 验证已登记本地用户通过原业务分派管理本人私有插件，不一律提升为管理员。
def test_registered_local_user_private_management(v13_native):
    from agent_py_agent.agent.conversation.store import ConversationStore
    from agent_py_agent.agent.gateway_parts.business_api import (
        execute_business,
        local_business_context,
    )
    from agent_py_agent.agent.user_space.home_layout import home_paths
    from agent_py_agent.agent.user_space.local_user_lifecycle import (
        LocalUserLifecycle,
        LocalUserOperation,
    )
    from agent_py_agent.agent.user_space.local_user_store import LocalUserStore
    from agent_py_agent.agent.user_space.owner_resolver import home_paths_with_owner

    _, agent = v13_native
    root = agent.home_paths.root
    LocalUserLifecycle(LocalUserStore(root)).create(
        "private-user", LocalUserOperation("plugin-test", "a" * 64, "private-user-create"),
        password="SyntheticOnly42!")
    owner = resolve_owner_home(root, OwnerIdentity.local_user("private-user"))
    seed = _install(agent, root / "seeds", schema=SECRET_SCHEMA)
    package = inspect_plugin_package(PluginInstallStore(_owner(agent)).package_bytes(seed.installation))
    store = PluginInstallStore(owner)
    store.install(PluginInstallRequest(package, "private-seed", 0))
    host = SimpleNamespace(home_paths=home_paths_with_owner(home_paths(root), owner), config=agent.config,
                           conversation_store=ConversationStore(owner.home_dir / "conversations", initialize=False),
                           business_native_admin=lambda: False)
    context, services = local_business_context(host)
    status, body = execute_business({"action": "plugins.config.update", "request_id": "private-config-request",
                                    "idempotency_key": "private-config-key", "expected_revision": 1,
                                    "params": {"plugin_id": "story-content", "values": {"region": "eu"}}},
                                   context, services)
    assert status == 200 and body["error"] is None and body["data"]["revision"] == 2, body
    status, body = execute_business({"action": "plugins.enable", "request_id": "private-enable-request",
                                    "idempotency_key": "private-enable-key",
                                    "params": {"plugin_id": "story-content"}}, context, services)
    assert status == 200 and body["data"]["plugin"]["enabled"] is True, body
    assert store.snapshot()[0].enabled
    assert PluginInstallStore(_owner(agent)).snapshot()[0].enabled is False


# LLM: 用原打包、原启用确认、真实子进程 MCP 与 HTTP 探测闭环；不替换 manager.command 或探测 outcome。
# 函数用途: 验证声明式 health 工具真实调用、原生拒绝及幂等不重复执行。
def test_declared_health_real_mcp_http(v13_native):
    import sys
    from dataclasses import asdict

    from agent_py_agent.agent.command_arguments import CommandActionSpec
    from agent_py_agent.agent.plugin_management import PluginManagement, plugin_management_context
    from agent_py_agent.agent.user_space.home_layout import home_paths
    from agent_py_agent.agent.user_space.owner_resolver import home_paths_with_owner
    from agent_py_agent.tests.plugin_enable_fixtures import enable_with_confirmation
    from scripts.build_plugin_files_package import build_files_package

    _, agent = v13_native
    root = agent.home_paths.root
    owner = _owner(agent)
    home = home_paths_with_owner(home_paths(root), owner)
    agent.config.plugin_legacy_sandbox_default = False
    home.owner_tool_policy_json.write_text(json.dumps({"schema_version": "tool-policy.v1",
                                                      "permission_mode": "full-access"}))
    source = root / "probe-source"
    source.mkdir()
    counter = root / "probe-calls.txt"
    program = ("#!" + sys.executable + "\n" +
        "import json,sys\nfrom pathlib import Path\n"
        "tools=[{'name':'health','description':'连通性检查','inputSchema':{'type':'object','properties':{},'additionalProperties':False}}]\n"
        "for line in sys.stdin:\n"
        " r=json.loads(line)\n"
        " if 'id' not in r: continue\n"
        " if r['method']=='initialize': result={'protocolVersion':r['params']['protocolVersion'],'capabilities':{'tools':{}},'serverInfo':{'name':'health-test','version':'1'}}\n"
        " elif r['method']=='tools/list': result={'tools':tools}\n"
        " elif r['method']=='tools/call':\n"
        "  with Path(" + repr(str(counter)) + ").open('a') as f: f.write('called\\n')\n"
        "  result={'content':[{'type':'text','text':'healthy'}]}\n"
        " else: raise RuntimeError('unexpected method')\n"
        " print(json.dumps({'jsonrpc':'2.0','id':r['id'],'result':result}),flush=True)\n")
    (source / "probe.py").write_text(program)
    declaration = {"plugin_id": "health-channel", "version": "1.0", "summary": "声明式检查",
                   "entry": {"kind": "executable", "command": "probe.py", "args": []},
                   "files": [{"path": "probe.py", "executable": True}], "platforms": ["any"],
                   "actions": [asdict(CommandActionSpec("health", "连通性检查", (), kind="tool", target="health"))],
                   "default_action": "health", "tools": [{"name": "health", "description": "连通性检查",
                       "requested_effect": "read_only", "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}}],
                   "settings_schema": {"type": "object", "properties": {}}, "skills": []}
    package_path = build_files_package(declaration, source, root / "health.zip")
    package = inspect_plugin_package(package_path.read_bytes())
    PluginInstallStore(owner).install(PluginInstallRequest(package, "health-install", 0))
    manager = PluginManagement(plugin_management_context(owner, home, agent.config, agent.conversation_store.threads,
        actor_id="business-alice", channel="seed", conversation_id="health-enable", is_admin=True))
    enable_with_confirmation(manager, "health-channel")
    assert not counter.exists()
    key = "real-health-" + uuid4().hex
    params = {"plugin_id": "health-channel"}
    first = _ok(v13_native, "plugins.test_connection", params, idempotency_key=key)
    assert first["ok"] is True and first["error_code"] is None, first
    assert counter.read_text().splitlines() == ["called"]
    assert _ok(v13_native, "plugins.test_connection", params, idempotency_key=key) == first
    assert counter.read_text().splitlines() == ["called"]
    _error(v13_native, "plugins.test_connection", {**params, "target": "changed"}, 409,
           "IDEMPOTENCY_CONFLICT", idempotency_key=key)


# LLM: 发送后回执保存中断必须维持 unknown，重送不能再次发送。
# 函数用途: 验证两阶段回执的失败单调性与防重复副作用。
def test_probe_receipt_interruption_does_not_resend(v13_native, monkeypatch):
    from agent_py_agent.agent.business_spaces.v13_store import V13Store

    _, agent = v13_native
    _install(agent, agent.home_paths.root / "seeds")
    sent = []

    # LLM: 接收端只记录调用次数，不代表外网平台。
    # 函数用途: 记录一次明确发送并确认测试接收。
    def send(target, message):
        sent.append(target)
        return True

    agent.channel_registry = SimpleNamespace(adapter_for=lambda name: SimpleNamespace(send_message=send))
    original = V13Store.transaction

    # LLM: 精确在副作用已完成后阻止回执提交，不修改或伪造原生发送结果。
    # 函数用途: 模拟最终结果回执写入失败。
    def fail_finish(store, collection, mutate, *, idempotency_key, params_digest,
                    expected_revision=None, receipt_view=None):
        if collection == "plugin_operations" and idempotency_key.startswith("finish-"):
            raise OSError("synthetic receipt failure")
        return original(store, collection, mutate, idempotency_key=idempotency_key,
                        params_digest=params_digest, expected_revision=expected_revision, receipt_view=receipt_view)

    key = "interrupted-" + uuid4().hex
    params = {"plugin_id": "story-content", "target": "synthetic-target"}
    monkeypatch.setattr(V13Store, "transaction", fail_finish)
    _error(v13_native, "plugins.test_connection", params, 503, "OUTCOME_UNKNOWN", idempotency_key=key)
    monkeypatch.setattr(V13Store, "transaction", original)
    _error(v13_native, "plugins.test_connection", params, 503, "OUTCOME_UNKNOWN", idempotency_key=key)
    assert sent == ["synthetic-target"]
