from __future__ import annotations

from agent_py_agent.agent.tooling.registry_payload_normalize import (
    normalize_tool_payload,
    parse_tool_block_payload,
)


# LLM: nested shell bundles should normalize into canonical run_command params before execution.
# 函数用途: 验证模型把 run_command 参数包进 shell 对象时，会被展开成 command/working_dir/timeout。
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


# LLM: some providers wrap the real tool params under args, so normalization must unwrap that bundle too.
# 函数用途: 验证 {"tool": "...", "args": {...}} 会被展开成工具可执行的扁平参数，避免 command/path 丢失。
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


# LLM: provider wrappers sometimes serialize arguments as a JSON string; this repairs only the structure.
# 函数用途: 验证 arguments 字符串是 JSON 对象时会被展开，并继续走参数别名归一。
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


# LLM: malformed structured wrapper JSON should become a stable parse error instead of leaking to tools.
# 函数用途: 验证 arguments JSON 字符串损坏时，解析层返回结构化错误而不是执行未知参数。
def test_normalize_tool_payload_rejects_bad_arguments_json_string() -> None:
    payload, error = normalize_tool_payload(
        {
            "tool": "write_file",
            "arguments": '{"path":"outputs/report.txt"',
        }
    )

    assert payload is None
    assert "arguments 参数包 JSON 解析失败" in error


# LLM: uppercase or legacy raw write aliases should collapse to the canonical write_file tool.
# 函数用途: 验证 WRITE_FILE_RAW / write_file_raw 这类漂移工具名会被统一成 write_file。
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


# LLM: raw-body write tool blocks should recover into standard write_file payloads instead of parse-failing.
# 函数用途: 验证 JSON 头后面直接跟正文时，解析层会把正文补进 content 字段。
def test_parse_tool_block_payload_recovers_write_file_raw_trailing_body() -> None:
    payload = parse_tool_block_payload(
        '{"tool":"WRITE_FILE_RAW","path":"outputs/index.html"}\n<html><body>ok</body></html>\n'
    )

    assert payload["tool"] == "write_file"
    assert payload["path"] == "outputs/index.html"
    assert payload["content"] == "<html><body>ok</body></html>\n"
