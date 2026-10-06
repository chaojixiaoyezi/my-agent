# LLM: cmdcap 用例：顶层命令源长度闸门——模块常量上限（3a 裁定去掉配置项）、解析前拒绝、
#   三个入口（evaluate/analyze/受控执行网关）超限时绝不调用 shlex；子代理文本解析点共用同一判定。
# 模块用途: 钉住 command_source_too_large 与各入口的命令源长度上限行为。

from __future__ import annotations

import shlex
import time

import pytest

from agent_py_agent.agent.contracts.error_taxonomy import ERROR_CONTRACTS
from agent_py_agent.agent.contracts.gates.command_policy import (
    analyze_command,
    command_source_too_large,
    evaluate_command_policy,
)

LIMIT = 65_536


# 函数用途: 断言超限时绝不做 shlex 解析；被调用即让用例失败。
def _fail_if_called(*_args, **_kwargs):
    raise AssertionError("超限输入不应进入 shlex 解析")


# 函数用途: 上限是常量：恰好等于上限放行到解析；上限加 1 拒绝。
def test_boundary_exactly_at_limit_passes_and_plus_one_rejected() -> None:
    assert command_source_too_large("x" * LIMIT) is None
    assert evaluate_command_policy("x" * LIMIT).allowed is True
    finding = command_source_too_large("x" * (LIMIT + 1))
    assert finding is not None and finding.code == "COMMAND_SOURCE_TOO_LARGE"
    assert evaluate_command_policy("x" * (LIMIT + 1)).allowed is False


# 函数用途: 超限命令在 shlex 解析前被拒，且耗时远小于解析 1MB 所需的十几秒。
def test_oversized_source_rejected_before_parse_under_50ms() -> None:
    big = "echo " + "x" * 1_000_000
    start = time.perf_counter()
    decision = evaluate_command_policy(big, allow_shell_operators=True)
    elapsed = time.perf_counter() - start
    assert decision.allowed is False
    assert "COMMAND_SOURCE_TOO_LARGE" in decision.finding_codes
    assert elapsed < 0.05


# 函数用途: analyze_command（效果分类路径）同样在解析前被闸门拦下。
def test_analyze_command_reports_source_too_large() -> None:
    analysis = analyze_command("echo " + "x" * 1_000_000)
    assert analysis.classification == "unknown"
    assert "COMMAND_SOURCE_TOO_LARGE" in analysis.reason_codes


# 函数用途: evaluate 入口超限时绝不调用 shlex（一调即失败）。
def test_evaluate_never_calls_shlex_when_oversized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shlex, "shlex", _fail_if_called)
    decision = evaluate_command_policy("x" * (LIMIT + 1))
    assert "COMMAND_SOURCE_TOO_LARGE" in decision.finding_codes


# 函数用途: analyze 入口超限时绝不调用 shlex（一调即失败）。
def test_analyze_never_calls_shlex_when_oversized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shlex, "shlex", _fail_if_called)
    analysis = analyze_command("x" * (LIMIT + 1))
    assert "COMMAND_SOURCE_TOO_LARGE" in analysis.reason_codes


# 函数用途: 受控执行网关超限时在解析前拒绝，且绝不调用 shlex。
def test_controlled_exec_rejects_oversized_source_without_shlex(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_py_agent.agent.subagents.controlled_exec_gateway import (
        ControlledExecRequest,
        plan_controlled_exec,
    )
    from agent_py_agent.agent.subagents.models import CapabilityGrant

    grant = CapabilityGrant(
        id="g1",
        request_id="r1",
        grant_to_run_id="run1",
        grant_type="shell",
        path_scope=["/tmp"],
    )
    request = ControlledExecRequest(command="x" * (LIMIT + 1), workspace_root="/tmp", grant=grant)
    monkeypatch.setattr(shlex, "shlex", _fail_if_called)
    monkeypatch.setattr(shlex, "split", _fail_if_called)
    plan = plan_controlled_exec(request)
    assert plan.allowed is False
    assert plan.reason == "COMMAND_SOURCE_TOO_LARGE"


# 函数用途: argv 形态不经过 shlex，不做长度闸门（避免误伤长参数数组）。
def test_argv_form_is_not_length_gated() -> None:
    assert evaluate_command_policy(["echo", "x" * 100_000]).allowed is True


# 函数用途: 子代理文本解析点（命令名提取、patch 测试命令校验）超限时不解析、按保守结果返回。
def test_subagent_text_helpers_skip_oversized_without_parse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_py_agent.agent.subagents.capability_request_identity import _command_name
    from agent_py_agent.agent.subagents.capability_scope import _requested_command_name
    from agent_py_agent.agent.subagents.patch.patch_apply_helpers import validate_patch_test_command

    monkeypatch.setattr(shlex, "shlex", _fail_if_called)
    monkeypatch.setattr(shlex, "split", _fail_if_called)
    oversized = "x" * (LIMIT + 1)
    assert _command_name(oversized) == ""
    assert _requested_command_name(oversized) == ""
    assert "过长" in validate_patch_test_command(oversized)


# 函数用途: 新错误码已登记、不可重试，恢复提示指向 write_file 写文件。
def test_source_too_large_code_is_registered_with_write_file_hint() -> None:
    contract = ERROR_CONTRACTS["COMMAND_SOURCE_TOO_LARGE"]
    assert contract.retryable is False
    assert "write_file" in contract.recovery_hint
