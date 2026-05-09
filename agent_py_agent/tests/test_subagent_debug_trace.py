from __future__ import annotations

import json

from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.manager_runner_results import RecordRunnerResultParams


def _trace_records(workspace):
    trace_file = workspace / "debug_traces" / "subagent_trace.jsonl"
    return [json.loads(line) for line in trace_file.read_text(encoding="utf-8").splitlines()]


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
