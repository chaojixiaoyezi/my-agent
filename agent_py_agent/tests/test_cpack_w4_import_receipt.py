"""W4 回执：顺序、路径、摘要及组件覆盖不变量。"""
from __future__ import annotations

import hashlib
import json

from agent_py_agent.agent.capability.external_plugin_import import (
    ExternalImportRequest,
    import_claude_plugin,
)
from agent_py_agent.tests.test_cpack_w4_import_synthesis import (
    bundled,
    fixture_tree,
    members,
    put,
    requires_fd,
)


@requires_fd
def test_parent_path_and_creation_order_do_not_change_package_or_receipt(tmp_path):
    one = fixture_tree(tmp_path / "one/source", servers=bundled())
    two = fixture_tree(tmp_path / "other/source", servers=bundled(), reverse=True)
    a, b = [import_claude_plugin(ExternalImportRequest(path)) for path in (one, two)]
    assert a.built.sha256 == b.built.sha256
    assert members(a)["import/RECEIPT.json"] == members(b)["import/RECEIPT.json"]
    assert a.receipt == b.receipt
    assert set(a.receipt) == {"schema", "source_kind", "source_tree_sha256", "plugin_id", "version",
                              "package_schema", "docs_only", "network", "components", "ignored_fields"}
    assert a.receipt["schema"] == "external_plugin_import_receipt.v1"
    assert a.receipt["package_schema"] == "plugin_package.v10"
    canonical = (json.dumps(a.receipt, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    assert members(a)["import/RECEIPT.json"] == canonical
    assert str(tmp_path) not in canonical.decode()
    rows = a.receipt["components"]
    assert rows == sorted(rows, key=lambda row: (row["kind"], row["source_path"], row["name"]))
    assert len(rows) == len({(r["kind"], r["source_path"], r["name"]) for r in rows})
    assert all(set(r) == {"kind", "name", "source_path", "status", "code", "target_path"} for r in rows)
    assert all(r["status"] in {"imported", "mapped", "unsupported", "ignored"} for r in rows)
    assert sum(r["code"] == "skill_imported" for r in rows) == 1
    paths = {r["source_path"] for r in rows}
    assert {"skills/story", "commands/draft.md", "agents/reviewer.md", "hooks", ".git"} <= paths
    lines = []
    # 输入路径只由夹具明确列出，不以 importer 输出构造期望摘要。
    for path in sorted([".claude-plugin/plugin.json", ".mcp.json", "agents/reviewer.md", "commands/draft.md",
                        "hooks/hooks.json", "notes.data", "server/config.txt", "server/main.py",
                        "skills/story/SKILL.md", "skills/story/data.bin"]):
        data = (one / path).read_bytes()
        lines.append(f"{path}\0{hashlib.sha256(data).hexdigest()}\0{len(data)}\n")
    assert a.receipt["source_tree_sha256"] == hashlib.sha256("".join(lines).encode()).hexdigest()


@requires_fd
def test_real_directory_install_and_update_keep_receipt_inside_original_envelope(tmp_path, monkeypatch):
    from agent_py_agent.tests.test_cpack_w4_import_synthesis import put
    from agent_py_agent.tests.test_pack_commands import _agent, _manager, _tui

    agent = _agent(tmp_path, monkeypatch)
    root = fixture_tree(agent.home_paths.owner_home_dir / "fixture-source", servers=bundled())
    result = _tui(agent, f"/plugins install {root}")
    assert result["ok"], result
    receipt = result["details"]["import_receipt"]
    assert receipt["plugin_id"] == "example-pack" and result["details"]["enabled"] is False
    row = _manager(agent).installations.snapshot()[0]
    assert row.activation is None and row.manifest.has_mcp_runtime
    assert "example-pack" in _tui(agent, "/plugins#")["message"]
    view = _tui(agent, "/plugins#example-pack 查看")
    assert view["ok"] and "带 MCP 工具：是" in view["message"]
    assert "外部导入：" in view["message"]
    # 当前 W1 冻结读口拒绝停用包；不能把读取失败写成 0 项，更不能为了详情伪造 active。
    assert "统计未验证" in view["message"]
    again = _tui(agent, f"/plugins install {root}")
    assert again["ok"] and _manager(agent).installations.snapshot()[0].package_sha256 == row.package_sha256
    put(root, ".claude-plugin/plugin.json", json.dumps({"name": "Example-Pack", "version": "1.3.0", "description": "新版分镜"}))
    changed = _tui(agent, f"/plugins update example-pack {root}")
    assert changed["ok"] and changed["details"]["import_receipt"]["version"] == "1.3.0"
    assert changed["details"]["package_version"] == "1.3.0" and "receipt" in changed["details"]
    before = _manager(agent).installations.snapshot()[0]
    put(root, ".claude-plugin/plugin.json", '{"name":"another-id"}')
    refused = _tui(agent, f"/plugins update example-pack {root}")
    assert refused["error_code"] == "PLUGIN_IMPORT_ID_CONFLICT"
    assert _manager(agent).installations.snapshot()[0] == before


@requires_fd
def test_docs_only_real_sequence_enables_reads_receipt_disables_and_removes(tmp_path, monkeypatch):
    from agent_py_agent.tests.test_pack_commands import _agent, _tui

    agent = _agent(tmp_path, monkeypatch)
    root = fixture_tree(agent.home_paths.owner_home_dir / "fixture-source")
    installed = _tui(agent, f"/plugins install {root}")
    assert installed["ok"], installed
    receipt = installed["details"]["import_receipt"]
    assert _tui(agent, "/plugins#example-pack 启用")["ok"]
    view = _tui(agent, "/plugins#example-pack 查看")
    counts = {status: sum(row["status"] == status for row in receipt["components"]) for status in ("imported", "mapped", "unsupported")}
    expected = f"外部导入：导入 {counts['imported']} 项、映射 {counts['mapped']} 项、未支持 {counts['unsupported']} 项"
    assert expected in view["message"] and "hooks_unsupported" in view["message"]
    assert _tui(agent, "/plugins#example-pack 停用")["ok"]
    assert _tui(agent, "/plugins#example-pack 删除")["ok"]
    assert "example-pack" not in _tui(agent, "/plugins#")["message"]


@requires_fd
def test_directory_pack_never_overwrites_legacy_plugin(tmp_path, monkeypatch):
    from agent_py_agent.tests.test_cpack_w4_import_synthesis import put
    from agent_py_agent.tests.test_pack_commands import _agent, _manager, _tui
    from agent_py_agent.tests.test_plugin_update import _v2_bundle

    agent = _agent(tmp_path, monkeypatch)
    home = agent.home_paths.owner_home_dir
    legacy = _v2_bundle(home)
    assert _tui(agent, f"/plugins install {legacy}")["ok"]
    before = _manager(agent).installations.snapshot()[0]
    assert not before.manifest.is_capability_pack
    root = fixture_tree(home / "fixture-source", manifest={"name": "sample-peek"})
    put(root, "CAPABILITY.md", "# 不能覆盖插件\n")
    for text in (f"/plugins install {root}", f"/plugins update sample-peek {root}"):
        refused = _tui(agent, text)
        assert refused["error_code"] == "PLUGIN_IMPORT_ID_CONFLICT", refused
        assert _manager(agent).installations.snapshot()[0] == before


@requires_fd
def test_directory_install_and_update_refuse_policy_denied_descendant(tmp_path, monkeypatch):
    from agent_py_agent.agent.plugin_install_tool import PluginInstallTool
    from agent_py_agent.agent.plugin_update_tool import PluginUpdateTool
    from agent_py_agent.tests.test_pack_commands import _agent, _manager, _tui

    outcomes = []
    for tool_type in (PluginInstallTool, PluginUpdateTool):
        original_execute = tool_type.execute

        def record_outcome(self, params, execute=original_execute):
            outcome = execute(self, params)
            outcomes.append(outcome)
            return outcome

        monkeypatch.setattr(tool_type, "execute", record_outcome)

    agent = _agent(tmp_path, monkeypatch)
    manager = _manager(agent)
    policy = manager.context.path_policy
    store = manager.installations
    root = fixture_tree(agent.home_paths.owner_home_dir / "workspace" / "runtime")
    denied = put(root, "services/queue/item.md", "marker\n")
    assert policy.check(root).allowed
    assert not policy.check(denied).allowed

    def store_bytes():
        return {path.relative_to(store.root): path.read_bytes()
                for path in store.root.rglob("*") if path.is_file()}

    empty = store_bytes()
    refused = _tui(agent, f"/plugins install {root}")
    assert refused["error_code"] == "PLUGIN_IMPORT_PATH_UNSAFE", refused
    assert refused["ok"] is False
    assert len(outcomes) == 1
    assert outcomes[-1].effect_outcome == "not_started"
    assert _manager(agent).installations.snapshot() == ()
    assert store_bytes() == empty

    clean = fixture_tree(agent.home_paths.owner_home_dir / "fixture-source")
    installed = _tui(agent, f"/plugins install {clean}")
    assert installed["ok"], installed
    before = _manager(agent).installations.snapshot()[0]
    before_bytes = store_bytes()
    package_bytes = store.package_bytes(before)
    outcomes.clear()
    refused = _tui(agent, f"/plugins update example-pack {root}")
    assert refused["error_code"] == "PLUGIN_IMPORT_PATH_UNSAFE", refused
    assert refused["ok"] is False
    assert len(outcomes) == 1
    assert outcomes[-1].effect_outcome == "not_started"
    assert _manager(agent).installations.snapshot() == (before,)
    assert store.package_bytes(before) == package_bytes
    assert store_bytes() == before_bytes
