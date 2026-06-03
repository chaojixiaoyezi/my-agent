from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runner.stage_trace import (
    RunnerModelStageTraceRequest,
    trace_runner_model_request_started,
)
from agent_py_agent.agent.backend import BaseBackend, ModelResponse
from agent_py_agent.agent.backends.errors import ProviderTimeoutError, ProviderTransientError
from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.manager_runner_results import RecordRunnerResultParams
from agent_py_agent.agent.subagents.services.hierarchy.recovery import HierarchyRecoveryRequest
from agent_py_agent.agent.subagents.services.hierarchy.scheduler import (
    HierarchyChildSpec,
    HierarchyScheduleRequest,
)


def _trace_records(workspace):
    trace_file = workspace / "debug_traces" / "subagent_trace.jsonl"
    return [json.loads(line) for line in trace_file.read_text(encoding="utf-8").splitlines()]


class _TraceToolBackend(BaseBackend):
    name = "trace_tool_backend"

    def __init__(self):
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                text='[TOOL_CALL]\n{"tool":"list_files","path":"."}\n[/TOOL_CALL]',
                backend=self.name,
            )
        return ModelResponse(
            text=(
                "[SUBAGENT_RESULT]\n"
                "{\n"
                '  "status": "DONE",\n'
                '  "summary": "trace runner finished.",\n'
                '  "used_tools": ["list_files"],\n'
                '  "used_skills": [],\n'
                '  "evidence": [{"kind": "tool", "summary": "listed workspace", "ok": true}],\n'
                '  "capability_requests": [],\n'
                '  "artifacts": [],\n'
                '  "tests": [],\n'
                '  "patches": [],\n'
                '  "lessons": [],\n'
                '  "next_actions": [],\n'
                '  "blocked_reason": "",\n'
                '  "failure_type": ""\n'
                "}\n"
                "[/SUBAGENT_RESULT]"
            ),
            backend=self.name,
        )


class _FailingTraceBackend(BaseBackend):
    name = "failing_trace_backend"

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        raise RuntimeError("trace backend failed")


class _ProviderTimeoutBackend(BaseBackend):
    name = "provider_timeout_backend"

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        raise ProviderTimeoutError("模型接口请求超时: request_timeout=17s")


class _ProviderTransientBackend(BaseBackend):
    name = "provider_transient_backend"

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        raise ProviderTransientError("网络请求失败: 模型接口临时断开")


def test_subagent_debug_trace_is_off_by_default(tmp_path):
    """默认等级 0 不写调试追踪文件，避免普通使用增加噪音。"""
    manager = SubAgentManager(tmp_path)

    manager.create_run(goal="write proof", thought="observe", plan=["create"])

    assert not (tmp_path / "debug_traces" / "subagent_trace.jsonl").exists()


def test_subagent_debug_trace_records_task_creation_when_enabled(tmp_path):
    """开启等级后，创建子代理会写 refs-only 调试事件到内部 runtime。"""
    manager = SubAgentManager(tmp_path, debug_trace_level=1)

    task = manager.create_run(goal="write proof file", thought="observe", plan=["create"], role="worker", depth=2)

    records = _trace_records(tmp_path)
    assert len(records) == 1
    assert records[0]["created_at"] > 0
    assert {key: records[0][key] for key in records[0] if key != "created_at"} == {
        "event_type": "task_created",
        "level": 1,
        "run_id": task.id,
        "root_id": task.root_id,
        "parent_id": "",
        "depth": 2,
        "role": "worker",
        "status": "PLANNING",
        "verification_status": "UNVERIFIED",
        "goal_preview": "write proof file",
    }


def test_runner_stage_trace_reports_current_run_load_error(caplog: pytest.LogCaptureFixture) -> None:
    """runner 追踪读不到当前 run 时要有 warning，不能静默丢掉心跳线索。"""

    class BrokenSubagents:
        def load(self, run_id: str):
            raise OSError("canonical state unavailable")

    agent = SimpleNamespace(_current_subagent_run_id="run-1", subagents=BrokenSubagents())
    request = RunnerModelStageTraceRequest(agent=agent, params=SimpleNamespace(), tool_rounds=1, prompt="hello")

    with caplog.at_level(logging.WARNING):
        trace_runner_model_request_started(request)

    assert "runner stage trace failed" in caplog.text
    assert "runner_stage_trace.subagents.load" in caplog.text
    assert "canonical state unavailable" in caplog.text


def test_subagent_debug_trace_records_runner_result_when_enabled(tmp_path):
    """开启等级后，runner 结果会写入 refs-only 调试事件，便于真实 E2E 排障。"""
    manager = SubAgentManager(tmp_path, debug_trace_level=2)
    task = manager.create_run(goal="write proof file", thought="observe", plan=["create"])

    manager.record_runner_result(
        RecordRunnerResultParams(
            run_id=task.id,
            dry_run=False,
            ok=True,
            message="done",
            status="DONE",
            verification_status="VERIFIED",
            backend="echo",
            tool_rounds=1,
        )
    )

    records = _trace_records(tmp_path)
    assert [record["event_type"] for record in records] == ["task_created", "runner_result_recorded"]
    runner_record = records[-1]
    assert runner_record["level"] == 2
    assert runner_record["run_id"] == task.id
    assert runner_record["status"] == "DONE"
    assert runner_record["verification_status"] == "VERIFIED"
    assert runner_record["ok"] is True
    assert runner_record["dry_run"] is False
    assert runner_record["backend"] == "echo"
    assert runner_record["tool_rounds"] == 1
    assert runner_record["runner_result_ref"].endswith("RUNNER_RESULT.md")


def test_subagent_debug_trace_records_hierarchy_schedule_when_enabled(tmp_path):
    """等级 2 记录层级调度结果，方便观察父节点实际创建了哪些下一层。"""
    manager = SubAgentManager(tmp_path, debug_trace_level=2)
    root = manager.create_run(goal="root", thought="split", plan=["plan"])

    result = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=root.id,
            child_specs=[
                HierarchyChildSpec(
                    goal="write proof.txt",
                    agent_name="leaf",
                    role="worker",
                    allowed_tools=["write"],
                )
            ],
            apply=True,
            requested_by="root",
        )
    )

    records = _trace_records(tmp_path)
    schedule_record = records[-1]
    assert schedule_record["event_type"] == "hierarchy_schedule_result"
    assert schedule_record["run_id"] == root.id
    assert schedule_record["dry_run"] is False
    assert schedule_record["blocked"] is False
    assert schedule_record["reason"] == "created"
    assert schedule_record["planned_count"] == 1
    assert schedule_record["created_count"] == 1
    assert schedule_record["created_run_ids"] == result.created_run_ids
    assert schedule_record["requested_by"] == "root"


@pytest.mark.skip(reason="旧 final closeout debug trace 已删除")
def test_subagent_debug_trace_records_final_closeout_decision_and_next_action(tmp_path):
    """等级 2 记录最终收口判断和下一动作，便于排查卡在人审还是测试。"""
    manager = SubAgentManager(tmp_path, debug_trace_level=2)
    task = manager.create_run(goal="acceptance", thought="check", plan=["plan"])

    manager.plan_final_closeout(task.id)
    manager.plan_final_closeout_next_action(task.id)

    records = _trace_records(tmp_path)
    assert [record["event_type"] for record in records] == [
        "task_created",
        "final_closeout_decision",
        "final_closeout_next_action",
    ]
    decision_record = records[-2]
    action_record = records[-1]
    assert decision_record["decision"] == "inspect_only"
    assert decision_record["risk_level"] == "low"
    assert decision_record["requires_human_confirmation"] is False
    assert action_record["action"] == "apply_result"
    assert action_record["mutates_task_state"] is True


def test_subagent_debug_trace_records_due_action_and_recovery_reports_at_level_three(tmp_path):
    """等级 3 记录 due/action/recovery 摘要，便于定位父超时子树恢复卡点。"""
    manager = SubAgentManager(tmp_path, debug_trace_level=3)
    root = manager.create_run(goal="root", thought="split", plan=["dispatch"])
    child_id = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=root.id,
            child_specs=[HierarchyChildSpec(goal="child waits", agent_name="child", role="coordinator")],
            apply=True,
        )
    ).created_run_ids[0]
    root_task = manager.load(root.id)
    root_task.status = "TIMEOUT"
    manager.save(root_task)

    manager.due_check()
    manager.plan_actions()
    manager.build_hierarchy_recovery_packet(
        params=HierarchyRecoveryRequest(root_run_id=root.id, include_healthy=False)
    )

    records = _trace_records(tmp_path)
    by_type = {record["event_type"]: record for record in records}
    assert by_type["due_check_report"]["issue_count"] >= 1
    assert by_type["due_check_report"]["summary"]["parent_timeout_with_unfinished_children"] == 1
    assert by_type["action_plan_report"]["action_count"] >= 1
    assert by_type["action_plan_report"]["summary"]["recover_child_after_parent_timeout"] == 1
    recovery = by_type["hierarchy_recovery_packet"]
    assert recovery["root_run_id"] == root.id
    assert recovery["candidate_count"] >= 2
    assert child_id in recovery["candidate_run_ids"]


def test_subagent_debug_trace_records_dispatch_reports_at_level_three(tmp_path):
    """等级 3 记录 dispatch/watch 摘要，但不复制报告正文。"""
    manager = SubAgentManager(tmp_path, debug_trace_level=3)
    task = manager.create_run(goal="dispatch target", thought="observe", plan=["dispatch"])
    record = manager.make_dispatch_record(
        step="runner",
        action="run_subagent",
        run_id=task.id,
        dry_run=True,
        ok=True,
        message="planned",
    )
    manager.write_dispatch_report(manager.build_dispatch_report([record], dry_run=True))
    watch = manager.make_dispatch_watch_record(
        cycle=1,
        dry_run=True,
        ok=True,
        message="watch",
        dispatch_record_count=1,
        dispatch_summary={"run_subagent": 1},
    )
    manager.write_dispatch_watch_report(manager.build_dispatch_watch_report([watch], dry_run=True))

    records = _trace_records(tmp_path)
    by_type = {record["event_type"]: record for record in records}
    assert by_type["dispatch_report"]["record_count"] == 1
    assert by_type["dispatch_report"]["summary"]["run_subagent"] == 1
    assert by_type["dispatch_watch_report"]["record_count"] == 1
    assert by_type["dispatch_watch_report"]["summary"]["dispatch_records"] == 1


def test_subagent_debug_trace_records_runner_model_and_tool_stages(tmp_path):
    """等级 3 记录模型请求/响应和工具调用阶段，便于定位长 runner 卡点。"""
    cfg = AgentConfig(
        enable_tools=True,
        model_backend="echo",
        subagent_workspace="subs",
        subagent_debug_trace_level=3,
        max_tool_rounds=3,
    )
    agent = SimpleAgent(cfg, tmp_path)
    agent.backend = _TraceToolBackend()
    task = agent.subagents.create_run(
        goal="observe runner stages",
        thought="需要一次工具调用再收口。",
        plan=["list", "finalize"],
        allowed_tools=["list_files"],
    )

    result = agent.run_subagent(task.id, dry_run=False, probe=False)

    assert result.ok
    records = _trace_records(tmp_path / "subs")
    event_types = [record["event_type"] for record in records]
    assert event_types.count("runner_model_request_started") == 2
    assert event_types.count("runner_model_response_received") == 2
    assert "runner_tool_call_started" in event_types
    assert "runner_tool_call_finished" in event_types
    tool_started = next(record for record in records if record["event_type"] == "runner_tool_call_started")
    tool_finished = next(record for record in records if record["event_type"] == "runner_tool_call_finished")
    assert tool_started["tool"] == "list_files"
    assert tool_started["payload_keys"] == ["path", "tool"]
    assert "payload" not in tool_started
    assert tool_finished["ok"] is True
    assert tool_finished["output_chars"] > 0


def test_runner_stage_trace_refreshes_active_ancestor_heartbeats(tmp_path):
    """孙级 runner 活动时刷新仍在执行的祖先 heartbeat，避免 nested dispatch 误接管活父级。"""
    manager = SubAgentManager(tmp_path / "subs", debug_trace_level=0)
    old = 10.0
    root, parent, child = _running_trace_hierarchy(manager, old)

    _trace_child_tool_started(manager, root.id, child.id)

    _assert_ancestor_heartbeats_refreshed(manager, [child.id, parent.id, root.id], old)


def _running_trace_hierarchy(manager: SubAgentManager, old: float):
    root = manager.create_run(goal="root", thought="root", plan=["root"], role="coordinator")
    parent = manager.create_run(
        goal="parent",
        thought="parent",
        plan=["parent"],
        role="child_coordinator",
        parent_id=root.id,
        root_id=root.id,
        depth=1,
    )
    child = manager.create_run(
        goal="child",
        thought="child",
        plan=["child"],
        role="leaf_worker",
        parent_id=parent.id,
        root_id=root.id,
        depth=2,
    )
    for task in [root, parent, child]:
        task.status = "RUNNING"
        task.runner_active_attempt_id = f"attempt-{task.id}"
        task.heartbeat_at = old
        task.updated_at = old
        manager.save(task)
    return root, parent, child


def _trace_child_tool_started(manager: SubAgentManager, root_id: str, child_id: str) -> None:
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core.runner.stage_trace import (
        RunnerToolStageTraceRequest,
        trace_runner_tool_call_started,
    )

    agent = SimpleNamespace(subagents=manager, _current_subagent_run_id=child_id)
    trace_runner_tool_call_started(
        RunnerToolStageTraceRequest(
            agent=agent,
            params=SimpleNamespace(source="test", request_id="", run_id=child_id, task_id=root_id),
            tool_rounds=1,
            idx=1,
            payload={"tool": "write_file", "path": "x"},
        )
    )


def _assert_ancestor_heartbeats_refreshed(manager: SubAgentManager, run_ids: list[str], old: float) -> None:
    for run_id in run_ids:
        assert manager.load(run_id).heartbeat_at > old


def test_subagent_debug_trace_level_four_records_stage_previews(tmp_path):
    """等级 4 写 prompt/response/tool 的短预览，方便实时 tail 定位模型传参。"""
    cfg = AgentConfig(
        enable_tools=True,
        model_backend="echo",
        subagent_workspace="subs",
        subagent_debug_trace_level=4,
        max_tool_rounds=3,
    )
    agent = SimpleAgent(cfg, tmp_path)
    agent.backend = _TraceToolBackend()
    task = agent.subagents.create_run(
        goal="observe detailed runner previews",
        thought="需要看短预览。",
        plan=["list", "finalize"],
        allowed_tools=["list_files"],
    )

    result = agent.run_subagent(task.id, dry_run=False, probe=False)

    assert result.ok
    records = _trace_records(tmp_path / "subs")
    model_started = next(record for record in records if record["event_type"] == "runner_model_request_started")
    model_received = next(record for record in records if record["event_type"] == "runner_model_response_received")
    tool_started = next(record for record in records if record["event_type"] == "runner_tool_call_started")
    tool_finished = next(record for record in records if record["event_type"] == "runner_tool_call_finished")
    assert "observe detailed runner previews" in model_started["task_goal_preview"]
    assert model_started["prompt_preview"]
    assert "TOOL_CALL" in model_received["response_preview"]
    assert "list_files" in tool_started["tool_payload_preview"]
    assert tool_finished["tool_output_preview"]
    assert "prompt_detail_ref" not in model_started


def test_subagent_debug_trace_level_five_writes_detail_refs(tmp_path):
    """等级 5 把完整 prompt/response/tool payload/output 写到内部 detail 文件。"""
    cfg = AgentConfig(
        enable_tools=True,
        model_backend="echo",
        subagent_workspace="subs",
        subagent_debug_trace_level=5,
        max_tool_rounds=3,
    )
    agent = SimpleAgent(cfg, tmp_path)
    agent.backend = _TraceToolBackend()
    task = agent.subagents.create_run(
        goal="observe full detail refs",
        thought="需要完整调试文件。",
        plan=["list", "finalize"],
        allowed_tools=["list_files"],
    )

    result = agent.run_subagent(task.id, dry_run=False, probe=False)

    assert result.ok
    records = _trace_records(tmp_path / "subs")
    model_started = next(record for record in records if record["event_type"] == "runner_model_request_started")
    model_received = next(record for record in records if record["event_type"] == "runner_model_response_received")
    tool_started = next(record for record in records if record["event_type"] == "runner_tool_call_started")
    tool_finished = next(record for record in records if record["event_type"] == "runner_tool_call_finished")
    assert "observe full detail refs" in _read_debug_detail(model_started["prompt_detail_ref"])
    assert _read_debug_detail(model_received["response_detail_ref"])
    response_details = [
        _read_debug_detail(record["response_detail_ref"])
        for record in records
        if record["event_type"] == "runner_model_response_received"
    ]
    assert any("SUBAGENT_RESULT" in detail for detail in response_details)
    assert "list_files" in _read_debug_detail(tool_started["tool_payload_detail_ref"])
    assert _read_debug_detail(tool_finished["tool_output_detail_ref"])


def test_subagent_debug_trace_records_runner_model_request_failure(tmp_path):
    """等级 3 记录模型请求异常，避免 trace 只停在 request_started。"""
    cfg = AgentConfig(
        enable_tools=True,
        model_backend="echo",
        subagent_workspace="subs",
        subagent_debug_trace_level=3,
    )
    agent = SimpleAgent(cfg, tmp_path)
    agent.backend = _FailingTraceBackend()
    task = agent.subagents.create_run(
        goal="observe failed model request",
        thought="模型请求会失败。",
        plan=["call model"],
        allowed_tools=[],
    )

    result = agent.run_subagent(task.id, dry_run=False, probe=False)

    assert not result.ok
    records = _trace_records(tmp_path / "subs")
    event_types = [record["event_type"] for record in records]
    assert "runner_model_request_started" in event_types
    assert "runner_model_request_failed" in event_types
    failed = next(record for record in records if record["event_type"] == "runner_model_request_failed")
    assert failed["error_type"] == "RuntimeError"
    assert "trace backend failed" in failed["error_preview"]


def test_subagent_run_failure_classifies_provider_timeout(tmp_path):
    cfg = AgentConfig(
        enable_tools=True,
        model_backend="echo",
        subagent_workspace="subs",
        subagent_debug_trace_level=3,
    )
    agent = SimpleAgent(cfg, tmp_path)
    agent.backend = _ProviderTimeoutBackend()
    task = agent.subagents.create_run(
        goal="observe provider timeout",
        thought="模型接口会超时。",
        plan=["call model"],
        allowed_tools=[],
    )

    result = agent.run_subagent(task.id, dry_run=False, probe=False)
    recorded = agent.subagents.load(task.id)

    assert not result.ok
    assert recorded.failure_type == "provider_timeout"
    assert "模型接口请求超时" in result.message
    failed = next(
        record for record in _trace_records(tmp_path / "subs")
        if record["event_type"] == "runner_model_request_failed"
    )
    assert failed["error_type"] == "ProviderTimeoutError"


def test_subagent_run_failure_classifies_provider_transient(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core import provider_transient_auto_resume

    monkeypatch.setattr(provider_transient_auto_resume, "provider_transient_retry_delays", lambda _policy=None: ())
    cfg = AgentConfig(
        enable_tools=True,
        model_backend="echo",
        subagent_workspace="subs",
        subagent_debug_trace_level=3,
    )
    agent = SimpleAgent(cfg, tmp_path)
    agent.backend = _ProviderTransientBackend()
    task = agent.subagents.create_run(
        goal="observe provider transient failure",
        thought="模型接口会临时断连。",
        plan=["call model"],
        allowed_tools=[],
    )

    result = agent.run_subagent(task.id, dry_run=False, probe=False)
    recorded = agent.subagents.load(task.id)

    assert not result.ok
    assert recorded.failure_type == "transient_error"
    assert "模型接口临时断开" in result.message
    failed = next(
        record for record in _trace_records(tmp_path / "subs")
        if record["event_type"] == "runner_model_request_failed"
    )
    assert failed["error_type"] == "ProviderTransientError"


def _read_debug_detail(path_text: str) -> str:
    from pathlib import Path

    return Path(path_text).read_text(encoding="utf-8")
