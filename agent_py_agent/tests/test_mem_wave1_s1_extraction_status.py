"""记忆第一波 S1：后台记忆提取状态（OL-1）、召回信封省略提示（OL-8）与日期/有效期字段（OL-9）。

只用临时目录、echo 部署模型与本机回环 Gateway；不调用真实模型、不连外网、不碰真实 owner 目录。
"""
from __future__ import annotations

import json
import socket
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_py_agent.agent.capability.skill_learning import SkillLearningService
from agent_py_agent.agent.core import _live_curator_profile
from agent_py_agent.agent.prompting_parts.memory_context import (
    BudgetedMemoryRecords,
    MemoryContextOptions,
    memory_context_options,
    memory_context_text,
    memory_inspect_tool,
)
from agent_py_agent.agent.settings.config import load_config
from agent_py_agent.agent.settings.curator_profile import (
    clear_auxiliary_profile_cooldown,
    curator_extraction_status,
    curator_extraction_status_line,
    record_auxiliary_profile_failure,
)
from agent_py_agent.agent.settings.model_profiles import execute_model_profile_operation
from agent_py_agent.tests.test_gateway_coordinator_client_memory import (
    memory_case,
    request,
    snapshot,
)


@pytest.fixture
def host(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_API_KEY", raising=False)
    monkeypatch.delenv("MY_AGENT_CONFIG", raising=False)
    home = SimpleNamespace(root=tmp_path, config_dir=tmp_path / "config", owner_provider="local",
                           owner_kind="main", owner_id="main")
    path = tmp_path / "desktop.yaml"
    path.write_text('agent_name: "wave1-status"\n', encoding="utf-8")
    config = load_config(path)
    config.model_backend, config.model_name = "echo", "deployment-test"
    return SimpleNamespace(home_paths=home, config=config)


# 函数用途: 在本 owner 模型目录里加一个后台辅助模型档案（假地址、假凭据，不会被连接）。
def _add_profile(host, **values) -> str:
    profile_id = str(uuid4())
    profile = {"model_name": "aux-test", "model_backend": "openai_compatible",
               "api_base": "https://aux.example.test/v1", "api_key": "fake-aux-credential",
               "model_context_window_tokens": 64000, **values}
    execute_model_profile_operation(host, "add", {"profile_id": profile_id, "profile": profile})
    return profile_id


def test_extraction_status_names_each_reason_without_backend_or_network(host, monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", lambda *_a, **_k: pytest.fail("状态读取不能连网络"))
    assert curator_extraction_status(host, memory_enabled=False) == {
        "extraction": "disabled", "disabled_reason": "owner_memory_disabled"}
    host.config.memory_curator_enabled = False
    assert curator_extraction_status(host)["disabled_reason"] == "curator_disabled"
    host.config.memory_curator_enabled = True
    assert curator_extraction_status(host) == {"extraction": "disabled", "disabled_reason": "profile_not_configured"}
    host.config.memory_curator_model_profile = str(uuid4())
    missing = curator_extraction_status(host)
    assert (missing["disabled_reason"], missing["profile_reason"]) == ("profile_unavailable", "profile_not_found")
    profile_id = _add_profile(host)
    host.config.memory_curator_model_profile = profile_id
    assert curator_extraction_status(host) == {"extraction": "enabled", "profile_id": profile_id, "model": "aux-test"}
    try:
        record_auxiliary_profile_failure(profile_id, "PROVIDER_TIMEOUT", now_monotonic=time.monotonic())
        cooling = curator_extraction_status(host)
        assert cooling["disabled_reason"] == "cooldown" and 0 < cooling["cooldown_remaining_seconds"] <= 601
    finally:
        clear_auxiliary_profile_cooldown(profile_id)
    assert "fake-aux-credential" not in json.dumps(curator_extraction_status(host))


def test_status_line_is_plain_chinese_with_the_next_step():
    off = curator_extraction_status_line({"extraction": "disabled", "disabled_reason": "profile_not_configured"})
    assert off.startswith("后台记忆提取：已停用（还没有设置后台辅助模型）") and "/model" in off
    on = curator_extraction_status_line({"extraction": "enabled", "profile_id": "p-1", "model": "aux-test"})
    assert on.startswith("后台记忆提取：已开启（后台辅助模型 aux-test）")
    cooling = curator_extraction_status_line({"extraction": "disabled", "disabled_reason": "cooldown",
                                              "cooldown_remaining_seconds": 30})
    assert "约 30 秒后自动重试" in cooling
    switch = curator_extraction_status_line({"extraction": "disabled", "disabled_reason": "curator_disabled"})
    assert "/settings set memory_curator_enabled true" in switch
    unknown = curator_extraction_status_line({"extraction": "unknown", "error_type": "AttributeError"})
    assert "读不出（AttributeError）" in unknown


def test_skill_learning_and_curator_read_the_same_live_profile(tmp_path, monkeypatch):
    monkeypatch.delenv("MY_AGENT_CONFIG", raising=False)

    def aux(live, guard):
        runtime = SimpleNamespace(config_provider=lambda: live, guard_config=guard)
        return SkillLearningService._auxiliary_profile(SimpleNamespace(runtime=runtime))

    guard = SimpleNamespace(memory_curator_model_profile="guard-profile")
    assert aux(SimpleNamespace(memory_curator_model_profile=""), guard) == "", "显式空串就是关闭，不复活 guard"
    assert aux(None, guard) == "guard-profile"
    assert aux(SimpleNamespace(), guard) == "guard-profile", "live 没有档案字段才看 guard"
    saved = tmp_path / "user.yaml"
    saved.write_text('memory_curator_model_profile: ""\n', encoding="utf-8")
    live = SimpleNamespace(memory_curator_model_profile="stale-at-start", config_path=str(saved))
    assert aux(live, guard) == "", "已保存的用户 YAML 清空即关闭"
    assert _live_curator_profile(SimpleNamespace(config=live), live, SimpleNamespace(model_profile="start")) == ""
    saved.write_text('memory_curator_model_profile: "switched"\n', encoding="utf-8")
    assert aux(live, guard) == "switched"
    assert _live_curator_profile(SimpleNamespace(config=live), live, SimpleNamespace(model_profile="start")) == "switched"
    assert _live_curator_profile(SimpleNamespace(), None, SimpleNamespace(model_profile="start")) == "start"


# ---------------------------------------------------------------- /memory 线缆往返


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.gateway_parts.http_service import (
        GatewayHTTPServer,
        GatewayHTTPServerParams,
    )
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.agent.settings import AgentConfig

    monkeypatch.setenv("MY_AGENT_HOME", str(tmp_path / "gateway-home"))
    monkeypatch.delenv("MY_AGENT_CONFIG", raising=False)
    agent = SimpleAgent(AgentConfig(model_backend="echo", enable_tools=False, memory_path="memory.jsonl"),
                        tmp_path / "workspace")
    server = GatewayHTTPServer(0, gateway_paths_from_root(agent.home_paths.root / "gateway"),
                               params=GatewayHTTPServerParams(agent=agent))
    server.start()
    try:
        yield SimpleNamespace(server=server, agent=agent, port=server.server.server_address[1], tmp=tmp_path)
    finally:
        server.stop()


def test_tui_bare_memory_prints_the_status_from_the_real_gateway(gateway, monkeypatch):
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
    from agent_py_agent.cli import chat_client_context
    from agent_py_agent.cli.chat_client_context import GatewayChatClientAgent
    from agent_py_agent.cli.chat_parts.slash_command_types import SlashCommandContext
    from agent_py_agent.cli.chat_parts.slash_commands import handle_common_slash_command

    sent = []
    forward = chat_client_context.urllib.request.urlopen

    def record(req, *args, **kwargs):
        sent.append(json.loads(req.data) if req.data else None)
        return forward(req, *args, **kwargs)

    monkeypatch.setattr(chat_client_context.urllib.request, "urlopen", record)
    client = GatewayChatClientAgent(
        SimpleNamespace(), SimpleNamespace(gateway_port=gateway.port, gateway_auth_token=""), gateway.tmp,
        [gateway.tmp], SimpleNamespace(root=gateway.agent.home_paths.root), owner_identity=OwnerIdentity.local_main())
    output: list[str] = []
    ctx = SlashCommandContext(client, 5, [], [], output.append, conversation_id="wave1-session")

    assert handle_common_slash_command("/memory", ctx=ctx)
    assert sent[-1]["operation"] == "recent" and sent[-1]["include_status"] is True
    assert output[0].startswith("后台记忆提取：已停用（还没有设置后台辅助模型）"), output
    assert output[1:] == ["没有找到记忆记录。"]

    assert handle_common_slash_command("/memory 咖啡", ctx=ctx)
    assert sent[-1]["operation"] == "search" and "include_status" not in sent[-1], "搜索请求正文保持原样"
    status, legacy = client.post_gateway_json(
        "/client/memory", {"operation": "recent", "limit": 2, "user_id": "local-agent", "channel": "chat",
                           "conversation_id": "wave1-session"}, timeout=10.0)
    assert status == 200 and set(legacy) == {"operation", "ok", "records", "error_code"}, "没要状态的老回执不变"


def test_tui_tolerates_an_older_gateway_without_status():
    from agent_py_agent.cli.chat_parts.slash_command_types import SlashCommandContext
    from agent_py_agent.cli.chat_parts.slash_commands import handle_common_slash_command

    agent = SimpleNamespace(gateway_client_only=True,
                            request_memory=lambda **_kwargs: {"ok": True, "records": [{"kind": "fact", "content": "x"}]})
    output: list[str] = []
    assert handle_common_slash_command("/memory", ctx=SlashCommandContext(agent, 5, [], [], output.append))
    assert output[0].startswith("后台记忆提取：Gateway 没有返回状态") and output[1] == "- [fact] : x"


@pytest.mark.parametrize("healthy", [False, True], ids=["cold", "healthy"])
def test_coordinator_recent_status_is_opt_in_and_writes_nothing(tmp_path, monkeypatch, healthy):
    monkeypatch.delenv("MY_AGENT_CONFIG", raising=False)
    with memory_case(tmp_path, monkeypatch, healthy=healthy) as case:
        before = snapshot(case.home)
        status, legacy = request(case)
        assert status == 200 and set(legacy) == {"operation", "ok", "records", "error_code"}
        status, result = request(case, {"include_status": True})
        assert status == 200 and result["ok"], result
        assert result["status"] == {"extraction": "disabled", "disabled_reason": "profile_not_configured"}
        assert [r["content"] for r in result["records"]] == case.expected["alice"]
        assert case.forbidden_calls == [] and snapshot(case.home) == before


@pytest.mark.parametrize("ready", [False, True], ids=["cold", "ready"])
def test_im_candidate_list_prints_one_status_line_on_both_paths(tmp_path, monkeypatch, ready):
    from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
    from agent_py_agent.agent.gateway_parts import control_operation_service as ops
    from agent_py_agent.tests.test_coordinator_model_card_delivery import delivery_case
    from agent_py_agent.tests.test_coordinator_model_menu import post_model
    from agent_py_agent.tests.test_gateway_ask_control_parity import parity_request

    for text in ("/memory", "/memory 资料"):
        assert parse_conversation_control(text) is None, "IM 不拦截裸 /memory 与普通查询"
    with delivery_case(tmp_path, monkeypatch) as (case, _delivered):
        _agent, _host, _path, row, _body, server, _runtime, _sent = case
        body = parity_request(case, ready=ready, text="/memory candidates")
        status, result = post_model(server, body, route="/control", actor=row["actor_id"])
        assert status == 200 and result["ok"], result
        lines = result["message"].split("\n")
        assert lines[0].startswith("后台记忆提取：已停用（还没有设置后台辅助模型）")
        assert lines[1] == "记忆候选（尚未确认的内容不会正式保存）："
        assert result["details"]["extraction_status"] == {"extraction": "disabled",
                                                          "disabled_reason": "profile_not_configured"}
        receipt = ops.read_gateway_control_operation(server.paths, result["operation_id"], read_only=True)
        assert receipt.result["message"] == result["message"], "回执与即时回复同一份文字"


# ---------------------------------------------------------------- 召回信封（OL-8 / OL-9）


class _Row:
    def __init__(self, entry_id, content, *, updated_at=0.0, attributes=None, kind="fact"):
        self.entry_id, self.content, self.kind, self.role = entry_id, content, kind, "user"
        self.updated_at, self.created_at = updated_at, updated_at
        self.attributes = attributes or {"origin": "user_explicit"}


def test_omitted_notice_names_the_tool_that_is_really_available():
    rows = BudgetedMemoryRecords([_Row("m-1", "喝美式")], omitted_count=1)
    assert "[... 1 formal memory entries omitted; use memory_search ...]" in memory_context_text(rows)
    remember = memory_context_text(rows, options=MemoryContextOptions(inspect_tool="remember"))
    assert '[... 1 formal memory entries omitted; use remember(action="list") ...]' in remember
    count_only = memory_context_text(rows, options=MemoryContextOptions(inspect_tool=""))
    assert "[... 1 formal memory entries omitted ...]" in count_only and "use " not in count_only.split("]")[-2]
    with memory_context_options(MemoryContextOptions(inspect_tool="remember")):
        assert memory_context_text(rows) == remember, "预算预估读同一份选项"
    assert "use memory_search" in memory_context_text(rows), "退出作用域后恢复默认"
    snapshot_both = SimpleNamespace(available_tool_names={"memory_search", "remember"})
    assert memory_inspect_tool(snapshot_both) == "memory_search"
    assert memory_inspect_tool(snapshot_both, ["remember"]) == "remember"
    assert memory_inspect_tool(SimpleNamespace(available_tool_names={"read_file"})) == ""


def test_recalled_rows_carry_owner_local_date_and_validity_but_never_1970():
    stamp = 1763596800.0  # 2025-11-20T00:00:00Z
    rows = [
        _Row("m-1", "住在上海", updated_at=stamp,
             attributes={"origin": "user_explicit", "valid_from": "2025-11-01", "valid_until": "2026-01-31"}),
        _Row("m-2", "没有时间戳的旧记录"),
    ]
    text = memory_context_text(rows, options=MemoryContextOptions(timezone_name="America/Los_Angeles"))
    envelope = json.loads(text.split("\n")[2].replace("\\u003c", "<").replace("\\u003e", ">").replace("\\u0026", "&"))
    first, second = envelope["records"]
    assert first["updated_at"] == stamp and first["updated_date"] == "2025-11-19", "按 owner 时区取日期"
    assert (first["valid_from"], first["valid_until"]) == ("2025-11-01", "2026-01-31")
    assert "updated_date" not in second and second["updated_at"] == 0.0, "缺时间戳不编造 1970-01-01"
    shanghai = memory_context_text(rows[:1], options=MemoryContextOptions(timezone_name="Asia/Shanghai"))
    assert '"updated_date":"2025-11-20"' in shanghai
    broken = memory_context_text(rows[:1], options=MemoryContextOptions(timezone_name="Not/AZone"))
    assert "updated_date" not in broken and "住在上海" in broken, "坏时区只不显示日期，不让构建失败"


def test_prompt_request_and_budget_share_the_inspect_tool(monkeypatch):
    from agent_py_agent.agent.agent_core import tool_request_projection as projection

    params = SimpleNamespace(
        user_prompt="q", memories=[], prompt_files=[], system_prompt_override=None, context_scope="default",
        selected_skill_ids=None, required_skill_ids=(), in_use_method_ids=(), tool_catalog_section="",
        tool_recommendations_section="", tool_context=None, allowed_tools=None,
        tool_runtime_snapshot=SimpleNamespace(available_tool_names={"remember"}, runtime=lambda _name: None),
    )
    monkeypatch.setattr(projection, "native_tool_use_active", lambda _params: False)
    request = projection.tool_loop_prompt_request(params, runtime_injections=[], workspace_context=None,
                                                  execution_facts="")
    assert request.tools.memory_inspect_tool == "remember"
    assert Path(projection.__file__).name == "tool_request_projection.py"
