from __future__ import annotations

from pathlib import Path

from agent_py_agent.agent.backends.tool_schema import tool_spec_to_input_schema

# 防回归：核心工具的精确 input_schema（schema 长尾）。
# 给 read_file/write_file/search_text/run_command 声明了 parameter_schema 精确类型后，
# native tool_use 不再把 start_line/limit/timeout 当 string、mode 当自由字符串，从源头减少
# 跨工具的 TOOL_INVALID_ARGUMENTS。string 参数仍自动回退（零破坏）。


def _props(spec):
    return tool_spec_to_input_schema(spec)["properties"]


def test_search_text_numeric_and_bool_params_precise():
    from agent_py_agent.agent.tooling._filesystem_search import build_search_text_spec

    p = _props(build_search_text_spec())
    assert p["limit"]["type"] == "integer"
    assert p["offset"]["type"] == "integer"
    assert p["context"]["type"] == "integer"
    assert p["literal"]["type"] == "boolean"
    assert p["ignore_case"]["type"] == "boolean"
    assert p["include_ignored"]["type"] == "boolean"


def test_run_command_timeout_and_background_precise():
    from agent_py_agent.agent.tooling.shell import _build_shell_tool_spec

    schema = tool_spec_to_input_schema(_build_shell_tool_spec("normal", 120, 4000))
    p = schema["properties"]
    assert p["timeout"]["type"] == "integer"
    assert p["run_in_background"]["type"] == "boolean"
    assert p["command"]["type"] == "string"  # 未声明的 string 参数仍回退
    assert schema["required"] == ["command"]


def test_read_file_line_and_char_params_integer():
    from agent_py_agent.agent.tooling._filesystem_read import ReadFileTool

    schema = tool_spec_to_input_schema(ReadFileTool(Path("/tmp"), 100000).spec)
    p = schema["properties"]
    for key in ("start_line", "end_line", "offset", "max_chars"):
        assert p[key]["type"] == "integer", key
    assert p["path"]["type"] == "string"
    assert schema["required"] == ["path"]


def test_write_file_mode_is_enum():
    from agent_py_agent.agent.tooling._filesystem_write import WriteFileTool

    schema = tool_spec_to_input_schema(WriteFileTool(Path("/tmp")).spec)
    assert schema["properties"]["mode"]["enum"] == ["overwrite", "append"]
    assert schema["required"] == ["path"]


def test_list_files_numeric_and_bool_params_precise():
    from agent_py_agent.agent.tooling._filesystem_list import build_list_files_spec

    p = _props(build_list_files_spec())
    assert p["recursive"]["type"] == "boolean"
    assert p["limit"]["type"] == "integer"
    assert p["max_depth"]["type"] == "integer"
    assert p["include_ignored"]["type"] == "boolean"


def test_find_files_params_precise_and_required_pattern():
    from agent_py_agent.agent.tooling._filesystem_find import FindFilesTool

    schema = tool_spec_to_input_schema(FindFilesTool(Path("/tmp"), 100).spec)
    p = schema["properties"]
    assert p["limit"]["type"] == "integer"
    assert p["offset"]["type"] == "integer"
    assert p["include_ignored"]["type"] == "boolean"
    assert schema["required"] == ["pattern"]


def test_controlled_exec_apply_boolean_command_stays_string():
    from agent_py_agent.agent.tooling.controlled_exec import ControlledExecTool

    p = _props(ControlledExecTool.spec)
    assert p["apply"]["type"] == "boolean"
    # command 可接受 string 或 argv 数组，保守保留 string 回退（不误拦数组用法）
    assert p["command"]["type"] == "string"
