# LLM: Fake only the shared decision service; real Todo read and ledger writes
# prove that a priority hint never becomes a Goal, status update or child run.
# 模块用途: 离线验证现有 Todo 的可选优先级建议与原回执、过期和停止边界。
from __future__ import annotations

import json
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import decision_planning as module
from agent_py_agent.agent.agent_core.task_progress_tool import TaskProgressTool
from agent_py_agent.agent.backends.decision_protocol import (
    DecisionAnswer,
    DecisionBinding,
    DecisionResponse,
)
from agent_py_agent.agent.settings.decision_settings import execute_decision_settings_operation
from agent_py_agent.agent.task_progress import (
    read_task_progress,
    with_task_progress_display_plan,
    write_task_progress,
)
from agent_py_agent.agent.tooling.cancellation import ToolCancelled
from agent_py_agent.cli.chat_parts.tui_decision_menu import _fields
from agent_py_agent.tests.test_decision_settings import host_at, patch


@pytest.fixture
def prepared(tmp_path):
    params = SimpleNamespace(
        request_id="req-1", run_id="run-1", task_id="task-1", context_scope="default",
        task_attributes={"agent_thread_id": "thread-1"},
    )
    agent = SimpleNamespace(
        root=tmp_path, home_paths=SimpleNamespace(owner_home_dir=tmp_path),
        _current_run_params=params, _current_user_prompt="请先处理最重要的待办",
        _main_agent_run_id="run-1",
    )
    rows = [
        {"id": "todo-a", "title": "整理资料", "status": "pending"},
        {"id": "todo-b", "title": "核对结果", "status": "in_progress"},
        {"id": "todo-c", "title": "已完成的事", "status": "done"},
    ]
    write_task_progress(tmp_path, "run-1", with_task_progress_display_plan(
        {"items": rows}, generation_id="req-1", item_ids=[row["id"] for row in rows],
    ))
    return agent, params, tmp_path


def install(monkeypatch, *, mode="apply", status="success", choice="todo_2", mutate=None, error=None):
    calls = []
    monkeypatch.setattr(module, "POINT_RUNTIME_SCOPES", {module._POINT: "thread"})

    def begin(_agent, params, **kwargs):
        assert kwargs["operation_id"].startswith("planning:")
        return SimpleNamespace(error_code="", deadline=time.monotonic() + 5,
                               enabled_points=() if mode == "off" else (module._POINT,))

    def decide(_agent, params, stage, **kwargs):
        calls.append((params, kwargs))
        if error is not None:
            raise error
        response = DecisionResponse(
            DecisionBinding(module._POINT, "owner", "operation", "policy", kwargs["candidates_revision"]),
            "digest", "decision-model", "decision-model",
            (DecisionAnswer("priority", "choice", choice),), b"{}",
        )
        if mutate is not None:
            response = mutate(response)
        return SimpleNamespace(
            mode=mode, status=status, may_apply=mode == "apply" and status == "success",
            response=response, deadline=stage.deadline,
        )

    monkeypatch.setattr(module, "begin_decision_stage", begin)
    monkeypatch.setattr(module, "decide", decide)
    monkeypatch.setattr(module, "decision_outcome_is_current", lambda *_args: True)
    return calls


def _read(agent):
    result = TaskProgressTool(agent).execute({"action": "read"})
    assert result.ok
    return json.loads(result.output)


def test_planning_setting_uses_original_owner_thread_and_tui_projection(tmp_path):
    host = host_at(tmp_path)
    thread = host.conversation_store.threads.get_or_create({"canonical_user_id": "alice", "owner_id": "alice"})
    original = execute_decision_settings_operation(host, "read", {}, thread_id=thread.thread_id)
    assert original["effective"]["points"]["planning"]["effective_mode"] == "off"
    assert "points.planning.mode" in _fields({**original, "scope": "thread"})
    patch(host, {"enabled": True}, thread_id=thread.thread_id)
    active = patch(host, {"points.planning.mode": "apply", "points.planning.timeout_seconds": 1.5},
                   thread_id=thread.thread_id, scope="thread")
    assert active["effective"]["points"]["planning"]["effective_mode"] == "apply"
    assert active["effective"]["points"]["planning"]["max_request_seconds"] == 1.5


def test_apply_only_adds_exact_existing_priority_without_reordering_or_writing(prepared, monkeypatch):
    agent, params, root = prepared
    calls = install(monkeypatch)
    before = read_task_progress(root, "run-1")

    result = _read(agent)

    assert result["planning_priority_hint"]["item_id"] == "todo-b"
    assert [row["id"] for row in result["items"]] == ["todo-a", "todo-b", "todo-c"]
    assert read_task_progress(root, "run-1") == before
    assert len(calls) == 1 and calls[0][0] is params
    assert [row["id"] for row in calls[0][1]["state"]["todos"]] == ["todo-a", "todo-b"]
    assert set(calls[0][1]["questions"]["priority"]["criteria"]) == {
        "todo_1", "todo_2", "not_needed", "no_match", "abstain", "need_data",
    }
    assert not (root / "conversation" / "goals").exists()


def test_existing_child_roster_row_is_not_a_planning_candidate(prepared, monkeypatch):
    agent, _, root = prepared
    agent.subagents = SimpleNamespace(list_runs=lambda: [SimpleNamespace(id="child-run-1")])
    write_task_progress(root, "run-1", with_task_progress_display_plan(
        {"items": [{"id": "child-run-1", "title": "子代理展示", "status": "in_progress"}]},
        generation_id="req-1", item_ids=["child-run-1"],
    ))
    calls = install(monkeypatch)

    assert _read(agent)["planning_priority_hint"]["item_id"] == "todo-b"
    assert [row["id"] for row in calls[0][1]["state"]["todos"]] == ["todo-a", "todo-b"]


@pytest.mark.parametrize("mode,status", [
    ("off", "off"), ("observe", "success"), ("apply", "deadline"),
    ("apply", "cooldown"), ("apply", "error"), ("apply", "stale"),
])
def test_off_observe_and_failures_preserve_original_result(prepared, monkeypatch, mode, status):
    agent, _, root = prepared
    calls = install(monkeypatch, mode=mode, status=status)
    baseline = read_task_progress(root, "run-1")

    result = _read(agent)

    assert "planning_priority_hint" not in result
    assert [row["id"] for row in result["items"]] == ["todo-a", "todo-b", "todo-c"]
    assert read_task_progress(root, "run-1") == baseline
    assert len(calls) == (0 if mode == "off" else 1)


@pytest.mark.parametrize("choice", ["not_needed", "no_match", "abstain", "need_data", "made_up"])
def test_nonselection_never_changes_the_plan(prepared, monkeypatch, choice):
    agent, _, root = prepared
    install(monkeypatch, choice=choice)
    baseline = read_task_progress(root, "run-1")
    assert "planning_priority_hint" not in _read(agent)
    assert read_task_progress(root, "run-1") == baseline


@pytest.mark.parametrize("change", ["missing", "duplicate", "error", "kind", "revision"])
def test_bad_answer_cannot_supply_an_item_id(prepared, monkeypatch, change):
    agent, _, _ = prepared

    def mutate(response):
        if change == "missing":
            return replace(response, answers=())
        if change == "duplicate":
            return replace(response, answers=(response.answers[0], response.answers[0]))
        if change == "revision":
            return replace(response, binding=replace(response.binding, candidates_revision="old"))
        answer = replace(response.answers[0], **(
            {"error_code": "invalid_answer"} if change == "error" else {"kind": "score"}
        ))
        return replace(response, answers=(answer,))

    install(monkeypatch, mutate=mutate)
    assert "planning_priority_hint" not in _read(agent)


@pytest.mark.parametrize("change", ["title", "status", "generation", "request"])
def test_changed_canonical_plan_or_request_rejects_late_advice(prepared, monkeypatch, change):
    agent, params, root = prepared

    def mutate(response):
        if change == "request":
            agent._current_user_prompt = "改为另一个请求"
            return response
        if change == "generation":
            update = with_task_progress_display_plan({"items": [{"id": "todo-a", "status": "pending"}]},
                                                     generation_id="req-2", item_ids=["todo-a", "todo-b"])
        elif change == "title":
            update = {"items": [{"id": "todo-b", "title": "新标题", "status": "in_progress", "correction": True}]}
        else:
            update = {"items": [{"id": "todo-b", "status": "done"}]}
        write_task_progress(root, "run-1", update)
        return response

    install(monkeypatch, mutate=mutate)
    assert "planning_priority_hint" not in _read(agent)


@pytest.mark.parametrize("change", ["historical", "child", "no_plan", "single", "oversized_request"])
def test_unproven_or_unhelpful_scope_does_not_call_jev(prepared, monkeypatch, change):
    agent, params, root = prepared
    calls = install(monkeypatch)
    target = "run-1"
    if change == "historical":
        target = "old-run"
        write_task_progress(root, target, with_task_progress_display_plan(
            {"items": [{"id": "x", "title": "X", "status": "pending"},
                       {"id": "y", "title": "Y", "status": "pending"}]},
            generation_id="old-req", item_ids=["x", "y"],
        ))
    elif change == "child":
        monkeypatch.setattr(module, "current_subagent_run_id", lambda _agent: "child-1")
    elif change == "no_plan":
        target = "empty-run"
        params.run_id = target
        agent._main_agent_run_id = target
    elif change == "single":
        write_task_progress(root, target, with_task_progress_display_plan(
            {}, generation_id="req-2", item_ids=["todo-a"],
        ))
    else:
        agent._current_user_prompt = "长" * 1025
    result = TaskProgressTool(agent).execute({"action": "read", **({"run_id": target} if change == "historical" else {})})
    assert result.ok and "planning_priority_hint" not in json.loads(result.output)
    assert not calls


def test_optional_provider_error_retains_original_but_user_stop_propagates(prepared, monkeypatch):
    agent, _, _ = prepared
    install(monkeypatch, error=RuntimeError("provider unavailable"))
    assert "planning_priority_hint" not in _read(agent)

    install(monkeypatch, error=ToolCancelled("stopped"))
    with pytest.raises(ToolCancelled):
        _read(agent)
