"""W4 确认码与回滚：v10 可见，但永不代用户确认 MCP。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability import learnpack_installer as installer
from agent_py_agent.agent.capability import pack_commands
from agent_py_agent.agent.capability.learnpack_store import BuildProvenance, LearnpackStore
from agent_py_agent.agent.capability.package_build import build_capability_pack
from agent_py_agent.agent.capability_package_manifest import PACK_MCP_CONFIRMATION_KIND
from agent_py_agent.agent.plugin_commands import plugin_namespace
from agent_py_agent.agent.plugin_package import inspect_plugin_package
from agent_py_agent.tests.test_pack_commands import _agent, _manager, _tui


def make_pack(*, version="1.0.0", runtime=True, executable=False):
    files = {"CAPABILITY.md": b"# fixture\n"}
    declaration = {"plugin_id": "w4-demo", "version": version, "summary": "分镜方法",
                   "capability": {"description": "分镜", "keywords": ["分镜"], "entry_document": "CAPABILITY.md"},
                   "settings_schema": {"type": "object", "properties": {}, "additionalProperties": False}}
    if runtime:
        files["mcp/demo/server.py"] = b"print('fixture only')\n"
        entry = {"kind": "executable" if executable else "interpreter", "command": "server.py", "args": []}
        if not executable:
            entry["interpreter"] = "python3"
        declaration["mcp_servers"] = [{"name": "demo", "root": "mcp/demo", "entry": entry, "platforms": ["any"],
                                        "permissions": {"network": False}, "tool_catalog": "pinned_on_enable", "tools": []}]
    declaration["files"] = [{"path": path, **({"executable": True} if runtime and executable and path.endswith("server.py") else {})}
                            for path in files]
    return build_capability_pack(declaration, files), declaration, files


def confirmation():
    return {"kind": PACK_MCP_CONFIRMATION_KIND, "package_id": "w4-demo", "server": "demo",
            "entry": {"kind": "interpreter", "command": "server.py", "args": [], "interpreter": "python3"},
            "files": [{"path": "server.py", "sha256": "a" * 64, "size": 1, "executable": False}],
            "network": False, "workspace_access": "none", "data_dir": "plugins/data/w4-demo",
            "tool_catalog": {"mode": "pinned_on_enable", "tools": []}, "per_call_approval": "dangerous",
            "idle_stop_seconds": 30, "confirm_code": "012345abcdef"}


def test_enable_code_forwards_original_request_and_revision():
    built, _, _ = make_pack()
    row = SimpleNamespace(manifest=inspect_plugin_package(built.payload).manifest)
    calls = []

    def command(text, **kwargs):
        calls.append((text, kwargs))
        return {"ok": True}

    manager = SimpleNamespace(context=SimpleNamespace(is_admin=True), installations=SimpleNamespace(snapshot=lambda: (row,)), command=command)
    result = pack_commands.run_pack_command(manager, plugin_namespace("/plugins#w4-demo 启用 012345abcdef"), "original", "revision")
    assert result == {"forwarded_reply": {"ok": True}}
    assert calls == [("/plugins enable w4-demo --confirm 012345abcdef", {"revision": "revision", "request_id": "original", "subject": "能力包"})]


@pytest.mark.parametrize("code", ["ABCDEF012345", "a" * 11, "a" * 13, "zzzzzzzzzzzz", "one two"])
def test_invalid_confirmation_code_never_forwards(code):
    result = pack_commands.run_pack_command(SimpleNamespace(context=SimpleNamespace(is_admin=True)),
                                            plugin_namespace(f"/plugins#w4-demo 启用 {code}"))
    assert result["error_code"] == "INVALID_COMMAND_ARGUMENTS"


@pytest.mark.parametrize("user_session", [False, True])
def test_mcp_confirmation_is_never_auto_and_user_line_is_pack_grammar(user_session):
    value = confirmation()
    assert installer.auto_confirmable(value) is False
    assert installer._user_command("w4-demo", value, installer.InstallConsent(True, user_session)) == "/plugins#w4-demo 启用 012345abcdef"
    text = installer._needs_user_message(value, "fixture 1.0.0")
    assert "server.py" in text and "联网：否" in text and "确认" in text


def test_installer_runtime_rollback_sequence_stops_at_pack_code(tmp_path):
    built, _, _ = make_pack()
    store = LearnpackStore(tmp_path / "owner")
    record = store.save_build(built, BuildProvenance("自己写的", "MIT", "fixture"))
    old = SimpleNamespace(manifest=SimpleNamespace(plugin_id="w4-demo"), package_sha256="b" * 64, enabled=True)
    calls = []

    def command(text, **kwargs):
        calls.append(text)
        if text == "/plugins enable w4-demo":
            return {"ok": False, "details": {"reason": "confirmation_required", "confirmation": confirmation()}}
        return {"ok": True}

    manager = SimpleNamespace(installations=SimpleNamespace(snapshot=lambda: (old,)), command=command,
                              catalog=lambda: SimpleNamespace(revision="rev"))
    request = installer.InstallRequest(record, "/fixture.zip", installer.InstallConsent(True, True))
    outcome = installer.run_install(manager, request)
    assert calls == ["/plugins disable w4-demo", "/plugins update w4-demo /fixture.zip", "/plugins enable w4-demo"]
    assert outcome.state == installer.STATE_NEEDS_USER
    assert outcome.next_command == "/plugins#w4-demo 启用 012345abcdef"


def test_v10_list_and_view_without_import_receipt_and_v7_view_unchanged(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)
    built, _, _ = make_pack()
    path = agent.home_paths.owner_home_dir / "fixture.zip"
    path.write_bytes(built.payload)
    result = _tui(agent, f"/plugins install {path}")
    assert result["ok"], result
    listing = _tui(agent, "/plugins#")
    assert "w4-demo" in listing["message"]
    view = _tui(agent, "/plugins#w4-demo 查看")
    assert view["ok"] and "带 MCP 工具：是" in view["message"] and "服务 demo" in view["message"]
    assert "联网：否" in view["message"] and "启用时固定" in view["message"]
    plain, _, _ = make_pack(version="0.9.0", runtime=False)
    _tui(agent, "/plugins#w4-demo 停用")
    path.write_bytes(plain.payload)
    assert _tui(agent, f"/plugins update w4-demo {path}")["ok"]
    text = _tui(agent, "/plugins#w4-demo 查看")["message"]
    expected = (f"w4-demo 0.9.0（停用）：分镜方法\n适用：分镜\n关键词：分镜\n带检查程序：否；指纹 {plain.sha256[:12]}\n"
                "管理：/plugins#w4-demo 启用｜/plugins#w4-demo 停用｜/plugins#w4-demo 删除｜/plugins#w4-demo 退回")
    assert text == expected
    assert _tui(agent, "/plugins#w4-demo 启用")["ok"]


@pytest.mark.parametrize(("current_runtime", "target_runtime"), [(False, True), (True, True), (True, False)])
def test_real_revert_service_walks_history_and_mcp_waits_for_user(tmp_path, monkeypatch, current_runtime, target_runtime):
    """真实安装表、构建历史与退回命令；仅用冻结 DTO 代替本分支缺少的 W2 启用预览。"""
    from agent_py_agent.agent.capability.external_plugin_import import (
        ExternalImportRequest,
        import_claude_plugin,
    )
    from agent_py_agent.tests.test_cpack_w4_import_synthesis import (
        bundled,
        fixture_tree,
        requires_fd,
    )

    if requires_fd.args[0]:
        pytest.skip("fd walker is deliberately unavailable here; its refusal has a separate non-skipped test")
    agent = _agent(tmp_path, monkeypatch)
    manager = _manager(agent)
    store = LearnpackStore(manager.context.owner.home_dir)
    previous, _, _ = make_pack(version="1.0.0", runtime=target_runtime)
    previous_record = store.save_build(previous, BuildProvenance("自己写的", "MIT", "fixture"))
    assert _tui(agent, f"/plugins install {store.build_path(previous.sha256)}")["ok"]
    store.remember_owned_id("w4-demo")
    store.record_install({"package_id": "w4-demo", "sha256": previous_record.sha256, "state": installer.STATE_NEEDS_USER})
    source = fixture_tree(tmp_path / "source", manifest={"name": "w4-demo", "version": "2.0.0"}, servers=bundled())
    docs = (make_pack(version="2.0.0")[0] if current_runtime
            else import_claude_plugin(ExternalImportRequest(source, docs_only=True)).built)
    current_record = store.save_build(docs, BuildProvenance("自己写的", "MIT", "fixture"))
    assert _tui(agent, f"/plugins update w4-demo {store.build_path(docs.sha256)}")["ok"]
    store.record_install({"package_id": "w4-demo", "sha256": current_record.sha256, "state": installer.STATE_NEEDS_USER})
    if not current_runtime:
        assert _tui(agent, "/plugins#w4-demo 启用")["ok"]
    preview = _tui(agent, "/plugins#w4-demo 退回")
    line = preview["message"].splitlines()[-1]
    assert preview["ok"] and line == f"/plugins#w4-demo 退回 {previous.sha256[:12]}"
    assert manager.installations.snapshot()[0].package_sha256 == docs.sha256
    sent = []
    delegate = manager.command

    def command(text, **kwargs):
        sent.append(text)
        if target_runtime and text == "/plugins enable w4-demo":
            return {"ok": False, "details": {"reason": "confirmation_required", "confirmation": confirmation()}}
        return delegate(text, **kwargs)

    monkeypatch.setattr(manager, "command", command)
    result = pack_commands.run_pack_command(manager, plugin_namespace(line), "revert-fixture", manager.catalog().revision)
    expected = [] if current_runtime else ["/plugins disable w4-demo"]
    assert sent == [*expected, f"/plugins update w4-demo {store.build_path(previous.sha256)}", "/plugins enable w4-demo"]
    installed = manager.installations.snapshot()[0]
    assert installed.package_sha256 == previous.sha256
    if target_runtime:
        assert result["state"] == installer.STATE_NEEDS_USER
        assert result["message"].splitlines()[-1] == "/plugins#w4-demo 启用 012345abcdef"
        assert not installed.enabled
        assert not any("--confirm" in text for text in sent)
    else:
        assert result["ok"] and result["state"] == installer.STATE_ENABLED and installed.enabled
    assert store.installs()[-1]["via"] == "revert"
    assert _tui(agent, "/plugins#w4-demo 停用")["ok"]
    assert _tui(agent, "/plugins#w4-demo 删除")["ok"]
    assert manager.installations.snapshot() == ()
