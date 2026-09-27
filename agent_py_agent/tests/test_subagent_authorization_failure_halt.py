"""子代理授权阶段同码连续失败即停并通知父代理（2026-09-27 真实缺口 B）。

真机：子代理反复重试被 PATH_OWNER_SCOPE_BLOCKED 拦下的路径，4 个子代理各卡约 20 分钟、父代理无告警。
原因（已查清）：主代理的重复失败机制对子代理同样生效，但默认只给返工提示并每 15 次清段；显式硬门默认关，
即使打开也只收成 unfinished→PENDING，随后被立即重派、失败计数清零。本文件钉住：
1. 仅 task_local 子代理、仅授权阶段、同一错误码连续达阈值（复用 repeated_failure_halt_threshold）才收口；
2. 收口为 blocked + REPEATED_TOOL_AUTHORIZATION_FAILURE，runner 落 BLOCKED 而不是 PENDING；
3. 父代理经原生命周期事件拿到结构化原因码、工具、错误码、次数和参数名（不含参数值）。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.agent_core._tool_loop_service import (
    _mark_authorization_failure_halt,
    _mark_repeated_failure_halt,
)
from agent_py_agent.agent.agent_core.runner.prompt_context_summary import _direct_child_prompt_row
from agent_py_agent.agent.agent_core.runtime.guidance import _task_event_payload
from agent_py_agent.agent.agent_core.subagent.finalize_helpers import (
    FinalizedRunnerRecordRequest,
    record_finalized_runner_result,
)
from agent_py_agent.agent.agent_core.subagent.params import SubagentFinalizeParams
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.contracts.subagent_completion import (
    TOOL_FAILURE_HALT_SCHEMA_VERSION,
    _subagent_completion_item,
    completion_tool_failure_halt_facts,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.subagents.runner_completion_payload import completion_handoff_payload
from agent_py_agent.agent.subagents.tool_failure_ledger import REPEATED_TOOL_AUTHORIZATION_FAILURE
from agent_py_agent.tests.test_agent.backends import _TestNativeBackend

_BLOCKED = "PATH_OWNER_SCOPE_BLOCKED"


def _archive(tool: str, *, ok: bool = False, code: str = _BLOCKED, stage: str = "authorization") -> dict:
    record = {"tool": tool, "ok": ok, "handler_executed": ok, "parameters": {"path": f"/outside/{tool}.md"}}
    if not ok:
        record.update({"error_code": code, "failure_stage": stage})
    return record


def _params(archive: list[dict], *, scope: str = "task_local", threshold: int = 3) -> SimpleNamespace:
    return SimpleNamespace(
        context_scope=scope,
        archive_tool_calls=list(archive),
        task_attributes={"repeated_failure_halt_threshold": threshold, "hard_failure_halt_enabled": False},
        runtime_guard_policy=None,
        repeated_failure_halt=None,
        repeated_failure_halt_exhausted=False,
        authorization_failure_halt=None,
        tool_context=[],
        executed_tools=[],
    )


def _record(params, tool: str = "read_file", *, ok: bool = False, code: str = _BLOCKED, stage: str = "authorization"):
    result = SimpleNamespace(
        ok=ok,
        tool_name=tool,
        error_code="" if ok else code,
        error_category="",
        failure_stage="" if ok else stage,
        handler_executed=ok,
        output="",
    )
    call = SimpleNamespace(tool_name=tool, arguments={"path": f"/outside/{tool}-now.md"})
    return SimpleNamespace(params=params, call=call, result=result)


# ---------------------------------------------------------------------------
# 1. 工具循环里的收口判定
# ---------------------------------------------------------------------------


def test_child_halts_when_same_code_fails_at_authorization_across_tools():
    params = _params([_archive("search_text", ok=True), _archive("list_files"), _archive("search_text")])
    record = _record(params, "read_file")

    _mark_authorization_failure_halt(record)

    halt = params.authorization_failure_halt
    assert halt["consecutive_failures"] == 3
    assert halt["error_code"] == _BLOCKED
    assert halt["failure_stage"] == "authorization"
    assert halt["tools"] == ["list_files", "search_text", "read_file"]
    assert halt["argument_names"] == ["path"]
    assert params.repeated_failure_halt == ("read_file", f"code:{_BLOCKED}", 3)
    assert params.repeated_failure_halt_exhausted is True
    assert "交给直属父级" in params.tool_context[-1]


def test_below_threshold_or_threshold_zero_keeps_child_running():
    params = _params([_archive("list_files")])
    _mark_authorization_failure_halt(_record(params))
    assert params.authorization_failure_halt is None and params.repeated_failure_halt is None
    disabled = _params([_archive("list_files")] * 10, threshold=0)
    _mark_authorization_failure_halt(_record(disabled))
    assert disabled.authorization_failure_halt is None


def test_main_agent_and_execution_stage_failures_are_not_halted():
    main = _params([_archive("read_file")] * 20, scope="default")
    _mark_authorization_failure_halt(_record(main))
    assert main.authorization_failure_halt is None and main.repeated_failure_halt is None
    handler_failures = _params([_archive("read_file", stage="execution")] * 20)
    _mark_authorization_failure_halt(_record(handler_failures, stage="execution"))
    assert handler_failures.authorization_failure_halt is None


def test_success_or_other_code_breaks_the_authorization_streak():
    interrupted = _params([_archive("read_file"), _archive("list_files", ok=True), _archive("read_file")])
    _mark_authorization_failure_halt(_record(interrupted))
    assert interrupted.authorization_failure_halt is None
    other_code = _params([_archive("read_file", code="PATH_CROSS_OWNER_BLOCKED"), _archive("read_file")])
    _mark_authorization_failure_halt(_record(other_code))
    assert other_code.authorization_failure_halt is None


def test_later_success_in_same_batch_revokes_the_halt():
    params = _params([_archive("list_files"), _archive("search_text")])
    _mark_authorization_failure_halt(_record(params))
    assert params.authorization_failure_halt is not None

    _mark_authorization_failure_halt(_record(params, "list_files", ok=True))

    assert params.authorization_failure_halt is None
    assert params.repeated_failure_halt is None
    assert params.repeated_failure_halt_exhausted is False


def test_halt_suppresses_the_contradictory_repair_hint():
    params = _params([_archive("read_file"), _archive("read_file")], threshold=3)
    record = _record(params)
    agent = SimpleNamespace(
        _tool_call_guardrail_records=tuple(
            {"tool_name": "read_file", "args_hash": f"a{i}", "failed": True, "failure_class": f"code:{_BLOCKED}"}
            for i in range(3)
        )
    )

    _mark_authorization_failure_halt(record)
    _mark_repeated_failure_halt(agent, record)

    joined = "\n".join(params.tool_context)
    assert "系统不会因此结束当前任务" not in joined
    assert params.repeated_failure_halt == ("read_file", f"code:{_BLOCKED}", 3)


def test_closeout_is_blocked_even_when_the_summary_reply_is_truncated(monkeypatch):
    from agent_py_agent.agent.agent_core import _tool_loop_service as loop
    from agent_py_agent.agent.turn_end import infer_turn_end_reason

    calls = []

    def fake_halt(agent, params, tool_rounds, *, reason="", runtime_reason="TOOL_ROUND_LIMIT_REACHED"):
        calls.append((reason, runtime_reason))
        return "prompt", ModelResponse(
            text="收口说明", backend="fake", runtime_status="unfinished", runtime_reason=runtime_reason,
            stop_reason="max_tokens",
        )

    monkeypatch.setattr(loop, "_final_response_after_halt", fake_halt)
    params = _params([])
    params.authorization_failure_halt = {"consecutive_failures": 3}
    _prompt, response = loop._final_response_after_repeated_failure(object(), params, 3)
    assert calls == [("repeated_authorization_failure", REPEATED_TOOL_AUTHORIZATION_FAILURE)]
    assert (response.runtime_status, response.runtime_reason) == ("blocked", REPEATED_TOOL_AUTHORIZATION_FAILURE)
    assert infer_turn_end_reason(
        explicit=response.turn_end_reason, runtime_status=response.runtime_status,
        runtime_reason=response.runtime_reason, stop_reason=response.stop_reason,
    ) == "blocked"
    # 部署者显式硬门仍走原 unfinished 收口，不受影响。
    params.authorization_failure_halt = None
    _prompt, legacy = loop._final_response_after_repeated_failure(object(), params, 3)
    assert calls[-1] == ("repeated_failure_exhausted", "REPEATED_TOOL_FAILURE_EXHAUSTED")
    assert legacy.runtime_status == "unfinished"


# ---------------------------------------------------------------------------
# 2. finalize 复算与合同投影
# ---------------------------------------------------------------------------


def _finalize(runtime_reason: str, runtime_status: str = "blocked"):
    captured = SimpleNamespace(params=None)

    def record_runner_result(params):
        captured.params = params
        return SimpleNamespace(status=params.status, turn_end_reason=params.turn_end_reason)

    agent = SimpleNamespace(subagents=SimpleNamespace(runner_result=SimpleNamespace(record_runner_result=record_runner_result)))
    params = SubagentFinalizeParams(
        run_id="worker-1",
        active_attempt_id="attempt-1",
        result=SimpleNamespace(
            prompt="p", response="被拦下", backend="test", runtime_status=runtime_status,
            runtime_reason=runtime_reason, turn_end_reason="", tool_rounds=3, executed_tools=[],
            archive_tool_calls=[_archive("read_file", ok=True), _archive("list_files"), _archive("read_file")],
        ),
        context=SimpleNamespace(goal="只读调研"),
        prompt="runner prompt",
    )
    result = record_finalized_runner_result(FinalizedRunnerRecordRequest(agent, params))
    return result, captured.params


def test_finalize_records_blocked_status_and_structured_halt():
    result, recorded = _finalize(REPEATED_TOOL_AUTHORIZATION_FAILURE)
    assert (result.status, result.turn_end_reason) == ("BLOCKED", "blocked")
    assert recorded.tool_failure_halt == {
        "schema_version": TOOL_FAILURE_HALT_SCHEMA_VERSION,
        "consecutive_failures": 2,
        "reason_code": REPEATED_TOOL_AUTHORIZATION_FAILURE,
        "tool": "read_file",
        "error_code": _BLOCKED,
        "failure_stage": "authorization",
        "tools": ["list_files", "read_file"],
        "argument_names": ["path"],
    }


def test_finalize_other_reasons_carry_no_halt():
    _result, recorded = _finalize("REPEATED_TOOL_FAILURE_EXHAUSTED", runtime_status="unfinished")
    assert recorded.tool_failure_halt is None


def test_contract_projection_rejects_bad_halts_and_never_copies_values():
    good = {
        "schema_version": TOOL_FAILURE_HALT_SCHEMA_VERSION, "consecutive_failures": 3,
        "reason_code": REPEATED_TOOL_AUTHORIZATION_FAILURE, "tool": "read_file",
        "error_code": _BLOCKED, "failure_stage": "authorization",
        "tools": ["read_file"] * 3 + [f"tool-{i}" for i in range(20)],
        "argument_names": ["path"], "path_values": ["/secret/x.md"],
    }
    projected = completion_tool_failure_halt_facts({"tool_failure_halt": good})["tool_failure_halt"]
    assert "path_values" not in projected
    assert projected["tools"][0] == "read_file" and len(projected["tools"]) == 8
    for bad in ({**good, "schema_version": "old"}, {**good, "consecutive_failures": 0},
                {**good, "consecutive_failures": True}, "not-a-dict"):
        assert completion_tool_failure_halt_facts({"tool_failure_halt": bad}) == {}


def test_handoff_carries_halt_only_for_blocked_children():
    halt = {
        "schema_version": TOOL_FAILURE_HALT_SCHEMA_VERSION, "consecutive_failures": 3,
        "reason_code": REPEATED_TOOL_AUTHORIZATION_FAILURE, "tool": "read_file",
        "error_code": _BLOCKED, "failure_stage": "authorization", "tools": ["read_file"], "argument_names": ["path"],
    }
    task = SimpleNamespace(
        status="BLOCKED", result="", latest_summary="", artifact_refs=[], agent_run_final_report_md="",
        attributes={"tool_failure_ledger": {"updated_at": 1.0, "failures": [], "halt": halt}},
    )
    assert completion_handoff_payload(task)["tool_failure_halt"] == halt
    task.status = "DONE"
    assert "tool_failure_halt" not in completion_handoff_payload(task)


def test_every_parent_consumer_keeps_the_structured_halt():
    halt = {
        "schema_version": TOOL_FAILURE_HALT_SCHEMA_VERSION, "consecutive_failures": 4,
        "reason_code": REPEATED_TOOL_AUTHORIZATION_FAILURE, "tool": "list_files",
        "error_code": _BLOCKED, "failure_stage": "authorization", "tools": ["list_files"], "argument_names": ["path"],
    }
    metadata = {"task_id": "child-1", "status": "BLOCKED", "turn_end_reason": "blocked",
                "completion_schema_version": "subagent-completion.v1", "tool_failure_halt": halt}
    event = SimpleNamespace(
        event_type="subagent_runner_finished", root_task_id="root-1", parent_agent_id="root-1",
        source_agent_id="child-1", observed_at=1.0, metadata=metadata,
        wake_signal_id="w1", reason="subagent_runner_finished", created_at=1.0,
    )
    # 前台活动回合事件、后台完成清单、递归父级直属孩子行都经同一合同投影保留收口事实。
    assert _task_event_payload(event)["tool_failure_halt"] == halt
    assert _subagent_completion_item(event, {"root-1"})[0][2]["tool_failure_halt"] == halt
    assert _direct_child_prompt_row({"run_id": "child-1", "status": "BLOCKED", "tool_failure_halt": halt})[
        "tool_failure_halt"
    ] == halt


# ---------------------------------------------------------------------------
# 3. 端到端：真实子代理 runner + 假模型反复读墙外路径
# ---------------------------------------------------------------------------


class _OutsideReadBackend(_TestNativeBackend):
    """测试用后端：每轮都换一个 owner home 外的路径调用 read_file（模型常见的"换参数重试"）。"""

    name = "outside_read_backend"

    def __init__(self, outside: Path):
        self.outside = outside
        self.calls = 0
        self.last_request = ""

    def generate(self, prompt: str, on_chunk=None, **kwargs) -> ModelResponse:
        self.calls += 1
        self.last_request = prompt + json.dumps(kwargs.get("messages") or [], ensure_ascii=False)
        return ModelResponse(
            text="",
            backend=self.name,
            tool_use_blocks=[
                {
                    "id": f"call-outside-{self.calls}",
                    "name": "read_file",
                    "input": {"path": str(self.outside / f"source-{self.calls}.md")},
                }
            ],
        )


# 函数用途: 搭一个真实父代理 + 绑定会话线程的子代理，假模型每轮换一个 owner home 外的路径读文件。
def _outside_reading_child(tmp_path) -> SimpleNamespace:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside-worktree"
    workspace.mkdir()
    outside.mkdir()
    # 轮数上限只是保护：没有授权收口时本用例会在 8 轮后失败，而不是跑满默认 5000 轮。
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo", my_agent_home=str(tmp_path / "home"), subagent_workspace="subs", max_tool_rounds=8,
        ),
        workspace,
    )
    agent.backend = _OutsideReadBackend(outside)
    task = agent.subagents.create_run(
        goal="只读调研 owner home 外的工作树",
        thought="读取源文件",
        plan=["read_file"],
        allowed_tools=["read_file"],
        attributes={"repeated_failure_halt_threshold": 3},
    )
    store = agent.conversation_store
    thread = store.threads.get_or_create(
        {"canonical_user_id": "user-1", "channel": "internal",
         "channel_conversation_id": "thread-1", "channel_user_id": "user-1"}
    )
    store.tasks.bind({"thread_id": thread.thread_id, "task_id": task.id, "goal": task.goal, "status": "active"})
    return SimpleNamespace(agent=agent, task=task, thread=thread, outside=outside)


def test_child_stops_blocked_and_parent_receives_structured_lifecycle_event(tmp_path):
    case = _outside_reading_child(tmp_path)

    result = case.agent.run_subagent(case.task.id, dry_run=False, probe=False)

    # 3 次被拦的工具轮 + 1 次收口回复；不再空转到轮数上限。
    assert case.agent.backend.calls == 4
    # 收口请求里只有授权收口说明，没有"系统不会因此结束当前任务"的相反返工提示。
    assert "交给直属父级处理" in case.agent.backend.last_request
    assert "系统不会因此结束当前任务" not in case.agent.backend.last_request
    assert (result.status, result.turn_end_reason) == ("BLOCKED", "blocked")
    stored = case.agent.subagents.load(case.task.id)
    assert stored.status == "BLOCKED"
    halt = stored.attributes["tool_failure_ledger"]["halt"]
    assert halt["reason_code"] == REPEATED_TOOL_AUTHORIZATION_FAILURE
    assert (halt["tool"], halt["error_code"], halt["failure_stage"]) == ("read_file", _BLOCKED, "authorization")
    assert halt["consecutive_failures"] == 3
    assert halt["argument_names"] == ["path"]
    assert str(case.outside) not in json.dumps(halt, ensure_ascii=False), "事件只给参数名，不带路径值"

    store = case.agent.conversation_store
    signals = store.wakes.pending()
    assert len(signals) == 1
    assert signals[0].metadata["status"] == "BLOCKED"
    assert signals[0].metadata["tool_failure_halt"] == halt
    observation = store.observations.recent(case.thread.thread_id)[-1]
    assert REPEATED_TOOL_AUTHORIZATION_FAILURE in observation.summary
    assert "参数名：path" in observation.summary
