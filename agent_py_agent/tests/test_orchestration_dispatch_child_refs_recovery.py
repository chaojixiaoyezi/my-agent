from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from agent_py_agent.agent.agent_core.orchestration.dispatch.params import DispatchContext
from agent_py_agent.agent.agent_core.orchestration.dispatch.runner_batches import (
    _runner_candidates_for_context,
)
from agent_py_agent.agent.agent_core.orchestration.dispatch.runner_selection import (
    scoped_current_turn_runner_tasks,
    scoped_runner_tasks,
)
from agent_py_agent.agent.agent_core.orchestration.dispatch.tool import DispatchSubagentsTool
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.services.hierarchy.scheduler import (
    HierarchyChildSpec,
    HierarchyScheduleRequest,
)
from agent_py_agent.tests.test_orchestration_dispatch_child_refs import (
    _dispatch_payload_for_record,
    _dispatch_payload_with_direct_children,
)


def test_dispatch_payload_surfaces_qa_repair_advice_from_direct_child(tmp_path: Path):
    tester_dir = tmp_path / "tester"
    tester_dir.mkdir()
    (tester_dir / "output.json").write_text(
        json.dumps({
            "structured_output": {
                "ok": False,
                "summary": "flow-b.html 缺少到 flow-done.html 的链接，流程断裂。",
            },
            "acceptance": ["flow-b.html 到 flow-done.html 链接缺失"],
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    payload = _dispatch_payload_with_direct_children([
        SimpleNamespace(
            id="tester-1",
            parent_id="root",
            role="tester",
            agent_name="小傻妞-tester",
            status="DONE",
            verification_status="VERIFIED",
            task_dir=str(tester_dir),
        )
    ])

    direct = payload["direct_children"]
    assert direct["needs_repair_wave"] is True
    assert direct["next_action"] == "create_repair_child_from_qa_refs"
    assert direct["qa_repair_advice"]["failed_or_conflicting_qa_run_ids"] == ["tester-1"]
    assert direct["qa_repair_advice"]["suggested_tool_call"]["children"][0]["role"] == "worker"


def test_dispatch_payload_reports_qa_scan_failure(tmp_path: Path):
    class BrokenSubagents:
        workspace = tmp_path

        def list_runs(self):
            raise ValueError("subagent ledger broken")

    mock_report = MagicMock()
    mock_report.dry_run = False
    mock_report.summary = {}
    mock_report.records = []
    mock_agent = MagicMock()
    mock_agent._current_subagent_run_id = "root"
    mock_agent.config.subagent_workflow_mode = "off"
    mock_agent.tools.specs.return_value = []
    mock_agent.dispatch_subagents.return_value = mock_report
    mock_agent.subagents = BrokenSubagents()

    direct = json.loads(DispatchSubagentsTool(mock_agent).execute({"dry_run": False}).output)["direct_children"]

    assert direct["load_error"]["context"] == "direct_children.list_runs"
    assert "不是没有子代理" in direct["load_error_hint"]


def test_dispatch_payload_treats_broken_qa_output_as_failure(tmp_path: Path):
    tester_dir = tmp_path / "tester"
    tester_dir.mkdir()
    (tester_dir / "output.json").write_text("{ broken", encoding="utf-8")

    payload = _dispatch_payload_with_direct_children([
        SimpleNamespace(
            id="tester-1",
            parent_id="root",
            role="tester",
            agent_name="小傻妞-tester",
            status="DONE",
            verification_status="VERIFIED",
            task_dir=str(tester_dir),
        )
    ])

    direct = payload["direct_children"]
    assert direct["needs_repair_wave"] is True
    assert direct["qa_repair_advice"]["qa_signal_refs"][0]["output_load_error"]["context"] == "qa.output_json"
    assert "不是 QA 通过" in direct["qa_repair_advice"]["qa_signal_refs"][0]["summary"]


def test_dispatch_payload_prefers_packet_recovery_over_qa_repair(tmp_path: Path):
    manager = SubAgentManager(tmp_path)
    parent = manager.create_run(goal="父任务", thought="派 tester", plan=["schedule"], role="coordinator")
    tester_id = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=parent.id,
            apply=True,
            child_specs=[HierarchyChildSpec(goal="继续 QA", role="tester", agent_name="小傻妞-tester")],
        )
    ).created_run_ids[0]
    tester = manager.load(tester_id)
    Path(tester.output_json).write_text(
        json.dumps({"structured_output": {"summary": "发现缺陷：按钮没有效果。"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    tester.status = "BLOCKED"
    manager.save(tester)

    mock_report = MagicMock()
    mock_report.dry_run = False
    mock_report.summary = {}
    mock_report.records = []
    mock_agent = MagicMock()
    mock_agent._current_subagent_run_id = parent.id
    mock_agent.config.subagent_workflow_mode = "off"
    mock_agent.tools.specs.return_value = []
    mock_agent.dispatch_subagents.return_value = mock_report
    mock_agent.subagents = manager

    direct = json.loads(DispatchSubagentsTool(mock_agent).execute({"dry_run": False}).output)["direct_children"]

    assert direct["needs_recovery"] is True
    assert direct["needs_repair_wave"] is True
    assert direct["repair_wave_deferred_by_recovery"] is True
    assert direct["next_action"] == "inspect_or_rescue_direct_children"
    assert direct["suggested_tool_call"]["run_ids"] == [tester_id]
    assert "latest_continue_packet.json" in direct["suggested_tool_call"]["runner_instruction"]


def test_dispatch_payload_surfaces_qa_repair_advice_from_descendant(tmp_path: Path):
    tester_dir = tmp_path / "tester"
    tester_dir.mkdir()
    (tester_dir / "output.json").write_text(
        json.dumps({"structured_output": {"ok": False, "summary": "发现缺陷：按钮没有效果。"}}, ensure_ascii=False),
        encoding="utf-8",
    )

    payload = _dispatch_payload_with_direct_children([
        SimpleNamespace(id="coord-1", parent_id="root", role="coordinator", agent_name="小傻妞-coord", status="DONE"),
        SimpleNamespace(
            id="tester-2",
            parent_id="coord-1",
            role="tester",
            agent_name="小小傻妞-tester",
            status="DONE",
            verification_status="VERIFIED",
            task_dir=str(tester_dir),
        ),
    ])

    direct = payload["direct_children"]
    assert direct["needs_repair_wave"] is True
    assert direct["qa_repair_advice"]["failed_or_conflicting_qa_run_ids"] == ["tester-2"]
    assert "tester-2" in direct["qa_repair_advice"]["suggested_tool_call"]["children"][0]["goal"]


def test_scoped_runner_tasks_honors_include_run_ids_order():
    tasks = [
        SimpleNamespace(id="child-a", parent_id="root", root_id="root"),
        SimpleNamespace(id="child-b", parent_id="root", root_id="root"),
        SimpleNamespace(id="child-c", parent_id="root", root_id="root"),
        SimpleNamespace(id="other", parent_id="other-parent", root_id="root"),
    ]
    ctx = DispatchContext(
        cfg=MagicMock(),
        normalized_workflow_mode="off",
        planner=False,
        runner_instruction="",
        max_runners=10,
        limit=20,
        reviewer="tester",
        note="",
        take_over_by="",
        locked_files=None,
        router=MagicMock(),
        parent_run_id="root",
        root_id="root",
        include_run_ids=["child-b", "child-a"],
    )

    scoped = scoped_runner_tasks(tasks, ctx)

    assert [task.id for task in scoped] == ["child-b", "child-a"]


def test_scoped_current_turn_runner_tasks_ignores_old_runs_without_explicit_include():
    tasks = [
        SimpleNamespace(id="current-worker", parent_id="", root_id="current-worker"),
        SimpleNamespace(id="old-worker", parent_id="", root_id="old-worker"),
        SimpleNamespace(id="old-child", parent_id="old-worker", root_id="old-worker"),
    ]
    ctx = DispatchContext(
        cfg=MagicMock(),
        normalized_workflow_mode="off",
        planner=False,
        runner_instruction="",
        max_runners=10,
        limit=20,
        reviewer="tester",
        note="",
        take_over_by="",
        locked_files=None,
        router=MagicMock(),
        include_run_ids=[],
    )

    scoped = scoped_current_turn_runner_tasks(
        tasks,
        ctx,
        active_run_ids={"current-worker"},
    )

    assert [task.id for task in scoped] == ["current-worker"]


def test_scoped_current_turn_runner_tasks_keeps_direct_children_when_parent_scoped():
    tasks = [
        SimpleNamespace(id="leaf-now", parent_id="active-parent", root_id="active-parent"),
    ]
    ctx = DispatchContext(
        cfg=MagicMock(),
        normalized_workflow_mode="off",
        planner=False,
        runner_instruction="",
        max_runners=10,
        limit=20,
        reviewer="tester",
        note="",
        take_over_by="",
        locked_files=None,
        router=MagicMock(),
        parent_run_id="active-parent",
        include_run_ids=[],
    )

    scoped = scoped_current_turn_runner_tasks(
        tasks,
        ctx,
        active_run_ids={"active-parent"},
    )

    assert [task.id for task in scoped] == ["leaf-now"]


def test_scoped_current_turn_runner_tasks_preserves_explicit_include_ids():
    tasks = [
        SimpleNamespace(id="current-worker", parent_id="", root_id="current-worker"),
        SimpleNamespace(id="old-worker", parent_id="", root_id="old-worker"),
    ]
    ctx = DispatchContext(
        cfg=MagicMock(),
        normalized_workflow_mode="off",
        planner=False,
        runner_instruction="",
        max_runners=10,
        limit=20,
        reviewer="tester",
        note="",
        take_over_by="",
        locked_files=None,
        router=MagicMock(),
        include_run_ids=["old-worker"],
    )

    scoped = scoped_current_turn_runner_tasks(
        tasks,
        ctx,
        active_run_ids={"current-worker"},
    )

    assert [task.id for task in scoped] == ["current-worker", "old-worker"]
