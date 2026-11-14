"""真实Gateway HTTP管理API往返：鉴权、结构化来源、未知披露及幂等副作用。"""
from __future__ import annotations

import http.client
import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts import admin_api as api
from agent_py_agent.agent.gateway_parts import admin_api_modules as modules
from agent_py_agent.agent.gateway_parts.http_routes import match_gateway_http_route
from agent_py_agent.tests.test_mega_business_api_actions import native_http as native_http
from agent_py_agent.tests.test_v13_w0_harness import v13_native as v13_native


# LLM: HOME只在临时目录，不让来源缺失测试触碰真实用户配置。
# 函数用途: 为所有HTTP测试隔离配置和磁盘。
@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MY_AGENT_HOME", str(tmp_path / "isolated-home"))


# LLM: 唯一被测响应来自实际Gateway路由与原鉴权，不手写HTTP响应。
# 函数用途: 使用真实HTTP客户端发请求。
def request(native, method, path, body=None, *, admin=True, headers=None):
    server, _ = native
    conn = http.client.HTTPConnection("127.0.0.1", server.server.server_address[1], timeout=10)
    fields = {"X-Gateway-Token": server._local_client_credential, "Content-Type": "application/json"}
    if not admin:
        fields.update({"X-User-Id": "ordinary-fixture", "X-Channel": "feishu"})
    fields.update(headers or {})
    try:
        conn.request(method, path, None if body is None else json.dumps(body).encode(), fields)
        response = conn.getresponse()
        return response.status, json.loads(response.read())
    finally:
        conn.close()


# LLM: 每个读口检查真实信封，partial必须带来源说明。
# 函数用途: 提取成功页的数据。
def get_module(native, name):
    status, body = request(native, "GET", "/api/admin/modules/" + name)
    assert status == 200, body
    assert set(body) == {"ok", "data", "coverage", "unavailable", "generated_at"}
    assert body["ok"] is True and body["generated_at"]
    assert body["coverage"] in {"complete", "partial", "unknown"}
    if body["coverage"] == "partial":
        assert body["unavailable"]
    return body


# LLM: 普通通道用户不能借合法宿主token成为管理员。
# 函数用途: 验证原管理员门与404映射。
def test_non_admin_refused_and_unknown_module_404(native_http):
    status, denied = request(native_http, "GET", "/api/admin/overview", admin=False)
    assert status == 403 and denied["error"] == "forbidden"
    assert request(native_http, "POST", "/api/admin/ops/channel-test", {}, admin=False)[0] == 403
    status, body = request(native_http, "GET", "/api/admin/modules/absent")
    assert status == 404 and body["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert request(native_http, "GET", "/api/admin/overview", headers={"X-Gateway-Token": "wrong"})[0] == 403


# LLM: 激活后不再注入测试专用路由。
# 函数用途: 验证生产路由表的两行。
def test_routes_registered_when_activation_applied():
    for method in ("GET", "POST"):
        route = match_gateway_http_route(method, "/api/admin/overview")
        assert route.handler_module == ".admin_api" and route.access_tier == "admin"


# LLM: 每个失源模块也必须在概览中出现，不能零填充。
# 函数用途: 验证固定十模块概览。
def test_api_admin_overview(native_http):
    status, body = request(native_http, "GET", "/api/admin/overview")
    assert status == 200, body
    rows = body["data"]["modules"]
    assert len(rows) == len(modules.MODULE_READERS) == 10
    assert {row["module"] for row in rows} == set(modules.MODULE_READERS)
    for row in rows:
        if row["status"] == "unavailable":
            assert row["reason_code"] and row["counts"] == {}


# LLM: 缺admin_query不能报会话与任务为零。
# 函数用途: 验证系统失源的可空字段。
def test_api_admin_modules_system(native_http):
    body = get_module(native_http, "system")
    assert body["data"]["gateway"]["pid"] > 0
    assert body["data"]["sessions_total"] is None
    assert body["data"]["tasks"] == {"running": None, "queued": None}
    assert any(row["source"] == "admin_query.sessions" for row in body["unavailable"])


# LLM: 用原模型保存API播种合成凭据，公共HTTP只应出现布尔存在性。
# 函数用途: 验证模型真实目录与秘密脱敏。
def test_api_admin_modules_models(native_http):
    server, _ = native_http
    reader = modules.ModelSettingsV2Handlers(modules.main_home_paths(server))
    reader.save_provider({"provider_id": "fixture-provider", "display_name": "测试服务商",
        "api_base": "https://models.example.invalid/v1", "api_key": "synthetic-secret-value",
        "capabilities": ["agentic"], "models": [{"model_name": "fixture-model", "capability": "agentic", "model_backend": "openai_compatible"}]})
    body = get_module(native_http, "models")
    assert body["data"]["profiles"][0]["model_name"] == "fixture-model"
    provider = body["data"]["providers"][0]
    assert provider["base_url_host"] == "models.example.invalid" and provider["has_secret"] is True
    assert "synthetic-secret-value" not in json.dumps(body)


# LLM: 构造真实冻结模型、策略和runtime，避免伪顶层属性掩盖canonical字段漂移；handler不会执行。
# 函数用途: 绑定仅供目录读取的规范工具对象。
def bind_catalog_tool(native, resolver=None, *, deferred=True):
    from agent_py_agent.agent.tooling.models import (
        EffectResolverPolicy,
        ToolModelHints,
        ToolModelSpec,
        ToolRuntime,
        ToolRuntimePolicy,
    )
    spec = ToolModelSpec(name="fixture_read", description="合成目录工具", input_schema={
        "type": "object", "properties": {"action": {"type": "string"}, "command": {"type": "string"}},
        "additionalProperties": False}, hints=ToolModelHints(category="read", default_deferred=deferred,
        deferred_summary="需要时读取合成状态" if deferred else ""))
    runtime = ToolRuntime(spec, ToolRuntimePolicy(effect_resolver=resolver or EffectResolverPolicy()), handler=None)
    native[1].tools = SimpleNamespace(tools={spec.name: runtime})
    return runtime


# LLM: HTTP读取真实canonical元数据，未默认收起的False也不能误判未知。
# 函数用途: 验证工具元数据白名单与完整覆盖。
@pytest.mark.parametrize("deferred", [True, False])
def test_api_admin_modules_tools(native_http, deferred):
    bind_catalog_tool(native_http, deferred=deferred)
    body = get_module(native_http, "tools")
    assert body["data"]["tools"] == [{"name": "fixture_read", "category": "read", "effect": "read_only", "default_deferred": deferred}]
    assert body["coverage"] == "complete" and body["unavailable"] == []


# LLM: 不给动态策略编造一次调用的effect；三个原生动态入口都需保留默认值并声明动态性。
# 函数用途: 验证参数、组合和命令effect在真实HTTP目录中的披露。
@pytest.mark.parametrize("strategy", ["parameter", "combination", "command"])
def test_tools_dynamic_effect_disclosed(native_http, strategy):
    from agent_py_agent.agent.tooling.models import EffectResolverPolicy
    resolvers = {
        "parameter": EffectResolverPolicy(default_effect="mutating", by_parameter=(("action", (("read", "read_only"),)),)),
        "combination": EffectResolverPolicy(default_effect="mutating", by_parameter_combinations=(((("action", "read"),), "read_only"),)),
        "command": EffectResolverPolicy(default_effect="mutating", strategy="command", command_parameter="command"),
    }
    bind_catalog_tool(native_http, resolvers[strategy])
    body = get_module(native_http, "tools")
    assert body["data"]["tools"][0]["effect"] == "dynamic(default=mutating)"
    assert body["coverage"] == "complete" and body["unavailable"] == []


# LLM: 在真实canonical对象上注入来源损坏，不伪造旧顶层属性；保留工具行且逐列说明未知。
# 函数用途: 验证工具元数据缺失不能静默null或整页500。
def test_tools_missing_metadata_is_unavailable(native_http):
    runtime = bind_catalog_tool(native_http)
    object.__setattr__(runtime.model_spec, "hints", None)
    object.__setattr__(runtime.runtime_policy, "effect_resolver", None)
    body = get_module(native_http, "tools")
    assert body["data"]["tools"] == [{"name": "fixture_read", "category": None, "effect": None, "default_deferred": None}]
    assert body["coverage"] == "partial"
    assert {row["source"] for row in body["unavailable"]} == {
        "agent.tools.fixture_read.category", "agent.tools.fixture_read.effect", "agent.tools.fixture_read.default_deferred"}


# LLM: 任务由原TaskRegistry落盘并经原AdminCrossChannelQuery读取；附加字段模拟未来来源扩展。
# 函数用途: 验证模块与概览的近期活动字段白名单和字符串秘密脱敏。
@pytest.mark.parametrize("path", ["/api/admin/modules/system", "/api/admin/overview"])
def test_system_activity_redacted_and_whitelisted(native_http, tmp_path, monkeypatch, path):
    from agent_py_agent.agent.local_storage import LocalStore
    from agent_py_agent.agent.session.admin_query import AdminCrossChannelQuery
    from agent_py_agent.agent.session.cross_channel import CrossChannelSession
    from agent_py_agent.agent.session.manager import SessionManager
    server, host = native_http
    store = LocalStore(tmp_path / "activity.sqlite3")
    store.task_registry.register_task("activity-task", user_id="admin", status="running",
        goal="检查状态 api_key=synthetic-activity-secret")
    host.config.session_workspace = str(tmp_path / "activity-sessions")
    query = AdminCrossChannelQuery(CrossChannelSession(host.config), SessionManager(host.config), store)
    original = query.get_recent_activity_report
    # LLM: 不替换原查询结果，只给真实任务行附加一个不该公开的未来字段。
    # 函数用途: 检验来源字段增加不会穿透白名单。
    def extended_activity(user_id):
        rows, errors = original(user_id)
        return [{**row, "private_payload": "synthetic-unlisted-value"} for row in rows], errors
    monkeypatch.setattr(query, "get_recent_activity_report", extended_activity)
    server.admin_query = query
    assert "synthetic-activity-secret" in original("admin")[0][0]["goal"]
    status, body = request(native_http, "GET", path)
    assert status == 200, body
    system = body["data"]["system"] if path.endswith("overview") else body["data"]
    activity = system["recent_activity"]
    assert len(activity) == 1 and activity[0]["task_id"] == "activity-task"
    assert "synthetic-activity-secret" not in json.dumps(body)
    assert "synthetic-unlisted-value" not in json.dumps(body)
    assert set(activity[0]) == {"type", "task_id", "status", "goal", "timestamp"}
    assert activity[0]["goal"] == "检查状态 api_key=<redacted>"


# LLM: 通过原审批请求builder和chunk writer播种，HTTP仍调用原pending_tool_approvals，不伪造列表返回。
# 函数用途: 验证待审批公开上限的完整与截断边界。
@pytest.mark.parametrize("count", [100, 101])
def test_pending_approvals_bound_discloses_truncation(native_http, count):
    import time

    from agent_py_agent.agent.contracts.tool_approval import build_tool_approval_request
    from agent_py_agent.agent.gateway_parts.admin_control_service import pending_tool_approvals
    from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
    from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
    from agent_py_agent.agent.gateway_parts.paths import claimed_request_chunk_path
    from agent_py_agent.agent.gateway_parts.stream_writer import write_chunk_event
    from agent_py_agent.agent.tooling.runtime_contracts import ToolCall
    server, host = native_http
    bind_catalog_tool(native_http)
    record = server.paths.processing / "approval-bound.json"
    write_json_file_atomic(record, {"id": "approval-bound", "status": "processing", "turn_phase": "open",
        "user_id": "admin", "lease_started_at": time.time() - 5,
        "conversation": {"channel": "chat", "channel_user_id": "admin", "channel_conversation_id": ""}})
    chunk = claimed_request_chunk_path(record, "approval-bound")
    for index in range(count):
        call = ToolCall(call_id="call-" + str(index), tool_name="fixture_read", arguments={}, source_protocol="native",
            schema_hash="sha256:approval-bound", run_id="approval-run", turn_id="approval-bound", attempt_id="approval-attempt")
        approval = build_tool_approval_request(call, request_id="approval-bound", round_number=1,
            call_index=index, description="合成审批")
        write_chunk_event(chunk, {"kind": "permission_requested", "permission": approval.to_dict()})
    scope = GatewayControlScope(user_id="admin", channel="chat", conversation_id="", all_user_access=True)
    assert len(pending_tool_approvals(host, server.paths, scope)) == count
    body = get_module(native_http, "tools")
    assert body["data"]["pending_approvals"] == [{"request_id": "approval-bound", "tool_name": "fixture_read"}] * 100
    assert body["coverage"] == ("partial" if count > 100 else "complete")
    assert body["unavailable"] == ([{"module": "tools", "source": "pending_tool_approvals.truncated", "reason_code": "HOST_NOT_READY"}]
        if count > 100 else [])


# LLM: 通过规范运行库创建当前attempt，不直接写SQL伪造投影。
# 函数用途: 播种父与子运行链。
def seed_runtime(native, *, exited=True):
    from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
    from agent_py_agent.agent.runtime_db.schema import runtime_db_path
    server, _ = native
    home = modules.main_home_paths(server).owner_home_dir
    home.mkdir(parents=True, exist_ok=True)
    repo = RuntimeRepository(runtime_db_path(home))
    repo.record_run_creation(owner_id="local/main", run_id="root-fixture", thread_id="thread-fixture")
    row = repo.record_run_creation(owner_id="local/main", run_id="child-fixture", parent_run_id="root-fixture")
    if exited:
        from agent_py_agent.agent.runtime_db.executor_liveness import attempt_executor
        with attempt_executor(repo, "child-fixture", row["attempt_id"]):
            pass
    return repo, row


# LLM: 真实退出证据才可回收；缺心跳不自动等于卡死。
# 函数用途: 验证子代理当前attempt投影。
def test_api_admin_modules_subagents(native_http):
    seed_runtime(native_http)
    body = get_module(native_http, "subagents")
    assert [row["run_id"] for row in body["data"]["reclaimable"]] == ["child-fixture"]
    assert body["data"]["reclaimable"][0]["reason"] == "executor_returned_without_result"


# LLM: 未配置memory服务不能报候选数量为零。
# 函数用途: 验证记忆与自学习可空披露。
def test_api_admin_modules_memory(native_http):
    body = get_module(native_http, "memory")
    assert body["data"]["memory_candidates_pending"] is None
    assert body["data"]["learning_requests_pending"] == 0
    assert any(row["source"] == "memory_candidates" for row in body["unavailable"])


# LLM: 读取真实直接Skill文件，层级始终包含四种。
# 函数用途: 验证Skill目录和自学登记投影。
def test_api_admin_modules_skills(native_http):
    server, _ = native_http
    root = modules.main_home_paths(server).owner_home_dir / "skills" / "fixture-skill"
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text("---\nname: fixture-skill\ndescription: 测试用途\n---\n测试内容", encoding="utf-8")
    body = get_module(native_http, "skills")
    layers = {row["layer"]: row for row in body["data"]["layers"]}
    assert set(layers) == {"workspace", "user", "builtin", "learned"}
    assert layers["user"]["skill_count"] == 1


# LLM: 缺绑定调度服务时不可把活跃计划显示为零。
# 函数用途: 验证持久调度失源披露。
def test_api_admin_modules_schedules(native_http):
    body = get_module(native_http, "schedules")
    assert body["data"]["counts"] == {"active": None, "paused": None, "failed_last_run": None}
    assert body["unavailable"]


# LLM: 配置存在性不解析秘密，不发测试消息。
# 函数用途: 验证通道只读投影。
def test_api_admin_modules_channels(native_http):
    _, host = native_http
    host.config.feishu_app_id, host.config.feishu_app_secret = "fixture-app", "synthetic-feishu-secret"
    body = get_module(native_http, "channels")
    row = next(row for row in body["data"]["channels"] if row["channel"] == "feishu")
    assert row["configured"] is True and row["summary"]["app_id_masked"] == "fix***"
    assert "synthetic-feishu-secret" not in json.dumps(body)


# LLM: workspace计数来自真实库，配额未知limit必须null。
# 函数用途: 验证工作区与配额来源。
def test_api_admin_modules_workspace(native_http):
    seed_runtime(native_http)
    body = get_module(native_http, "workspace")
    row = body["data"]["owners"][0]
    assert row["owner_id"] == "local/main" and row["workspace_bindings"] == 0
    assert row["limit_bytes"] is None


# LLM: 审计首页与查询入口共用相同分页读口。
# 函数用途: 验证审计模块信封。
def test_api_admin_modules_audit(native_http):
    body = get_module(native_http, "audit")
    assert set(body["data"]) == {"records", "next_cursor", "coverage"}


# LLM: 用真实事务写审计，再经HTTP查询验证分页和篡改拒绝。
# 函数用途: 验证审计游标、过滤与闭合查询。
def test_api_admin_audit(native_http):
    from agent_py_agent.agent.business_spaces.v13_store import V13Store
    server, _ = native_http
    store = V13Store(modules.main_home_paths(server).owner_home_dir, owner_id="local/main", subject_id="fixture-auditor", action="fixture.write")
    for i in range(3):
        store.transaction("async_jobs", lambda rows: {"saved": True}, idempotency_key=str(i), params_digest=str(i))
    status, first = request(native_http, "GET", "/api/admin/audit?limit=1&actor=fixture-auditor")
    assert status == 200 and len(first["data"]["records"]) == 1, first
    token = first["data"]["next_cursor"]
    from urllib.parse import quote
    assert token
    status, second = request(native_http, "GET", "/api/admin/audit?limit=1&actor=fixture-auditor&cursor=" + quote(token))
    assert status == 200 and second["data"]["records"] != first["data"]["records"]
    status, bad = request(native_http, "GET", "/api/admin/audit?cursor=tampered")
    assert status == 503 and bad["error"]["code"] == "CURSOR_EXPIRED" and bad["error"]["retryable"] is True


# LLM: 所有无效查询都通过真实GET，不能因解析失败降格成默认全量。
# 函数用途: 验证审计参数边界。
@pytest.mark.parametrize("query", ["limit=0", "limit=101", "limit=x", "limit=1&limit=2", "since=2026-11-02", "actor=", "extra=x"])
def test_invalid_audit_query(native_http, query):
    status, body = request(native_http, "GET", "/api/admin/audit?" + query)
    assert status == 400 and body["error"]["code"] == "INVALID_SCHEMA"


# LLM: 不接真实网络，合成adapter只记录原公共send API调用。
# 类用途: 记录测试消息副作用并返回结构化投递结果。
class SyntheticAdapter:
    # LLM: 不读取真实凭据。
    # 函数用途: 保存调用记录和合成结果。
    def __init__(self, delivered=True):
        self.calls = []
        self.delivered = delivered

    # LLM: 与FeishuAdapter公共接口同形，不伪造HTTP响应。
    # 函数用途: 记录一次合成通道发送。
    def send_message(self, user_id, message):
        self.calls.append((user_id, message))
        return self.delivered


# LLM: 使用原注册表装配测试适配器，不替换HTTP、鉴权或运维分派。
# 函数用途: 绑定隔离通道源。
def bind_channel(native, *, delivered=True):
    from agent_py_agent.agent.delivery.registry import ChannelAdapterRegistry
    _, host = native
    host.channel_registry = ChannelAdapterRegistry()
    adapter = SyntheticAdapter(delivered)
    host.channel_registry.register_adapter("feishu", adapter)
    return adapter


# LLM: 真实配额默认有界对账，同键复用原操作回执。
# 函数用途: 验证配额对账和main运维存储。
def test_api_admin_ops_quota_reconcile(native_http):
    from agent_py_agent.agent.user_space.owner_resolver import ensure_owner_home
    server, _ = native_http
    home = modules.main_home_paths(server)
    ensure_owner_home(home.root)
    body = {"owner_id": "local/main", "idempotency_key": "quota-fixture"}
    status, first = request(native_http, "POST", "/api/admin/ops/quota-reconcile", body)
    assert status == 200, first
    assert first["data"]["owner_id"] == "local/main" and isinstance(first["data"]["stats"], dict)
    assert request(native_http, "POST", "/api/admin/ops/quota-reconcile", body)[1]["data"] == first["data"]
    path = home.owner_home_dir / "data/business_v13/admin_ops.json"
    saved = json.loads(path.read_text())
    assert saved["owner_id"] == "local/main" and len(saved["records"]) == 1
    changed = {**body, "owner_id": "local/other"}
    status, conflict = request(native_http, "POST", "/api/admin/ops/quota-reconcile", changed)
    assert status == 409 and conflict["error"]["code"] == "IDEMPOTENCY_CONFLICT"


# LLM: 只允许一次已标明测试的公共send调用，回放不得重发。
# 函数用途: 验证测试消息成功、失败和幂等冲突。
@pytest.mark.parametrize("delivered", [True, False])
def test_api_admin_ops_channel_test(native_http, delivered):
    adapter = bind_channel(native_http, delivered=delivered)
    body = {"channel": "feishu", "target": "ou_synthetic", "idempotency_key": "channel-fixture"}
    status, first = request(native_http, "POST", "/api/admin/ops/channel-test", body)
    assert status == 200 and first["data"]["delivered"] is delivered, first
    assert first["data"]["error_code"] == (None if delivered else "CHANNEL_DELIVERY_FAILED")
    assert request(native_http, "POST", "/api/admin/ops/channel-test", body)[1]["data"] == first["data"]
    assert len(adapter.calls) == 1 and "管理员通道测试" in adapter.calls[0][1].content
    assert adapter.calls[0][1].metadata["delivery_idempotency_key"] == first["data"]["op_id"]
    from agent_py_agent.agent.delivery.registry import ChannelAdapterRegistry
    native_http[1].channel_registry = ChannelAdapterRegistry()
    status, after_port_loss = request(native_http, "POST", "/api/admin/ops/channel-test", body)
    assert status == 200 and after_port_loss["data"] == first["data"]
    status, conflict = request(native_http, "POST", "/api/admin/ops/channel-test", {**body, "target": "ou_different"})
    assert status == 409 and conflict["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert len(adapter.calls) == 1


# LLM: 未配置时不能调用公共send，也不能伪报delivered。
# 函数用途: 验证通道未就绪。
def test_channel_unconfigured_is_host_not_ready(native_http):
    body = {"channel": "feishu", "target": "ou_synthetic", "idempotency_key": "missing-channel"}
    status, response = request(native_http, "POST", "/api/admin/ops/channel-test", body)
    assert status == 503 and response["error"]["code"] == "HOST_NOT_READY"


# LLM: 读口列出的精确current attempt经原cancel API关闭，同键不再执行。
# 函数用途: 验证规范运行库回收主链。
def test_api_admin_ops_reclaim(native_http):
    repo, row = seed_runtime(native_http)
    body = {"kind": "subagent", "target_id": "child-fixture", "reason": "合成执行器已退出", "idempotency_key": "reclaim-fixture"}
    assert get_module(native_http, "subagents")["data"]["reclaimable"]
    status, first = request(native_http, "POST", "/api/admin/ops/reclaim", body)
    assert status == 200 and first["data"]["outcome"] == "reclaimed", first
    assert first["data"]["effect_outcome"] == "committed"
    assert repo.get_attempt(row["attempt_id"])["status"] == "cancelled"
    assert request(native_http, "POST", "/api/admin/ops/reclaim", body)[1]["data"] == first["data"]
    assert get_module(native_http, "subagents")["data"]["reclaimable"] == []


# LLM: 没有退出证据的运行不能因缺心跳被回收。
# 函数用途: 验证活跃与未知目标无副作用。
def test_non_reclaimable_runtime_and_missing_turn_are_not_changed(native_http):
    repo, row = seed_runtime(native_http, exited=False)
    body = {"kind": "subagent", "target_id": "child-fixture", "reason": "", "idempotency_key": "live-fixture"}
    status, response = request(native_http, "POST", "/api/admin/ops/reclaim", body)
    assert status == 200 and response["data"]["outcome"] == "not_stuck", response
    assert response["data"]["effect_outcome"] == "not_started"
    assert repo.get_attempt(row["attempt_id"])["status"] == "running"
    status, missing = request(native_http, "POST", "/api/admin/ops/reclaim", {**body, "kind": "turn", "target_id": "absent-turn", "idempotency_key": "absent-fixture"})
    assert status == 200 and missing["data"]["outcome"] == "not_found"


# LLM: 全局writer拒绝必须早于连admin_ops回执都写入。
# 函数用途: 验证受限执行器无写入、无发送。
def test_writer_guard_precedes_every_side_effect(native_http, monkeypatch):
    from agent_py_agent.agent.common import global_write_guard as guard
    server, _ = native_http
    adapter = bind_channel(native_http)
    home = modules.main_home_paths(server).owner_home_dir
    before = sorted(str(path.relative_to(home)) for path in home.rglob("*")) if home.exists() else []
    # LLM: 只在运维writer门调用期间绑定原生worker身份，不阻断HTTP线程池的设施登记。
    # 函数用途: 经真实writer判断拒绝本次运维副作用。
    def worker_guard(resource):
        previous = guard._restricted_writer
        guard._restricted_writer = lambda: "fixture-worker"
        try:
            guard.require_global_writer(resource)
        finally:
            guard._restricted_writer = previous
    monkeypatch.setattr(api, "require_global_writer", worker_guard)
    status, response = request(native_http, "POST", "/api/admin/ops/channel-test", {
        "channel": "feishu", "target": "ou_synthetic", "idempotency_key": "guarded-fixture"})
    assert status == 503 and response.get("error", {}).get("code") == "HOST_NOT_READY", response
    after = sorted(str(path.relative_to(home)) for path in home.rglob("*")) if home.exists() else []
    assert before == after and adapter.calls == []


# LLM: 无效输入在回执预留和send前闭合拒绝。
# 函数用途: 验证写参数闭合校验。
@pytest.mark.parametrize("body", [{}, {"channel": "other", "idempotency_key": "one"},
    {"channel": "feishu", "idempotency_key": "one", "target": "ou_synthetic", "extra": True},
    {"channel": "feishu", "idempotency_key": "", "target": "ou_synthetic"}])
def test_invalid_operation_body_has_no_side_effect(native_http, body):
    adapter = bind_channel(native_http)
    status, response = request(native_http, "POST", "/api/admin/ops/channel-test", body)
    assert status == 400 and response["error"]["code"] == "INVALID_SCHEMA"
    assert adapter.calls == []


# LLM: 发送后结果落盘失败必须保持未知，重投不能重发。
# 函数用途: 验证副作用与耐久回执间中断。
def test_unknown_after_send_never_repeats(native_http, monkeypatch):
    from agent_py_agent.agent.business_spaces.v13_store import V13Store
    adapter = bind_channel(native_http)
    original = V13Store.transaction
    # LLM: 只注入结果保存阶段故障，预留和send仍运行原实现。
    # 函数用途: 模拟运维副作用后的持久化中断。
    def fail_finish(self, collection, mutate, *, idempotency_key, params_digest, expected_revision=None, receipt_view=None):
        if idempotency_key.startswith("admin-"):
            raise OSError("synthetic storage failure")
        return original(self, collection, mutate, idempotency_key=idempotency_key, params_digest=params_digest,
                        expected_revision=expected_revision, receipt_view=receipt_view)
    monkeypatch.setattr(V13Store, "transaction", fail_finish)
    body = {"channel": "feishu", "target": "ou_synthetic", "idempotency_key": "unknown-fixture"}
    assert request(native_http, "POST", "/api/admin/ops/channel-test", body)[0] == 503
    monkeypatch.setattr(V13Store, "transaction", original)
    status, response = request(native_http, "POST", "/api/admin/ops/channel-test", body)
    assert status == 503 and response["error"]["code"] == "OUTCOME_UNKNOWN"
    assert len(adapter.calls) == 1


# LLM: 规范claim、线程和请求经原store/原atomic writer建立，不伪造控制返回值。
# 函数用途: 验证只有当前实际执行的turn能沿原停止链取消。
@pytest.mark.parametrize("state", ["claimed", "main", "waiting", "closed", "other_user"])
def test_turn_reclaim_requires_current_native_claim(v13_native, state):
    from agent_py_agent.agent.gateway_parts.execution_target import read_gateway_execution_target
    from agent_py_agent.agent.gateway_parts.io import read_json_file_report, write_json_file_atomic
    server, agent = v13_native
    store = agent.conversation_store
    thread = store.threads.get_or_create({"canonical_user_id": "admin", "channel": "chat",
        "channel_conversation_id": "fixture-conversation", "channel_user_id": "admin"})
    payload = {"id": "turn-fixture", "status": "processing", "turn_phase": "closed" if state == "closed" else "open",
        "user_id": "someone-else" if state == "other_user" else "admin", "execution_attempt_id": "attempt-fixture",
        "conversation": {"channel": "chat", "channel_conversation_id": "fixture-conversation", "channel_user_id": "admin"},
        "conversation_cancellation_scope": "detached", "conversation_claim": {"schema_version": "gateway_conversation_claim.v1",
            "request_id": "turn-fixture", "thread_id": thread.thread_id, "task_id": "gateway:turn-fixture"}}
    path = server.paths.processing / "turn-fixture.json"
    if state == "main":
        payload.pop("conversation_cancellation_scope")
    write_json_file_atomic(path, payload)
    if state == "main":
        from agent_py_agent.agent.gateway_parts.request_binding import GatewayTaskBindingWriter
        repo = agent.subagents.runtime_db
        run = repo.record_run_creation(owner_id=agent.home_paths.owner_id, conversation_task_id="turn-task",
            run_id="turn-main", thread_id=thread.thread_id, role="main")
        writer = GatewayTaskBindingWriter(path, "turn-fixture", execution_attempt_id="attempt-fixture")
        assert writer.bind_runtime_authority({"invocation_run_id": "turn-fixture", "task_id": "turn-task",
            "run_id": "turn-main", "agent_run_id": run["agent_run_id"], "attempt_id": run["attempt_id"]})
    if state != "waiting":
        claim = store.claims.acquire({"thread_id": thread.thread_id, "task_id": "gateway:turn-fixture", "reason": "gateway_foreground_turn"})
        assert claim
    if state in {"claimed", "main"}:
        assert read_gateway_execution_target(server.paths, store, thread.thread_id).turn_id == "turn-fixture"
    body = {"kind": "turn", "target_id": "turn-fixture", "reason": "管理员精确回收测试", "idempotency_key": "turn-fixture-key"}
    status, first = request(v13_native, "POST", "/api/admin/ops/reclaim", body)
    assert status == 200, first
    after = read_json_file_report(path).payload
    if state in {"claimed", "main"}:
        assert first["data"]["outcome"] == "reclaimed" and first["data"]["effect_outcome"] == "committed", first
        assert after["cancel_requested"] is True and after["turn_phase"] == "closing"
        if state == "main":
            assert repo.get_attempt(run["attempt_id"])["status"] == "cancelled"
    else:
        assert first["data"]["outcome"] == "not_stuck" and first["data"]["effect_outcome"] == "not_started"
        assert after == payload
    assert request(v13_native, "POST", "/api/admin/ops/reclaim", body)[1]["data"] == first["data"]
    assert request(v13_native, "GET", "/api/admin/overview")[0] == 200, "回收不能停止Gateway本身"


# LLM: 原scheduler实例由测试建立；被测GET不负责物化持久服务。
# 函数用途: 验证调度健康空源可真实返回零而非未知。
def test_schedules_bound_native_repository(native_http):
    from agent_py_agent.agent.scheduler.repository import SchedulerRepository
    server, host = native_http
    host.scheduler_repository = SchedulerRepository(modules.main_home_paths(server).owner_home_dir / "data/scheduler",
        owner_provider="local", owner_kind="main", owner_id="local/main")
    body = get_module(native_http, "schedules")
    assert body["data"] == {"jobs": [], "counts": {"active": 0, "paused": 0, "failed_last_run": 0}}
    assert body["coverage"] == "complete" and body["unavailable"] == []


# LLM: 原生来源异常不能把概览状态变ready，也不能补出零计数。
# 函数用途: 验证源故障披露和所有模块状态行保留。
def test_source_failure_is_not_zero_filled(native_http, monkeypatch):
    # LLM: 只注入目录读故障，保持HTTP与授权真实。
    # 函数用途: 模拟真实目录来源不可读。
    def fail_models(server):
        raise OSError("synthetic source unavailable")
    monkeypatch.setattr(modules, "model_state", fail_models)
    body = get_module(native_http, "models")
    assert body["coverage"] == "partial" and body["unavailable"]
    status, overview = request(native_http, "GET", "/api/admin/overview")
    assert status == 200
    row = next(row for row in overview["data"]["modules"] if row["module"] == "models")
    assert row["status"] == "unavailable" and row["counts"] == {} and row["reason_code"] == "HOST_NOT_READY"


# LLM: 用Event固定副作用窗口；同键并发请求只能看见pending回执，不能再发消息。
# 函数用途: 验证耐久预留在真实HTTP并发中的互斥性。
def test_concurrent_channel_retry_does_not_duplicate_send(native_http, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    adapter = bind_channel(native_http)
    entered, release = threading.Event(), threading.Event()
    original = adapter.send_message
    # LLM: 保留公共API副作用，只在其进入与返回之间制造确定性交错。
    # 函数用途: 暂停第一次合成发送以观察第二个HTTP请求。
    def held_send(user_id, message):
        result = original(user_id, message)
        entered.set()
        assert release.wait(10)
        return result
    monkeypatch.setattr(adapter, "send_message", held_send)
    body = {"channel": "feishu", "target": "ou_synthetic", "idempotency_key": "concurrent-fixture"}
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(request, native_http, "POST", "/api/admin/ops/channel-test", body)
        try:
            assert entered.wait(10)
            status, retry = request(native_http, "POST", "/api/admin/ops/channel-test", body)
            assert status == 503 and retry["error"]["code"] == "OUTCOME_UNKNOWN"
            assert len(adapter.calls) == 1
        finally:
            release.set()
        status, result = first.result(timeout=10)
    assert status == 200 and result["data"]["delivered"] is True
    assert request(native_http, "POST", "/api/admin/ops/channel-test", body)[1]["data"] == result["data"]
    assert len(adapter.calls) == 1


# LLM: 原目录可超过公开页上限；保留有界内容且必须披露截断，不能称完整。
# 函数用途: 验证模型列表上限不隐瞒覆盖缺口。
def test_model_bound_discloses_truncation(native_http):
    server, _ = native_http
    reader = modules.ModelSettingsV2Handlers(modules.main_home_paths(server))
    reader.save_provider({"provider_id": "bounded-provider", "display_name": "有界模型目录",
        "api_base": "https://models.example.invalid/v1", "api_key": "synthetic-bound-secret",
        "capabilities": ["agentic"], "models": [{"model_name": "fixture-" + str(i), "capability": "agentic",
            "model_backend": "openai_compatible"} for i in range(201)]})
    body = get_module(native_http, "models")
    assert len(body["data"]["profiles"]) == 200
    assert len(body["data"]["providers"][0]["models"]) == 200
    assert body["coverage"] == "partial"
    assert any(row["source"] == "model_profiles.truncated" for row in body["unavailable"])
