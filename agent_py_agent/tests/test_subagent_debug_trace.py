from __future__ import annotations

import json

from agent_py_agent.agent.backend import BaseBackend, ModelResponse
from agent_py_agent.agent.backends.errors import ProviderTimeoutError
from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.manager_runner_results import RecordRunnerResultParams
from agent_py_agent.agent.subagents.services.hierarchy_recovery import HierarchyRecoveryRequest
from agent_py_agent.agent.subagents.services.hierarchy_scheduler import (
    HierarchyChildSpec,
    HierarchyScheduleRequest,
)


def _trace_records(workspace):
    trace_file = workspace / "debug_traces" / "subagent_trace.jsonl"
    return [json.loads(line) for line in trace_file.read_text(encoding="utf-8").splitlines()]


# LLM: _TraceToolBackend drives one model request, one tool call, and one final response for trace assertions.
# 类用途: 测试 runner 阶段心跳时使用的假后端；第一轮请求 list_files，第二轮返回结构化子代理结果。
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
                '  "status": "AWAITING_ACCEPTANCE",\n'
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


# LLM: _FailingTraceBackend verifies request-failed trace writes before runner failure handling.
# 类用途: 测试模型请求抛异常时，debug trace 也能留下失败阶段事件。
class _FailingTraceBackend(BaseBackend):
    name = "failing_trace_backend"

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        raise RuntimeError("trace backend failed")


# LLM: _ProviderTimeoutBackend lets runner tests exercise timeout classification without sleeping.
# 类用途: 测试模型接口超时时，子代理失败类型和 trace 都能稳定记录。
class _ProviderTimeoutBackend(BaseBackend):
    name = "provider_timeout_backend"

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        raise ProviderTimeoutError("模型接口请求超时: request_timeout=17s")


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
            status="AWAITING_ACCEPTANCE",
            verification_status="NEEDS_ACCEPTANCE",
            backend="echo",
            tool_rounds=1,
        )
    )

    records = _trace_records(tmp_path)
    assert [record["event_type"] for record in records] == ["task_created", "runner_result_recorded"]
    runner_record = records[-1]
    assert runner_record["level"] == 2
    assert runner_record["run_id"] == task.id
    assert runner_record["status"] == "AWAITING_ACCEPTANCE"
    assert runner_record["verification_status"] == "NEEDS_ACCEPTANCE"
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


def test_subagent_debug_trace_records_parent_acceptance_decision_and_next_action(tmp_path):
    """等级 2 记录父级验收判断和下一动作，便于排查卡在人审还是测试。"""
    manager = SubAgentManager(tmp_path, debug_trace_level=2)
    task = manager.create_run(goal="acceptance", thought="check", plan=["plan"])

    manager.plan_parent_acceptance(task.id)
    manager.plan_parent_acceptance_next_action(task.id)

    records = _trace_records(tmp_path)
    assert [record["event_type"] for record in records] == [
        "task_created",
        "parent_acceptance_decision",
        "parent_acceptance_next_action",
    ]
    decision_record = records[-2]
    action_record = records[-1]
    assert decision_record["decision"] == "inspect_only"
    assert decision_record["risk_level"] == "low"
    assert decision_record["requires_human_confirmation"] is False
    assert action_record["action"] == "apply_acceptance"
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


# LLM: provider timeouts should become recoverable runner facts instead of generic runner_error.
# 函数用途: 确认子代理模型接口超时会写成 provider_timeout，方便父级接管和后续调度。
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


def _read_debug_detail(path_text: str) -> str:
    from pathlib import Path

    return Path(path_text).read_text(encoding="utf-8")
