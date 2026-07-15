from __future__ import annotations

import json

from agent_py_agent.agent.tooling.models import ToolExecutionResult

# 防回归：ToolExecutionResult 的通用错误码兜底（models.py __post_init__）。
# 背景：工具失败时若把含 error_code 的 payload json.dumps 进 output，却没把 error_code=
# 传给构造函数，旧实现会兜底成 UNKNOWN_ERROR（retryable=False）误导模型放弃。read_artifact
# 在 native 真机实测中跨模型高频踩中（deepseek 3 次 / minimax 1 次）。修复后从 output JSON
# 兜底提取，并经 error_contract 大小写归一化命中已注册契约。


def _fail(output: str, error_code: str = "") -> ToolExecutionResult:
    return ToolExecutionResult("read_artifact", False, output, error_code=error_code)


def test_error_code_recovered_from_json_output_when_not_passed():
    r = _fail(json.dumps({"ok": False, "error_code": "artifact_not_registered"}))
    assert r.error_code == "ARTIFACT_NOT_REGISTERED"
    assert r.retryable is True


def test_explicit_error_code_takes_precedence_over_output():
    r = _fail(json.dumps({"error_code": "artifact_missing"}), error_code="TOOL_INVALID_ARGUMENTS")
    assert r.error_code == "TOOL_INVALID_ARGUMENTS"


def test_plain_text_output_falls_back_to_unknown():
    r = _fail("artifact 读取预算已达到：run_id=x 最近 60 秒最多读取 N 字符。")
    assert r.error_code == "UNKNOWN_ERROR"


def test_unregistered_output_code_falls_back_to_unknown():
    r = _fail(json.dumps({"error_code": "some_made_up_code_xyz"}))
    assert r.error_code == "UNKNOWN_ERROR"
    assert r.reported_error_code == "SOME_MADE_UP_CODE_XYZ"


def test_ok_result_never_carries_error_code():
    r = ToolExecutionResult("x", True, json.dumps({"error_code": "artifact_missing"}))
    assert r.error_code == ""
    assert r.reported_error_code == ""
    assert r.retryable is False


def test_command_too_long_code_is_registered_with_change_strategy():
    """COMMAND_TOO_LONG 必须是已注册契约(非 UNKNOWN),且动作=change_strategy。

    真实任务回归:run_command 超长命令旧实现报 TOOL_INVALID_ARGUMENTS(误导改参数),
    现改报 COMMAND_TOO_LONG。若忘记在 error_taxonomy 注册,会回落 UNKNOWN_ERROR
    (retryable=False)误导模型放弃,本测试守住这一点。
    """
    r = ToolExecutionResult("run_command", False, "command 过长(2187 字符)，最多 2000 个字符；...", error_code="COMMAND_TOO_LONG")
    assert r.error_code == "COMMAND_TOO_LONG"
    assert r.error_category == "tool"
    assert r.retryable is True
    assert r.recommended_action == "change_strategy"


def test_all_reader_codes_map_to_registered_non_unknown_contracts():
    reader_codes = [
        "missing_artifact_ref",
        "artifact_not_registered",
        "artifact_path_outside_tool_outputs",
        "artifact_missing",
        "artifact_unreadable",
        "invalid_tool_output_artifact",
        "artifact_hash_mismatch",
        "invalid_read_mode",
        "missing_search_query",
    ]
    for code in reader_codes:
        r = _fail(json.dumps({"ok": False, "error_code": code}))
        assert r.error_code != "UNKNOWN_ERROR", f"{code} 仍回落 UNKNOWN_ERROR"
        assert r.error_code == code.upper()
