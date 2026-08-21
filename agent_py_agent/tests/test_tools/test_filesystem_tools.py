"""LLM: tests for filesystem tools — write boundary, symlink, write/append, replace, search, params.

给人看的解释：
这个文件放所有和文件系统工具相关的测试：写入边界保护、符号链接逃逸拦截、
写文件/追加文件、替换内容、搜索文本、参数校验和绝对路径隐藏。
"""

import base64
import io
import json
import tempfile
import types
from pathlib import Path

from agent_py_agent.agent.tooling import _filesystem_search as search_mod
from agent_py_agent.agent.tooling._filesystem_find import FindFilesTool
from agent_py_agent.agent.tooling._filesystem_list import ListFilesTool
from agent_py_agent.agent.tooling._filesystem_patch import ApplyPatchTool
from agent_py_agent.agent.tooling._filesystem_read import ReadFileTool
from agent_py_agent.agent.tooling._filesystem_search import SearchTextTool
from agent_py_agent.agent.tooling._filesystem_write import WriteFileTool
from agent_py_agent.tests._tool_runtime_harness import execute_registry_test_call
from agent_py_agent.tests.support.xlsx_fixtures import write_xlsx_fixture

from .backends import make_tool_registry


def _execute(registry, tool_name, arguments, **kwargs):
    return execute_registry_test_call(registry, tool_name, arguments, **kwargs)


def _write_boundary_test_registry(workspace: Path) -> tuple:
    registry = make_tool_registry(workspace)
    task_dir = workspace / "subs" / "run-1"
    task_dir.mkdir(parents=True)
    (task_dir / "private").mkdir()
    boundary = {
        "allowed_write_roots": [str(task_dir)],
        "forbidden_write_roots": [str(task_dir / "private")],
        "locked_files": ["subs/run-1/LOCKED.md"],
    }
    return registry, task_dir, boundary


def _assert_allowed_write(registry, task_dir, boundary) -> None:
    result = _execute(
        registry,
        "write_file",
        {"path": "subs/run-1/output.md", "content": "ok"},
        allowed_tools=["write_file"],
        write_boundary=boundary,
    )
    assert result.ok
    assert (task_dir / "output.md").read_text(encoding="utf-8") == "ok"


def _assert_blocked_outside_allowed_roots(registry, workspace, boundary) -> None:
    result = _execute(
        registry,
        "write_file",
        {"path": "README.md", "content": "bad"},
        allowed_tools=["write_file"],
        write_boundary=boundary,
    )
    assert not result.ok
    assert "allowed_write_roots" in result.output
    assert not (workspace / "README.md").exists()


def _assert_blocked_forbidden_root(registry, task_dir, boundary) -> None:
    result = _execute(
        registry,
        "write_file",
        {"path": "subs/run-1/private/secret.md", "content": "bad"},
        allowed_tools=["write_file"],
        write_boundary=boundary,
    )
    assert not result.ok
    assert "forbidden_write_roots" in result.output


def _assert_blocked_locked_file(registry, task_dir, boundary) -> None:
    locked = _execute(
        registry,
        "write_file",
        {"path": "subs/run-1/LOCKED.md", "content": "bad"},
        allowed_tools=["write_file"],
        write_boundary=boundary,
    )
    assert not locked.ok
    assert locked.error_code == "WRITE_FORBIDDEN"
    assert "locked_files" in locked.output
    locked_child = _execute(
        registry,
        "write_file",
        {"path": "subs/run-1/LOCKED.md/child.txt", "content": "bad"},
        allowed_tools=["write_file"],
        write_boundary=boundary,
    )
    assert not locked_child.ok
    assert locked_child.error_code == "WRITE_FORBIDDEN"
    assert "locked_files" in locked_child.output
    assert not (task_dir / "LOCKED.md").exists()


def _assert_blocked_non_string_path(registry, boundary) -> None:
    result = _execute(
        registry,
        "write_file",
        {"path": {"unexpected": "object"}, "content": "bad"},
        allowed_tools=["write_file"],
        write_boundary=boundary,
    )
    assert not result.ok
    assert result.error_code == "TOOL_PARAMETER_TYPE_INVALID"
    assert result.metadata["action_decision"]["evidence"]["issues"][0]["path"] == "$.path"


def test_write_boundary_enforces_declared_allowed_roots_for_write_tools():
    """LLM: declared allowed_write_roots are the positive write boundary."""
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        registry, task_dir, boundary = _write_boundary_test_registry(workspace)
        _assert_allowed_write(registry, task_dir, boundary)
        _assert_blocked_outside_allowed_roots(registry, workspace, boundary)
        _assert_blocked_forbidden_root(registry, task_dir, boundary)
        _assert_blocked_locked_file(registry, task_dir, boundary)
        _assert_blocked_non_string_path(registry, boundary)


def test_write_boundary_allows_explicit_product_root_outside_primary_workspace():
    """LLM: product roots granted by the parent must be usable even outside the agent code workspace.

    新手说明:
    真实任务常把产物写到用户指定目录，而 my-agent 代码在另一个目录。
    只要父级 write_boundary 明确给了 allowed_write_roots，写工具就应该能写进去。
    """
    with tempfile.TemporaryDirectory() as workspace_td, tempfile.TemporaryDirectory() as product_td:
        workspace = Path(workspace_td)
        product_root = Path(product_td) / "deliverables"
        registry = make_tool_registry(workspace)

        result = _execute(
            registry,
            "write_file",
            {
                "path": str(product_root / "index.html"),
                "content": "<!doctype html><html><body>ok</body></html>",
            },
            allowed_tools=["write_file"],
            write_boundary={
                "allowed_write_roots": [str(product_root)],
                "product_write_roots": [str(product_root)],
                "product_write_policy": "direct",
            },
        )

        assert result.ok
        assert (product_root / "index.html").read_text(encoding="utf-8").startswith("<!doctype")


def test_write_boundary_extends_read_tools_to_explicit_product_root():
    """LLM: subagents should be able to read the product root that the parent allowed them to write."""
    with tempfile.TemporaryDirectory() as workspace_td, tempfile.TemporaryDirectory() as product_td:
        workspace = Path(workspace_td)
        product_root = Path(product_td) / "deliverables"
        product_root.mkdir()
        (product_root / "index.html").write_text("hello product", encoding="utf-8")
        registry = make_tool_registry(workspace)
        boundary = {
            "allowed_write_roots": [str(product_root)],
            "product_write_roots": [str(product_root)],
            "product_write_policy": "direct",
        }

        result = _execute(
            registry,
            "read_file",
            {"path": str(product_root / "index.html")},
            allowed_tools=["read_file"],
            write_boundary=boundary,
        )

        assert result.ok
        assert "hello product" in result.output


def test_write_boundary_blocks_symlink_escape_outside_declared_allowed_roots():
    """LLM: resolved symlink targets must still remain inside declared write roots.

    新手说明:
    如果父级已经给了 allowed_write_roots，符号链接解析后的真实路径也必须还在授权范围内。
    """
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as outside_td:
        workspace = Path(td)
        outside = Path(outside_td)
        registry = make_tool_registry(workspace)
        task_dir = workspace / "subs" / "run-1"
        task_dir.mkdir(parents=True)
        link = task_dir / "outside-link"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            return

        result = _execute(
            registry,
            "write_file",
            {"path": "subs/run-1/outside-link/escape.txt", "content": "bad"},
            allowed_tools=["write_file"],
            write_boundary={"allowed_write_roots": [str(task_dir)]},
        )

        assert not result.ok
        assert "allowed_write_roots" in result.output
        assert not (outside / "escape.txt").exists()


def test_write_and_apply_patch_tools():
    """LLM: verify that WriteFileTool creates files and ApplyPatchTool edits existing files.

    新手说明:
    先写完整文件，再用补丁做局部修改，确认内容正确。
    """
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        write_tool = WriteFileTool(workspace)
        patch_tool = ApplyPatchTool(workspace)

        write_result = write_tool.execute({"path": "src/demo.py", "content": "print('a')\n"})
        patch_result = patch_tool.execute(
            {
                "patch": (
                    "*** Begin Patch\n"
                    "*** Update File: src/demo.py\n"
                    " print('a')\n"
                    "+print('b')\n"
                    "*** End Patch\n"
                )
            }
        )

        assert write_result.ok
        assert patch_result.ok
        assert (workspace / "src" / "demo.py").read_text(encoding="utf-8") == "print('a')\nprint('b')\n"


def test_write_file_missing_path_is_actionable_tool_argument_error():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        write_tool = WriteFileTool(workspace)

        result = write_tool.execute({"content": "missing path"})

        assert result.ok is False
        assert result.error_code == "TOOL_INVALID_ARGUMENTS"
        assert result.recommended_action


def test_write_file_rejects_invalid_xlsx_without_overwriting_previous_good_file():
    """LLM: final binary writes should validate package integrity before replacing an existing artifact."""
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        target = write_xlsx_fixture(
            workspace,
            "outputs/report.xlsx",
            sheets=[{"name": "summary", "rows": [{"name": "ok"}]}],
        )
        before = target.read_bytes()
        write_tool = WriteFileTool(workspace)

        result = write_tool.execute(
            {
                "path": "outputs/report.xlsx",
                "data_base64": base64.b64encode(b"not a workbook").decode("ascii"),
            }
        )

        assert result.ok is False
        assert "XLSX_INVALID" in result.output or "XLSX_INVALID_PACKAGE" in result.output
        assert target.read_bytes() == before


def test_filesystem_tools_allow_configured_extra_workspace_root():
    with tempfile.TemporaryDirectory() as primary_td, tempfile.TemporaryDirectory() as extra_td:
        primary = Path(primary_td)
        extra = Path(extra_td)
        write_tool = WriteFileTool(primary, workspace_roots=[primary, extra])
        read_tool = ReadFileTool(primary, max_chars=2000, workspace_roots=[primary, extra])
        list_tool = ListFilesTool(primary, max_entries=20, workspace_roots=[primary, extra])

        target = extra / "report.txt"
        write_result = write_tool.execute({"path": str(target), "content": "ok"})
        read_result = read_tool.execute({"path": str(target)})
        list_result = list_tool.execute({"path": str(extra)})

        assert write_result.ok
        assert read_result.ok
        assert "ok" in read_result.output
        assert list_result.ok
        assert "report.txt" in list_result.output


def test_read_file_routes_internal_agent_status_refs_to_agent_tree(tmp_path: Path):
    workspace = tmp_path / "workspace"
    internal = workspace / "tasks" / "2026-06-06" / "demo" / "work" / "agents" / "subagent-123" / "final_report.md"
    child_output = workspace / "tasks" / "2026-06-06" / "demo" / "work" / "child_outputs" / "subagent-123.md"
    internal.parent.mkdir(parents=True)
    child_output.parent.mkdir(parents=True)
    internal.write_text("internal progress only", encoding="utf-8")
    child_output.write_text("declared child result", encoding="utf-8")
    (internal.parent / "canonical_state.json").write_text(
        json.dumps(
            {
                "status": "DONE",
                "artifact_refs": [str(child_output)],
                "attributes": {
                    "output_files": [str(child_output)],
                    "artifact_registry_refs": [
                        {
                            "artifact_id": "result-1",
                            "path": str(child_output),
                            "kind": "md",
                            "status": "ready",
                            "size_bytes": child_output.stat().st_size,
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    read_tool = ReadFileTool(workspace, max_chars=2000)

    internal_result = read_tool.execute({"path": str(internal)})
    output_result = read_tool.execute({"path": str(child_output)})
    payload = json.loads(internal_result.output)

    assert internal_result.ok is False
    assert internal_result.error_code == "WRONG_STATUS_SURFACE"
    assert payload["error"] == "internal_agent_status_ref"
    assert payload["next_action"] == "await_direct_child_lifecycle_event"
    assert payload["child_result_index_row"]["status"] == "DONE"
    assert payload["child_result_index_row"]["read_order"] == [str(child_output)]
    assert payload["child_result_index_row"]["primary_artifact_refs"] == [str(child_output)]
    assert payload["child_result_index_row"]["primary_artifact_stats"] == [
        {
            "path": str(child_output),
            "size_bytes": child_output.stat().st_size,
            "line_count": 1,
            "char_count": len("declared child result"),
        }
    ]
    assert payload["child_result_index_row"]["expected_outputs"] == [str(child_output)]
    assert payload["child_result_index_row"]["artifact_registry_refs"][0]["artifact_id"] == "result-1"
    assert output_result.ok is True
    assert "declared child result" in output_result.output


def test_list_files_routes_internal_agent_status_dirs_to_agent_tree(tmp_path: Path):
    workspace = tmp_path / "workspace"
    internal = workspace / "tasks" / "2026-06-06" / "demo" / "work" / "agents" / "subagent-123"
    child_output = workspace / "tasks" / "2026-06-06" / "demo" / "work" / "child_outputs" / "subagent-123.md"
    internal.mkdir(parents=True)
    child_output.parent.mkdir(parents=True)
    child_output.write_text("declared child result", encoding="utf-8")
    (internal / "state.json").write_text(
        json.dumps(
            {
                "status": "DONE",
                "artifact_refs": [str(child_output)],
                "attributes": {"output_files": [str(child_output)]},
            }
        ),
        encoding="utf-8",
    )
    list_tool = ListFilesTool(workspace, max_entries=20)

    result = list_tool.execute({"path": str(internal)})
    payload = json.loads(result.output)

    assert result.ok is False
    assert result.error_code == "WRONG_STATUS_SURFACE"
    assert payload["next_action"] == "await_direct_child_lifecycle_event"
    assert payload["child_result_index_row"]["read_order"] == [str(child_output)]


def test_list_files_hides_internal_agent_status_refs_when_listing_task_root(tmp_path: Path):
    workspace = tmp_path / "workspace"
    task_root = workspace / "tasks" / "2026-06-06" / "demo"
    internal = task_root / "work" / "agents" / "subagent-123"
    child_output = task_root / "work" / "child_outputs" / "subagent-123.md"
    final_output = task_root / "output" / "report.md"
    (internal / "compactions").mkdir(parents=True)
    child_output.parent.mkdir(parents=True)
    final_output.parent.mkdir(parents=True)
    (internal / "canonical_state.json").write_text("{}", encoding="utf-8")
    (internal / "compactions" / "latest_summary.md").write_text("internal", encoding="utf-8")
    child_output.write_text("declared child result", encoding="utf-8")
    final_output.write_text("final", encoding="utf-8")
    list_tool = ListFilesTool(workspace, max_entries=50)

    result = list_tool.execute({"path": str(task_root), "recursive": True})

    assert result.ok is True
    assert "work/agents" not in result.output
    assert "canonical_state.json" not in result.output
    assert "latest_summary.md" not in result.output
    assert "work/child_outputs/subagent-123.md" in result.output
    assert "output/report.md" in result.output


def test_read_file_reports_active_child_declared_output_not_ready(tmp_path: Path):
    workspace = tmp_path / "workspace"
    output = workspace / "tasks" / "2026-06-06" / "demo" / "output" / "child.md"
    output.parent.mkdir(parents=True)
    registry = make_tool_registry(workspace)

    result = _execute(
        registry,
        "read_file",
        {"path": str(output)},
        allowed_tools=["read_file"],
        write_boundary={"locked_files": [str(output)]},
    )
    payload = json.loads(result.output)

    assert result.ok is False
    assert result.error_code == "PATH_NOT_FOUND"
    assert payload["error"] == "active_child_output_not_ready"
    assert payload["next_action"] == "await_direct_child_lifecycle_event"
    assert "suggested_tool_call" not in payload


def test_filesystem_tool_reports_missing_external_path_without_permission_claim():
    """LLM: near-miss workspace paths are no longer misreported as permission problems.

    新手说明:
    模型把用户名或工作区前缀拼错时，工具返回普通路径不存在，后续可继续搜索定位。
    """
    with tempfile.TemporaryDirectory() as td:
        base = Path(td).resolve()
        workspace = base / "my-claude-code"
        workspace.mkdir()
        wrong = base / "wrong-user" / "my-claude-code" / "deliverables" / "shop" / "build"
        suggested = workspace / "deliverables" / "shop" / "build"
        tool = ListFilesTool(workspace, max_entries=20)

        result = tool.execute({"path": str(wrong)})

        assert not result.ok
        assert suggested
        assert result.error_code == "PATH_NOT_FOUND"
        assert "路径不存在" in result.output


def test_read_file_missing_path_returns_workspace_candidates_not_a_dead_end(tmp_path: Path):
    """LLM: stale copied paths should return bounded workspace candidates instead of a bare miss.

    新手说明:
    模型拿到旧路径或抄错路径时，系统不应该直接让它撞墙。
    它应该告诉模型：这个路径不存在，但工作区里有几个可能的候选路径，你自己再读。
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    actual = workspace / "data" / "subagents" / "tasks" / "root-1" / "agents" / "agent-3" / "data"
    actual.mkdir(parents=True)
    report = actual / "finding_report.md"
    report.write_text("real child report", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "finding_report.md").write_text("outside secret", encoding="utf-8")
    stale_path = "data/subagents/agent-3/data/finding_report.md"
    read_tool = ReadFileTool(workspace, max_chars=2000)

    result = read_tool.execute({"path": stale_path})

    assert not result.ok
    assert result.error_code == "PATH_NOT_FOUND"
    assert result.result_envelope["path_not_found"] is True
    assert str(report) in result.result_envelope["candidate_paths"]
    assert str(outside) not in result.output
    assert "candidate_paths" in result.output
    assert "请用 read_file 重新读取确认" in result.output


def test_list_and_search_missing_path_return_recovery_candidates(tmp_path: Path):
    """LLM: list/search path misses should share the same recovery surface as read_file."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    reports = workspace / "reports"
    reports.mkdir()
    (reports / "weekly-summary.md").write_text("needle", encoding="utf-8")
    list_tool = ListFilesTool(workspace, max_entries=20)
    search_tool = SearchTextTool(workspace, max_matches=20)

    listed = list_tool.execute({"path": "reports/weekly"})
    searched = search_tool.execute({"query": "needle", "path": "reports/weekly"})

    assert not listed.ok
    assert listed.error_code == "PATH_NOT_FOUND"
    assert str(reports / "weekly-summary.md") in listed.output
    assert not searched.ok
    assert searched.error_code == "PATH_NOT_FOUND"
    assert str(reports / "weekly-summary.md") in searched.output


def test_filesystem_tools_reject_bad_parameters_and_allow_absolute_external_paths():
    """LLM: verify that filesystem tools reject bad params and allow ordinary external paths.

    新手说明:
    读普通工作区外文件可成功；start_line 传字符串应报错；
    path 传非字符串应报错；recursive="false" 应只列一层。
    """
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as outside_td:
        workspace = Path(td)
        outside_file = Path(outside_td) / "secret.txt"
        outside_file.write_text("secret", encoding="utf-8")
        (workspace / "notes.txt").write_text("line one\nline two\n", encoding="utf-8")
        read_tool = ReadFileTool(workspace, max_chars=2000)
        write_tool = WriteFileTool(workspace)
        list_tool = ListFilesTool(workspace, max_entries=20)
        (workspace / "src").mkdir()
        (workspace / "src" / "nested.txt").write_text("nested", encoding="utf-8")

        outside = read_tool.execute({"path": str(outside_file)})
        bad_line = read_tool.execute({"path": "notes.txt", "start_line": "abc"})
        bad_path_type = write_tool.execute({"path": {"bad": "type"}, "content": "x"})
        non_recursive = list_tool.execute({"path": ".", "recursive": "false"})

        assert outside.ok
        assert "secret" in outside.output
        assert not bad_line.ok
        assert "start_line 必须是整数" in bad_line.output
        assert not bad_path_type.ok
        assert "path 参数必须是字符串路径" in bad_path_type.output
        assert non_recursive.ok
        assert "src/" in non_recursive.output
        assert "src/nested.txt" not in non_recursive.output


def test_read_file_reports_next_start_line_when_truncated():
    """LLM: long read_file outputs should include a continuation cursor.

    新手说明:
    文件太长被截断时，要告诉模型下次从哪一行继续读，避免它乱猜行号。
    """
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        target = workspace / "long.txt"
        target.write_text("\n".join(f"Line {idx}" for idx in range(1, 80)), encoding="utf-8")
        read_tool = ReadFileTool(workspace, max_chars=80)

        result = read_tool.execute({"path": "long.txt"})

        assert result.ok
        assert "PARTIAL view only" in result.output
        assert "total_lines=79" in result.output
        assert "next_start_line=" in result.output


def test_read_file_start_line_with_max_chars_keeps_line_cursor():
    """LLM: start_line is a line cursor; max_chars should only bound that page."""
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        target = workspace / "long.txt"
        target.write_text("\n".join(f"Line {idx}" for idx in range(1, 20)), encoding="utf-8")
        read_tool = ReadFileTool(workspace, max_chars=30)

        result = read_tool.execute({"path": "long.txt", "start_line": 5, "max_chars": 30})

        assert result.ok
        assert "5: Line 5" in result.output
        assert "1: Line 1" not in result.output
        assert "char-window offset=0" not in result.output


def test_read_file_line_mode_honors_request_max_chars_below_tool_cap():
    """LLM: model-requested line read pages should stay small when max_chars is explicit."""
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        target = workspace / "long.txt"
        target.write_text("\n".join(f"Line {idx} has extra text" for idx in range(1, 20)), encoding="utf-8")
        read_tool = ReadFileTool(workspace, max_chars=2000)

        result = read_tool.execute({"path": "long.txt", "start_line": 5, "max_chars": 40})

        assert result.ok
        assert "5: Line 5" in result.output
        assert "PARTIAL view only" in result.output
        assert "limit_chars=40" in result.output
        assert "recommended_next_max_chars=2000" in result.output
        assert "next_call=read_file(start_line=" in result.output
        assert "max_chars=2000" in result.output
        assert "next_start_line=" in result.output
        assert len(result.output) < 260


def test_read_file_single_long_line_reports_next_offset_and_resumes():
    """LLM: minified JSON/log style files may be one huge line; line cursor alone would skip data."""
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        target = workspace / "one-line.txt"
        target.write_text("A" * 120 + "NEEDLE-IN-LATE-CHUNK" + "B" * 60, encoding="utf-8")
        read_tool = ReadFileTool(workspace, max_chars=80)

        first = read_tool.execute({"path": "one-line.txt"})
        second = read_tool.execute({"path": "one-line.txt", "offset": 80, "max_chars": 120})

        assert first.ok
        assert "PARTIAL view only" in first.output
        assert "next_offset=80" in first.output
        assert second.ok
        assert "NEEDLE-IN-LATE-CHUNK" in second.output
        assert "offset=80" in second.output


def test_read_file_char_window_streams_without_full_read(monkeypatch):
    """LLM: char-window reads should not load the entire file before slicing."""
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        target = workspace / "large.txt"
        target.write_text("甲" * 200 + "NEEDLE" + "乙" * 200, encoding="utf-8")
        read_tool = ReadFileTool(workspace, max_chars=80)

        original_read_text = Path.read_text

        def fail_read_text(self, *args, **kwargs):
            if self == target:
                raise AssertionError("char-window read_file must stream instead of Path.read_text")
            return original_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", fail_read_text)

        first = read_tool.execute({"path": "large.txt", "offset": 180, "max_chars": 80})
        second = read_tool.execute({"path": "large.txt", "offset": 260, "max_chars": 80})

        assert first.ok
        assert "NEEDLE" in first.output
        assert "total_chars=406" in first.output
        assert "next_offset=260" in first.output
        assert first.result_envelope["read_window"] == {
            "kind": "char_window",
            "offset": 180,
            "chars": 80,
            "next_offset": 260,
            "total_chars": 406,
            "complete": False,
        }
        assert second.ok
        assert "offset=260" in second.output


def test_read_file_start_line_past_eof_reports_total_lines():
    """LLM: tail reads past EOF should return actionable line-count guidance.

    新手说明:
    模型读超过文件末尾时，要返回总行数和建议尾部范围，而不是让它看到空结果继续猜。
    """
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        target = workspace / "short.txt"
        target.write_text("one\ntwo\nthree\n", encoding="utf-8")
        read_tool = ReadFileTool(workspace, max_chars=2000)

        result = read_tool.execute({"path": "short.txt", "start_line": 99})

        assert not result.ok
        assert "start_line 超出文件末尾" in result.output
        assert "total_lines=3" in result.output
        assert "end_line=3" in result.output


def test_search_text_does_not_walk_symlink_but_read_file_allows_non_dangerous_target():
    """LLM: directory search still does not walk symlinks, while direct reads allow ordinary targets.

    新手说明:
    在工作区内创建指向普通外部文件的符号链接，目录搜索不展开，直接读取可以按路径策略读取。
    """
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as outside_td:
        workspace = Path(td)
        outside_file = Path(outside_td) / "outside.txt"
        outside_file.write_text("needle outside workspace", encoding="utf-8")
        link = workspace / "linked-outside.txt"
        try:
            link.symlink_to(outside_file)
        except OSError:
            return
        search_tool = SearchTextTool(workspace, max_matches=10)
        read_tool = ReadFileTool(workspace, max_chars=2000)

        search_result = search_tool.execute({"query": "needle", "path": "."})
        read_result = read_tool.execute({"path": "linked-outside.txt"})

        assert search_result.ok
        assert "没有找到匹配项" in search_result.output
        assert read_result.ok
        assert "needle outside workspace" in read_result.output


def test_search_text_supports_limit_offset_and_glob(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "a.py").write_text("needle one\nneedle two\n", encoding="utf-8")
    (workspace / "b.md").write_text("needle markdown\n", encoding="utf-8")
    tool = SearchTextTool(workspace, max_matches=10)

    result = tool.execute({"query": "needle", "file_glob": "*.py", "limit": 1, "offset": 1})

    assert result.ok
    assert "a.py:2" in result.output
    assert "b.md" not in result.output
    assert "next_offset" not in result.output
    assert result.result_envelope["page_window"] == {
        "kind": "offset_page",
        "tool": "search_text",
        "source_path": ".",
        "offset": 1,
        "limit": 1,
        "returned": 1,
        "next_offset": 0,
        "complete": True,
        "output_mode": "content",
    }


def test_search_text_returns_structured_page_window_when_more_matches_remain(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "a.py").write_text("needle one\nneedle two\n", encoding="utf-8")
    tool = SearchTextTool(workspace, max_matches=10)

    result = tool.execute({"query": "needle", "path": ".", "limit": 1})

    assert result.ok
    assert "next_offset=1" in result.output
    assert result.result_envelope["page_window"] == {
        "kind": "offset_page",
        "tool": "search_text",
        "source_path": ".",
        "offset": 0,
        "limit": 1,
        "returned": 1,
        "next_offset": 1,
        "complete": False,
        "output_mode": "content",
    }


def test_search_text_supports_files_and_count_output_modes(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "a.py").write_text("Needle one\nNeedle two\n", encoding="utf-8")
    (workspace / "b.py").write_text("Needle three\n", encoding="utf-8")
    tool = SearchTextTool(workspace, max_matches=10)

    files = tool.execute({"query": "Needle", "output_mode": "files_with_matches"})
    counts = tool.execute({"query": "Needle", "output_mode": "count"})

    assert files.ok
    assert "a.py" in files.output
    assert "b.py" in files.output
    assert "Needle one" not in files.output
    assert counts.ok
    assert "a.py: 2" in counts.output
    assert "b.py: 1" in counts.output


def test_search_text_supports_line_numbers_output_mode(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "journal.txt").write_text("===== 章节 001 =====\nnoise\n===== 章节 002 =====\n", encoding="utf-8")
    tool = SearchTextTool(workspace, max_matches=10)

    result = tool.execute({"query": "===== 章节", "path": "journal.txt", "output_mode": "line_numbers"})

    assert result.ok
    assert result.output.splitlines() == ["journal.txt:1", "journal.txt:3"]


def test_search_text_supports_regex_and_ignore_case(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "alerts.txt").write_text("ALERT-123\nalert-abc\n", encoding="utf-8")
    tool = SearchTextTool(workspace, max_matches=10)

    regex = tool.execute({"query": r"alert-\d+", "literal": False, "ignore_case": True})
    literal = tool.execute({"query": r"alert-\d+", "literal": True, "ignore_case": True})

    assert regex.ok
    assert "ALERT-123" in regex.output
    assert "alert-abc" not in regex.output
    assert literal.ok
    assert "没有找到匹配项" in literal.output


def test_search_text_skips_common_noise_dirs_by_default(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "src.py").write_text("needle visible\n", encoding="utf-8")
    node_modules = workspace / "node_modules"
    node_modules.mkdir()
    (node_modules / "pkg.py").write_text("needle hidden\n", encoding="utf-8")
    tool = SearchTextTool(workspace, max_matches=10)

    default = tool.execute({"query": "needle"})
    included = tool.execute({"query": "needle", "include_ignored": True})

    assert default.ok
    assert "src.py" in default.output
    assert "node_modules" not in default.output
    assert included.ok
    assert "node_modules/pkg.py" in included.output


def test_search_text_uses_rg_backend_when_available(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "a.py").write_text("Needle one\n", encoding="utf-8")
    tool = SearchTextTool(workspace, max_matches=10)
    calls: list[list[str]] = []

    class FakePopen:
        def __init__(self, args, **_kwargs):
            self.args = args
            self.stdout = io.StringIO(
                '{"type":"match","data":{"path":{"text":"'
                + str(workspace / "a.py")
                + '"},"lines":{"text":"Needle one\\n"},"line_number":1}}\n'
            )
            self.returncode = 0
            calls.append(args)

        def wait(self, timeout=None):
            return self.returncode

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = -15

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr(search_mod, "shutil", types.SimpleNamespace(which=lambda _name: "/usr/bin/rg"), raising=False)
    monkeypatch.setattr(
        search_mod,
        "subprocess",
        types.SimpleNamespace(Popen=FakePopen, PIPE=object(), DEVNULL=None, TimeoutExpired=TimeoutError),
        raising=False,
    )

    result = tool.execute({"query": "Needle", "file_glob": "*.py", "literal": True})

    assert result.ok
    assert "a.py:1: Needle one" in result.output
    assert calls
    assert calls[0][0] == "/usr/bin/rg"
    assert "--fixed-strings" in calls[0]
    assert "--glob" in calls[0]


def test_search_text_streams_rg_and_stops_after_page(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "large.log").write_text("status=ok\n" * 100, encoding="utf-8")
    tool = SearchTextTool(workspace, max_matches=5)
    processes: list[object] = []

    class FakePopen:
        def __init__(self, _args, **_kwargs):
            self.returncode = None
            self.terminated = False
            self.stdout = (
                f'{{"type":"match","data":{{"path":{{"text":"{workspace / "large.log"}"}},'
                f'"lines":{{"text":"status=ok {idx}\\n"}},"line_number":{idx}}}}}\n'
                for idx in range(1, 100)
            )
            processes.append(self)

        def wait(self, timeout=None):
            if self.returncode is None:
                self.returncode = 0
            return self.returncode

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = -15

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr(search_mod, "shutil", types.SimpleNamespace(which=lambda _name: "/usr/bin/rg"), raising=False)
    monkeypatch.setattr(
        search_mod,
        "subprocess",
        types.SimpleNamespace(Popen=FakePopen, PIPE=object(), DEVNULL=None, TimeoutExpired=TimeoutError),
        raising=False,
    )

    result = tool.execute({"query": "status=", "path": "large.log", "limit": 5})

    assert result.ok
    assert "large.log:1" in result.output
    assert "large.log:5" in result.output
    assert "next_offset=5" in result.output
    assert processes and processes[0].terminated


def test_search_text_treats_rg_no_matches_as_empty_result(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "a.py").write_text("Needle one\n", encoding="utf-8")
    tool = SearchTextTool(workspace, max_matches=10)

    class FakePopen:
        stdout = io.StringIO("")
        returncode = 1

        def __init__(self, *_args, **_kwargs):
            pass

        def wait(self, timeout=None):
            return self.returncode

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = -15

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr(search_mod, "shutil", types.SimpleNamespace(which=lambda _name: "/usr/bin/rg"), raising=False)
    monkeypatch.setattr(
        search_mod,
        "subprocess",
        types.SimpleNamespace(Popen=FakePopen, PIPE=object(), DEVNULL=None, TimeoutExpired=TimeoutError),
        raising=False,
    )

    result = tool.execute({"query": "Missing"})

    assert result.ok
    assert "没有找到匹配项" in result.output


def test_list_files_supports_limit_offset_depth_and_glob(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "a.py").write_text("a", encoding="utf-8")
    (workspace / "b.py").write_text("b", encoding="utf-8")
    (workspace / "c.md").write_text("c", encoding="utf-8")
    (workspace / "d.py").write_text("d", encoding="utf-8")
    nested = workspace / "nested"
    nested.mkdir()
    (nested / "deep.py").write_text("d", encoding="utf-8")
    tool = ListFilesTool(workspace, max_entries=10)

    result = tool.execute(
        {
            "path": ".",
            "recursive": True,
            "file_glob": "*.py",
            "limit": 1,
            "offset": 1,
            "max_depth": 1,
        }
    )

    assert result.ok
    assert "b.py" in result.output
    assert "a.py" not in result.output
    assert "c.md" not in result.output
    assert "d.py" not in result.output
    assert "nested/deep.py" not in result.output
    assert "next_offset=2" in result.output
    assert result.result_envelope["page_window"] == {
        "kind": "offset_page",
        "tool": "list_files",
        "source_path": ".",
        "offset": 1,
        "limit": 1,
        "returned": 1,
        "next_offset": 2,
        "complete": False,
    }


def test_list_files_sorts_entries_and_skips_common_noise_dirs(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "zeta.txt").write_text("z", encoding="utf-8")
    (workspace / "alpha.txt").write_text("a", encoding="utf-8")
    git_dir = workspace / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("ref", encoding="utf-8")
    node_modules = workspace / "node_modules"
    node_modules.mkdir()
    (node_modules / "pkg.js").write_text("pkg", encoding="utf-8")
    tool = ListFilesTool(workspace, max_entries=20)

    result = tool.execute({"path": ".", "recursive": True})

    assert result.ok
    lines = result.output.splitlines()
    assert lines.index("alpha.txt") < lines.index("zeta.txt")
    assert ".git" not in result.output
    assert "node_modules" not in result.output


def test_list_files_explicit_glob_retries_common_noise_dirs(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    cache = workspace / "pkg" / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "module.pyc").write_bytes(b"cache")
    tool = ListFilesTool(workspace, max_entries=20)

    result = tool.execute(
        {
            "path": ".",
            "recursive": True,
            "file_glob": "*.pyc",
        }
    )
    excluded = tool.execute(
        {
            "path": ".",
            "recursive": True,
            "file_glob": "*.pyc",
            "include_ignored": False,
        }
    )

    assert result.ok
    assert "pkg/__pycache__/module.pyc" in result.output
    assert result.result_envelope["discovery"] == {
        "included_ignored": True,
        "reason": "explicit_pattern_no_visible_matches",
    }
    assert "module.pyc" not in excluded.output


def test_find_files_finds_glob_matches_and_skips_common_noise_dirs(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    src = workspace / "src"
    src.mkdir()
    (src / "a.py").write_text("a", encoding="utf-8")
    (src / "b.md").write_text("b", encoding="utf-8")
    node_modules = workspace / "node_modules"
    node_modules.mkdir()
    (node_modules / "hidden.py").write_text("hidden", encoding="utf-8")
    tool = FindFilesTool(workspace, max_matches=20)

    result = tool.execute({"pattern": "**/*.py", "path": "."})

    assert result.ok
    assert "src/a.py" in result.output
    assert "src/b.md" not in result.output
    assert "node_modules" not in result.output
    assert result.result_envelope["page_window"]["complete"] is True


def test_find_files_explicit_glob_retries_common_noise_dirs(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    cache = workspace / "pkg" / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "module.pyc").write_bytes(b"cache")
    tool = FindFilesTool(workspace, max_matches=20)

    result = tool.execute({"pattern": "**/*.pyc", "path": "."})
    excluded = tool.execute(
        {
            "pattern": "**/*.pyc",
            "path": ".",
            "include_ignored": False,
        }
    )

    assert result.ok
    assert "pkg/__pycache__/module.pyc" in result.output
    assert result.result_envelope["discovery"] == {
        "included_ignored": True,
        "reason": "explicit_pattern_no_visible_matches",
    }
    assert "module.pyc" not in excluded.output


def test_find_files_returns_structured_page_window_when_more_matches_remain(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    src = workspace / "src"
    src.mkdir()
    for name in ("a.py", "b.py", "c.py"):
        (src / name).write_text(name, encoding="utf-8")
    tool = FindFilesTool(workspace, max_matches=20)

    result = tool.execute({"pattern": "**/*.py", "path": ".", "limit": 2})

    assert result.ok
    assert "next_offset=2" in result.output
    assert result.result_envelope["page_window"] == {
        "kind": "offset_page",
        "tool": "find_files",
        "source_path": ".",
        "offset": 0,
        "limit": 2,
        "returned": 2,
        "next_offset": 2,
        "complete": False,
    }


def test_find_files_is_registered_in_base_registry(tmp_path: Path):
    registry = make_tool_registry(tmp_path)

    result = _execute(
        registry,
        "find_files",
        {"pattern": "*.py", "path": "."},
        allowed_tools=["find_files"],
    )

    assert result.ok
    assert "没有找到匹配文件" in result.output


def test_apply_patch_tool_updates_text():
    """LLM: verify that ApplyPatchTool updates text with explicit context.

    新手说明:
    在已有文件里替换一处文本，确认文件内容正确。
    """
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        target = workspace / "src" / "demo.py"
        target.parent.mkdir(parents=True)
        target.write_text("def hello():\n    return 'old'\n", encoding="utf-8")

        tool = ApplyPatchTool(workspace)
        result = tool.execute(
            {
                "patch": (
                    "*** Begin Patch\n"
                    "*** Update File: src/demo.py\n"
                    " def hello():\n"
                    "-    return 'old'\n"
                    "+    return 'new'\n"
                    "*** End Patch\n"
                )
            }
        )

        assert result.ok
        assert target.read_text(encoding="utf-8") == "def hello():\n    return 'new'\n"
