from __future__ import annotations

import json
import time
from pathlib import Path

from agent_py_agent.agent.agent_core.runner_prompts import _build_subagent_runner_prompt
from agent_py_agent.agent.agent_core.subagent_compact_continuation import (
    _is_stale_packet,
    _read_json,
)
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.manager_runner_results import RecordRunnerResultParams


# LLM: subagent saves must materialize a task-local continue packet before any parent rerun.
# 函数用途: 验证子代理保存时自动生成 latest_continue_packet 和 session compact ledger，供父级按 refs 接管。
def test_subagent_save_writes_task_local_continue_packet(tmp_path: Path) -> None:
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(
        goal="继续实现 checkout.html",
        thought="购物站 leaf 被 compact 后要能接着写。",
        plan=["恢复 checkpoint", "继续补测试"],
        role="leaf_worker",
    )
    task.status = "BLOCKED"
    task.current_step = "等待父级授权后继续 checkout tests"
    task.latest_summary = "已经写完商品页，checkout tests 还没补齐。"
    task.blockers = ["缺少父级重新 dispatch"]
    task.artifact_refs = ["build/products.html"]
    task.evidence_refs = ["reports/runner_result.json"]

    manager.save(task)
    loaded = manager.load(task.id)

    packet_ref = Path(loaded.agent_run_compactions_dir) / "latest_continue_packet.json"
    ledger_ref = Path(loaded.agent_run_compactions_dir) / "session_compact_ledger.jsonl"
    packet = json.loads(packet_ref.read_text(encoding="utf-8"))
    ledger_lines = ledger_ref.read_text(encoding="utf-8").splitlines()

    assert packet["schema_version"] == "subagent_continue_packet.v1"
    assert packet["memory_scope"] == "task_local"
    assert packet["writes_main_memory"] is False
    assert packet["automatic_tool_execution"] == "none"
    assert packet["ready_to_continue"] is True
    assert packet["owner"] == {"owner_type": "subagent_run", "owner_id": task.id}
    assert packet["next_action"] == "等待父级授权后继续 checkout tests"
    assert packet["latest_summary"] == "已经写完商品页，checkout tests 还没补齐。"
    assert packet["restore_refs"]["agent_run_checkpoint"].endswith("checkpoint.json")
    assert loaded.agent_run_checkpoint_json in packet["recommended_read_paths"]
    assert ledger_lines
    assert json.loads(ledger_lines[-1])["packet_ref"] == str(packet_ref)


# LLM: parent reruns should pick up generated task-local continue packets through the normal context path.
# 函数用途: 验证父级重新构建 runner prompt 时自动读取保存生成的 continue packet，而不是靠手工传入。
def test_runner_prompt_uses_generated_task_local_continue_packet(tmp_path: Path) -> None:
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(
        goal="继续购物网站 leaf 任务",
        thought="需要从 task-local packet 接续。",
        plan=["读 checkpoint", "继续写验收证据"],
        role="leaf_worker",
    )
    task.status = "RUNNING"
    task.current_step = "从 task-local packet 继续写验收证据"
    task.latest_summary = "页面骨架已经存在，剩余验收证据。"
    manager.save(task)

    context = manager.build_execution_context(task.id)
    prompt = _build_subagent_runner_prompt(context)

    assert "Task-Local Compact Continuation" in prompt
    assert "Continue Packet" in prompt
    assert "latest_continue_packet.json" in prompt
    assert "从 task-local packet 继续写验收证据" in prompt
    assert "页面骨架已经存在" in prompt
    assert "SOUL.md" not in prompt


# LLM: corrupted continue packets must not make parent reruns reinterpret the original task from scratch.
# 函数用途: 验证 latest_continue_packet 损坏时，runner prompt 明确降级到 checkpoint/summary/task-local refs。
def test_runner_prompt_falls_back_to_checkpoint_when_continue_packet_is_corrupt(tmp_path: Path) -> None:
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(
        goal="继续恢复损坏 packet 的任务",
        thought="packet 坏了也要读 checkpoint。",
        plan=["读 checkpoint", "继续写结果"],
        role="leaf_worker",
    )
    task.status = "RUNNING"
    task.current_step = "从 checkpoint 降级恢复"
    task.latest_summary = "checkpoint 里还有可用恢复事实。"
    manager.save(task)
    loaded = manager.load(task.id)
    packet_ref = Path(loaded.agent_run_compactions_dir) / "latest_continue_packet.json"
    packet_ref.write_text("{not valid json", encoding="utf-8")

    prompt = _build_subagent_runner_prompt(manager.build_execution_context(task.id))

    assert "Task-Local Compact Continuation" in prompt
    assert "packet_status: unreadable_json" in prompt
    assert "fallback_to: checkpoint/summary/task-local refs" in prompt
    assert "agent_run_checkpoint" in prompt
    assert "checkpoint 里还有可用恢复事实" in prompt


# LLM: runner prepare may self-heal packet files, but the prompt must still show the damaged preflight state.
# 函数用途: 验证真实 dispatch 前的 prepare_runner_attempt 会记录 packet 损坏事实，并让 runner prompt 可审计地降级到 checkpoint。
def test_prepare_runner_attempt_preserves_corrupt_packet_preflight(tmp_path: Path) -> None:
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(
        goal="继续恢复被损坏 packet 的真实 runner",
        thought="prepare 会重写 packet，但不能丢掉启动前的损坏状态。",
        plan=["读 checkpoint", "继续写结果"],
        role="worker",
    )
    task.status = "TIMEOUT"
    task.failure_type = "runner_timeout"
    task.current_step = "从损坏 packet 降级恢复"
    task.latest_summary = "checkpoint 仍然可用。"
    manager.save(task)
    loaded = manager.load(task.id)
    packet_ref = Path(loaded.agent_run_compactions_dir) / "latest_continue_packet.json"
    packet_ref.write_text("{not valid json", encoding="utf-8")

    prepared = manager.prepare_runner_attempt(task.id)
    context = manager.build_execution_context(prepared.id)
    prompt = _build_subagent_runner_prompt(context)

    assert "Recovery Preflight" in prompt
    assert "packet_status_before_prepare: corrupt" in prompt
    assert "fallback_to: checkpoint/summary/task-local refs" in prompt
    assert "prepare may regenerate latest_continue_packet" in prompt
    assert "checkpoint 仍然可用" in prompt


# LLM: stale continue packets should be treated as hints only, with durable refs as the recovery source.
# 函数用途: 验证 latest_continue_packet 过期时，runner prompt 标记 stale 并降级读 checkpoint/summary。
def test_runner_prompt_falls_back_to_checkpoint_when_continue_packet_is_stale(tmp_path: Path) -> None:
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(
        goal="继续恢复过期 packet 的任务",
        thought="packet 过期时用 checkpoint。",
        plan=["读 checkpoint", "继续写结果"],
        role="leaf_worker",
    )
    task.status = "RUNNING"
    task.current_step = "从 stale packet 降级恢复"
    task.latest_summary = "summary 是过期 packet 后的稳定恢复事实。"
    manager.save(task)
    loaded = manager.load(task.id)
    packet_ref = Path(loaded.agent_run_compactions_dir) / "latest_continue_packet.json"
    packet = json.loads(packet_ref.read_text(encoding="utf-8"))
    packet["created_at"] = time.time() - 8 * 24 * 60 * 60
    packet_ref.write_text(json.dumps(packet, ensure_ascii=False), encoding="utf-8")
    assert _is_stale_packet(packet_ref, _read_json(packet_ref))

    prompt = _build_subagent_runner_prompt(manager.build_execution_context(task.id))

    assert "Task-Local Compact Continuation" in prompt
    assert "packet_status: stale" in prompt
    assert "fallback_to: checkpoint/summary/task-local refs" in prompt
    assert "summary 是过期 packet 后的稳定恢复事实" in prompt


# LLM: missing continue packets should still leave the durable task-local recovery refs visible.
# 函数用途: 验证 latest_continue_packet 缺失时不会放弃恢复，而是继续展示 checkpoint/summary/task refs。
def test_runner_prompt_uses_checkpoint_when_continue_packet_is_missing(tmp_path: Path) -> None:
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(
        goal="继续恢复缺失 packet 的任务",
        thought="packet 不在时也要读 checkpoint。",
        plan=["读 checkpoint", "继续写结果"],
        role="leaf_worker",
    )
    task.status = "RUNNING"
    task.current_step = "packet 缺失时读 checkpoint"
    task.latest_summary = "summary 是缺失 packet 后的稳定恢复事实。"
    manager.save(task)
    loaded = manager.load(task.id)
    packet_ref = Path(loaded.agent_run_compactions_dir) / "latest_continue_packet.json"
    packet_ref.unlink()

    prompt = _build_subagent_runner_prompt(manager.build_execution_context(task.id))

    assert "Task-Local Compact Continuation" in prompt
    assert "### Continue Packet" not in prompt
    assert "agent_run_checkpoint" in prompt
    assert "summary 是缺失 packet 后的稳定恢复事实" in prompt


# LLM: timeout recovery packets must reflect the final runner state, not the earlier RUNNING attempt state.
# 函数用途: 验证 runner timeout 写回后，接管包、失败交接和 continue packet 都能指向同一个 TIMEOUT 事实。
def test_timeout_runner_result_refreshes_recovery_packets(tmp_path: Path) -> None:
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(
        goal="长任务执行中发生超时",
        thought="恢复包必须按失败状态更新。",
        plan=["开始执行", "失败后接续"],
        role="worker",
    )
    manager.prepare_runner_attempt(task.id, retry_reason="")

    manager.record_runner_result(
        RecordRunnerResultParams(
            run_id=task.id,
            dry_run=False,
            ok=False,
            message="runner timed out after 30.00s",
            status="TIMEOUT",
            verification_status="UNVERIFIED",
            failure_type="runner_timeout",
        )
    )
    loaded = manager.load(task.id)

    packet_ref = Path(loaded.agent_run_compactions_dir) / "latest_continue_packet.json"
    packet = json.loads(packet_ref.read_text(encoding="utf-8"))
    readiness = json.loads(Path(loaded.takeover_readiness_json).read_text(encoding="utf-8"))
    handoff = json.loads(Path(loaded.failure_handoff_json).read_text(encoding="utf-8"))

    assert loaded.status == "TIMEOUT"
    assert packet["status"] == "TIMEOUT"
    assert packet["current_step"] == "TIMEOUT"
    assert packet["blockers"] == ["runner timed out after 30.00s"]
    assert readiness["status"] == "TIMEOUT"
    assert readiness["failure_handoff_ref"] == loaded.failure_handoff_json
    assert handoff["failure_type"] == "runner_timeout"
