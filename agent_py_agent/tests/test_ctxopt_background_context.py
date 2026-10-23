"""CTX-13：完整后台段计入标题和控制说明，必要事实即使超限也不伪装已满足预算。"""
from dataclasses import replace

import pytest

from agent_py_agent.agent.conversation.background_context import (
    PreparedBackgroundContext,
    render_background_context,
)
from agent_py_agent.agent.conversation.context_budget import (
    BackgroundContextBudget,
    BackgroundContextPayloadRequest,
)
from agent_py_agent.agent.memory_archive.tokens import estimate_tokens


# LLM: 构造冻结的后台材料，不访问真实会话账或发送通知；所有引用可逐字核对。
# 函数用途: 用可裁正文加固定控制开销复现整体超预算与引用被截断。
def _prepared(*, narrow=False, maximum=2600):
    ref = "/workspace/task/" + "long-report-name-" * 30 + ".md"
    payload = BackgroundContextPayloadRequest(
        bundle={"thread": {"thread_id": "thread-test"},
                "messages": [{"role": "user", "content": "可裁历史资料" * 180} for _ in range(25)],
                "tasks": [{"task_id": "task-test", "run_prompt": "核对当前目标", "task_path": ref}],
                "observations": [{"summary": "历史观察" * 150} for _ in range(20)]},
        active_wake_signal={"wake_signal_id": "wake-test", "reason": "subagent_runner_finished",
                            "evidence_refs": [ref], "metadata": {"task_id": "task-test", "final_report_ref": ref}},
        pending_wake_signals=[], agent_tree={}, load_errors=[{"error_code": "LOAD_FAIL", "evidence_ref": ref}],
        subagent_completions={"schema": "completion.v1", "workspace_task_id": "task-test", "items": [
            {"task_id": "child-test", "root_task_id": "task-test", "status": "done", "final_report_ref": ref,
             "completion_message": "子代理交付" * 150}]},
        budget=BackgroundContextBudget(max_total_tokens=maximum),
    )
    return PreparedBackgroundContext(payload=payload, header=("[background-main-agent-context]", "后台控制事实" * 40),
                                     control_policy={"allowed_tools": ["read_file", "task_progress"]},
                                     control_actions=("- read_file: " + "使用当前权限读取已确认产物。" * 25,),
                                     task_id="task-test", narrow_audit_event=narrow, include_recent_messages=True)


@pytest.mark.parametrize("narrow", [False, True])
def test_background_context_total_budget_measures_actual_rendered_envelope(narrow):
    prepared = _prepared(narrow=narrow)
    result = render_background_context(prepared)
    assert estimate_tokens(result) <= prepared.payload.budget.max_total_tokens
    ref = prepared.payload.active_wake_signal["evidence_refs"][0]
    assert ref in result and '"wake-test"' in result and '"LOAD_FAIL"' in result
    if not narrow:
        assert '"child-test"' in result and '"final_report_ref": "' + ref + '"' in result
    assert render_background_context(prepared) == result


def test_background_context_reports_unavoidable_protected_fact_overflow():
    prepared = _prepared(maximum=200)
    result = render_background_context(prepared)
    assert '"protected_facts_exceed_budget": true' in result
    ref = prepared.payload.active_wake_signal["evidence_refs"][0]
    assert ref in result and '"wake-test"' in result and '"child-test"' in result


def test_background_context_zero_budget_remains_unlimited():
    prepared = _prepared()
    unlimited = replace(prepared, payload=replace(prepared.payload, budget=BackgroundContextBudget(max_total_tokens=0)))
    result = render_background_context(unlimited)
    assert '"max_total_tokens"' not in result
    assert "## Available Control Actions" in result


def test_background_context_keeps_real_task_link_refs_beyond_key_window():
    from agent_py_agent.agent.conversation.context_budget import bounded_background_context_payload
    from agent_py_agent.agent.conversation.models import ThreadTaskLink

    prepared = _prepared(maximum=200)
    ref = prepared.payload.active_wake_signal["evidence_refs"][0]
    task = ThreadTaskLink(
        thread_id="thread-test", task_id="task-test", goal="当前任务", task_path=ref,
        effective_evidence_refs=(ref,), continuation_refs=({"run_id": "continued-run", "report_ref": ref},),
    ).to_dict()
    request = replace(prepared.payload, bundle={"tasks": [task]})
    actual = bounded_background_context_payload(request)["tasks"][0]
    for key in ("task_id", "thread_id", "task_path", "effective_evidence_refs", "continuation_refs"):
        assert actual[key] == task[key]


@pytest.mark.parametrize("audit", [False, True])
def test_background_context_preserves_deep_refs_and_audit_task_path(audit):
    from agent_py_agent.agent.conversation.context_budget import bounded_background_context_payload

    prepared = _prepared(maximum=200)
    ref = prepared.payload.active_wake_signal["evidence_refs"][0]
    state = {"task_id": "task-test", "task_path": ref,
             "layers": {"nested": {"checkpoint_ref": ref, "task_id": "deep-task"}}}
    if audit:
        state["work_kind"] = "audit"
    actual = bounded_background_context_payload(replace(prepared.payload, task_runtime_state=state))["task_runtime_state"]
    assert actual["task_path"] == ref
    if not audit:
        assert actual["layers"]["nested"]["checkpoint_ref"] == ref
        assert actual["layers"]["nested"]["task_id"] == "deep-task"
