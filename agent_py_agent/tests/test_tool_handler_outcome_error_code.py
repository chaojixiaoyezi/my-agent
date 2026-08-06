from __future__ import annotations

import json

from agent_py_agent.agent.tooling.models import ToolHandlerOutcome

# 防回归：ToolHandlerOutcome 只信宿主显式字段，不从不可信 output JSON 提升控制事实。
# 工具实现必须通过 error_code= 报码；正文即使长得像 JSON，也只能留在业务输出里。


def _fail(output: str, error_code: str = "") -> ToolHandlerOutcome:
    return ToolHandlerOutcome("read_artifact", False, output, error_code=error_code)


def test_error_code_is_not_recovered_from_untrusted_json_output():
    r = _fail(json.dumps({"ok": False, "error_code": "artifact_not_registered"}))
    assert r.error_code == "UNKNOWN_ERROR"
    assert r.reported_error_code == "UNKNOWN_ERROR"
    assert r.retryable is False


def test_explicit_error_code_takes_precedence_over_output():
    r = _fail(json.dumps({"error_code": "artifact_missing"}), error_code="TOOL_INVALID_ARGUMENTS")
    assert r.error_code == "TOOL_INVALID_ARGUMENTS"


def test_control_and_reported_error_codes_remain_distinct():
    r = ToolHandlerOutcome(
        "raise_collaboration",
        False,
        json.dumps({"error": "thread_required"}),
        error_code="TOOL_PARAMETER_REQUIRED",
        reported_error_code="THREAD_REQUIRED",
    )

    assert r.error_code == "TOOL_PARAMETER_REQUIRED"
    assert r.reported_error_code == "THREAD_REQUIRED"
    assert r.retryable is True


def test_plain_text_output_falls_back_to_unknown():
    r = _fail("artifact 读取预算已达到：run_id=x 最近 60 秒最多读取 N 字符。")
    assert r.error_code == "UNKNOWN_ERROR"


def test_unregistered_output_code_falls_back_to_unknown():
    r = _fail(json.dumps({"error_code": "some_made_up_code_xyz"}))
    assert r.error_code == "UNKNOWN_ERROR"
    assert r.reported_error_code == "UNKNOWN_ERROR"


def test_ok_result_never_carries_error_code():
    r = ToolHandlerOutcome("x", True, json.dumps({"error_code": "artifact_missing"}))
    assert r.error_code == ""
    assert r.reported_error_code == ""
    assert r.retryable is False


def test_command_too_long_code_is_registered_with_change_strategy():
    """COMMAND_TOO_LONG 必须是已注册契约(非 UNKNOWN),且动作=change_strategy。

    真实任务回归:run_command 超长命令旧实现报 TOOL_INVALID_ARGUMENTS(误导改参数),
    现改报 COMMAND_TOO_LONG。若忘记在 error_taxonomy 注册,会回落 UNKNOWN_ERROR
    (retryable=False)误导模型放弃,本测试守住这一点。
    """
    r = ToolHandlerOutcome("run_command", False, "command 过长(2187 字符)，最多 2000 个字符；...", error_code="COMMAND_TOO_LONG")
    assert r.error_code == "COMMAND_TOO_LONG"
    assert r.error_category == "tool"
    assert r.retryable is True
    assert r.recommended_action == "change_strategy"


def test_command_parse_failure_keeps_exact_repairable_contract():
    """命令引号未闭合等解析错误必须引导重建调用，不能降级为 UNKNOWN_ERROR。"""
    r = ToolHandlerOutcome(
        "run_command",
        False,
        "runtime gate denied: findings=COMMAND_PARSE_FAILED; No closing quotation",
        error_code="COMMAND_PARSE_FAILED",
    )

    assert r.error_code == "COMMAND_PARSE_FAILED"
    assert r.reported_error_code == "COMMAND_PARSE_FAILED"
    assert r.error_category == "tool"
    assert r.retryable is True
    assert r.recommended_action == "repair_tool_call"


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
        r = _fail(json.dumps({"ok": False, "error_code": code}), error_code=code)
        assert r.error_code != "UNKNOWN_ERROR", f"{code} 仍回落 UNKNOWN_ERROR"
        assert r.error_code == code.upper()
