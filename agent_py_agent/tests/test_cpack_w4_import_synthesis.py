"""W4 合成：真实字节构建、保留来源路径、MCP 不支持项不静默丢失。"""
from __future__ import annotations

import io
import json
import os
import zipfile

import pytest

from agent_py_agent.agent.capability.external_plugin_import import (
    ExternalImportError,
    ExternalImportRequest,
    import_claude_plugin,
)
from agent_py_agent.agent.common.log_redaction import redact_sensitive_text
from agent_py_agent.agent.plugin_package import inspect_plugin_package

requires_fd = pytest.mark.skipif(os.listdir not in os.supports_fd, reason="目录导入明确只支持 FD 平台")


def put(root, path, content):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content.encode() if isinstance(content, str) else content)
    return target


def fixture_tree(root, *, servers=None, manifest=None, reverse=False):
    files = {
        ".claude-plugin/plugin.json": json.dumps(manifest if manifest is not None else {
            "name": "Example-Pack", "version": "1.2.0", "description": "分镜方法\n第二行",
            "keywords": ["分镜", "分镜"], "hooks": {}, "author": {"name": "Example"},
            "defaultEnabled": True, "allowedTools": ["all"], "futureField": "kept-open",
        }),
        "skills/story/SKILL.md": "---\nname: story\ndescription: 拆解故事\n---\n# 故事\n",
        "skills/story/data.bin": b"\xff\x00\x01", "commands/draft.md": "# 起草\n",
        "agents/reviewer.md": "# 审阅\n", "hooks/hooks.json": "{}",
        "notes.data": "扩展名不决定导入方式", "server/main.py": "print('fixture only')\n",
        "server/config.txt": "ordinary data\n", ".git/secret": "不应读取",
    }
    if servers is not None:
        files[".mcp.json"] = json.dumps({"mcpServers": servers})
    for path in sorted(files, reverse=reverse):
        put(root, path, files[path])
    return root


def members(result):
    with zipfile.ZipFile(io.BytesIO(result.built.payload)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def bundled():
    return {"a-server": {"command": "python3", "args": ["${CLAUDE_PLUGIN_ROOT}/server/main.py", "--fixture"]},
            "z-download": {"command": "npx", "args": ["remote-package"]}}


@requires_fd
def test_synthesis_preserves_documents_and_builds_one_pinned_server(tmp_path):
    root = fixture_tree(tmp_path / "source", servers=bundled())
    result = import_claude_plugin(ExternalImportRequest(root))
    manifest = inspect_plugin_package(result.built.payload).manifest
    assert manifest.is_capability_pack and manifest.has_mcp_runtime
    assert result.built.package_id == "example-pack" and manifest.version == "1.2.0"
    server = manifest.mcp_servers[0]
    assert server.root == "server" and server.entry.command == "main.py"
    assert server.entry.kind == "interpreter" and server.entry.interpreter == "python3"
    assert server.entry.args == ("--fixture",) and not server.network
    assert server.tool_catalog == "pinned_on_enable" and server.tools == ()
    payload = members(result)
    assert payload["skills/story/data.bin"] == b"\xff\x00\x01"
    assert "hooks/hooks.json" not in payload and ".git/secret" not in payload
    assert all(not item.executable for item in manifest.files)
    codes = {row["code"] for row in result.receipt["components"]}
    assert {"skill_imported", "command_as_document", "agent_as_document", "hooks_unsupported",
            "hidden_entry_ignored", "mcp_stdio_bundled", "mcp_command_not_bundled",
            "unknown_field_ignored", "untrusted_field_ignored"} <= codes
    entry = payload["CAPABILITY.md"].decode()
    assert "适用：" in entry and "拆解故事" in entry and "只作参考文档" in entry
    assert "启用时需要你确认" in entry and "未导入的部分" in entry
    assert "defaultEnabled" not in json.loads(payload["plugin.json"])


@requires_fd
@pytest.mark.parametrize("server,code", [
    ({"command": "npx", "args": ["x"]}, "mcp_command_not_bundled"),
    ({"command": "uvx", "args": ["x"]}, "mcp_command_not_bundled"),
    ({"command": "docker", "args": ["x"]}, "mcp_command_not_bundled"),
    ({"command": "/usr/bin/python3", "args": []}, "mcp_command_not_bundled"),
    ({"url": "https://example.invalid/service"}, "mcp_remote_transport_unsupported"),
    ({"type": "sse", "url": "https://example.invalid"}, "mcp_remote_transport_unsupported"),
    ({"command": "python3", "args": ["${CLAUDE_PLUGIN_ROOT}/server/main.py"], "env": {"CUSTOM": "private-value"}}, "mcp_env_unsupported"),
    ({"command": "python3", "args": ["${CLAUDE_PROJECT_DIR}/main.py"]}, "mcp_variables_unsupported"),
    ({"command": "python3", "args": ["${CLAUDE_PLUGIN_ROOT}/server/main.py"], "cwd": "."}, "mcp_variables_unsupported"),
    ("./server/example.mcpb", "mcp_bundle_unsupported"),
])
def test_unsupported_server_does_not_block_documents(tmp_path, server, code):
    root = fixture_tree(tmp_path / "source", servers={"server": server})
    result = import_claude_plugin(ExternalImportRequest(root))
    assert inspect_plugin_package(result.built.payload).manifest.is_content_only
    assert any(row["code"] == code for row in result.receipt["components"])
    assert "private-value" not in json.dumps(result.receipt)


@requires_fd
def test_docs_only_and_network_are_explicit_flags(tmp_path):
    root = fixture_tree(tmp_path / "source", servers=bundled())
    docs = import_claude_plugin(ExternalImportRequest(root, docs_only=True))
    assert inspect_plugin_package(docs.built.payload).manifest.is_content_only
    rows = [row for row in docs.receipt["components"] if row["kind"] == "mcp"]
    assert len(rows) == 2 and {r["code"] for r in rows} == {"mcp_skipped_docs_only"}
    online = import_claude_plugin(ExternalImportRequest(root, network=True))
    assert inspect_plugin_package(online.built.payload).manifest.mcp_servers[0].network is True


@requires_fd
def test_source_entry_and_optional_manifest(tmp_path):
    root = tmp_path / "simple-pack"
    put(root, "CAPABILITY.md", "# 原入口\n不改原文\n")
    result = import_claude_plugin(ExternalImportRequest(root))
    assert result.built.package_id == "simple-pack"
    assert members(result)["CAPABILITY.md"] == b"# \xe5\x8e\x9f\xe5\x85\xa5\xe5\x8f\xa3\n\xe4\xb8\x8d\xe6\x94\xb9\xe5\x8e\x9f\xe6\x96\x87\n"
    assert result.built.version == "0.1.0"


@requires_fd
@pytest.mark.parametrize("raw", ['[]', '{}', '{"name":"one","name":"two"}', '{"name":"ok","version":"a b"}', '{"name":"ok","skills":"../outside"}'])
def test_manifest_invalid_is_not_a_package_error(tmp_path, raw):
    root = tmp_path / "source"
    put(root, ".claude-plugin/plugin.json", raw)
    put(root, "CAPABILITY.md", "# 方法\n")
    with pytest.raises(ExternalImportError) as error:
        import_claude_plugin(ExternalImportRequest(root))
    assert error.value.code == "PLUGIN_IMPORT_MANIFEST_INVALID"


@requires_fd
def test_empty_tree_has_no_content_and_invalid_id_uses_builder(tmp_path):
    root = tmp_path / "empty"
    root.mkdir()
    with pytest.raises(ExternalImportError) as error:
        import_claude_plugin(ExternalImportRequest(root))
    assert error.value.code == "PLUGIN_IMPORT_NO_CONTENT"
    put(root, ".claude-plugin/plugin.json", '{"name":"中文"}')
    put(root, "CAPABILITY.md", "# 方法\n")
    with pytest.raises(ExternalImportError) as error:
        import_claude_plugin(ExternalImportRequest(root))
    assert error.value.code == "PLUGIN_IMPORT_ID_INVALID"


@requires_fd
def test_secret_refusal_does_not_echo_source_text(tmp_path):
    root = tmp_path / "source"
    value = "sk-" + "live-" + "a" * 32
    text = f'api_key = "{value}"\n'
    assert redact_sensitive_text(text, code_file=True) != text
    put(root, "notes.data", text)
    with pytest.raises(ExternalImportError) as error:
        import_claude_plugin(ExternalImportRequest(root))
    assert error.value.code == "PACKAGE_BUILD_SECRET_FOUND"
    assert value not in error.value.detail and "1" in error.value.detail


@requires_fd
@pytest.mark.parametrize("transport", [[], {}, 123, None, "future-transport"])
def test_malformed_transport_never_escapes_or_silently_becomes_stdio(tmp_path, transport):
    root = fixture_tree(tmp_path / "source", servers={"server": {
        "type": transport, "command": "python3", "args": ["${CLAUDE_PLUGIN_ROOT}/server/main.py"]}})
    result = import_claude_plugin(ExternalImportRequest(root))
    assert inspect_plugin_package(result.built.payload).manifest.is_content_only
    code = "mcp_remote_transport_unsupported" if isinstance(transport, str) else "mcp_command_not_bundled"
    assert any(row["code"] == code for row in result.receipt["components"])


@requires_fd
def test_leading_variable_contract_does_not_accept_embedded_expansions(tmp_path):
    root = fixture_tree(tmp_path / "source", servers={"server": {"command": "python3", "args": [
        "${CLAUDE_PLUGIN_ROOT}/server/main.py", "--config=${CLAUDE_PLUGIN_ROOT}/server/config.txt"]}})
    result = import_claude_plugin(ExternalImportRequest(root))
    assert any(row["code"] == "mcp_variables_unsupported" for row in result.receipt["components"])


@requires_fd
def test_executable_is_declared_not_inherited_and_second_server_is_limited(tmp_path):
    root = fixture_tree(tmp_path / "source", servers={
        "a-main": {"command": "${CLAUDE_PLUGIN_ROOT}/server/main.py"},
        "b-second": {"command": "python3", "args": ["${CLAUDE_PLUGIN_ROOT}/second/main.py"]},
    })
    (root / "server/main.py").chmod(0o600)
    (root / "server/config.txt").chmod(0o755)
    put(root, "second/main.py", "print('not executed')\n")
    result = import_claude_plugin(ExternalImportRequest(root))
    manifest = inspect_plugin_package(result.built.payload).manifest
    assert manifest.mcp_servers[0].entry.kind == "executable"
    assert {item.path for item in manifest.files if item.executable} == {"server/main.py"}
    assert any(row["code"] == "mcp_server_limit" for row in result.receipt["components"])
    assert "second/main.py" in members(result)


@requires_fd
def test_extra_component_roots_and_path_array_mcp_sources(tmp_path):
    root = fixture_tree(tmp_path / "source", manifest={"name": "extra-roots", "skills": "./custom-skills",
        "commands": ["./custom-commands/one.md"], "agents": "./custom-agents", "mcpServers": ["./config/mcp.json"]})
    put(root, "custom-skills/plan/SKILL.md", "---\nname: plan\ndescription: 规划\n---\n# 规划\n")
    put(root, "custom-skills/plan/data", "data")
    put(root, "custom-commands/one.md", "# 仅参考\n")
    put(root, "custom-agents/reviewer.md", "# 仅参考\n")
    put(root, "config/mcp.json", json.dumps({"server": {"command": "python3", "args": ["${CLAUDE_PLUGIN_ROOT}/server/main.py"]}}))
    result = import_claude_plugin(ExternalImportRequest(root))
    assert inspect_plugin_package(result.built.payload).manifest.has_mcp_runtime
    payload = members(result)
    assert "config/mcp.json" not in payload
    assert {"custom-skills/plan/SKILL.md", "custom-skills/plan/data", "custom-commands/one.md", "custom-agents/reviewer.md"} <= payload.keys()
    assert any(row["source_path"] == "custom-skills/plan" and row["code"] == "skill_imported" for row in result.receipt["components"])


@requires_fd
def test_runtime_tree_invalid_falls_back_to_document_import(tmp_path):
    root = fixture_tree(tmp_path / "source", servers=bundled())
    put(root, "server/中文.txt", "运行树不接中文路径，但它可作为文档数据")
    result = import_claude_plugin(ExternalImportRequest(root))
    assert inspect_plugin_package(result.built.payload).manifest.is_content_only
    assert "server/中文.txt" in members(result)
    assert any(row["code"] == "mcp_runtime_tree_unsupported" for row in result.receipt["components"])
