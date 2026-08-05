"""LLM: parser and compact tool-catalog contract tests live outside the tool-loop flow tests.

函数/模块用途: 验证模型工具调用解析、坏格式重试提示、工具目录示例和参数包兼容，不让主 tool_loop 测试文件继续膨胀。
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.agent.contracts.tool_input_schema import validate_tool_input
from agent_py_agent.agent.ingestion.watch_tool_spec import build_watch_stream_spec
from agent_py_agent.agent.tooling.models import ToolSpec
from agent_py_agent.agent.tooling.registry import ToolRegistry, ToolRegistryParams
from agent_py_agent.agent.tooling.tool_spec_schema import tool_spec_input_schema

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
            operation_store_required=False,
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
    assert '{"tool": "tool_name", "parameter_name": "parameter_value"}' not in catalog
    assert '{"tool": "read_file", "path": "README.md"}' in catalog
    assert '"actual_parameter_name": "actual_value"' not in catalog
    assert '"param_name": "param_value"' not in catalog
    assert "不要写 param_name、args、arguments" in catalog


def test_tool_catalog_includes_global_large_content_protocol():
    registry = _registry()

    catalog = registry.render_catalog_section()

    assert "# Tool Content Transport Protocol" in catalog
    assert "不要把完整大文件正文塞进一个 JSON 工具参数" in catalog
    assert "write_file" in catalog
    assert "apply_patch" in catalog
    assert "[WRITE_FILE_RAW" in catalog


def test_read_only_tool_catalog_omits_irrelevant_write_transport_protocol():
    registry = _registry()

    catalog = registry.render_catalog_section(allowed_tools=["read_file"])

    assert "read_file" in catalog
    assert "# Tool Content Transport Protocol" not in catalog
    assert "[WRITE_FILE_RAW" not in catalog


def test_watch_verdict_schema_requires_explicit_per_record_results():
    spec = build_watch_stream_spec()

    assert "minItems" not in spec.parameter_schema["verdicts"]
    assert "verdict_default" not in spec.parameter_schema
    assert "verdict_batch" not in spec.parameter_schema
    assert spec.parameter_schema["delivery_ref"]["pattern"] == "^ad-[0-9a-f]{24}$"
    verdict_item = spec.parameter_schema["verdicts"]["items"]
    assert verdict_item["properties"]["verdict_token"]["pattern"] == (
        "^vt-[0-9a-f]{24}$"
    )
    required = verdict_item["required"]
    assert required == [
        "verdict",
        "score",
    ]
    assert "allOf" not in verdict_item
    assert "hit/unsure 必填" in verdict_item["properties"]["note"]["description"]
    verdict_description = verdict_item["properties"]["verdict"]["description"]
    score_description = verdict_item["properties"]["score"]["description"]
    assert "hit=该记录满足任务定义的命中/关注条件" in verdict_description
    assert "unsure=现有证据不足或冲突" in verdict_description
    assert "程序不会把分数解释成风险、置信度或 verdict" in score_description


def test_audit_source_tool_contract_makes_action_parameters_mutually_exclusive():
    spec = build_watch_stream_spec(surface="audit_source")

    assert "pull 只传 watch_id" in spec.description
    assert "绝不能携带 delivery_ref 或 verdicts" in spec.description
    assert "仅 action=pull 可用" in spec.parameters["target_records"]
    assert "仅 action=verdict 可用" in spec.parameters["delivery_ref"]
    assert "本轮已完成的每条记录都要单列" in spec.parameters["verdicts"]


def test_watch_http_binding_schema_matches_location_specific_runtime_contract():
    schema = tool_spec_input_schema(build_watch_stream_spec())

    query = {
        "action": "open",
        "url": "https://example.test/events",
        "http_request": {
            "cursor_binding": {"location": "query", "name": "after", "initial": 0},
            "page_size_binding": {"location": "query", "name": "batch"},
            "secret_bindings": [
                {
                    "location": "header",
                    "name": "Authorization",
                    "secret_ref": "env:AUDIT_TOKEN",
                }
            ],
        },
    }
    body = {
        "action": "open",
        "url": "https://example.test/search",
        "http_request": {
            "method": "POST",
            "json_body": {"page": {}},
            "cursor_binding": {
                "location": "json_body",
                "path": ["page", "cursor"],
                "initial": 0,
            },
            "page_size_binding": {
                "location": "json_body",
                "path": ["page", "size"],
            },
        },
    }

    assert validate_tool_input(query, schema).ok is True
    assert validate_tool_input(body, schema).ok is True
    assert validate_tool_input(
        {
            **query,
            "http_request": {
                "cursor_binding": {
                    "location": "query",
                    "name": "after",
                    "path": ["page", "cursor"],
                }
            },
        },
        schema,
    ).ok is False
    assert validate_tool_input(
        {
            **query,
            "http_request": {"cursor_binding": {"location": "query"}},
        },
        schema,
    ).ok is False


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
    catalog_entries = catalog.split("# Tool Catalog", 1)[1]

    assert "find_files" in catalog_entries
    assert "list_files" not in catalog_entries
    assert "read_file" not in catalog_entries
    assert "示例：" not in catalog
    assert "next_offset=2" in catalog


def test_tool_call_parser_keeps_model_param_name_bundle_unmodified():
    """LLM: wrapped params are malformed current protocol, not a hidden compatibility shape."""
    registry = make_tool_registry(Path.cwd())

    calls = registry.parse_tool_calls(
        '[TOOL_CALL]\n{"tool":"read_file","param_name":{"path":"README.md"}}\n[/TOOL_CALL]'
    )

    assert calls == [{"tool": "read_file", "param_name": {"path": "README.md"}}]


def test_tool_executor_rejects_model_param_name_bundle(tmp_path: Path):
    """LLM: direct execution must not recover literal param_name bundles before dispatch."""
    (tmp_path / "notes.txt").write_text("bundle recovered", encoding="utf-8")
    registry = make_tool_registry(tmp_path)

    result = registry.execute_call({"tool": "read_file", "param_name": {"path": "notes.txt"}})

    assert not result.ok
    assert "bundle recovered" not in result.output


def test_tool_executor_rejects_unknown_parameter_name_without_defaulting_to_workspace(tmp_path: Path):
    """LLM: unknown fields must not be silently ignored by tools with default parameters."""
    registry = make_tool_registry(tmp_path)

    result = registry.execute_call({"tool": "list_files", "parameter_name": {"path": "."}})

    assert not result.ok
    assert result.error_code == "TOOL_INVALID_ARGUMENTS"
    assert "parameter_name" in result.output


def test_tool_call_parser_keeps_orchestration_params_flat():
    """LLM: orchestration tools no longer unwrap a second category bundle."""
    registry = make_tool_registry(Path.cwd())

    calls = registry.parse_tool_calls(
        '[TOOL_CALL]\n{"tool":"inspect_agent_tree","scope":"root_tree"}\n[/TOOL_CALL]'
    )

    assert calls == [{"tool": "inspect_agent_tree", "scope": "root_tree"}]


def test_tool_executor_rejects_model_filesystem_bundle(tmp_path: Path):
    """LLM: filesystem category wrappers are not current tool payload syntax."""
    (tmp_path / "notes.txt").write_text("category bundle recovered", encoding="utf-8")
    registry = make_tool_registry(tmp_path)

    result = registry.execute_call({"tool": "read_file", "filesystem": {"path": "notes.txt"}})

    assert not result.ok
    assert "category bundle recovered" not in result.output


def test_tool_call_parser_keeps_actual_parameter_name_bundle_unmodified():
    registry = make_tool_registry(Path.cwd())

    calls = registry.parse_tool_calls(
        '[TOOL_CALL]\n'
        '{"tool":"run_command","actual_parameter_name":{"command":"echo ok","working_dir":"/tmp"}}\n'
        '[/TOOL_CALL]'
    )

    assert calls == [{"tool": "run_command", "actual_parameter_name": {"command": "echo ok", "working_dir": "/tmp"}}]


def test_tool_executor_rejects_arguments_bundle(tmp_path: Path):
    registry = make_tool_registry(tmp_path)

    result = registry.execute_call(
        {"tool": "write_file", "arguments": {"path": "notes.txt", "content": "wrapped"}}
    )

    assert not result.ok
    assert not (tmp_path / "notes.txt").exists()


def test_tool_call_parser_keeps_json_tool_and_param_aliases_unmodified():
    registry = make_tool_registry(Path.cwd())

    calls = registry.parse_tool_calls(
        '[TOOL_CALL]\n{"tool":"write","file_path":"notes.txt","content":"ok"}\n[/TOOL_CALL]'
    )

    assert calls == [{"tool": "write", "file_path": "notes.txt", "content": "ok"}]


def test_tool_executor_rejects_json_aliases(tmp_path: Path):
    registry = make_tool_registry(tmp_path)

    result = registry.execute_call({"tool": "write", "file_path": "notes.txt", "content": "alias ok"})

    assert not result.ok
    assert not (tmp_path / "notes.txt").exists()


def test_tool_call_parser_repairs_markdown_escapes_inside_json_strings():
    registry = make_tool_registry(Path.cwd())

    calls = registry.parse_tool_calls(
        "[TOOL_CALL]\n"
        r'{"tool":"write_file","path":"output/openclaude\_main.md",'
        r'"content":"# openclaude\_main\nA\|B"}'
        "\n[/TOOL_CALL]"
    )

    assert calls == [
        {
            "tool": "write_file",
            "path": r"output/openclaude\_main.md",
            "content": "# openclaude\\_main\nA\\|B",
        }
    ]


def test_tool_call_parser_keeps_valid_json_escapes_unchanged():
    registry = make_tool_registry(Path.cwd())

    calls = registry.parse_tool_calls(
        '[TOOL_CALL]\n{"tool":"write_file","path":"out.txt","content":"line 1\\nline 2"}\n[/TOOL_CALL]'
    )

    assert calls == [{"tool": "write_file", "path": "out.txt", "content": "line 1\nline 2"}]


def test_tool_call_parser_accepts_inline_closing_marker_after_complete_json():
    registry = make_tool_registry(Path.cwd())

    calls = registry.parse_tool_calls(
        '[TOOL_CALL]\n{"tool":"create_subagents","goal":"分析源码",'
        '"items":[{"goal":"读源码","role":"worker"}]}[/TOOL_CALL]'
    )

    assert calls == [
        {
            "tool": "create_subagents",
            "goal": "分析源码",
            "items": [{"goal": "读源码", "role": "worker"}],
        }
    ]


def test_tool_call_parser_ignores_inline_closing_marker_inside_json_string():
    registry = make_tool_registry(Path.cwd())
    payload = {
        "tool": "write_file",
        "path": "report.md",
        "content": "正文里提到 [/TOOL_CALL] 只是文本",
    }

    calls = registry.parse_tool_calls(
        "[TOOL_CALL]\n"
        f"{json.dumps(payload, ensure_ascii=False)}\n"
        "[/TOOL_CALL]"
    )

    assert calls == [payload]


def test_tool_call_parser_does_not_canonicalize_conflicting_aliases():
    registry = make_tool_registry(Path.cwd())

    calls = registry.parse_tool_calls(
        '[TOOL_CALL]\n{"tool":"read_file","path":"A.md","file_path":"B.md"}\n[/TOOL_CALL]'
    )

    assert calls == [{"tool": "read_file", "path": "A.md", "file_path": "B.md"}]


def test_tool_call_parser_keeps_model_memory_bundle_unmodified():
    """LLM: memory/read_artifact wrappers are not tool gateway payload syntax."""
    registry = make_tool_registry(Path.cwd())

    calls = registry.parse_tool_calls(
        '[TOOL_CALL]\n'
        '{"tool":"read_artifact","memory":{"artifact_ref":"/tmp/out.json","offset":0,"max_chars":4000}}\n'
        '[/TOOL_CALL]'
    )

    assert calls == [{"tool": "read_artifact", "memory": {"artifact_ref": "/tmp/out.json", "offset": 0, "max_chars": 4000}}]


def test_tool_call_parser_recovers_single_extra_trailing_brace():
    registry = make_tool_registry(Path.cwd())

    calls = registry.parse_tool_calls(
        '[TOOL_CALL]\n{"tool":"read_file","path":"README.md"}\n}\n[/TOOL_CALL]'
    )

    assert calls == [{"tool": "read_file", "path": "README.md"}]


def test_tool_call_parser_recovers_extra_trailing_closing_bracket():
    registry = make_tool_registry(Path.cwd())

    calls = registry.parse_tool_calls(
        '[TOOL_CALL]\n'
        '{"tool":"task_progress","action":"update","items":[{"id":"080","status":"done"}]}\n'
        "]\n"
        '[/TOOL_CALL]'
    )

    assert calls == [
        {
            "tool": "task_progress",
            "action": "update",
            "items": [{"id": "080", "status": "done"}],
        }
    ]


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
    assert calls[0]["error_code"] == "TOOL_CALL_UNCLOSED"
    assert calls[0]["previous_write_committed"] is False
    assert result.ok is False
    assert "mode=\"append\"" in result.output


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


def test_tool_call_parser_ignores_subagent_call_alias():
    """LLM: [SUBAGENT_CALL] is not a current executable tool marker."""
    registry = _registry()
    calls = registry.parse_tool_calls(
        '[SUBAGENT_CALL]\n{"tool":"read_file","path":"README.md"}\n[/TOOL_CALL]'
    )

    assert calls == []


def test_tool_call_parser_keeps_xmlish_read_alias_unmodified():
    """LLM: XML-ish transport does not canonicalize tool or parameter names."""
    registry = _registry()
    calls = registry.parse_tool_calls(
        "\n"
        "<function=read>\n"
        "<parameter=file_path>\nREADME.md\n</parameter>\n"
        "</function>\n"
        ""
    )

    assert calls == [{"tool": "read", "file_path": "README.md"}]


def test_tool_call_parser_keeps_xmlish_write_alias_unmodified():
    """LLM: XML-ish transport decodes values but keeps current-protocol names exact."""
    registry = _registry()
    calls = registry.parse_tool_calls(
        '<function name="write">'
        '<parameter name="file_path">notes.txt</parameter>'
        '<parameter name="content">hello &amp; hi</parameter>'
        "</function>"
    )

    assert calls == [{"tool": "write", "file_path": "notes.txt", "content": "hello & hi"}]


def test_tool_call_parser_ignores_tool_like_examples_inside_json_content():
    registry = _registry()
    content = (
        "报告正文里引用协议示例：<tool_call>{JSON}</tool_call>，"
        "也可能引用 [TOOL_CALL] 和 [/TOOL_CALL]，"
        "也可能引用 [WRITE_FILE_RAW path=\"bad.md\"]x[/WRITE_FILE_RAW]，"
        "这些都只是文件内容。"
    )
    payload = {"tool": "write_file", "path": "report.md", "content": content}

    calls = registry.parse_tool_calls(
        "[TOOL_CALL]\n"
        f"{json.dumps(payload, ensure_ascii=False)}\n"
        "[/TOOL_CALL]"
    )

    assert calls == [payload]


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

    assert calls[0] == {"tool": "read", "file_path": "A.md"}
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
