from __future__ import annotations

from pathlib import Path

# 防回归：核心工具直接声明唯一精确 ToolModelSpec.input_schema。
# provider 与运行时读取同一份快照，不再从旧参数描述字段推导第二套结构。


def test_search_text_numeric_and_bool_params_precise():
    from agent_py_agent.agent.tooling._filesystem_search import build_search_text_model_spec

    spec = build_search_text_model_spec()
    p = spec.input_schema["properties"]
    assert p["limit"]["type"] == "integer"
    assert p["offset"]["type"] == "integer"
    assert p["context"]["type"] == "integer"
    assert p["literal"]["type"] == "boolean"
    assert p["literal"]["default"] is False
    assert p["ignore_case"]["type"] == "boolean"
    assert p["include_ignored"]["type"] == "boolean"
    assert "只在当前本地工作区" in spec.description
    assert "不联网" in spec.description
    assert "不做语义搜索" in spec.description
    assert "ripgrep 正则语义" in p["query"]["description"]


def test_run_command_timeout_and_background_precise():
    from agent_py_agent.agent.tooling.shell import _build_shell_tool_model_spec

    schema = _build_shell_tool_model_spec("normal", 120, 4000).input_schema
    p = schema["properties"]
    assert p["timeout"]["type"] == "integer"
    assert p["run_in_background"]["type"] == "boolean"
    assert p["command"]["type"] == "string"
    assert schema["required"] == ["command"]


def test_read_file_line_and_char_params_integer():
    from agent_py_agent.agent.tooling._filesystem_read import ReadFileTool

    schema = ReadFileTool(Path("/tmp"), 100000).model_spec.input_schema
    p = schema["properties"]
    for key in ("start_line", "end_line", "offset", "max_chars"):
        assert p[key]["type"] == "integer", key
    assert p["path"]["type"] == "string"
    assert schema["required"] == ["path"]


def test_write_file_mode_is_enum():
    from agent_py_agent.agent.tooling._filesystem_write import WriteFileTool

    schema = WriteFileTool(Path("/tmp")).model_spec.input_schema
    assert schema["properties"]["mode"]["enum"] == ["overwrite", "append"]
    assert schema["required"] == ["path"]


def test_list_files_numeric_and_bool_params_precise():
    from agent_py_agent.agent.tooling._filesystem_list import build_list_files_model_spec

    p = build_list_files_model_spec().input_schema["properties"]
    assert p["recursive"]["type"] == "boolean"
    assert p["limit"]["type"] == "integer"
    assert p["max_depth"]["type"] == "integer"
    assert p["include_ignored"]["type"] == "boolean"


def test_find_files_params_precise_and_required_pattern():
    from agent_py_agent.agent.tooling._filesystem_find import FindFilesTool

    schema = FindFilesTool(Path("/tmp"), 100).model_spec.input_schema
    p = schema["properties"]
    assert p["limit"]["type"] == "integer"
    assert p["offset"]["type"] == "integer"
    assert p["include_ignored"]["type"] == "boolean"
    assert schema["required"] == ["pattern"]


def test_controlled_exec_apply_boolean_command_stays_string():
    from agent_py_agent.agent.tooling.controlled_exec import ControlledExecTool

    p = ControlledExecTool.model_spec.input_schema["properties"]
    assert p["apply"]["type"] == "boolean"
    assert p["command"]["anyOf"] == [
        {"type": "string"},
        {"type": "array", "items": {"type": "string"}, "minItems": 1},
    ]
