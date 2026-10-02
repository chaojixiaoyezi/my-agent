"""MCP 逐工具声明表与共用观察绑定（J16 片 A）：审批只能更严、观察声明解析、按发现到的工具核对、整服务拒绝带原因码、
未发现只提醒、代理铸 ID 与候选复核、无声明代理行为不变、always 审批进投影并真的产生审批。"""
from __future__ import annotations

import json
import sys
import textwrap
import uuid
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.tool_runtime_ledger import persist_tool_runtime_ledger
from agent_py_agent.agent.contracts.tool_manifest_contract import tool_manifest_payload
from agent_py_agent.agent.plugin_observation import (
    OBSERVATION_CANDIDATE_UNKNOWN,
    OBSERVATION_KEY,
    OBSERVATION_META_EXTENSION,
    OBSERVATION_SCHEMA,
    OBSERVATION_STALE,
    PluginToolObservation,
    PluginToolObservationRef,
)
from agent_py_agent.agent.tooling.action_policy import ActionPolicy, ActionPolicyRequest
from agent_py_agent.agent.tooling.mcp_client import MCPError, MCPServerConfig, MCPToolInfo
from agent_py_agent.agent.tooling.mcp_declarations import (
    DECLARATION_INVALID_CODE,
    DECLARED_TOOL_NOT_DISCOVERED,
    INPUT_SCHEMA_INVALID,
    MCPDeclarationError,
    resolve_server_declarations,
)
from agent_py_agent.agent.tooling.mcp_registration import (
    build_proxy_tool,
    mcp_server_facts,
    refresh_registered_mcp_client,
    register_mcp_servers,
)
from agent_py_agent.agent.tooling.mcp_transport import MCPTransport
from agent_py_agent.tests._tool_runtime_harness import (
    canonical_test_call,
    runtime_snapshot_for_tools,
)
from agent_py_agent.tests.test_plugin_observation import _Repo

RUN_SCOPE = {"owner_id": "local/main", "owner_home": "/owner", "run_id": "run-1", "task_id": "task-1", "attempt_id": "attempt-1", "turn_id": "turn-1"}
READ_SCHEMA = {"type": "object", "properties": {"window": {"type": "string"}}}
CLICK_SCHEMA = {"type": "object", "properties": {"candidate_id": {"type": "string"}, "selector": {"type": "string"}}}
TOOLS = [MCPToolInfo("read", "采样", READ_SCHEMA), MCPToolInfo("click", "点击", CLICK_SCHEMA), MCPToolInfo("plain", "普通", {})]


# 函数用途: 造一份带观察声明的服务配置（read 只读观察，click 动作引用 candidate_id）。
def _config(**overrides) -> MCPServerConfig:
    raw = {"command": "x", "tool_effects": {"read": "read_only"}, "tool_approvals": {"read": "always"},
           "tool_observations": {"read": {"observation": {"target_kind": "window", "max_candidates": 8}},
                                 "click": {"observation_ref": {"target_kind": "window", "param": "candidate_id"}}}}
    raw.update(overrides)
    return MCPServerConfig.from_mapping("srv", raw)


def test_tool_approvals_only_tighten_and_never_is_rejected():
    config = MCPServerConfig.from_mapping("srv", {"command": "x", "tool_approvals": {"a": "always", "b": " MUTATING "}})
    assert (config.approval_for_tool("a"), config.approval_for_tool("b"), config.approval_for_tool("zzz")) == ("always", "mutating", "dangerous")
    for bad in ({"a": "never"}, {"a": "auto"}, "always"):
        with pytest.raises(MCPError) as info:
            MCPServerConfig.from_mapping("srv", {"command": "x", "tool_approvals": bad})
        assert info.value.code == "MCP_CONFIG_INVALID"
    wildcard = MCPServerConfig.from_mapping("srv", {"command": "x", "tool_approvals": {"*": "always"}})
    assert wildcard.approval_for_tool("anything") == "always"


def test_tool_observations_parse_into_the_v5_declaration_types():
    config = _config()
    assert config.tool_observations["read"] == PluginToolObservation("window", 8)
    assert config.tool_observations["click"] == PluginToolObservationRef("window", "candidate_id")
    for bad in ({"read": {"observation": {}, "observation_ref": {}}}, {"read": {"other": {}}}, {"read": {"observation": {"target_kind": "Bad Kind"}}},
                {"read": {"observation_ref": {"target_kind": "window"}}}, {"read": "observation"}, ["read"]):
        with pytest.raises(MCPError) as info:
            MCPServerConfig.from_mapping("srv", {"command": "x", "tool_observations": bad})
        assert info.value.code == "MCP_CONFIG_INVALID"


def test_resolve_rejects_the_whole_server_with_structured_reasons():
    bad = _config(tool_effects={"read": "dangerous"}, tool_observations={
        "read": {"observation": {"target_kind": "window"}}, "click": {"observation_ref": {"target_kind": "window", "param": "selector"}},
        "plain": {"observation_ref": {"target_kind": "page", "param": "candidate_id"}}})
    plain_with_param = [*TOOLS[:2], MCPToolInfo("plain", "普通", CLICK_SCHEMA)]
    with pytest.raises(MCPDeclarationError) as info:
        resolve_server_declarations(bad, plain_with_param)
    assert info.value.code == DECLARATION_INVALID_CODE
    assert list(info.value.reasons) == [{"tool": "read", "code": "observation_effect"}, {"tool": "plain", "code": "observation_ref_unpaired"}]
    required = [MCPToolInfo("read", "采样", READ_SCHEMA), MCPToolInfo("click", "点击", {**CLICK_SCHEMA, "required": ["candidate_id"]})]
    with pytest.raises(MCPDeclarationError) as param:
        resolve_server_declarations(_config(), required)
    assert list(param.value.reasons) == [{"tool": "click", "code": "observation_ref_param"}]


def test_resolve_only_notices_declared_tools_that_were_not_discovered():
    resolved = resolve_server_declarations(_config(), [TOOLS[0], TOOLS[2]])
    assert resolved.notices == ({"tool": "click", "code": DECLARED_TOOL_NOT_DISCOVERED},)
    assert resolved.tools["read"].observation == PluginToolObservation("window", 8) and resolved.tools["read"].approval == "always"
    assert resolved.tools["plain"].observation is None and resolved.tools["plain"].approval == "dangerous"
    unpaired = _config(tool_observations={"click": {"observation_ref": {"target_kind": "window", "param": "candidate_id"}}})
    with pytest.raises(MCPDeclarationError):
        resolve_server_declarations(unpaired, TOOLS), "配对只按发现到的工具算"


# 类用途: 假 MCP 客户端：固定配置，按预设返回结果，记录发出的参数与 _meta。
class _Client:
    def __init__(self, config: MCPServerConfig, result=None):
        self.config, self.result, self.sent = config, result, []
        self.publication, self.transport = None, None

    def is_running(self):
        return True

    def is_closed(self):
        return False

    def connection(self):
        return self.transport

    def list_tools(self, *, transport=None):
        self.transport = transport
        return list(TOOLS)

    def publish_tools(self, transport, publish):
        return publish()

    def call_tool(self, tool_name, arguments, **options):
        self.sent.append({"tool": tool_name, "arguments": dict(arguments), "meta": options.get("request_meta")})
        return self.result


# 类用途: 最小注册表替身：tools 映射与构造参数里的 owner 权威库。
class _Registry:
    def __init__(self, repo=None):
        self.tools = {}
        self._construction_params = SimpleNamespace(runtime_repo=repo)
        self._mcp_clients = []


# 函数用途: 固定连接替身：带进程出生身份和宿主连接随机串（真 MCPTransport 构造时也是这样生成的）。
def _transport(birth: str = "proc:1234"):
    return SimpleNamespace(binding=SimpleNamespace(birth_token=birth), connection_id=uuid.uuid4().hex)


def _read_result(observation):
    body = {"window": "main", "count": 2}
    return {"content": json.dumps(body), "content_blocks": [{"type": "text", "text": json.dumps(body)}],
            "structuredContent": {**body, OBSERVATION_KEY: observation}, "isError": False}


def _observation():
    return {"schema": OBSERVATION_SCHEMA, "target": {"ref": "win:boot:1", "generation": "boot-1-3"},
            "frame": {"space": "screen_points", "origin": [0, 0], "size": [800, 600], "scale": [1, 1]},
            "candidates": [{"key": "ocr:1", "role": "ocr_text", "label": "提交", "actions": ["click"], "region": [10, 20, 60, 18]},
                           {"key": "ocr:2", "role": "ocr_text", "label": "取消", "actions": ["click"], "region": [90, 20, 60, 18]}]}


# 函数用途: 经发布链拿到带绑定的代理（transport 不给就用替身）。
def _published(repo, client, transport=None):
    registry = _Registry(repo)
    registry._mcp_clients.append(client)
    assert refresh_registered_mcp_client(registry, client, transport=transport or _transport()) == 3
    return registry


def _recorded(repo, registry):
    outcome = registry.tools["mcp__srv__read"].execute({"window": "main", "__run_scope": RUN_SCOPE, "__operation_id": "op-1"})
    archive = {"run_id": "run-1", "task_id": "task-1", "operation_id": "op-1", "attempt_id": "attempt-1", "tool": outcome.tool, "ok": True,
               "error_code": "", "idempotency_key": "", "tool_result_envelope": outcome.result_envelope}
    persist_tool_runtime_ledger(SimpleNamespace(subagents=SimpleNamespace(runtime_db=repo), local_store=SimpleNamespace()), archive)
    return outcome


def test_published_read_tool_mints_host_ids_with_mcp_provider_and_connection_generation():
    repo, client = _Repo(), _Client(_config(), _read_result(_observation()))
    registry = _published(repo, client)
    read = registry.tools["mcp__srv__read"]
    assert read.runtime_policy.approval_policy.mode == "always" and read.runtime_policy.effect_resolver.default_effect == "read_only"
    assert set(read.runtime_policy.input_policy.internal_parameters) == {"__operation_id", "__run_scope"}
    outcome = _recorded(repo, registry)
    assert outcome.ok and client.sent[0]["arguments"] == {"window": "main"}, "宿主参数不转发"
    visible = json.loads(outcome.output)["structuredContent"][OBSERVATION_KEY]
    assert set(visible) == {"observation_id", "candidates"} and visible["candidates"][0]["actions"] == ["mcp__srv__click"]
    assert "region" not in json.dumps(visible) and "win:boot" not in outcome.output
    record = outcome.result_envelope["observation"]
    assert record["provider_id"] == "mcp:srv" and record["activation_id"].startswith("mcp:srv:") and len(record["activation_id"]) == len("mcp:srv:") + 16
    assert record["frame"]["size"] == [800, 600] and record["candidates"][1]["region"] == [90, 20, 60, 18]
    assert repo.events[-1]["payload"]["observation"]["frame"] == {"size": [800, 600], "scale": [1, 1]}
    assert client.publication.status == "published" and client.publication.tool_count == 3 and client.publication.notices == ()
    assert mcp_server_facts(registry) == [{"server": "srv", "running": True, "publication": client.publication.as_dict()}]


def test_each_connection_gets_its_own_activation_id_even_with_equal_or_empty_birth_tokens():
    repo, client = _Repo(), _Client(_config(), _read_result(_observation()))
    first = _published(repo, client, transport=_transport(birth=""))
    candidates = json.loads(_recorded(repo, first).output)["structuredContent"][OBSERVATION_KEY]["candidates"]
    second = _published(repo, client, transport=_transport(birth=""))
    first_id = first.tools["mcp__srv__read"].observation_binding.activation_id
    assert first_id != second.tools["mcp__srv__read"].observation_binding.activation_id, "出生身份相同甚至为空，代次仍由宿主随机串区分"
    assert first_id.startswith("mcp:srv:") and len(first_id) == len("mcp:srv:") + 16
    sent_before = len(client.sent)
    stale = second.tools["mcp__srv__click"].execute({"candidate_id": candidates[0]["candidate_id"], "__run_scope": RUN_SCOPE})
    assert stale.reported_error_code == OBSERVATION_STALE and stale.effect_outcome == "not_started", "子进程重启换代后旧候选一律过期"
    assert len(client.sent) == sent_before, "过期候选不发给新实例"
    fresh = json.loads(_recorded(repo, second).output)["structuredContent"][OBSERVATION_KEY]["candidates"]
    client.result = {"content": "{}", "structuredContent": {"clicked": True}, "isError": False}
    outcome = second.tools["mcp__srv__click"].execute({"candidate_id": fresh[0]["candidate_id"], "__run_scope": RUN_SCOPE})
    assert outcome.ok and client.sent[-1]["meta"][OBSERVATION_META_EXTENSION]["key"] == "ocr:1", "新代次的观察照常可用"


def test_action_tool_rechecks_candidate_before_sending_and_lifts_provider_rejection():
    repo, client = _Repo(), _Client(_config(), _read_result(_observation()))
    registry = _published(repo, client)
    candidates = json.loads(_recorded(repo, registry).output)["structuredContent"][OBSERVATION_KEY]["candidates"]
    click = registry.tools["mcp__srv__click"]
    sent_before = len(client.sent)
    unknown = click.execute({"candidate_id": "cand-0123456789abcdef", "__run_scope": RUN_SCOPE})
    assert (unknown.error_code, unknown.reported_error_code, unknown.effect_outcome) == ("TOOL_INVALID_ARGUMENTS", OBSERVATION_CANDIDATE_UNKNOWN, "not_started")
    assert len(client.sent) == sent_before, "未知候选不发送"
    client.result = {"content": "{}", "structuredContent": {"clicked": True}, "isError": False}
    ok = click.execute({"candidate_id": candidates[1]["candidate_id"], "__run_scope": RUN_SCOPE})
    assert ok.ok and client.sent[-1]["meta"][OBSERVATION_META_EXTENSION] == {
        "version": "1", "observation_id": repo.events[-1]["payload"]["observation"]["observation_id"], "key": "ocr:2",
        "target": {"ref": "win:boot:1", "generation": "boot-1-3"}}
    client.result = {"content": "{}", "structuredContent": {"my_agent_observation_error": {"code": "stale"}}, "isError": True}
    lifted = click.execute({"candidate_id": candidates[1]["candidate_id"], "__run_scope": RUN_SCOPE})
    assert (lifted.reported_error_code, lifted.effect_outcome, lifted.result_envelope["observation_rejected"]) == (OBSERVATION_STALE, "not_started", OBSERVATION_STALE)
    plain = click.execute({"selector": "#go", "__run_scope": RUN_SCOPE})
    assert plain.reported_error_code == "TOOL_EXECUTION_FAILED" and client.sent[-1]["meta"] is None, "没填候选沿原路径，不附 _meta、不提升"


def test_real_transport_mints_a_host_connection_id_independent_of_process_birth():
    transports = [MCPTransport(SimpleNamespace(pid=0, stderr=None), "srv", max_line_chars=1024, connect_timeout=1) for _ in range(2)]
    assert transports[0].binding.birth_token == transports[1].binding.birth_token, "同一 pid 的出生身份相同（取不到时都为空）"
    assert len(transports[0].connection_id) == 32 and transports[0].connection_id != transports[1].connection_id
    repo, client = _Repo(), _Client(_config(), _read_result(_observation()))
    ids = {_published(repo, client, transport=transport).tools["mcp__srv__read"].observation_binding.activation_id for transport in transports}
    assert len(ids) == 2 and all(item == f"mcp:srv:{transport.connection_id[:16]}" for item, transport in zip(sorted(ids), sorted(transports, key=lambda t: t.connection_id)))


def test_schema_canonicalization_failure_and_undiscovered_approvals_get_their_own_codes():
    broken = [MCPToolInfo("read", "采样", {"type": "object", "properties": {"window": "not-a-schema"}}), TOOLS[1]]
    with pytest.raises(MCPDeclarationError) as info:
        resolve_server_declarations(_config(), broken)
    assert list(info.value.reasons) == [{"tool": "read", "code": INPUT_SCHEMA_INVALID}]
    resolved = resolve_server_declarations(_config(tool_approvals={"read": "always", "ghost": "mutating", "*": "dangerous"}), TOOLS)
    assert resolved.notices == ({"tool": "ghost", "code": DECLARED_TOOL_NOT_DISCOVERED},), "审批声明了但没发现的工具也提醒；通配 * 不算"


def test_server_without_declarations_publishes_byte_identical_proxies():
    config = MCPServerConfig.from_mapping("srv", {"command": "x"})
    client = _Client(config, {"content": "{}", "structuredContent": {"ok": 1}, "isError": False})
    registry = _published(_Repo(), client)
    for info in TOOLS:
        proxy = registry.tools[f"mcp__srv__{info.name}"]
        reference = build_proxy_tool(client, "srv", info, effect=config.effect_for_tool(info.name), catalog_category="mcp")
        assert proxy.observation_binding is None and proxy.runtime_policy == reference.runtime_policy and proxy.model_spec == reference.model_spec
        assert proxy.runtime_policy.approval_policy.mode == "dangerous" and proxy.runtime_policy.input_policy.internal_parameters == ()
    outcome = registry.tools["mcp__srv__plain"].execute({"__run_scope": RUN_SCOPE})
    assert outcome.ok and outcome.result_envelope == {} and client.sent[-1]["meta"] is None
    assert client.publication.as_dict() == {"status": "published", "tool_count": 3, "code": "", "reasons": [], "notices": []}


def test_always_approval_is_projected_and_asks_even_in_autonomous_mode(tmp_path):
    registry = _published(_Repo(), _Client(_config(), {"content": "{}", "isError": False}))
    read, plain = registry.tools["mcp__srv__read"], registry.tools["mcp__srv__plain"]
    manifest = tool_manifest_payload(runtime_snapshot_for_tools({"mcp__srv__read": read}))
    assert manifest["tools"][0]["runtime_policy"]["approval_policy"] == {"mode": "always"}
    snapshot = runtime_snapshot_for_tools({"mcp__srv__read": read, "mcp__srv__plain": plain})
    for mode, expected in (("ask", "ask"), ("auto", "ask")):
        decision = ActionPolicy().decide(ActionPolicyRequest(canonical_test_call(snapshot, "mcp__srv__read", {}), snapshot, tmp_path, approval_mode=mode))
        assert decision.status == expected, f"只读但 always 的工具在 {mode} 下仍要本人确认"
    auto_plain = ActionPolicy().decide(ActionPolicyRequest(canonical_test_call(snapshot, "mcp__srv__plain", {}), snapshot, tmp_path, approval_mode="auto"))
    assert auto_plain.status == "allow", "默认 dangerous 审批的工具自主模式照常放行"


# 函数用途: 一个只暴露单工具的 stdio MCP 服务脚本。
def _single_tool_server(tool_name: str) -> str:
    return textwrap.dedent(
        f"""
        import json, sys
        TOOL = {{"name": {tool_name!r}, "description": "t", "inputSchema": {{"type": "object", "properties": {{}}}}}}
        def send(message):
            sys.stdout.write(json.dumps(message) + "\\n"); sys.stdout.flush()
        for line in sys.stdin:
            request = json.loads(line); method = request.get("method"); request_id = request.get("id")
            if method == "initialize":
                send({{"jsonrpc": "2.0", "id": request_id, "result": {{"protocolVersion": "2024-11-05", "capabilities": {{"tools": {{}}}},
                      "serverInfo": {{"name": "dynamic", "version": "1"}}}}}})
            elif method == "tools/list":
                send({{"jsonrpc": "2.0", "id": request_id, "result": {{"tools": [TOOL]}}}})
            elif method == "tools/call":
                send({{"jsonrpc": "2.0", "id": request_id, "result": {{"content": [{{"type": "text", "text": TOOL["name"]}}], "isError": False}}}})
        """
    )


@pytest.mark.integration
def test_register_rejects_the_declared_server_only_and_keeps_the_other_one():
    registry = _Registry()
    servers = {
        "bad": {"command": sys.executable, "args": ["-c", _single_tool_server("probe")], "connect_timeout": 10, "timeout": 10,
                "tool_observations": {"probe": {"observation": {"target_kind": "page"}}}},
        "good": {"command": sys.executable, "args": ["-c", _single_tool_server("echo")], "connect_timeout": 10, "timeout": 10},
    }
    clients = register_mcp_servers(registry, servers)
    try:
        registry._mcp_clients = clients
        by_name = {client.config.name: client for client in clients}
        assert set(registry.tools) == {"mcp__good__echo"}, "坏声明的服务不发布，不连带别的服务"
        rejected = by_name["bad"].publication
        assert (rejected.status, rejected.code) == ("rejected", DECLARATION_INVALID_CODE)
        assert list(rejected.reasons) == [{"tool": "probe", "code": "observation_effect"}], "observation 挂在了默认 dangerous 的工具上"
        assert by_name["good"].publication.status == "published" and by_name["good"].publication.tool_count == 1
        facts = {row["server"]: row["publication"]["status"] for row in mcp_server_facts(registry)}
        assert facts == {"bad": "rejected", "good": "published"}
    finally:
        for client in clients:
            client.stop()
