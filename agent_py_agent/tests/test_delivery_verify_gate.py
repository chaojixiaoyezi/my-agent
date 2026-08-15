"""第 4 层收口机器验证 gate 测试（2026-08-15 3×3 假完成根治）。

覆盖：
1. delivery_verify_commands 结构解析（合法/缺 command/非 list/非 dict）
2. run_delivery_verification 执行（全过/任一失败/超时/cwd 不可解析/无 contract）
3. _delivery_verify_no_tool_call_decision 裁决（无 contract 不干预/全过不干预/
   失败 → unfinished+DELIVERY_VERIFY_FAILED+tool_context 注入）
4. delivery_contract_preflight_findings 新字段校验
5. 回归探针：test_cli_resume_contract（resume_loop 对 unfinished 续跑）
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from agent_py_agent.agent.agent_core.delivery_contract_prompting import (
    delivery_contract_preflight_findings,
)
from agent_py_agent.agent.agent_core.tool_loop.response_decision import (
    _NoToolCallsRequest,
    ToolLoopResponseDecision,
    _delivery_verify_no_tool_call_decision,
)
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.cli.delivery_verify import (
    delivery_verify_commands,
    run_delivery_verification,
)


class _FakeParams:
    def __init__(self, contract=None, executed_tools=None):
        self.delivery_contract = contract
        self.executed_tools = executed_tools or []
        self.tool_context: list[str] = []


class _FakeAgent:
    def __init__(self):
        self.config = type("C", (), {"enable_tools": True})()


class _FakeCounters:
    empty_text_repairs = 0
    protocol_repairs = 0
    protected_marker_repairs = 0


def _request(contract, text="done"):
    return _NoToolCallsRequest(
        _FakeAgent(),
        _FakeParams(contract=contract),
        ModelResponse(text=text, backend="fake"),
        _FakeCounters(),
        has_protected_marker=False,
    )


def test_verify_commands_parsing_ok():
    contract = {
        "verify_commands": [
            {"command": "go build ./...", "cwd": "output/arrow-go"},
            {"command": "go test ./...", "timeout_seconds": 30},
        ]
    }
    commands = delivery_verify_commands(contract)
    assert len(commands) == 2
    assert commands[0]["command"] == "go build ./..."
    assert commands[0]["cwd"] == "output/arrow-go"
    assert commands[0]["timeout_seconds"] == 120  # 默认
    assert commands[1]["timeout_seconds"] == 30


def test_verify_commands_parsing_invalid():
    assert delivery_verify_commands(None) == []
    assert delivery_verify_commands({}) == []
    assert delivery_verify_commands({"verify_commands": "not-list"}) == []
    assert delivery_verify_commands({"verify_commands": [{"cwd": "x"}]}) == []  # 缺 command
    assert delivery_verify_commands({"verify_commands": ["not-dict"]}) == []


def test_run_verification_all_pass(tmp_path):
    contract = {"verify_commands": [{"command": "echo ok", "cwd": str(tmp_path)}]}
    all_ok, results = run_delivery_verification(_FakeParams(contract=contract))
    assert all_ok is True
    assert len(results) == 1
    assert results[0]["ok"] is True
    assert results[0]["exit_code"] == 0


def test_run_verification_one_fails(tmp_path):
    contract = {
        "verify_commands": [
            {"command": "echo ok", "cwd": str(tmp_path)},
            {"command": "exit 3", "cwd": str(tmp_path)},
        ]
    }
    all_ok, results = run_delivery_verification(_FakeParams(contract=contract))
    assert all_ok is False
    assert results[0]["ok"] is True
    assert results[1]["ok"] is False
    assert results[1]["exit_code"] == 3


def test_run_verification_timeout_fails(tmp_path):
    contract = {
        "verify_commands": [
            {"command": "sleep 5", "cwd": str(tmp_path), "timeout_seconds": 1}
        ]
    }
    all_ok, results = run_delivery_verification(_FakeParams(contract=contract))
    assert all_ok is False
    assert results[0]["ok"] is False
    assert "timeout" in results[0]["detail"]


def test_run_verification_bad_cwd_fails(tmp_path):
    contract = {"verify_commands": [{"command": "echo x", "cwd": "/no/such/dir-xyz"}]}
    all_ok, results = run_delivery_verification(_FakeParams(contract=contract))
    assert all_ok is False
    assert results[0]["ok"] is False


def test_run_verification_no_contract_noop():
    all_ok, results = run_delivery_verification(_FakeParams(contract=None))
    assert all_ok is True
    assert results == []


def test_run_verification_relative_cwd_with_workspace(tmp_path):
    sub = tmp_path / "output"
    sub.mkdir()
    contract = {"verify_commands": [{"command": "pwd", "cwd": "output"}]}
    all_ok, results = run_delivery_verification(_FakeParams(contract=contract), workspace_root=tmp_path)
    assert all_ok is True
    assert str(sub) in results[0]["output"]


def test_gate_no_contract_not_intervened():
    decision = _delivery_verify_no_tool_call_decision(_request(None))
    assert decision is None


def test_gate_verify_pass_not_intervened(tmp_path):
    contract = {"verify_commands": [{"command": "echo ok", "cwd": str(tmp_path)}]}
    decision = _delivery_verify_no_tool_call_decision(_request(contract))
    assert decision is None


def test_gate_verify_fail_unfinished(tmp_path):
    contract = {"verify_commands": [{"command": "exit 7", "cwd": str(tmp_path)}]}
    request = _request(contract)
    decision = _delivery_verify_no_tool_call_decision(request)
    assert decision is not None
    assert decision.action == "break"
    response = decision.response
    assert response.runtime_status == "unfinished"
    assert response.runtime_reason == "DELIVERY_VERIFY_FAILED"
    assert response.runtime_source == "delivery_verify"
    # 失败输出注入 tool_context（模型续跑轮可见）
    assert any("delivery-verify-failed" in line for line in request.params.tool_context)
    assert "产物未通过机器验证" in response.text


def test_preflight_verify_commands_validation():
    assert delivery_contract_preflight_findings({"verify_commands": []}) == []
    assert delivery_contract_preflight_findings(
        {"verify_commands": [{"command": "go build ./..."}]}
    ) == []
    findings = delivery_contract_preflight_findings({"verify_commands": "bad"})
    assert any(f["code"] == "DELIVERY_CONTRACT_VERIFY_COMMANDS_INVALID" for f in findings)
    findings = delivery_contract_preflight_findings(
        {"verify_commands": [{"cwd": "x"}]}
    )
    assert any(f["code"] == "DELIVERY_CONTRACT_VERIFY_COMMAND_INVALID" for f in findings)
