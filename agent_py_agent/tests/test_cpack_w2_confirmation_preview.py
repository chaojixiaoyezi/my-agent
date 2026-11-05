"""运行确认只绑定结构化事实；老插件预览字节不变，导入摘要不回显外部正文。"""
from __future__ import annotations

import builtins
import copy
import hashlib
import json
import os
from dataclasses import replace

import pytest

from agent_py_agent.agent import plugin_runtime_facts as facts
from agent_py_agent.agent.plugin_files_environment import inspect_plugin_files
from agent_py_agent.agent.plugin_runtime_facts import (
    PluginRuntimeFacts,
    confirmation_code,
    confirmation_details,
    confirmation_message,
)
from agent_py_agent.tests.test_cpack_w2_enable_pin_catalog import _build, _enable, _service
from agent_py_agent.tests.test_plugin_management import manager


@pytest.mark.parametrize("mode", ["declared", "pinned_on_enable"])
def test_pack_facts_are_pure_runtime_only_and_have_frozen_dto(mode, monkeypatch):
    manifest = _build(mode).manifest
    builder = getattr(facts, "pack_mcp_confirmation_facts", None)
    assert callable(builder), "缺少运行包确认事实构造器"
    calls = []
    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", lambda *a, **kw: calls.append("open"))
        patch.setattr(os, "stat", lambda *a, **kw: calls.append("stat"))
        result = builder(manifest, idle_stop_seconds=0)
    assert calls == []
    assert set(result) == {"kind", "package_id", "server", "entry", "files", "network", "workspace_access",
                           "data_dir", "tool_catalog", "per_call_approval", "idle_stop_seconds"}
    assert result["kind"] == "capability_pack_mcp" and result["package_id"] == "w2-pack"
    assert result["server"] == "checks" and result["entry"] == manifest.entry.to_payload()
    assert [item["path"] for item in result["files"]] == ["server.py"]
    assert result["network"] is False and result["workspace_access"] == "none"
    assert result["data_dir"] == "plugins/data/w2-pack"
    assert result["per_call_approval"] == "dangerous" and result["idle_stop_seconds"] == 0
    assert result["tool_catalog"] == {"mode": mode, "tools": ["inspect"] if mode == "declared" else []}


@pytest.mark.parametrize("mode,idle", [("declared", 600), ("pinned_on_enable", 0)])
def test_real_preview_binds_runtime_file_sizes_and_plain_chinese_message(tmp_path, mode, idle):
    from agent_py_agent.agent.plugin_management import PluginManagement

    service, _ = _service(tmp_path, mode=mode)
    service = PluginManagement(replace(service.context, capability_pack_mcp_idle_stop_seconds=idle))
    reply = _enable(service)
    details = reply["details"]["confirmation"]
    assert details["files"][0]["size"] == len(inspect_plugin_files(_build(mode))[0].content)
    assert details["confirm_code"] == confirmation_code({k: v for k, v in details.items() if k != "confirm_code"})
    assert details["interpreter"]["path"] and len(details["interpreter"]["sha256"]) == 64
    message = confirmation_message(details)
    assert "MCP" in message and "server.py" in message and details["interpreter"]["path"] in message
    assert "禁止联网" in message and "工作区" in message and "不能读写" in message
    assert "plugins/data/w2-pack" in message and "强制" in message and "沙箱" in message
    assert "危险" in message and "审批" in message and "订阅事件：无" not in message
    assert ("永不" in message) if idle == 0 else ("600" in message)
    assert ("inspect" in message) if mode == "declared" else ("固定" in message and "变化" in message)
    assert message.splitlines()[-1] == f"确认无误后输入：/plugins#w2-pack 启用 {details['confirm_code']}"


@pytest.mark.parametrize("field", ["interpreter_path", "interpreter_sha256", "platform", "file_digest", "network",
                                   "catalog_mode", "catalog_names", "idle_stop_seconds", "entry_args"])
def test_each_runtime_fact_changes_the_confirmation_code(tmp_path, field):
    service, _ = _service(tmp_path, mode="declared")
    details = _enable(service)["details"]["confirmation"]
    changed = copy.deepcopy({k: v for k, v in details.items() if k != "confirm_code"})
    if field.startswith("interpreter_"):
        changed["interpreter"][field.removeprefix("interpreter_")] += "changed"
    elif field == "file_digest":
        changed["files"][0]["sha256"] = "f" * 64
    elif field == "catalog_mode":
        changed["tool_catalog"]["mode"] = "pinned_on_enable"
    elif field == "catalog_names":
        changed["tool_catalog"]["tools"].append("other")
    elif field == "entry_args":
        changed["entry"]["args"].append("--other")
    elif field == "network":
        changed[field] = not changed[field]
    elif field == "idle_stop_seconds":
        changed[field] += 1
    else:
        changed[field] += "changed"
    assert confirmation_code(changed) != details["confirm_code"]


@pytest.mark.parametrize("network", [True, False])
def test_old_confirmation_bytes_remain_identical_to_anchor(network):
    # 固定完整旧输入与原文 oracle，不让新实现自生成期望值。
    confirmation = {"plugin_id": "old", "version": "1", "package_sha256": "a" * 64,
        "entry": {"kind": "executable", "command": "bin/run", "args": []}, "platform": "fixture",
        "declared_platforms": ["any"], "files": [], "confirm_code": "abcdef012345"}
    expected = ("启用前需要你确认：这个插件会以你本人的权限在本机运行下面的程序。\n"
        "插件：old 1（包摘要 aaaaaaaaaaaaaaaa…）\n入口：随包可执行文件 bin/run\n"
        "本机平台：fixture（包声明：any）\n随包文件（0 个）：\n"
        "确认无误后输入：/plugins enable old --confirm abcdef012345")
    if network:
        confirmation.update(events=[], tool_gates=[], network=False, sandbox="required", read_scope="原读范围")
        expected = expected.replace("确认无误后输入", "订阅事件：无\n网络权限：禁止联网（含本机回环）\n"
            "沙箱要求：强制使用插件进程沙箱；不可用时不得启用。\n原读范围\n确认无误后输入")
    assert confirmation_message(confirmation).encode() == expected.encode()


@pytest.mark.parametrize("tool", ["plugin_install", "plugin_update"])
def test_import_receipt_reaches_details_and_summary_uses_status_counts_only(tmp_path, tool):
    service, _ = manager(tmp_path)
    receipt = {"schema": "external_plugin_import_receipt.v1", "plugin_id": "w2-pack",
        "components": [{"status": status, "name": "untrusted body", "source_path": "untrusted path"}
            for status in ("imported", "imported", "mapped", "unsupported", "ignored")]}
    payload = {"ok": True, "state": "succeeded", "tool_name": tool,
               "result": {tool: {"plugin_id": "w2-pack", "enabled": False, "import_receipt": receipt}}}
    result = service._reply(payload)
    assert result["details"]["import_receipt"] == receipt
    assert "已导入为能力包" in result["message"]
    assert "2 项已导入" in result["message"] and "1 项已映射" in result["message"]
    assert "1 项不支持且未运行" in result["message"] and "/plugins#w2-pack 查看" in result["message"]
    assert "untrusted" not in result["message"]
    plain = service._reply({**payload, "result": {tool: {"plugin_id": "w2-pack", "enabled": False}}})
    assert plain["message"] == "插件管理请求已完成。"


def test_confirmation_details_legacy_digest_is_schema_stable():
    package = _build("declared")
    manifest = replace(package.manifest, capability=None, mcp_servers=(), permissions=None,
                       files=package.manifest.runtime_files)
    runtime = PluginRuntimeFacts("interpreter", "fixture", "python3", "/fixture/python3", "a" * 64)
    files = inspect_plugin_files(package)
    actual = confirmation_details(manifest, "b" * 64, runtime, files)
    assert "kind" not in actual and "tool_catalog" not in actual
    oracle = hashlib.sha256(json.dumps(actual, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:12]
    assert confirmation_code(actual) == oracle


@pytest.mark.parametrize("mode,idle", [("declared", 600), ("pinned_on_enable", 0)])
def test_component_confirmation_uses_real_package_bytes_without_os_launch(tmp_path, mode, idle):
    from types import SimpleNamespace

    from agent_py_agent.agent.plugin_enable_tool import PluginEnablePolicy, PluginEnableTool

    service, row = _service(tmp_path, mode=mode)
    tool = PluginEnableTool(service.context.owner, None,
        SimpleNamespace(request=SimpleNamespace(operation_id="component-preview")), row, "fixed",
        policy=PluginEnablePolicy(pack_mcp_enabled=True, pack_mcp_idle_stop_seconds=idle))
    details = tool._confirmation()
    assert details["files"][0]["size"] == len(inspect_plugin_files(_build(mode))[0].content)
    assert details["workspace_access"] == "none" and details["tool_catalog"]["mode"] == mode
    assert details["confirm_code"] == confirmation_code({key: value for key, value in details.items() if key != "confirm_code"})
    message = confirmation_message(details)
    assert "禁止联网" in message and "强制" in message and "不能读写工作区" in message
    assert "危险动作" in message and "审批" in message and "订阅事件：无" not in message
    assert message.endswith(f"确认无误后输入：/plugins#w2-pack 启用 {details['confirm_code']}")


def test_component_changed_idle_fact_invalidates_old_code_before_any_candidate(tmp_path, monkeypatch):
    from agent_py_agent.agent.plugin_management import PluginManagement
    from agent_py_agent.tests.test_cpack_w2_enable_pin_catalog import _component_transport

    observed = _component_transport(monkeypatch)
    service, row = _service(tmp_path)
    original = _enable(service)["details"]["confirmation"]
    changed = PluginManagement(replace(service.context, capability_pack_mcp_idle_stop_seconds=0))
    result = _enable(changed, "changed-fact", original["confirm_code"])
    assert result["details"]["reason"] == "confirmation_required"
    assert result["details"]["confirmation"]["confirm_code"] != original["confirm_code"]
    assert changed.installations.snapshot() == (row,)
    assert observed == {"starts": 0, "lists": 0, "stops": 0}
