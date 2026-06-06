"""LLM: Tool gateway contract tests for exact tool protocol and bounded shell output.

函数/模块用途: 验证工具网关拒绝旧工具写法，并阻止大 shell 输出直接撑爆上下文。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from agent_py_agent.agent.tooling.registry import ToolRegistry, ToolRegistryParams


def _registry(root: Path, *, shell_output_max_chars: int = 80, access_mode: str = "workspace-write") -> ToolRegistry:
    return ToolRegistry(
        ToolRegistryParams(
            workspace_root=root,
            max_chars=6000,
            max_entries=200,
            max_matches=50,
            web_max_chars=12000,
            http_timeout=30,
            catalog_limit=20,
            retrieval_limit=3,
            vector_search_enabled=False,
            shell_tool_timeout=30,
            shell_tool_output_max_chars=shell_output_max_chars,
            access_mode=access_mode,
        )
    )


def test_tool_gateway_rejects_shell_alias(tmp_path: Path):
    result = _registry(tmp_path).execute_call({
        "tool": "shell",
        "cmd": f'{sys.executable} -c "print(123)"',
        "cwd": str(tmp_path),
    })

    assert result.ok is False
    assert result.tool == "shell"
    assert "TOOL_NOT_REGISTERED" in result.output


def test_tool_gateway_hides_controlled_exec_from_default_catalog(tmp_path: Path):
    registry = _registry(tmp_path)

    names = {spec.name for spec in registry.specs(include_orchestration=True)}
    manifest = json.loads(registry.execute_call({"tool": "list_tools"}).output)
    manifest_names = {item["name"] for item in manifest["tools"]}
    retired_names = {
        "append_file",
        "replace_in_file",
        "write_structured_json",
        "data_to_workbook",
        "markdown_to_pdf",
        "file_write_session",
    }

    assert "run_command" in names
    assert "controlled_exec" not in names
    assert retired_names.isdisjoint(names)
    assert "run_command" in manifest_names
    assert "controlled_exec" not in manifest_names
    assert retired_names.isdisjoint(manifest_names)


def test_tool_gateway_can_still_expose_controlled_exec_when_explicitly_allowed(tmp_path: Path):
    names = {spec.name for spec in _registry(tmp_path).specs(allowed_tools=["controlled_exec"])}

    assert names == {"controlled_exec"}


def _controlled_exec_boundary(root: Path) -> dict[str, object]:
    return {
        "controlled_exec_grants": [
            {
                "grant_id": "grant-shell-1",
                "request_id": "req-1",
                "run_id": "child-1",
                "command_allowlist": ["pwd"],
                "path_scope": [str(root)],
                "output_budget": {"stdout_bytes": 1000, "stderr_bytes": 1000},
            }
        ],
    }


def test_tool_gateway_rejects_hidden_controlled_exec_when_not_explicitly_allowed(tmp_path: Path):
    result = _registry(tmp_path).execute_call(
        {
            "tool": "controlled_exec",
            "command": "pwd",
            "cwd": str(tmp_path),
            "apply": False,
        },
        write_boundary=_controlled_exec_boundary(tmp_path),
    )

    assert result.ok is False
    assert result.tool == "controlled_exec"
    assert "内部显式授权" in result.output


def test_tool_gateway_executes_controlled_exec_only_when_explicitly_allowed(tmp_path: Path):
    result = _registry(tmp_path).execute_call(
        {
            "tool": "controlled_exec",
            "command": "pwd",
            "cwd": str(tmp_path),
            "apply": False,
        },
        allowed_tools=["controlled_exec"],
        write_boundary=_controlled_exec_boundary(tmp_path),
    )

    assert result.ok is True
    assert '"mode": "dry_run"' in result.output
    assert '"allowed": true' in result.output


def test_run_command_output_is_bounded_by_gateway_budget(tmp_path: Path):
    result = _registry(tmp_path, shell_output_max_chars=40).execute_call({
        "tool": "run_command",
        "command": f'{sys.executable} -c "print(\\"A\\" * 200)"',
    })

    assert result.ok
    assert "stdout_truncated=True" in result.output
    assert "stdout_chars=201" in result.output
    assert "A" * 80 not in result.output


def test_run_command_shell_access_override_uses_normal_path_policy(tmp_path: Path):
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    registry = _registry(workspace, access_mode="full-access")

    result = registry.execute_call(
        {
            "tool": "run_command",
            "command": "pwd",
            "working_dir": str(external),
        },
        write_boundary={"shell_access_mode": "workspace-write"},
    )

    assert result.ok is True
    assert "return_code=0" in result.output


def test_tool_gateway_preserves_unknown_read_artifact_parameters_unmodified(tmp_path: Path):
    registry = _registry(tmp_path)

    payload = registry.parse_tool_calls(
        '[TOOL_CALL]\n{"tool":"read_artifact","ref":"run-1:2-1","limit":123}\n[/TOOL_CALL]'
    )[0]

    assert payload == {"tool": "read_artifact", "ref": "run-1:2-1", "limit": 123}


def test_tool_gateway_reports_malformed_write_file_raw_marker(tmp_path: Path):
    registry = _registry(tmp_path)

    calls = registry.parse_tool_calls(
        "准备写文件\n"
        "[FILE_WRITE_SESSION_APPEND]\n"
        '{"tool":"write_file","action":"begin","target_path":"out/index.html"}\n'
        "[/TOOL_CALL]"
    )

    assert calls == []


def test_tool_gateway_reports_single_error_for_closed_raw_block_missing_attrs(tmp_path: Path):
    registry = _registry(tmp_path)

    calls = registry.parse_tool_calls(
        "[WRITE_FILE_RAW]\n"
        "hello\n"
        "[/WRITE_FILE_RAW]"
    )

    assert len(calls) == 1
    assert calls[0]["tool"] == "__parse_error__"
    assert calls[0]["error"] == "WRITE_FILE_RAW 缺少结构化属性: path"


def test_tool_gateway_parses_write_file_raw_content_block(tmp_path: Path):
    registry = _registry(tmp_path)
    html = (
        "<!doctype html>\n"
        "<html>\n"
        "<head><meta charset=\"utf-8\"><title>ARCA</title></head>\n"
        "<body><h1>ARCA</h1></body>\n"
        "</html>"
    )

    calls = registry.parse_tool_calls(
        '[WRITE_FILE_RAW path="out/index.html"]\n'
        f"{html}\n"
        "[/WRITE_FILE_RAW]"
    )

    assert calls == [
        {
            "tool": "write_file",
            "path": "out/index.html",
            "content": html,
        }
    ]
    result = registry.execute_call(calls[0])
    assert result.ok is True
    assert (tmp_path / "out" / "index.html").read_text(encoding="utf-8") == html
