"""LLM: parser and compact tool-catalog contract tests live outside the tool-loop flow tests.

函数/模块用途: 验证模型工具调用解析、坏格式重试提示、工具目录示例和参数包兼容，不让主 tool_loop 测试文件继续膨胀。
"""

from __future__ import annotations

from pathlib import Path

from agent_py_agent.agent.tools import ToolRegistry, ToolRegistryParams, ToolSpec

from .backends import make_tool_registry


# LLM: _registry builds a catalog/parser registry with stable test defaults.
# 函数用途: 生成测试用 ToolRegistry，避免每个解析器用例重复一大段配置。
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
    检查工具目录里有 http_request，推荐工具区能根据自然语言选到 http_request。
    """
    registry = _registry()

    catalog = registry.render_catalog_section()
    recommended = registry.render_recommended_tools_section("帮我测试一个 REST API 接口并查看返回")

    assert "# Tool Catalog" in catalog
    assert "http_request [api]" in catalog
    assert "适用场景" in catalog
    assert "## http_request" in recommended
    assert "推荐理由" in recommended


# LLM: ToolRegistry treats configured inline write limits as transport advice, not a write blocker.
# 函数用途: 验证注册表里的 write_file 会使用用户配置的推荐值提示模型，但合法内容仍然先写入文件。
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


# LLM: Shell timeout config should reach the actual run_command tool, not stop at AgentConfig.
# 函数用途: 验证工具注册表把 tool_shell_timeout 传给 run_command 的默认超时和工具说明。
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


# LLM: long content protocol must be visible before compact catalog entries can hide write-file details.
# 函数用途: 验证工具目录顶部始终提示大 HTML/JS/报告要分块写入，避免真实模型先走超大 write_file。
def test_tool_catalog_includes_global_large_content_protocol():
    registry = _registry()

    catalog = registry.render_catalog_section()

    assert "# Tool Content Transport Protocol" in catalog
    assert "不要把完整大文件正文塞进一个 JSON 工具参数" in catalog
    assert "write_file 写短骨架" in catalog
    assert "append_file 分块追加" in catalog


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

    assert "read_file" in catalog
    assert "list_files" not in catalog
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


def test_tool_call_parser_unwraps_model_category_bundle():
    """LLM: tolerate models that wrap params by tool category such as orchestration or filesystem."""
    registry = make_tool_registry(Path.cwd())

    calls = registry.parse_tool_calls(
        '[TOOL_CALL]\n{"tool":"subagent_board","orchestration":{"limit":20,"status":"running"}}\n[/TOOL_CALL]'
    )

    assert calls == [{"tool": "subagent_board", "limit": 20, "status": "running"}]


def test_tool_executor_unwraps_model_filesystem_bundle(tmp_path: Path):
    """LLM: filesystem category wrappers should be flattened before file tool execution."""
    (tmp_path / "notes.txt").write_text("category bundle recovered", encoding="utf-8")
    registry = make_tool_registry(tmp_path)

    result = registry.execute_call({"tool": "read_file", "filesystem": {"path": "notes.txt"}})

    assert result.ok
    assert "category bundle recovered" in result.output


# LLM: test_tool_call_parser_canonicalizes_json_tool_and_param_aliases covers non-XML model drift.
# 函数用途: 标准 JSON 工具块里写 write/file_path 这类别名时，协议层应归一成 write_file/path。
def test_tool_call_parser_canonicalizes_json_tool_and_param_aliases():
    registry = make_tool_registry(Path.cwd())

    calls = registry.parse_tool_calls(
        '[TOOL_CALL]\n{"tool":"write","file_path":"notes.txt","content":"ok"}\n[/TOOL_CALL]'
    )

    assert calls == [{"tool": "write_file", "path": "notes.txt", "content": "ok"}]


# LLM: test_tool_executor_canonicalizes_json_aliases keeps direct envelope execution equally robust.
# 函数用途: 直接执行旧 dict/envelope payload 时也要归一工具名和路径别名，不能只修 parser。
def test_tool_executor_canonicalizes_json_aliases(tmp_path: Path):
    registry = make_tool_registry(tmp_path)

    result = registry.execute_call({"tool": "write", "file_path": "notes.txt", "content": "alias ok"})

    assert result.ok
    assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "alias ok"


# LLM: test_tool_call_parser_rejects_conflicting_canonical_aliases avoids silent path swaps.
# 函数用途: 如果 path 和 file_path 同时出现且不同，必须明确报错，不能猜哪个是真的。
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


# LLM: test_tool_call_parser_recovers_single_extra_trailing_brace covers real MiniMax tool-call drift.
# 函数用途: 模型在有效 JSON 后多吐一个 `}` 时，解析器应保留完整工具参数而不是逼模型缩短任务。
def test_tool_call_parser_recovers_single_extra_trailing_brace():
    registry = make_tool_registry(Path.cwd())

    calls = registry.parse_tool_calls(
        '[TOOL_CALL]\n{"tool":"read_file","path":"README.md"}\n}\n[/TOOL_CALL]'
    )

    assert calls == [{"tool": "read_file", "path": "README.md"}]


# LLM: test_parse_error_result_includes_retry_format_hint covers malformed XML-ish tool-call recovery.
# 函数用途: 模型工具调用格式坏掉时，执行结果要明确告诉它下一轮用标准 JSON 工具块重试。
def test_parse_error_result_includes_retry_format_hint():
    registry = make_tool_registry(Path.cwd())
    calls = registry.parse_tool_calls("<tool_call><function=read><parameter=file_path>README.md</parameter>")

    result = registry.execute_call(calls[0])

    assert result.ok is False
    assert result.tool == "__parse_error__"
    assert "[TOOL_CALL]" in result.output
    assert "[/TOOL_CALL]" in result.output


# LLM: test_tool_call_parser_reports_missing_closing_tool_marker covers R25 truncated JSON.
# 函数用途: 模型开始写标准工具块但没闭合时，运行循环要让它重试，而不是把半截工具调用当最终回答。
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
        '  "status": "AWAITING_ACCEPTANCE",\n'
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


# LLM: complete JSON without the closing marker should still execute when the payload is intact.
# 函数用途: 真实模型偶尔少写 [/TOOL_CALL]，但 JSON 已完整；这种情况不应浪费一轮重试。
def test_tool_call_parser_recovers_complete_json_without_closing_marker():
    registry = make_tool_registry(Path.cwd())
    calls = registry.parse_tool_calls(
        '[TOOL_CALL]\n{"tool":"write_file","path":"index.html","content":"<main>ok</main>"}'
    )

    assert calls == [
        {"tool": "write_file", "path": "index.html", "content": "<main>ok</main>"}
    ]


# LLM: test_parse_error_hint_recommends_append_for_truncated_write covers long generated CSS/HTML writes.
# 函数用途: 写文件内容太长被截断时，错误提示要引导模型用 append_file 分块写，避免重复失败。
def test_parse_error_hint_recommends_append_for_truncated_write():
    registry = make_tool_registry(Path.cwd())
    calls = registry.parse_tool_calls(
        '[TOOL_CALL]\n{"tool":"write_file","path":"style.css","content":"body { color: red;'
    )

    result = registry.execute_call(calls[0])

    assert calls[0]["tool"] == "__parse_error__"
    assert result.ok is False
    assert "append_file 分块追加内容" in result.output
    assert "1500-2000 字符" in result.output
    assert "不超过 800 字符" in result.output
    assert "只能输出 1 个 write_file/append_file" in result.output


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
        examples=['{"tool":"schedule_child_subagents","apply":true,"children":[]}'],
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
