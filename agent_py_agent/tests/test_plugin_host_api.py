# LLM: 宿主API合同测试只使用临时Owner和测试激活引用，不读真实用户安装或令牌。
# 模块用途: 验证令牌生命周期、公开投影、细粒度源码权限和Broker接线。
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_py_agent.agent import plugin_host_api as api
from agent_py_agent.agent.plugin_manifest import (
    PLUGIN_PACKAGE_SCHEMA_V4,
    PluginManifest,
    PluginPackageError,
)


# LLM: 模拟权威激活引用的最小合同，控制同一测试中发布、换代和撤销。
# 类用途: 提供可改变有效性的测试引用。
class _Ref:
    # LLM: 仅构造测试状态，不登记真实安装。
    # 函数用途: 初始化固定激活与Owner占位信息。
    def __init__(self, activation_id="act-1"):
        self.scope = SimpleNamespace(activation_id=activation_id)
        self.current = activation_id
        self.owner_value = SimpleNamespace(identity="owner-a", plugins_dir=None)

    # LLM: 与真实引用同样在失效时抛错。
    # 函数用途: 读回当前测试激活。
    def require(self):
        if self.current is None:
            raise ValueError("revoked")
        return SimpleNamespace(activation=SimpleNamespace(activation_id=self.current))

    # LLM: 只返回宿主夹具写入的Owner，不受请求体影响。
    # 函数用途: 提供测试用户范围。
    def owner(self):
        return self.owner_value


# LLM: 每例前后清空令牌表，防止测试之间共用会话权限。
# 函数用途: 隔离宿主API全局状态。
@pytest.fixture(autouse=True)
def _reset():
    api.set_host_api_base(None)
    yield
    api.set_host_api_base(None)


# LLM: 令牌生命周期应与Gateway和原激活严格绑定。
# 函数用途: 验证换代、撤销、关闭后令牌均失效。
def test_tokens_only_exist_while_gateway_serves_and_die_with_activation():
    ref = _Ref()
    assert api.issue_host_api_env(ref, "harness") == {}
    api.set_host_api_base("http://127.0.0.1:8420")
    env = api.issue_host_api_env(ref, "harness")
    assert env[api.HOST_API_URL_ENV] == "http://127.0.0.1:8420/plugin-host/query"
    token = env[api.HOST_API_TOKEN_ENV]
    assert api.verify_host_api_token(token).plugin_id == "harness"
    assert api.verify_host_api_token("forged") is None
    ref.current = "act-2"  # 换代
    assert api.verify_host_api_token(token) is None
    ref.current = "act-1"
    assert api.verify_host_api_token(token) is None  # 失效后不复活
    env = api.issue_host_api_env(ref, "harness")
    ref.current = None  # 停用/卸载
    assert api.verify_host_api_token(env[api.HOST_API_TOKEN_ENV]) is None
    env = api.issue_host_api_env(_Ref(), "harness")
    api.set_host_api_base(None)  # Gateway 停止
    assert api.verify_host_api_token(env[api.HOST_API_TOKEN_ENV]) is None


# LLM: 原始会话包含私有正文和路径，输出仅允许公开白名单。
# 函数用途: 验证主题校验与会话字段裁剪。
def test_query_whitelists_topics_and_projects_public_thread_fields(monkeypatch):
    thread = SimpleNamespace(thread_id="t1", title="  很长的标题 " * 20, status="active", updated_at=5.0,
                             compact_generation=2, summary="不应出现", owner_home="/secret")
    store = SimpleNamespace(threads=SimpleNamespace(list_report=lambda limit: ([thread], []), load=lambda tid: thread))
    owner_agent = SimpleNamespace(conversation_store=store)
    monkeypatch.setattr("agent_py_agent.agent.gateway_parts.request_worker._resolve_loaded_request_agent_for_owner",
                        lambda base, identity: owner_agent)
    monkeypatch.setattr(api, "_plugins", lambda owner: [{"plugin_id": "p", "version": "1", "enabled": True, "summary": "s"}])
    monkeypatch.setattr("agent_py_agent.agent.conversation.agent_activity.conversation_agent_activity",
                        lambda agent, store, tid: SimpleNamespace(to_dict=lambda: {"compact_count": 1}))
    grant = api._Grant(_Ref(), "harness")
    result = api.query_host(object(), grant, {"topics": ["threads", "plugins", "activity"], "thread_id": "t1"})
    row = result["threads"][0]
    assert set(row) == {"thread_id", "title", "status", "updated_at", "compact_generation"} and len(row["title"]) <= 60
    assert "不应出现" not in str(result) and "/secret" not in str(result)
    assert result["plugins"][0]["plugin_id"] == "p" and result["activity"]["context"]["compact_count"] == 1
    for bad in ({"topics": ["messages"]}, {"topics": []}, {"topics": ["activity"]}):
        with pytest.raises(ValueError):
            api.query_host(object(), grant, bad)


# LLM: 真实Broker接线不得把read授权升级为写入。
# 函数用途: 验证批量主题、资产读取与写入拒绝。
def test_query_host_supports_broker_topics_and_read_actions(tmp_path, monkeypatch):
    monkeypatch.setattr("agent_py_agent.agent.gateway_parts.request_worker._resolve_loaded_request_agent_for_owner",
                        lambda base, identity: None)
    ref = _Ref()
    ref.owner_value = SimpleNamespace(identity="local/main", owner_id="local/main", home_dir=tmp_path, plugins_dir=None)
    grant = api._Grant(ref, "harness")
    result = api.query_host(object(), grant, {
        "topics": ["assets_summary", "topology", "recent_tasks", "recent_tool_calls", "tool_stats", "workflow_runs"],
    })
    assert result["assets_summary"]["ok"] is True and result["topology"]["ok"] is True
    assert result["tool_stats"]["ok"] is True and result["workflow_runs"]["ok"] is True
    listed = api.query_host(object(), grant, {"action": "assets.list", "payload": {"kind": "all"}})
    assert listed == {"ok": True, "data": []}
    denied = api.query_host(object(), grant, {"action": "assets.save", "payload": {"kind": "script"}})
    assert denied == {"ok": False, "error_code": "BROKER_SCOPE_DENIED"}


# LLM: 仅对临时Owner记录真实验证回执，不依赖宿主在线业务。
# 函数用途: 验证工具调用的遥测关联和脱敏。
def test_runtime_verification_hook_records_tool_call_telemetry(tmp_path):
    from agent_py_agent.agent.observability.telemetry_ledger import ToolTelemetryLedger
    from agent_py_agent.agent.tooling.runtime_contracts import (
        ToolCall,
        ToolResult,
        ToolSuccessFacts,
    )
    from agent_py_agent.agent.verification.runtime import record_tool_verification

    owner_home = tmp_path / "owners" / "local" / "main"
    owner_home.mkdir(parents=True)
    agent = SimpleNamespace(
        root=tmp_path,
        home_paths=SimpleNamespace(owner_home_dir=owner_home, owner_id="local/main"),
        _current_run_params=None,
        _current_request_id="task-runtime-1",
    )
    call = ToolCall(
        call_id="call-rt-1",
        tool_name="read_file",
        arguments={"path": "README.md", "api_key": "sk-secret"},
        source_protocol="native",
        schema_hash="sha256:" + "0" * 64,
        run_id="run-rt-1",
        turn_id="turn-1",
        attempt_id="att-1",
    )
    result = ToolResult.succeeded(call, "hello", facts=ToolSuccessFacts(duration_ms=42))
    record_tool_verification(agent, call, result)
    rows = ToolTelemetryLedger(owner_home, owner_id="local/main").query_tool_calls(task_id="task-runtime-1")
    assert len(rows) == 1
    assert rows[0]["call_id"] == "call-rt-1" and rows[0]["duration_ms"] == 42.0
    assert rows[0]["args_summary"]["api_key"] == "[REDACTED]"


# LLM: 本轮不扩展包声明协议，原read白名单仍然有效。
# 函数用途: 验证原manifest往返及不支持权限拒绝。
def test_manifest_v4_host_api_round_trip():
    payload = {
        "schema_version": PLUGIN_PACKAGE_SCHEMA_V4, "plugin_id": "harness-console", "version": "0.1.0",
        "summary": "控制台", "entry_module": "harness_console", "entry_wheel": "wheels/a.whl",
        "wheels": [{"path": "wheels/a.whl", "sha256": "0" * 64}], "actions": [], "default_action": "",
        "settings_schema": {"type": "object", "properties": {}},
        "tools": [{"name": "open", "description": "打开", "input_schema": {"type": "object", "properties": {}},
                   "requested_effect": "mutating"}],
        "panels": [], "skills": [], "host_api": ["read"],
    }
    manifest = PluginManifest.from_payload(payload)
    assert manifest.host_api == ("read",) and manifest.to_payload()["schema_version"] == PLUGIN_PACKAGE_SCHEMA_V4
    assert PluginManifest.from_payload(manifest.to_payload()) == manifest
    for bad in (["write"], [], ["read", "read"]):
        with pytest.raises(PluginPackageError):
            PluginManifest.from_payload({**payload, "host_api": bad})


# LLM: 使用真实Grant/令牌/宿主查询入口，require替身只提供安装快照，不伪造Broker输出。
# 函数用途: 验证宿主只读插件拿不到脚本正文且显式源码权限可读脱敏正文。
@pytest.mark.parametrize('permissions,allowed', [(('read',), False), (('read', 'assets.source'), True)])
def test_host_asset_source_permissions(tmp_path, permissions, allowed):
    from agent_py_agent.agent.assets.descriptor import AssetDescriptor
    from agent_py_agent.agent.assets.script_store import ScriptStore
    ScriptStore(tmp_path).save_script(AssetDescriptor(id='s', kind='script', name='script'),
                                     'print(42)\npassword="private-secret"')
    ref = _Ref()
    ref.owner_value = SimpleNamespace(home_dir=tmp_path, owner_id='local/main')
    ref.require = lambda: SimpleNamespace(activation=SimpleNamespace(activation_id='act-1'),
                                         manifest=SimpleNamespace(host_api=permissions))
    api.set_host_api_base('http://127.0.0.1:8420')
    env = api.issue_host_api_env(ref, 'harness')
    grant = api.verify_host_api_token(env[api.HOST_API_TOKEN_ENV])
    result = api.query_host(object(), grant, {'action': 'assets.get', 'payload': {
        'kind': 'script', 'id': 's', 'host_api': ['assets.source'], 'include_asset_source': True}})
    assert result['ok'], result
    detail = result['data']
    assert detail['id'] == 's' and detail['name'] == 'script'
    assert 'entrypoint_path' not in detail and str(tmp_path) not in str(detail)
    assert 'private-secret' not in str(detail)
    assert (detail['code'].startswith('print(42)') if allowed else detail['code'] == '')
    assert detail.get('source_redacted', False) is not allowed


# LLM: 缺少源码权限时Skill只公开逻辑引用；直接HTTP处理函数也必须使用同一投影。
# 函数用途: 验证真实宿主请求入口下的Skill正文隐藏与路径相对化。
def test_host_skill_projection_http(tmp_path):
    path = tmp_path / 'skills/k/SKILL.md'
    path.parent.mkdir(parents=True)
    path.write_text('---\nname: k\n---\nprivate skill prompt\n')
    ref = _Ref()
    ref.owner_value = SimpleNamespace(home_dir=tmp_path, owner_id='local/main')
    api.set_host_api_base('http://127.0.0.1:8420')
    env = api.issue_host_api_env(ref, 'harness')
    responses = []
    handler = SimpleNamespace(headers={'X-Plugin-Host-Token': env[api.HOST_API_TOKEN_ENV]},
        client_address=('127.0.0.1', 9000), _send_json=lambda status, data: responses.append((status, data)),
        _read_json=lambda: {'action': 'assets.get', 'payload': {'kind': 'skill', 'id': 'k'}})
    api.handle_plugin_host_query(handler, SimpleNamespace(agent=object(), port=8420))
    status, result = responses[0]
    assert status == 200 and result['ok'] is True
    assert result['data']['body_markdown'] == '' and result['data']['source_redacted'] is True
    assert 'private skill prompt' not in str(result) and str(tmp_path) not in str(result)


# LLM: 真实授权元数据应来自鲜活安装声明，而非客户端正文或默认高权限。
# 函数用途: 锁定Grant公开的最小授权视图。
def test_grant_activation_view():
    ref = _Ref()
    ref.require = lambda: SimpleNamespace(manifest=SimpleNamespace(host_api=('read',)))
    assert api._Grant(ref, 'harness').activation == {'host_api': ('read',)}
