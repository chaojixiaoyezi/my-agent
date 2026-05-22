"""LLM: Tool gateway contract tests for tolerant aliases and bounded shell output.

函数/模块用途: 验证工具网关把自然别名归一为正式工具参数，并阻止大 shell 输出直接撑爆上下文。
"""

from __future__ import annotations

import sys
from pathlib import Path

from agent_py_agent.agent.tooling.registry import ToolRegistry, ToolRegistryParams


# LLM: _registry keeps gateway contract tests independent from the larger parser suite.
# 函数用途: 创建可配置 shell 输出上限的工具注册表，方便验证网关行为。
def _registry(root: Path, *, shell_output_max_chars: int = 80) -> ToolRegistry:
    return ToolRegistry(
        ToolRegistryParams(
            workspace_root=root,
            max_chars=6000,
            max_entries=200,
            max_matches=50,
            web_max_chars=12000,
            http_timeout=30,
            catalog_limit=20,
            retrieval_limit=3,
            vector_search_enabled=False,
            shell_tool_timeout=30,
            shell_tool_output_max_chars=shell_output_max_chars,
        )
    )


# LLM: Shell aliases should be repaired before execution, not pushed back to the model as unknown tools.
# 函数用途: 模型写 shell/cmd/cwd 时也能执行到 run_command/command/working_dir。
def test_tool_gateway_canonicalizes_shell_aliases(tmp_path: Path):
    result = _registry(tmp_path).execute_call({
        "tool": "shell",
        "cmd": f'{sys.executable} -c "print(123)"',
        "cwd": str(tmp_path),
    })

    assert result.ok
    assert result.tool == "run_command"
    assert "123" in result.output


# LLM: Large shell output must return a bounded preview while preserving total-size facts.
# 函数用途: 命令 stdout 很大时，工具返回截断预览、总字符数和截断标记，避免 live prompt 被大日志淹没。
def test_run_command_output_is_bounded_by_gateway_budget(tmp_path: Path):
    result = _registry(tmp_path, shell_output_max_chars=40).execute_call({
        "tool": "run_command",
        "command": f'{sys.executable} -c "print(\\"A\\" * 200)"',
    })

    assert result.ok
    assert "stdout_truncated=True" in result.output
    assert "stdout_chars=201" in result.output
    assert "A" * 80 not in result.output


# LLM: read_artifact aliases keep artifact refs machine-shaped when models say ref/path/call_id.
# 函数用途: 验证 artifact 读取参数别名会归一成 artifact_ref/max_chars，不让模型小错变成找不到参数。
def test_tool_gateway_canonicalizes_read_artifact_aliases(tmp_path: Path):
    registry = _registry(tmp_path)

    payload = registry.parse_tool_calls(
        '[TOOL_CALL]\n{"tool":"read_artifact","ref":"run-1:2-1","limit":123}\n[/TOOL_CALL]'
    )[0]

    assert payload["artifact_ref"] == "run-1:2-1"
    assert payload["max_chars"] == 123


# LLM: file_write_session raw blocks avoid forcing large HTML through escaped JSON strings.
# 函数用途: 验证结构化 raw content block 会变成 file_write_session.append，不依赖自然语言续写。
def test_tool_gateway_parses_file_write_session_raw_content_block(tmp_path: Path):
    registry = _registry(tmp_path)
    html = "<!doctype html>\n<html><body><h1>ARCA</h1></body></html>"

    calls = registry.parse_tool_calls(
        '[FILE_WRITE_SESSION_APPEND session_id="homepage-1" target_path="out/index.html" chunk_index=0]\n'
        f"{html}\n"
        "[/FILE_WRITE_SESSION_APPEND]"
    )

    assert calls == [
        {
            "tool": "file_write_session",
            "action": "append",
            "session_id": "homepage-1",
            "target_path": "out/index.html",
            "chunk_index": 0,
            "content": html,
        }
    ]
    append = registry.execute_call(calls[0])
    finish = registry.execute_call({
        "tool": "file_write_session",
        "action": "finish",
        "session_id": "homepage-1",
    })
    assert append.ok is True
    assert finish.ok is True
    assert (tmp_path / "out" / "index.html").read_text(encoding="utf-8") == html


# LLM: Malformed raw write markers must feed the repair loop instead of becoming final prose.
# 函数用途: 模型把 FILE_WRITE_SESSION_APPEND 写坏时，网关返回 parse-error，让主循环继续修复。
def test_tool_gateway_reports_malformed_file_write_session_raw_marker(tmp_path: Path):
    registry = _registry(tmp_path)

    calls = registry.parse_tool_calls(
        "准备写文件\n"
        "[FILE_WRITE_SESSION_APPEND]\n"
        '{"tool":"file_write_session","action":"begin","target_path":"out/index.html"}\n'
        "[/TOOL_CALL]"
    )

    assert len(calls) == 1
    assert calls[0]["tool"] == "__parse_error__"
    assert "FILE_WRITE_SESSION_APPEND" in calls[0]["error"]
    result = registry.execute_call(calls[0])
    assert result.ok is False
    assert "请重新输出标准工具调用格式" in result.output


# LLM: Valid raw blocks with missing attrs already have a single structured parse error.
# 函数用途: 确认完整闭合但缺 header 的 raw block 不会被 malformed scanner 重复报错。
def test_tool_gateway_reports_single_error_for_closed_raw_block_missing_attrs(tmp_path: Path):
    registry = _registry(tmp_path)

    calls = registry.parse_tool_calls(
        "[WRITE_FILE_RAW]\n"
        "hello\n"
        "[/WRITE_FILE_RAW]"
    )

    assert len(calls) == 1
    assert calls[0]["tool"] == "__parse_error__"
    assert calls[0]["error"] == "WRITE_FILE_RAW 缺少结构化属性: path"


# LLM: WRITE_FILE_RAW gives single-file deliverables a one-shot structured commit path.
# 函数用途: 验证完整单文件 raw content block 会直接转成 write_file，不再要求模型手工管理 chunk。
def test_tool_gateway_parses_write_file_raw_content_block(tmp_path: Path):
    registry = _registry(tmp_path)
    html = (
        "<!doctype html>\n"
        "<html>\n"
        "<head><meta charset=\"utf-8\"><title>ARCA</title></head>\n"
        "<body><h1>ARCA</h1></body>\n"
        "</html>"
    )

    calls = registry.parse_tool_calls(
        '[WRITE_FILE_RAW path="out/index.html"]\n'
        f"{html}\n"
        "[/WRITE_FILE_RAW]"
    )

    assert calls == [
        {
            "tool": "write_file",
            "path": "out/index.html",
            "content": html,
        }
    ]
    result = registry.execute_call(calls[0])
    assert result.ok is True
    assert (tmp_path / "out" / "index.html").read_text(encoding="utf-8") == html
