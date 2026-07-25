from __future__ import annotations

from agent_py_agent.agent.backends.tool_schema import tool_spec_to_input_schema
from agent_py_agent.agent.tooling.models import ToolSpec

# 防回归：ToolSpec.parameter_schema / required_parameters 的精确 schema 通道。
# 背景：native tool_use 弱推导把每个参数都当 string，导致 web_fetch（urls=array/mode=enum/
# headers=object/max_chars=integer）被模型传错 → TOOL_INVALID_ARGUMENTS（M3 真机 3 次）。
# parameter_schema 声明精确类型后真机降到 0；未声明的工具回退弱推导（零破坏）。


def _spec(parameters, parameter_schema=None, required=None, parameter_details=None):
    return ToolSpec(
        name="t", category="c", description="d",
        use_cases=[], avoid_when=[], keywords=[],
        parameters=parameters,
        parameter_details=parameter_details or {},
        parameter_schema=parameter_schema or {},
        required_parameters=required or [],
    )


def test_declared_parameter_schema_overrides_weak_string():
    spec = _spec(
        {"urls": "URL 数组", "mode": "读取模式"},
        parameter_schema={
            "urls": {"type": "array", "items": {"type": "string"}},
            "mode": {"type": "string", "enum": ["auto", "extract"]},
        },
    )
    props = tool_spec_to_input_schema(spec)["properties"]
    assert props["urls"]["type"] == "array"
    assert props["urls"]["items"] == {"type": "string"}
    assert props["mode"]["enum"] == ["auto", "extract"]
    assert props["urls"]["description"] == "URL 数组"  # 中文描述自动补上


def test_undeclared_params_fall_back_to_string():
    spec = _spec({"a": "甲", "b": "乙"}, parameter_schema={"a": {"type": "integer"}})
    props = tool_spec_to_input_schema(spec)["properties"]
    assert props["a"] == {"type": "integer", "description": "甲"}
    assert props["b"] == {"type": "string", "description": "乙"}


def test_required_parameters_mapped_and_filtered():
    spec = _spec({"x": "必填", "y": "选填"}, required=["x", "ghost"])
    schema = tool_spec_to_input_schema(spec)
    assert schema["required"] == ["x"]  # 不存在的 ghost 被过滤


def test_no_schema_uses_closed_legacy_derivation():
    spec = _spec({"path": "路径"})
    schema = tool_spec_to_input_schema(spec)
    assert schema == {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "路径"}},
        "additionalProperties": False,
    }
    assert "required" not in schema


def test_explicit_schema_description_not_overwritten():
    spec = _spec({"u": "默认描述"}, parameter_schema={"u": {"type": "string", "description": "自定义"}})
    assert tool_spec_to_input_schema(spec)["properties"]["u"]["description"] == "自定义"


def test_parameter_details_are_the_native_schema_description():
    spec = _spec(
        {"patch": "简短目录说明"},
        parameter_schema={"patch": {"type": "string"}},
        parameter_details={"patch": "完整语法说明"},
    )

    assert tool_spec_to_input_schema(spec)["properties"]["patch"]["description"] == "完整语法说明"
