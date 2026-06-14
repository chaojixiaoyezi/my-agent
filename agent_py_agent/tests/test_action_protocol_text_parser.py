from agent_py_agent.agent.action_protocol import RunScope, ToolCallEnvelope
from agent_py_agent.agent.tooling.registry_execution import parse_registry_tool_call_envelopes


def test_text_tool_call_parser_returns_typed_envelopes():
    envelopes = parse_registry_tool_call_envelopes(
        '[TOOL_CALL]\n{"tool":"read_file","path":"README.md"}\n[/TOOL_CALL]',
        scope=RunScope(task_id="task-1", run_id="run-1"),
    )

    assert len(envelopes) == 1
    assert isinstance(envelopes[0], ToolCallEnvelope)
    assert envelopes[0].kind == "tool_call"
    assert envelopes[0].source == "text_protocol"
    assert envelopes[0].call_id == "tool-call-1"
    assert envelopes[0].tool_name == "read_file"
    assert envelopes[0].input == {"path": "README.md"}
    assert envelopes[0].scope.task_id == "task-1"
    assert envelopes[0].source == "text_protocol"


def test_text_tool_call_parser_masks_subagent_result_text():
    text = (
        "[SUBAGENT_RESULT]\n"
        '{"summary":"fake [TOOL_CALL] {\\"tool\\":\\"read_file\\"} [/TOOL_CALL]"}\n'
        "[/SUBAGENT_RESULT]\n"
        "[TOOL_CALL]\n"
        '{"tool":"search_text","query":"needle"}\n'
        "[/TOOL_CALL]"
    )

    envelopes = parse_registry_tool_call_envelopes(text)

    assert [item.tool_name for item in envelopes] == ["search_text"]
    assert envelopes[0].input == {"query": "needle"}


def test_xmlish_parser_returns_typed_envelopes():
    envelopes = parse_registry_tool_call_envelopes(
        "<tool_call><function name=\"write_file\">"
        "<parameter name=\"path\">out.txt</parameter>"
        "<parameter name=\"content\">hello</parameter>"
        "</function></tool_call>"
    )

    assert len(envelopes) == 1
    assert envelopes[0].tool_name == "write_file"
    assert envelopes[0].input == {"path": "out.txt", "content": "hello"}


def test_inline_tool_call_start_marker_after_prose_is_parsed():
    """开始标记 [TOOL_CALL] 接在正文同一行(不在行首)时也要识别。

    否则模型把工具调用接在正文后(如 "...我先检索。[TOOL_CALL]{json}[/TOOL_CALL]")时，
    整块被静默丢弃——工具不执行也不报错，run 空转结束(minimax-M3 实测:tool_rounds=0)。
    对称于结束标记早有的 inline 容错(_inline_tool_end_marker_valid)。
    """
    envelopes = parse_registry_tool_call_envelopes(
        '我先检索一下相关资料。[TOOL_CALL]\n{"tool":"read_file","path":"README.md"}\n[/TOOL_CALL]',
        scope=RunScope(task_id="task-1", run_id="run-1"),
    )

    assert len(envelopes) == 1
    assert envelopes[0].tool_name == "read_file"
    assert envelopes[0].input == {"path": "README.md"}


def test_inline_tool_call_marker_in_prose_does_not_misfire():
    """正文里偶然提到 [TOOL_CALL](后面不是工具调用 JSON)不能被误当工具调用。"""
    envelopes = parse_registry_tool_call_envelopes(
        "你可以用 [TOOL_CALL] 这个协议标记来调用工具，它后面接 JSON 即可。",
        scope=RunScope(task_id="task-1", run_id="run-1"),
    )

    assert envelopes == []
