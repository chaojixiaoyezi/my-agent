from __future__ import annotations

from agent_py_agent.agent.tooling.registry_payload_normalize import (
    normalize_tool_payload,
    parse_tool_block_payload,
)


def test_normalize_tool_payload_unwraps_shell_bundle_for_run_command() -> None:
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
        "command": "mkdir -p outputs/site",
        "working_dir": "/tmp/demo",
        "timeout": 10,
    }


def test_normalize_tool_payload_unwraps_args_bundle() -> None:
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
        "command": "mkdir -p outputs/site",
        "working_dir": "/tmp/demo",
    }


def test_normalize_tool_payload_unwraps_arguments_json_string() -> None:
    payload, error = normalize_tool_payload(
        {
            "tool": "write_file",
            "arguments": '{"file_path":"outputs/report.txt","content":"ok"}',
        }
    )

    assert not error
    assert payload == {
        "tool": "write_file",
        "path": "outputs/report.txt",
        "content": "ok",
    }


def test_normalize_tool_payload_rejects_bad_arguments_json_string() -> None:
    payload, error = normalize_tool_payload(
        {
            "tool": "write_file",
            "arguments": '{"path":"outputs/report.txt"',
        }
    )

    assert payload is None
    assert "arguments 参数包 JSON 解析失败" in error


def test_normalize_tool_payload_maps_write_file_raw_aliases() -> None:
    upper, upper_error = normalize_tool_payload(
        {"tool": "WRITE_FILE_RAW", "path": "outputs/a.txt", "content": "hello"}
    )
    lower, lower_error = normalize_tool_payload(
        {"tool": "write_file_raw", "path": "outputs/b.txt", "content": "world"}
    )

    assert not upper_error
    assert not lower_error
    assert upper is not None and upper["tool"] == "write_file"
    assert lower is not None and lower["tool"] == "write_file"


def test_parse_tool_block_payload_recovers_write_file_raw_trailing_body() -> None:
    payload = parse_tool_block_payload(
        '{"tool":"WRITE_FILE_RAW","path":"outputs/index.html"}\n<html><body>ok</body></html>\n'
    )

    assert payload["tool"] == "write_file"
    assert payload["path"] == "outputs/index.html"
    assert payload["content"] == "<html><body>ok</body></html>\n"
