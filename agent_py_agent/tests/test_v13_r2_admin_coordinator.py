"""无业务Agent的真实协调器HTTP管理回归；只使用隔离home和原生存储。"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts import admin_api as api
from agent_py_agent.agent.gateway_parts import admin_api_modules as modules
from agent_py_agent.tests import test_v13_w0_admin_api as admin_tests


# LLM: 原C lease、controller、HTTP listener及鉴权均真实，不替换writer角色，不创建业务Agent。
# 函数用途: 启动只有控制面和规范owner配置的真实HTTP服务。
@pytest.fixture
def coordinator_http(tmp_path, monkeypatch):
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.gateway_parts.control_plane import GatewayControlPlane
    from agent_py_agent.agent.gateway_parts.http_service import (
        GatewayHTTPServer,
        GatewayHTTPServerParams,
    )
    from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import coordinator_guard
    from agent_py_agent.agent.gateway_parts.multiprocess_drain_runtime import GatewayDrainRuntime
    from agent_py_agent.agent.gateway_parts.multiprocess_takeover_runtime import (
        prepare_coordinator_takeover,
    )
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.agent.settings import AgentConfig
    from agent_py_agent.agent.user_space.home_layout import home_paths
    from agent_py_agent.agent.user_space.owner_resolver import (
        OwnerIdentity,
        ensure_owner_home,
        home_paths_with_owner,
        resolve_owner_home,
    )

    # LLM: 补丁只拒绝业务Agent物化，不替代控制面、模型读写或管理HTTP结果。
    # 函数用途: 确保冷管理路径没有偷偷创建完整Agent。
    def forbid_agent(self, *args, **kwargs):
        raise AssertionError("coordinator administration must not construct a business Agent")

    monkeypatch.setattr(SimpleAgent, "__init__", forbid_agent)
    root = (tmp_path / "coordinator-home").resolve()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MY_AGENT_HOME", str(root))
    ensure_owner_home(root)
    home = home_paths_with_owner(home_paths(root), resolve_owner_home(root, OwnerIdentity.local_main()))
    config = AgentConfig(my_agent_home=str(root), business_spaces_enabled=True, enable_tools=False)
    settings = modules.ModelSettingsV2Handlers(home)
    settings.save_provider({"provider_id": "r5-provider", "display_name": "协调器回归服务商",
        "api_base": "https://models.example.invalid/v1", "api_key": "synthetic-r5-secret",
        "capabilities": ["agentic"], "models": [{"model_name": "r5-persisted-model",
            "capability": "agentic", "model_backend": "openai_compatible"}]})
    paths = gateway_paths_from_root(root / "gateway")
    workspace = tmp_path / "workspace"
    with coordinator_guard(paths.root, 2):
        controller = prepare_coordinator_takeover(paths, 2, "r5-admin-coordinator")
        control = GatewayControlPlane(config, root, paths, 2, controller.c_run_id,
            agent_root=workspace, workspace_roots=(workspace,))
        runtime = GatewayDrainRuntime(control)
        server = GatewayHTTPServer(0, paths, params=GatewayHTTPServerParams(
            control_plane=control, drain_runtime=runtime))
        server.start()
        assert server.agent is None
        try:
            yield SimpleNamespace(server=server, control=control, home=home, settings=settings)
            assert server.agent is None
        finally:
            server.stop()


# LLM: 复用原真实HTTP客户端，所有响应仍来自生产路由和实时管理员鉴权。
# 函数用途: 向当前协调器服务发送管理员请求。
def request(case, method, path, body=None, *, admin=True, headers=None):
    return admin_tests.request((case.server, None), method, path, body, admin=admin, headers=headers)


# LLM: 不调用待修的路径helper播种；检查已持久化档案，而非config上的合成模型列表。
# 函数用途: 验证无Agent时模型模块仍读取规范目录并脱敏。
def test_coordinator_models_http_reads_persisted_profile(coordinator_http):
    case = coordinator_http
    status, body = request(case, "GET", "/api/admin/modules/models")
    assert status == 200, body
    assert body["coverage"] == "complete" and body["unavailable"] == []
    profiles = case.settings.business_profiles({"limit": 100})["items"]
    assert body["data"]["profiles"] == profiles
    assert any(row["model_name"] == "r5-persisted-model" for row in profiles)
    assert body["data"]["providers"] == [{"provider_id": "r5-provider", "kind": "openai_compatible",
        "base_url_host": "models.example.invalid", "has_secret": True, "models": ["r5-persisted-model"]}]
    assert "synthetic-r5-secret" not in json.dumps(body)
    assert body["data"]["routing"] == {key: getattr(case.control.config, key) for key in (
        "model_routing_enabled", "default_model_profile_id", "decision_model_profile_id", "embedding_model_profile_id")
        if hasattr(case.control.config, key)}


# LLM: 仅观察原reconcile调用，真实扫描和账本写入不替换；同键回放必须复用原op_id及结果。
# 函数用途: 验证协调器额度对账和原运维回执真正连通。
def test_coordinator_quota_http_reconciles_and_replays(coordinator_http, monkeypatch):
    from agent_py_agent.agent.user_space import owner_quota

    case, observed = coordinator_http, []
    original = owner_quota.reconcile_owner_quota_slice

    # LLM: 原函数完整执行后只记录其参数和真实返回值。
    # 函数用途: 捕获额度对账实际结果用于HTTP逐字段比对。
    def observe(home):
        result = original(home)
        observed.append((home, result))
        return result

    monkeypatch.setattr(owner_quota, "reconcile_owner_quota_slice", observe)
    body = {"owner_id": "local/main", "idempotency_key": "r5-quota-reconcile"}
    status, first = request(case, "POST", "/api/admin/ops/quota-reconcile", body)
    assert status == 200, first
    assert len(observed) == 1 and observed[0][0] == case.home.owner_home_dir
    data = first["data"]
    assert data["stats"] == observed[0][1]
    assert data["owner_id"] == "local/main" and data["op_id"].startswith("admin-")
    assert data["reconciled"] is (not data["reconcile_pending_after"])
    status, replay = request(case, "POST", "/api/admin/ops/quota-reconcile", body)
    assert status == 200 and replay["data"] == data and len(observed) == 1
    saved = json.loads((case.home.owner_home_dir / "data/business_v13/admin_ops.json").read_text())
    assert set(saved["records"]) == {data["op_id"]}
    assert saved["records"][data["op_id"]]["outcome"] == data
    status, conflict = request(case, "POST", "/api/admin/ops/quota-reconcile", {**body, "owner_id": "local/missing"})
    assert status == 409 and conflict["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert len(observed) == 1


# LLM: 无执行端口时仍返回可用配置；缺源必须有稳定unavailable，不能把整页报503或补零。
# 函数用途: 验证协调器系统和执行依赖模块的覆盖边界。
def test_coordinator_optional_ports_http_are_explicit(coordinator_http):
    case = coordinator_http
    status, body = request(case, "GET", "/api/admin/modules/system")
    assert status == 200, body
    assert body["data"]["business_spaces_enabled"] is True
    assert body["data"]["model_configured"] is True
    assert body["data"]["gateway"]["multiprocess"] is True
    assert body["data"]["sessions_total"] is None
    assert body["data"]["tasks"] == {"running": None, "queued": None}
    assert body["coverage"] == "partial" and body["unavailable"]
    for module, source in [("tools", "agent.tools"), ("schedules", "scheduler_repository")]:
        status, body = request(case, "GET", "/api/admin/modules/" + module)
        assert status == 200 and body["coverage"] == "partial", body
        assert {"module": module, "source": source, "reason_code": "HOST_NOT_READY"} in body["unavailable"]
    status, body = request(case, "GET", "/api/admin/modules/channels")
    assert status == 200 and body["coverage"] == "partial", body
    status, body = request(case, "GET", "/api/admin/overview")
    assert status == 200, body
    model = next(row for row in body["data"]["modules"] if row["module"] == "models")
    assert model["status"] == "ready" and body["data"]["system"]["model_configured"] is True


# LLM: 初次请求和已完成回执的重放都保留原管理员门，普通成员和坏凭据均不能写账。
# 函数用途: 验证真实HTTP鉴权不会因控制面回退或幂等回执而绕过。
def test_coordinator_http_denied_admin_and_replay(coordinator_http):
    case = coordinator_http
    body = {"owner_id": "local/main", "idempotency_key": "r5-admin-replay"}
    for method, path, payload in [("GET", "/api/admin/modules/models", None),
            ("POST", "/api/admin/ops/quota-reconcile", body)]:
        status, response = request(case, method, path, payload, admin=False)
        assert status == 403 and response["error"] == "forbidden"
        assert request(case, method, path, payload, headers={"X-Gateway-Token": "synthetic-wrong-token"})[0] == 403
    ledger = case.home.owner_home_dir / "data/business_v13/admin_ops.json"
    assert not ledger.exists()
    status, first = request(case, "POST", "/api/admin/ops/quota-reconcile", body)
    assert status == 200, first
    before = ledger.read_bytes()
    assert request(case, "POST", "/api/admin/ops/quota-reconcile", body, admin=False)[0] == 403
    assert ledger.read_bytes() == before
    status, replay = request(case, "POST", "/api/admin/ops/quota-reconcile", body)
    assert status == 200 and replay["data"] == first["data"]


# LLM: 没有执行适配器是进入前不可用，不应留下已执行不确定的运维回执。
# 函数用途: 验证冷协调器缺可选发送端口时拒绝且不预留操作。
def test_coordinator_missing_execution_port_is_not_entered(coordinator_http):
    case = coordinator_http
    body = {"channel": "feishu", "target": "synthetic-target", "idempotency_key": "r5-no-channel"}
    for _ in range(2):
        status, response = request(case, "POST", "/api/admin/ops/channel-test", body)
        assert status == 503 and response["error"]["code"] == "HOST_NOT_READY", response
        assert not (case.home.owner_home_dir / "data/business_v13/admin_ops.json").exists()
    status, response = request(case, "POST", "/api/admin/ops/reclaim", {
        "kind": "turn", "target_id": "synthetic-turn", "reason": "隔离回归", "idempotency_key": "r5-no-turn-port"})
    assert status == 503 and response["error"]["code"] == "HOST_NOT_READY", response
    assert not (case.home.owner_home_dir / "data/business_v13/admin_ops.json").exists()


# LLM: 可信基座失源在执行前拒绝；恢复同一原control后，原键仍可首次执行。
# 函数用途: 验证未进入与已执行未知的错误分类不混淆。
def test_coordinator_missing_trusted_base_is_retryable_before_entry(coordinator_http):
    case = coordinator_http
    body = {"owner_id": "local/main", "idempotency_key": "r5-missing-base"}
    case.server.control_plane = None
    try:
        status, response = request(case, "POST", "/api/admin/ops/quota-reconcile", body)
        assert status == 503 and response["error"]["code"] == "HOST_NOT_READY", response
        assert not (case.home.owner_home_dir / "data/business_v13/admin_ops.json").exists()
    finally:
        case.server.control_plane = case.control
    assert request(case, "POST", "/api/admin/ops/quota-reconcile", body)[0] == 200


# LLM: 故障发生在真实对账函数返回后；已进入的未知结果必须耐久保存，回放不能再执行。
# 函数用途: 验证协调器仍保留副作用后的未知回执和原操作身份。
def test_coordinator_uncertain_after_entry_is_persisted_without_repeat(coordinator_http, monkeypatch):
    from agent_py_agent.agent.user_space import owner_quota

    case, calls = coordinator_http, []
    original = owner_quota.reconcile_owner_quota_slice

    # LLM: 原始扫描确实运行后才注入结果返回故障，不伪造执行结果。
    # 函数用途: 模拟对账已发生但调用方无法确认完整返回。
    def fail_after_entry(home):
        original(home)
        calls.append(home)
        raise OSError("synthetic post-entry failure")

    monkeypatch.setattr(owner_quota, "reconcile_owner_quota_slice", fail_after_entry)
    body = {"owner_id": "local/main", "idempotency_key": "r5-uncertain-quota"}
    for _ in range(2):
        status, response = request(case, "POST", "/api/admin/ops/quota-reconcile", body)
        assert status == 503 and response["error"]["code"] == "OUTCOME_UNKNOWN", response
    assert calls == [case.home.owner_home_dir]
    saved = json.loads((case.home.owner_home_dir / "data/business_v13/admin_ops.json").read_text())
    assert len(saved["records"]) == 1
    assert next(iter(saved["records"].values()))["outcome"] == {"_error": "OUTCOME_UNKNOWN"}


# LLM: 本次管理接口不接受客户端文件根；未知owner仅按规范LocalUserStore查找，不把owner字符串拼成路径。
# 函数用途: 验证请求根和路径形owner都不能替换可信控制面目录。
def test_coordinator_http_rejects_request_filesystem_roots(coordinator_http):
    case = coordinator_http
    status, response = request(case, "GET", "/api/admin/modules/models?home_root=/synthetic/elsewhere")
    assert status == 400 and response["error"]["code"] == "INVALID_SCHEMA"
    status, response = request(case, "POST", "/api/admin/ops/quota-reconcile", {
        "owner_id": "local/main", "home_root": "/synthetic/elsewhere", "idempotency_key": "r5-forged-root"})
    assert status == 400 and response["error"]["code"] == "INVALID_SCHEMA"
    status, response = request(case, "POST", "/api/admin/ops/quota-reconcile", {
        "owner_id": "/synthetic/elsewhere", "idempotency_key": "r5-forged-owner"})
    assert status == 404 and response["error"]["code"] == "RESOURCE_NOT_FOUND", response


# LLM: 执行器原global writer门先于main运维账本，不因存在可信control而放宽角色。
# 函数用途: 验证协调器回退仍拒绝受限writer，且不预留运维操作。
def test_coordinator_writer_guard_precedes_reservation(coordinator_http, monkeypatch):
    from agent_py_agent.agent.common import global_write_guard as guard

    case = coordinator_http

    # LLM: 只在原writer判断内绑定原受限事实，不阻断监听器已存在的线程设施。
    # 函数用途: 用实际worker拒绝门检查本次管理副作用。
    def worker_guard(resource):
        with monkeypatch.context() as scoped:
            scoped.setattr(guard, "_restricted_writer", lambda: "r5-worker")
            guard.require_global_writer(resource)

    monkeypatch.setattr(api, "require_global_writer", worker_guard)
    status, response = request(case, "POST", "/api/admin/ops/quota-reconcile", {
        "owner_id": "local/main", "idempotency_key": "r5-worker-denied"})
    assert status == 503 and response["error"]["code"] == "HOST_NOT_READY", response
    assert not (case.home.owner_home_dir / "data/business_v13/admin_ops.json").exists()


# LLM: 配置字段相同的控制面副本不能替代原C来源；恢复原对象前不得预留或对账。
# 函数用途: 验证冷管理写入保留原生控制面同一性，而不靠home_root字段自证写权。
def test_coordinator_copied_control_cannot_write(coordinator_http):
    from copy import copy

    case = coordinator_http
    body = {"owner_id": "local/main", "idempotency_key": "r5-original-control"}
    case.server.control_plane = copy(case.control)
    try:
        status, response = request(case, "POST", "/api/admin/ops/quota-reconcile", body)
        assert status == 503 and response["error"]["code"] == "HOST_NOT_READY", response
        assert not (case.home.owner_home_dir / "data/business_v13/admin_ops.json").exists()
    finally:
        case.server.control_plane = case.control
    assert request(case, "POST", "/api/admin/ops/quota-reconcile", body)[0] == 200
