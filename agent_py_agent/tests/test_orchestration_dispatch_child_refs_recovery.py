from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from agent_py_agent.agent.agent_core.dispatch_params import DispatchContext
from agent_py_agent.agent.agent_core.dispatch_runner_batches import _runner_candidates_for_context
from agent_py_agent.agent.agent_core.dispatch_runner_selection import (
    scoped_current_turn_runner_tasks,
    scoped_runner_tasks,
)
from agent_py_agent.agent.agent_core.orchestration_dispatch_tool import DispatchSubagentsTool
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.services.hierarchy_scheduler import (
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
            "structured_output": {"summary": "flow-b.html 缺少到 flow-done.html 的链接，流程断裂。"},
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
    assert direct["ready_for_final_closeout"] is False
    assert direct["qa_repair_advice"]["failed_or_conflicting_qa_run_ids"] == ["tester-1"]
    assert direct["qa_repair_advice"]["suggested_tool_call"]["children"][0]["role"] == "worker"


# LLM: Artifact integrity blocks should steer parents to repair children before generic recovery.
# 函数用途: 直接 child 的 HTML 结构检查失败时，父 runner 应拿到 refs-first 修复建议，不应先读正文或泛化接管。
def test_dispatch_payload_surfaces_artifact_integrity_repair_from_direct_child(tmp_path: Path):
    run_dir = tmp_path / "worker"
    product_root = tmp_path / "deliverables" / "site-output"
    product_root.mkdir(parents=True)
    artifact = product_root / "index.html"
    run_dir.mkdir()
    artifact.write_text("<html><body>", encoding="utf-8")
    (run_dir / "output.json").write_text(
        json.dumps({
            "status": "BLOCKED",
            "blockers": [f"artifact_integrity_failed:{artifact}:missing_body_close"],
            "artifacts": [{"path": str(artifact)}],
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    payload = _dispatch_payload_with_direct_children([
        SimpleNamespace(
            id="worker-1",
            parent_id="root",
            role="worker",
            agent_name="小傻妞-worker",
            status="BLOCKED",
            verification_status="UNVERIFIED",
            failure_type="artifact_integrity_failed",
            blockers=[f"artifact_integrity_failed:{artifact}:missing_body_close"],
            task_dir=str(run_dir),
            output_json=str(run_dir / "output.json"),
            allowed_write_roots=[str(run_dir), str(product_root)],
        )
    ])

    direct = payload["direct_children"]
    assert direct["needs_artifact_integrity_repair_wave"] is True
    assert direct["needs_recovery"] is False
    assert direct["next_action"] == "create_repair_child_from_artifact_integrity_refs"
    advice = direct["artifact_integrity_repair_advice"]
    assert advice["failed_run_ids"] == ["worker-1"]
    assert advice["failure_refs"][0]["artifact_refs"] == [str(artifact)]
    child = advice["suggested_tool_call"]["children"][0]
    assert child["role"] == "worker"
    assert str(artifact) in child["goal"]


# LLM: Recovery-ready QA blockers must prefer packet continuation before repair waves.
# 函数用途: 当同一个 tester 既有失败信号又有 latest_continue_packet 时，父级应先复用原 run 续跑。
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

    direct = json.loads(DispatchSubagentsTool(mock_agent).execute({"apply": True}).output)["direct_children"]

    assert direct["needs_recovery"] is True
    assert direct["needs_repair_wave"] is True
    assert direct["repair_wave_deferred_by_recovery"] is True
    assert direct["next_action"] == "inspect_or_rescue_direct_children"
    assert direct["suggested_tool_call"]["run_ids"] == [tester_id]
    assert "latest_continue_packet.json" in direct["suggested_tool_call"]["runner_instruction"]


# LLM: QA repair advice must scan descendants so upper coordinators see lower tester failures.
# 函数用途: root 的直接 child 是 coordinator 时，孙级 tester 的失败也应作为 refs-first repair 建议返回。
def test_dispatch_payload_surfaces_qa_repair_advice_from_descendant(tmp_path: Path):
    tester_dir = tmp_path / "tester"
    tester_dir.mkdir()
    (tester_dir / "output.json").write_text(
        json.dumps({"structured_output": {"summary": "发现缺陷：按钮没有效果。"}}, ensure_ascii=False),
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


# LLM: test_scoped_runner_tasks_honors_include_run_ids protects exact child dispatch waves.
# 函数用途: 父 runner 指定 run_ids 时，调度候选只能包含这些直接孩子，并按指定顺序执行。
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
        apply=True,
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


# LLM: Active root-turn scope must keep stale global workspace runs out of implicit dispatch waves.
# 函数用途: 当前轮已有 run_id 记录且模型省略 run_ids 时，runner 候选只保留本轮任务，避免旧测试子代理被误调度。
def test_scoped_current_turn_runner_tasks_ignores_old_runs_without_explicit_include():
    tasks = [
        SimpleNamespace(id="current-worker", parent_id="", root_id="current-worker"),
        SimpleNamespace(id="old-worker", parent_id="", root_id="old-worker"),
        SimpleNamespace(id="old-child", parent_id="old-worker", root_id="old-worker"),
    ]
    ctx = DispatchContext(
        cfg=MagicMock(),
        normalized_workflow_mode="off",
        apply=True,
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


# LLM: Runner-context parent scope is already precise and must not hide freshly scheduled children.
# 函数用途: 覆盖小傻妞派小小傻妞后，当前轮过滤误把直接 child 过滤掉导致只做 due-check 的回归。
def test_scoped_current_turn_runner_tasks_keeps_direct_children_when_parent_scoped():
    tasks = [
        SimpleNamespace(id="leaf-now", parent_id="active-parent", root_id="active-parent"),
    ]
    ctx = DispatchContext(
        cfg=MagicMock(),
        normalized_workflow_mode="off",
        apply=True,
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


# LLM: Explicit run_ids remain an exact operator request and are not narrowed by remembered root-turn scope.
# 函数用途: 父级明确传 run_ids 时保持原有精确调度语义，避免当前轮过滤误伤接管/恢复操作。
def test_scoped_current_turn_runner_tasks_preserves_explicit_include_ids():
    tasks = [
        SimpleNamespace(id="current-worker", parent_id="", root_id="current-worker"),
        SimpleNamespace(id="old-worker", parent_id="", root_id="old-worker"),
    ]
    ctx = DispatchContext(
        cfg=MagicMock(),
        normalized_workflow_mode="off",
        apply=True,
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


# LLM: Explicit packet recovery should rerun a blocked original child instead of only classifying it.
# 函数用途: 覆盖真实 E2E 暴露的问题：run_ids+latest_continue_packet 指令必须让 BLOCKED run 进入 runner 候选。
