from __future__ import annotations

from agent_py_agent.agent.tooling.registry_payload_normalize import (
    normalize_tool_payload,
    parse_tool_block_payload,
)


def test_normalize_tool_payload_keeps_shell_bundle_as_explicit_field() -> None:
    payload, error = normalize_tool_payload(
        {
            "tool": "run_command",
            "shell": {
                "command": "mkdir -p outputs/site",
                "working_dir": "/tmp/demo",
                "timeout": 10,
            },
        }
    )

    assert not error
    assert payload == {
        "tool": "run_command",
        "shell": {
            "command": "mkdir -p outputs/site",
            "working_dir": "/tmp/demo",
            "timeout": 10,
        },
    }


def test_normalize_tool_payload_keeps_args_bundle_as_explicit_field() -> None:
    payload, error = normalize_tool_payload(
        {
            "tool": "run_command",
            "args": {
                "command": "mkdir -p outputs/site",
                "working_dir": "/tmp/demo",
            },
        }
    )

    assert not error
    assert payload == {
        "tool": "run_command",
        "args": {
            "command": "mkdir -p outputs/site",
            "working_dir": "/tmp/demo",
        },
    }


def test_normalize_tool_payload_keeps_arguments_json_string_as_explicit_field() -> None:
    payload, error = normalize_tool_payload(
        {
            "tool": "write_file",
            "arguments": '{"file_path":"outputs/report.txt","content":"ok"}',
        }
    )

    assert not error
    assert payload == {
        "tool": "write_file",
        "arguments": '{"file_path":"outputs/report.txt","content":"ok"}',
    }


def test_normalize_tool_payload_does_not_parse_arguments_json_string() -> None:
    payload, error = normalize_tool_payload(
        {
            "tool": "write_file",
            "arguments": '{"path":"outputs/report.txt"',
        }
    )

    assert not error
    assert payload == {"tool": "write_file", "arguments": '{"path":"outputs/report.txt"'}


def test_normalize_tool_payload_keeps_write_file_raw_tool_names_unmodified() -> None:
    upper, upper_error = normalize_tool_payload(
        {"tool": "WRITE_FILE_RAW", "path": "outputs/a.txt", "content": "hello"}
    )
    lower, lower_error = normalize_tool_payload(
        {"tool": "write_file_raw", "path": "outputs/b.txt", "content": "world"}
    )

    assert not upper_error
    assert not lower_error
    assert upper is not None and upper["tool"] == "WRITE_FILE_RAW"
    assert lower is not None and lower["tool"] == "write_file_raw"


def test_parse_tool_block_payload_rejects_json_write_file_raw_trailing_body() -> None:
    payload = parse_tool_block_payload(
        '{"tool":"WRITE_FILE_RAW","path":"outputs/index.html"}\n<html><body>ok</body></html>\n'
    )

    assert payload["tool"] == "__parse_error__"
    assert payload["error_code"] == "TOOL_CALL_JSON_INVALID"


def test_parse_tool_block_payload_rejects_json_write_file_trailing_body() -> None:
    payload = parse_tool_block_payload(
        '{"tool":"write_file","path":"outputs/index.html","content":""}\n<html><body>ok</body></html>\n'
    )

    assert payload["tool"] == "__parse_error__"
    assert payload["error_code"] == "TOOL_CALL_JSON_INVALID"


def test_parse_tool_block_payload_repairs_bounded_arrow_cli_call() -> None:
    payload = parse_tool_block_payload(
        '''{tool => "session_search", args => {
          --query "社区图书馆测试通过冒烟限制"
          --window 10
          --include-archived true
        }}'''
    )

    assert payload == {
        "tool": "session_search",
        "query": "社区图书馆测试通过冒烟限制",
        "window": 10,
        "include_archived": True,
    }


def test_parse_tool_block_payload_repairs_arrow_cli_call_with_empty_args() -> None:
    payload = parse_tool_block_payload('{tool => "list_tools", args => {}}')

    assert payload == {"tool": "list_tools"}


def test_parse_tool_block_payload_rejects_ambiguous_arrow_cli_multiline_value() -> None:
    payload = parse_tool_block_payload(
        '''{tool => "run_command", args => {
          --command "printf "unsafe"
          next line"
        }}'''
    )

    assert payload["tool"] == "__parse_error__"
    assert payload["error_code"] == "TOOL_CALL_JSON_INVALID"


def test_parse_tool_block_payload_rejects_arrow_cli_duplicate_argument() -> None:
    payload = parse_tool_block_payload(
        '''{tool => "read_file", args => {
          --path "one.txt"
          --path "two.txt"
        }}'''
    )

    assert payload["tool"] == "__parse_error__"
    assert payload["error_code"] == "TOOL_CALL_JSON_INVALID"
