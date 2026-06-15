from __future__ import annotations

from agent_py_agent.agent.backends.tool_schema import (
    tool_spec_to_anthropic_tool,
    tool_spec_to_input_schema,
    tool_specs_to_anthropic_tools,
)
from agent_py_agent.agent.tooling.models import ToolSpec


def _spec(name: str, parameters: dict[str, str], description: str = "") -> ToolSpec:
    return ToolSpec(
        name=name,
        category="test",
        description=description or f"{name} 工具",
        use_cases=[],
        avoid_when=[],
        keywords=[],
        parameters=parameters,
    )


def test_input_schema_has_object_root_and_string_props():
    spec = _spec("read_file", {"path": "要读取的文件路径", "limit": "可选，最多读取行数"})

    schema = tool_spec_to_input_schema(spec)

    assert schema["type"] == "object"
    assert schema["properties"]["path"] == {"type": "string", "description": "要读取的文件路径"}
    assert schema["properties"]["limit"] == {"type": "string", "description": "可选，最多读取行数"}
    # required is intentionally omitted (conservative: do not over-enforce).
    assert "required" not in schema


def test_input_schema_empty_parameters_yields_empty_properties():
    schema = tool_spec_to_input_schema(_spec("noop", {}))

    assert schema == {"type": "object", "properties": {}}


def test_input_schema_coerces_non_string_description_to_string():
    spec = _spec("weird", {"flag": None})  # type: ignore[arg-type]

    schema = tool_spec_to_input_schema(spec)

    assert schema["properties"]["flag"] == {"type": "string", "description": ""}


def test_anthropic_tool_carries_name_description_and_schema():
    spec = _spec(
        "web_fetch",
        {"url": "完整 URL", "mode": "auto/markdown/text"},
        description="读取 URL 或调用 HTTP",
    )

    tool = tool_spec_to_anthropic_tool(spec)

    assert tool["name"] == "web_fetch"
    assert tool["description"] == "读取 URL 或调用 HTTP"
    assert tool["input_schema"]["properties"]["url"]["type"] == "string"
    assert tool["input_schema"]["properties"]["mode"]["description"] == "auto/markdown/text"


def test_tools_array_for_real_filesystem_tools_is_well_formed():
    specs = [
        _spec("read_file", {"path": "文件路径", "offset": "起始行", "limit": "行数"}),
        _spec("write_file", {"path": "目标路径", "content": "写入内容"}),
        _spec("web_fetch", {"url": "完整 URL", "urls": "URL 数组", "mode": "抽取模式"}),
    ]

    tools = tool_specs_to_anthropic_tools(specs)

    assert [t["name"] for t in tools] == ["read_file", "write_file", "web_fetch"]
    for tool in tools:
        assert tool["input_schema"]["type"] == "object"
        assert isinstance(tool["input_schema"]["properties"], dict)
        assert all(
            prop == {"type": "string", "description": prop["description"]}
            for prop in tool["input_schema"]["properties"].values()
        )


def test_tools_array_drops_duplicate_names_keeping_first():
    specs = [
        _spec("read_file", {"path": "first"}),
        _spec("read_file", {"path": "second"}),
    ]

    tools = tool_specs_to_anthropic_tools(specs)

    assert len(tools) == 1
    assert tools[0]["input_schema"]["properties"]["path"]["description"] == "first"
