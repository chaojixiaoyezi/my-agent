"""W2 真实管理入口与 stdio 候选；全部包、进程和安装表只在临时 home。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields, replace
from threading import Event
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability.package_build import build_capability_pack
from agent_py_agent.agent.plugin_activation import PluginActivationRequest
from agent_py_agent.agent.plugin_activation_record import (
    PluginActivation,
    activation_catalog_digest,
    catalog_digest_of,
    pinned_catalog_json_from_tools,
    pinned_tool_declarations,
)
from agent_py_agent.agent.plugin_environment_plan import PluginEnvironmentPlan
from agent_py_agent.agent.plugin_installation import PluginInstallationError, PluginInstallRequest
from agent_py_agent.agent.plugin_installation_state import (
    decode_installation_state,
    encode_installation_state,
)
from agent_py_agent.agent.plugin_package import inspect_plugin_package
from agent_py_agent.agent.plugin_runtime import plugin_tool_name
from agent_py_agent.agent.tooling.mcp_client import MCPToolInfo
from agent_py_agent.tests.test_plugin_management import manager


def _build(mode="pinned_on_enable", *, name="w2-pack", infos=None, network=False):
    tool = {"name": "inspect", "description": "检查输入", "input_schema": {"type": "object", "properties": {}},
            "requested_effect": "dangerous"}
    infos = [{"name": tool["name"], "description": tool["description"], "inputSchema": tool["input_schema"]}] if infos is None else infos
    program = '''import json, os, sys
from pathlib import Path
data = Path(os.environ["MY_AGENT_PLUGIN_DATA_DIR"])
with (data / "launches").open("a") as f: f.write("1\\n")
tools = __TOOLS__
for line in sys.stdin:
    req = json.loads(line)
    if "id" not in req: continue
    if req["method"] == "initialize":
        result = {"protocolVersion": req["params"]["protocolVersion"], "capabilities": {"tools": {}},
                  "serverInfo": {"name": "w2-fixture", "version": "1"}}
    elif req["method"] == "tools/list":
        with (data / "lists").open("a") as f: f.write("1\\n")
        result = {"tools": tools}
    else: raise RuntimeError("unexpected fixture method")
    print(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": result}), flush=True)
'''.replace("__TOOLS__", repr(infos)).encode()
    contents = {"CAPABILITY.md": b"# W2\n", "scripts/document.py": b"document only\n", "mcp/server.py": program}
    declaration = {"plugin_id": name, "version": "1.0", "summary": "W2 能力",
        "capability": {"description": "检查输入", "keywords": ["检查"], "entry_document": "CAPABILITY.md"},
        "files": [{"path": path} for path in contents],
        "settings_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        "mcp_servers": [{"name": "checks", "root": "mcp", "entry": {"kind": "interpreter", "command": "server.py",
            "interpreter": "python3", "args": []}, "platforms": ["any"], "permissions": {"network": network},
            "tool_catalog": mode, "tools": [tool] if mode == "declared" else []}]}
    return inspect_plugin_package(build_capability_pack(declaration, contents).payload)


def _service(tmp_path, *, switch=True, **build):
    # 先断言配置接口，旧版以明确的功能缺失失败，而非构造器 TypeError。
    from agent_py_agent.agent.plugin_management import PluginManagementContext

    assert "capability_pack_mcp_enabled" in {item.name for item in fields(PluginManagementContext)}
    service, _ = manager(tmp_path, capability_pack_mcp_enabled=switch)
    entry = service.installations.install(PluginInstallRequest(_build(**build), "install-pack", 0)).installation
    return service, entry


def _enable(service, request_id="preview", code="", *, revision=None):
    suffix = f" --confirm {code}" if code else ""
    return service.command("/plugins enable w2-pack" + suffix,
        revision=service.catalog().revision if revision is None else revision, request_id=request_id)


def _confirmed(service):
    revision = service.catalog().revision
    preview = _enable(service, revision=revision)
    assert preview["details"]["reason"] == "confirmation_required", preview
    confirmation = preview["details"]["confirmation"]
    assert confirmation["kind"] == "capability_pack_mcp"
    assert service.installations.snapshot()[0].activation is None
    result = _enable(service, "enable", confirmation["confirm_code"], revision=revision)
    return result, confirmation, revision


def _roundtrip(store):
    raw = (store.root / "installations.json").read_bytes()
    decoded = decode_installation_state(raw, store._owner)
    assert encode_installation_state(decoded.entries, store._owner, decoded.migration_json).encode() == raw
    return decoded.entries


def _component_transport(monkeypatch, *, infos=None, cleanup_confirmed=True, raw_tools=None):
    """仅供状态/预算组件测试的传输替身；上面的真实 stdio 验收绝不使用它。"""
    from agent_py_agent.agent import plugin_enable_tool

    observed = {"starts": 0, "lists": 0, "stops": 0}
    sample = [MCPToolInfo("inspect", "检查输入", {"type": "object", "properties": {}})]

    class Client:
        def __init__(self, owner, installation, **kwargs):
            assert kwargs["process_sandbox"] is True
            assert installation.activation.phase == "preparing"
            self.installation = installation

        def start(self):
            observed["starts"] += 1
            return self

        def list_tools(self, *, transport):
            assert transport is self
            observed["lists"] += 1
            if raw_tools is not None:
                from agent_py_agent.agent.tooling.mcp_client import _discover_tools

                return _discover_tools(lambda *args, **kwargs: {"tools": raw_tools}, 1)
            return sample if infos is None else infos

        def discover_tools(self, transport):
            self.list_tools(transport=transport)
            return [SimpleNamespace(model_spec=SimpleNamespace(name=plugin_tool_name("w2-pack", "inspect")))]

        def stop(self):
            observed["stops"] += 1
            return SimpleNamespace(confirmed=cleanup_confirmed, record={"session_id": "component-candidate"}, terminations=())

    monkeypatch.setattr(plugin_enable_tool, "PluginMCPClient", Client)
    monkeypatch.setattr(plugin_enable_tool, "_sandbox_problem", lambda tool: "")
    return observed


@pytest.mark.parametrize("mode", ["pinned_on_enable", "declared"])
def test_component_enable_keeps_declared_rows_and_publishes_pinned_rows(tmp_path, monkeypatch, mode):
    observed = _component_transport(monkeypatch)
    service, _ = _service(tmp_path, mode=mode)
    result, confirmation, revision = _confirmed(service)
    assert result["state"] == "succeeded", result
    row = _roundtrip(service.installations)[0]
    assert row.activation.catalog_sha256 == activation_catalog_digest(row.manifest, row.activation)
    assert (row.activation.pinned_catalog_json is not None) is (mode == "pinned_on_enable")
    assert _enable(service, "enable", confirmation["confirm_code"], revision=revision)["details"] == result["details"]
    assert _roundtrip(service.installations) == (row,)
    assert observed == {"starts": 1, "lists": 1, "stops": 1}


@pytest.mark.parametrize("infos,confirmed,state,code", [([], True, "failed", "MCP_PACK_CATALOG_INVALID"),
    (None, False, "outcome_unknown", ""), ([], False, "outcome_unknown", "")])
def test_component_catalog_error_after_stop_and_unconfirmed_stop_stays_unknown(tmp_path, monkeypatch, infos, confirmed, state, code):
    from agent_py_agent.agent.plugin_enable_tool import PluginEnableTool

    original, outcomes = PluginEnableTool.execute, []

    def captured(self, params):
        outcome = original(self, params)
        outcomes.append(outcome)
        return outcome

    monkeypatch.setattr(PluginEnableTool, "execute", captured)
    observed = _component_transport(monkeypatch, infos=infos, cleanup_confirmed=confirmed)
    service, _ = _service(tmp_path)
    result, _, _ = _confirmed(service)
    assert result["state"] == state, result
    if code:
        assert result["error_code"] == code and result["details"]["reason"] == "pack_catalog_invalid"
    else:
        # 管理请求的 UNKNOWN 投影不导出错误细节；在实际 handler 观察，不能编造外层字段。
        assert outcomes[-1].error_code == "TOOL_EXECUTION_FAILED"
        assert outcomes[-1].effect_outcome == "unknown"
        assert outcomes[-1].result_envelope["plugin_enable"]["reason"] == "candidate_cleanup_unconfirmed"
    assert _roundtrip(service.installations)[0].activation.phase == "preparing"
    assert observed == {"starts": 1, "lists": 1, "stops": 1}


@pytest.mark.parametrize("phase,code", [("start", "MCP_PROTOCOL_ERROR"), ("list_tools", "MCP_TIMEOUT")])
def test_component_non_catalog_protocol_and_timeout_errors_preserve_endpoint_failure(tmp_path, monkeypatch, phase, code):
    from agent_py_agent.agent import plugin_enable_tool
    from agent_py_agent.agent.tooling.mcp_protocol import MCPError

    observed = _component_transport(monkeypatch)

    def fail(self, *args, **kwargs):
        observed["starts" if phase == "start" else "lists"] += 1
        raise MCPError("组件故障注入，不是真实进程", code=code)

    monkeypatch.setattr(plugin_enable_tool.PluginMCPClient, phase, fail)
    service, _ = _service(tmp_path)
    result, _, _ = _confirmed(service)
    assert result["state"] == "failed", result
    assert result["error_code"] == "TOOL_EXECUTION_FAILED"
    assert result["details"]["reason"] == "plugin_endpoint_failed"
    assert _roundtrip(service.installations)[0].activation.phase == "preparing"
    assert observed == {"starts": 1, "lists": int(phase == "list_tools"), "stops": 1}


def test_enable_table_decode_count_is_fixed_for_ten_and_one_hundred_packs(tmp_path, monkeypatch):
    from agent_py_agent.agent import plugin_install_store

    _component_transport(monkeypatch)
    decode = plugin_install_store.decode_installation_state
    counts = []
    for size in (10, 100):
        root = tmp_path / str(size)
        root.mkdir()
        service, _ = _service(root)
        for index in range(size - 1):
            service.installations.install(PluginInstallRequest(_build(name=f"other-{index}"), f"install-{index}", 0))
        count = []
        with monkeypatch.context() as patch:
            def counted(*args, **kwargs):
                count.append(1)
                return decode(*args, **kwargs)
            patch.setattr(plugin_install_store, "decode_installation_state", counted)
            result, _, _ = _confirmed(service)
        assert result["state"] == "succeeded", result
        counts.append(len(count))
    assert 0 < counts[0] == counts[1]


def test_component_duplicate_names_in_real_mcp_parser_are_catalog_invalid_after_stop(tmp_path, monkeypatch):
    item = {"name": "inspect", "description": "检查输入", "inputSchema": {"type": "object", "properties": {}}}
    observed = _component_transport(monkeypatch, raw_tools=[item, item])
    service, _ = _service(tmp_path)
    result, _, _ = _confirmed(service)
    assert result["state"] == "failed", result
    assert result["error_code"] == "MCP_PACK_CATALOG_INVALID"
    assert result["details"]["reason"] == "pack_catalog_invalid"
    assert observed == {"starts": 1, "lists": 1, "stops": 1}


def test_component_concurrent_revoke_after_candidate_stopped_cannot_reactivate(tmp_path, monkeypatch):
    from agent_py_agent.agent.plugin_enable_tool import PluginEnableTool

    observed = _component_transport(monkeypatch)
    service, _ = _service(tmp_path)
    reached, resume = Event(), Event()
    original = PluginEnableTool._candidate

    def candidate(self, operation, installation):
        result = original(self, operation, installation)
        reached.set()
        assert resume.wait(10)
        return result

    monkeypatch.setattr(PluginEnableTool, "_candidate", candidate)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_confirmed, service)
        try:
            assert reached.wait(10)
            preparing = _roundtrip(service.installations)[0]
            revoked = service.installations.change_activation(PluginActivationRequest("disable-race", preparing.revision,
                replace(preparing.activation, phase="revoked"))).installation
        finally:
            resume.set()
        result, _, _ = future.result(timeout=10)
    assert result["state"] == "failed" and result["details"]["reason"] == "revision_conflict"
    assert _roundtrip(service.installations) == (revoked,)
    assert observed == {"starts": 1, "lists": 1, "stops": 1}


def test_pinned_preparing_active_revoked_digest_lockstep_and_byte_identical_pin(tmp_path):
    service, _ = manager(tmp_path)
    store = service.installations
    entry = store.install(PluginInstallRequest(_build(), "install-pack", 0)).installation
    plan = PluginEnvironmentPlan("enable-pack", "w2-pack", entry.package_sha256, entry.revision, 0, "a" * 64)
    preparing = store.change_activation(PluginActivationRequest(plan.operation_id, entry.revision, PluginActivation(plan, "preparing")))
    assert _roundtrip(store) == (preparing.installation,)
    pin = pinned_catalog_json_from_tools([MCPToolInfo("inspect", "检查输入", {"type": "object", "properties": {}})])
    active = replace(preparing.installation.activation, phase="active", pinned_catalog_json=pin,
                     catalog_sha256=catalog_digest_of(pinned_tool_declarations(pin)))
    published_request = PluginActivationRequest(plan.operation_id, preparing.installation.revision, active)
    published = store.change_activation(published_request).installation
    assert active.catalog_sha256 == activation_catalog_digest(entry.manifest, active)
    assert _roundtrip(store) == (published,)
    assert store.change_activation(published_request).outcome == "replayed"
    revoked = store.change_activation(PluginActivationRequest("disable-pack", published.revision,
                                    replace(active, phase="revoked"))).installation
    assert _roundtrip(store) == (revoked,)
    assert revoked.activation.pinned_catalog_json.encode() == pin.encode()
    with pytest.raises(PluginInstallationError) as error:
        store.change_activation(replace(published_request, expected_revision=revoked.revision))
    assert error.value.reason == "activation_revoked"
    other_pin = pinned_catalog_json_from_tools([MCPToolInfo("changed", "不同", {})])
    with pytest.raises(PluginInstallationError) as error:
        store.change_activation(PluginActivationRequest("disable-again", revoked.revision,
            replace(revoked.activation, pinned_catalog_json=other_pin)))
    assert error.value.reason == "activation_catalog_conflict"
    assert _roundtrip(store) == (revoked,)


@pytest.mark.parametrize("mode", ["pinned_on_enable", "declared"])
def test_real_management_preview_confirm_candidate_and_replay(tmp_path, mode):
    service, _ = _service(tmp_path, mode=mode)
    result, confirmation, revision = _confirmed(service)
    assert result["state"] == "succeeded", result
    assert result["details"]["candidate_cleanup"]["confirmed"] is True
    assert result["details"]["tools"] == [plugin_tool_name("w2-pack", "inspect")]
    entry = _roundtrip(service.installations)[0]
    assert entry.enabled and entry.activation.phase == "active"
    pin = entry.activation.pinned_catalog_json
    assert (pin is not None) is (mode == "pinned_on_enable")
    data = service.context.owner.plugins_dir / "data" / "w2-pack"
    assert (data / "launches").read_text() == "1\n"
    assert (data / "lists").read_text() == "1\n"
    repeated = _enable(service, "enable", confirmation["confirm_code"], revision=revision)
    assert repeated["details"] == result["details"]
    assert _roundtrip(service.installations) == (entry,)
    assert (data / "lists").read_text() == "1\n"
    environment = service.context.owner.plugins_dir / "environments" / entry.activation.plan.environment_ref / "files"
    assert (environment / "server.py").is_file()
    assert not (environment / "CAPABILITY.md").exists()


@pytest.mark.parametrize("infos", [[], [
    {"name": "inspect", "description": "检查", "inputSchema": {"type": "object"}},
    {"name": "inspect", "description": "重复", "inputSchema": {"type": "object"}},
]])
def test_invalid_catalog_has_known_failed_outcome_after_confirmed_stop(tmp_path, monkeypatch, infos):
    from agent_py_agent.agent.plugin_enable_tool import PluginEnableTool

    original, outcomes = PluginEnableTool.execute, []

    def observed(self, params):
        outcome = original(self, params)
        outcomes.append(outcome)
        return outcome

    monkeypatch.setattr(PluginEnableTool, "execute", observed)
    service, _ = _service(tmp_path, infos=infos)
    result, _, _ = _confirmed(service)
    assert result["state"] == "failed", result
    assert result["error_code"] == "MCP_PACK_CATALOG_INVALID"
    assert outcomes[-1].effect_outcome == "failed"
    assert result["details"]["reason"] == "pack_catalog_invalid"
    assert _roundtrip(service.installations)[0].activation.phase == "preparing"


def test_event_ordered_disable_wins_before_active_cas_and_candidate_is_stopped(tmp_path, monkeypatch):
    from agent_py_agent.agent.plugin_enable_tool import PluginEnableTool

    service, _ = _service(tmp_path)
    reached, resume = Event(), Event()
    original = PluginEnableTool._candidate

    def candidate(self, operation, installation):
        answer = original(self, operation, installation)
        reached.set()
        assert resume.wait(15)
        return answer

    monkeypatch.setattr(PluginEnableTool, "_candidate", candidate)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_confirmed, service)
        try:
            assert reached.wait(20)
            preparing = _roundtrip(service.installations)[0]
            assert preparing.activation.phase == "preparing"
            revoked = service.installations.change_activation(PluginActivationRequest("racing-disable", preparing.revision,
                replace(preparing.activation, phase="revoked"))).installation
        finally:
            resume.set()
        result, _, _ = future.result(timeout=20)
    assert result["state"] == "failed", result
    assert result["details"]["reason"] == "revision_conflict"
    assert _roundtrip(service.installations) == (revoked,)
    from agent_py_agent.agent.tooling.process_session_store import (
        ProcessSessionStore,
        process_session_store_root,
    )

    home = service.context.owner.home_dir
    records, errors = ProcessSessionStore(process_session_store_root(home, home)).list_records()
    assert not errors and records
    assert all(row["status"] in {"exited", "killed"} for row in records)
