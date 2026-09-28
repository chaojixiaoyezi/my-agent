"""task_progress / cancel_subagents 的范围裁决：显式 run_id 不在可见范围时给结构化拒绝。

来源：dev 派活任务 5（沿用 my-agent-4 在 list_agents 里定的裁决码做法，commit 30c3b98d4）。
两处工具过去都静默：task_progress 会把不可见的显式 id 当成账本键（甚至新建一本），
cancel_subagents 会把它一路带到取消执行层，最后报成无意义的 programmer 错误。
现在两者共用 `orchestration/scope_resolution.UNMATCHED_RUN_SCOPE_CODE`。

判定只看宿主事实（当前身份、磁盘上的账本文件、可见树），不解析自然语言；"不存在"和"无权看"
返回完全相同的答复，不泄漏目标是否存在。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.agent_core import task_progress_tool as tpt
from agent_py_agent.agent.agent_core.orchestration.scope_resolution import UNMATCHED_RUN_SCOPE_CODE
from agent_py_agent.agent.agent_core.orchestration.tools import cancel as cancel_tool


# 函数用途: 造一个"正在普通会话里运行"的最小宿主。
def _agent(tmp_path: Path, *, run_id: str = "run-current", task_id: str = "task-current") -> SimpleNamespace:
    current = SimpleNamespace(
        run_id=run_id, task_id=task_id, context_scope="conversation", source="gateway",
        task_attributes={"conversation_thread_id": "thread-1"},
    )
    return SimpleNamespace(_current_run_params=current, _main_agent_run_id="main-run", root=tmp_path,
                           home_paths=SimpleNamespace(root=tmp_path),
                           subagents=SimpleNamespace(list_runs=lambda: []))


# 函数用途: 造一个没有可信运行身份的宿主（显式 id 无处可比对时的分支）。
def _bare_agent(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(_current_run_params=None, _main_agent_run_id="", _current_request_id="",
                           root=tmp_path, home_paths=SimpleNamespace(root=tmp_path),
                           subagents=SimpleNamespace(list_runs=lambda: []))


def test_scope_rejection_payload_names_the_shared_decision_code():
    outcome = tpt._scope_rejection_outcome("ghost-run-9999", detail="x")
    payload = json.loads(outcome.output)
    assert outcome.ok is False
    assert outcome.error_code == "TOOL_INVALID_ARGUMENTS"
    assert outcome.effect_outcome == "not_started"
    # 请求的 id 只留在 explicit，不冒充 effective 范围。
    assert payload["scope_resolution"]["explicit"] == {"run_id": "ghost-run-9999"}
    assert payload["scope_resolution"]["effective"] == {}
    assert UNMATCHED_RUN_SCOPE_CODE in payload["scope_resolution"]["reasons"]
    assert UNMATCHED_RUN_SCOPE_CODE in payload["scope_warnings"]


def test_unmatched_explicit_target_is_denied_and_does_not_create_a_ledger(tmp_path):
    agent = _agent(tmp_path)
    denial = tpt._unmatched_explicit_run_target(agent, tmp_path, "ghost-run-9999", {})
    assert denial is not None and denial.ok is False
    assert UNMATCHED_RUN_SCOPE_CODE in json.loads(denial.output)["scope_warnings"]
    # 关键：没有在磁盘上凭空建出这本账。
    assert not tpt.progress_path(tmp_path, "ghost-run-9999").exists()


def test_current_ledger_and_existing_ledger_are_not_denied(tmp_path):
    agent = _agent(tmp_path)
    assert tpt._unmatched_explicit_run_target(agent, tmp_path, "run-current", {}) is None
    assert tpt._unmatched_explicit_run_target(agent, tmp_path, "task-current", {}) is None
    target = tpt._target_run_id(agent, {}, allow_explicit=False)
    assert tpt._unmatched_explicit_run_target(agent, tmp_path, target, {}) is None
    # 已存在的历史账本照旧可读可写（不误伤正常补充旧计划）。
    existing = tpt.progress_path(tmp_path, "legacy-run")
    existing.parent.mkdir(parents=True)
    existing.write_text("{}", encoding="utf-8")
    assert tpt._unmatched_explicit_run_target(agent, tmp_path, "legacy-run", {}) is None


def test_empty_explicit_target_is_denied_even_without_current_identity(tmp_path):
    """没有可信运行身份时更需要裁决：否则显式 id 会被直接当成新账本键。"""
    agent = _bare_agent(tmp_path)
    denial = tpt._unmatched_explicit_run_target(agent, tmp_path, "ghost-run-9999", {})
    assert denial is not None and denial.ok is False
    assert tpt._unmatched_explicit_run_target(agent, tmp_path, "", {}) is None


def test_cancel_invisible_explicit_target_is_judged_at_resolution():
    agent = SimpleNamespace(subagents=SimpleNamespace(list_runs=lambda: []))
    result = cancel_tool._resolve_run_ids(agent, {"run_id": "ghost-run-9999"})
    # 不可见的显式目标在解析阶段就被判掉：不给 run_ids，只给结构化原因。
    assert result.ok is True and result.run_ids == []
    assert result.error_payload["reason"] == UNMATCHED_RUN_SCOPE_CODE
    assert result.error_payload["requested_run_ids"] == ["ghost-run-9999"]


def test_cancel_visible_explicit_target_is_not_denied():
    visible = SimpleNamespace(id="child-1", status="RUNNING")
    agent = SimpleNamespace(subagents=SimpleNamespace(list_runs=lambda: [visible]))
    result = cancel_tool._resolve_run_ids(agent, {"run_id": "child-1"})
    assert result.ok is True and result.run_ids == ["child-1"] and result.error_payload == {}


def test_cancel_without_any_target_keeps_parameter_required_semantics():
    agent = SimpleNamespace(subagents=SimpleNamespace(list_runs=lambda: []))
    for params in ({}, {"run_ids": []}, {"root_id": ""}):
        result = cancel_tool._resolve_run_ids(agent, params)
        assert result.error_payload == {}, f"{params} 不该被当成范围裁决"


def test_cancel_empty_explicit_target_returns_structured_receipt():
    agent = SimpleNamespace(subagents=SimpleNamespace(list_runs=lambda: []),
                            _current_run_params=None, home_paths=SimpleNamespace(root=Path("/tmp")))
    outcome = cancel_tool.execute_cancel_subagents(agent, {"run_id": "ghost-run-9999"})
    payload = json.loads(outcome.output)
    assert outcome.ok is False
    assert outcome.error_code == "TOOL_INVALID_ARGUMENTS"
    assert payload["reason"] == UNMATCHED_RUN_SCOPE_CODE


def test_cancel_missing_parameters_keeps_tool_parameter_required():
    agent = SimpleNamespace(subagents=SimpleNamespace(list_runs=lambda: []),
                            _current_run_params=None, home_paths=SimpleNamespace(root=Path("/tmp")))
    outcome = cancel_tool.execute_cancel_subagents(agent, {})
    assert outcome.error_code == "TOOL_PARAMETER_REQUIRED"


def test_cancel_error_code_maps_scope_denial_to_invalid_arguments():
    """裁决码要有准确分类码，不能兜底成 UNKNOWN_ERROR 让模型以为放弃就行。"""
    assert cancel_tool._cancel_error_code({"reason": UNMATCHED_RUN_SCOPE_CODE}) == "TOOL_INVALID_ARGUMENTS"
    assert cancel_tool._cancel_error_code({"error": "invalid_status_filter"}) == "TOOL_INVALID_ARGUMENTS"
    assert cancel_tool._cancel_error_code({"error": {"category": "runtime"}}) == "TOOL_EXECUTION_FAILED"


def test_tool_progress_entry_point_returns_the_denial_not_a_silent_write(tmp_path):
    """接线用例：走 TaskProgressTool.execute 入口，确认裁决真的接进去了。

    只测判定函数本身不足以证明生效——曾经把入口那两行改成 no-op，整套用例仍然全绿。
    这里断言：既拿到裁决回执，又没有在磁盘上留下任何账本。
    """
    agent = _agent(tmp_path)
    outcome = tpt.TaskProgressTool(agent).execute({
        "action": "update", "run_id": "ghost-run-9999",
        "items": [{"id": "x", "title": "t", "status": "pending"}],
    })
    assert outcome.ok is False
    assert outcome.error_code == "TOOL_INVALID_ARGUMENTS"
    payload = json.loads(outcome.output)
    assert UNMATCHED_RUN_SCOPE_CODE in payload["scope_warnings"]
    assert not tpt.progress_path(tmp_path, "ghost-run-9999").exists()


def test_tool_progress_entry_point_read_is_also_denied(tmp_path):
    agent = _agent(tmp_path)
    outcome = tpt.TaskProgressTool(agent).execute({"action": "read", "run_id": "ghost-run-9999"})
    assert outcome.ok is False
    payload = json.loads(outcome.output)
    assert UNMATCHED_RUN_SCOPE_CODE in payload["scope_warnings"]
