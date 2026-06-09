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


def test_tool_gateway_reports_raw_block_append_recovery_for_unclosed_marker(tmp_path: Path):
    registry = _registry(tmp_path)

    calls = registry.parse_tool_calls(
        '[WRITE_FILE_RAW path="out/report.md"]\n'
        "# Report\n"
    )

    assert len(calls) == 1
    assert calls[0]["tool"] == "__parse_error__"
    assert calls[0]["error_code"] == "WRITE_FILE_RAW_MALFORMED"
    assert calls[0]["path"] == "out/report.md"
    assert calls[0]["previous_write_committed"] is False
    assert calls[0]["write_recovery"]["first_tool_call"]["mode"] == "overwrite"
    assert calls[0]["write_recovery"]["next_tool_call"]["mode"] == "append"
    result = registry.execute_call(calls[0])
    assert result.ok is False
    assert result.error_code == "WRITE_FILE_RAW_MALFORMED"


def test_tool_gateway_reports_unclosed_write_file_json_as_recoverable_parse_error(tmp_path: Path):
    registry = _registry(tmp_path)

    calls = registry.parse_tool_calls(
        '[TOOL_CALL]\n'
        '{"tool":"write_file","path":"out/report.md","content":"# Report\\n'
    )

    assert len(calls) == 1
    assert calls[0]["tool"] == "__parse_error__"
    assert calls[0]["error_code"] == "TOOL_CALL_UNCLOSED"
    assert calls[0]["path"] == "out/report.md"
    assert calls[0]["previous_write_committed"] is False
    assert calls[0]["write_recovery"]["strategy"] == "restart_same_file_with_append_chunks"
    result = registry.execute_call(calls[0])
    assert result.ok is False
    assert not (tmp_path / "out/report.md").exists()
    assert "mode=\"append\"" in result.output


def test_tool_gateway_does_not_commit_unclosed_write_file_json_inside_task_output(tmp_path: Path):
    workspace = tmp_path / "workspace"
    task_output = tmp_path / "home" / "tasks" / "today" / "task" / "output"
    workspace.mkdir()
    registry = _registry(workspace)
    boundary = {"task_output_dir": str(task_output), "task_work_dir": str(task_output.parent / "work")}

    calls = registry.parse_tool_calls(
        '[TOOL_CALL]\n'
        '{"tool":"write_file","path":"output/report.md","content":"# Report\\n'
    )
    result = registry.execute_call(calls[0], write_boundary=boundary)

    assert calls[0]["tool"] == "__parse_error__"
    assert calls[0]["previous_write_committed"] is False
    assert result.ok is False
    assert not (task_output / "report.md").exists()
    assert "mode=\"append\"" in result.output


def test_tool_gateway_reports_unclosed_write_file_json_append_mode_as_parse_error(tmp_path: Path):
    workspace = tmp_path / "workspace"
    task_output = tmp_path / "home" / "tasks" / "today" / "task" / "output"
    workspace.mkdir()
    task_output.mkdir(parents=True)
    (task_output / "report.md").write_text("# Report\n", encoding="utf-8")
    registry = _registry(workspace)
    boundary = {"task_output_dir": str(task_output), "task_work_dir": str(task_output.parent / "work")}

    calls = registry.parse_tool_calls(
        '[TOOL_CALL]\n'
        '{"tool":"write_file","path":"output/report.md","mode":"append","content":"## Next\\n'
    )

    assert len(calls) == 1
    assert calls[0]["tool"] == "__parse_error__"
    assert calls[0]["error_code"] == "TOOL_CALL_UNCLOSED"
    assert calls[0]["previous_write_committed"] is False
    result = registry.execute_call(calls[0], write_boundary=boundary)
    assert result.ok is False
    assert (task_output / "report.md").read_text(encoding="utf-8") == "# Report\n"
    assert "mode=\"append\"" in result.output


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


def test_tool_gateway_parses_write_file_raw_append_mode(tmp_path: Path):
    registry = _registry(tmp_path)

    calls = registry.parse_tool_calls(
        '[WRITE_FILE_RAW path="out/report.md" mode="append"]\n'
        "## Section\n"
        "[/WRITE_FILE_RAW]"
    )

    assert calls == [
        {
            "tool": "write_file",
            "path": "out/report.md",
            "mode": "append",
            "content": "## Section",
        }
    ]


def test_tool_gateway_executes_write_file_overwrite_and_append_modes(tmp_path: Path):
    registry = _registry(tmp_path)
    output_root = tmp_path / "task" / "output"
    boundary = {
        "allowed_write_roots": [str(output_root)],
        "task_output_dir": str(output_root),
        "forbidden_write_roots": [],
        "locked_files": [],
    }
    target = output_root / "report.md"

    first = registry.execute_call(
        {
            "tool": "write_file",
            "path": str(target),
            "mode": "overwrite",
            "content": "# Report\n",
        },
        write_boundary=boundary,
    )
    second = registry.execute_call(
        {
            "tool": "write_file",
            "path": str(target),
            "mode": "append",
            "content": "\nbody\n",
        },
        write_boundary=boundary,
    )

    assert first.ok is True
    assert second.ok is True
    assert target.read_text(encoding="utf-8") == "# Report\n\nbody\n"


def test_tool_gateway_continues_existing_task_output_when_mode_is_omitted(tmp_path: Path):
    workspace = tmp_path / "workspace"
    task_output = tmp_path / "home" / "tasks" / "today" / "task" / "output"
    workspace.mkdir()
    registry = _registry(workspace)
    boundary = {"task_output_dir": str(task_output), "task_work_dir": str(task_output.parent / "work")}

    first = registry.execute_call(
        {"tool": "write_file", "path": "output/report.md", "content": "# Report\n"},
        write_boundary=boundary,
    )
    second = registry.execute_call(
        {"tool": "write_file", "path": "output/report.md", "content": "## Section\n"},
        write_boundary=boundary,
    )

    assert first.ok is True
    assert second.ok is True
    assert "续写保护" in second.output
    assert 'mode="overwrite"' in second.output
    assert (task_output / "report.md").read_text(encoding="utf-8") == "# Report\n## Section\n"


def test_tool_gateway_continues_existing_task_work_when_mode_is_omitted(tmp_path: Path):
    workspace = tmp_path / "workspace"
    task_output = tmp_path / "home" / "tasks" / "today" / "task" / "output"
    task_work = task_output.parent / "work"
    workspace.mkdir()
    registry = _registry(workspace)
    boundary = {"task_output_dir": str(task_output), "task_work_dir": str(task_work)}

    first = registry.execute_call(
        {"tool": "write_file", "path": "work/facts.md", "content": "# Facts\n"},
        write_boundary=boundary,
    )
    second = registry.execute_call(
        {"tool": "write_file", "path": "work/facts.md", "content": "- detail\n"},
        write_boundary=boundary,
    )

    assert first.ok is True
    assert second.ok is True
    assert "续写保护" in second.output
    assert (task_work / "facts.md").read_text(encoding="utf-8") == "# Facts\n- detail\n"


def test_tool_gateway_overwrites_execution_output_json_when_mode_is_omitted(tmp_path: Path):
    workspace = tmp_path / "workspace"
    task_output = tmp_path / "home" / "tasks" / "today" / "task" / "output"
    task_work = task_output.parent / "work"
    output_json = task_work / "agents" / "run-1" / "output.json"
    output_json.parent.mkdir(parents=True)
    output_json.write_text('{"status":"PLANNING"}', encoding="utf-8")
    workspace.mkdir()
    registry = _registry(workspace)
    boundary = {
        "task_output_dir": str(task_output),
        "task_work_dir": str(task_work),
        "output_json": str(output_json),
    }

    result = registry.execute_call(
        {"tool": "write_file", "path": str(output_json), "content": '{"status":"DONE"}'},
        write_boundary=boundary,
    )

    assert result.ok is True
    assert "续写保护" not in result.output
    assert output_json.read_text(encoding="utf-8") == '{"status":"DONE"}'


def test_tool_gateway_explicit_overwrite_still_replaces_task_output(tmp_path: Path):
    workspace = tmp_path / "workspace"
    task_output = tmp_path / "home" / "tasks" / "today" / "task" / "output"
    workspace.mkdir()
    registry = _registry(workspace)
    boundary = {"task_output_dir": str(task_output), "task_work_dir": str(task_output.parent / "work")}

    registry.execute_call(
        {"tool": "write_file", "path": "output/report.md", "content": "# Report\n"},
        write_boundary=boundary,
    )
    result = registry.execute_call(
        {"tool": "write_file", "path": "output/report.md", "mode": "overwrite", "content": "# Replacement\n"},
        write_boundary=boundary,
    )

    assert result.ok is True
    assert (task_output / "report.md").read_text(encoding="utf-8") == "# Replacement\n"


def test_tool_gateway_omitted_mode_still_overwrites_non_task_output_file(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = _registry(workspace)
    target = workspace / "notes.md"
    target.write_text("old\n", encoding="utf-8")

    result = registry.execute_call({"tool": "write_file", "path": "notes.md", "content": "new\n"})

    assert result.ok is True
    assert target.read_text(encoding="utf-8") == "new\n"


def test_tool_gateway_maps_task_output_alias_from_write_boundary(tmp_path: Path):
    workspace = tmp_path / "workspace"
    task_output = tmp_path / "home" / "tasks" / "today" / "task" / "output"
    task_work = task_output.parent / "work"
    workspace.mkdir()
    registry = _registry(workspace)
    boundary = {"task_output_dir": str(task_output), "task_work_dir": str(task_work)}

    result = registry.execute_call(
        {"tool": "write_file", "path": "output/report.md", "content": "hello"},
        write_boundary=boundary,
    )

    assert result.ok is True
    assert (task_output / "report.md").read_text(encoding="utf-8") == "hello"
    assert not (workspace / "output" / "report.md").exists()
    readback = registry.execute_call(
        {"tool": "read_file", "path": "output/report.md"},
        write_boundary=boundary,
    )
    assert readback.ok is True
    assert "hello" in readback.output


def test_tool_gateway_parses_raw_block_with_literal_tool_markers(tmp_path: Path):
    registry = _registry(tmp_path)
    content = (
        "# 报告\n\n"
        "正文会直接提到 `[TOOL_CALL]` 和 `[/TOOL_CALL]`，这些只是文档内容。\n"
        "[TOOL_CALL]\n"
        '{"tool":"read_file","path":"README.md"}\n'
        "[/TOOL_CALL]\n"
    )

    calls = registry.parse_tool_calls(
        '[WRITE_FILE_RAW path="out/report.md"]\n'
        f"{content}"
        "[/WRITE_FILE_RAW]"
    )

    assert calls == [
        {
            "tool": "write_file",
            "path": "out/report.md",
            "content": content.rstrip("\n"),
        }
    ]


def test_tool_gateway_ignores_inline_raw_marker_example(tmp_path: Path):
    registry = _registry(tmp_path)

    calls = registry.parse_tool_calls(
        '普通说明：`[WRITE_FILE_RAW path="..."]...[/WRITE_FILE_RAW]` 只是文档示例。'
    )

    assert calls == []
