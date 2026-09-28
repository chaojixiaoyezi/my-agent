"""子代理同一调用同一失败硬上限：优先级、收口回复、finalize 复算、父级事实与 fake LLM 子代理端到端。

复用主代理的 identical_failure 模块与 repeated_failure_halt_threshold；子代理按 blocked 交直属父级，
同一次调用同时满足授权阶段收口时按授权阶段收口。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from agent_py_agent.agent.agent_core._tool_loop_service import (
    _final_response_after_repeated_failure,
    _mark_tool_call_halts,
)
from agent_py_agent.agent.agent_core.subagent.finalize_helpers import (
    FinalizedRunnerRecordRequest,
    record_finalized_runner_result,
)
from agent_py_agent.agent.agent_core.subagent.params import SubagentFinalizeParams
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.contracts.subagent_completion import TOOL_FAILURE_HALT_SCHEMA_VERSION
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.subagents.runner_completion_wake import _tool_failure_halt_summary
from agent_py_agent.agent.subagents.tool_failure_ledger import (
    REPEATED_IDENTICAL_TOOL_FAILURE,
    REPEATED_TOOL_AUTHORIZATION_FAILURE,
)
from agent_py_agent.tests.test_agent.backends import _TestNativeBackend

_THRESHOLD = 3


def _child_params(archive=None):
    return SimpleNamespace(
        context_scope="task_local", archive_tool_calls=list(archive or []), identical_failure_streak=None,
        identical_failure_halt=None, repeated_failure_halt=None, repeated_failure_halt_exhausted=False,
        authorization_failure_halt=None, tool_context=[], executed_tools=[], runtime_guard_policy=None,
        task_attributes={"repeated_failure_halt_threshold": _THRESHOLD, "hard_failure_halt_enabled": False},
    )


def _archive(stage="execution", code="FILE_NOT_FOUND"):
    return {"tool": "read_file", "ok": False, "handler_executed": stage != "authorization",
            "parameters": {"path": "notes/missing.md"}, "error_code": code, "failure_stage": stage}


def _record(params, stage="execution", code="FILE_NOT_FOUND"):
    call = SimpleNamespace(tool_name="read_file", arguments={"path": "notes/missing.md"}, args_hash="sha256:same")
    result = SimpleNamespace(ok=False, tool_name="read_file", error_code=code, error_category="", failure_stage=stage,
                             handler_executed=stage != "authorization", output="")
    return SimpleNamespace(params=params, call=call, result=result)


# 函数用途: 经工具循环真实的收口标记入口（固定优先级）喂同一个失败调用 count 次，并同步归档。
def _feed_both(params, count, stage):
    agent = SimpleNamespace(_tool_call_guardrail_records=())
    for _ in range(count):
        record = _record(params, stage=stage, code="PATH_OWNER_SCOPE_BLOCKED" if stage == "authorization" else "FILE_NOT_FOUND")
        _mark_tool_call_halts(agent, record)
        params.archive_tool_calls.append(_archive(stage, record.result.error_code))
    return params


def test_same_call_meeting_both_rules_closes_as_authorization_halt():
    params = _feed_both(_child_params(), _THRESHOLD, "authorization")
    assert params.authorization_failure_halt is not None
    assert params.identical_failure_halt is None
    assert params.repeated_failure_halt == ("read_file", "code:PATH_OWNER_SCOPE_BLOCKED", _THRESHOLD)


def test_non_authorization_identical_failures_close_as_identical_halt():
    params = _feed_both(_child_params(), _THRESHOLD, "execution")
    assert params.authorization_failure_halt is None
    assert params.identical_failure_halt.to_dict()["count"] == _THRESHOLD


def test_child_closeout_is_blocked_host_reply_without_model_call(monkeypatch):
    from agent_py_agent.agent.agent_core import _tool_loop_service

    monkeypatch.setattr(_tool_loop_service, "build_tool_loop_prompt", lambda agent, params: "PROMPT")
    params = _feed_both(_child_params(), _THRESHOLD, "execution")
    _prompt, response = _final_response_after_repeated_failure(SimpleNamespace(backend=None), params, 3)
    assert (response.runtime_status, response.turn_end_reason, response.runtime_reason) == (
        "blocked", "blocked", REPEATED_IDENTICAL_TOOL_FAILURE)
    assert "直属父代理" in response.text and "read_file" in response.text


def _finalize(archive, reason=REPEATED_IDENTICAL_TOOL_FAILURE):
    captured = SimpleNamespace(params=None)

    def record_runner_result(params):
        captured.params = params
        return SimpleNamespace(status=params.status, turn_end_reason=params.turn_end_reason)

    agent = SimpleNamespace(subagents=SimpleNamespace(runner_result=SimpleNamespace(record_runner_result=record_runner_result)))
    params = SubagentFinalizeParams(
        run_id="worker-1", active_attempt_id="attempt-1",
        result=SimpleNamespace(prompt="p", response="停下", backend="test", runtime_status="blocked",
                               runtime_reason=reason, turn_end_reason="blocked", tool_rounds=3,
                               executed_tools=[], archive_tool_calls=archive),
        context=SimpleNamespace(goal="读资料"), prompt="runner prompt",
    )
    return record_finalized_runner_result(FinalizedRunnerRecordRequest(agent, params)), captured.params


def test_finalize_recomputes_identical_halt_facts_from_archive():
    ok = {"tool": "list_files", "ok": True, "parameters": {"path": "."}}
    result, recorded = _finalize([ok] + [_archive()] * _THRESHOLD)
    assert (result.status, result.turn_end_reason) == ("BLOCKED", "blocked")
    assert recorded.tool_failure_halt == {
        "schema_version": TOOL_FAILURE_HALT_SCHEMA_VERSION, "consecutive_failures": _THRESHOLD,
        "reason_code": REPEATED_IDENTICAL_TOOL_FAILURE, "tool": "read_file", "error_code": "FILE_NOT_FOUND",
        "failure_stage": "execution", "tools": ["read_file"], "argument_names": ["path"],
    }
    _result, tail_ok = _finalize([_archive()] * _THRESHOLD + [ok])
    assert tail_ok.tool_failure_halt is None


def test_parent_wake_summary_names_the_structured_reason():
    def task_with(reason):
        halt = {"schema_version": TOOL_FAILURE_HALT_SCHEMA_VERSION, "consecutive_failures": 3, "reason_code": reason,
                "tool": "read_file", "error_code": "FILE_NOT_FOUND", "failure_stage": "execution",
                "tools": ["read_file"], "argument_names": ["path"]}
        return SimpleNamespace(attributes={"tool_failure_ledger": {"updated_at": 1.0, "failures": [], "halt": halt}})

    identical = _tool_failure_halt_summary(task_with(REPEATED_IDENTICAL_TOOL_FAILURE), "BLOCKED")
    assert REPEATED_IDENTICAL_TOOL_FAILURE in identical and "以相同参数反复调用" in identical
    assert "授权阶段" not in identical
    assert "授权阶段" in _tool_failure_halt_summary(task_with(REPEATED_TOOL_AUTHORIZATION_FAILURE), "BLOCKED")
    assert _tool_failure_halt_summary(task_with(REPEATED_IDENTICAL_TOOL_FAILURE), "DONE") == ""


class _SameMissingReadBackend(_TestNativeBackend):
    """测试用后端：每轮都原样读取同一个工作区内不存在的文件（执行阶段失败，不是授权失败）。"""

    name = "same_missing_read_backend"

    def __init__(self):
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None, **kwargs) -> ModelResponse:
        self.calls += 1
        return ModelResponse(text="", backend=self.name, tool_use_blocks=[
            {"id": f"call-same-{self.calls}", "name": "read_file", "input": {"path": "notes/missing.md"}}])


def test_fake_llm_child_stops_blocked_and_parent_wake_carries_identical_reason(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"),
                                    subagent_workspace="subs", max_tool_rounds=10), workspace)
    agent.backend = _SameMissingReadBackend()
    task = agent.subagents.create_run(goal="读资料", thought="读取", plan=["read_file"], allowed_tools=["read_file"],
                                      attributes={"repeated_failure_halt_threshold": _THRESHOLD})
    store = agent.conversation_store
    thread = store.threads.get_or_create({"canonical_user_id": "user-1", "channel": "internal",
                                          "channel_conversation_id": "thread-1", "channel_user_id": "user-1"})
    store.tasks.bind({"thread_id": thread.thread_id, "task_id": task.id, "goal": task.goal, "status": "active"})

    result = agent.run_subagent(task.id, dry_run=False, probe=False)

    assert agent.backend.calls == _THRESHOLD  # 第 N 次同调用同失败后宿主直接收口，不再请求收口模型
    assert (result.status, result.turn_end_reason) == ("BLOCKED", "blocked")
    halt = agent.subagents.load(task.id).attributes["tool_failure_ledger"]["halt"]
    assert (halt["reason_code"], halt["tool"], halt["consecutive_failures"]) == (
        REPEATED_IDENTICAL_TOOL_FAILURE, "read_file", _THRESHOLD)
    assert halt["argument_names"] == ["path"] and "missing.md" not in json.dumps(halt, ensure_ascii=False)
    signals = store.wakes.pending()
    assert len(signals) == 1 and signals[0].metadata["tool_failure_halt"] == halt
    observation = store.observations.recent(thread.thread_id)[-1]
    assert REPEATED_IDENTICAL_TOOL_FAILURE in observation.summary and "以相同参数反复调用" in observation.summary
