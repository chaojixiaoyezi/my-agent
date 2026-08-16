"""Compact tool-catalog and model-visible schema contract tests."""

from __future__ import annotations

from pathlib import Path

from agent_py_agent.agent.contracts.tool_input_schema import validate_tool_input
from agent_py_agent.agent.ingestion.watch_tool_spec import build_watch_stream_model_spec
from agent_py_agent.agent.tooling.registry import ToolRegistry, ToolRegistryParams
from agent_py_agent.tests._tool_runtime_harness import (
    execute_registry_test_call,
    make_test_model_spec,
)


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

    # native 协议(EXEC-31b text 已删): 目录不展开条目, Schema 走原生工具通道
    assert "# Tool Catalog" in catalog
    assert "原生工具通道" in catalog
    assert "http_request [api]" not in catalog
    assert "web_extract [web]" not in catalog
    assert "关键参数" not in catalog
    assert "适用场景" not in catalog
    assert "示例：" not in catalog
    assert "web_fetch" in recommended
    assert "命中" in recommended


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

    result = execute_registry_test_call(
        registry,
        "write_file",
        {"path": "site/app.js", "content": "A" * 513},
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
    assert "240" in tool.model_spec.parameter_descriptions["timeout"]


def test_tool_catalog_format_example_does_not_bias_to_path_param():
    """LLM: Tool call instructions should not teach all tools to pass a fake path parameter."""
    registry = _registry()

    catalog = registry.render_catalog_section()

    # native 协议: 不教模型写 [TOOL_CALL] 文本块, 也不给文本参数示例
    assert '{"tool": "read_file", "path": "README.md"}' not in catalog
    assert "[TOOL_CALL]" not in catalog
    assert '"param_name": "param_value"' not in catalog
    assert "原生工具调用" in catalog


def test_tool_catalog_includes_global_large_content_protocol():
    registry = _registry()

    catalog = registry.render_catalog_section()

    assert "# Tool Content Transport Protocol" in catalog
    assert "不要把完整大文件正文塞进一个 JSON 工具参数" in catalog
    assert "write_file" in catalog
    assert "apply_patch" in catalog
    assert 'mode="append"' in catalog
    assert "WRITE_FILE_RAW" not in catalog


def test_read_only_tool_catalog_omits_irrelevant_write_transport_protocol():
    registry = _registry()

    catalog = registry.render_catalog_section(allowed_tools=["read_file"])

    # native 协议: 条目不展开(工具名不在目录文本), 只读场景不注入写传输协议
    assert "原生工具通道" in catalog
    assert "# Tool Content Transport Protocol" not in catalog
    assert "WRITE_FILE_RAW" not in catalog


def test_watch_verdict_schema_requires_explicit_per_record_results():
    spec = build_watch_stream_model_spec()
    parameters = spec.input_schema["properties"]

    assert "minItems" not in parameters["verdicts"]
    assert "verdict_default" not in parameters
    assert "verdict_batch" not in parameters
    assert parameters["delivery_ref"]["pattern"] == "^ad-[0-9a-f]{24}$"
    verdict_item = parameters["verdicts"]["items"]
    assert verdict_item["properties"]["verdict_token"]["pattern"] == ("^vt-[0-9a-f]{24}$")
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
    spec = build_watch_stream_model_spec(surface="audit_source")

    assert "pull 只传 watch_id" in spec.description
    assert "绝不能携带 delivery_ref 或 verdicts" in spec.description
    assert "仅 action=pull 可用" in spec.parameter_descriptions["target_records"]
    assert "仅 action=verdict 可用" in spec.parameter_descriptions["delivery_ref"]
    assert "本轮已完成的每条记录都要单列" in spec.parameter_descriptions["verdicts"]


def test_watch_http_binding_schema_matches_location_specific_runtime_contract():
    schema = build_watch_stream_model_spec().input_schema

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
    assert (
        validate_tool_input(
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
        ).ok
        is False
    )
    assert (
        validate_tool_input(
            {
                **query,
                "http_request": {"cursor_binding": {"location": "query"}},
            },
            schema,
        ).ok
        is False
    )


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

    # native 协议: 目录不展开条目也不分页(offset 只作用于 text 展开, 已随 text 删除)
    assert "find_files" not in catalog_entries
    assert "list_files" not in catalog_entries
    assert "read_file" not in catalog_entries
    assert "示例：" not in catalog
    assert "next_offset=2" not in catalog
    assert "原生工具通道" in catalog


def test_tool_model_spec_catalog_entry_includes_first_example():
    """LLM: Compact catalog entries should show tool-specific JSON when examples are available."""
    spec = make_test_model_spec(
        "schedule_child_subagents",
        category="orchestration",
        description="create child runs",
        use_cases=("split hierarchy",),
        input_schema={
            "type": "object",
            "properties": {
                "children": {"type": "array", "description": "child specs"},
                "apply": {"type": "boolean", "description": "write"},
            },
            "additionalProperties": False,
        },
        examples=('{"tool":"schedule_child_subagents","dry_run":false,"children":[]}',),
    )

    entry = spec.render_catalog_entry()

    assert "示例" in entry
    assert '"children":[]' in entry
