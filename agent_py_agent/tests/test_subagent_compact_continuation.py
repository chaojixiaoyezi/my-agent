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
from agent_py_agent.agent.subagents.models import SubAgentParsedOutput


# LLM: subagent saves must materialize a task-local continue packet before any parent rerun.
# 函数用途: 验证子代理保存时自动生成 latest_continue_packet 和 session compact ledger，供父级按 refs 接管。
def test_subagent_save_writes_task_local_continue_packet(tmp_path: Path) -> None:
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(
        goal="继续实现 flow-b.html",
        thought="示例站 leaf 被 compact 后要能接着写。",
        plan=["恢复 checkpoint", "继续补测试"],
        role="leaf_worker",
    )
    task.status = "BLOCKED"
    task.current_step = "等待父级授权后继续 checkout tests"
    task.latest_summary = "已经写完条目页，checkout tests 还没补齐。"
    task.blockers = ["缺少父级重新 dispatch"]
    task.artifact_refs = ["build/items.html"]
    task.evidence_refs = ["reports/runner_result.json"]

    manager.save(task)
    loaded = manager.load(task.id)

    packet_ref = Path(loaded.agent_run_latest_session_continue_packet_json)
    ledger_ref = Path(loaded.agent_run_session_compaction_ledger_jsonl)
    packet = json.loads(packet_ref.read_text(encoding="utf-8"))
    ledger_lines = ledger_ref.read_text(encoding="utf-8").splitlines()

    assert packet["schema_version"] == "subagent_continue_packet.v1"
    assert packet["memory_scope"] == "task_local"
    assert packet["writes_main_memory"] is False
    assert packet["automatic_tool_execution"] == "none"
    assert packet["ready_to_continue"] is True
    assert packet["owner"] == {"owner_type": "subagent_run", "owner_id": task.id}
    assert packet["next_action"] == "等待父级授权后继续 checkout tests"
    assert packet["latest_summary"] == "已经写完条目页，checkout tests 还没补齐。"
    assert packet["restore_refs"]["agent_run_checkpoint"].endswith("checkpoint.json")
    assert loaded.agent_run_checkpoint_json in packet["recommended_read_paths"]
    assert ledger_lines
    assert json.loads(ledger_lines[-1])["packet_ref"] == str(packet_ref)


# LLM: parent reruns should pick up generated task-local continue packets through the normal context path.
# 函数用途: 验证父级重新构建 runner prompt 时自动读取保存生成的 continue packet，而不是靠手工传入。
def test_runner_prompt_uses_generated_task_local_continue_packet(tmp_path: Path) -> None:
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(
        goal="继续示例网站 leaf 任务",
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


# LLM: repeated saves should not create noisy duplicate continue-packet ledger rows.
# 函数用途: 防止长任务每轮保存都把相同 packet 追加到 session ledger，避免日志膨胀。
def test_continue_packet_ledger_dedupes_unchanged_state(tmp_path: Path) -> None:
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(
        goal="保持同一恢复状态",
        thought="连续保存不应重复写 ledger。",
        plan=["保存", "再次保存"],
        role="worker",
    )
    manager.save(manager.load(task.id))
    loaded = manager.load(task.id)
    ledger_ref = Path(loaded.agent_run_session_compaction_ledger_jsonl)
    rows = ledger_ref.read_text(encoding="utf-8").splitlines()

    assert len(rows) == 1
    assert json.loads(rows[0])["state_fingerprint"]


# LLM: empty packets should be summarized enough that a runner can start work without rereading the packet body.
# 函数用途: 覆盖真实 E2E 暴露的慢路径：新任务 packet 无进度时，runner prompt 要明确不要反复读完整 JSON。
def test_runner_prompt_says_empty_continue_packet_can_start_from_goal(tmp_path: Path) -> None:
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(
        goal="创建算法测试方案.md",
        thought="新任务还没有写作进度。",
        plan=["直接写文件", "提交证据"],
        role="worker",
    )
    manager.save(task)

    prompt = _build_subagent_runner_prompt(manager.build_execution_context(task.id))

    assert "work_progress: none" in prompt
    assert "because work_progress/session_compact are empty" in prompt
    assert "start from the task goal instead of reading the packet body" in prompt


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
    packet_ref = Path(loaded.agent_run_latest_session_continue_packet_json)
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
    packet_ref = Path(loaded.agent_run_latest_session_continue_packet_json)
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
    packet_ref = Path(loaded.agent_run_latest_session_continue_packet_json)
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
    packet_ref = Path(loaded.agent_run_latest_session_continue_packet_json)
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

    packet_ref = Path(loaded.agent_run_latest_session_continue_packet_json)
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


# LLM: subagent-owned compact packages must stay inside the run workspace, never main memory_archive.
# 函数用途: 验证子代理模型回合接近上下文上限时，会写 task-local session compact 包并挂到 continue packet。
def test_subagent_runner_result_writes_task_local_session_compact_package(tmp_path: Path) -> None:
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(
        goal="长任务 worker 需要压缩后继续",
        thought="runner 会接近上下文窗口。",
        plan=["写第一段", "压缩后继续第二段"],
        role="worker",
        root_id="root-session-compact",
    )
    manager.prepare_runner_attempt(task.id)

    _record_session_compact_result(
        manager,
        task.id,
        {
            "summary": "已完成第一段，下一步继续第二段。",
            "next_action": "继续第二段实现",
            "compact": {
                "status": "near_limit",
                "ratio": 0.92,
                "message": "subagent local compact needed",
                "token_budget": {"current_tokens": 9200, "max_context_tokens": 10000},
            },
        },
    )
    loaded = manager.load(task.id)
    refs = _session_compact_refs(loaded)
    metadata = refs["metadata"]
    packet = refs["packet"]

    assert metadata["schema_version"] == "subagent_session_compact.v1"
    assert metadata["owner"] == {"owner_type": "subagent_run", "owner_id": task.id}
    assert metadata["memory_scope"] == "task_local"
    assert metadata["writes_main_memory"] is False
    assert metadata["source"]["auto_status"] == "needs_user_confirmation"
    assert metadata["token_budget"]["current_tokens"] == 9200
    assert metadata["restore_refs"]["agent_run_checkpoint"].endswith("checkpoint.json")
    assert refs["latest_summary"].read_text(encoding="utf-8").startswith("# Subagent Session Compact")
    assert packet["session_compact"]["metadata_ref"] == str(refs["latest_metadata"])
    assert packet["session_compact"]["summary_ref"] == str(refs["latest_summary"])
    assert str(refs["latest_metadata"]) in packet["recommended_read_paths"]
    assert any(row.get("event_type") == "subagent_session_compact" for row in refs["ledger_rows"])
    assert not (tmp_path / "memory_archive" / "compact_applies").exists()


# LLM: Session compact packages need their own refs so checkpoint compact latest files stay authoritative.
# 函数用途: 验证子代理会话续接包不会覆盖普通 checkpoint compact chain 的 latest_metadata/latest_summary。
def test_session_compact_refs_do_not_overwrite_checkpoint_compact_latest_refs(tmp_path: Path) -> None:
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(
        goal="隔离 session compact refs",
        thought="普通 checkpoint 和会话续接必须分开。",
        plan=["写 checkpoint", "写 session compact"],
        role="worker",
    )
    checkpoint_metadata_ref = Path(manager.load(task.id).agent_run_latest_compaction_metadata_json)

    manager.prepare_runner_attempt(task.id)
    _record_session_compact_result(
        manager,
        task.id,
        {
            "summary": "第一轮已完成，需要继续。",
            "next_action": "继续第二轮",
            "compact": {"token_budget": {"current_tokens": 9000, "max_context_tokens": 10000}},
        },
    )

    loaded = manager.load(task.id)
    checkpoint_metadata = json.loads(Path(loaded.agent_run_latest_compaction_metadata_json).read_text(encoding="utf-8"))
    session_metadata_ref = Path(loaded.agent_run_latest_session_compaction_metadata_json)
    session_packet_ref = Path(loaded.agent_run_latest_session_continue_packet_json)

    assert Path(loaded.agent_run_latest_compaction_metadata_json) == checkpoint_metadata_ref
    assert checkpoint_metadata["event_type"] == "checkpoint_snapshot"
    assert session_metadata_ref.exists()
    assert session_packet_ref.exists()
    assert session_metadata_ref.parent.name == "session"
    assert json.loads(session_metadata_ref.read_text(encoding="utf-8"))["schema_version"] == "subagent_session_compact.v1"


# LLM: runner prompts should surface the task-local session compact package before stale parent memory.
# 函数用途: 验证父级重新 dispatch 时，runner prompt 展示子代理 compact metadata/summary refs 和下一步。
def test_runner_prompt_includes_task_local_session_compact_package(tmp_path: Path) -> None:
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(
        goal="继续压缩后的 worker 任务",
        thought="需要读子代理自己的 compact package。",
        plan=["读本地 compact metadata", "继续实现"],
        role="worker",
    )
    manager.prepare_runner_attempt(task.id)
    _record_session_compact_result(
        manager,
        task.id,
        {
            "summary": "第一轮已写完目录结构。",
            "next_action": "继续补齐页面交互",
            "compact": {"token_budget": {"current_tokens": 9300, "max_context_tokens": 10000}},
        },
    )

    prompt = _build_subagent_runner_prompt(manager.build_execution_context(task.id))

    assert "Session Compact Package" in prompt
    assert "subagent_session_compact.v1" in prompt
    assert "继续补齐页面交互" in prompt
    assert "latest_metadata.json" in prompt
    assert "latest_summary.md" in prompt
    assert "memory_archive/compact_applies" not in prompt


# LLM: _record_session_compact_result keeps compact package tests focused on assertions.
# 函数用途: 写入一个带 session_compact 信号的 runner result，复用结构化输出和 token budget 形状。
def _record_session_compact_result(
    manager: SubAgentManager,
    run_id: str,
    case: dict[str, object],
) -> None:
    compact = case.get("compact") if isinstance(case.get("compact"), dict) else {}
    compact_payload = {
        "suggested": True,
        "auto_status": "needs_user_confirmation",
        **compact,
    }
    manager.record_runner_result(
        RecordRunnerResultParams(
            run_id=run_id,
            dry_run=False,
            ok=True,
            message=str(compact_payload.get("message") or "runner compacted locally"),
            status="RUNNING",
            verification_status="UNVERIFIED",
            structured_output=SubAgentParsedOutput(
                found=True,
                ok=True,
                status="RUNNING",
                summary=str(case.get("summary") or ""),
                next_actions=[str(case.get("next_action") or "")],
            ),
            session_compact=compact_payload,
        )
    )


# LLM: _session_compact_refs reads the compact package files produced by manager persistence.
# 函数用途: 返回测试断言需要的 metadata、continue packet 和 ledger 行，避免测试函数变长。
def _session_compact_refs(task) -> dict[str, object]:
    latest_metadata = Path(task.agent_run_latest_session_compaction_metadata_json)
    latest_summary = Path(task.agent_run_latest_session_compaction_summary_md)
    packet_ref = Path(task.agent_run_latest_session_continue_packet_json)
    ledger_ref = Path(task.agent_run_session_compaction_ledger_jsonl)
    return {
        "latest_metadata": latest_metadata,
        "latest_summary": latest_summary,
        "metadata": json.loads(latest_metadata.read_text(encoding="utf-8")),
        "packet": json.loads(packet_ref.read_text(encoding="utf-8")),
        "ledger_rows": [json.loads(line) for line in ledger_ref.read_text(encoding="utf-8").splitlines()],
    }
