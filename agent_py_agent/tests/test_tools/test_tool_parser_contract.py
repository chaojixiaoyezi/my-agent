"""LLM: parser and compact tool-catalog contract tests live outside the tool-loop flow tests.

函数/模块用途: 验证模型工具调用解析、坏格式重试提示、工具目录示例和参数包兼容，不让主 tool_loop 测试文件继续膨胀。
"""

from __future__ import annotations

from pathlib import Path

from agent_py_agent.agent.tooling.models import ToolSpec
from agent_py_agent.agent.tooling.registry import ToolRegistry, ToolRegistryParams

from .backends import make_tool_registry


def _registry() -> ToolRegistry:
    return ToolRegistry(
        ToolRegistryParams(
            workspace_root=Path.cwd(),
            max_chars=6000,
            max_entries=200,
            max_matches=50,
            web_max_chars=12000,
            http_timeout=30,
            catalog_limit=20,
            retrieval_limit=3,
            vector_search_enabled=False,
            shell_tool_timeout=30,
        )
    )


def test_tool_catalog_and_recommended_sections():
    """LLM: verify that catalog and recommended-tools sections render correctly.

    新手说明:
    检查 API/HTTP 场景也统一推荐 web_fetch，避免模型看到重复网络入口。
    """
    registry = _registry()

    catalog = registry.render_catalog_section()
    recommended = registry.render_recommended_tools_section("帮我测试一个 REST API 接口并查看返回")

    assert "# Tool Catalog" in catalog
    assert "web_fetch [web" in catalog
    assert "http_request [api]" not in catalog
    assert "web_extract [web]" not in catalog
    assert "关键参数" in catalog
    assert "适用场景" not in catalog
    assert "示例：" not in catalog
    assert "web_fetch [web]" in recommended
    assert "参数：" in recommended
    assert "推荐理由" in recommended


def test_registry_uses_configured_write_inline_recommendation(tmp_path: Path):
    registry = ToolRegistry(
        ToolRegistryParams(
            workspace_root=tmp_path,
            max_chars=6000,
            max_entries=200,
            max_matches=50,
            web_max_chars=12000,
            http_timeout=30,
            catalog_limit=20,
            retrieval_limit=3,
            vector_search_enabled=False,
            shell_tool_timeout=30,
            tool_write_inline_max_chars=512,
        )
    )

    result = registry.execute_call(
        {"tool": "write_file", "path": "site/app.js", "content": "A" * 513},
        allowed_tools=None,
    )

    assert result.ok is True
    assert (tmp_path / "site/app.js").read_text(encoding="utf-8") == "A" * 513
    assert "超过推荐值" in result.output
    assert "512" in result.output


def test_registry_uses_configured_shell_timeout(tmp_path: Path):
    registry = ToolRegistry(
        ToolRegistryParams(
            workspace_root=tmp_path,
            max_chars=6000,
            max_entries=200,
            max_matches=50,
            web_max_chars=12000,
            http_timeout=30,
            catalog_limit=20,
            retrieval_limit=3,
            vector_search_enabled=False,
            shell_tool_timeout=240,
            shell_tool_output_max_chars=1234,
        )
    )

    tool = registry.tools["run_command"]

    assert tool.default_timeout == 240
    assert tool.max_output_chars == 1234
    assert "default 240" in tool.spec.parameters["timeout"]


def test_tool_catalog_format_example_does_not_bias_to_path_param():
    """LLM: Tool call instructions should not teach all tools to pass a fake path parameter."""
    registry = _registry()

    catalog = registry.render_catalog_section()

    assert '{"tool": "tool_name", "path": "example"}' not in catalog
    assert '"actual_parameter_name": "actual_value"' in catalog
    assert '"param_name": "param_value"' not in catalog
    assert "不要写 param_name" in catalog


def test_tool_catalog_includes_global_large_content_protocol():
    registry = _registry()

    catalog = registry.render_catalog_section()

    assert "# Tool Content Transport Protocol" in catalog
    assert "不要把完整大文件正文塞进一个 JSON 工具参数" in catalog
    assert "write_file" in catalog
    assert "apply_patch" in catalog
    assert "[WRITE_FILE_RAW" in catalog


def test_tool_catalog_uses_configured_categories_offset_and_notice():
    registry = ToolRegistry(
        ToolRegistryParams(
            workspace_root=Path.cwd(),
            max_chars=6000,
            max_entries=200,
            max_matches=50,
            web_max_chars=12000,
            http_timeout=30,
            catalog_limit=1,
            catalog_offset=1,
            catalog_categories=["filesystem"],
            catalog_include_examples=False,
            retrieval_limit=3,
            vector_search_enabled=False,
            shell_tool_timeout=30,
        )
    )

    catalog = registry.render_catalog_section()

    assert "find_files" in catalog
    assert "list_files" not in catalog
    assert "read_file" not in catalog
    assert "示例：" not in catalog
    assert "next_offset=2" in catalog


def test_tool_call_parser_unwraps_model_param_name_bundle():
    """LLM: tolerate models that wrap real tool parameters in a literal param_name bundle."""
    registry = make_tool_registry(Path.cwd())

    calls = registry.parse_tool_calls(
        '[TOOL_CALL]\n{"tool":"read_file","param_name":{"path":"README.md"}}\n[/TOOL_CALL]'
    )

    assert calls == [{"tool": "read_file", "path": "README.md"}]


def test_tool_executor_unwraps_model_param_name_bundle(tmp_path: Path):
    """LLM: direct execution should also recover literal param_name bundles before tool dispatch."""
    (tmp_path / "notes.txt").write_text("bundle recovered", encoding="utf-8")
    registry = make_tool_registry(tmp_path)

    result = registry.execute_call({"tool": "read_file", "param_name": {"path": "notes.txt"}})

    assert result.ok
    assert "bundle recovered" in result.output


def test_tool_call_parser_keeps_orchestration_params_flat():
    """LLM: orchestration tools no longer unwrap a second category bundle."""
    registry = make_tool_registry(Path.cwd())

    calls = registry.parse_tool_calls(
        '[TOOL_CALL]\n{"tool":"inspect_agent_tree","scope":"root_tree"}\n[/TOOL_CALL]'
    )

    assert calls == [{"tool": "inspect_agent_tree", "scope": "root_tree"}]


def test_tool_executor_unwraps_model_filesystem_bundle(tmp_path: Path):
    """LLM: filesystem category wrappers should be flattened before file tool execution."""
    (tmp_path / "notes.txt").write_text("category bundle recovered", encoding="utf-8")
    registry = make_tool_registry(tmp_path)

    result = registry.execute_call({"tool": "read_file", "filesystem": {"path": "notes.txt"}})

    assert result.ok
    assert "category bundle recovered" in result.output


def test_tool_call_parser_unwraps_actual_parameter_name_bundle():
    registry = make_tool_registry(Path.cwd())

    calls = registry.parse_tool_calls(
        '[TOOL_CALL]\n'
        '{"tool":"run_command","actual_parameter_name":{"command":"echo ok","working_dir":"/tmp"}}\n'
        '[/TOOL_CALL]'
    )

    assert calls == [{"tool": "run_command", "command": "echo ok", "working_dir": "/tmp"}]


def test_tool_executor_unwraps_arguments_bundle(tmp_path: Path):
    registry = make_tool_registry(tmp_path)

    result = registry.execute_call(
        {"tool": "write_file", "arguments": {"path": "notes.txt", "content": "wrapped"}}
    )

    assert result.ok
    assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "wrapped"


def test_tool_call_parser_canonicalizes_json_tool_and_param_aliases():
    registry = make_tool_registry(Path.cwd())

    calls = registry.parse_tool_calls(
        '[TOOL_CALL]\n{"tool":"write","file_path":"notes.txt","content":"ok"}\n[/TOOL_CALL]'
    )

    assert calls == [{"tool": "write_file", "path": "notes.txt", "content": "ok"}]


def test_tool_executor_canonicalizes_json_aliases(tmp_path: Path):
    registry = make_tool_registry(tmp_path)

    result = registry.execute_call({"tool": "write", "file_path": "notes.txt", "content": "alias ok"})

    assert result.ok
    assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "alias ok"


def test_tool_call_parser_rejects_conflicting_canonical_aliases():
    registry = make_tool_registry(Path.cwd())

    calls = registry.parse_tool_calls(
        '[TOOL_CALL]\n{"tool":"read_file","path":"A.md","file_path":"B.md"}\n[/TOOL_CALL]'
    )

    assert calls[0]["tool"] == "__parse_error__"
    assert "conflicting parameter aliases" in calls[0]["error"]


def test_tool_call_parser_unwraps_model_memory_bundle():
    """LLM: memory/read_artifact wrappers should flatten to stable tool params."""
    registry = make_tool_registry(Path.cwd())

    calls = registry.parse_tool_calls(
        '[TOOL_CALL]\n'
        '{"tool":"read_artifact","memory":{"artifact_ref":"/tmp/out.json","offset":0,"max_chars":4000}}\n'
        '[/TOOL_CALL]'
    )

    assert calls == [
        {"tool": "read_artifact", "artifact_ref": "/tmp/out.json", "offset": 0, "max_chars": 4000}
    ]


def test_tool_call_parser_recovers_single_extra_trailing_brace():
    registry = make_tool_registry(Path.cwd())

    calls = registry.parse_tool_calls(
        '[TOOL_CALL]\n{"tool":"read_file","path":"README.md"}\n}\n[/TOOL_CALL]'
    )

    assert calls == [{"tool": "read_file", "path": "README.md"}]


def test_tool_call_parser_recovers_detached_top_level_fields():
    registry = make_tool_registry(Path.cwd())

    calls = registry.parse_tool_calls(
        '[TOOL_CALL]\n'
        '{"tool":"write_structured_json","path":"outputs/source_index.json",'
        '"data":{"rows":[{"title":"Paper","translated":true}]}}'
        ', "source_refs":[{"source_id":"src-1","uri":"https://example.com"}],'
        ' "claims":[{"field":"title","source_ids":["src-1"],"value":"Paper"}]}\n'
        '[/TOOL_CALL]'
    )

    assert calls == [
        {
            "tool": "write_structured_json",
            "path": "outputs/source_index.json",
            "data": {"rows": [{"title": "Paper", "translated": True}]},
            "source_refs": [{"source_id": "src-1", "uri": "https://example.com"}],
            "claims": [{"field": "title", "source_ids": ["src-1"], "value": "Paper"}],
        }
    ]


def test_parse_error_result_includes_retry_format_hint():
    registry = make_tool_registry(Path.cwd())
    calls = registry.parse_tool_calls("<tool_call><function=read><parameter=file_path>README.md</parameter>")

    result = registry.execute_call(calls[0])

    assert result.ok is False
    assert result.tool == "__parse_error__"
    assert "[TOOL_CALL]" in result.output
    assert "[/TOOL_CALL]" in result.output


def test_tool_call_parser_reports_missing_closing_tool_marker():
    registry = make_tool_registry(Path.cwd())
    calls = registry.parse_tool_calls(
        '[TOOL_CALL]\n{"tool":"create_subagents","goal":"long task","plan":"1. start'
    )

    result = registry.execute_call(calls[0])

    assert calls[0]["tool"] == "__parse_error__"
    assert "缺少结束标记" in calls[0]["error"]
    assert result.ok is False
    assert "缩短 goal/plan/acceptance_checks" in result.output


def test_tool_call_parser_ignores_markers_inside_subagent_result_payload():
    """LLM: SUBAGENT_RESULT JSON strings are data, not executable tool calls."""
    registry = make_tool_registry(Path.cwd())
    calls = registry.parse_tool_calls(
        "[SUBAGENT_RESULT]\n"
        "{\n"
        '  "status": "DONE",\n'
        '  "summary": "model mentioned [TOOL_CALL] {\\\\\\"tool\\\\\\":\\\\\\"read_file\\\\\\"}"\n'
        "}\n"
        "[/SUBAGENT_RESULT]\n"
    )

    assert calls == []


def test_tool_call_parser_still_accepts_tool_call_after_subagent_result():
    """LLM: protected result masking should not hide a real tool call outside the result block."""
    registry = make_tool_registry(Path.cwd())
    calls = registry.parse_tool_calls(
        "[SUBAGENT_RESULT]\n"
        '{"summary":"quoted [TOOL_CALL] marker"}\n'
        "[/SUBAGENT_RESULT]\n"
        "[TOOL_CALL]\n"
        '{"tool":"read_file","path":"README.md"}\n'
        "[/TOOL_CALL]\n"
    )

    assert calls == [{"tool": "read_file", "path": "README.md"}]


def test_tool_call_parser_recovers_complete_json_without_closing_marker():
    registry = make_tool_registry(Path.cwd())
    calls = registry.parse_tool_calls(
        '[TOOL_CALL]\n{"tool":"write_file","path":"index.html","content":"<main>ok</main>"}'
    )

    assert calls == [
        {"tool": "write_file", "path": "index.html", "content": "<main>ok</main>"}
    ]


def test_tool_call_parser_reports_malformed_opening_marker():
    registry = make_tool_registry(Path.cwd())
    calls = registry.parse_tool_calls(
        '[TOOL_CALL\n{"tool":"read_file","path":"README.md"}\n[/TOOL_CALL]'
    )

    result = registry.execute_call(calls[0])

    assert calls[0]["tool"] == "__parse_error__"
    assert "开始标记格式错误" in calls[0]["error"]
    assert result.ok is False
    assert "[TOOL_CALL]" in result.output


def test_parse_error_hint_recommends_append_for_truncated_write():
    registry = make_tool_registry(Path.cwd())
    calls = registry.parse_tool_calls(
        '[TOOL_CALL]\n{"tool":"write_file","path":"style.css","content":"body { color: red;'
    )

    result = registry.execute_call(calls[0])

    assert calls[0]["tool"] == "__parse_error__"
    assert result.ok is False
    assert "WRITE_FILE_RAW" in result.output
    assert "1500-2000 字符" in result.output
    assert "不超过 800 字符" in result.output
    assert "只能输出 1 个 write_file" in result.output


def test_tool_spec_catalog_entry_includes_first_example():
    """LLM: Compact catalog entries should show tool-specific JSON when examples are available."""
    spec = ToolSpec(
        name="schedule_child_subagents",
        category="orchestration",
        description="create child runs",
        use_cases=["split hierarchy"],
        avoid_when=[],
        keywords=[],
        parameters={"children": "child specs", "apply": "write"},
        examples=['{"tool":"schedule_child_subagents","dry_run":false,"children":[]}'],
    )

    entry = spec.render_catalog_entry()

    assert "示例" in entry
    assert '"children":[]' in entry


def test_tool_call_parser_accepts_subagent_call_alias():
    """LLM: verify that [SUBAGENT_CALL] opening tag is accepted as an alias for [TOOL_CALL].

    新手说明:
    有些模型输出 [SUBAGENT_CALL]，解析器应该和 [TOOL_CALL] 一视同仁。
    """
    registry = _registry()
    calls = registry.parse_tool_calls(
        '[SUBAGENT_CALL]\n{"tool":"read_file","path":"README.md"}\n[/TOOL_CALL]'
    )

    assert calls == [{"tool": "read_file", "path": "README.md"}]


def test_tool_call_parser_accepts_qwen_xmlish_read_call():
    """LLM: verify that Qwen-style XML-ish function call for read is parsed correctly.

    新手说明:
    Qwen 模型可能输出 <function=read> 格式的工具调用，需要正确映射到 read_file。
    """
    registry = _registry()
    calls = registry.parse_tool_calls(
        "\n"
        "<function=read>\n"
        "<parameter=file_path>\nREADME.md\n</parameter>\n"
        "</function>\n"
        ""
    )

    assert calls == [{"tool": "read_file", "path": "README.md"}]


def test_tool_call_parser_accepts_qwen_xmlish_write_call():
    """LLM: verify that Qwen-style XML-ish function call for write handles HTML entities.

    新手说明:
    Qwen 写文件调用里 &amp; 应该被解码成 &。
    """
    registry = _registry()
    calls = registry.parse_tool_calls(
        '<function name="write">'
        '<parameter name="file_path">notes.txt</parameter>'
        '<parameter name="content">hello &amp; hi</parameter>'
        "</function>"
    )

    assert calls == [{"tool": "write_file", "path": "notes.txt", "content": "hello & hi"}]


def test_tool_call_parser_reports_incomplete_qwen_xmlish_call():
    """LLM: verify that an incomplete Qwen XML-ish call is reported as a parse error.

    新手说明:
    第二个调用缺少闭合标签，解析器应返回 __parse_error__ 而不是崩溃。
    """
    registry = _registry()
    calls = registry.parse_tool_calls(
        "<function=read><parameter=file_path>A.md</parameter>\n"
        "<function=read><parameter=file_path>B.md</parameter>"
    )

    assert calls[0] == {"tool": "read_file", "path": "A.md"}
    assert calls[1]["tool"] == "__parse_error__"
    assert "missing a closing " in calls[1]["error"]
    assert "B.md" in calls[1]["raw"]


def test_tool_call_parser_and_executor_reject_non_object_payloads():
    """LLM: verify that non-object JSON payloads are rejected at parse and execute time.

    新手说明:
    传入数组或列表应该报错，提示必须是 JSON 对象。
    """
    registry = make_tool_registry(Path.cwd())

    calls = registry.parse_tool_calls("[TOOL_CALL]\n[1, 2, 3]\n[/TOOL_CALL]")
    parsed_result = registry.execute_call(calls[0])
    direct_result = registry.execute_call(["not", "a", "dict"])

    assert calls[0]["tool"] == "__parse_error__"
    assert "JSON 对象" in calls[0]["error"]
    assert not parsed_result.ok
    assert "JSON 对象" in parsed_result.output
    assert not direct_result.ok
    assert "JSON 对象" in direct_result.output


def test_tool_call_parser_recovers_next_block_after_unclosed_nested_start():
    registry = make_tool_registry(Path.cwd())

    calls = registry.parse_tool_calls(
        "[TOOL_CALL]\n"
        '{"tool":"write_structured_json","path":"outputs/data.json","sheets":[{"rows":[\n'
        "[TOOL_CALL]\n"
        '{"tool":"run_command","command":"mkdir -p outputs"}\n'
        "[/TOOL_CALL]"
    )

    assert calls[0]["tool"] == "__parse_error__"
    assert "缺少结束标记" in calls[0]["error"]
    assert calls[1] == {"tool": "run_command", "command": "mkdir -p outputs"}
