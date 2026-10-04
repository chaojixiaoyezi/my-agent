"""B1 清单/确认/构建及启用关闭门合同；只在临时 owner 安装，不启动插件、Gateway 或模型。"""

from __future__ import annotations

import copy
import io
import json
import zipfile
from dataclasses import FrozenInstanceError, replace
from itertools import product
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.plugin_files_environment import inspect_plugin_files
from agent_py_agent.agent.plugin_manifest import PluginManifest, PluginPackageError
from agent_py_agent.agent.plugin_package import inspect_plugin_package
from agent_py_agent.agent.plugin_runtime_facts import (
    PluginRuntimeFacts,
    confirmation_code,
    confirmation_details,
    confirmation_message,
)
from agent_py_agent.tests.test_capability_package import content_bundle
from agent_py_agent.tests.test_plugin_any_language import (
    _declaration,
    _enable,
    _installed,
    _payload,
)
from agent_py_agent.tests.test_plugin_package import _manifest, _v5_manifest, _wheel
from scripts.build_plugin_files_package import build_files_package

_PROGRAM = b"raise RuntimeError('must not execute')\n"
_EVENTS = (
    "prompt_submitted", "turn_started", "turn_ended", "tool_call_started",
    "tool_call_finished", "command_executed",
)
_GOLDEN = Path(__file__).with_name("fixtures") / "plugin_manifest_v1_v7.json"


def legacy_payload(version: int) -> dict:
    """供固定基线采样；旧字节 fixture 不在测试运行时重建。"""
    if version == 7:
        return inspect_plugin_package(content_bundle()).manifest.to_payload()
    if version == 6:
        return _payload(_declaration(), {"bin/server.py": _PROGRAM})
    result = _v5_manifest(_wheel()) if version == 5 else _manifest(_wheel())
    result["schema_version"] = f"plugin_package.v{version}"
    if version >= 2:
        result["panels"] = [{"id": "state", "title": "状态", "kind": "text", "topics": ["activity"]}]
    if version >= 3:
        result["skills"] = ["hello"]
    if version >= 4:
        result["host_api"] = ["read"]
    return json.loads(json.dumps(result))


def _v8(**changes) -> dict:
    result = legacy_payload(6)
    result.update(schema_version="plugin_package.v8", events=[{"type": "prompt_submitted", "content": "none"}],
                  tool_gates=[{"id": "guard-rm", "tools": ["run_command"], "effects": [], "arguments": "full"}],
                  permissions={"network": False})
    result.update(changes)
    return result


def _bundle(payload: dict) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("plugin.json", json.dumps(payload, ensure_ascii=False))
        archive.writestr("bin/server.py", _PROGRAM)
    return stream.getvalue()


def _confirmation(payload: dict) -> dict:
    package = inspect_plugin_package(_bundle(payload))
    # 固定相同包摘要，确保测试确实验证新增四项，而非只靠已有 package_sha256 改变。
    return confirmation_details(package.manifest, "a" * 64, PluginRuntimeFacts("executable", "test-platform"),
                                inspect_plugin_files(package))


@pytest.mark.parametrize("version", range(1, 8))
def test_v1_v7_serialization_is_byte_exact_against_274cedb1e(version):
    golden = json.loads(_GOLDEN.read_text(encoding="utf-8"))
    expected = golden["payloads"][str(version)].encode("utf-8")
    restored = PluginManifest.from_payload(json.loads(expected))
    actual = json.dumps(restored.to_payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    assert actual == expected
    assert not {"events", "tool_gates", "permissions"} & restored.to_payload().keys()


@pytest.mark.parametrize("event_type", _EVENTS)
def test_each_event_type_accepts_no_content_and_defaults_to_none(event_type):
    manifest = PluginManifest.from_payload(_v8(events=[{"type": event_type}]))
    assert manifest.to_payload()["events"] == [{"type": event_type, "content": "none"}]


def test_only_prompt_accepts_text():
    manifest = PluginManifest.from_payload(_v8(events=[{"type": "prompt_submitted", "content": "text"}]))
    assert manifest.events[0].content == "text"


@pytest.mark.parametrize("event_type", ["turn_started", "turn_ended", "tool_call_started",
                                      "tool_call_finished", "command_executed"])
def test_other_events_reject_text(event_type):
    with pytest.raises(PluginPackageError) as error:
        PluginManifest.from_payload(_v8(events=[{"type": event_type, "content": "text"}]))
    assert error.value.reason == "invalid_manifest"


def test_tool_start_text_is_rejected_by_direct_declaration_and_zip_reader():
    from agent_py_agent.agent.plugin_events.declarations import PluginEventDeclaration

    with pytest.raises(ValueError, match="这类事件不提供正文"):
        PluginEventDeclaration("tool_call_started", "text")
    with pytest.raises(PluginPackageError) as error:
        inspect_plugin_package(_bundle(_v8(events=[{"type": "tool_call_started", "content": "text"}])))
    assert error.value.reason == "invalid_manifest"


@pytest.mark.parametrize("events,gates", [
    ([{"type": "prompt_submitted", "content": "text"}], []),
    ([], [{"id": "guard-rm", "tools": ["run_command"], "effects": [], "arguments": "full"}]),
], ids=["events-only", "gates-only"])
def test_v8_subscriptions_count_as_contribution_without_tools_or_panels(events, gates):
    payload = _v8(tools=[], panels=[], actions=[], default_action="", events=events, tool_gates=gates)
    manifest = PluginManifest.from_payload(payload)
    assert manifest.tools == () and manifest.panels == () and manifest.actions == ()
    assert manifest.events or manifest.tool_gates
    assert manifest.to_payload()["schema_version"] == "plugin_package.v8"
    assert PluginManifest.from_payload(manifest.to_payload()) == manifest
    assert inspect_plugin_package(_bundle(payload)).manifest == manifest
    assert replace(manifest, tools=(), panels=()) == manifest


def test_v6_without_tools_or_panels_still_fails():
    payload = legacy_payload(6)
    payload.update(tools=[], panels=[], actions=[], default_action="")
    with pytest.raises(PluginPackageError) as error:
        PluginManifest.from_payload(payload)
    assert error.value.reason == "invalid_manifest"
    assert str(error.value.__cause__) == "工具重名，或既没有工具也没有面板"


def test_v8_all_four_contributions_empty_still_fails_subscription_gate():
    with pytest.raises(PluginPackageError) as error:
        PluginManifest.from_payload(_v8(tools=[], panels=[], actions=[], default_action="", events=[], tool_gates=[]))
    assert error.value.reason == "invalid_manifest"
    assert str(error.value.__cause__) == "v8 必须使用文件入口并声明订阅"


@pytest.mark.parametrize("events,gates", [
    ([{"type": name, "content": "none"} for name in _EVENTS], []),
    ([], [{"id": f"gate-{i}", "tools": [f"future_tool_{j}" for j in range(16)],
           "effects": [], "arguments": "full"} for i in range(4)]),
    ([], [{"id": "x", "tools": [], "effects": ["mutating", "dangerous"], "arguments": "none"}]),
    ([], [{"id": "g" * 32, "tools": ["plugin__other__read"], "effects": ["mutating"], "arguments": "none"}]),
])
def test_valid_event_and_gate_boundaries(events, gates):
    manifest = PluginManifest.from_payload(_v8(events=events, tool_gates=gates))
    assert manifest.to_payload()["schema_version"] == "plugin_package.v8"
    assert PluginManifest.from_payload(manifest.to_payload()) == manifest


@pytest.mark.parametrize("network", [False, True])
def test_network_permission_is_a_real_boolean(network):
    manifest = PluginManifest.from_payload(_v8(permissions={"network": network}))
    assert manifest.permissions.network is network


def test_default_network_and_optional_content_are_canonical():
    manifest = PluginManifest.from_payload(_v8(events=[{"type": "prompt_submitted"}], permissions={}))
    assert manifest.to_payload()["permissions"] == {"network": False}
    assert manifest.events[0].content == "none"


@pytest.mark.parametrize("change", [
    lambda p: p.update(events="prompt_submitted"),
    lambda p: p.update(events=[None]),
    lambda p: p.update(events=[{"type": "not_an_event"}]),
    lambda p: p["events"][0].update(type=4),
    lambda p: p["events"][0].update(content="full"),
    lambda p: p["events"][0].update(content=True),
    lambda p: p["events"][0].update(extra="unknown"),
    lambda p: p["events"][0].pop("type"),
    lambda p: p["events"].append(copy.deepcopy(p["events"][0])),
    lambda p: p.update(events=[{"type": name} for name in _EVENTS] + [{"type": "prompt_submitted"}]),
    lambda p: p.update(tool_gates="guard"),
    lambda p: p.update(tool_gates=[None]),
    lambda p: p.update(tool_gates=[{**p["tool_gates"][0], "id": f"g{i}"} for i in range(5)]),
    lambda p: p["tool_gates"].append(copy.deepcopy(p["tool_gates"][0])),
    lambda p: p["tool_gates"][0].update(id=""),
    lambda p: p["tool_gates"][0].update(id="g" * 33),
    lambda p: p["tool_gates"][0].update(id="Guard"),
    lambda p: p["tool_gates"][0].update(id="a_b"),
    lambda p: p["tool_gates"][0].update(id=3),
    lambda p: p["tool_gates"][0].update(tools="run_command"),
    lambda p: p["tool_gates"][0].update(tools=["run_*"]),
    lambda p: p["tool_gates"][0].update(tools=["run?"]),
    lambda p: p["tool_gates"][0].update(tools=[None]),
    lambda p: p["tool_gates"][0].update(tools=[f"tool_{i}" for i in range(17)]),
    lambda p: p["tool_gates"][0].update(effects="dangerous"),
    lambda p: p["tool_gates"][0].update(effects=["read_only"], arguments="none"),
    lambda p: p["tool_gates"][0].update(effects=[True], arguments="none"),
    lambda p: p["tool_gates"][0].update(tools=[], effects=[], arguments="none"),
    lambda p: p["tool_gates"][0].update(arguments="text"),
    lambda p: p["tool_gates"][0].update(arguments=True),
    lambda p: p["tool_gates"][0].update(tools=[], effects=["dangerous"], arguments="full"),
    lambda p: p["tool_gates"][0].update(effects=["dangerous"], arguments="full"),
    lambda p: p["tool_gates"][0].update(extra="unknown"),
    lambda p: p["tool_gates"][0].pop("arguments"),
    lambda p: p.update(permissions=None),
    lambda p: p.update(permissions={"network": 1}),
    lambda p: p.update(permissions={"network": "false"}),
    lambda p: p.update(permissions={"network": None}),
    lambda p: p.update(permissions={"network": False, "read": True}),
    lambda p: p.update(events=[], tool_gates=[]),
    lambda p: p.pop("permissions"),
    lambda p: p.update(owner="local/main"),
    lambda p: p.update(entry_module="peek"),
])
def test_invalid_v8_declarations_are_structured_failures(change):
    payload = _v8()
    change(payload)
    with pytest.raises(PluginPackageError) as error:
        PluginManifest.from_payload(payload)
    assert error.value.reason == "invalid_manifest"


def test_host_api_conflict_reason_survives_manifest_and_zip_reader():
    payload = _v8(host_api=["read"])
    for read in (PluginManifest.from_payload, lambda p: inspect_plugin_package(_bundle(p))):
        with pytest.raises(PluginPackageError) as error:
            read(payload)
        assert error.value.reason == "events_with_host_api_unsupported"


@pytest.mark.parametrize("version", range(1, 8))
@pytest.mark.parametrize("field,value", [("events", []), ("tool_gates", []), ("permissions", {"network": False})])
def test_v1_v7_cannot_smuggle_even_empty_v8_fields(version, field, value):
    payload = legacy_payload(version)
    payload[field] = value
    with pytest.raises(PluginPackageError) as error:
        PluginManifest.from_payload(payload)
    assert error.value.reason == "invalid_manifest"


def test_v8_is_deeply_immutable_and_cannot_be_converted_to_python_or_content():
    payload = _v8()
    manifest = PluginManifest.from_payload(payload)
    payload["events"][0]["content"] = "text"
    payload["tool_gates"][0]["tools"].append("delete_file")
    payload["permissions"]["network"] = True
    projection = manifest.to_payload()
    projection["tool_gates"][0]["tools"].clear()
    assert manifest.events[0].content == "none"
    assert manifest.tool_gates[0].tools == ("run_command",)
    assert manifest.permissions.network is False
    with pytest.raises(FrozenInstanceError):
        manifest.permissions.network = True
    python = PluginManifest.from_payload(legacy_payload(1))
    content = PluginManifest.from_payload(legacy_payload(7))
    for other in (python, content):
        with pytest.raises(ValueError):
            replace(other, events=manifest.events, tool_gates=manifest.tool_gates, permissions=manifest.permissions)
    with pytest.raises(ValueError):
        replace(manifest, permissions=None)


def test_confirmation_contains_all_four_v8_facts_but_keeps_v6_unchanged():
    details = _confirmation(_v8(events=[{"type": "prompt_submitted", "content": "text"}]))
    assert details["events"] == [{"type": "prompt_submitted", "content": "text"}]
    assert details["tool_gates"] == [{"id": "guard-rm", "tools": ["run_command"], "effects": [], "arguments": "full"}]
    assert details["network"] is False
    assert details["sandbox"] == "required"
    assert details["read_scope"] == "插件只能读它自己的目录、解释器所在目录和系统目录，读不到你家目录里的其它文件"
    assert not {"events", "tool_gates", "network", "sandbox"} & _confirmation(legacy_payload(6)).keys()


@pytest.mark.parametrize("changes", [
    {"events": [{"type": "turn_started", "content": "none"}]},
    {"events": [{"type": "prompt_submitted", "content": "text"}]},
    {"tool_gates": [{"id": "guard-rm", "tools": ["delete_file"], "effects": [], "arguments": "full"}]},
    {"tool_gates": [{"id": "guard-rm", "tools": ["run_command"], "effects": [], "arguments": "none"}]},
    {"tool_gates": [{"id": "guard-all", "tools": [], "effects": ["dangerous"], "arguments": "none"}]},
    {"permissions": {"network": True}},
])
def test_changing_subscriptions_or_permissions_changes_confirmation_with_same_package_hash(changes):
    before = _confirmation(_v8())
    after = _confirmation(_v8(**changes))
    assert before["package_sha256"] == after["package_sha256"]
    assert confirmation_code(before) != confirmation_code(after)
    assert confirmation_code(before) == confirmation_code(copy.deepcopy(before))


def test_confirmation_preview_explains_full_arguments_network_and_required_sandbox():
    details = _confirmation(_v8(events=[{"type": "prompt_submitted", "content": "text"}], permissions={"network": True}))
    details["confirm_code"] = confirmation_code(details)
    message = confirmation_message(details)
    assert "prompt_submitted" in message and "提示文字" in message
    assert "能看到这些工具的完整参数：run_command" in message
    assert "允许联网" in message and "强制" in message and "沙箱" in message
    assert "插件只能读它自己的目录、解释器所在目录和系统目录，读不到你家目录里的其它文件" in message
    assert message.endswith(f"/plugins enable sample-any --confirm {details['confirm_code']}")


def test_confirmation_preview_explains_effect_gate_without_full_arguments():
    details = _confirmation(_v8(events=[{"type": "tool_call_started", "content": "none"}],
                               tool_gates=[{"id": "guard-all", "tools": [], "effects": ["dangerous"], "arguments": "none"}]))
    message = confirmation_message(details)
    assert "tool_call_started（不含正文）" in message
    assert "工具参数" not in message and "能看到这些工具的完整参数" not in message
    assert "dangerous" in message and "不提供完整参数" in message
    assert "禁止联网" in message


def test_confirmation_event_preview_never_offers_tool_arguments():
    from agent_py_agent.agent.plugin_events.confirmation import event_confirmation_lines

    # 即使投影收到未验证的旧式事实，也不能在观察段声称能看到工具参数。
    lines = event_confirmation_lines({"events": [{"type": "tool_call_started", "content": "text"}],
                                      "tool_gates": [], "network": False})
    assert lines[0] == "订阅事件：tool_call_started（不含正文）"
    assert not any("工具参数" in line for line in lines)


@pytest.mark.parametrize("events,gates", [
    ([{"type": "prompt_submitted"}], []),
    ([], [{"id": "guard", "tools": ["run_command"], "effects": [], "arguments": "full"}]),
], ids=["events-only", "gates-only"])
def test_builder_emits_v8_with_only_subscriptions_without_execution(tmp_path, monkeypatch, events, gates):
    monkeypatch.setattr("subprocess.Popen", lambda *a, **kw: pytest.fail("静态构建不能启动插件"))
    root = tmp_path / "files"
    (root / "bin").mkdir(parents=True)
    (root / "bin/server.py").write_bytes(_PROGRAM)
    declaration = _declaration(tools=[], panels=[], actions=[], default_action="", events=events, tool_gates=gates)
    package = build_files_package(declaration, root, tmp_path / "subscriptions.zip")
    manifest = inspect_plugin_package(package.read_bytes()).manifest
    assert manifest.to_payload()["schema_version"] == "plugin_package.v8"
    assert manifest.tools == () and manifest.panels == () and manifest.actions == ()
    assert manifest.events or manifest.tool_gates
    assert _confirmation(manifest.to_payload())["sandbox"] == "required"


def test_builder_emits_reproducible_v8_and_reads_it_back_without_execution(tmp_path, monkeypatch):
    monkeypatch.setattr("subprocess.Popen", lambda *a, **kw: pytest.fail("静态构建不能启动插件"))
    root = tmp_path / "files"
    (root / "bin").mkdir(parents=True)
    (root / "bin/server.py").write_bytes(_PROGRAM)
    declaration = _declaration(events=[{"type": "prompt_submitted"}],
                               tool_gates=[{"id": "guard", "tools": ["future_tool"], "effects": [], "arguments": "full"}])
    first = build_files_package(declaration, root, tmp_path / "a.zip")
    second = build_files_package(declaration, root, tmp_path / "b.zip")
    assert first.read_bytes() == second.read_bytes()
    manifest = inspect_plugin_package(first.read_bytes()).manifest
    assert manifest.to_payload()["schema_version"] == "plugin_package.v8"
    assert manifest.permissions.network is False
    assert manifest.tool_gates[0].tools == ("future_tool",)


@pytest.mark.parametrize("changes,reason", [
    ({"events": [], "tool_gates": []}, "invalid_manifest"),
    ({"events": [{"type": "tool_call_started", "content": "text"}]}, "invalid_manifest"),
    ({"events": [{"type": "prompt_submitted"}], "host_api": ["read"]}, "events_with_host_api_unsupported"),
    ({"events": [{"type": "prompt_submitted"}], "permissions": {"network": "false"}}, "invalid_manifest"),
])
def test_builder_does_not_publish_bad_v8_or_silently_downgrade(tmp_path, changes, reason):
    root = tmp_path / "files"
    (root / "bin").mkdir(parents=True)
    (root / "bin/server.py").write_bytes(_PROGRAM)
    target = tmp_path / "bad.zip"
    with pytest.raises(PluginPackageError) as error:
        build_files_package(_declaration(**changes), root, target)
    assert error.value.reason == reason
    assert not target.exists()


def test_v8_disabled_rejects_code_from_previous_subscriptions(monkeypatch):
    from agent_py_agent.agent import plugin_enable_tool as enable

    old_details = _confirmation(_v8())
    current_bytes = _bundle(_v8(permissions={"network": True}))
    current_package = inspect_plugin_package(current_bytes)
    tool = enable.PluginEnableTool.__new__(enable.PluginEnableTool)
    tool.owner = object()
    tool.installation = SimpleNamespace(manifest=current_package.manifest)
    tool.runtime = PluginRuntimeFacts("executable", "test-platform")
    tool.runtime_error, tool.plan, tool.catalog_revision = "", None, "fixed-revision"
    # B7：v8 事件插件；总开关关着（events_enabled=False）→ 启用被拒，旧确认码更不可能兑现。
    tool._is_v8, tool.events_enabled, tool.events_owner_allowed = True, False, True
    store = SimpleNamespace(package_bytes=lambda installation: current_bytes)
    monkeypatch.setattr(enable, "PluginInstallStore", lambda owner: store)
    monkeypatch.setattr(tool, "_enable", lambda: pytest.fail("旧确认码不得执行启用"))
    current = tool._confirmation()
    assert current["network"] is True and current["sandbox"] == "required"
    outcome = tool.execute({"plugin": "sample-any", "catalog_revision": "fixed-revision",
                            "confirm": confirmation_code(old_details)})
    assert outcome.ok is False
    assert outcome.error_code == "TOOL_EXECUTION_FAILED"
    assert outcome.effect_outcome == "not_started"
    details = outcome.result_envelope[enable.PLUGIN_ENABLE_TOOL]
    assert details == {"reason": "plugin_events_disabled", "commit_state": "not_committed"}


@pytest.mark.parametrize("options", tuple(product((False, True), repeat=3)),
                         ids=lambda row: f"confirm-{row[0]}-network-{row[1]}-sandbox-{row[2]}")
def test_v8_disabled_before_confirmation_keeps_real_installation_unchanged(tmp_path, monkeypatch, options):
    from agent_py_agent.agent import plugin_enable_tool as enable
    from agent_py_agent.agent.plugin_management import PluginManagement

    confirmed, network, sandbox = options
    declaration = _declaration(events=[{"type": "prompt_submitted"}], tool_gates=[], permissions={"network": network})
    service = _installed(tmp_path, declaration, {"bin/server.py": _PROGRAM})
    monkeypatch.setattr(enable, "prepare_plugin_environment", lambda *a, **kw: pytest.fail("v8 关闭门不能准备环境"))
    monkeypatch.setattr(enable.PluginMCPClient, "start", lambda self: pytest.fail("v8 关闭门不能启动插件"))
    service = PluginManagement(replace(service.context, process_sandbox=sandbox))
    owner, entry = service.context.owner, service.installations.snapshot()[0]
    assert entry.manifest.permissions is not None and entry.activation is None and not entry.enabled
    package = inspect_plugin_package(service.installations.package_bytes(entry))
    facts = enable.resolve_plugin_runtime(entry.manifest)
    details = confirmation_details(entry.manifest, package.sha256, facts, inspect_plugin_files(package))
    code = confirmation_code(details) if confirmed else ""
    original = (owner.plugins_dir / "installations.json").read_bytes()
    calls = []
    monkeypatch.setattr(enable.PluginEnableTool, "_enable", lambda self: calls.append("enable") or {})
    monkeypatch.setattr(enable.PluginEnableTool, "_confirmation", lambda self: pytest.fail("必须在索取确认码之前拒绝"))
    binding = SimpleNamespace(request=SimpleNamespace(operation_id="enable-v8"))
    # events_enabled 默认 False（总开关关）→ v8 启用在索取确认码之前就被拒，真实安装一字节不动。
    tool = enable.PluginEnableTool(owner, object(), binding, entry, "fixed", policy=enable.PluginEnablePolicy(process_sandbox=sandbox))
    outcome = tool.execute({"plugin": entry.manifest.plugin_id, "catalog_revision": "fixed", "confirm": code})
    assert outcome.ok is False and outcome.error_code == "TOOL_EXECUTION_FAILED"
    assert outcome.effect_outcome == "not_started"
    assert outcome.result_envelope[enable.PLUGIN_ENABLE_TOOL] == {
        "reason": "plugin_events_disabled", "commit_state": "not_committed",
    }
    # B7：v8 现在和旧版可执行插件一样会先构造运行计划（纯内存，不写盘），隔离在 PluginMCPClient 强制套上；
    # 关闭门改为执行期按总开关判定，所以不再断言“构造期不建计划”，只断言拒绝后真实安装未变。
    result = _enable(service, "enable-v8", code)
    assert result["state"] == "failed" and result["details"] == outcome.result_envelope[enable.PLUGIN_ENABLE_TOOL]
    assert not calls
    assert service.installations.snapshot() == (entry,)
    assert (owner.plugins_dir / "installations.json").read_bytes() == original
    assert not (owner.plugins_dir / "environments").exists()


def test_v8_enable_requires_local_main_owner(tmp_path, monkeypatch):
    """B7：总开关开着，但当前 owner 不是本机管理员（local/main）→ plugin_events_owner_not_allowed，索取确认码之前就拒。"""
    from agent_py_agent.agent import plugin_enable_tool as enable

    declaration = _declaration(events=[{"type": "prompt_submitted"}], tool_gates=[], permissions={"network": False})
    service = _installed(tmp_path, declaration, {"bin/server.py": _PROGRAM})
    owner, entry = service.context.owner, service.installations.snapshot()[0]
    monkeypatch.setattr(enable.PluginEnableTool, "_enable", lambda self: pytest.fail("owner 不允许时不能启用"))
    monkeypatch.setattr(enable.PluginEnableTool, "_confirmation", lambda self: pytest.fail("必须在索取确认码之前拒绝"))
    binding = SimpleNamespace(request=SimpleNamespace(operation_id="enable-v8-owner"))
    tool = enable.PluginEnableTool(owner, object(), binding, entry, "fixed",
                                   policy=enable.PluginEnablePolicy(events_enabled=True, events_owner_allowed=False))
    outcome = tool.execute({"plugin": entry.manifest.plugin_id, "catalog_revision": "fixed", "confirm": ""})
    assert outcome.ok is False and outcome.error_code == "TOOL_EXECUTION_FAILED"
    assert outcome.result_envelope[enable.PLUGIN_ENABLE_TOOL]["reason"] == "plugin_events_owner_not_allowed"


def test_v8_enable_passes_gate_when_switch_on_and_owner_allowed(tmp_path, monkeypatch):
    """B7：总开关开 + 本机管理员 → 越过关闭门（v8 强制沙箱由 process_sandbox 恒 True 落实），进入确认/启用。"""
    from agent_py_agent.agent import plugin_enable_tool as enable

    monkeypatch.setattr(enable, "plugin_sandbox_problem", lambda *args, **kwargs: "")
    declaration = _declaration(events=[{"type": "prompt_submitted"}], tool_gates=[], permissions={"network": False})
    service = _installed(tmp_path, declaration, {"bin/server.py": _PROGRAM})
    owner, entry = service.context.owner, service.installations.snapshot()[0]
    reached = []
    monkeypatch.setattr(enable.PluginEnableTool, "_enable", lambda self: reached.append(self.process_sandbox) or {"enabled": True})
    binding = SimpleNamespace(request=SimpleNamespace(operation_id="enable-v8-ok"))
    tool = enable.PluginEnableTool(owner, object(), binding, entry, "fixed",
                                   policy=enable.PluginEnablePolicy(events_enabled=True, events_owner_allowed=True))
    # v8 强制沙箱：不管全局 plugin_process_sandbox 开没开，这里都应为 True。
    assert tool._is_v8 and tool.process_sandbox is True
    confirmation = tool._confirmation()
    tool.execute({"plugin": entry.manifest.plugin_id, "catalog_revision": "fixed",
                  "confirm": confirmation["confirm_code"]})
    assert reached == [True], "越过关闭门后应进入启用，且强制沙箱"


def test_v8_enable_fails_closed_when_hidden_root_cannot_be_derived(tmp_path, monkeypatch):
    from agent_py_agent.agent import plugin_enable_tool as enable
    from agent_py_agent.agent import plugin_sandbox

    declaration = _declaration(events=[{"type": "prompt_submitted"}], tool_gates=[], permissions={"network": False})
    service = _installed(tmp_path, declaration, {"bin/server.py": _PROGRAM})
    owner, entry = service.context.owner, service.installations.snapshot()[0]
    binding = SimpleNamespace(request=SimpleNamespace(operation_id="enable-v8-hidden-root"))
    tool = enable.PluginEnableTool(
        owner, object(), binding, entry, "fixed",
        policy=enable.PluginEnablePolicy(events_enabled=True, events_owner_allowed=True),
    )
    monkeypatch.setattr(plugin_sandbox.Path, "home", lambda: None)
    monkeypatch.setattr(enable.PluginEnableTool, "_confirmation", lambda self: pytest.fail("安全自检失败前不得索取确认"))
    monkeypatch.setattr(enable.PluginEnableTool, "_enable", lambda self: pytest.fail("隐藏根缺失时不得准备环境"))

    outcome = tool.execute({"plugin": entry.manifest.plugin_id, "catalog_revision": "fixed", "confirm": ""})

    assert outcome.ok is False and outcome.error_code == "TOOL_EXECUTION_FAILED"
    assert outcome.effect_outcome == "not_started"
    assert outcome.result_envelope[enable.PLUGIN_ENABLE_TOOL] == {
        "reason": "sandbox_unavailable", "commit_state": "not_committed",
    }
    assert service.installations.snapshot() == (entry,)
    assert not (owner.plugins_dir / "environments").exists()


@pytest.mark.parametrize("confirmed", [False, True], ids=["no-code", "correct-code"])
def test_v6_original_confirmation_path_is_not_disabled(monkeypatch, confirmed):
    from agent_py_agent.agent import plugin_enable_tool as enable

    package_bytes = _bundle(legacy_payload(6))
    manifest = inspect_plugin_package(package_bytes).manifest
    tool = enable.PluginEnableTool.__new__(enable.PluginEnableTool)
    tool.owner, tool.installation = object(), SimpleNamespace(manifest=manifest, activation=None)
    tool.runtime = PluginRuntimeFacts("executable", "test-platform")
    tool.runtime_error, tool.plan, tool.catalog_revision = "", None, "fixed"
    tool._is_v8, tool.events_enabled, tool.events_owner_allowed = False, False, False  # v6 旧版不走 v8 门
    monkeypatch.setattr(enable, "PluginInstallStore", lambda owner: SimpleNamespace(package_bytes=lambda entry: package_bytes))
    calls = []
    monkeypatch.setattr(tool, "_enable", lambda: calls.append("enable") or {"enabled": True})
    confirmation = tool._confirmation()
    outcome = tool.execute({"plugin": manifest.plugin_id, "catalog_revision": "fixed",
                            "confirm": confirmation["confirm_code"] if confirmed else ""})
    assert manifest.permissions is None
    assert calls == (["enable"] if confirmed else [])
    assert outcome.ok is confirmed
    assert outcome.result_envelope[enable.PLUGIN_ENABLE_TOOL] == (
        {"enabled": True} if confirmed else {"reason": "confirmation_required", "state": "confirmation_required",
                                             "commit_state": "not_committed", "confirmation": confirmation})
