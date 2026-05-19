from __future__ import annotations

import json
import os
from pathlib import Path

from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.services.hierarchy_scheduler import (
    HierarchyChildSpec,
    HierarchyScheduleRequest,
)
from agent_py_agent.agent.subagents.services.recovery_strategy import (
    SubagentRecoveryStrategyRequest,
    build_subagent_recovery_strategy,
)


# LLM: _saved_task creates a persisted task so packet/checkpoint refs exist like production.
# 函数用途: 构造已经 save 过的子代理任务，方便恢复策略读取真实文件路径。
def _saved_task(tmp_path: Path, *, status: str = "BLOCKED"):
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(
        goal="继续购物网站 checkout 任务",
        thought="需要从本地恢复包接着做。",
        plan=["读恢复包", "继续实现"],
        role="leaf_worker",
    )
    task.status = status
    task.current_step = "继续补 checkout QA 证据"
    task.latest_summary = "商品页已完成，checkout QA 还没结束。"
    manager.save(task)
    return manager, manager.load(task.id)


# LLM: test_recovery_strategy_prefers_task_local_continue_packet protects packet-first recovery.
# 函数用途: 子代理失败但 packet 正常时，父级恢复建议必须优先续跑原 run，而不是重新理解任务。
def test_recovery_strategy_prefers_task_local_continue_packet(tmp_path: Path) -> None:
    _, task = _saved_task(tmp_path)

    result = build_subagent_recovery_strategy(SubagentRecoveryStrategyRequest(task=task))

    assert result.recommended_action == "rerun_original_from_continue_packet"
    assert result.packet_status == "ready"
    assert result.uses_continue_packet is True
    assert result.packet_ref.endswith("latest_continue_packet.json")
    assert "latest_continue_packet.json" in result.runner_instruction
    assert "不要重新从用户目标开始规划" in result.runner_instruction
    assert result.to_dict()["memory_scope"] == "task_local"


# LLM: test_recovery_strategy_falls_back_when_packet_is_corrupt covers broken JSON downgrade.
# 函数用途: latest_continue_packet 损坏时不能卡死，要降级读取 checkpoint/summary。
def test_recovery_strategy_falls_back_when_packet_is_corrupt(tmp_path: Path) -> None:
    _, task = _saved_task(tmp_path)
    packet_ref = Path(task.agent_run_latest_session_continue_packet_json)
    packet_ref.write_text("{not-json", encoding="utf-8")

    result = build_subagent_recovery_strategy(SubagentRecoveryStrategyRequest(task=task))

    assert result.packet_status == "corrupt"
    assert result.uses_continue_packet is False
    assert result.recommended_action == "rerun_original_from_checkpoint"
    assert task.agent_run_checkpoint_json in result.fallback_refs
    assert task.agent_run_summary_md in result.fallback_refs


# LLM: test_recovery_strategy_falls_back_when_packet_is_stale covers old packet detection.
# 函数用途: packet 过期时继续使用 checkpoint/summary，而不是盲目相信旧恢复包。
def test_recovery_strategy_falls_back_when_packet_is_stale(tmp_path: Path) -> None:
    _, task = _saved_task(tmp_path)
    packet_ref = Path(task.agent_run_latest_session_continue_packet_json)
    os.utime(packet_ref, (100.0, 100.0))

    result = build_subagent_recovery_strategy(
        SubagentRecoveryStrategyRequest(task=task, now=200.0, packet_max_age_seconds=10.0)
    )

    assert result.packet_status == "stale"
    assert result.recommended_action == "rerun_original_from_checkpoint"
    assert result.fallback_refs[0] == task.agent_run_checkpoint_json


# LLM: test_recovery_strategy_stops_after_repeated_failures covers no-progress fuse.
# 函数用途: 连续失败次数过多时给出熔断建议，避免恢复流程无限扩容或无限重试。
def test_recovery_strategy_stops_after_repeated_failures(tmp_path: Path) -> None:
    _, task = _saved_task(tmp_path, status="FAILED")
    task.runner_attempts = 5

    result = build_subagent_recovery_strategy(
        SubagentRecoveryStrategyRequest(task=task, no_progress_attempt_limit=3)
    )

    assert result.no_progress_fuse is True
    assert result.recommended_action == "stop_no_progress_and_escalate"
    assert "不要继续自动重试" in result.runner_instruction


# LLM: test_recovery_strategy_takeover_timeout_worker_when_packet_is_ready covers dead-run recovery.
# 函数用途: 原 run 超时即使已有 continue packet，也创建 takeover 读取 packet，避免复用不可信执行槽。
def test_recovery_strategy_takeover_timeout_worker_when_packet_is_ready(tmp_path: Path) -> None:
    _, task = _saved_task(tmp_path, status="TIMEOUT")
    task.failure_type = "runner_timeout"

    result = build_subagent_recovery_strategy(SubagentRecoveryStrategyRequest(task=task))

    assert result.recommended_action == "create_takeover_run_from_continue_packet"
    assert result.uses_continue_packet is True
    assert "latest_continue_packet.json" in result.runner_instruction


def test_recovery_strategy_repairs_artifact_integrity_instead_of_rerunning_original(tmp_path: Path) -> None:
    _, task = _saved_task(tmp_path, status="BLOCKED")
    artifact = tmp_path / "lab_outputs" / "shop-demo" / "index.html"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("<html><body>unfinished", encoding="utf-8")
    task.failure_type = "artifact_integrity_failed"
    task.blockers = [f"artifact_integrity_failed:{artifact}:missing_body_close,missing_html_close"]
    task.artifact_refs = [str(artifact)]

    result = build_subagent_recovery_strategy(SubagentRecoveryStrategyRequest(task=task))

    assert result.recommended_action == "create_repair_child_from_artifact_integrity_refs"
    assert result.uses_continue_packet is False
    assert "不要继续原 run" in result.runner_instruction


def test_recovery_strategy_does_not_chain_artifact_repair_children(tmp_path: Path) -> None:
    _, task = _saved_task(tmp_path, status="BLOCKED")
    artifact = tmp_path / "lab_outputs" / "shop-demo" / "index.html"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("<html><head></head><body><head></head><body></body></html>", encoding="utf-8")
    task.failure_type = "artifact_integrity_failed"
    task.blockers = [f"artifact_integrity_failed:{artifact}:multiple_body_open"]
    task.artifact_refs = [str(artifact)]
    task.attributes = {
        "repair_kind": "artifact_integrity",
        "repair_source_run_id": "source-run",
        "target_artifact_refs": [str(artifact)],
    }

    result = build_subagent_recovery_strategy(SubagentRecoveryStrategyRequest(task=task))

    assert result.recommended_action.startswith("create_takeover_run")
    assert result.recommended_action != "create_repair_child_from_artifact_integrity_refs"
    assert "接管" in result.runner_instruction


def test_recovery_strategy_suggests_takeover_for_dead_worker_without_packet(tmp_path: Path) -> None:
    _, task = _saved_task(tmp_path, status="TIMEOUT")
    task.failure_type = "runner_timeout"
    Path(task.agent_run_latest_session_continue_packet_json).unlink()

    result = build_subagent_recovery_strategy(SubagentRecoveryStrategyRequest(task=task))

    assert result.recommended_action == "create_takeover_run_from_checkpoint"
    assert task.task_dir in result.takeover_refs
    assert task.agent_run_artifacts_dir in result.takeover_refs
    assert "同一个任务目录" in result.runner_instruction


# LLM: test_recovery_strategy_suggests_leadership_recovery_for_failed_coordinator covers leader handoff.
# 函数用途: coordinator 带着下级挂掉时，恢复策略要走 leader recovery，而不是无限给 coordinator 本人重试。
def test_recovery_strategy_suggests_leadership_recovery_for_failed_coordinator(tmp_path: Path) -> None:
    manager = SubAgentManager(tmp_path)
    coordinator = manager.create_run(
        goal="协调购物网站实现",
        thought="拆给 leaf。",
        plan=["派工"],
        role="coordinator",
    )
    manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=coordinator.id,
            apply=True,
            child_specs=[HierarchyChildSpec(goal="写商品列表", role="worker", agent_name="小小傻妞-catalog")],
        )
    )
    coordinator = manager.load(coordinator.id)
    coordinator.status = "TIMEOUT"
    coordinator.failure_type = "runner_timeout"
    manager.save(coordinator)
    coordinator = manager.load(coordinator.id)

    result = build_subagent_recovery_strategy(SubagentRecoveryStrategyRequest(task=coordinator))

    assert result.recommended_action == "recover_coordinator_leadership"
    assert result.leadership_recovery is True
    assert result.child_run_ids == coordinator.child_ids
    assert "subagents-leadership-recovery-plan" in result.runner_instruction
