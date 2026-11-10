"""W5：真实客户端、HTTP 业务路由、领域 CAS 与冻结结果合同。"""
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.http_service import (
    GatewayHTTPServer,
    GatewayHTTPServerParams,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.plugin_host.business_action_specs import business_action_spec
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.cli.chat_client_context import GatewayChatClientAgent
from agent_py_agent.cli.space_commands import business_client_for_agent
from agent_py_agent.tests.test_model_profiles import add


# LLM: 只创建隔离宿主；真实存储和业务端口不替换，外部模型由调用测试显式提供。
# 函数用途: 为线缆与后台消费测试建立同源宿主。
def native_host(tmp_path):
    return SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"),
        business_spaces_enabled=True, enable_tools=False, prompt_files=[]), tmp_path / "workspace")


# LLM: 原认证、客户端序列化、服务端校验及客户端解析全部运行；监听只在回环临时端口。
# 函数用途: 为测试提供真实业务客户端并在退出时关闭本测试服务。
@pytest.fixture
def business_wire(tmp_path):
    agent = native_host(tmp_path)
    server = GatewayHTTPServer(0, gateway_paths_from_root(agent.home_paths.root / "gateway"),
        params=GatewayHTTPServerParams(agent=agent))
    server.start()
    thin = GatewayChatClientAgent(SimpleNamespace(), SimpleNamespace(
        gateway_port=server.server.server_address[1], gateway_auth_token=""), tmp_path, [tmp_path],
        SimpleNamespace(root=agent.home_paths.root), owner_identity=OwnerIdentity.local_main())
    try:
        yield agent, business_client_for_agent(thin)
    finally:
        server.stop()


# LLM: 请求经过真实客户端与结果合同；非成功回执必须在调用处明确断言。
# 函数用途: 发送一次合法业务调用并返回完成 DTO。
def completed(client, action, params, **options):
    result = client.send(client.prepare(action, params, **options))
    assert result["http_status"] == 200 and result["state"] == "completed", result
    assert result["ok"] is True and result["error"] is None, result
    business_action_spec(action).validate_result(result["data"])
    return result["data"]


# LLM: 两个档案只写本测试目录，不发模型请求；后台身份由真实 create 产生。
# 函数用途: 准备同一助手的两份模型选择。
@pytest.fixture
def model_case(business_wire):
    host, client = business_wire
    first, _ = add(host, model_name="w5-model-A")
    second, _ = add(host, model_name="w5-model-B")
    space = completed(client, "spaces.create", {"name": "模型采纳核验"})
    view = completed(client, "background_agents.create", {
        "space_id": space["space_id"], "name": "worker", "model_profile_id": first})["agent"]
    return host, client, view, first, second


@pytest.mark.parametrize("running", [False, True])
def test_model_set_wire_reports_real_claim_adoption(model_case, running):
    host, client, view, _first, second = model_case
    store, agent_id = host.conversation_store, view["agent_id"]
    claim = store.claims.acquire({"thread_id": agent_id, "reason": "session_task", "task_id": ""}) if running else None
    if running:
        assert claim and claim["status"] == "running"
    before = completed(client, "background_agents.model.get", {"agent_id": agent_id})
    assert before["effective_at"] == ("next_safe_turn" if running else "current")
    prepared = client.prepare("background_agents.model.set", {"agent_id": agent_id, "model_profile_id": second},
        expected_revision=before["selection_revision"])
    try:
        result = client.send(prepared)
        assert result["http_status"] == 200 and result["state"] == "completed", result
        assert result["ok"] is True and result["effect_outcome"] == "committed", result
        business_action_spec("background_agents.model.set").validate_result(result["data"])
        assert result["data"] == {"selection_revision": before["selection_revision"] + 1,
            "adoption_status": "pending_safe_boundary" if running else "applied"}
        selected = store.threads.require(agent_id)
        assert selected.model_profile_id == second
        assert selected.model_selection_revision == result["data"]["selection_revision"]
        # 同一已准备请求仍走原幂等回执，不再增加选择修订。
        replay = client.send(prepared)
        assert replay["ok"] and replay["http_status"] == 200, replay
        assert replay["data"]["selection_revision"] == selected.model_selection_revision
    finally:
        if claim:
            store.claims.finish({"thread_id": agent_id, "claim_id": claim["claim_id"], "status": "finished"})


def test_same_profile_is_unchanged_but_preserves_existing_mutation_revision(model_case):
    host, client, view, first, _second = model_case
    agent_id = view["agent_id"]
    before = host.conversation_store.threads.require(agent_id)
    result = completed(client, "background_agents.model.set", {"agent_id": agent_id, "model_profile_id": first},
        expected_revision=before.model_selection_revision)
    assert result["adoption_status"] == "unchanged"
    # 原 _mutate 对相同档案也增加修订；不能为了 DTO 的 unchanged 取消 CAS 或原幂等路径。
    assert result["selection_revision"] == before.model_selection_revision + 1
    assert host.conversation_store.threads.require(agent_id).model_profile_id == first


def test_stale_model_revision_keeps_original_selection(model_case):
    host, client, view, first, second = model_case
    agent_id = view["agent_id"]
    before = host.conversation_store.threads.require(agent_id)
    result = client.send(client.prepare("background_agents.model.set", {"agent_id": agent_id, "model_profile_id": second},
        expected_revision=before.model_selection_revision + 10))
    # 原运输层曾将领域 ValueError 包成 OUTCOME_UNKNOWN；BIZ-006（6b2159e05）后写入前确定的版本冲突是结构化拒绝：
    # 409 REVISION_CONFLICT / not_started，且选择、版本与元数据都不变（下列断言保留）。
    assert result["ok"] is False and result["http_status"] == 409, result
    assert result["error"]["code"] == "REVISION_CONFLICT" and result["effect_outcome"] == "not_started", result
    after = host.conversation_store.threads.require(agent_id)
    assert after.model_profile_id == first
    assert after.model_selection_revision == before.model_selection_revision
    assert after.metadata == before.metadata


def test_unreadable_claim_is_pending_for_both_model_views(model_case, monkeypatch):
    host, client, view, _first, second = model_case
    agent_id = view["agent_id"]
    # 唯一受控边界是读取错误；领域、真实运输及 DTO 校验不替换。
    monkeypatch.setattr(host.conversation_store.claims, "load_report", lambda *a, **k: ({}, {"error": "simulated I/O error"}))
    assert completed(client, "background_agents.model.get", {"agent_id": agent_id})["effective_at"] == "pending"
    result = completed(client, "background_agents.model.set", {"agent_id": agent_id, "model_profile_id": second},
        expected_revision=view["selection_revision"])
    assert result["adoption_status"] == "pending_safe_boundary"


def test_stale_same_profile_still_checks_cas(model_case):
    host, _client, view, first, _second = model_case
    # 领域对同档案不能提前返回，仍须原 _mutate 检查 stale CAS。
    from agent_py_agent.agent.conversation.host_actor import HostChannelActor
    from agent_py_agent.agent.gateway_parts.business_api import (
        BusinessServices,
        _BusinessBackgroundService,
        bind_business_context,
    )

    actor = HostChannelActor(str(host.home_paths.owner_id), view["managed_by_thread_id"],
        "check", "local", "local", "local-agent")
    context = bind_business_context(host, authorize=lambda action, facts: True, actor=actor)
    service = _BusinessBackgroundService(BusinessServices(host), context)
    with pytest.raises(ValueError, match="REVISION_CONFLICT"):
        service.set_model(view["agent_id"], first, actor=actor,
            expected_revision=0, idempotency_key="stale-same")
