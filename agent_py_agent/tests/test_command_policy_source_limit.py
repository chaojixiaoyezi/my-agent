# LLM: cmdcap 用例：顶层命令源长度闸门——解析前拒绝、配置生效、0 不限制、坏值恢复默认、错误码登记；
#   配置注入链（AgentConfig→RegistryParams→注册入口）由 config/registry 测试各自覆盖，这里钉行为合同。
# 模块用途: 钉住 evaluate_command_policy/analyze_command 的命令源长度上限行为。

from __future__ import annotations

import time

import pytest

from agent_py_agent.agent.contracts.error_taxonomy import ERROR_CONTRACTS
from agent_py_agent.agent.contracts.gates.command_policy import (
    analyze_command,
    command_source_max_chars,
    configure_command_source_max_chars,
    evaluate_command_policy,
)


# 函数用途: 每个用例后恢复默认上限，避免进程级状态污染其它测试。
@pytest.fixture(autouse=True)
def _restore_limit():
    yield
    configure_command_source_max_chars(None)


# 函数用途: 默认上限 64K；正常命令与 64K 内的长命令照常通过。
def test_default_limit_is_64k_and_normal_command_passes() -> None:
    assert command_source_max_chars() == 65_536
    assert evaluate_command_policy("echo hello").allowed is True
    assert evaluate_command_policy("echo " + "x" * 60_000).allowed is True


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


# 函数用途: 配置改小后真的生效（边界内通过、边界外拒绝）。
def test_configured_limit_takes_effect() -> None:
    configure_command_source_max_chars(100)
    assert command_source_max_chars() == 100
    assert evaluate_command_policy("x" * 100).allowed is True
    decision = evaluate_command_policy("x" * 101)
    assert "COMMAND_SOURCE_TOO_LARGE" in decision.finding_codes


# 函数用途: 配置为 0 表示不限制（超过默认上限的输入照常解析）。
def test_zero_means_unlimited() -> None:
    configure_command_source_max_chars(0)
    assert command_source_max_chars() == 0
    assert evaluate_command_policy("echo " + "x" * 200_000).allowed is True


# 函数用途: 坏配置值恢复默认，不放行也不误拦。
def test_bad_value_falls_back_to_default() -> None:
    configure_command_source_max_chars("bogus")  # type: ignore[arg-type]
    assert command_source_max_chars() == 65_536


# 函数用途: argv 形态不经过 shlex，不做长度闸门（避免误伤长参数数组）。
def test_argv_form_is_not_length_gated() -> None:
    assert evaluate_command_policy(["echo", "x" * 100_000]).allowed is True


# 函数用途: 新错误码已登记、不可重试，恢复提示指向 write_file 写文件。
def test_source_too_large_code_is_registered_with_write_file_hint() -> None:
    contract = ERROR_CONTRACTS["COMMAND_SOURCE_TOO_LARGE"]
    assert contract.retryable is False
    assert "write_file" in contract.recovery_hint
