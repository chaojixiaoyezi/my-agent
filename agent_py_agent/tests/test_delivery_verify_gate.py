"""收口机器兜底 gate 测试（2026-08-16 收口模式对齐 会话运行时：程序验证只做未知错误）。

覆盖：
1. delivery_verify_commands 结构解析（合法/缺 command/非 list/非 dict/空列表）
2. run_delivery_verification 执行（全过/任一失败/超时/cwd 不可解析/无 contract/
   坏合同 fail-closed）
3. cwd 受控边界（绝对 cwd 越出 workspace → fail-closed）
4. _delivery_verify_no_tool_call_decision 裁决（2026-08-16 新语义）：
   - attempt 内无工具结果未知族 → 不干预（模型自审收口，即使有 contract
     也不强制机器验证）
   - 有未知 + contract 验证命令 → 机器兜底：passed → 终态收口 ok；
     failed/坏合同/cwd 越界 → unfinished + DELIVERY_VERIFY_FAILED（fail-closed）
   - 有未知 + 无验证命令 → 注入未知核验提示 + continue + deferral 事件落账
   - 熔断（第 4 次放过）：同一 attempt 内兜底未决达 4 次 →
     BLOCKED/UNKNOWN_UNRESOLVED 通知用户，绝不无限重试
5. attempt_unknown_operation_count / attempt_closeout_unresolved_count 计数
   （runtime_events 持久化，只计结构化未知族，重启不清零）
6. should_continue_task 结构门（DELIVERY_VERIFY_FAILED + delivery_verify +
   unfinished → 可续跑；source/status 不匹配 → 不续跑）——双席硬缺口 1
7. delivery_contract_preflight_findings 新字段校验
8. 回归探针：test_cli_resume_contract（resume_loop 对 unfinished 续跑）
"""

from __future__ import annotations

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
from agent_py_agent.agent.conversation.runtime import should_continue_task
from agent_py_agent.agent.agent_core.tool_loop.delivery_verify import (
    VERIFY_CONTRACT_INVALID,
    VERIFY_FAILED,
    VERIFY_PASSED,
    VERIFY_SKIPPED,
    delivery_verify_commands,
    run_delivery_verification,
)

UNKNOWN_CODE = "TOOL_OPERATION_OUTCOME_UNKNOWN"


class _FakeRepo:
    """runtime_events 假权威库（events_for_attempt / append_event / agent_run）。"""

    def __init__(self, events=None):
        self.events = list(events or [])

    def events_for_attempt(self, attempt_id: str, *, limit: int = 500):
        return [e for e in self.events if str(e.get("attempt_id")) == str(attempt_id)][:limit]

    def append_event(self, **kwargs):
        self.events.append(kwargs)

    def agent_run_for_run_id(self, run_id: str):
        return {"agent_run_id": f"agent-run-{run_id}", "task_run_id": f"task-run-{run_id}"}


class _FakeParams:
    def __init__(self, contract=None, executed_tools=None, attempt_id="attempt-1", run_id="run-1"):
        self.delivery_contract = contract
        self.executed_tools = executed_tools or []
        self.tool_context: list[str] = []
        self.attempt_id = attempt_id
        self.run_id = run_id


class _FakeAgent:
    def __init__(self, repo=None):
        self.config = type("C", (), {"enable_tools": True})()
        self.subagents = type("S", (), {"runtime_db": repo})() if repo is not None else None
        self._current_run_task_workspace = None


class _FakeCounters:
    empty_text_repairs = 0
    protocol_repairs = 0
    protected_marker_repairs = 0


def _unknown_event(attempt_id="attempt-1", error_code=UNKNOWN_CODE):
    """工具结果未知族事件（写副作用未知/超时/执行者死 → 对账后仍无终态）。"""
    return {
        "event_type": "tool_completed",
        "attempt_id": attempt_id,
        "payload": {"error_code": error_code, "ok": False, "status": "failed"},
    }


def _verify_event(attempt_id, state, n=1):
    return {
        "event_type": "delivery_verify",
        "attempt_id": attempt_id,
        "payload": {"state": state},
        "_n": n,
    }


def _deferral_event(attempt_id="attempt-1"):
    return {
        "event_type": "closeout_unknown_deferral",
        "attempt_id": attempt_id,
        "payload": {},
    }


def _request(contract, repo=None, text="done", attempt_id="attempt-1", run_id="run-1"):
    return _NoToolCallsRequest(
        _FakeAgent(repo=repo),
        _FakeParams(contract=contract, attempt_id=attempt_id, run_id=run_id),
        ModelResponse(text=text, backend="fake"),
        _FakeCounters(),
        has_protected_marker=False,
    )


# ---------- 结构解析 ----------


def test_verify_commands_parsing_ok():
    contract = {
        "verify_commands": [
            {"command": "go build ./...", "cwd": "output/arrow-go"},
            {"command": "go test ./...", "timeout_seconds": 30},
        ]
    }
    commands, state = delivery_verify_commands(contract)
    assert state == VERIFY_PASSED
    assert len(commands) == 2
    assert commands[0]["command"] == "go build ./..."
    assert commands[0]["cwd"] == "output/arrow-go"
    assert commands[0]["timeout_seconds"] == 120  # 默认
    assert commands[1]["timeout_seconds"] == 30


def test_verify_commands_parsing_invalid():
    # 未声明 → SKIPPED（不干预）
    assert delivery_verify_commands(None) == ([], VERIFY_SKIPPED)
    assert delivery_verify_commands({}) == ([], VERIFY_SKIPPED)
    # 声明了但结构非法 → CONTRACT_INVALID（fail-closed）
    assert delivery_verify_commands({"verify_commands": "not-list"})[1] == VERIFY_CONTRACT_INVALID
    assert delivery_verify_commands({"verify_commands": [{"cwd": "x"}]})[1] == VERIFY_CONTRACT_INVALID
    assert delivery_verify_commands({"verify_commands": ["not-dict"]})[1] == VERIFY_CONTRACT_INVALID
    assert delivery_verify_commands({"verify_commands": []})[1] == VERIFY_CONTRACT_INVALID
    assert delivery_verify_commands({"verify_commands": [{"command": "x", "timeout_seconds": "bad"}]})[1] == VERIFY_CONTRACT_INVALID


# ---------- 执行 ----------


def test_run_verification_all_pass(tmp_path):
    contract = {"verify_commands": [{"command": "echo ok", "cwd": str(tmp_path)}]}
    state, results = run_delivery_verification(_FakeParams(contract=contract), workspace_root=tmp_path)
    assert state == VERIFY_PASSED
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
    state, results = run_delivery_verification(_FakeParams(contract=contract), workspace_root=tmp_path)
    assert state == VERIFY_FAILED
    assert results[0]["ok"] is True
    assert results[1]["ok"] is False
    assert results[1]["exit_code"] == 3


def test_run_verification_timeout_fails(tmp_path):
    contract = {
        "verify_commands": [
            {"command": "sleep 5", "cwd": str(tmp_path), "timeout_seconds": 1}
        ]
    }
    state, results = run_delivery_verification(_FakeParams(contract=contract), workspace_root=tmp_path)
    assert state == VERIFY_FAILED
    assert "timeout" in results[0]["detail"]


def test_run_verification_bad_cwd_fails(tmp_path):
    contract = {"verify_commands": [{"command": "echo x", "cwd": "/no/such/dir-xyz"}]}
    state, results = run_delivery_verification(_FakeParams(contract=contract))
    assert state == VERIFY_FAILED
    assert results[0]["ok"] is False


def test_run_verification_cwd_out_of_workspace_fails(tmp_path):
    # 绝对 cwd 越出 workspace 根 → fail-closed（双席硬缺口 3）
    outside = tmp_path.parent / "outside-xyz"
    contract = {"verify_commands": [{"command": "echo x", "cwd": str(outside)}]}
    state, results = run_delivery_verification(
        _FakeParams(contract=contract), workspace_root=tmp_path
    )
    assert state == VERIFY_FAILED
    assert "越界" in results[0]["detail"]


def test_run_verification_cwd_inside_workspace_ok(tmp_path):
    sub = tmp_path / "output"
    sub.mkdir()
    contract = {"verify_commands": [{"command": "echo x", "cwd": str(sub)}]}
    state, _ = run_delivery_verification(
        _FakeParams(contract=contract), workspace_root=tmp_path
    )
    assert state == VERIFY_PASSED


def test_run_verification_no_contract_skip():
    state, results = run_delivery_verification(_FakeParams(contract=None))
    assert state == VERIFY_SKIPPED
    assert results == []


def test_run_verification_invalid_contract_fail_closed():
    state, results = run_delivery_verification(
        _FakeParams(contract={"verify_commands": "bad"})
    )
    assert state == VERIFY_CONTRACT_INVALID
    assert results == []


def test_run_verification_relative_cwd_with_workspace(tmp_path):
    sub = tmp_path / "output"
    sub.mkdir()
    contract = {"verify_commands": [{"command": "pwd", "cwd": "output"}]}
    state, results = run_delivery_verification(
        _FakeParams(contract=contract), workspace_root=tmp_path
    )
    assert state == VERIFY_PASSED
    assert str(sub) in results[0]["output"]


# ---------- 收口 gate 裁决 ----------


def test_gate_no_contract_not_intervened():
    assert _delivery_verify_no_tool_call_decision(_request(None)) is None


def test_gate_no_unknown_not_intervened_even_with_contract(tmp_path):
    """2026-08-16 新语义核心：无工具结果未知 → 模型自审收口，即使 contract
    声明了 verify_commands 也不强制机器验证（程序验证只做未知错误）。"""
    contract = {"verify_commands": [{"command": "echo ok", "cwd": str(tmp_path)}]}
    assert _delivery_verify_no_tool_call_decision(_request(contract)) is None


def test_gate_unknown_verify_fail_unfinished(tmp_path):
    """有未知 + 验证命令真失败 → unfinished + DELIVERY_VERIFY_FAILED（机器兜底
    裁决：未知未核实 ≠ 完成）。"""
    contract = {"verify_commands": [{"command": "exit 7", "cwd": str(tmp_path)}]}
    agent = _FakeAgent(repo=_FakeRepo([_unknown_event()]))
    agent._current_run_task_workspace = str(tmp_path)
    request = _NoToolCallsRequest(
        agent,
        _FakeParams(contract=contract),
        ModelResponse(text="done", backend="fake"),
        _FakeCounters(),
        has_protected_marker=False,
    )
    decision = _delivery_verify_no_tool_call_decision(request)
    assert decision is not None
    assert decision.action == "break"
    response = decision.response
    assert response.runtime_status == "unfinished"
    assert response.runtime_reason == "DELIVERY_VERIFY_FAILED"
    assert response.runtime_source == "delivery_verify"
    assert any("delivery-verify-failed" in line for line in request.params.tool_context)
    assert "产物未通过机器验证" in response.text


def test_gate_unknown_invalid_contract_fail_closed():
    """有未知 + contract 结构非法 → fail-closed（坏合同不得伪装成自然收口）。"""
    contract = {"verify_commands": "bad"}
    request = _request(contract, repo=_FakeRepo([_unknown_event()]))
    decision = _delivery_verify_no_tool_call_decision(request)
    assert decision is not None
    response = decision.response
    assert response.runtime_status == "unfinished"
    assert response.runtime_reason == "DELIVERY_VERIFY_FAILED"
    assert any("结构非法" in line for line in request.params.tool_context)


def test_gate_cwd_out_of_bounds_fail_closed(tmp_path):
    """绝对 cwd 越出 workspace 根 → fail-closed（双席硬缺口 3）。"""
    outside = tmp_path.parent / "outside-xyz"
    contract = {"verify_commands": [{"command": "echo x", "cwd": str(outside)}]}
    agent = _FakeAgent(repo=_FakeRepo([_unknown_event()]))
    agent._current_run_task_workspace = str(tmp_path)
    request = _NoToolCallsRequest(
        agent,
        _FakeParams(contract=contract),
        ModelResponse(text="done", backend="fake"),
        _FakeCounters(),
        has_protected_marker=False,
    )
    decision = _delivery_verify_no_tool_call_decision(request)
    assert decision is not None
    assert decision.response.runtime_reason == "DELIVERY_VERIFY_FAILED"


def test_gate_unknown_no_contract_injects_nudge_and_defers():
    """有未知 + 无验证命令 → 注入未知核验提示 + continue + deferral 事件落账
    （模型去核验实际状态后再收口；每轮注入计一次未决，第 4 次熔断）。"""
    repo = _FakeRepo([_unknown_event()])
    request = _request(None, repo=repo)
    decision = _delivery_verify_no_tool_call_decision(request)
    assert decision is not None
    assert decision.action == "continue"
    assert any("closeout-unknown" in line for line in request.params.tool_context)
    assert "TOOL_OPERATION_OUTCOME_UNKNOWN" in request.params.tool_context[-1]
    # deferral 事件已落账（熔断计数数据源）
    assert any(
        e.get("event_type") == "closeout_unknown_deferral" for e in repo.events
    )


def test_gate_unknown_circuit_breaker_blocked_on_fourth():
    """熔断（第 4 次放过）：同 attempt 兜底未决 ≥ 4 → BLOCKED/UNKNOWN_UNRESOLVED
    通知用户，绝不无限重试（cell6 90 分钟死循环根治）。"""
    repo = _FakeRepo(
        [_unknown_event()] + [_deferral_event() for _ in range(3)]
    )
    request = _request(None, repo=repo)
    decision = _delivery_verify_no_tool_call_decision(request)
    assert decision is not None
    assert decision.action == "break"
    response = decision.response
    assert response.runtime_status == "blocked"
    assert response.runtime_reason == "UNKNOWN_UNRESOLVED"
    assert response.runtime_source == "closeout_unknown_fallback"
    assert "第 4 次放过" in response.text
    assert "未知" in response.text


def test_gate_unknown_verify_failures_also_count_toward_breaker(tmp_path):
    """验证失败同样计入熔断计数：failed 事件 3 次 + 本轮第 4 次 → 熔断。"""
    repo = _FakeRepo(
        [_unknown_event()]
        + [_verify_event("attempt-1", VERIFY_FAILED) for _ in range(3)]
    )
    contract = {"verify_commands": [{"command": "exit 1", "cwd": str(tmp_path)}]}
    agent = _FakeAgent(repo=repo)
    agent._current_run_task_workspace = str(tmp_path)
    request = _NoToolCallsRequest(
        agent,
        _FakeParams(contract=contract),
        ModelResponse(text="done", backend="fake"),
        _FakeCounters(),
        has_protected_marker=False,
    )
    decision = _delivery_verify_no_tool_call_decision(request)
    assert decision is not None
    assert decision.response.runtime_reason == "UNKNOWN_UNRESOLVED"


def test_gate_unknown_verify_passed_terminates_even_ledger_open(tmp_path):
    """机器兜底验证通过 = 未知已核实 = 任务终态：runtime_reason 清空、
    source=delivery_verify（bd88a585/bd620bc1 语义保留）。"""
    contract = {
        "verify_commands": [{"command": "echo verify-ok", "cwd": str(tmp_path)}],
        "task_workspace": {"task_root": str(tmp_path)},
    }
    repo = _FakeRepo([_unknown_event()])
    agent = _FakeAgent(repo=repo)
    agent._current_run_task_workspace = str(tmp_path)
    request = _NoToolCallsRequest(
        agent,
        _FakeParams(contract=contract),
        ModelResponse(text="done", backend="fake"),
        _FakeCounters(),
        has_protected_marker=False,
    )
    decision = _delivery_verify_no_tool_call_decision(request)
    assert decision is not None
    assert decision.response.runtime_status == "ok"
    assert decision.response.runtime_reason == ""
    assert decision.response.runtime_source == "delivery_verify"


# ---------- should_continue_task 结构门（双席硬缺口 1） ----------


def test_should_continue_delivery_verify_failed_true():
    response = ModelResponse(
        text="x",
        backend="fake",
        runtime_status="unfinished",
        runtime_reason="DELIVERY_VERIFY_FAILED",
        runtime_source="delivery_verify",
    )
    should, reason = should_continue_task(response)
    assert should is True
    assert reason == "DELIVERY_VERIFY_FAILED"


def test_should_continue_delivery_verify_wrong_source_false():
    # 同 reason 但 source 不匹配 → 结构门拒绝续跑
    response = ModelResponse(
        text="x",
        backend="fake",
        runtime_status="unfinished",
        runtime_reason="DELIVERY_VERIFY_FAILED",
        runtime_source="tool_loop",
    )
    should, _ = should_continue_task(response)
    assert should is False


def test_should_continue_delivery_verify_wrong_status_false():
    response = ModelResponse(
        text="x",
        backend="fake",
        runtime_status="ok",
        runtime_reason="DELIVERY_VERIFY_FAILED",
        runtime_source="delivery_verify",
    )
    should, _ = should_continue_task(response)
    assert should is False


# ---------- DELIVERY_VERIFY_PASSED 结构门（2026-08-16 第三阶段） ----------
# 部署者验收通过(delivery_verify+ok)=契约完成=任务终态——任何续跑原因
# (TASK_PROGRESS_OPEN 账本未关等)不得覆盖; 结构化字段判断, 不解析正文。


def test_should_continue_verify_passed_overrides_task_progress_open():
    # 验收通过但模型账本未关(TASK_PROGRESS_OPEN)——passed 门一票否决续跑
    response = ModelResponse(
        text="x",
        backend="fake",
        runtime_status="ok",
        runtime_reason="TASK_PROGRESS_OPEN",
        runtime_source="delivery_verify",
    )
    should, reason = should_continue_task(response)
    assert should is False
    assert reason == "DELIVERY_VERIFY_PASSED"


def test_should_continue_verify_passed_overrides_round_limit():
    # 验收通过但轮限原因——同样被 passed 门否决
    response = ModelResponse(
        text="x",
        backend="fake",
        runtime_status="ok",
        runtime_reason="TOOL_ROUND_LIMIT_REACHED",
        runtime_source="delivery_verify",
    )
    should, reason = should_continue_task(response)
    assert should is False
    assert reason == "DELIVERY_VERIFY_PASSED"


def test_should_continue_verify_passed_wrong_source_not_shortcut():
    # source 不是 delivery_verify → 不走 passed 门, 按普通逻辑(此处不进
    # CONTINUABLE_REASONS → 不续跑, 但 reason 不是 DELIVERY_VERIFY_PASSED)
    response = ModelResponse(
        text="x",
        backend="fake",
        runtime_status="ok",
        runtime_reason="TASK_PROGRESS_OPEN",
        runtime_source="tool_loop",
    )
    should, reason = should_continue_task(response)
    assert should is False  # 2026-08-16 第 4 条: tool_loop + TASK_PROGRESS_OPEN 不再续跑
    assert reason == "TASK_PROGRESS_OPEN"


def test_should_continue_verify_passed_wrong_status_not_shortcut():
    # status 不是 ok(delivery_verify 未通过) → 不走 passed 门
    response = ModelResponse(
        text="x",
        backend="fake",
        runtime_status="unfinished",
        runtime_reason="DELIVERY_VERIFY_FAILED",
        runtime_source="delivery_verify",
    )
    should, reason = should_continue_task(response)
    assert should is True  # failed 门: delivery_verify+unfinished 仍续跑
    assert reason == "DELIVERY_VERIFY_FAILED"


# ---------- preflight ----------


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


# ---------- 双席 seq2013/2014 第二轮硬缺口 ----------


def test_gate_workspace_root_missing_fail_closed(tmp_path):
    """workspace 根缺失 → fail-closed 不执行（双席硬缺口1）。"""
    contract = {"verify_commands": [{"command": "echo x", "cwd": str(tmp_path)}]}
    request = _request(contract, repo=_FakeRepo([_unknown_event()]))
    decision = _delivery_verify_no_tool_call_decision(request)
    # workspace 拿不到（fake agent 无 workspace）→ run 内根缺失拒绝
    assert decision is not None
    assert decision.response.runtime_reason == "DELIVERY_VERIFY_FAILED"
    assert any("workspace 根缺失" in line for line in request.params.tool_context)


def test_gate_workspace_resolved_runs_real_verify(tmp_path):
    """回归（2026-08-16 3×3 实锤）：workspace 可解析时必须真正执行 verify。

    修复前 `_delivery_verify_no_tool_call_decision` 从 `runner.context` 导入
    `current_run_task_workspace_root`——该函数不存在 → ImportError 被 except
    吞掉 → workspace_root 恒 None → 全过也判 DELIVERY_VERIFY_FAILED →
    无限续跑（8/15 起所有 delivery_verify 事件全 failed 实锤）。
    修复后从 `run_task_workspace_writer` 导入；2026-08-16 起触发条件收敛为
    「attempt 内有工具结果未知」——本测试带 unknown 事件，验证真实执行且
    全过 → 终态收口（break）：runtime_reason 清空、source=delivery_verify
    （bd88a585: 验收通过覆盖 TASK_PROGRESS_OPEN 等续跑原因）。
    """
    contract = {
        "verify_commands": [{"command": "echo verify-ok", "cwd": str(tmp_path)}],
        "task_workspace": {"task_root": str(tmp_path)},
    }
    repo = _FakeRepo([_unknown_event()])
    agent = _FakeAgent(repo=repo)
    agent._current_run_task_workspace = str(tmp_path)
    request = _NoToolCallsRequest(
        agent,
        _FakeParams(contract=contract),
        ModelResponse(text="done", backend="fake"),
        _FakeCounters(),
        has_protected_marker=False,
    )
    decision = _delivery_verify_no_tool_call_decision(request)
    assert decision is not None
    assert decision.response.runtime_status == "ok"
    assert decision.response.runtime_reason == ""
    assert decision.response.runtime_source == "delivery_verify"
    assert not any("delivery-verify" in line for line in request.params.tool_context)


def test_gate_workspace_resolved_verify_failure_still_unfinished(tmp_path):
    """workspace 可解析但 verify 命令真失败 → 仍 unfinished（真验证，非误伤）。"""
    contract = {
        "verify_commands": [{"command": "exit 3", "cwd": str(tmp_path)}],
        "task_workspace": {"task_root": str(tmp_path)},
    }
    repo = _FakeRepo([_unknown_event()])
    agent = _FakeAgent(repo=repo)
    agent._current_run_task_workspace = str(tmp_path)
    request = _NoToolCallsRequest(
        agent,
        _FakeParams(contract=contract),
        ModelResponse(text="done", backend="fake"),
        _FakeCounters(),
        has_protected_marker=False,
    )
    decision = _delivery_verify_no_tool_call_decision(request)
    assert decision is not None
    assert decision.response.runtime_reason == "DELIVERY_VERIFY_FAILED"
    assert any("FAIL" in line for line in request.params.tool_context)


def test_run_verification_root_missing_fail_closed():
    state, results = run_delivery_verification(
        _FakeParams(contract={"verify_commands": [{"command": "echo x"}]}),
        workspace_root=None,
    )
    assert state == VERIFY_FAILED
    assert "workspace 根缺失" in results[0]["detail"]


def test_verification_id_stable_and_distinct():
    c1 = {"verify_commands": [{"command": "go build ./..."}]}
    c2 = {"verify_commands": [{"command": "go test ./..."}]}
    p1 = _FakeParams(contract=c1)
    p1.attempt_id = "attempt-1"
    p2 = _FakeParams(contract=c2)
    p2.attempt_id = "attempt-1"
    from agent_py_agent.agent.agent_core.tool_loop.delivery_verify import build_verification_id

    assert build_verification_id(p1, c1) == build_verification_id(p1, c1)  # 稳定
    assert build_verification_id(p1, c1) != build_verification_id(p2, c2)  # 命令不同
    p3 = _FakeParams(contract=c1)
    p3.attempt_id = "attempt-2"
    assert build_verification_id(p1, c1) != build_verification_id(p3, c1)  # attempt 不同


def test_persist_event_uses_append_event(tmp_path):
    """落账走权威 API append_event（双席 seq2014 实锤修复）。"""
    from agent_py_agent.agent.agent_core.tool_loop.delivery_verify import (
        _contract_hash,
        persist_delivery_verify_event,
    )

    calls = {"append": 0, "events": []}

    class _FakeRepo:
        def append_event(self, **kwargs):
            calls["append"] += 1
            calls["events"].append(kwargs)

        def agent_run_for_run_id(self, run_id):
            return {"agent_run_id": "agr-1"}

        def events_for_attempt(self, attempt_id, **kwargs):
            return []

    class _FakeAgentWithRepo:
        def __init__(self):
            self.config = type("C", (), {"enable_tools": True})()
            self.subagents = type("S", (), {"runtime_db": _FakeRepo()})()

    params = _FakeParams(contract={"verify_commands": [{"command": "go build ./..."}]})
    params.attempt_id = "attempt-1"
    params.run_id = "run-1"
    contract_hash = _contract_hash(params.delivery_contract)
    persist_delivery_verify_event(
        _FakeAgentWithRepo(), params, VERIFY_FAILED, [], contract_hash, "vid-1"
    )
    assert calls["append"] == 1
    assert calls["events"][0]["event_type"] == "delivery_verify"
    assert calls["events"][0]["attempt_id"] == "attempt-1"
    assert calls["events"][0]["agent_run_id"] == "agr-1"
    assert calls["events"][0]["payload"]["verification_id"] == "vid-1"


def test_persist_event_idempotent_skip(tmp_path):
    """同 verification_id 重复落账 → 幂等跳过（双席硬缺口3）。"""
    from agent_py_agent.agent.agent_core.tool_loop.delivery_verify import (
        _contract_hash,
        persist_delivery_verify_event,
    )

    calls = {"append": 0}

    class _FakeRepo:
        def append_event(self, **kwargs):
            calls["append"] += 1

        def agent_run_for_run_id(self, run_id):
            return {"agent_run_id": "agr-1"}

        def events_for_attempt(self, attempt_id, **kwargs):
            return [
                {
                    "event_type": "delivery_verify",
                    "payload_json": '{"verification_id": "vid-1"}',
                }
            ]

    class _FakeAgentWithRepo:
        def __init__(self):
            self.config = type("C", (), {"enable_tools": True})()
            self.subagents = type("S", (), {"runtime_db": _FakeRepo()})()

    params = _FakeParams(contract={"verify_commands": [{"command": "go build ./..."}]})
    params.attempt_id = "attempt-1"
    params.run_id = "run-1"
    persist_delivery_verify_event(
        _FakeAgentWithRepo(),
        params,
        VERIFY_FAILED,
        [],
        _contract_hash(params.delivery_contract),
        "vid-1",
    )
    assert calls["append"] == 0  # 已存在 → 跳过


def test_persist_event_idempotent_real_sqlite(tmp_path):
    """真实 RuntimeRepository(SQLite) 幂等: 同 verification_id 重复落账只落一条。

    双席 seq2016/2017 硬缺口3: 旧实现调 list_runtime_events(权威库不存在,
    AttributeError 被吞 → 永不幂等, 每次重裁决重复落账)。此测试用真实
    RuntimeRepository 证明 events_for_attempt + verification_id 去重在
    SQLite 账本上生效, 不是 fake repo 自证。
    """
    from types import SimpleNamespace

    from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
    from agent_py_agent.agent.agent_core.tool_loop.delivery_verify import (
        _contract_hash,
        persist_delivery_verify_event,
    )

    repo = RuntimeRepository(tmp_path / "home" / "runtime.db")
    agent = SimpleNamespace(
        config=SimpleNamespace(enable_tools=True),
        subagents=SimpleNamespace(runtime_db=repo),
    )
    params = SimpleNamespace(
        run_id="run-verify-1",
        attempt_id="attempt-verify-1",
        task_run_id="taskrun-verify-1",
        delivery_contract={"verify_commands": [{"command": "go build ./..."}]},
    )
    contract_hash = _contract_hash(params.delivery_contract)
    persist_delivery_verify_event(
        agent, params, VERIFY_FAILED, [], contract_hash, "vid-real-1"
    )
    # 同 verification_id 第二次落账 → 幂等跳过
    persist_delivery_verify_event(
        agent, params, VERIFY_FAILED, [], contract_hash, "vid-real-1"
    )
    events = repo.events_for_attempt("attempt-verify-1", limit=50)
    verify_events = [e for e in events if e.get("event_type") == "delivery_verify"]
    assert len(verify_events) == 1, "同 verification_id 在真实 SQLite 只落一条"
    payload = verify_events[0].get("payload") or verify_events[0].get("payload_json") or {}
    if isinstance(payload, str):
        import json as _json

        payload = _json.loads(payload)
    assert payload.get("verification_id") == "vid-real-1"
    assert payload.get("state") == "failed"


# ---------- 产物预检移除（2026-08-15 owner seq2035 决策） ----------
# 参考项目(会话运行时/工具运行时/终端交互)都没有「必须有源码+测试文件」的产物预检;
# 撤掉后完成与否只由 verify_commands 真实输出裁决。空壳假绿的根治方向在底座
# (让模型别假完成), 见 owner seq2035「接同模型跑 会话运行时 对照」。


def test_run_verification_no_artifact_precheck(tmp_path):
    """gate 不再做产物存在性预检: 只按 verify_commands 真实输出判结果。

    撤掉 precheck 后, 产物是否存在不决定结果; 命令真实执行成功即 VERIFY_PASSED。
    """
    mod = tmp_path / "arrow-go"
    mod.mkdir()
    (mod / "util.go").write_text("package arrow\n")
    contract = {
        "artifacts": [{"path": str(mod), "kind": "go-module"}],
        "verify_commands": [{"command": "echo fake-ok", "cwd": str(mod)}],
    }
    state, results = run_delivery_verification(
        _FakeParams(contract=contract), workspace_root=tmp_path
    )
    assert state == VERIFY_PASSED
    assert results[0]["ok"] is True
